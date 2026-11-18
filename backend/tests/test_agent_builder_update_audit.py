"""``update_agent`` audits every edit of an existing agent (#251).

The REST update writes its ``Updated agent`` row on every edit; the builder
tool used to write one only for the catalog switch or a versioned value, so a
description, binding or flag change made through an agent left no trace in the
audit log. Each edit now writes one ``update`` row, ``via: agent``, naming the
fields it changed.
"""

from __future__ import annotations

from tests.test_agent_builder_catalog_personal_tools import _record_audit, _update
from tests.test_agent_builder_crud_tools import _admin_caller
from tests.test_catalog_personal_tools_download import _published


async def test_an_unversioned_edit_writes_one_row_naming_its_fields(monkeypatch):
    agent, _session, _variable = _published(include=False)
    agent.is_company_managed = False
    caller = _admin_caller(monkeypatch, is_admin=False)
    entries = _record_audit(monkeypatch)

    result, _ = await _update(monkeypatch, agent, description="Reads the tickets.", disable_chat="true")

    assert not result.is_error
    [entry] = entries
    assert entry["user"] is caller
    assert entry["action"] == "update" and entry["entity_type"] == "agent"
    assert entry["details"]["via"] == "agent"
    assert {"description", "disable_chat"} <= set(entry["details"]["fields"])
    assert entry["details"]["fields"] == sorted(entry["details"]["fields"])


async def test_a_refused_edit_writes_no_row(monkeypatch):
    agent, _session, _variable = _published(include=False)
    agent.is_company_managed = True
    _admin_caller(monkeypatch, is_admin=False)
    entries = _record_audit(monkeypatch)

    result, _ = await _update(monkeypatch, agent, catalog_include_personal_tools="true")

    assert "Only admins" in result.content
    assert entries == []
