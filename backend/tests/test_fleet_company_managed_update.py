"""An existing fleet or workspace changes company-managed status in place (#398), without a database.

The PostgreSQL half (the refusals naming the personal members and the
personal workspace, the round trip with the id kept) is in
``test_fleet_tools_postgres.py``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services.builtin_tools import fleets as fleet_tools


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def session(monkeypatch) -> _Session:
    db = _Session()

    @asynccontextmanager
    async def _factory():
        yield db

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    return db


def _setup(monkeypatch, *, is_admin: bool, company: bool = False) -> tuple[SimpleNamespace, SimpleNamespace]:
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin)
    fleet = SimpleNamespace(
        id=uuid.uuid4(), name="Desk", is_builtin=False, is_company_managed=company, workspace_id=uuid.uuid4()
    )

    async def _load(_db, _uid):
        return user

    async def _manageable(_db, _model, _uid, _entity_uuid, **_kw):
        return fleet, None

    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    return user, fleet


async def _call(user_id: str, tool: str, **args) -> str:
    _defs, executor = fleet_tools.build_fleet_tools(user_id)
    return await executor.handler(tool)(**args)


def test_update_fleet_and_update_workspace_declare_the_flag() -> None:
    from app.services.builtin_tools import build_agent_builder_tool

    fleet_defs, _ = fleet_tools.build_fleet_tools("")
    update_fleet = next(d for d in fleet_defs if d.name == "update_fleet")
    assert update_fleet.input_schema["properties"]["is_company_managed"]["type"] == "boolean"
    builder_defs, _ = build_agent_builder_tool(None, "", conversation_id=None)
    update_workspace = next(d for d in builder_defs if d.name == "update_workspace")
    assert update_workspace.input_schema["properties"]["is_company_managed"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_a_non_admin_cannot_change_the_flag_and_nothing_is_saved(monkeypatch, session) -> None:
    user, fleet = _setup(monkeypatch, is_admin=False)
    answer = await _call(str(user.id), "update_fleet", fleet_id=str(fleet.id), is_company_managed=True)
    assert answer == "Only admins can change whether a fleet is company-managed. Nothing was saved."
    assert fleet.is_company_managed is False
    assert (session.commits, session.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_the_flag_as_it_already_is_changes_nothing(monkeypatch, session) -> None:
    user, fleet = _setup(monkeypatch, is_admin=False, company=True)
    answer = await _call(str(user.id), "update_fleet", fleet_id=str(fleet.id), is_company_managed="true")
    assert answer.startswith("Nothing to change"), answer
    assert "is_company_managed" in answer
    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_flag_that_is_not_a_boolean_is_refused(monkeypatch, session) -> None:
    user, fleet = _setup(monkeypatch, is_admin=True)
    answer = await _call(str(user.id), "update_fleet", fleet_id=str(fleet.id), is_company_managed="maybe")
    assert "is_company_managed" in answer and "'true' or 'false'" in answer, answer
    assert session.commits == 0
