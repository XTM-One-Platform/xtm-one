"""update_integration sets and clears an integration's company-managed status.

The builder tool follows the REST ``PUT /integrations/{id}`` rule: admin only,
written through the shared ``apply_integration_company_managed``. A field the
tool does not take is refused by name instead of being dropped, so an update
never answers "No fields to update" for a request it did not understand.
"""

from __future__ import annotations

import uuid

import pytest

import app.core.database as database
from app.services.builtin_tools import build_agent_builder_tool
from tests.test_agent_builder_crud_tools import (
    _admin_caller,
    _call,
    _slug_integration,
    _slug_session_factory,
    _stub_manageable_gate,
)


def _integration(**overrides):
    values = {
        "shared_group_ids": [str(uuid.uuid4())],
        "shared_group_permissions": {},
        "is_builtin": False,
    }
    values.update(overrides)
    return _slug_integration(**values)


def _executor():
    _, executor = build_agent_builder_tool(object(), str(uuid.uuid4()))
    return executor


@pytest.mark.asyncio
async def test_an_admin_makes_an_integration_company_managed_and_private_again(monkeypatch):
    integ = _integration()
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=True)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))
    executor = _executor()

    result = await _call(executor, "update_integration", integration_id=str(integ.id), is_company_managed=True)

    assert not result.is_error, result.content
    assert "company-managed" in result.content
    assert integ.is_company_managed is True
    # Visible to everyone: the group grants are redundant and cleared.
    assert integ.shared_group_ids is None
    assert created[0].commits == 1

    owner = integ.user_id
    result = await _call(executor, "update_integration", integration_id=str(integ.id), is_company_managed="false")

    assert not result.is_error, result.content
    assert integ.is_company_managed is False
    assert integ.user_id == owner
    # One commit per call, whatever other sessions the post-commit steps open.
    assert sum(session.commits for session in created) == 2


@pytest.mark.asyncio
async def test_a_non_admin_cannot_change_company_managed_status(monkeypatch):
    integ = _integration()
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))

    result = await _call(
        _executor(),
        "update_integration",
        integration_id=str(integ.id),
        is_company_managed=True,
        name="Renamed",
    )

    assert "only admins can change the company-managed status" in result.content
    assert integ.is_company_managed is False
    # Refused before anything is written, the other fields included.
    assert integ.name == "Notion Filigran"
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_the_current_status_is_no_change_and_needs_no_admin(monkeypatch):
    integ = _integration()
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))

    result = await _call(_executor(), "update_integration", integration_id=str(integ.id), is_company_managed=False)

    assert result.content == "No fields to update. Provide at least one field."
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_an_unreadable_company_managed_value_is_refused(monkeypatch):
    integ = _integration()
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))

    result = await _call(_executor(), "update_integration", integration_id=str(integ.id), is_company_managed="maybe")

    assert "is_company_managed must be 'true' or 'false'" in result.content
    assert created == []


@pytest.mark.asyncio
async def test_an_unknown_field_is_refused_by_name(monkeypatch):
    integ = _integration()
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))

    result = await _call(
        _executor(),
        "update_integration",
        integration_id=str(integ.id),
        name="Renamed",
        owner_email="someone@example.com",
        visibility="public",
    )

    assert result.content == ("Integration not updated - unsupported field(s): owner_email, visibility.")
    assert integ.name == "Notion Filigran"
    assert created == []


def test_update_integration_declares_is_company_managed():
    definitions, _ = build_agent_builder_tool(object(), str(uuid.uuid4()))
    schema = {d.name: d for d in definitions}["update_integration"].input_schema
    assert schema["properties"]["is_company_managed"]["type"] == "boolean"
