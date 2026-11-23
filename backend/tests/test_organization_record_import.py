"""An asset import or sync planned on the asset records (#1543).

:func:`plan_record_import` is pure: the merge rules of the profile import
(a source updates its own rows, keeps the local fields a person set, never
overwrites another writer's row of the same name, removes only the rows its
own sync wrote) against the records the read addresses, with no 5,000 cap.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services import organization_asset_records as records
from app.services import organization_context_import as imp

ORG = uuid.uuid4()


def _aev(asset_id: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"asset_id": asset_id, "asset_name": name, **extra}


def _row(asset_id: str, name: str, **extra: Any) -> Any:
    row = imp.openaev_asset(_aev(asset_id, name, **extra))
    assert row is not None
    return row


def _read(*rows: Any, truncated: bool = False) -> imp.ReadResult:
    return imp.ReadResult(rows=list(rows), total=len(rows), truncated=truncated)


def _stored(asset_id: str, name: str, **extra: Any) -> dict[str, Any]:
    return records.validate_asset({**_row(asset_id, name).model_dump(mode="json"), **extra})


def _plan(read: imp.ReadResult, **kwargs: Any) -> imp.ImportPlan:
    args: dict[str, Any] = {"stored": {}, "names": {}, "source_rows": []} | kwargs
    return imp.plan_record_import(
        "assets", "openaev", read, validate=records.validate_asset, organization_id=ORG, **args
    )


def test_a_new_row_is_one_write_and_no_profile_is_built() -> None:
    plan = _plan(_read(_row("a1", "web-01")))
    assert plan.on_records and plan.profile is None
    assert plan.new == ["web-01"] and [w["external_id"] for w in plan.writes] == ["a1"]
    assert plan.as_dict()["counts"]["new"] == 1


def test_a_row_equal_to_its_record_is_unchanged_and_not_written() -> None:
    plan = _plan(_read(_row("a1", "web-01")), stored={("openaev", "a1"): _stored("a1", "web-01")})
    assert plan.unchanged == ["web-01"] and plan.writes == [] and not plan.has_writes


def test_an_update_keeps_the_local_fields_a_person_set() -> None:
    stored = {("openaev", "a1"): _stored("a1", "web-01", owner="Alice", environment="production")}
    plan = _plan(_read(_row("a1", "web-01", asset_criticality="HIGH")), stored=stored)
    assert plan.updated == ["web-01"]
    assert plan.writes[0]["owner"] == "Alice" and plan.writes[0]["environment"] == "production"


def test_a_new_row_named_like_another_record_of_the_source_is_skipped() -> None:
    plan = _plan(_read(_row("a2", "Web-01")), names={"web-01": "openaev"})
    assert plan.skipped == ["Web-01"] and plan.writes == []


def test_a_sync_removes_only_its_own_gone_rows_and_keeps_the_others() -> None:
    source_rows = [
        imp.StoredRow("a1", "web-01", "s1"),
        imp.StoredRow("a2", "db-01", "s1"),
        imp.StoredRow("a3", "old-import", ""),
    ]
    plan = _plan(_read(_row("a1", "web-01")), source_rows=source_rows, remove_gone=True, sync_id="s1")
    assert sorted(plan.gone) == ["db-01", "old-import"]
    assert plan.gone_kept == ["old-import"]
    assert plan.remove_refs == [("openaev", "a2")]


def test_a_truncated_read_removes_nothing() -> None:
    source_rows = [imp.StoredRow("a2", "db-01", "s1")]
    plan = _plan(_read(_row("a1", "web-01"), truncated=True), source_rows=source_rows, remove_gone=True, sync_id="s1")
    assert not plan.remove_gone and plan.gone == [] and plan.remove_refs == []


def test_an_excluded_id_is_neither_written_nor_kept() -> None:
    source_rows = [imp.StoredRow("a1", "web-01", "s1")]
    plan = _plan(_read(_row("a1", "web-01")), source_rows=source_rows, excluded_ids=["a1"], sync_id="s1")
    assert plan.excluded == ["web-01"] and plan.excluded_removed == ["web-01"]
    assert plan.writes == [] and plan.remove_refs == [("openaev", "a1")]


def test_no_cap_on_the_rows_one_import_writes() -> None:
    plan = _plan(_read(*[_row(f"a{i}", f"host-{i}") for i in range(6000)]))
    assert len(plan.writes) == 6000
