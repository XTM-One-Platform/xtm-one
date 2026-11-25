"""get_agent reads the agent's tool approval policy and the platform tool rules (#2189).

The orchestrator of a partner platform could not tell whether an agent asks
before running tools, nor which tools the administrator allowed: get_agent
printed ``human_in_the_loop`` only. The lines come from the gate's own
resolvers, and a failed read says so.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.services.hitl_whitelist as hitl
from app.services.agent_approval_policy import (
    MAX_NAMED_RULES,
    UNREADABLE,
    approval_policy_lines,
    effective_label,
    own_setting_label,
    rules_label,
)


def test_own_setting_names_the_switch_and_the_default_it_follows() -> None:
    assert own_setting_label(True, False) == "Ask"
    assert own_setting_label(False, True) == "Don't ask"
    assert own_setting_label(None, True) == "Platform default (asks)"
    assert own_setting_label(None, False) == "Platform default (does not ask)"
    assert UNREADABLE in own_setting_label(None, None)


def test_effective_follows_the_gate_order() -> None:
    # The platform switch wins over everything, then the seeded-fleet rule.
    assert "off platform-wide" in effective_label(platform_off=True, seeded_member=True, gate_on=True)
    assert "seeded fleet" in effective_label(platform_off=False, seeded_member=True, gate_on=False)
    assert effective_label(platform_off=False, seeded_member=False, gate_on=None) == UNREADABLE
    assert effective_label(platform_off=None, seeded_member=False, gate_on=True).startswith("asks")
    assert effective_label(platform_off=False, seeded_member=False, gate_on=False) == "runs every tool without asking"


def test_rules_name_added_and_removed_tools() -> None:
    assert rules_label(None) == UNREADABLE
    assert rules_label({}).startswith("None")
    text = rules_label({"b_tool": hitl.ACTION_ADD, "a_tool": hitl.ACTION_ADD, "gated": hitl.ACTION_REMOVE})
    assert text == "run without asking: `a_tool`, `b_tool`; always ask: `gated`"
    many = {f"t{i:02d}": hitl.ACTION_ADD for i in range(MAX_NAMED_RULES + 3)}
    assert rules_label(many).endswith(", +3 more")


class _Nested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _Db:
    def __init__(self, row: Any = None, *, fail: bool = False) -> None:
        self._row = row
        self._fail = fail

    def begin_nested(self) -> _Nested:
        return _Nested()

    async def execute(self, _stmt: Any) -> _Result:
        if self._fail:
            raise RuntimeError("db down")
        return _Result(self._row)


def _patch(monkeypatch: pytest.MonkeyPatch, *, seeded: bool, gate_on: bool | Exception, overrides: Any) -> None:
    async def _seeded(_db: Any, _agent: Any) -> bool:
        return seeded

    async def _gate(_db: Any, _agent: Any) -> bool:
        if isinstance(gate_on, Exception):
            raise gate_on
        return gate_on

    async def _overrides(_db: Any) -> dict[str, str]:
        if isinstance(overrides, Exception):
            raise overrides
        return cast(dict[str, str], overrides)

    monkeypatch.setattr(hitl, "seeded_fleet_member", _seeded)
    monkeypatch.setattr(hitl, "resolve_approve_tool_calls", _gate)
    monkeypatch.setattr(hitl, "load_overrides", _overrides)


def _agent(own: bool | None) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), is_builtin=False, approve_tool_calls=own)


@pytest.mark.asyncio
async def test_lines_for_an_admin_read_the_settings_and_the_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, seeded=False, gate_on=True, overrides={"manage_x": hitl.ACTION_ADD})
    lines = await approval_policy_lines(cast(Any, _Db((True, True))), _agent(None), caller_is_admin=True)
    assert lines == [
        "  • Ask before running tools: Platform default (asks)",
        "  • Tool approvals (effective): asks before a tool off the platform's allowed list "
        "(standing approvals of the person apply)",
        "  • Platform tool rules (every agent): run without asking: `manage_x`",
    ]


@pytest.mark.asyncio
async def test_platform_switch_off_wins_and_a_non_admin_is_pointed_at_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, seeded=False, gate_on=True, overrides={"manage_x": hitl.ACTION_ADD})
    lines = await approval_policy_lines(cast(Any, _Db((False, False))), _agent(True), caller_is_admin=False)
    assert lines[0].endswith("Ask")
    assert "off platform-wide" in lines[1]
    assert "manage_x" not in lines[2]
    assert "administrators only" in lines[2]


@pytest.mark.asyncio
async def test_no_settings_row_reads_as_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, seeded=True, gate_on=False, overrides={})
    lines = await approval_policy_lines(cast(Any, _Db(None)), _agent(None), caller_is_admin=True)
    assert lines[0].endswith("Platform default (does not ask)")
    assert "seeded fleet" in lines[1]
    assert lines[2].endswith("None (the shipped default applies)")


@pytest.mark.asyncio
async def test_failed_reads_say_unreadable_never_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, seeded=False, gate_on=RuntimeError("boom"), overrides=RuntimeError("boom"))
    lines = await approval_policy_lines(cast(Any, _Db(fail=True)), _agent(None), caller_is_admin=True)
    assert UNREADABLE in lines[0]
    assert lines[1].endswith(UNREADABLE)
    assert lines[2].endswith(UNREADABLE)
