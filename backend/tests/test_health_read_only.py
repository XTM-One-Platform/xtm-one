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


def test_a_custom_fleet_named_like_it_is_not_the_health_fleet():
    assert is_health_fleet(SimpleNamespace(is_builtin=False, slug="platform-health")) is False
    ensure_builtin_fleet_writable(SimpleNamespace(is_builtin=False, slug="platform-health"), {"name"})
