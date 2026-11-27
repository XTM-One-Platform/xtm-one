"""An agent's sharing carries its integrations (#2715), without a database.

What these pin:

* sharing an agent with a person grants "use" on each bound personal
  integration the sharer may manage, origin recorded; unsharing removes only
  those grants (a grant given earlier by hand, to a person or a group, stays);
* a grant two agents gave goes only with the last unshare;
* an integration the sharer may not manage is named as not shared, untouched;
* company-managed integrations are never touched;
* ``update_integration`` declares ``users`` / ``users_permission`` and the
  fleet health finding ``integration_not_shared`` is a safe (auto) fix whose
  evidence round-trips;
* the drift read names a sharee without access and clears once granted.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import agent_integration_shares as shares
from app.services import fleet_health_signals as signals

OWNER = str(uuid.uuid4())
OTHER_OWNER = str(uuid.uuid4())
PERSON = str(uuid.uuid4())
GROUP = str(uuid.uuid4())


def _integration(owner: str = OWNER, **kw: Any) -> Any:
    base = dict(
        id=uuid.uuid4(),
        name=kw.pop("name", "Grafana"),
        user_id=uuid.UUID(owner),
        is_company_managed=False,
        access_restricted=False,
        shared_user_ids=None,
        shared_user_permissions=None,
        shared_group_ids=None,
        shared_group_permissions=None,
        agent_share_origins=None,
        credential_mode="shared",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _agent(*integrations: Any, users: list[str] | None = None, groups: list[str] | None = None) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.UUID(OWNER),
        is_company_managed=False,
        access_restricted=False,
        shared_user_ids=users,
        shared_group_ids=groups,
        integration_bindings={"grafana": [str(i.id) for i in integrations]},
    )


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _Db:
    def __init__(self, integrations: list[Any]) -> None:
        self.integrations = integrations

    async def execute(self, _stmt: object) -> _Rows:
        return _Rows(self.integrations)

    async def flush(self) -> None:
        pass


async def _sync(db: Any, agent: Any, before: set[str], actor: str = OWNER, admin: bool = False) -> shares.ShareSync:
    return await shares.sync_agent_integration_shares(
        db, agent, before=before, actor_id=actor, actor_is_admin=admin, actor_group_ids=[]
    )


@pytest.mark.asyncio
async def test_sharing_an_agent_with_a_person_shares_its_two_integrations_and_unsharing_removes_them() -> None:
    a, b = _integration(name="Grafana"), _integration(name="Jira")
    agent = _agent(a, b)
    db = _Db([a, b])
    before = shares.principal_keys(agent)
    agent.shared_user_ids = [PERSON]
    result = await _sync(db, agent, before)
    assert result.shared == ["Grafana", "Jira"] and not result.not_shared
    for integ in (a, b):
        assert integ.shared_user_ids == [PERSON]
        assert integ.shared_user_permissions == {PERSON: "read_only"}
        assert integ.agent_share_origins == {f"user:{PERSON}": [str(agent.id)]}
    assert any("shared with the same audience" in line for line in result.lines())

    before = shares.principal_keys(agent)
    agent.shared_user_ids = None
    result = await _sync(db, agent, before)
    assert result.removed == ["Grafana", "Jira"]
    for integ in (a, b):
        assert integ.shared_user_ids is None and integ.shared_user_permissions is None
        assert integ.agent_share_origins is None


@pytest.mark.asyncio
async def test_a_grant_given_by_hand_is_never_removed_by_an_unshare() -> None:
    integ = _integration(shared_group_ids=[GROUP], shared_group_permissions={GROUP: "config"})
    agent = _agent(integ)
    db = _Db([integ])
    agent.shared_group_ids = [GROUP]
    await _sync(db, agent, set())
    # Already granted (at a higher tier): kept as it was, no origin.
    assert integ.shared_group_permissions == {GROUP: "config"}
    assert not (integ.agent_share_origins or {}).get(f"group:{GROUP}")
    before = shares.principal_keys(agent)
    agent.shared_group_ids = None
    result = await _sync(db, agent, before)
    assert not result.removed
    assert integ.shared_group_ids == [GROUP]


@pytest.mark.asyncio
async def test_a_grant_two_agents_gave_goes_with_the_last_unshare() -> None:
    integ = _integration()
    first, second = _agent(integ), _agent(integ)
    db = _Db([integ])
    for agent in (first, second):
        agent.shared_user_ids = [PERSON]
        await _sync(db, agent, set())
    assert integ.agent_share_origins == {f"user:{PERSON}": [str(first.id), str(second.id)]}
    first.shared_user_ids = None
    await _sync(db, first, {f"user:{PERSON}"})
    assert integ.shared_user_ids == [PERSON]
    second.shared_user_ids = None
    await _sync(db, second, {f"user:{PERSON}"})
    assert integ.shared_user_ids is None


@pytest.mark.asyncio
async def test_an_integration_the_sharer_may_not_manage_is_named_and_untouched() -> None:
    integ = _integration(owner=OTHER_OWNER, name="Their Slack")
    agent = _agent(integ)
    agent.shared_user_ids = [PERSON]
    result = await _sync(_Db([integ]), agent, set())
    assert not result.shared
    assert [name for name, _why in result.not_shared] == ["Their Slack"]
    assert integ.shared_user_ids is None
    assert any("NOT shared" in line for line in result.lines())


@pytest.mark.asyncio
async def test_company_managed_integrations_are_left_alone() -> None:
    integ = _integration(is_company_managed=True)
    agent = _agent(integ)
    agent.shared_user_ids = [PERSON]
    result = await _sync(_Db([integ]), agent, set(), admin=True)
    assert not result.changed and not result.not_shared
    assert integ.shared_user_ids is None


def test_the_finding_is_a_safe_fix_with_advice_and_its_evidence_round_trips() -> None:
    assert signals.FINDING_FIX["integration_not_shared"] == signals.FIX_AUTO
    assert "apply_safe_fixes" in signals.FINDING_ADVICE["integration_not_shared"]
    drift = shares.Drift(str(uuid.uuid4()), str(uuid.uuid4()), f"group:{GROUP}")
    assert shares.Drift.parse(drift.evidence) == drift
    assert shares.Drift.parse("not:evidence") is None


@pytest.mark.asyncio
async def test_the_drift_names_a_group_without_access_and_clears_after_the_safe_fix() -> None:
    integ = _integration()
    agent = _agent(integ, groups=[GROUP])
    db: Any = _Db([integ])
    [drift] = await shares.agent_share_drift(db, [agent])
    assert drift.principal == f"group:{GROUP}"
    written = await shares.grant_missing_shares(db, [drift], actor_id=OWNER, actor_is_admin=False, actor_group_ids=[])
    assert written == 1
    assert await shares.agent_share_drift(db, [agent]) == []
    # The fix records the origin, so the agent's unshare takes it back.
    assert integ.agent_share_origins == {f"group:{GROUP}": [str(agent.id)]}


def test_update_integration_declares_person_level_sharing() -> None:
    import inspect

    from app.services.builtin_tools import agent_builder

    source = inspect.getsource(agent_builder)
    start = source.index('name="update_integration"')
    schema = source[start : start + 6000]
    assert '"users":' in schema and '"users_permission":' in schema
