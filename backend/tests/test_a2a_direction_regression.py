"""Direction gates exposure — the shipped-behaviour guard for SC-020.

This is the regression suite for the one change this feature makes to code
that is already live. Before direction existed, binding ANY A2A integration
was the exposure opt-in, and ``binds_a2a`` tested provider-key membership.
Once a shipped provider key can carry ``direction='outbound'``, that test
exposes an agent bound purely to obtain a delegation tool — to a partner
nobody approved.

SC-020 requires the property across every path that reaches the exposure
hook, because they are separate code paths: ``_sync_implicit_exposure``
serves an EDITED agent and ``_sync_new_agent_exposure`` serves one CREATED
with bindings already set. A direction bug fixed in one and not the other is
exactly the kind of half-fix these tests exist to catch.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext

import pytest

from app.a2a.providers import (
    ENTRA_A2A_PROVIDER,
    OAUTH_A2A_PROVIDER,
    binds_a2a,
    bound_integration_ids,
    inbound_integration_ids,
)
from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND
from app.models.agent import Agent


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Answers the one query ``directions_for`` makes.

    A stub rather than a real session because these tests pin a decision
    rule, not SQL: the same rule must hold whatever the storage. The
    Postgres-backed behaviour is covered by the integration-marked tests.
    """

    no_autoflush = nullcontext()

    def __init__(self, directions: dict[uuid.UUID, str]):
        self.directions = directions
        self.queries = 0

    def execute(self, stmt):
        self.queries += 1
        return _FakeResult(list(self.directions.items()))


def _agent_binding(**by_provider: uuid.UUID) -> Agent:
    agent = Agent(name="bound")
    agent.integration_bindings = {k: [str(v)] for k, v in by_provider.items()}
    return agent


class TestDirectionGatesExposure:
    def test_outbound_binding_does_not_expose(self):
        # THE regression. An operator binds an integration to get a delegation
        # tool; the agent must not become reachable by that partner.
        iid = uuid.uuid4()
        agent = _agent_binding(oauth_a2a=iid)
        session = _FakeSession({iid: DIRECTION_OUTBOUND})
        assert binds_a2a(session, agent) is False
        assert inbound_integration_ids(session, agent) == []

    def test_inbound_binding_still_exposes(self):
        # The shipped behaviour must be untouched for an inbound integration.
        iid = uuid.uuid4()
        agent = _agent_binding(oauth_a2a=iid)
        session = _FakeSession({iid: DIRECTION_INBOUND})
        assert binds_a2a(session, agent) is True
        assert inbound_integration_ids(session, agent) == [iid]

    def test_missing_settings_row_is_neither_direction(self):
        # Fail-closed backstop: an A2A integration with no settings row
        # exposes nothing and confers nothing. A missing row must degrade to
        # a visible "not exposed", never to an unintended exposure.
        #
        # Only the "exposes nothing" half is asserted here. "Confers nothing"
        # is checked where the tool is actually built —
        # test_a2a_client_tool_wiring's
        # ``test_a_missing_settings_row_confers_no_tool`` — rather than
        # against an agent-level helper that nothing called.
        iid = uuid.uuid4()
        agent = _agent_binding(entra_a2a=iid)
        session = _FakeSession({})  # no row for iid
        assert bound_integration_ids(agent) == [iid]  # it IS bound...
        assert binds_a2a(session, agent) is False  # ...but not exposed

    def test_mixed_bindings_expose_only_via_the_inbound_one(self):
        # An agent may legitimately be reachable by one partner and able to
        # call another. Exposure must follow only the inbound half.
        inbound, outbound = uuid.uuid4(), uuid.uuid4()
        agent = _agent_binding(oauth_a2a=inbound, entra_a2a=outbound)
        session = _FakeSession(
            {inbound: DIRECTION_INBOUND, outbound: DIRECTION_OUTBOUND}
        )
        assert binds_a2a(session, agent) is True
        assert inbound_integration_ids(session, agent) == [inbound]

    def test_last_inbound_binding_removed_stops_exposure_despite_outbound(self):
        # The ``elif`` in the hook: an agent that still binds an outbound
        # integration is NOT still exposed. Testing "binds any A2A
        # integration" here would leave a dead exposure resolving.
        outbound = uuid.uuid4()
        agent = _agent_binding(entra_a2a=outbound)
        session = _FakeSession({outbound: DIRECTION_OUTBOUND})
        assert binds_a2a(session, agent) is False

    @pytest.mark.parametrize("provider", [OAUTH_A2A_PROVIDER, ENTRA_A2A_PROVIDER])
    def test_property_holds_for_every_shipped_mechanism(self, provider):
        # Both shipped keys can now carry either direction, so neither may be
        # special-cased into always exposing.
        iid = uuid.uuid4()
        agent = Agent(name="a")
        agent.integration_bindings = {provider: [str(iid)]}
        assert binds_a2a(_FakeSession({iid: DIRECTION_OUTBOUND}), agent) is False
        assert binds_a2a(_FakeSession({iid: DIRECTION_INBOUND}), agent) is True


class TestUnboundAgentUnaffected:
    def test_agent_with_no_bindings_is_not_exposed(self):
        session = _FakeSession({})
        assert binds_a2a(session, Agent(name="a")) is False

    def test_malformed_binding_is_skipped_not_raised(self):
        # Bindings are JSON: a malformed entry is data, not an error. Pinned
        # because the direction lookup added a second place it passes through.
        agent = Agent(name="a")
        agent.integration_bindings = {OAUTH_A2A_PROVIDER: ["not-a-uuid"]}
        assert bound_integration_ids(agent) == []
        assert binds_a2a(_FakeSession({}), agent) is False


class TestHookCallSitesResolveDirection:
    def test_both_hook_paths_pass_a_session(self):
        # SC-020 covers the edit path AND the create path because they are
        # separate functions. A call site that forgot the session would be a
        # TypeError at runtime rather than a silent wrong answer, and this
        # asserts both were actually updated.
        import inspect

        from app.a2a import hooks

        for fn in (hooks._sync_new_agent_exposure, hooks._sync_implicit_exposure):
            src = inspect.getsource(fn)
            assert "binds_a2a(session, agent)" in src, (
                f"{fn.__name__} must resolve direction, not provider membership"
            )
