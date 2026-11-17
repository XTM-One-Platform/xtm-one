"""On-demand storage purge: the retention applied now, dry run first, never the last hour."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services import storage_purge

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def test_the_cutoff_is_the_threshold_and_never_the_last_hour():
    assert storage_purge.cutoff(7, now=NOW) == NOW - timedelta(days=7)
    # 0 days is everything past the floor: work in flight is never purged.
    assert storage_purge.cutoff(0, now=NOW) == NOW - storage_purge.FLOOR
    assert storage_purge.cutoff(-3, now=NOW) == NOW - storage_purge.FLOOR


def _stills(monkeypatch, listing):
    deleted: list[str] = []

    async def list_objects_modified(_prefix):
        return listing

    async def delete(name):
        deleted.append(name)

    from app.services import file_storage as module

    monkeypatch.setattr(module.file_storage, "list_objects_modified", list_objects_modified, raising=False)
    monkeypatch.setattr(module.file_storage, "delete", delete, raising=False)
    return deleted


def test_a_dry_run_counts_the_old_stills_and_deletes_none(monkeypatch):
    listing = [("old-a", NOW - timedelta(days=3)), ("old-b", NOW - timedelta(days=2)), ("new", NOW), ("none", None)]
    deleted = _stills(monkeypatch, listing)
    before = storage_purge.cutoff(1, now=NOW)
    tally = asyncio.run(storage_purge._figure_stills(before, dry_run=True, limit=10))
    assert tally == {"count": 2, "bytes": None, "more": False} and deleted == []
    applied = asyncio.run(storage_purge._figure_stills(before, dry_run=False, limit=1))
    assert applied["count"] == 1 and applied["more"] is True and deleted == ["old-a"]


def test_purge_reports_per_category_and_the_totals(monkeypatch):
    async def check(_db, before, *, dry_run, limit):
        assert before == NOW - timedelta(days=2) and dry_run and limit == 100
        return {"count": 3, "bytes": 300, "kept_in_use": 1, "objects_left": 0, "more": False}

    async def stills(_before, *, dry_run, limit):
        return {"count": 4, "bytes": None, "more": True}

    monkeypatch.setattr(storage_purge, "_agent_check", check)
    monkeypatch.setattr(storage_purge, "_figure_stills", stills)
    report = asyncio.run(
        storage_purge.purge(
            None, older_than_days=2, categories=["agent_check", "figure_stills", "bogus"], limit=100, now=NOW
        )
    )
    assert list(report["categories"]) == ["agent_check", "figure_stills"]
    assert report["dry_run"] is True and report["total_bytes"] == 300 and report["more"] is True


# ── The tool ──────────────────────────────────────────────────────────────


class _Session:
    committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        self.committed = True


def _tool(monkeypatch, *, admin):
    from app.services.builtin_tools import platform_admin

    session = _Session()
    calls: list[dict] = []
    audits: list[dict] = []

    async def _admin(_db, _user_id):
        return SimpleNamespace(id="u", is_admin=True) if admin else None

    async def purge(_db, **named):
        calls.append(named)
        return {"dry_run": named["dry_run"], "categories": {}, "total_bytes": 0, "more": False}

    async def audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(platform_admin, "_admin", _admin)
    monkeypatch.setattr(storage_purge, "purge", purge)
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    _defs, handlers = platform_admin.build_platform_admin_tools("00000000-0000-4000-8000-000000000001")
    return handlers[platform_admin.PURGE_PLATFORM_STORAGE], calls, audits, session


def test_a_non_admin_gets_403_and_nothing_runs(monkeypatch):
    handler, calls, audits, _session = _tool(monkeypatch, admin=False)
    assert asyncio.run(handler(older_than_days=0)).startswith("Error 403")
    assert calls == [] and audits == []


def test_the_tool_is_a_dry_run_unless_told_otherwise_and_audits_a_real_purge(monkeypatch):
    handler, calls, audits, session = _tool(monkeypatch, admin=True)
    answer = json.loads(asyncio.run(handler(older_than_days=0)))
    assert answer["dry_run"] is True and calls[-1]["dry_run"] is True and audits == [] and not session.committed
    asyncio.run(handler(older_than_days=7, dry_run=False, categories=["agent_check"]))
    assert calls[-1]["dry_run"] is False and calls[-1]["categories"] == ["agent_check"]
    assert audits and audits[0]["details"]["via"] == "purge_platform_storage" and session.committed


def test_the_tool_refuses_a_bad_threshold_or_category(monkeypatch):
    handler, calls, _audits, _session = _tool(monkeypatch, admin=True)
    assert asyncio.run(handler()).startswith("Error: older_than_days is required")
    assert asyncio.run(handler(older_than_days=-1)).startswith("Error")
    assert "unknown categories" in asyncio.run(handler(older_than_days=1, categories=["renders"]))
    assert calls == []
