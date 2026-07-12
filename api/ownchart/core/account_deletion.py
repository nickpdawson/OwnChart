"""Account deletion (5.1.1(v)) — purge a user + the data they solely own.

App Store guideline 5.1.1(v) requires an in-app path to delete the
account and its data. This module owns the backend half: the
transactional cascade that a ``DELETE /api/account`` route drives after
verifying the caller's password.

Semantics (PM-scoped, mirrors OneApp's approved deletion shape):

  - A PersonRecord the deleting user **solely owns** (they are an active
    owner OR its creator, and there is no OTHER active owner) is fully
    purged. Postgres ``ON DELETE CASCADE`` fans the delete out to every
    person-scoped table: source_documents, extracted_facts,
    evidence_anchors, topics, topic_briefs, brief_messages, episodes +
    members, conversations + messages + citations, extraction_jobs,
    sensemaking jobs/candidates, calendar sources/events/creds,
    auto_export tokens, invitations, provider connections, oauth
    sessions, user_assertions, exports.

  - A **co-owned** record (another user still holds an active *owner*
    membership) SURVIVES. The deleting user's own membership is removed
    by the ``memberships.user_id`` CASCADE when the user row goes. If the
    deleting user was the record's creator, provenance
    (``created_by_user_id``, a RESTRICT FK) is transferred to a surviving
    owner so the user delete isn't blocked.

  - The user row is then deleted. ``ON DELETE CASCADE`` fans out to the
    user-scoped tables (device_tokens, user_settings, healthkit_cursors,
    llm_provider_credentials, memberships, and the user's own
    conversations/episodes/sensemaking/exports/provider-connections/
    oauth-sessions/calendar/auto-export tokens/invitations they issued).
    Cookie (web) sessions die because the user row no longer resolves;
    device bearer tokens are cascaded away.

The three RESTRICT-to-``users`` authored tables (brief_messages,
extraction_jobs, user_assertions) are cascaded for purged records;
their survivor-scoped remainders authored by the deleting user are
deleted explicitly here so the RESTRICT FK can't block the user delete.

**Blob GC is a deliberate follow-up.** The content-addressed evidence
vault on disk is deduped across records — a blob may be referenced by a
record that survives — so orphaned-blob garbage collection is NOT done
here. The DB purge is the primary, verifiable requirement; on-disk GC
is a separate sweep. See the route docstring.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.brief_message import BriefMessage
from ..models.extraction_job import ExtractionJob
from ..models.membership import Membership
from ..models.person_record import PersonRecord
from ..models.user import User
from ..models.user_assertion import UserAssertion
from .logger import get_logger

log = get_logger("ownchart.core.account_deletion")


@dataclass(frozen=True)
class RecordDisposition:
    """Plan for what happens to each candidate PersonRecord."""

    purge: list[uuid.UUID] = field(default_factory=list)
    survive: list[uuid.UUID] = field(default_factory=list)
    # record_id -> new creator (a surviving owner) for provenance transfer.
    reassign_created_by: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)


def plan_record_dispositions(
    *,
    deleting_user_id: uuid.UUID,
    candidate_records: set[uuid.UUID],
    active_owners_by_record: dict[uuid.UUID, set[uuid.UUID]],
    created_by_by_record: dict[uuid.UUID, uuid.UUID],
) -> RecordDisposition:
    """Classify each candidate record as purge vs survive (pure).

    ``candidate_records`` = records where the deleting user is an active
    OWNER or the creator. A record with any OTHER active owner survives
    (and gets a provenance transfer if the deleting user created it);
    otherwise it is solely owned and purged.

    Deterministic ordering (uuid string sort) so the plan — and any test
    asserting on it — is stable.
    """
    disposition = RecordDisposition()
    for rid in sorted(candidate_records, key=str):
        other_owners = set(active_owners_by_record.get(rid, set())) - {deleting_user_id}
        if other_owners:
            disposition.survive.append(rid)
            if created_by_by_record.get(rid) == deleting_user_id:
                disposition.reassign_created_by[rid] = sorted(other_owners, key=str)[0]
        else:
            disposition.purge.append(rid)
    return disposition


async def delete_user_account(db: AsyncSession, user: User) -> dict[str, int]:
    """Purge the user and the records they solely own, in one transaction.

    Returns count summary (no PII) for logging / the audit trail. The
    caller is responsible for having verified the password and for
    clearing the session cookie on the response.
    """
    uid = user.id

    # 1. Candidate records: the user is an active OWNER, or the creator.
    owner_record_ids = set((await db.execute(
        select(Membership.person_record_id)
        .where(Membership.user_id == uid)
        .where(Membership.role == "owner")
        .where(Membership.revoked_at.is_(None))
    )).scalars().all())
    created_record_ids = set((await db.execute(
        select(PersonRecord.id).where(PersonRecord.created_by_user_id == uid)
    )).scalars().all())
    candidate_ids = owner_record_ids | created_record_ids

    # 2. For each candidate, the full set of active owners + its creator.
    active_owners_by_record: dict[uuid.UUID, set[uuid.UUID]] = {}
    created_by_by_record: dict[uuid.UUID, uuid.UUID] = {}
    if candidate_ids:
        for rid, mem_uid in (await db.execute(
            select(Membership.person_record_id, Membership.user_id)
            .where(Membership.person_record_id.in_(candidate_ids))
            .where(Membership.role == "owner")
            .where(Membership.revoked_at.is_(None))
        )).all():
            active_owners_by_record.setdefault(rid, set()).add(mem_uid)
        for rid, cb in (await db.execute(
            select(PersonRecord.id, PersonRecord.created_by_user_id)
            .where(PersonRecord.id.in_(candidate_ids))
        )).all():
            created_by_by_record[rid] = cb

    plan = plan_record_dispositions(
        deleting_user_id=uid,
        candidate_records=candidate_ids,
        active_owners_by_record=active_owners_by_record,
        created_by_by_record=created_by_by_record,
    )

    # 3. Transfer creation provenance on SURVIVING co-owned records so the
    #    person_records.created_by_user_id RESTRICT FK can't block the
    #    user delete.
    for rid, new_owner in plan.reassign_created_by.items():
        await db.execute(
            update(PersonRecord)
            .where(PersonRecord.id == rid)
            .values(created_by_user_id=new_owner)
        )

    # 4. Purge solely-owned records — Postgres CASCADE fans out to every
    #    person-scoped table.
    purged_records = 0
    if plan.purge:
        res = await db.execute(
            delete(PersonRecord).where(PersonRecord.id.in_(plan.purge))
        )
        purged_records = res.rowcount if res.rowcount is not None else len(plan.purge)

    # 5. Remove the deleting user's RESTRICT-to-users authored rows that
    #    remain on co-owned survivors (purge-set rows already cascaded).
    #    Left in place they would block the user delete.
    for model in (BriefMessage, ExtractionJob, UserAssertion):
        await db.execute(delete(model).where(model.user_id == uid))

    # 6. Delete the user. CASCADE handles user-scoped rows; SET NULL nulls
    #    audit-event actors and other users' invited_by / default-record
    #    references that pointed at this user.
    await db.execute(delete(User).where(User.id == uid))
    await db.commit()

    summary = {
        "purged_person_records": int(purged_records),
        "survived_person_records": len(plan.survive),
        "reassigned_created_by": len(plan.reassign_created_by),
    }
    log.info("account_deleted", user_id=str(uid), **summary)
    return summary
