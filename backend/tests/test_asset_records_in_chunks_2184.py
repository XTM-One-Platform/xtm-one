"""#2184: a 10,000-row batch never sends one row-value IN of the whole batch.

The membership write of a batch looked up every ``(group, record)`` pair in one
``IN``, which PostgreSQL refuses as too complex at 9,999 rows. The session is a
fake recording each statement; no database is needed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import organization_asset_records as records


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Session:
    """Answers the group lookup with one group and every other read with nothing."""

    def __init__(self, group_id: uuid.UUID) -> None:
        self.group_id = group_id
        self.statements: list[Any] = []
        self.added: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        columns = [c.name for c in getattr(statement, "selected_columns", [])]
        if columns == ["name_key", "id"]:
            return _Rows([("ops", self.group_id)])
        return _Rows([])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _largest_in_list(statement: Any) -> int:
    """The longest list value bound into *statement* (an expanding ``IN``)."""
    params = statement.compile().params
    return max((len(v) for v in params.values() if isinstance(v, (list, tuple))), default=0)


@pytest.mark.asyncio
async def test_the_membership_write_of_a_whole_batch_chunks_its_pair_lookup() -> None:
    group_id = uuid.uuid4()
    rows = [SimpleNamespace(id=uuid.uuid4(), group_key="ops") for _ in range(2_500)]
    db = _Session(group_id)

    await records._write_memberships(db, uuid.uuid4(), rows)  # type: ignore[arg-type]

    pair_lookups = [
        s for s in db.statements if [c.name for c in getattr(s, "selected_columns", [])] == ["group_id", "record_id"]
    ]
    assert len(pair_lookups) == 3
    largest = max(_largest_in_list(s) for s in pair_lookups)
    assert largest <= records.IN_CHUNK, largest
    # Every record still gets its membership, the chunks change nothing else.
    assert len(db.added) == 2_500
