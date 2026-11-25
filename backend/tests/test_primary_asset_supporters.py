# SPDX-License-Identifier: Apache-2.0

"""The supporting assets of one primary asset, paged and searched on the server (#1980 S2)."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_primary_assets as pa


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalar(self) -> Any:
        return self._rows[0] if self._rows else None


class _Db:
    """Answers the counts, the total and the page in that order, and keeps each statement."""

    def __init__(self, by: list[tuple[Any, ...]], total: int, page: list[Any]) -> None:
        self.answers = [_Result(by), _Result([total]), _Result(page)]
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.answers.pop(0)


def _row(name: str, kind: str, through: str = "", source: str = "opencrq") -> Any:
    return SimpleNamespace(
        record_id=uuid.uuid4(),
        asset_id=None,
        name=name,
        category="host",
        criticality="high",
        through_kind=kind,
        through=through,
        source=source,
        roles=["stores"],
        inherited_needs={"confidentiality": "full"},
        via="",
    )


@pytest.mark.asyncio
async def test_rows_counts_and_total_come_back_with_names_not_ids() -> None:
    db = _Db(
        by=[("asset", "manual", 2), ("asset_group", "opencrq", 263)],
        total=265,
        page=[_row("laptop-001", "asset_group", "Employee laptops"), _row("payments-db", "asset", source="manual")],
    )
    out = await pa.primary_asset_supporters(db, uuid.uuid4(), " Employee  Workstations ")  # type: ignore[arg-type]
    assert out["total"] == 265
    assert out["counts"] == {
        "total": 265,
        "through": {"asset": 2, "asset_group": 263},
        "source": {"manual": 2, "opencrq": 263},
    }
    first = out["items"][0]
    assert first["name"] == "laptop-001"
    assert first["through_kind"] == "asset_group" and first["through"] == "Employee laptops"
    assert first["roles"] == ["stores"] and first["inherited_needs"] == {"confidentiality": "full"}
    assert out["items"][1]["through"] == ""


@pytest.mark.asyncio
async def test_the_query_leaves_out_removals_and_follows_groups() -> None:
    db = _Db(by=[], total=0, page=[])
    out = await pa.primary_asset_supporters(db, uuid.uuid4(), "Payments", q="lap", limit=999)  # type: ignore[arg-type]
    assert out == {
        "items": [],
        "total": 0,
        "offset": 0,
        "limit": pa.SUPPORTERS_PAGE_MAX,
        "counts": {"total": 0, "through": {}, "source": {}},
    }
    sql = str(db.statements[2])
    assert "organization_primary_asset_links.removed IS false" in sql
    assert "UNION ALL" in sql
    assert "organization_asset_group_members" in sql
    assert "LIKE" in sql


@pytest.mark.asyncio
async def test_filters_and_the_criticality_sort_reach_the_page_query() -> None:
    db = _Db(by=[], total=0, page=[])
    await pa.primary_asset_supporters(
        db,  # type: ignore[arg-type]
        uuid.uuid4(),
        "Payments",
        through=("asset_group",),
        sources=("opencrq", "rule"),
        sort="criticality",
        descending=True,
    )
    total_sql, page_sql = str(db.statements[1]), str(db.statements[2])
    for sql in (total_sql, page_sql):
        assert "through_kind IN" in sql
        assert "coalesce(" in sql
    assert "CASE" in page_sql and "DESC" in page_sql


@pytest.mark.asyncio
async def test_no_filter_keeps_every_row_in_name_order() -> None:
    db = _Db(by=[], total=0, page=[])
    await pa.primary_asset_supporters(db, uuid.uuid4(), "Payments")  # type: ignore[arg-type]
    page_sql = str(db.statements[2])
    assert "through_kind IN" not in page_sql
    assert "CASE" not in page_sql and "DESC" not in page_sql
