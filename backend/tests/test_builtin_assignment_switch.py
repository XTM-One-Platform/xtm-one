"""The switch of one built-in assignment is an operator field (#2192).

Whoever may manage a built-in agent may switch any one of its assignments
off or on, through the REST route and the agent-builder tool alike; every
other field stays the seed's. A boot keeps the pause a person stamped instead
of reverting it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.agents.builtin_agents import (
    BUILTIN_ASSIGNMENT_SWITCH_FIELDS,
    builtin_assignment_write_allowed,
)
from app.agents.seed import seed_builtin_assignments
from app.api.v1.helpers import writable_assignment_error
from app.api.v1.schemas.assignment import UpdateAssignmentRequest
from app.services.assignment_pause import is_paused_by_person
from tests.test_builtin_assignments_never_disabled import _Result
from tests.test_builtin_hitl_endpoint_wiring import (
    _admin,
    _assignment,
    _assignment_agent,
    _invoke_assignment_update,
    _member,
)


@pytest.fixture(autouse=True)
def _xtm_one_mode(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "platform_mode", "xtm_one", raising=False)


def _named_admin():
    admin = _admin()
    admin.email = "admin@example.com"
    admin.full_name = "Admin"
    return admin


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_the_switch_is_the_only_field_opened():
    assert frozenset({"enabled"}) == BUILTIN_ASSIGNMENT_SWITCH_FIELDS


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ({"enabled"}, True),
        (set(), False),
        ({"enabled", "prompt"}, False),
        ({"prompt"}, False),
        ({"enabled", "run_as_user_id"}, False),
    ],
)
def test_the_decision_on_an_ordinary_builtin_assignment(written, expected):
    agent = _assignment_agent()
    agent.slug = "ctem-discovery-analyst"
    assignment = _assignment(agent.id)
    assert builtin_assignment_write_allowed(agent, assignment, written) is expected


def test_a_non_builtin_agent_never_needs_the_carve_out():
    agent = _assignment_agent()
    agent.is_builtin = False
    assert builtin_assignment_write_allowed(agent, _assignment(agent.id), {"enabled"}) is False


def test_the_company_managed_gate_still_applies_to_the_switch():
    agent = _assignment_agent()
    assignment = SimpleNamespace(id=uuid.uuid4(), user_id=None, is_company_managed=True)
    err = writable_assignment_error(
        assignment,
        agent,
        _member(),
        [],
        action="edit",
        allow_builtin=builtin_assignment_write_allowed(agent, assignment, {"enabled"}),
    )
    assert err == "Only admins can edit company-managed assignments."


# ---------------------------------------------------------------------------
# PUT /assignments/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admin_switches_one_builtin_assignment_off(monkeypatch):
    agent = _assignment_agent()
    agent.slug = "ctem-discovery-analyst"
    assignment = _assignment(agent.id)
    assignment.enabled = True
    recorded: list = []

    async def _record(_db, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr("app.api.v1.assignments.record_pause_audit", _record)

    await _invoke_assignment_update(
        monkeypatch, UpdateAssignmentRequest(enabled=False), agent, assignment, _named_admin()
    )

    assert assignment.enabled is False
    assert assignment.paused_by_user_id is not None
    assert recorded, "the pause is audited"


@pytest.mark.asyncio
async def test_the_rest_of_a_builtin_assignment_stays_read_only(monkeypatch):
    agent = _assignment_agent()
    agent.slug = "ctem-discovery-analyst"
    assignment = _assignment(agent.id)
    assignment.enabled = True

    with pytest.raises(HTTPException) as exc:
        await _invoke_assignment_update(
            monkeypatch,
            UpdateAssignmentRequest(enabled=False, prompt="rewritten"),
            agent,
            assignment,
            _admin(),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Cannot edit assignments on built-in agents."
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_a_member_cannot_switch_a_company_managed_builtin_assignment(monkeypatch):
    agent = _assignment_agent()
    assignment = _assignment(agent.id)
    assignment.enabled = True

    with pytest.raises(HTTPException) as exc:
        await _invoke_assignment_update(
            monkeypatch, UpdateAssignmentRequest(enabled=False), agent, assignment, _member()
        )

    assert exc.value.status_code == 403
    assert assignment.enabled is True


# ---------------------------------------------------------------------------
# update_assignment (agent-builder tool)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_loader_lifts_the_gate_for_the_switch_alone(monkeypatch):
    from app.services.builtin_tools import agent_builder

    agent = _assignment_agent()
    agent.slug = "ctem-discovery-analyst"
    assignment = _assignment(agent.id)
    user = _admin()

    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", AsyncMock(return_value=user))
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.v1.helpers.find_user_assignment", AsyncMock(return_value=assignment))

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(scalar=agent))

    row, _agent, err = await agent_builder._load_writable_assignment_for_builder(
        db, user.id, assignment.id, action="edit", written=frozenset({"enabled"})
    )
    assert err is None and row is assignment

    row, _agent, err = await agent_builder._load_writable_assignment_for_builder(
        db, user.id, assignment.id, action="edit", written=frozenset({"enabled", "prompt"})
    )
    assert row is None
    assert err == "Cannot edit assignments on built-in agents."


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------


def _seed_entry(slug: str) -> dict:
    return {
        "agent_slug": slug,
        "name": "Weekly discovery assessment",
        "description": "Test entry",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 8 * * 1"},
        "run_as_all_users": True,
        "prompt": "Assess the discovery stage.",
        "enabled": True,
    }


async def _seed(monkeypatch, existing):
    agent = SimpleNamespace(id=uuid.uuid4(), slug="ctem-discovery-analyst", user_id=None, is_company_managed=True)
    monkeypatch.setattr("app.agents.seed.BUILTIN_ASSIGNMENTS", [_seed_entry(agent.slug)], raising=True)
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _Result(scalar=agent),
            _Result(items=[]),
            _Result(scalar=agent),
            _Result(scalar=existing, items=[existing]),
        ]
    )
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    await seed_builtin_assignments(db, platform_mode="xtm_one")


def _paused(reason: str, by_user: bool) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=False,
        user_id=None,
        paused_reason=reason,
        paused_by_user_id=uuid.uuid4() if by_user else None,
        paused_by_label="Admin" if by_user else None,
        paused_at=None,
        paused_details=None,
        trigger_config={},
        run_as_user_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["user_toggle", "agent_tool", "created_disabled"])
async def test_a_boot_keeps_a_persons_pause(monkeypatch, reason):
    existing = _paused(reason, by_user=True)
    await _seed(monkeypatch, existing)
    assert existing.enabled is False
    assert existing.paused_reason == reason


@pytest.mark.asyncio
async def test_a_boot_still_reverts_a_pause_nobody_decided(monkeypatch):
    existing = _paused("created_disabled", by_user=False)
    assert is_paused_by_person(existing) is False
    await _seed(monkeypatch, existing)
    assert existing.enabled is True
