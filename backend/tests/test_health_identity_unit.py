"""Platform Concierge level 1 (#8254), without a database: the flag, the
identity rules and the settings model. Against PostgreSQL:
``test_health_identity_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.helpers import agent_tool_admin_bypass
from app.core.config import settings
from app.core.feature_flags import KNOWN_FEATURE_FLAGS
from app.services.health import identity
from app.services.health.settings import HealthBudgetCaps, HealthSettings, _read, scope_narrowed


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")


def _agent(*, role: str | None = "experimenter", company_managed: bool = True):
    return SimpleNamespace(id=uuid.uuid4(), health_role=role, is_company_managed=company_managed)


def _user(role: str = "admin", *, active: bool = True):
    return SimpleNamespace(id=uuid.uuid4(), role=role, is_active=active)


# ── The flag ──────────────────────────────────────────────────────────────


def test_there_is_no_concierge_flag_any_more() -> None:
    # Built in since #101 (S1): the flag left the known list.
    assert "CONCIERGE" not in KNOWN_FEATURE_FLAGS


# ── The identity ──────────────────────────────────────────────────────────


def test_the_principal_id_is_stable_and_only_it_names_the_concierge() -> None:
    assert identity.health_identity_id() == uuid.uuid5(uuid.NAMESPACE_URL, "xtm-one:platform-concierge")
    assert identity.is_health_identity(identity.HEALTH_PRINCIPAL_ID) is True
    assert identity.is_health_identity(str(identity.HEALTH_PRINCIPAL_ID)) is True
    assert identity.is_health_identity(uuid.uuid4()) is False
    assert identity.is_health_identity("not-a-uuid") is False
    assert identity.is_health_identity(None) is False


@pytest.mark.parametrize(
    ("agent", "user", "expected"),
    [
        (_agent(), None, True),
        (_agent(), _user("admin"), True),
        (_agent(), _user("user"), False),
        (_agent(), _user("admin", active=False), False),
        (_agent(role=None), None, False),
        (_agent(role="sandbox"), None, False),
        (_agent(company_managed=False), None, False),
    ],
)
def test_who_acts_as_the_concierge(flag_on, agent, user, expected: bool) -> None:
    principal = identity.health_principal_for(agent, user)
    assert (principal is not None) is expected
    if principal is not None:
        assert principal.agent_id == agent.id
        assert principal.on_behalf_of_admin_id == (user.id if user else None)


def test_a_concierge_agent_acts_as_the_concierge_with_no_flag(monkeypatch) -> None:
    monkeypatch.setattr(settings, "enabled_features", "")
    assert identity.health_principal_for(_agent(), None) is not None


def test_the_principal_lives_for_its_scope_with_no_flag(monkeypatch) -> None:
    monkeypatch.setattr(settings, "enabled_features", "")
    principal = identity.health_principal_for(_agent(), None)
    with identity.acting_as_health_principal(principal):
        assert identity.current_health_principal() == principal
        assert identity.in_health_run() is True
    assert identity._CURRENT.get() is None


def test_a_mark_outside_a_run_scope_marks_nothing_and_ends_with_the_scope(flag_on) -> None:
    agent = _agent()

    async def scenario() -> None:
        assert await identity.mark_health_run(None, agent, None) is None
        assert identity.current_health_principal() is None
        with identity.health_run_scope():
            marked = await identity.mark_health_run(None, agent, None)
            assert marked is not None
            assert identity.current_health_principal() == marked
        assert identity.current_health_principal() is None

    asyncio.run(scenario())


def test_a_concierge_run_never_takes_the_admin_bypass(flag_on) -> None:
    # A Concierge run has no user (a company-managed assignment run).
    assert agent_tool_admin_bypass(None) is False


def test_the_seed_alone_gives_a_role_and_only_a_known_one_to_a_company_managed_agent() -> None:
    class _Db:
        async def flush(self) -> None:
            return None

    agent = _agent(role=None)
    asyncio.run(identity.set_health_role(_Db(), agent, "flow_lab"))
    assert agent.health_role == "flow_lab"
    with pytest.raises(ValueError):
        asyncio.run(identity.set_health_role(_Db(), agent, "sandbox"))
    with pytest.raises(ValueError):
        asyncio.run(identity.set_health_role(_Db(), _agent(role=None, company_managed=False), "flow_lab"))


# ── Who a run acts as ─────────────────────────────────────────────────────


def test_a_fleet_run_with_no_user_acts_with_no_user_never_its_admin_owner() -> None:
    """The engine's one resolution: tools, quota, provider exclusions and
    variables follow it. A fleet agent's owner is an administrator (invariant 9)."""
    from app.services.assignment_engine import _acting_identity

    owner, admin = uuid.uuid4(), uuid.uuid4()
    fleet = SimpleNamespace(**vars(_agent(role="orchestrator")), user_id=owner)
    personal = SimpleNamespace(**vars(_agent(role=None, company_managed=False)), user_id=owner)
    assert _acting_identity(None, fleet) is None
    # An administrator's own "Run now" acts as that administrator, as before.
    assert _acting_identity(admin, fleet) == admin
    assert _acting_identity(None, personal) == owner


def test_the_concierge_acts_alone_only_with_nobody_behind_it(flag_on) -> None:
    agent = _agent()
    assert identity.health_acts_alone() is False
    with identity.acting_as_health_principal(identity.health_principal_for(agent, None)):
        assert identity.health_acts_alone() is True
    with identity.acting_as_health_principal(identity.health_principal_for(agent, _user("admin"))):
        assert identity.health_acts_alone() is False


def test_a_delegation_with_no_user_is_recorded_under_the_fleet_agents_owner_only(flag_on, monkeypatch) -> None:
    """``sub_agent_tasks.user_id`` needs a row: the owner of record, offered only to the Concierge acting alone."""
    owner, fallback = uuid.uuid4(), uuid.uuid4()
    fleet = SimpleNamespace(**vars(_agent(role="orchestrator")), user_id=owner)
    ownerless = SimpleNamespace(**vars(_agent(role="quality_analyst")), user_id=None)
    personal = SimpleNamespace(**vars(_agent(role=None, company_managed=False)), user_id=owner)

    async def _resolved(_db):
        return fallback

    monkeypatch.setattr("app.api.v1.helpers.resolve_company_managed_owner_id", _resolved)

    async def scenario() -> list:
        seen = [await identity.health_delegation_owner_id(None, fleet)]
        with identity.acting_as_health_principal(identity.health_principal_for(fleet, None)):
            seen += [await identity.health_delegation_owner_id(None, a) for a in (fleet, ownerless, personal)]
        with identity.acting_as_health_principal(identity.health_principal_for(fleet, _user("admin"))):
            seen.append(await identity.health_delegation_owner_id(None, fleet))
        return seen

    assert asyncio.run(scenario()) == [None, owner, fallback, None, None]


def test_a_sandbox_turn_never_takes_the_admin_bypass_principal_or_not(flag_on) -> None:
    """A copy runs as its source's owner, often an admin: its turn is the
    Concierge's reach whoever started it (an evaluation case has no principal)."""
    from app.services.health.sandbox.session import SandboxSession, sandbox_session

    admin = SimpleNamespace(**vars(_user("admin")), is_admin=True)
    assert agent_tool_admin_bypass(admin) is True
    assert identity.in_health_reach() is False
    with sandbox_session(SandboxSession(mode="sandbox")):
        assert identity.current_health_principal() is None
        assert identity.in_health_reach() is True
        assert agent_tool_admin_bypass(admin) is False
    # A dry run is a Concierge turn's own session: it is no sandbox turn.
    with sandbox_session(SandboxSession(mode="dry_run")):
        assert identity.in_sandbox_turn() is False
    with identity.acting_as_health_principal(identity.health_principal_for(_agent(), admin)):
        assert agent_tool_admin_bypass(admin) is False


def test_a_sandbox_copys_turn_keeps_the_principal_its_runner_holds(flag_on) -> None:
    """``health_turn_scoped`` would reset it for a non-fleet agent: a copy's
    turn, and any turn inside a sandbox case, keep it (``calling_health_principal``
    still refuses the copy the Concierge's own tools)."""
    from app.services.health.sandbox.session import SandboxSession, sandbox_session
    from app.services.health.turns import health_turn_scoped

    copy = SimpleNamespace(id=uuid.uuid4(), health_role=None, is_company_managed=False, is_sandbox=True)
    consulted = SimpleNamespace(id=uuid.uuid4(), health_role=None, is_company_managed=False, is_sandbox=False)
    principal = identity.health_principal_for(_agent(), None)
    seen: list = []

    @health_turn_scoped
    async def turn(db, user_id, agent):
        seen.append(identity.current_health_principal())

    async def scenario() -> None:
        with identity.acting_as_health_principal(principal):
            await turn(None, str(uuid.uuid4()), copy)
            await turn(None, str(uuid.uuid4()), consulted)
            with sandbox_session(SandboxSession(mode="sandbox")):
                await turn(None, str(uuid.uuid4()), consulted)

    asyncio.run(scenario())
    assert seen == [principal, None, principal]


# ── The settings ──────────────────────────────────────────────────────────


def test_stored_settings_read_fail_closed() -> None:
    assert _read(None) == HealthSettings()
    assert _read({"enabled": "maybe"}).enabled is False
    assert _read({"enabled": True, "removed_setting": 3}).enabled is True
    assert _read({"enabled": True, "budget": {"month_tokens": 5, "gone": 1}}).budget.month_tokens == 5
    assert HealthSettings().enabled is False
    assert HealthSettings().scope_mode == "all"


def test_a_request_body_with_an_unknown_key_or_a_negative_cap_is_refused() -> None:
    with pytest.raises(ValueError):
        HealthSettings.model_validate({"enabled": True, "dry_run_typo": True})
    with pytest.raises(ValueError):
        HealthBudgetCaps.model_validate({"cycle_tokens": -1})


@pytest.mark.parametrize(
    ("before", "after", "narrowed"),
    [
        # Switching off stops reads and takes back no consent: nothing to purge.
        ({"enabled": True}, {"enabled": False}, False),
        ({"enabled": True, "scope_mode": "selected"}, {"enabled": False}, False),
        ({"enabled": False}, {"enabled": True}, False),
        ({"enabled": True}, {"enabled": True, "scope_mode": "selected"}, True),
        ({"enabled": True, "scope_mode": "selected"}, {"enabled": True}, False),
        (
            {"enabled": True, "scope_mode": "selected", "scope_agent_ids": [str(uuid.UUID(int=1))]},
            {"enabled": True, "scope_mode": "selected"},
            True,
        ),
        (
            {"enabled": True, "scope_mode": "selected"},
            {"enabled": True, "scope_mode": "selected", "scope_flow_ids": [str(uuid.UUID(int=2))]},
            False,
        ),
    ],
)
def test_a_narrowed_scope_is_told_apart(before: dict, after: dict, narrowed: bool) -> None:
    assert scope_narrowed(HealthSettings(**before), HealthSettings(**after)) is narrowed


def test_the_assignment_engine_scope_bounds_the_mark(flag_on) -> None:
    from app.api.v1.helpers import company_managed_run_scoped

    agent = _agent()

    @company_managed_run_scoped
    async def run():
        await identity.mark_health_run(None, agent, None)
        return identity.current_health_principal()

    principal = asyncio.run(run())
    assert principal is not None and principal.agent_id == agent.id
    assert identity.current_health_principal() is None


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [({identity.HEALTH_ALONE_KEY: True}, ""), ({}, "owner"), (None, "owner")],
)
def test_a_task_the_concierge_delegated_alone_acts_and_sees_as_no_user(metadata, expected) -> None:
    """The row's owner of record never becomes the task's turn user nor the
    viewer its target agent is reloaded with."""
    from app.services.sub_agent import task_turn_user_id

    assert task_turn_user_id(metadata, "owner") == expected
