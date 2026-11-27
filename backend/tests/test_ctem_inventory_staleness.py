# SPDX-License-Identifier: Apache-2.0
"""Path 6 evaluates the inventory as it stands and says why it records nothing (#2642)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_inventory_staleness as staleness
from app.services import ctem_signal_sources as sources
from app.services import ctem_signals

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _record(name: str, *, days: int | None, sync_id: str = "") -> Any:
    seen = None if days is None else NOW - timedelta(days=days)
    return SimpleNamespace(id=uuid.uuid4(), name=name, source="openaev", sync_id=sync_id, last_seen_at=seen)


def _sync(*, error: str = "", paused: str = "") -> Any:
    return SimpleNamespace(id=uuid.uuid4(), paused_reason=paused, last_result={"error": error} if error else {})


def test_an_asset_unseen_past_the_threshold_is_one_finding() -> None:
    sync = _sync()
    records = [_record("old.example", days=30, sync_id=str(sync.id)), _record("fresh.example", days=2)]
    found, counts = staleness.findings(records, [sync], now=NOW, stale_days=14)
    assert [(r.name, d) for r, d, _ in found] == [("old.example", "unseen")]
    assert found[0][2]["days_unseen"] == 30
    assert counts["evaluated"] == 2 and counts["unseen"] == 1 and counts["orphaned"] == 0


def test_an_asset_whose_sync_is_gone_is_orphaned() -> None:
    found, counts = staleness.findings([_record("lost.example", days=1, sync_id="deleted")], [], now=NOW, stale_days=14)
    assert [(r.name, d) for r, d, _ in found] == [("lost.example", "orphaned")]
    assert counts["orphaned"] == 1


def test_an_asset_of_a_failed_sync_is_withheld_never_stale() -> None:
    failed = _sync(error="503 from the platform")
    paused = _sync(paused="token revoked")
    records = [
        _record("a.example", days=40, sync_id=str(failed.id)),
        _record("b.example", days=40, sync_id=str(paused.id)),
    ]
    found, counts = staleness.findings(records, [failed, paused], now=NOW, stale_days=14)
    assert found == []
    assert counts["withheld_sync_failed"] == 2 and counts["failed_syncs"] == 2


def test_nothing_past_the_threshold_finds_nothing() -> None:
    found, counts = staleness.findings([_record("x", days=3), _record("y", days=None)], [], now=NOW, stale_days=14)
    assert found == [] and counts["unseen"] == 0


def test_the_inventory_evaluation_is_a_source_of_path_6_always_on() -> None:
    keys = {s.key for s in sources.PATH_SOURCES["stale_asset"]}
    assert "xtm_one:pass" in keys
    row = {s["key"]: s for s in sources.path_sources("stale_asset", sources.BoundSources({sources.INVENTORY_PASS}))}
    assert row["xtm_one:pass"]["bound"] is True


def _path(**kw: Any) -> dict[str, Any]:
    return {"id": "stale_asset", "status": "enabled", "count_30d": 0, "sources": [], **kw}


def test_a_silent_path_6_says_why() -> None:
    assert ctem_signals.silent_reason(_path(), None)["code"] == "never_evaluated"
    clean = {"at": NOW.isoformat(), "evaluated": 500, "unseen": 0, "failed_syncs": 0, "withheld_sync_failed": 0}
    assert ctem_signals.silent_reason(_path(), clean)["code"] == "no_asset_past_threshold"
    failed = {**clean, "failed_syncs": 2, "withheld_sync_failed": 40}
    reason = ctem_signals.silent_reason(_path(), failed)
    assert reason["code"] == "upstream_sync_failed" and reason["evaluation"]["withheld_sync_failed"] == 40


def test_an_inventory_past_the_cap_never_reads_nothing_past_threshold() -> None:
    capped = {"at": NOW.isoformat(), "evaluated": 100, "truncated": 7, "failed_syncs": 0, "withheld_sync_failed": 0}
    reason = ctem_signals.silent_reason(_path(), capped)
    assert reason is not None
    assert reason["code"] == "inventory_truncated" and reason["evaluation"]["truncated"] == 7


async def test_a_failed_daily_pass_gives_its_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import organization_context, organizations

    released: list[bool] = []

    async def _primary(_db: Any) -> Any:
        return SimpleNamespace(id=uuid.uuid4())

    async def _profile(_db: Any, _org: Any) -> Any:
        return None, SimpleNamespace()

    async def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("database gone")

    async def _due() -> bool:
        return True

    async def _release() -> None:
        released.append(True)

    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(organization_context, "current_profile", _profile)
    monkeypatch.setattr(staleness, "evaluate", _boom)
    monkeypatch.setattr(staleness, "_due", _due)
    monkeypatch.setattr(staleness, "release_pass_slot", _release)
    with pytest.raises(RuntimeError):
        await staleness.scheduled_pass(SimpleNamespace())  # type: ignore[arg-type]
    assert released == [True]


def test_a_path_with_signals_or_off_has_no_reason() -> None:
    assert ctem_signals.silent_reason(_path(count_30d=3), None) is None
    assert ctem_signals.silent_reason(_path(status="off"), None) is None


def test_a_listener_path_that_never_fired_says_so() -> None:
    row = {"id": "code_flaw", "status": "enabled", "count_30d": 0, "sources": [{"last_signal_at": None}]}
    assert ctem_signals.silent_reason(row)["code"] == "listener_never_fired"


def test_the_catalog_carries_the_reason_through_the_api_shape() -> None:
    bound = sources.BoundSources({sources.INVENTORY_PASS})
    rows = {
        r["id"]: r
        for r in ctem_signals.path_catalog(
            {}, bound=bound, activity={}, evaluations={"stale_asset": {"evaluated": 10, "failed_syncs": 1}}
        )
    }
    assert rows["stale_asset"]["silent_reason"]["code"] == "upstream_sync_failed"
