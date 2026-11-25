# SPDX-License-Identifier: Apache-2.0

"""The primary-asset link counts the Assets column reads (#1978 S2): synced links count, removals do not."""

import uuid
from typing import Any

import pytest

from app.services import organization_asset_records as records


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Db:
    """Answers the grouped count query with fixed rows and keeps the statement it ran."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_counts_group_by_primary_asset_and_source() -> None:
    db = _Db([("employee workstations", "opencrq", 263), ("employee workstations", "manual", 2), ("payments", None, 1)])
    counts = await records.primary_asset_link_counts(db, uuid.uuid4())  # type: ignore[arg-type]
    assert counts == {"employee workstations": {"opencrq": 263, "manual": 2}, "payments": {"manual": 1}}


@pytest.mark.asyncio
async def test_the_query_leaves_out_a_persons_removal_and_groups_in_one_statement() -> None:
    db = _Db([])
    assert await records.primary_asset_link_counts(db, uuid.uuid4()) == {}  # type: ignore[arg-type]
    assert len(db.statements) == 1
    sql = str(db.statements[0])
    assert "organization_primary_asset_links.removed IS false" in sql
    assert "GROUP BY organization_primary_asset_links.primary_asset_key, organization_primary_asset_links.source" in sql
