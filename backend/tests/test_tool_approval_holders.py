"""How many people a tool change would surprise.

Editing a custom tool's parameters changes its schema fingerprint, so every
standing approval for it stops describing the tool and the gate asks again.
That is correct — consent was to the old shape — but it happened silently, and
"3 people will be asked again" is harder to skim past than a caveat.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import tool_approvals as api
from app.tools.approval import fingerprint

USER = SimpleNamespace(id=uuid.uuid4(), role="user", email="r@example.com")
SCHEMA = {"type": "object", "properties": {"to": {"type": "string"}}}


@pytest.fixture(autouse=True)
def _schema(monkeypatch):
    """The tool's current shape. Resolving it for real would query several
    tables; what the endpoint does with it is the part under test."""

    async def _resolve(_db, _key, *, user_id, is_admin):
        return SCHEMA, "custom", None

    monkeypatch.setattr(api, "_resolve_schema", _resolve)


class _DB:
    """Answers the count, and keeps the statement so its filters can be read."""

    def __init__(self, count=0):
        self._count = count
        self.statements: list = []

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return self._count


@pytest.mark.asyncio
async def test_it_counts_the_others_and_names_nobody():
    db = _DB(count=3)

    result = await api.count_approval_holders(
        tool_key="send_email", user=USER, db=db
    )

    assert result == {"tool_key": "send_email", "others": 3}


@pytest.mark.asyncio
async def test_the_caller_s_own_approval_is_not_counted():
    """The number answers "besides me": the caller's own is already on their
    profile and on the tool's row, so including it would double-report the one
    holder who is not going to be surprised."""
    db = _DB(count=0)

    await api.count_approval_holders(tool_key="send_email", user=USER, db=db)

    where = str(db.statements[-1].whereclause.compile(
        compile_kwargs={"literal_binds": True}
    ))
    assert "revoked_at IS NULL" in where
    # Literal-bound UUIDs compile without dashes.
    assert USER.id.hex in where
    assert "user_id !=" in where


@pytest.mark.asyncio
async def test_an_empty_tool_key_is_refused():
    with pytest.raises(HTTPException) as exc:
        await api.count_approval_holders(tool_key="   ", user=USER, db=_DB())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_only_approvals_that_still_describe_the_tool_are_counted():
    """Invalidation is lazy, so a stale row is still un-revoked.

    Counting one would promise to surprise somebody this change cannot reach:
    their consent stopped describing the tool at an earlier edit and the gate
    is already asking them again.
    """
    db = _DB(count=0)

    await api.count_approval_holders(tool_key="send_email", user=USER, db=db)

    where = str(db.statements[-1].whereclause.compile(
        compile_kwargs={"literal_binds": True}
    ))
    assert fingerprint(SCHEMA) in where


@pytest.mark.asyncio
async def test_an_unresolvable_shape_counts_every_live_row(monkeypatch):
    """Over-report rather than under-report: a warning that misses people is
    worse than one that names a few who were being re-asked anyway."""

    async def _unknown(_db, _key, *, user_id, is_admin):
        return None, "", None

    monkeypatch.setattr(api, "_resolve_schema", _unknown)
    db = _DB(count=2)

    result = await api.count_approval_holders(
        tool_key="send_email", user=USER, db=db
    )

    where = str(db.statements[-1].whereclause.compile(
        compile_kwargs={"literal_binds": True}
    ))
    assert "schema_fingerprint" not in where
    assert result["others"] == 2
