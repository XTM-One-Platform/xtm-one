# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The Platform Health fleet is read-only for everyone, in every mode (#101 S1b)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.helpers import can_manage_as, writable_agent_error, writable_assignment_error
from app.core.config import settings
from app.services.fleets import BUILTIN_FLEET_WRITABLE_FIELDS, FleetActionRefusedError, ensure_builtin_fleet_writable
from app.services.health.read_only import HEALTH_READ_ONLY_REFUSAL, is_health_agent, is_health_fleet


def _agent(**kw):
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=True,
        is_builtin=True,
        health_role="orchestrator",
        shared_group_ids=[],
        shared_with=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _user(role: str):
    return SimpleNamespace(id=uuid.uuid4(), role=role, is_admin=role == "admin")


@pytest.mark.parametrize("mode", ["copilot", "xtm_one"])
@pytest.mark.parametrize("role", ["admin", "user"])
def test_no_one_manages_or_writes_a_health_agent(monkeypatch, mode, role):
    monkeypatch.setattr(settings, "platform_mode", mode)
    agent, user = _agent(), _user(role)
    assignment = SimpleNamespace(is_company_managed=True)

    assert can_manage_as(user.id, role == "admin", agent, []) is False
    assert writable_agent_error(agent, user, [], action="edit") == HEALTH_READ_ONLY_REFUSAL
    # The integration and opt-in carve-outs of a built-in do not open it either.
    assert writable_agent_error(agent, user, [], action="edit", allow_builtin=True) == HEALTH_READ_ONLY_REFUSAL
    assert (
        writable_assignment_error(assignment, agent, user, [], action="edit", allow_builtin=True)
        == HEALTH_READ_ONLY_REFUSAL
    )


def test_another_company_managed_agent_keeps_its_rule():
    agent = _agent(is_builtin=False, health_role=None)
    assert is_health_agent(agent) is False
    assert can_manage_as(uuid.uuid4(), True, agent, []) is True
    assert can_manage_as(uuid.uuid4(), False, agent, []) is False


def test_the_health_fleet_takes_no_write_not_even_a_switch():
    fleet = SimpleNamespace(is_builtin=True, slug="platform-health")
    assert is_health_fleet(fleet) is True
    for fields in (None, {"name"}, {"is_archived"}, set(BUILTIN_FLEET_WRITABLE_FIELDS)):
        with pytest.raises(FleetActionRefusedError) as refused:
            ensure_builtin_fleet_writable(fleet, fields)
        assert (refused.value.status_code, refused.value.detail) == (403, HEALTH_READ_ONLY_REFUSAL)


async def test_the_health_fleet_is_never_forked():
    from app.services.fleet_fork import fork_fleet

    fleet = SimpleNamespace(id=uuid.uuid4(), is_builtin=True, slug="platform-health")
    with pytest.raises(FleetActionRefusedError) as refused:
        # Refused before any read: the session is never touched.
        await fork_fleet(None, fleet, _user("admin"), [])  # type: ignore[arg-type]
    assert (refused.value.status_code, refused.value.detail) == (403, HEALTH_READ_ONLY_REFUSAL)


async def test_a_health_agent_is_never_duplicated(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import agents as agents_api
    from app.services import llm_provider_deletion

    agent = _agent()

    async def _no_lock(*_a, **_k):
        return None

    async def _groups(*_a, **_k):
        return []

    async def _found(*_a, **_k):
        return agent

    async def _never(*_a, **_k):
        raise AssertionError("a health agent must not be copied")

    monkeypatch.setattr(llm_provider_deletion, "lock_provider_configs", _no_lock)
    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _found)
    monkeypatch.setattr(agents_api, "copy_agent_for_user", _never)
    with pytest.raises(HTTPException) as refused:
        await agents_api.duplicate_agent(str(agent.id), None, user=_user("admin"), db=None)  # type: ignore[arg-type]
    assert (refused.value.status_code, refused.value.detail) == (403, HEALTH_READ_ONLY_REFUSAL)


def test_the_health_fleet_response_offers_no_control_to_an_admin() -> None:
    """The fleet response's ``can_manage`` is false on it, so the UI shows no switch the API refuses."""
    from app.api.v1.fleets import _can_manage_fleet

    admin = _user("admin")
    health = SimpleNamespace(
        is_builtin=True, slug="platform-health", is_company_managed=True, user_id=None, shared_group_ids=[]
    )
    assert _can_manage_fleet(admin, health, []) is False  # type: ignore[arg-type]
    other = SimpleNamespace(is_builtin=True, slug="ctem-posture", is_company_managed=True, user_id=None)
    assert _can_manage_fleet(admin, other, []) is True  # type: ignore[arg-type]


def test_no_prerequisite_of_the_health_fleet_is_fixable_from_its_page() -> None:
    """The live lines answer ``can_fix`` false on the health fleet, kept on any other (UX r2 B1 on #464)."""
    from app.api.v1.fleets import _lock_fixes_on_health_fleet

    health = SimpleNamespace(is_builtin=True, slug="platform-health")
    lines = [SimpleNamespace(can_fix=True), SimpleNamespace(can_fix=False)]
    _lock_fixes_on_health_fleet(health, lines)  # type: ignore[arg-type]
    assert [line.can_fix for line in lines] == [False, False]
    other = SimpleNamespace(is_builtin=True, slug="ctem-posture")
    kept = [SimpleNamespace(can_fix=True)]
    _lock_fixes_on_health_fleet(other, kept)  # type: ignore[arg-type]
    assert kept[0].can_fix is True


def test_platform_health_off_line_keeps_its_settings_fix_for_the_administrator() -> None:
    """UX r2 B3 on #980: the Settings switch stays the one fix of the health fleet.

    ``can_fix`` comes in as ``can_open`` computed it: true for an administrator,
    false for anyone else; the lock keeps it on that line and clears every other.
    """
    from app.api.v1.fleets import _lock_fixes_on_health_fleet

    health = SimpleNamespace(is_builtin=True, slug="platform-health")
    admin = [
        SimpleNamespace(reason_code="platform_health_off", can_fix=True),
        SimpleNamespace(reason_code="not_bound", can_fix=True),
    ]
    _lock_fixes_on_health_fleet(health, admin)  # type: ignore[arg-type]
    assert [line.can_fix for line in admin] == [True, False]
    member = [SimpleNamespace(reason_code="platform_health_off", can_fix=False)]
    _lock_fixes_on_health_fleet(health, member)  # type: ignore[arg-type]
    assert member[0].can_fix is False


def test_responses_carry_the_read_only_reason_field() -> None:
    from app.api.v1.schemas.agent import AgentResponse
    from app.api.v1.schemas.fleet import FleetResponse

    assert AgentResponse.model_fields["read_only_reason"].default is None
    assert FleetResponse.model_fields["read_only_reason"].default is None


def test_a_custom_fleet_named_like_it_is_not_the_health_fleet():
    assert is_health_fleet(SimpleNamespace(is_builtin=False, slug="platform-health")) is False
    ensure_builtin_fleet_writable(SimpleNamespace(is_builtin=False, slug="platform-health"), {"name"})
