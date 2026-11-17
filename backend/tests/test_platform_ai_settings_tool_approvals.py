"""The tool approval switches through the admin tools, without a database.

Parity rule: every switch of Settings > Access and security > Tool approvals an
administrator changes in the UI is readable by ``get_platform_ai_settings``
and writable by ``update_platform_ai_settings``, with the same admin check,
the same validation and the same audit entry as ``PUT /settings/platform``
(both go through ``app.services.tool_approval_settings``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


class _FakeSession:
    def __init__(self):
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        self.committed = True


def _ps(**overrides):
    values = {
        "maximum_wait_time": None,
        "maximum_total_wait_time": None,
        "wait_unbounded_with_until": None,
        "default_reasoning_effort": None,
        "agent_default_model": None,
        "agent_default_provider": None,
        "tool_approvals_enabled": True,
        "approve_tool_calls_default": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _tools(monkeypatch, *, admin, ps, env_seed=None):
    from app.services.builtin_tools import platform_admin

    session = _FakeSession()
    audits: list[dict] = []

    async def _admin(_db, _user_id):
        return SimpleNamespace(id="u", is_admin=True) if admin else None

    async def _load(_db):
        return ps

    async def _audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(platform_admin, "_admin", _admin)
    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _load)
    monkeypatch.setattr("app.services.audit.log_audit", _audit)
    monkeypatch.setattr("app.core.config.settings.agent_maximum_wait_time", None, raising=False)
    monkeypatch.setattr("app.core.config.settings.agent_maximum_total_wait_time", None, raising=False)
    monkeypatch.setattr("app.core.config.settings.default_reasoning_effort", "", raising=False)
    monkeypatch.setattr("app.core.config.settings.tool_approvals_enabled", env_seed, raising=False)
    definitions, handlers = platform_admin.build_platform_admin_tools("00000000-0000-4000-8000-000000000001")
    return {d.name: d for d in definitions}, handlers, session, audits


def test_an_admin_reads_both_switches_and_the_env_seed(monkeypatch):
    _defs, handlers, _session, _audits = _tools(monkeypatch, admin=True, ps=_ps(), env_seed="false")
    read = json.loads(asyncio.run(handlers["get_platform_ai_settings"]()))
    assert read["tool_approvals"] == {
        "tool_approvals_enabled": True,
        "approve_tool_calls_default": False,
        "env_seed": "false",
    }


def test_an_unset_env_seed_reads_as_null(monkeypatch):
    _defs, handlers, _session, _audits = _tools(monkeypatch, admin=True, ps=_ps())
    read = json.loads(asyncio.run(handlers["get_platform_ai_settings"]()))
    assert read["tool_approvals"]["env_seed"] is None


def test_an_admin_suspends_approvals_audited_like_the_settings_page(monkeypatch):
    from app.services.tool_approval_settings import serialize_tool_approval_settings

    ps = _ps()
    _defs, handlers, session, audits = _tools(monkeypatch, admin=True, ps=ps)
    answer = json.loads(asyncio.run(handlers["update_platform_ai_settings"](tool_approvals_enabled=False)))
    assert answer["changed"] == {"tool_approvals_enabled": False}
    assert answer["settings"]["tool_approvals"]["tool_approvals_enabled"] is False
    assert ps.tool_approvals_enabled is False and session.committed
    assert audits == [
        {
            "user": audits[0]["user"],
            "action": "toggle",
            "entity_type": "platform_settings",
            "entity_name": "Tool approvals",
            "summary": "Disabled tool approval pauses platform-wide",
            "details": {"tool_approvals_enabled": False, "via": "update_platform_ai_settings"},
        }
    ]
    read = json.loads(asyncio.run(handlers["get_platform_ai_settings"]()))
    assert read["tool_approvals"]["tool_approvals_enabled"] is False
    # GET /settings/platform serves the switches through the same serializer.
    assert serialize_tool_approval_settings(ps)["tool_approvals_enabled"] is False


def test_an_admin_changes_the_install_default(monkeypatch):
    ps = _ps()
    _defs, handlers, _session, audits = _tools(monkeypatch, admin=True, ps=ps)
    answer = json.loads(asyncio.run(handlers["update_platform_ai_settings"](approve_tool_calls_default=True)))
    assert answer["changed"] == {"approve_tool_calls_default": True}
    assert ps.approve_tool_calls_default is True
    assert audits[0]["summary"] == "Every agent asks before running tools by default"


def test_a_switch_mixed_with_a_wait_setting_writes_one_entry_each(monkeypatch):
    ps = _ps()
    _defs, handlers, _session, audits = _tools(monkeypatch, admin=True, ps=ps)
    answer = json.loads(
        asyncio.run(handlers["update_platform_ai_settings"](tool_approvals_enabled=False, maximum_total_wait_time=7200))
    )
    assert answer["changed"] == {"maximum_total_wait_time": 7200, "tool_approvals_enabled": False}
    assert sorted(a["action"] for a in audits) == ["toggle", "update"]
    update = next(a for a in audits if a["action"] == "update")
    assert "tool_approvals_enabled" not in update["details"]


def test_the_stored_value_again_is_not_a_change(monkeypatch):
    ps = _ps(tool_approvals_enabled=False)
    _defs, handlers, session, audits = _tools(monkeypatch, admin=True, ps=ps)
    answer = json.loads(asyncio.run(handlers["update_platform_ai_settings"](tool_approvals_enabled=False)))
    assert answer["changed"] == {} and not audits and not session.committed


def test_a_non_admin_gets_the_403_sentence_and_nothing_changes(monkeypatch):
    ps = _ps()
    _defs, handlers, session, audits = _tools(monkeypatch, admin=False, ps=ps)
    assert asyncio.run(handlers["get_platform_ai_settings"]()).startswith("Error 403")
    answer = asyncio.run(handlers["update_platform_ai_settings"](tool_approvals_enabled=False))
    assert answer.startswith("Error 403")
    assert ps.tool_approvals_enabled is True and not session.committed and not audits


@pytest.mark.parametrize("value", ["false", 0, 1, "off"])
def test_a_value_that_is_not_a_boolean_is_an_error_line(monkeypatch, value):
    ps = _ps()
    _defs, handlers, session, audits = _tools(monkeypatch, admin=True, ps=ps)
    answer = asyncio.run(handlers["update_platform_ai_settings"](tool_approvals_enabled=value))
    assert answer == "Error: tool_approvals_enabled: must be true or false"
    assert ps.tool_approvals_enabled is True and not session.committed and not audits


def test_null_leaves_a_switch_unchanged(monkeypatch):
    ps = _ps()
    _defs, handlers, _session, _audits = _tools(monkeypatch, admin=True, ps=ps)
    answer = asyncio.run(handlers["update_platform_ai_settings"](tool_approvals_enabled=None))
    assert answer == "Error: name at least one setting to change."
    assert ps.tool_approvals_enabled is True


def test_the_update_schema_names_both_switches(monkeypatch):
    defs, _handlers, _session, _audits = _tools(monkeypatch, admin=True, ps=_ps())
    properties = defs["update_platform_ai_settings"].input_schema["properties"]
    assert properties["tool_approvals_enabled"] == {"type": ["boolean", "null"]}
    assert properties["approve_tool_calls_default"] == {"type": ["boolean", "null"]}
    # A write gated like any other for agents that opt in; never always-ask.
    assert not getattr(defs["update_platform_ai_settings"], "always_ask", False)
    assert not getattr(defs["update_platform_ai_settings"], "always_ask_argument", None)


def test_the_shared_helper_validates_before_writing():
    from app.services.tool_approval_settings import apply_tool_approval_settings

    ps = _ps()
    with pytest.raises(ValueError, match="approve_tool_calls_default: must be true or false"):
        apply_tool_approval_settings(ps, {"tool_approvals_enabled": False, "approve_tool_calls_default": "yes"})
    assert ps.tool_approvals_enabled is True, "nothing is written when one value is malformed"
