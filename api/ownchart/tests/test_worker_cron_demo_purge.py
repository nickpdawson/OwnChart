"""The demo visitor-state purge runs on a schedule, not only at startup."""

from __future__ import annotations

from ownchart.workers import vision_extract_job as w


def test_demo_purge_cron_is_registered_hourly():
    names = {c.coroutine.__name__: c for c in w.WorkerSettings.cron_jobs}
    assert "purge_demo_state_cron" in names
    job = names["purge_demo_state_cron"]
    assert job.minute == 30
    assert job.run_at_startup is False


async def test_demo_purge_cron_delegates_and_reports(monkeypatch):
    calls = []

    async def _fake_purge(db, **k):
        calls.append(db)
        return {"conversations": 3, "episodes": 1}

    class _Ctx:
        async def __aenter__(self):
            return "db"

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(w, "SessionLocal", lambda: _Ctx())
    import ownchart.core.demo_data_seed as dds
    monkeypatch.setattr(dds, "purge_stale_demo_state_if_needed", _fake_purge)
    out = await w.purge_demo_state_cron({})
    assert out == {"conversations": 3, "episodes": 1}
    assert calls == ["db"]
