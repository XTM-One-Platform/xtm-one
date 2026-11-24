# SPDX-License-Identifier: Apache-2.0

"""#1591 S3 review: per-signal gate reads shared per session; held reasons filtered in SQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.services import ctem_scope_gate_store as store


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


@pytest.mark.asyncio
async def test_a_read_is_loaded_once_per_session_and_key() -> None:
    db = _Session()
    calls: list[str] = []

    async def load() -> str:
        calls.append("x")
        return "index"

    for _ in range(50):
        assert await store._session_cached(db, ("links", 1), load) == "index"  # type: ignore[arg-type]
    await store._session_cached(db, ("links", 2), load)  # type: ignore[arg-type]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_read_older_than_the_window_is_loaded_again(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Session()
    clock = [100.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    calls: list[int] = []

    async def load() -> int:
        calls.append(1)
        return len(calls)

    assert await store._session_cached(db, ("k",), load) == 1  # type: ignore[arg-type]
    clock[0] += store.READ_CACHE_SECONDS + 1
    assert await store._session_cached(db, ("k",), load) == 2  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_session_without_info_loads_every_time() -> None:
    calls: list[int] = []

    async def load() -> int:
        calls.append(1)
        return 0

    await store._session_cached(object(), ("k",), load)  # type: ignore[arg-type]
    await store._session_cached(object(), ("k",), load)  # type: ignore[arg-type]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_recheck_held_filters_the_reason_in_sql_before_the_limit() -> None:
    seen: list[Any] = []

    class _Result:
        def scalars(self) -> list[Any]:
            return []

    class _Db:
        async def execute(self, statement: Any) -> _Result:
            seen.append(statement)
            return _Result()

    counts = await store.recheck_held(
        _Db(),  # type: ignore[arg-type]
        organization_id=uuid.uuid4(),
        since=datetime(2026, 11, 24, tzinfo=UTC),
        batch=7,
    )
    assert counts == {}
    sql = str(seen[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "reason_code" in sql
    assert "primary_asset_below_policy" in sql
    assert "LIMIT 7" in sql
