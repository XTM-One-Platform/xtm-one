"""Runner visibility scoping (pure, no DB).

`Profile > Runners` is a PER-USER fleet list. A runner is someone's actual
machine under OS control, so the list must be scoped to own + group-shared +
company-managed for EVERYONE, including admins — exactly like every other
personal entity. Passing ``is_admin`` to the list query made
``user_visible_filter`` return an unconditional ``TRUE``, so an admin's own
profile surfaced every user's PRIVATE laptop with edit/revoke/delete actions.

The by-id fetch (`_get_visible_runner`) is the deliberate counterpart: it KEEPS
the admin bypass so an admin retains administrative reach on a runner id they
already hold, matching ``get_user_agent(..., is_admin=True)`` and friends.

These tests drive the real endpoint code paths with a capturing fake session
and assert on the SQL the visibility query compiles to, so the list can never
silently re-acquire the admin bypass and the by-id fetch can never silently
lose it.
"""

import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1.runners import _get_visible_runner, list_runners


class _Result:
    """Empty result usable as both a scalars() list and a single-row lookup."""

    def scalars(self):
        return self

    def all(self):
        return []

    def scalar_one_or_none(self):
        return None


class _CapturingSession:
    """Records every executed query; returns empty results."""

    def __init__(self):
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        return _Result()


def _where(query) -> str:
    """The WHERE clause of a compiled query.

    ``select(Runner)`` puts every column (including ``runners.user_id``) in the
    SELECT list, so asserting on the full SQL string can't tell a scoped query
    from a bypassed one — only the predicate can.
    """
    sql = re.sub(r"\s+", " ", str(query))
    upper = sql.upper()
    if " WHERE " not in upper:
        return ""
    start = upper.index(" WHERE ") + len(" WHERE ")
    end = upper.find(" ORDER BY ", start)
    return sql[start : end if end != -1 else len(sql)]


async def test_list_runners_scopes_to_caller_even_for_admin():
    # The reported bug: as an admin this list returned ALL runners.
    admin = SimpleNamespace(id=uuid4(), is_admin=True)
    db = _CapturingSession()

    out = await list_runners(user=admin, db=db)

    assert out == []
    # queries[0] is the group-ids lookup; queries[1] is the runner list.
    where = _where(db.queries[1])
    # Scoped: the owner + company-managed predicate is present. The admin-bypass
    # form was an unconditional TRUE with no owner predicate at all.
    assert "runners.user_id" in where
    assert "runners.is_company_managed" in where


async def test_list_runners_scoped_for_non_admin():
    user = SimpleNamespace(id=uuid4(), is_admin=False)
    db = _CapturingSession()

    out = await list_runners(user=user, db=db)

    assert out == []
    where = _where(db.queries[1])
    assert "runners.user_id" in where
    assert "runners.is_company_managed" in where


async def test_get_visible_runner_keeps_admin_bypass():
    # By-id administrative reach is intentional:
    # an admin acting on a KNOWN runner id is not filtered by ownership.
    admin = SimpleNamespace(id=uuid4(), is_admin=True)
    db = _CapturingSession()

    with pytest.raises(HTTPException):
        await _get_visible_runner(str(uuid4()), admin, db)

    where = _where(db.queries[1])
    # Bypass retained: no owner predicate, only the id lookup remains.
    assert "runners.user_id" not in where
    assert "runners.id" in where


async def test_get_visible_runner_scopes_for_non_admin():
    user = SimpleNamespace(id=uuid4(), is_admin=False)
    db = _CapturingSession()

    with pytest.raises(HTTPException):
        await _get_visible_runner(str(uuid4()), user, db)

    where = _where(db.queries[1])
    # A non-admin can only reach a runner they own / share / that is company-
    # managed, even by id.
    assert "runners.user_id" in where
    assert "runners.id" in where
