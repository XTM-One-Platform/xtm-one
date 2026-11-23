"""An OpenAEV asset group import carries each group's members to the plan (#1556).

The asset groups read lists each group's static and dynamic members by group,
the record plan keeps them so :func:`apply_record_import` writes them, and a
failed member read stops the import instead of writing groups with no members.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import organization_context_import as imp

GROUPS = [
    {"asset_group_id": "g1", "asset_group_name": "Servers"},
    {"asset_group_id": "g2", "asset_group_name": "Laptops"},
]
DETAILS = {
    "g1": {"asset_group_assets": ["a1", "a2"], "asset_group_dynamic_assets": ["a2", "a3"]},
    "g2": {"asset_group_assets": [], "asset_group_dynamic_assets": None},
}


def _patch(monkeypatch: pytest.MonkeyPatch, *, failing: str = "") -> None:
    async def fake_creds(_db: Any, _user: Any, _platform: str) -> tuple[str, dict[str, str]]:
        return "https://aev", {}

    async def fake_search(*_args: Any, **_kwargs: Any) -> imp.ReadResult:
        return imp.ReadResult(rows=list(GROUPS), total=len(GROUPS))

    async def fake_api(_base: str, _headers: Any, _method: str, path: str, *_a: Any, **_k: Any) -> Any:
        group_id = path.rsplit("/", 1)[-1]
        if group_id == failing:
            return None, "HTTP 500"
        return SimpleNamespace(json=lambda: DETAILS[group_id]), None

    monkeypatch.setattr("app.services.ctem_metrics_collector._user_creds", fake_creds)
    monkeypatch.setattr(imp, "_openaev_search", fake_search)
    monkeypatch.setattr("app.integrations.providers.openaev._common._api", fake_api)


async def test_the_groups_read_carries_each_groups_static_and_dynamic_members(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    read = await imp._read_openaev(None, "u", "asset_groups", {}, "")  # type: ignore[arg-type]
    assert read.error is None
    assert [r.external_id for r in read.rows] == ["g1", "g2"]
    assert read.members == {"g1": ["a1", "a2", "a3"], "g2": []}
    assert read.members_source == "openaev"


async def test_a_failed_member_read_stops_the_import(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, failing="g2")
    read = await imp._read_openaev(None, "u", "asset_groups", {}, "")  # type: ignore[arg-type]
    assert read.error == "OpenAEV asset group g2: HTTP 500"
    assert read.members == {}


def test_the_record_plan_keeps_the_members_and_counts_them_as_a_write() -> None:
    read = imp.ReadResult(rows=[], total=0, members={"g1": ["a1"]}, members_source="openaev")
    plan = imp.plan_record_import(
        "asset_groups",
        "openaev",
        read,
        validate=dict,
        stored={},
        names={},
        source_rows=[],
        organization_id=uuid.uuid4(),
    )
    assert plan.members == {"g1": ["a1"]} and plan.members_source == "openaev"
    assert plan.has_writes


def test_a_read_error_carries_no_members_into_the_plan() -> None:
    read = imp.ReadResult(error="boom", members={"g1": ["a1"]}, members_source="openaev")
    plan = imp.plan_record_import(
        "asset_groups",
        "openaev",
        read,
        validate=dict,
        stored={},
        names={},
        source_rows=[],
        organization_id=uuid.uuid4(),
    )
    assert plan.members == {} and not plan.has_writes
