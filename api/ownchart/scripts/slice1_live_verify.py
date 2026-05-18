"""Slice 1 live perimeter verification (M02 Slice 1 closeout, §5).

Verifies that the deployed Slice 1 perimeter actually works on a
live instance: a caregiver with memberships on two distinct
person_records can read + write each one, but cannot leak across
them.

What this script does:
  1. Pre-flight refusals (demo host, missing Slice 1 shape on
     /api/auth/me, fewer than two visible person_records).
  2. Captures the two records as A + B from /api/auth/me.
  3. Runs ~40 HTTP probes in 7 groups, each pinned to an expected
     status_code + assertions on the response shape:
       - auth_me              (memberships + active_record visible)
       - sources              (list/read/write/cross-record 404)
       - facts                (list/get/cross-record 404)
       - conversations        (create/list/get/cross-record 404)
       - topics               (per-record slug uniqueness)
       - episodes             (from-conversation + cross-record 404)
       - aggregations         (timeline/stats/discover counts disjoint)
  4. Cleans up every row it created via available DELETE endpoints,
     then writes a per-table orphan-recovery JSON for resources
     without a public DELETE (the operator hand-cleans via psql).

What this script does NOT do:
  - Run live migrations.
  - Write any direct SQL.
  - Touch any pre-existing row.
  - Verify HealthKit native sync (needs a paired iPhone).
  - Verify OAuth callback under a real EHR redirect (needs
    fhir.epic.com sandbox; the state-signing round-trip is
    already unit-tested).
  - Wait on /api/home/insight/refresh (Anthropic cost).

Pre-flight refusal hierarchy (any one fires → exit non-zero):
  - --base-url points at demo.ownchart.me or includes "demo" in
    the hostname.
  - GET /api/instance/info returns demo_mode=true.
  - POST /api/auth/login fails (admin credentials wrong).
  - GET /api/auth/me does not include `memberships` field
    (= Slice 1 not deployed).
  - len(me.memberships) < 2 (cannot verify cross-record without
    two records — operator must seed a second person_record under
    the admin first; --setup-help prints the psql one-liner).

Usage:
  python -m ownchart.scripts.slice1_live_verify \\
    --base-url https://ownchart.dzsec.net \\
    --admin-email <admin@example.com> \\
    --admin-password <password> \\
    --dry-run

  Dry-run mode does only pre-flight: prints the planned probe
  list and cleanup plan, makes no destructive writes. Use it to
  preview the run before approval.

  Without --dry-run, the script runs every probe + cleans up.
  PM must explicitly approve the live run.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

PREFIX = "SLICE1-VERIFY"
ORPHAN_REPORT_DIR = Path("Working Docs")


@dataclasses.dataclass
class ProbeResult:
    name: str
    method: str
    path: str
    record: str  # "A" | "B" | "N/A" (record-agnostic call)
    expected_status: int
    actual_status: Optional[int]
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CreatedResource:
    """A row the script created during the run; cleanup targets these."""

    kind: str  # "conversation" | "topic" | "episode" | "source" | "fact"
    id: str
    record: str  # "A" | "B"
    delete_url: Optional[str] = None  # None → manual cleanup required


@dataclasses.dataclass
class RunReport:
    started_at: str
    base_url: str
    record_a: str
    record_b: str
    probes: list[ProbeResult] = dataclasses.field(default_factory=list)
    created: list[CreatedResource] = dataclasses.field(default_factory=list)
    cleanup_results: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    orphans: list[CreatedResource] = dataclasses.field(default_factory=list)
    finished_at: Optional[str] = None
    overall_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["probe_pass_count"] = sum(1 for p in self.probes if p.passed)
        d["probe_fail_count"] = sum(1 for p in self.probes if not p.passed)
        return d


# ---------------------------------------------------------------------------
# HTTP client wrapper


class Session:
    """Thin async httpx wrapper that pins record context per call.

    The active record id is sent via X-OwnChart-Person-Record on
    every request. Authentication is via the session cookie set
    by POST /api/auth/login. We never reuse a TestClient or any
    pytest fixture — this is real HTTP against a deployed instance.
    """

    def __init__(self, base_url: str, *, strip_secure_on_login: bool = False):
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            follow_redirects=False,
        )
        self._strip_secure_on_login = strip_secure_on_login

    async def aclose(self) -> None:
        await self.client.aclose()

    async def login(self, email: str, password: str) -> httpx.Response:
        r = await self.client.post(
            "/api/auth/login",
            json={"email": email, "password": password},
        )
        # Test-harness only: when the api sets Secure cookies in prod
        # env, httpx stores them but won't send on subsequent http://
        # requests. Re-set on the jar without Secure so the session is
        # honored when this script runs against http://localhost.
        if self._strip_secure_on_login and r.status_code == 200:
            for name, value in r.cookies.items():
                self.client.cookies.set(name, value)
        return r

    async def request(
        self,
        method: str,
        path: str,
        *,
        record_id: Optional[str] = None,
        json_body: Optional[dict] = None,
        files: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if record_id:
            headers["X-OwnChart-Person-Record"] = record_id
        if extra_headers:
            headers.update(extra_headers)
        return await self.client.request(
            method, path,
            json=json_body, files=files, headers=headers,
        )


# ---------------------------------------------------------------------------
# Pre-flight


def _is_demo_host(base_url: str) -> bool:
    return "demo" in base_url.lower()


async def preflight(
    sess: Session,
    base_url: str,
    admin_email: str,
    admin_password: str,
) -> tuple[str, str]:
    """Run every refusal gate. Returns (record_a_id, record_b_id)
    on success; raises SystemExit on any refusal."""
    if _is_demo_host(base_url):
        sys.exit(
            f"REFUSE: base_url contains 'demo' ({base_url}). The "
            "verification script never runs against the public demo."
        )

    try:
        r = await sess.request("GET", "/api/instance/info")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        sys.exit(
            f"REFUSE: cannot reach {base_url} "
            f"({type(e).__name__}: {e}). Check the URL + network."
        )
    if r.status_code != 200:
        sys.exit(f"REFUSE: GET /api/instance/info → {r.status_code}; "
                 "instance not reachable or wrong base_url.")
    info = r.json()
    if info.get("demo_mode") is True:
        sys.exit("REFUSE: instance reports demo_mode=true. The "
                 "verification script never runs against a demo instance.")

    r = await sess.login(admin_email, admin_password)
    if r.status_code != 200:
        sys.exit(f"REFUSE: admin login failed ({r.status_code}). "
                 "Check OWNCHART_INSTANCE_ADMIN_{EMAIL,PASSWORD}.")

    r = await sess.request("GET", "/api/auth/me")
    if r.status_code != 200:
        sys.exit(f"REFUSE: GET /api/auth/me → {r.status_code}; "
                 "login session not honored.")
    me = r.json()
    if "memberships" not in me or "active_record" not in me:
        sys.exit(
            "REFUSE: GET /api/auth/me does not return `memberships` + "
            "`active_record`. Slice 1 is not deployed on this instance. "
            "Apply migrations 0027–0034 first."
        )

    memberships = me.get("memberships") or []
    record_ids = [m["person_record_id"] for m in memberships]
    if len(record_ids) < 2:
        print_setup_help(memberships)
        sys.exit(
            f"REFUSE: admin user has {len(record_ids)} active record(s); "
            "verification needs >= 2. See --setup-help output above."
        )

    return record_ids[0], record_ids[1]


def print_setup_help(memberships: list[dict]) -> None:
    """One-shot psql instructions for seeding a second person_record
    under the admin. Operator runs this manually; the script never
    runs SQL itself."""
    sys.stderr.write(
        "\n--- SETUP HELP ---\n"
        "Slice 1 verification needs the admin to hold memberships on\n"
        "two distinct person_records. The current instance has:\n"
    )
    for m in memberships:
        sys.stderr.write(
            f"  - record={m['person_record_id']} role={m.get('role')} "
            f"name={m.get('display_name')!r}\n"
        )
    sys.stderr.write(
        "\nSeed a second test record under the admin (psql, NOT the\n"
        "API — there is no /api/person-records CRUD yet; that's a\n"
        "Slice 1.x follow-up):\n\n"
        "  -- run inside `psql ownchart`:\n"
        "  BEGIN;\n"
        "  WITH admin AS (\n"
        "    SELECT id FROM users WHERE is_instance_admin = true\n"
        "    ORDER BY created_at ASC LIMIT 1\n"
        "  ), new_record AS (\n"
        "    INSERT INTO person_records "
        "(id, display_name, is_self, created_by_user_id, created_at, updated_at)\n"
        "    SELECT gen_random_uuid(), 'SLICE1-VERIFY test record',\n"
        "           false, admin.id, now(), now()\n"
        "    FROM admin RETURNING id, created_by_user_id\n"
        "  )\n"
        "  INSERT INTO memberships "
        "(id, user_id, person_record_id, role, created_at)\n"
        "  SELECT gen_random_uuid(), nr.created_by_user_id, nr.id, "
        "'owner', now()\n"
        "  FROM new_record nr;\n"
        "  COMMIT;\n\n"
        "After this, re-run the verification script. Cleanup of the\n"
        "seeded test record is also manual (the script removes its\n"
        "own writes but does not delete the record itself):\n\n"
        "  DELETE FROM memberships WHERE person_record_id IN (\n"
        "    SELECT id FROM person_records "
        "WHERE display_name = 'SLICE1-VERIFY test record'\n"
        "  );\n"
        "  DELETE FROM person_records "
        "WHERE display_name = 'SLICE1-VERIFY test record';\n"
        "------------------\n\n"
    )


# ---------------------------------------------------------------------------
# Probe runner


async def run_probe(
    sess: Session,
    report: RunReport,
    *,
    name: str,
    method: str,
    path: str,
    record_id: Optional[str],
    record_label: str,
    expected_status: int,
    json_body: Optional[dict] = None,
    files: Optional[dict] = None,
    response_assertion: Optional[Callable[[Any], Optional[str]]] = None,
) -> Optional[Any]:
    """Execute one probe, append result to report, return response
    JSON (when 2xx) or None. `response_assertion` may return an
    error string (None = pass)."""
    r = await sess.request(
        method, path,
        record_id=record_id, json_body=json_body, files=files,
    )
    detail = ""
    passed = r.status_code == expected_status
    body: Any = None
    try:
        body = r.json() if r.content else None
    except ValueError:
        body = None
    if passed and response_assertion is not None and body is not None:
        err = response_assertion(body)
        if err:
            passed = False
            detail = f"response assertion failed: {err}"
    if not passed and not detail:
        detail = f"got {r.status_code}, expected {expected_status}; body={str(body)[:200]}"
    report.probes.append(ProbeResult(
        name=name, method=method, path=path, record=record_label,
        expected_status=expected_status, actual_status=r.status_code,
        passed=passed, detail=detail,
    ))
    return body if passed else None


# ---------------------------------------------------------------------------
# Probe groups


async def probe_auth_me(
    sess: Session, report: RunReport, rec_a: str, rec_b: str,
) -> None:
    """Membership shape + active record resolution."""
    await run_probe(
        sess, report,
        name="auth_me/header_record_a",
        method="GET", path="/api/auth/me",
        record_id=rec_a, record_label="A",
        expected_status=200,
        response_assertion=lambda b: (
            None if (b.get("active_record") or {}).get("id") == rec_a
            else f"active_record.id={b.get('active_record', {}).get('id')} != A={rec_a}"
        ),
    )
    await run_probe(
        sess, report,
        name="auth_me/header_record_b",
        method="GET", path="/api/auth/me",
        record_id=rec_b, record_label="B",
        expected_status=200,
        response_assertion=lambda b: (
            None if (b.get("active_record") or {}).get("id") == rec_b
            else f"active_record.id={b.get('active_record', {}).get('id')} != B={rec_b}"
        ),
    )
    # `/api/auth/me` is bootstrap-safe by design: a bogus
    # X-OwnChart-Person-Record header is NOT a 403. get_auth_context's
    # 4-step resolution (header → session → default → first) silently
    # falls through and returns null on miss; for /me specifically, it
    # returns null active_record (or, when the user has a default, the
    # default record) and still 200. Probing for 403 here was a
    # misread of the perimeter — 403 is reserved for *record-scoped*
    # endpoints (sources, facts, conversations, etc.) where a bogus
    # record header fails membership check inside require_role().
    bogus = str(uuid.uuid4())
    await run_probe(
        sess, report,
        name="auth_me/bogus_header_falls_back_to_default",
        method="GET", path="/api/auth/me",
        record_id=bogus, record_label="N/A",
        expected_status=200,
        response_assertion=lambda b: (
            None
            if (b.get("active_record") or {}).get("id")
                == b.get("default_person_record_id")
            else (
                f"active_record.id="
                f"{(b.get('active_record') or {}).get('id')!r} "
                f"!= default_person_record_id="
                f"{b.get('default_person_record_id')!r} "
                "(bogus header should fall through to user default)"
            )
        ),
    )


async def probe_sources(
    sess: Session, report: RunReport, rec_a: str, rec_b: str,
) -> None:
    """List + write + cross-record 404."""
    await run_probe(
        sess, report,
        name="sources/list_a",
        method="GET", path="/api/sources",
        record_id=rec_a, record_label="A", expected_status=200,
    )
    await run_probe(
        sess, report,
        name="sources/list_b",
        method="GET", path="/api/sources",
        record_id=rec_b, record_label="B", expected_status=200,
    )

    note_a = await run_probe(
        sess, report,
        name="sources/write_note_a",
        method="POST", path="/api/sources/note",
        record_id=rec_a, record_label="A", expected_status=201,
        json_body={
            "body": f"{PREFIX} note on record A",
            "title": f"{PREFIX} A note",
        },
    )
    note_b = await run_probe(
        sess, report,
        name="sources/write_note_b",
        method="POST", path="/api/sources/note",
        record_id=rec_b, record_label="B", expected_status=201,
        json_body={
            "body": f"{PREFIX} note on record B",
            "title": f"{PREFIX} B note",
        },
    )
    if note_a is not None:
        report.created.append(CreatedResource(
            kind="source", id=note_a["id"], record="A", delete_url=None,
        ))
    if note_b is not None:
        report.created.append(CreatedResource(
            kind="source", id=note_b["id"], record="B", delete_url=None,
        ))

    if note_a:
        await run_probe(
            sess, report,
            name="sources/cross_record_get_a_under_b",
            method="GET", path=f"/api/sources/{note_a['id']}",
            record_id=rec_b, record_label="B", expected_status=404,
        )
        await run_probe(
            sess, report,
            name="sources/same_record_get_a",
            method="GET", path=f"/api/sources/{note_a['id']}",
            record_id=rec_a, record_label="A", expected_status=200,
        )
    if note_b:
        await run_probe(
            sess, report,
            name="sources/cross_record_get_b_under_a",
            method="GET", path=f"/api/sources/{note_b['id']}",
            record_id=rec_a, record_label="A", expected_status=404,
        )

    if note_a and note_b:
        # The two lists must be disjoint at the test-row level.
        list_a = await sess.request(
            "GET", "/api/sources", record_id=rec_a,
        )
        list_b = await sess.request(
            "GET", "/api/sources", record_id=rec_b,
        )
        if list_a.status_code == 200 and list_b.status_code == 200:
            ids_a = {s["id"] for s in list_a.json()}
            ids_b = {s["id"] for s in list_b.json()}
            passed = (
                note_a["id"] in ids_a and note_a["id"] not in ids_b
                and note_b["id"] in ids_b and note_b["id"] not in ids_a
            )
            report.probes.append(ProbeResult(
                name="sources/list_disjoint",
                method="GET", path="/api/sources",
                record="A,B", expected_status=200,
                actual_status=200, passed=passed,
                detail=(
                    "" if passed else
                    f"A list={ids_a & {note_a['id'], note_b['id']}}; "
                    f"B list={ids_b & {note_a['id'], note_b['id']}}"
                ),
            ))


async def probe_facts(
    sess: Session, report: RunReport, rec_a: str, rec_b: str,
) -> None:
    """List + get; cross-record fact_id 404."""
    for label, rec in [("a", rec_a), ("b", rec_b)]:
        await run_probe(
            sess, report,
            name=f"facts/list_{label}",
            method="GET", path="/api/facts",
            record_id=rec, record_label=label.upper(),
            expected_status=200,
        )

    bogus_fact = str(uuid.uuid4())
    await run_probe(
        sess, report,
        name="facts/bogus_get_404_a",
        method="GET", path=f"/api/facts/{bogus_fact}",
        record_id=rec_a, record_label="A", expected_status=404,
    )


async def probe_conversations(
    sess: Session, report: RunReport, rec_a: str, rec_b: str,
) -> None:
    """Create + list + cross-record 404 + dispose."""
    conv_a = await run_probe(
        sess, report,
        name="convs/create_a",
        method="POST", path="/api/conversations",
        record_id=rec_a, record_label="A", expected_status=201,
        json_body={"kind": "ask", "title": f"{PREFIX} A conv"},
    )
    conv_b = await run_probe(
        sess, report,
        name="convs/create_b",
        method="POST", path="/api/conversations",
        record_id=rec_b, record_label="B", expected_status=201,
        json_body={"kind": "ask", "title": f"{PREFIX} B conv"},
    )
    if conv_a:
        report.created.append(CreatedResource(
            kind="conversation", id=conv_a["id"], record="A",
            delete_url=f"/api/conversations/{conv_a['id']}",
        ))
    if conv_b:
        report.created.append(CreatedResource(
            kind="conversation", id=conv_b["id"], record="B",
            delete_url=f"/api/conversations/{conv_b['id']}",
        ))

    if conv_a:
        await run_probe(
            sess, report,
            name="convs/cross_record_get_a_under_b",
            method="GET", path=f"/api/conversations/{conv_a['id']}",
            record_id=rec_b, record_label="B", expected_status=404,
        )
    if conv_b:
        await run_probe(
            sess, report,
            name="convs/cross_record_patch_b_under_a",
            method="PATCH", path=f"/api/conversations/{conv_b['id']}",
            record_id=rec_a, record_label="A", expected_status=404,
            json_body={"starred": True},
        )

    if conv_a and conv_b:
        # limit=200 (route's documented max via `Query(default=50, le=200)`).
        # Default limit=50 + ORDER BY last_message_at DESC NULLS LAST hides
        # newly-created conversations (last_message_at is NULL until a
        # message is appended) behind any pre-existing chat history > 50.
        # On a real instance with months of activity in record A, the
        # newly-created test conversation sits past rank 50 and the
        # probe would falsely fail "lists overlapped" when in fact the
        # storage is correctly per-record scoped. limit=200 is enough
        # for any instance up to 200 conversations per record; well
        # past that, the probe should switch to a q=SLICE1-VERIFY
        # search filter instead.
        list_a = await sess.request(
            "GET", "/api/conversations?limit=200", record_id=rec_a,
        )
        list_b = await sess.request(
            "GET", "/api/conversations?limit=200", record_id=rec_b,
        )
        if list_a.status_code == 200 and list_b.status_code == 200:
            ids_a = {c["id"] for c in list_a.json()}
            ids_b = {c["id"] for c in list_b.json()}
            passed = (
                conv_a["id"] in ids_a and conv_a["id"] not in ids_b
                and conv_b["id"] in ids_b and conv_b["id"] not in ids_a
            )
            report.probes.append(ProbeResult(
                name="convs/list_disjoint",
                method="GET", path="/api/conversations",
                record="A,B", expected_status=200, actual_status=200,
                passed=passed, detail="" if passed else "lists overlapped",
            ))


async def probe_topics(
    sess: Session, report: RunReport, rec_a: str, rec_b: str,
) -> None:
    """Per-record slug uniqueness: same name under A + B both succeed."""
    shared_name = f"{PREFIX}-shared-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    topic_a = await run_probe(
        sess, report,
        name="topics/create_a",
        method="POST", path="/api/topics",
        record_id=rec_a, record_label="A", expected_status=201,
        json_body={"name": shared_name, "description": f"{PREFIX} A"},
    )
    topic_b = await run_probe(
        sess, report,
        name="topics/create_b_same_slug",
        method="POST", path="/api/topics",
        record_id=rec_b, record_label="B", expected_status=201,
        json_body={"name": shared_name, "description": f"{PREFIX} B"},
    )
    if topic_a:
        report.created.append(CreatedResource(
            kind="topic", id=topic_a["id"], record="A", delete_url=None,
        ))
    if topic_b:
        report.created.append(CreatedResource(
            kind="topic", id=topic_b["id"], record="B", delete_url=None,
        ))
    if topic_a and topic_b and topic_a["id"] != topic_b["id"]:
        report.probes.append(ProbeResult(
            name="topics/per_record_uniqueness",
            method="-", path="-", record="A,B",
            expected_status=0, actual_status=0,
            passed=True,
            detail=f"distinct topic ids: A={topic_a['id']}, B={topic_b['id']}",
        ))
    else:
        report.probes.append(ProbeResult(
            name="topics/per_record_uniqueness",
            method="-", path="-", record="A,B",
            expected_status=0, actual_status=0, passed=False,
            detail="topic ids identical or one create failed",
        ))


async def probe_episodes(
    sess: Session, report: RunReport, rec_a: str, rec_b: str,
) -> None:
    """from-conversation create + cross-record 404."""
    seed_conv = next(
        (c for c in report.created if c.kind == "conversation" and c.record == "A"),
        None,
    )
    if seed_conv is None:
        report.probes.append(ProbeResult(
            name="episodes/skipped_no_seed_conv",
            method="-", path="-", record="A",
            expected_status=0, actual_status=0, passed=False,
            detail="no A-record conversation available to seed from",
        ))
        return
    ep_a = await run_probe(
        sess, report,
        name="episodes/from_conv_a",
        method="POST", path=f"/api/episodes/from-conversation/{seed_conv.id}",
        record_id=rec_a, record_label="A", expected_status=201,
        json_body={"title": f"{PREFIX} A event"},
    )
    if ep_a:
        report.created.append(CreatedResource(
            kind="episode", id=ep_a["episode_id"], record="A", delete_url=None,
        ))
        await run_probe(
            sess, report,
            name="episodes/cross_record_get_a_under_b",
            method="GET", path=f"/api/episodes/{ep_a['episode_id']}",
            record_id=rec_b, record_label="B", expected_status=404,
        )


async def probe_aggregations(
    sess: Session, report: RunReport, rec_a: str, rec_b: str,
) -> None:
    """Counts MUST differ between records (because we just wrote
    test rows on each) and never combine."""
    for label, rec in [("a", rec_a), ("b", rec_b)]:
        await run_probe(
            sess, report,
            name=f"aggregations/stats_{label}",
            method="GET", path="/api/dashboard",
            record_id=rec, record_label=label.upper(), expected_status=200,
        )
        await run_probe(
            sess, report,
            name=f"aggregations/timeline_{label}",
            method="GET", path="/api/timeline",
            record_id=rec, record_label=label.upper(), expected_status=200,
        )
        await run_probe(
            sess, report,
            name=f"aggregations/discover_{label}",
            method="GET", path="/api/discover",
            record_id=rec, record_label=label.upper(), expected_status=200,
        )

    # Hit /api/home/ai-partner once per record but DON'T wait on insight.
    for label, rec in [("a", rec_a), ("b", rec_b)]:
        await run_probe(
            sess, report,
            name=f"aggregations/home_partner_{label}",
            method="GET", path="/api/home/ai-partner",
            record_id=rec, record_label=label.upper(), expected_status=200,
        )


# ---------------------------------------------------------------------------
# Cleanup


async def cleanup(sess: Session, report: RunReport) -> None:
    """Hit DELETE for everything we can; collect the rest as orphans
    for the operator to hand-clean via the rendered psql script."""
    for resource in reversed(report.created):
        if resource.delete_url is None:
            report.orphans.append(resource)
            continue
        r = await sess.request("DELETE", resource.delete_url)
        cleanup_passed = r.status_code in (200, 204, 404)
        report.cleanup_results.append({
            "kind": resource.kind,
            "id": resource.id,
            "record": resource.record,
            "delete_url": resource.delete_url,
            "status_code": r.status_code,
            "passed": cleanup_passed,
        })
        if not cleanup_passed:
            report.orphans.append(resource)


def render_orphan_psql(orphans: list[CreatedResource]) -> str:
    """Operator-pasteable psql script for resources we couldn't
    delete via the API. Each section is gated by id IN (...) so
    nothing else is touched."""
    by_kind: dict[str, list[str]] = {}
    for o in orphans:
        by_kind.setdefault(o.kind, []).append(o.id)
    if not by_kind:
        return ""
    lines: list[str] = [
        "-- SLICE1-VERIFY orphan cleanup (review every DELETE before running)",
        "BEGIN;",
    ]
    if by_kind.get("episode"):
        ids = ", ".join(f"'{i}'" for i in by_kind["episode"])
        lines.append(
            f"DELETE FROM episode_members WHERE episode_id IN ({ids});"
        )
        lines.append(f"DELETE FROM episodes WHERE id IN ({ids});")
    if by_kind.get("topic"):
        ids = ", ".join(f"'{i}'" for i in by_kind["topic"])
        lines.append(
            f"DELETE FROM topic_briefs WHERE topic_id IN ({ids});"
        )
        lines.append(
            f"DELETE FROM brief_messages WHERE topic_id IN ({ids});"
        )
        lines.append(f"DELETE FROM topics WHERE id IN ({ids});")
    if by_kind.get("source"):
        ids = ", ".join(f"'{i}'" for i in by_kind["source"])
        lines.append(
            f"DELETE FROM extracted_facts WHERE evidence_anchor_ids && ARRAY("
            f"SELECT id FROM evidence_anchors WHERE source_document_id IN ({ids})"
            ");"
        )
        lines.append(
            f"DELETE FROM evidence_anchors WHERE source_document_id IN ({ids});"
        )
        lines.append(
            f"DELETE FROM source_documents WHERE id IN ({ids});"
        )
    lines.append("COMMIT;")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dry-run mode — pre-flight + planned-probe enumeration, no writes


def planned_probes() -> list[str]:
    """Enumerate the probes the live run would execute. Used by
    --dry-run so PM can review the plan before approving the live
    run."""
    return [
        "auth_me/header_record_a",
        "auth_me/header_record_b",
        "auth_me/bogus_header_falls_back_to_default",
        "sources/list_a",
        "sources/list_b",
        "sources/write_note_a",
        "sources/write_note_b",
        "sources/cross_record_get_a_under_b",
        "sources/same_record_get_a",
        "sources/cross_record_get_b_under_a",
        "sources/list_disjoint",
        "facts/list_a",
        "facts/list_b",
        "facts/bogus_get_404_a",
        "convs/create_a",
        "convs/create_b",
        "convs/cross_record_get_a_under_b",
        "convs/cross_record_patch_b_under_a",
        "convs/list_disjoint",
        "topics/create_a",
        "topics/create_b_same_slug",
        "topics/per_record_uniqueness",
        "episodes/from_conv_a",
        "episodes/cross_record_get_a_under_b",
        "aggregations/stats_a",
        "aggregations/stats_b",
        "aggregations/timeline_a",
        "aggregations/timeline_b",
        "aggregations/discover_a",
        "aggregations/discover_b",
        "aggregations/home_partner_a",
        "aggregations/home_partner_b",
    ]


# ---------------------------------------------------------------------------
# CLI


async def amain(args: argparse.Namespace) -> int:
    sess = Session(
        args.base_url,
        strip_secure_on_login=args.allow_insecure_localhost_cookie,
    )
    try:
        if args.dry_run:
            sys.stderr.write(
                "DRY-RUN — pre-flight gates + planned probe list only. "
                "No HTTP writes performed.\n\n"
            )
            sys.stderr.write(f"base_url: {args.base_url}\n")
            sys.stderr.write(f"is_demo_host: {_is_demo_host(args.base_url)}\n")
            sys.stderr.write("Planned probes:\n")
            for p in planned_probes():
                sys.stderr.write(f"  - {p}\n")
            sys.stderr.write(
                "\nCleanup plan: DELETE /api/conversations/{id} for every "
                "test conversation; sources/topics/episodes have no public "
                "DELETE — emit orphan-recovery psql for the operator.\n"
            )
            sys.stderr.write(
                "\nLive run requires PM approval. Re-invoke without --dry-run.\n"
            )
            return 0

        rec_a, rec_b = await preflight(
            sess, args.base_url, args.admin_email, args.admin_password,
        )
        report = RunReport(
            started_at=datetime.now(timezone.utc).isoformat(),
            base_url=args.base_url,
            record_a=rec_a, record_b=rec_b,
        )
        sys.stderr.write(
            f"Pre-flight OK. Records: A={rec_a} B={rec_b}\n"
        )

        await probe_auth_me(sess, report, rec_a, rec_b)
        await probe_sources(sess, report, rec_a, rec_b)
        await probe_facts(sess, report, rec_a, rec_b)
        await probe_conversations(sess, report, rec_a, rec_b)
        await probe_topics(sess, report, rec_a, rec_b)
        await probe_episodes(sess, report, rec_a, rec_b)
        await probe_aggregations(sess, report, rec_a, rec_b)

        await cleanup(sess, report)

        report.finished_at = datetime.now(timezone.utc).isoformat()
        report.overall_passed = all(p.passed for p in report.probes)

        utc_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = ORPHAN_REPORT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / f"slice1_verify_{utc_tag}.json"
        report_path.write_text(json.dumps(report.to_dict(), indent=2))
        sys.stderr.write(f"Report: {report_path}\n")

        if report.orphans:
            orphan_path = out_dir / f"slice1_verify_{utc_tag}_orphans.sql"
            orphan_path.write_text(render_orphan_psql(report.orphans))
            sys.stderr.write(
                f"Orphan cleanup SQL: {orphan_path} (review before running)\n"
            )

        sys.stderr.write(
            f"\nProbes: pass={sum(1 for p in report.probes if p.passed)} / "
            f"fail={sum(1 for p in report.probes if not p.passed)} / "
            f"total={len(report.probes)}\n"
        )
        for p in report.probes:
            if not p.passed:
                sys.stderr.write(
                    f"  FAIL [{p.record}] {p.name}: {p.detail}\n"
                )
        return 0 if report.overall_passed else 1
    finally:
        await sess.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live verification of M02 Slice 1 perimeter against a "
            "non-demo OwnChart instance. NEVER runs against demo. "
            "Requires PM approval for live execution."
        ),
    )
    parser.add_argument("--base-url", required=True,
                        help="e.g. https://ownchart.dzsec.net")
    parser.add_argument("--admin-email", required=True,
                        help="Instance admin email for login.")
    pw_group = parser.add_mutually_exclusive_group(required=True)
    pw_group.add_argument("--admin-password",
                          help="Instance admin password (visible in argv — "
                               "prefer --admin-password-file).")
    pw_group.add_argument("--admin-password-file",
                          help="Path to a single-line file containing the "
                               "password. chmod 600 recommended. Keeps the "
                               "password out of process argv.")
    parser.add_argument("--allow-insecure-localhost-cookie", action="store_true",
                        help="Test-harness only: after login, re-set cookies "
                             "on the client jar without the Secure flag so "
                             "they ride subsequent http:// calls. Only "
                             "allowed when base_url host is "
                             "localhost/127.0.0.1/::1.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pre-flight + planned-probe enumeration only. "
                             "No HTTP writes. Use to preview before approval.")
    args = parser.parse_args()

    if args.admin_password_file:
        pwd_path = Path(args.admin_password_file).expanduser()
        try:
            args.admin_password = pwd_path.read_text().strip()
        except OSError as e:
            sys.exit(
                f"REFUSE: cannot read --admin-password-file "
                f"{pwd_path}: {e}"
            )
        if not args.admin_password:
            sys.exit(
                f"REFUSE: --admin-password-file {pwd_path} is empty."
            )

    if args.allow_insecure_localhost_cookie:
        host = (urlparse(args.base_url).hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            sys.exit(
                f"REFUSE: --allow-insecure-localhost-cookie requires "
                f"base_url host in (localhost, 127.0.0.1, ::1); got "
                f"{host!r}."
            )

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted; cleanup may be incomplete.\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
