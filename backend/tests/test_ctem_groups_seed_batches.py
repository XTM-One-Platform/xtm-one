# SPDX-License-Identifier: Apache-2.0

"""The first seed of "CTEM Viewers" inserts its memberships in batches (#2441 review).

asyncpg refuses a statement over 32,767 bind parameters; one multi-row insert of
four parameters per account failed past about 8,190 active accounts.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services import ctem_groups


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Answers the active-account read, then echoes each insert's rows."""

    def __init__(self, ids: list[uuid.UUID]) -> None:
        self._ids = ids
        self.insert_rows: list[int] = []

    async def execute(self, statement: Any) -> _Result:
        if not self.insert_rows and getattr(statement, "is_select", False):
            return _Result(list(self._ids))
        rows = statement._multi_values[0]
        self.insert_rows.append(len(rows))
        return _Result([object() for _ in rows])


@pytest.mark.asyncio
async def test_first_seed_inserts_in_batches_under_the_parameter_limit() -> None:
    ids = [uuid.uuid4() for _ in range(ctem_groups.SEED_BATCH_ROWS * 2 + 7)]
    db = _FakeSession(ids)

    added = await ctem_groups._all_active_into(db, uuid.uuid4())  # type: ignore[arg-type]

    assert added == len(ids)
    assert db.insert_rows == [ctem_groups.SEED_BATCH_ROWS, ctem_groups.SEED_BATCH_ROWS, 7]
    assert max(db.insert_rows) * 4 < 32_767


@pytest.mark.asyncio
async def test_first_seed_with_no_active_account_inserts_nothing() -> None:
    db = _FakeSession([])

    assert await ctem_groups._all_active_into(db, uuid.uuid4()) == 0  # type: ignore[arg-type]
    assert db.insert_rows == []
