"""Batch A item 3 — evidence-blob GC on account deletion (GDPR Art. 17).

Three layers:

  1. Pure ``plan_blob_gc`` — the reference-count decision: unlink a blob
     only when NO surviving source_document references its hash.
  2. ``_unlink_blobs`` — filesystem behavior: removes vault files, refuses
     paths outside the vault, and never raises on failure.
  3. ``delete_user_account`` end-to-end (DB session faked): the surviving-
     reference query gates the unlink and ``removed_blobs`` is reported.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from ownchart.core import account_deletion as ad
from ownchart.core.account_deletion import (
    _unlink_blobs,
    delete_user_account,
    plan_blob_gc,
)


# ---------------------------------------------------------------------------
# 1. plan_blob_gc — pure reference check


def test_unshared_blob_is_unlinked():
    plan = plan_blob_gc({"sha256:aaa": {"/vault/aa/aa/aaa.jpg"}}, surviving_hashes=set())
    assert plan == ["/vault/aa/aa/aaa.jpg"]


def test_blob_shared_with_survivor_is_kept():
    """The load-bearing guarantee: a blob whose hash is still referenced by
    a surviving (co-owned or other-user) record is NOT unlinked."""
    plan = plan_blob_gc(
        {"sha256:shared": {"/vault/sh/ar/shared.pdf"}},
        surviving_hashes={"sha256:shared"},
    )
    assert plan == []


def test_mixed_set_partitions_by_surviving_hash():
    plan = plan_blob_gc(
        {
            "sha256:gone": {"/vault/go/ne/gone.jpg"},
            "sha256:kept": {"/vault/ke/pt/kept.jpg"},
        },
        surviving_hashes={"sha256:kept"},
    )
    assert plan == ["/vault/go/ne/gone.jpg"]


def test_same_hash_multiple_suffixes_all_unlinked_when_unshared():
    plan = plan_blob_gc(
        {"sha256:x": {"/vault/x/x/x.jpg", "/vault/x/x/x.png"}},
        surviving_hashes=set(),
    )
    assert plan == ["/vault/x/x/x.jpg", "/vault/x/x/x.png"]


def test_empty_inputs_yield_empty_plan():
    assert plan_blob_gc({}, set()) == []


# ---------------------------------------------------------------------------
# 2. _unlink_blobs — filesystem


def _point_vault_at(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ad, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path),
    )
    return (tmp_path / "evidence").resolve()


def test_unlink_removes_vault_files(monkeypatch, tmp_path):
    evidence = _point_vault_at(monkeypatch, tmp_path)
    blob = evidence / "ab" / "cd" / "abcd.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"phi-bytes")
    assert blob.exists()

    removed = _unlink_blobs([str(blob)])
    assert removed == 1
    assert not blob.exists()


def test_unlink_refuses_paths_outside_vault(monkeypatch, tmp_path):
    _point_vault_at(monkeypatch, tmp_path)
    outsider = tmp_path / "not-the-vault.txt"
    outsider.write_bytes(b"keep me")

    removed = _unlink_blobs([str(outsider)])
    assert removed == 0
    # A corrupt storage_uri must NOT delete files outside the vault.
    assert outsider.exists()


def test_unlink_never_raises_on_failure(monkeypatch, tmp_path):
    """A failed unlink logs + continues; it must never fail the deletion.
    Passing a directory path makes Path.unlink raise IsADirectoryError,
    which the helper must swallow."""
    evidence = _point_vault_at(monkeypatch, tmp_path)
    a_dir = evidence / "de" / "ad" / "deadbeef"  # a directory, not a file
    a_dir.mkdir(parents=True)

    # Must not raise; the directory is left alone and not counted.
    removed = _unlink_blobs([str(a_dir)])
    assert removed == 0
    assert a_dir.exists()


def test_unlink_tolerates_already_missing_file(monkeypatch, tmp_path):
    evidence = _point_vault_at(monkeypatch, tmp_path)
    ghost = evidence / "gh" / "os" / "ghost.jpg"
    ghost.parent.mkdir(parents=True)
    # File never created — unlink(missing_ok=True) is a tolerated no-op.
    removed = _unlink_blobs([str(ghost)])
    assert removed == 1  # counted as removed; idempotent


# ---------------------------------------------------------------------------
# 3. delete_user_account — GC gated by surviving-reference query


class _Res:
    def __init__(self, rows=None, rowcount=None):
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SeqSession:
    """Session that returns a pre-scripted result for each execute() call,
    in order. Mirrors the exact read/write sequence delete_user_account
    performs so the GC path can be exercised without Postgres."""

    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


async def test_delete_account_gcs_unshared_blob_keeps_shared(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    other = uuid.uuid4()
    rec_solo = uuid.uuid4()
    rec_shared = uuid.uuid4()

    sha_gone, uri_gone = "sha256:gone", "/vault/go/ne/gone.jpg"
    sha_keep, uri_keep = "sha256:keep", "/vault/ke/ep/keep.pdf"

    # Scripted, in the order delete_user_account issues them:
    results = [
        _Res(rows=[rec_solo, rec_shared]),                 # 1 owner memberships
        _Res(rows=[rec_solo]),                             # 2 created records
        _Res(rows=[                                        # 3 active owners
            (rec_solo, uid), (rec_shared, uid), (rec_shared, other),
        ]),
        _Res(rows=[                                        # 4 created_by
            (rec_solo, uid), (rec_shared, other),
        ]),
        _Res(rows=[                                        # 5 purge blobs
            (sha_gone, uri_gone), (sha_keep, uri_keep),
        ]),
        _Res(rowcount=1),                                  # 6 delete PersonRecord
        _Res(),                                            # 7 delete BriefMessage
        _Res(),                                            # 8 delete ExtractionJob
        _Res(),                                            # 9 delete UserAssertion
        _Res(),                                            # 10 delete User
        # 11 commit (no execute)
        _Res(rows=[sha_keep]),                             # 12 surviving hashes
    ]
    session = _SeqSession(results)

    captured = {}

    def _fake_unlink(uris):
        captured["uris"] = list(uris)
        return len(uris)

    monkeypatch.setattr(ad, "_unlink_blobs", _fake_unlink)

    user = SimpleNamespace(id=uid)
    summary = await delete_user_account(session, user)  # type: ignore[arg-type]

    # rec_solo purged, rec_shared survived.
    assert summary["purged_person_records"] == 1
    assert summary["survived_person_records"] == 1
    # Only the unshared blob is unlinked; the shared one is protected by the
    # surviving-reference query returning its hash.
    assert captured["uris"] == [uri_gone]
    assert summary["removed_blobs"] == 1
    assert session.commits == 1
