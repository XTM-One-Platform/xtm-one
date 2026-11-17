"""The activity feed accepts several types in one request.

The notification bell needs operational alerts — an integration credential that
stopped working — alongside plain notifications. A durable Activity row that no
feed returns is not a notification, so the endpoint takes a comma-separated
list, and the single-value form keeps behaving exactly as before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.activity import list_activity


def _fake_db() -> AsyncMock:
    """Session that records the statement and yields no rows."""
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    return db


async def _statement(activity_type: str | None):
    db = _fake_db()
    user = SimpleNamespace(id=uuid.uuid4())
    rows = await list_activity(
        user=user, db=db, limit=50, offset=0, activity_type=activity_type
    )
    assert rows == []
    return db.execute.await_args.args[0]


def _where(statement) -> str:
    """The WHERE clause only — the SELECT list always names every column."""
    sql = str(statement).replace("\n", " ")
    return sql.split("WHERE", 1)[1] if "WHERE" in sql else ""


def _bound_types(statement) -> list[str]:
    """The values bound to the IN clause.

    SQLAlchemy renders an IN against a list as a single post-compile
    parameter, so the wanted types arrive as one list-valued bind rather than
    one bind each.
    """
    for value in statement.compile().params.values():
        if isinstance(value, (list, tuple)):
            return list(value)
    return []


@pytest.mark.asyncio
async def test_single_type_still_filters_on_that_type():
    statement = await _statement("notification")
    assert "activity_type IN" in _where(statement)
    assert _bound_types(statement) == ["notification"]


@pytest.mark.asyncio
async def test_several_types_are_all_requested():
    statement = await _statement(
        "notification,assignment_auth_quarantine,assignment_auth_failure"
    )
    assert "activity_type IN" in _where(statement)
    assert _bound_types(statement) == [
        "notification",
        "assignment_auth_quarantine",
        "assignment_auth_failure",
    ]


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_tolerated():
    statement = await _statement(" notification , assignment_auth_failure ")
    assert _bound_types(statement) == ["notification", "assignment_auth_failure"]


@pytest.mark.asyncio
async def test_blank_entries_never_produce_a_filter_matching_nothing():
    """``?activity_type=,`` must fall back to "no filter", not to an empty IN ()."""
    statement = await _statement(" , ")
    assert "activity_type" not in _where(statement)


@pytest.mark.asyncio
async def test_no_type_returns_every_activity():
    statement = await _statement(None)
    assert "activity_type" not in _where(statement)
