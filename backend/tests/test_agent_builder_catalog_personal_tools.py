"""The agent builder sets the personal-tools catalog switch like the UI.

``update_agent`` - the builder tool that publishes an agent to the Filigran AI
Catalog, and therefore the ``/mcp/xtm`` tool built from the same definition -
takes ``catalog_include_personal_tools``. A call that does not name it never
changes it (a new publish stays off); naming it goes through the UI switch's
gate - on a company-managed agent only an admin may change it - and writes the
audit entry the REST update writes. The catalog download then follows it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import app.core.database as database
import app.services.audit as audit_module
import app.services.builtin_tools as builtin_tools_module
from tests.test_agent_builder_crud_tools import (
    _admin_caller,
    _call,
    _session_factory,
    _stub_writable_agent,
)
from tests.test_catalog_personal_tools_download import _download, _published

build_agent_builder_tool = builtin_tools_module.build_agent_builder_tool


def _record_audit(monkeypatch):
    entries: list[dict] = []

    async def _fake(_db, **kwargs):
        entries.append(kwargs)

    monkeypatch.setattr(audit_module, "log_audit", _fake)
    return entries


async def _update(monkeypatch, agent, **arguments):
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _stub_writable_agent(monkeypatch, agent=agent)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(agent.user_id or agent.id))
    result = await _call(executor, "update_agent", agent_id=str(agent.id), **arguments)
    return result, created


async def test_a_call_that_does_not_name_the_switch_leaves_it_alone(monkeypatch):
    agent, _session, _variable = _published(include=True)
    _admin_caller(monkeypatch, is_admin=False)
    entries = _record_audit(monkeypatch)

    result, _ = await _update(monkeypatch, agent, disable_chat="true")

    assert not result.is_error
    assert agent.catalog_include_personal_tools is True
    assert entries == []


async def test_the_owner_sets_it_and_the_download_follows(monkeypatch):
    """A non-admin owner of their own private published agent."""
    agent, session, _variable = _published(include=False)
    agent.is_company_managed = False
    caller = _admin_caller(monkeypatch, is_admin=False)
    entries = _record_audit(monkeypatch)

    result, created = await _update(monkeypatch, agent, catalog_include_personal_tools="true")

    assert not result.is_error and "catalog_include_personal_tools" in result.content
    assert agent.catalog_include_personal_tools is True
    assert any(c.commits == 1 for c in created)
    [entry] = entries
    assert entry["user"] is caller and entry["action"] == "update"
    assert entry["details"]["catalog_include_personal_tools"] is True
    assert entry["details"]["via"] == "agent"
    bundle = await _download(agent, session)
    assert sorted(t["name"] for t in bundle["custom_tools"]) == ["Company lookup", "My scraper"]

    result, _ = await _update(monkeypatch, agent, catalog_include_personal_tools="false")
    assert not result.is_error
    bundle = await _download(agent, session)
    assert bundle["omitted_custom_tools"] == [{"name": "My scraper"}]


async def test_a_non_admin_cannot_change_it_on_a_company_managed_agent(monkeypatch):
    agent, _session, _variable = _published(include=False)
    agent.is_company_managed = True
    _admin_caller(monkeypatch, is_admin=False)
    entries = _record_audit(monkeypatch)

    result, created = await _update(monkeypatch, agent, catalog_include_personal_tools="true")

    # The REST route's refusal, word for word.
    assert "Only admins can change the catalog settings of company-managed entities" in (
        result.content
    )
    assert agent.catalog_include_personal_tools is False
    assert all(c.commits == 0 for c in created)
    assert entries == []


async def test_an_admin_changes_it_on_a_company_managed_agent(monkeypatch):
    agent, _session, _variable = _published(include=False)
    agent.is_company_managed = True
    _admin_caller(monkeypatch, is_admin=True)
    _record_audit(monkeypatch)

    result, _ = await _update(monkeypatch, agent, catalog_include_personal_tools="true")

    assert not result.is_error
    assert agent.catalog_include_personal_tools is True


def test_the_tool_says_what_turning_it_on_publishes():
    """Also the ``/mcp/xtm`` definition, built from the same builder."""
    definitions, _ = build_agent_builder_tool(SimpleNamespace(), "00000000-0000-0000-0000-000000000001")
    [update] = [d for d in definitions if d.name == "update_agent"]
    prop = update.input_schema["properties"]["catalog_include_personal_tools"]
    assert "publishes the code of the user's personal custom tools" in prop["description"]
    assert "public catalog download" in prop["description"]
    json.dumps(update.input_schema)
