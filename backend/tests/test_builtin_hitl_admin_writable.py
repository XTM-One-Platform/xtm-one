"""An administrator may set the approval gate on a built-in agent.

Built-in agents are read-only in XTM One mode, which left no way to turn the
per-tool gate on or off for the product-shipped fleet. FR-012b opens exactly
``approve_tool_calls`` (and the existing ``enabled`` toggle), nothing else.
Built-in agents' assignments stay read-only, as on main: the gate is per agent.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.agents import (
    BUILTIN_WRITABLE_AGENT_FIELDS,
    builtin_agent_edit_allowed,
)
from app.api.v1.helpers import writable_assignment_error


@pytest.mark.parametrize(
    "fields",
    [
        {"approve_tool_calls"},
        {"enabled"},
        {"enabled", "approve_tool_calls"},
    ],
)
def test_builtin_agent_edit_allows_the_narrow_field_sets(fields: set[str]):
    assert builtin_agent_edit_allowed(fields) is True


@pytest.mark.parametrize(
    "fields",
    [
        # The legacy switch is product-owned on a built-in, like its prompt.
        pytest.param({"human_in_the_loop"}, id="legacy-hitl"),
        pytest.param({"human_in_the_loop_prompt"}, id="prompt-alone"),
        pytest.param(
            {"approve_tool_calls", "human_in_the_loop_prompt"},
            id="prompt-smuggled-alongside-an-allowed-field",
        ),
        pytest.param({"persona"}, id="unrelated-field"),
        pytest.param({"enabled", "persona"}, id="allowed-plus-unrelated"),
        pytest.param(set(), id="empty-body"),
    ],
)
def test_builtin_agent_edit_refuses_everything_else(fields: set[str]):
    assert builtin_agent_edit_allowed(fields) is False


def test_builtin_writable_agent_fields_is_pinned():
    """Pins the decision itself, so widening the set is a visible diff."""
    assert {"enabled", "approve_tool_calls", "hide_activity_from_non_admins"} == BUILTIN_WRITABLE_AGENT_FIELDS


@pytest.fixture
def _xtm_one_mode(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "platform_mode", "xtm_one", raising=False)
    assert settings.is_xtm_one


@pytest.mark.usefixtures("_xtm_one_mode")
def test_a_builtin_agents_assignment_stays_read_only():
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        is_builtin=True,
        is_company_managed=True,
        shared_group_ids=None,
        shared_group_permissions=None,
    )
    assignment = SimpleNamespace(id=uuid.uuid4(), user_id=None, is_company_managed=True)
    admin = SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)
    err = writable_assignment_error(assignment, agent, admin, [], action="edit")
    assert err == "Cannot edit assignments on built-in agents."
