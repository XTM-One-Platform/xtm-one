# SPDX-License-Identifier: Apache-2.0

"""The per-device history of a case's running versions and the list's asset count (#1694)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ctem_case_version_history as history
from app.services import ctem_case_versions as versions

T0 = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)


def _device(version: str, status: str | None, *sources: str) -> dict[str, Any]:
    return {"versions": [version], "status": status, "sources": list(sources)}


def _event(fields: dict[str, str], at: datetime) -> SimpleNamespace:
    return SimpleNamespace(kind=history.KIND, at=at, **fields)


def test_running_versions_with_devices_gives_each_asset_its_state() -> None:
    rows = [
        {"vendor": "Mozilla", "product": "Firefox", "version": "120.0", "asset_ids": ["a1"], "sources": ["intune"]},
        {"vendor": "Mozilla", "product": "Firefox", "version": "128.0", "asset_ids": ["a2"], "sources": ["defender"]},
    ]
    answer = versions.running_versions(rows, product_label="Mozilla Firefox", fixed_version="125.0", with_devices=True)
    assert answer is not None
    assert answer["devices"] == {
        "a1": {"versions": ["120.0"], "status": "below_fixed", "sources": ["intune"]},
        "a2": {"versions": ["128.0"], "status": "fixed", "sources": ["defender"]},
    }
    assert "devices" not in (
        versions.running_versions(rows, product_label="Mozilla Firefox", fixed_version="125.0") or {}
    )


def test_a_first_record_is_a_baseline_with_no_device_lines() -> None:
    fields = history.event_fields(None, {"a1": _device("120.0", "below_fixed", "intune")})
    assert fields is not None
    assert fields["to_value"] == "baseline: 1 device(s), 1 below the fix"
    assert fields["reason"] == ""
    assert json.loads(fields["evidence"])["devices"]["a1"]["status"] == "below_fixed"


def test_nothing_changed_writes_nothing() -> None:
    devices = {"a1": _device("120.0", "below_fixed", "intune")}
    assert history.event_fields(devices, devices) is None


def test_a_shrinking_below_fix_set_says_why_per_device() -> None:
    before = {
        "a1": _device("120.0", "below_fixed", "intune"),
        "a2": _device("121.0", "below_fixed", "intune"),
        "a3": _device("122.0", "below_fixed", "intune"),
    }
    after = {
        "a1": _device("128.0", "fixed", "intune"),
        "a2": _device("unknown", "not_comparable", "defender"),
    }
    fields = history.event_fields(before, after)
    assert fields is not None
    assert fields["to_value"] == "3 device(s) changed: 3 below the fix -> 0"
    lines = fields["reason"].split("\n")
    assert lines[0] == "a1: 120.0 (below_fixed) -> 128.0 (fixed), upgraded"
    assert (
        lines[1] == "a2: 121.0 (below_fixed) -> unknown (not_comparable), version became unknown (reported by defender)"
    )
    assert lines[2] == "a3: 122.0 (below_fixed) -> absent, no longer reported by the inventory"


def test_a_newly_reported_device_is_named() -> None:
    changes = history.diff({}, {"a9": _device("120.0", "below_fixed", "csv")})
    assert changes == [
        {
            "asset_id": "a9",
            "before": None,
            "after": {"versions": ["120.0"], "status": "below_fixed", "sources": ["csv"]},
            "reason": "newly reported by the inventory",
        }
    ]


def test_history_serves_the_newest_record_first_with_its_changes() -> None:
    first = history.event_fields(None, {"a1": _device("120.0", "below_fixed")})
    second = history.event_fields({"a1": _device("120.0", "below_fixed")}, {"a1": _device("128.0", "fixed")})
    assert first and second
    events = [_event(first, T0), _event(second, T0 + timedelta(hours=1)), SimpleNamespace(kind="note", at=T0)]
    served = history.history_of(events)
    assert [h["summary"] for h in served] == [second["to_value"], first["to_value"]]
    assert served[0]["changes"][0]["reason"] == "upgraded"
    assert served[0]["at"] == (T0 + timedelta(hours=1)).isoformat()


def test_list_asset_count_comes_from_the_issues_then_the_inventory_never_a_silent_zero() -> None:
    assert (
        history.with_asset_count({"asset_count": 2, "remediation_kind": "upgrade"}, [])["asset_count_source"]
        == "issues"
    )
    baseline = history.event_fields(None, {"a1": _device("1", "fixed"), "a2": _device("1", "fixed")})
    assert baseline
    row = history.with_asset_count({"asset_count": 0, "remediation_kind": "upgrade"}, [_event(baseline, T0)])
    assert row["asset_count"] == 2
    assert row["asset_count_source"] == "inventory"
    assert row["asset_count_as_of"] == T0.isoformat()
    unread = history.with_asset_count({"asset_count": 0, "remediation_kind": "upgrade"}, [])
    assert unread["asset_count"] is None
    assert unread["asset_count_source"] == "unknown"
    assert unread["asset_count_reason"] == history.NO_COUNT_NOT_READ
    other = history.with_asset_count({"asset_count": 0, "remediation_kind": "course_of_action"}, [])
    assert other["asset_count"] is None
    assert other["asset_count_reason"] == history.NO_COUNT_NOT_UPGRADE


def test_case_read_counts_the_assets_of_its_live_running_versions() -> None:
    """No issue asset, no recorded snapshot, a live answer of 2 devices: 2 from the inventory (#2225)."""
    row = history.with_asset_count({"asset_count": 0, "remediation_kind": "upgrade"}, [])
    assert row["asset_count"] is None and row["asset_count_source"] == "unknown"
    running = versions.running_versions(
        [{"vendor": "MinIO", "product": "MinIO", "version": "2023.1", "asset_ids": ["a1", "a2"], "sources": ["csv"]}],
        product_label="MinIO",
        fixed_version="2023.3",
    )
    assert running is not None and running["asset_count"] == 2
    history.with_live_asset_count(row, running, at=T0)
    assert row["asset_count"] == 2
    assert row["asset_count_source"] == "inventory"
    assert row["asset_count_as_of"] == T0.isoformat()
    assert "asset_count_reason" not in row


def test_live_count_leaves_an_issue_count_and_an_unread_inventory_alone() -> None:
    issues = history.with_asset_count({"asset_count": 3, "remediation_kind": "upgrade"}, [])
    assert history.with_live_asset_count(issues, {"asset_count": 7}, at=T0)["asset_count"] == 3
    unread = history.with_asset_count({"asset_count": 0, "remediation_kind": "upgrade"}, [])
    assert history.with_live_asset_count(unread, None, at=T0)["asset_count_source"] == "unknown"
    assert history.with_live_asset_count(unread, {"asset_count": 0}, at=T0)["asset_count"] is None


class _Db:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


@pytest.mark.asyncio
async def test_record_writes_only_when_devices_moved_and_never_on_an_unread_inventory() -> None:
    case = SimpleNamespace(id="00000000-0000-0000-0000-000000000001")
    db = _Db()
    # The inventory was not read: nothing is recorded, never "every device gone".
    assert await history.record(db, case, None, [], at=T0) is False
    answer = {"devices": {"a1": _device("120.0", "below_fixed")}}
    assert await history.record(db, case, answer, [], at=T0) is True
    written = db.added[-1]
    assert written.kind == history.KIND
    # The same state again: no second event.
    assert await history.record(db, case, answer, [written], at=T0 + timedelta(hours=1)) is False
    moved = {"devices": {"a1": _device("128.0", "fixed")}}
    assert await history.record(db, case, moved, [written], at=T0 + timedelta(hours=2)) is True
    assert "upgraded" in db.added[-1].reason


@pytest.mark.asyncio
async def test_for_case_with_devices_tells_an_empty_inventory_from_an_unread_one() -> None:
    case = SimpleNamespace(remediation_kind="course_of_action")
    assert await versions.for_case(cast(AsyncSession, object()), case, with_devices=True) is None
