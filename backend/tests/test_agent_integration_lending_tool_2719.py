"""Integration lending from the agent tools (#2719).

The owner's switch "Use my integrations through this agent"
(``Agent.integration_lending``, #8824) is set by ``update_agent`` /
``create_agent`` (``integration_lending``), read by ``get_agent`` and
``list_agents``, and reported by ``get_fleet_health``
(``shared_agent_without_lending``). A consulted, delegated or transferred run
resolves the called agent's bindings with the called agent's own lending.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_health_signals as signals
from app.services.builtin_tools import agent_builder
from app.services.integration_credentials import (
    LENT_INTEGRATION_NOT_OWNED_DETAIL,
    PER_USER_CREDENTIAL_MODE,
    lendable_integration_ids,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _SequencedDB:
    """Answers each ``execute`` with the next configured row list."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls = 0

    async def execute(self, *_args, **_kwargs):
        self.calls += 1
        return _Result(self._answers.pop(0) if self._answers else [])


def _agent(owner, bindings, lending=None, **extra):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner,
        integration_bindings=bindings,
        integration_lending=lending,
        shared_group_ids=extra.get("groups"),
        shared_user_ids=extra.get("people"),
        is_company_managed=extra.get("company", False),
    )


# ── What 'true' lends ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lendable_ids_leave_out_per_user_integrations():
    owner = uuid.uuid4()
    mine, per_user = uuid.uuid4(), uuid.uuid4()
    agent = _agent(owner, {"grafana": [str(mine)], "github": [str(per_user)]})
    db = _SequencedDB([(mine, "shared"), (per_user, PER_USER_CREDENTIAL_MODE)])

    assert await lendable_integration_ids(db, agent) == [str(mine)]


@pytest.mark.asyncio
async def test_lendable_ids_of_an_agent_without_bindings_read_nothing():
    db = _SequencedDB()
    assert await lendable_integration_ids(db, _agent(uuid.uuid4(), {})) == []
    assert db.calls == 0


# ── update_agent / create_agent: integration_lending ──────────────────────


@pytest.mark.asyncio
async def test_true_lends_every_lendable_binding_with_its_audit_trail():
    owner = uuid.uuid4()
    mine = uuid.uuid4()
    agent = _agent(owner, {"grafana": [str(mine)]})
    # 1: lendable rows; 2: the ownership check of checked_integration_lending.
    db = _SequencedDB([(mine, "shared")], [mine])

    error = await agent_builder._apply_integration_lending(db, agent, owner, "true", bindings_changed=False)

    assert error is None
    assert agent.integration_lending["integration_ids"] == [str(mine)]
    assert agent.integration_lending["set_by"] == str(owner)
    assert agent.integration_lending["set_at"]


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_and_nothing_is_written():
    owner, editor = uuid.uuid4(), uuid.uuid4()
    agent = _agent(owner, {"grafana": [str(uuid.uuid4())]})
    db = _SequencedDB()

    error = await agent_builder._apply_integration_lending(db, agent, editor, "true", bindings_changed=False)

    assert error == LENT_INTEGRATION_NOT_OWNED_DETAIL
    assert agent.integration_lending is None
    assert db.calls == 0


@pytest.mark.asyncio
async def test_false_stops_lending():
    owner = uuid.uuid4()
    lent = str(uuid.uuid4())
    agent = _agent(owner, {"grafana": [lent]}, {"integration_ids": [lent], "set_by": str(owner)})

    error = await agent_builder._apply_integration_lending(
        _SequencedDB(), agent, owner, "false", bindings_changed=False
    )

    assert error is None and agent.integration_lending is None


@pytest.mark.asyncio
async def test_blank_leaves_lending_unchanged():
    owner = uuid.uuid4()
    lent = str(uuid.uuid4())
    stored = {"integration_ids": [lent], "set_by": str(owner), "set_at": "2026-11-27T00:00:00+00:00"}
    agent = _agent(owner, {"grafana": [lent]}, dict(stored))

    error = await agent_builder._apply_integration_lending(_SequencedDB(), agent, owner, "", bindings_changed=False)

    assert error is None and agent.integration_lending == stored


@pytest.mark.asyncio
async def test_a_binding_change_keeps_only_the_lent_ids_still_bound():
    owner = uuid.uuid4()
    kept, gone = str(uuid.uuid4()), str(uuid.uuid4())
    agent = _agent(owner, {"grafana": [kept]}, {"integration_ids": [kept, gone], "set_by": str(owner)})

    await agent_builder._apply_integration_lending(_SequencedDB(), agent, owner, "", bindings_changed=True)

    assert agent.integration_lending["integration_ids"] == [kept]


@pytest.mark.asyncio
async def test_true_with_nothing_lendable_is_refused():
    owner = uuid.uuid4()
    agent = _agent(owner, {"grafana": [str(uuid.uuid4())]})

    error = await agent_builder._apply_integration_lending(
        _SequencedDB([]), agent, owner, "true", bindings_changed=False
    )

    assert error == agent_builder.INTEGRATION_LENDING_NOTHING_TO_LEND
    assert agent.integration_lending is None


@pytest.mark.asyncio
async def test_a_value_other_than_true_or_false_is_refused():
    owner = uuid.uuid4()
    agent = _agent(owner, {})

    error = await agent_builder._apply_integration_lending(
        _SequencedDB(), agent, owner, "maybe", bindings_changed=False
    )

    assert error == agent_builder.INTEGRATION_LENDING_VALUE_ERROR


def test_both_tools_declare_the_field():
    definitions, _executor = agent_builder.build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    by_name = {d.name: d for d in definitions}
    assert "integration_lending" in by_name["update_agent"].input_schema["properties"]
    assert "integration_lending" in by_name["create_agent"].input_schema["properties"]


# ── get_fleet_health: shared_agent_without_lending ────────────────────────


@pytest.mark.asyncio
async def test_a_shared_member_bound_to_its_owners_unlent_integration_is_a_finding():
    owner = uuid.uuid4()
    mine = uuid.uuid4()
    member = _agent(owner, {"grafana": [str(mine)]}, groups=["g1"])
    db = _SequencedDB([member], [(mine, owner)])

    [finding] = await signals._lending_findings(db, [member.id])

    assert finding.kind == "shared_agent_without_lending"
    assert finding.evidence == [f"{member.id}:{mine}"]
    assert signals.FINDING_FIX[finding.kind] == signals.FIX_OWNER


@pytest.mark.asyncio
async def test_a_lending_or_unshared_member_is_no_finding():
    owner = uuid.uuid4()
    mine = str(uuid.uuid4())
    lending = _agent(owner, {"grafana": [mine]}, {"integration_ids": [mine]}, people=["u1"])
    private = _agent(owner, {"grafana": [str(uuid.uuid4())]})
    db = _SequencedDB([lending, private])

    assert await signals._lending_findings(db, [lending.id, private.id]) == []
    assert db.calls == 1


@pytest.mark.asyncio
async def test_a_colleagues_integration_on_a_shared_member_is_no_finding():
    """Only the owner's own rows can be lent: another person's is not this finding."""
    owner = uuid.uuid4()
    theirs = uuid.uuid4()
    member = _agent(owner, {"grafana": [str(theirs)]}, groups=["g1"])
    db = _SequencedDB([member], [(theirs, uuid.uuid4())])

    assert await signals._lending_findings(db, [member.id]) == []


def test_the_finding_has_advice():
    assert "integration_lending" in signals.FINDING_ADVICE["shared_agent_without_lending"]


# ── Consult, delegation, transfer: the called agent's own lending ─────────


@pytest.mark.asyncio
async def test_a_consulted_agent_keeps_its_own_lent_binding_for_the_chatting_person(monkeypatch):
    """Mathieu chats with an office assistant that consults its desk: the desk's
    run resolves the DESK's bindings with the DESK's lending for Mathieu
    (agent_chat._run_helper_turn -> run_agent_chat(target, user_id) ->
    resolve_agent_integration_bindings(target, user_id))."""
    from app.services import integration_tools

    owner, person = uuid.uuid4(), uuid.uuid4()
    grafana = str(uuid.uuid4())
    desk = _agent(owner, {"grafana": [grafana]}, {"integration_ids": [grafana]}, groups=["g1"])
    desk.integration_template_bindings = []
    desk.name = "desk"

    async def _no_orphans(*_a, **_k):
        return None

    async def _groups(*_a, **_k):
        return []

    monkeypatch.setattr("app.services.integration_credentials.drop_orphaned_bindings", _no_orphans)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    # shadow_invisible_bindings: nothing visible to the person; the lent row is the owner's.
    db = _SequencedDB([], [uuid.UUID(grafana)])

    resolved = await integration_tools.resolve_agent_integration_bindings(db, desk, str(person))

    assert resolved.bindings == {"grafana": grafana}


@pytest.mark.asyncio
async def test_the_same_desk_without_lending_withholds_the_owners_binding(monkeypatch):
    from app.services import integration_tools

    owner, person = uuid.uuid4(), uuid.uuid4()
    grafana = str(uuid.uuid4())
    desk = _agent(owner, {"grafana": [grafana]}, None, groups=["g1"])
    desk.integration_template_bindings = []
    desk.name = "desk"

    async def _no_orphans(*_a, **_k):
        return None

    async def _groups(*_a, **_k):
        return []

    async def _filter(*_a, **_k):
        from sqlalchemy import false

        return false()

    monkeypatch.setattr("app.services.integration_credentials.drop_orphaned_bindings", _no_orphans)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    monkeypatch.setattr(integration_tools, "template_candidate_filter", _filter)
    db = _SequencedDB([], [], [])

    resolved = await integration_tools.resolve_agent_integration_bindings(db, desk, str(person))

    assert "grafana" not in resolved.bindings
