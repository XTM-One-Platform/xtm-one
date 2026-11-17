"""Regression tests for ``build_skill_access_context``.

The admin flag is resolved by querying the ``users`` table. ``User.is_admin``
looks like the obvious column to select, but it is a plain Python ``@property``
— selecting it raises ``ArgumentError`` at statement-construction time, before
any database is touched, and takes down every chat turn on an agent that has a
skill attached.

These tests need no database: a stub session is enough, because the failure
happens while *building* the statement.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from app.models.user import User
from app.services.builtin_tools.skills import build_skill_access_context


class _StubResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value

    def scalars(self) -> _StubResult:
        return self

    def all(self) -> list:
        return []


class _StubSession:
    """Records the statements it is handed and answers them from a script."""

    def __init__(self, role: str | None) -> None:
        self._role = role
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        # First call resolves the role; later calls (group ids) return nothing.
        if len(self.statements) == 1:
            return _StubResult(self._role)
        return _StubResult(None)


def test_is_admin_is_a_property_not_a_column():
    """Pins the reason the obvious query is wrong.

    If ``is_admin`` ever becomes a real mapped column this test fails, which is
    the signal to simplify ``build_skill_access_context`` back to selecting it
    directly.
    """
    assert isinstance(User.__dict__["is_admin"], property)
    assert not isinstance(User.is_admin, ColumnElement)
    with pytest.raises(Exception, match="property object"):
        select(User.is_admin)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected"),
    [("admin", True), ("user", False), (None, False)],
)
async def test_admin_flag_is_resolved_from_the_role_column(role, expected):
    """The happy path, and the guard against the ArgumentError regression:
    building the statement must not raise, whatever the role is."""
    session = _StubSession(role)
    ctx = await build_skill_access_context(session, uuid.uuid4())
    assert ctx.is_admin is expected
    # A mapped column was selected, not a Python property.
    assert "users.role" in str(session.statements[0]).lower()


@pytest.mark.asyncio
async def test_no_identity_short_circuits_without_querying():
    """An owner-less agent with no fallback resolves to the anonymous surface
    and must not hit the database at all."""
    session = _StubSession("admin")
    ctx = await build_skill_access_context(session, None)
    assert ctx.is_admin is False
    assert ctx.user_id is None
    assert session.statements == []
