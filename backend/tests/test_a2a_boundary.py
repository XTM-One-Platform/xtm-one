"""What identity external work runs as.

There is no A2A-specific data scope to test: a run sees what the user it runs
as sees. What is enforceable is *which user* that is, and that it is never the
person who flipped the exposure switch — the platform's own answer for a
company-managed agent being ``Assignment.run_as_user_id``, an explicit,
admin-chosen human. These pin that A2A follows it.
"""

import uuid

import pytest

from app.a2a import exposure as expo


class _Agent:
    def __init__(self, *, company_managed=False, owner=None):
        self.id = uuid.uuid4()
        self.user_id = owner or uuid.uuid4()
        self.is_company_managed = company_managed
        self.is_builtin = False


class TestPersonallyOwnedAgent:
    def test_runs_as_its_owner(self):
        agent = _Agent()
        assert expo.resolve_run_as(agent) == agent.user_id

    def test_an_ownerless_agent_cannot_be_exposed(self):
        agent = _Agent()
        agent.user_id = None
        with pytest.raises(expo.RunAsRequiredError):
            expo.resolve_run_as(agent)


class TestCompanyManagedAgent:
    def test_inherits_the_stored_owner(self):
        """Exposure is implicit in binding an A2A integration, so there is no
        moment at which to demand a run-as choice — the agent's stored owner
        is inherited, always.

        This is an ACCEPTED trade-off, not an oversight: for a company-managed
        agent that column holds whoever seeded it, so the partner sees that
        person's data. It is why the run-as identity is surfaced in the UI and
        the audit trail rather than left implicit."""
        agent = _Agent(company_managed=True)
        assert expo.resolve_run_as(agent) == agent.user_id

    def test_an_ownerless_company_agent_still_cannot_be_exposed(self):
        """Nothing to inherit: fail closed rather than invent an identity."""
        agent = _Agent(company_managed=True)
        agent.user_id = None
        with pytest.raises(expo.RunAsRequiredError):
            expo.resolve_run_as(agent)


class TestExposingNeverLendsTheExposersReach:
    def test_resolution_cannot_see_the_actor(self):
        """The strongest available guarantee: the signature has no actor —
        and, since the expose endpoint went, no nomination either. There is
        nothing to pass but the agent, so the identity cannot come from
        anywhere else."""
        import inspect

        params = list(inspect.signature(expo.resolve_run_as).parameters)
        assert params == ["agent"]

    def test_the_hook_that_exposes_has_no_actor_to_lend_from(self):
        """Structural, and stronger than the assertion it replaces. Exposure
        follows an integration binding, so it is created by a before_flush
        listener with no request context at all — there is no actor in scope
        for the run-as identity to be taken from even by mistake."""
        import inspect

        from app.a2a import hooks

        src = inspect.getsource(hooks._run_as_or_none)
        assert "resolve_run_as(agent)" in src
        # Neither entry point records an actor, because neither has one.
        assert "exposed_by_user_id" not in inspect.getsource(hooks)


class TestInvalidation:
    def _exposure(self, run_as):
        return type("E", (), {"run_as_user_id": run_as})()

    def test_ownership_change_invalidates(self):
        agent = _Agent()
        exposure = self._exposure(uuid.uuid4())
        assert expo.exposure_invalidated(exposure, agent) is True

    def test_unchanged_owner_does_not_invalidate(self):
        agent = _Agent()
        assert expo.exposure_invalidated(self._exposure(agent.user_id), agent) is False
