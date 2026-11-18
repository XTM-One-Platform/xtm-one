"""The automatic-revocation listener.

Two properties matter here and neither is obvious from reading the call site,
so both are pinned rather than left to be rediscovered:

1. The listener is NOT gated on chat availability, on purpose.
2. It reads ``session.dirty`` only — a newly created agent must never look
   like an agent whose tier just changed.
"""

import uuid
from contextlib import nullcontext

from sqlalchemy.orm import Session

from app.a2a import hooks
from app.models.a2a import (
    REVOKED_AGENT_DISABLED,
    REVOKED_OWNER_CHANGE,
    REVOKED_SHARING_CHANGE,
    REVOKED_TIER_CHANGE,
    A2AExposure,
)
from app.models.agent import Agent
from tests.a2a_session_doubles import TxnSurface as _TxnSurface


class TestWatchedAttributes:
    def test_tier_flip_revokes(self):
        agent = Agent(name="a")
        agent.is_company_managed = True
        assert hooks._revocation_reason(agent) == REVOKED_TIER_CHANGE

    def test_sharing_change_revokes(self):
        agent = Agent(name="a")
        agent.shared_group_ids = [str(uuid.uuid4())]
        assert hooks._revocation_reason(agent) == REVOKED_SHARING_CHANGE

    def test_permission_tier_change_revokes(self):
        agent = Agent(name="a")
        agent.shared_group_permissions = {str(uuid.uuid4()): "config"}
        assert hooks._revocation_reason(agent) == REVOKED_SHARING_CHANGE

    def test_per_user_grant_change_revokes(self):
        """Per-user grants confer the same config-tier authority a
        group grant does (``can_manage`` honours both), so changing them must
        revoke for the same reason: the grant may be the entire authority
        under which the agent was exposed."""
        agent = Agent(name="a")
        agent.shared_user_ids = [str(uuid.uuid4())]
        assert hooks._revocation_reason(agent) == REVOKED_SHARING_CHANGE

    def test_per_user_permission_tier_change_revokes(self):
        agent = Agent(name="a")
        agent.shared_user_permissions = {str(uuid.uuid4()): "config"}
        assert hooks._revocation_reason(agent) == REVOKED_SHARING_CHANGE

    def test_access_restricted_flip_does_not_revoke(self):
        """Deliberate exclusion, pinned. The flag narrows INTERNAL visibility
        of a company-managed agent; the exposure's boundary (the run-as user)
        and the authority to expose (admin-only) both stay put, so tearing
        down a partner integration over it would revoke an exposure that is
        still exactly as approved."""
        agent = Agent(name="a")
        agent.access_restricted = True
        assert hooks._revocation_reason(agent) is None

    def test_disabling_revokes(self):
        agent = Agent(name="a", enabled=True)
        agent.enabled = False
        assert hooks._revocation_reason(agent) == REVOKED_AGENT_DISABLED

    def test_re_enabling_does_not_revoke(self):
        """Only disabling matters. Re-enabling an agent must not tear down an
        exposure that is live and still correctly bounded."""
        agent = Agent(name="a", enabled=False)
        agent.enabled = True
        assert hooks._revocation_reason(agent) is None

    def test_untouched_agent_is_ignored(self):
        assert hooks._revocation_reason(Agent(name="a")) is None

    def test_unwatched_attribute_is_ignored(self):
        agent = Agent(name="a")
        agent.name = "renamed"
        assert hooks._revocation_reason(agent) is None


class TestNotGatedOnChat:
    """Answering "should this be behind the chat gate?" — no, and here is the guard.

    The routes gate reachability; this listener maintains an invariant. Exposure
    rows outlive the gate — AI switched off, or a deployment whose last EE
    platform disconnects, keeps its rows intact — so gating the listener would
    leave a window in which an agent's tier changes without its exposure being
    revoked, and chat coming back, an ordinary operator action, would then
    resolve a stale exposure under a boundary nobody re-approved.
    """

    def test_the_listener_never_consults_the_gate(self):
        """Checks usage, not text — the docstring names the gate to explain
        why it is deliberately not consulted."""
        import inspect as pyi

        assert not hasattr(hooks, "is_chat_enabled")
        code_lines = [
            line
            for line in pyi.getsource(hooks).splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        assert not [line for line in code_lines if "is_chat_" in line]

    def test_revocation_fires_regardless_of_the_gate(self, monkeypatch):
        """Chat refused everywhere it is consulted; revocation still fires."""
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch, enabled=False)
        agent = Agent(name="a")
        agent.is_company_managed = True
        assert hooks._revocation_reason(agent) == REVOKED_TIER_CHANGE

    def test_exposure_is_minted_regardless_of_the_gate(self, monkeypatch):
        """The bind half too, and for the same reason. An exposure row that
        failed to appear while A2A was not offered would leave the agent
        reading as exposed everywhere — every surface derives that from
        ``binds_a2a`` — with nothing to resolve once chat came back."""
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch, enabled=False)
        agent = Agent(name="a", enabled=True)
        agent.user_id = uuid.uuid4()
        agent.integration_bindings = {"entra_a2a": [str(uuid.uuid4())]}

        class _Session(_TxnSurface):
            def __init__(self):
                self.added = []

            def add(self, obj):
                self.added.append(obj)

        session = _Session()
        hooks._expose(session, agent)
        assert [o for o in session.added if isinstance(o, A2AExposure)]


class _EmptyScalars:
    def __iter__(self):
        return iter(())

    def all(self):
        return []


class _EmptyResult:
    def scalars(self):
        return _EmptyScalars()

    def all(self):
        # ``a2a.providers.directions_for`` calls ``.all()`` when resolving an
        # integration's direction. Returning nothing means "no settings row",
        # which the callers read as neither direction — so a double that has
        # not opted into a direction leaves an agent unexposed, matching the
        # fail-closed rule rather than silently exposing it.
        return []


class _InboundResult:
    """A direction lookup that reports *ids* as inbound.

    Used by the tests that predate direction and assert exposure happens:
    exposure still follows a binding, for an INBOUND integration.
    """

    def __init__(self, ids):
        self._ids = list(ids)

    def scalars(self):
        return _EmptyScalars()

    def all(self):
        return [(i, "inbound") for i in self._ids]


class TestListenerScope:
    class _Session(_TxnSurface):
        no_autoflush = nullcontext()

        def __init__(self, dirty, inbound_ids=()):
            self.dirty = dirty
            self.new = []
            self.queried = []
            self.added = []
            self.info = {}
            # Which bound integrations this session reports as INBOUND.
            # Empty means "no settings row", which reads as neither direction
            # — the fail-closed default, so a test must opt in to exposure.
            self.inbound_ids = list(inbound_ids)

        def execute(self, stmt):
            self.queried.append(stmt)
            return _InboundResult(self.inbound_ids) if self.inbound_ids else _EmptyResult()

        def add(self, obj):
            self.added.append(obj)

    def test_non_agent_dirty_objects_are_skipped(self):
        session = self._Session(dirty=[object(), "not an agent", 42])
        hooks._before_flush(session, None, None)
        assert session.queried == []

    def test_unchanged_agent_triggers_no_lookup(self):
        session = self._Session(dirty=[Agent(name="a")])
        hooks._before_flush(session, None, None)
        assert session.queried == []

    def test_changed_agent_triggers_a_lookup(self):
        agent = Agent(name="a")
        agent.is_company_managed = True
        session = self._Session(dirty=[agent])
        hooks._before_flush(session, None, None)
        assert len(session.queried) == 1

    def test_a_new_agent_never_takes_the_revocation_path(self):
        """``session.new`` is read, but only for the BIND half.

        A pending agent has no loaded values to diff against, so every
        attribute reads as changed — routing one through ``_revocation_reason``
        would make ordinary agent creation look like a tier change and fire a
        revocation lookup for an exposure that cannot exist. The two passes are
        separate for exactly this reason.
        """
        agent = Agent(name="a")
        agent.integration_bindings = {}
        session = self._Session(dirty=[])
        session.new = [agent]
        hooks._before_flush(session, None, None)
        # Binds nothing, so the bind half returns before any lookup — and the
        # revocation half never sees it at all.
        assert session.queried == []

    def test_a_new_agent_that_binds_a2a_is_queued_for_exposure(self):
        """The invariant every read surface assumes: an agent created with a
        binding already set becomes reachable. Without this the UI derives
        "exposed" from ``binds_a2a`` while no exposure row exists, and the
        partner's credential resolves to nothing until somebody edits bindings
        again.

        Queued, not added — nothing orders ``agents`` before ``a2a_exposures``
        in the unit of work, so the row is written by ``_after_flush`` once the
        agent exists. ``test_a2a_hooks_postgres`` covers that end to end
        against a real foreign key; here it is the queueing and the settled
        primary key.
        """
        agent = Agent(name="a", enabled=True)
        agent.user_id = uuid.uuid4()
        integration_id = uuid.uuid4()
        agent.integration_bindings = {"oauth_a2a": [str(integration_id)]}
        # Inbound: exposure follows a binding whose integration faces inward.
        session = self._Session(dirty=[], inbound_ids=[integration_id])
        session.new = [agent]
        hooks._before_flush(session, None, None)

        queued = session.info.get(hooks._PENDING) or []
        assert [(a.id, r) for a, r in queued] == [(agent.id, agent.user_id)]
        # The key must be settled here: the queued insert names it as a
        # foreign key, and the column default would not run until INSERT.
        assert agent.id is not None
        # Nothing ORM-added — that is what raced the agent's own INSERT.
        assert [o for o in session.added if isinstance(o, A2AExposure)] == []

    def test_a_new_ownerless_agent_is_not_queued(self):
        """Fail closed: no identity to run as, so no exposure — checked before
        queueing rather than at write time, where a refusal would be silent."""
        agent = Agent(name="a", enabled=True)
        agent.user_id = None
        agent.integration_bindings = {"oauth_a2a": [str(uuid.uuid4())]}
        session = self._Session(dirty=[])
        session.new = [agent]
        hooks._before_flush(session, None, None)
        assert not session.info.get(hooks._PENDING)

    def test_a_new_disabled_agent_is_not_queued(self):
        agent = Agent(name="a", enabled=False)
        agent.user_id = uuid.uuid4()
        agent.integration_bindings = {"oauth_a2a": [str(uuid.uuid4())]}
        session = self._Session(dirty=[])
        session.new = [agent]
        hooks._before_flush(session, None, None)
        assert not session.info.get(hooks._PENDING)


class TestRegistration:
    def test_is_idempotent(self):
        from sqlalchemy import event

        hooks.register_a2a_hooks()
        hooks.register_a2a_hooks()
        assert event.contains(Session, "before_flush", hooks._before_flush)


class TestEverythingRegistersUnconditionally:
    """The listener, the grant and the routers register whether or not chat
    is enabled; the gate is asked per request (``require_chat_enabled``). A
    startup check would reintroduce the stale-boundary hazard for the listener
    and, for the routers, a 404 that flips with the gate — so the shape is
    pinned here.
    """

    def test_no_a2a_registration_is_gated(self):
        import inspect as pyi

        import app.main

        src = pyi.getsource(app.main)
        for registered in (
            "a2a_hooks.register_a2a_hooks()",
            "a2a_grants.register_a2a_grants()",
            "a2a_server.router",
            "a2a_admin.router",
            "a2a_client.router",
        ):
            assert registered in src, registered
        assert "is_chat_enabled" not in src


class TestRevocationRequiresARealChange:
    """Assignment of an identical value must not revoke a live exposure.

    ``History.has_changes()`` reports True on assignment even when the value
    is unchanged, and ``agents.update_agent`` assigns the sharing fields
    whenever they appear in the payload. So re-saving an agent without
    touching its sharing silently dropped a partner integration.
    """

    @staticmethod
    def _persistent(**kw):
        from sqlalchemy import inspect as sa_inspect

        agent = Agent(name="a", **kw)
        # Mark the current values as loaded-from-database, so attribute
        # history behaves as it does for the persistent objects the listener
        # actually sees in session.dirty.
        sa_inspect(agent)._commit_all({})
        return agent

    def test_identical_group_ids_do_not_revoke(self):
        agent = self._persistent(shared_group_ids=["g1"])
        agent.shared_group_ids = ["g1"]
        assert hooks._revocation_reason(agent) is None

    def test_reordered_group_ids_do_not_revoke(self):
        """Order carries no meaning for a set of groups."""
        agent = self._persistent(shared_group_ids=["g1", "g2"])
        agent.shared_group_ids = ["g2", "g1"]
        assert hooks._revocation_reason(agent) is None

    def test_identical_permissions_do_not_revoke(self):
        agent = self._persistent(shared_group_permissions={"g1": "config"})
        agent.shared_group_permissions = {"g1": "config"}
        assert hooks._revocation_reason(agent) is None

    def test_identical_user_grants_do_not_revoke(self):
        """The no-op guard covers the per-user columns too: the sharing
        helpers re-assign them wholesale, and an unchanged re-assignment must
        not drop a partner integration over an unrelated edit."""
        agent = self._persistent(shared_user_ids=["u1"])
        agent.shared_user_ids = ["u1"]
        assert hooks._revocation_reason(agent) is None

    def test_added_user_grant_revokes(self):
        agent = self._persistent(shared_user_ids=["u1"])
        agent.shared_user_ids = ["u1", "u2"]
        assert hooks._revocation_reason(agent) == REVOKED_SHARING_CHANGE

    def test_removed_user_grant_revokes(self):
        """The gap this closes: a per-user config grant can be the entire
        authority under which the agent was exposed, and revoking the grant
        used to leave the exposure standing."""
        agent = self._persistent(shared_user_permissions={"u1": "config"})
        agent.shared_user_permissions = None
        assert hooks._revocation_reason(agent) == REVOKED_SHARING_CHANGE

    def test_added_group_revokes(self):
        agent = self._persistent(shared_group_ids=["g1"])
        agent.shared_group_ids = ["g1", "g2"]
        assert hooks._revocation_reason(agent) == REVOKED_SHARING_CHANGE

    def test_permission_tier_change_revokes(self):
        agent = self._persistent(shared_group_permissions={"g1": "config"})
        agent.shared_group_permissions = {"g1": "read_only"}
        assert hooks._revocation_reason(agent) == REVOKED_SHARING_CHANGE

    def test_tier_flip_revokes(self):
        agent = self._persistent(is_company_managed=False)
        agent.is_company_managed = True
        assert hooks._revocation_reason(agent) == REVOKED_TIER_CHANGE

    def test_disabling_revokes_but_re_enabling_does_not(self):
        off = self._persistent(enabled=True)
        off.enabled = False
        assert hooks._revocation_reason(off) == REVOKED_AGENT_DISABLED
        on = self._persistent(enabled=False)
        on.enabled = True
        assert hooks._revocation_reason(on) is None


class TestCredentialsAreDeletedNotOrphaned:
    def test_revocation_deletes_the_client_row(self):
        """Nulling a2a_exposure_id left a row no cleanup path could find
        again — every lookup filters on that same column — and an orphan is
        indistinguishable from a DCR client, so it also occupied a slot under
        the registration cap forever."""
        import inspect as pyi

        src = pyi.getsource(hooks._revoke_live_exposures)
        assert "delete(OAuthClient)" in src
        assert "a2a_exposure_id=None" not in src

    def test_owner_is_notified_on_automatic_revocation(self):
        """The UI promises this; nothing called notify_owner_revoked before."""
        import inspect as pyi

        src = pyi.getsource(hooks._revoke_live_exposures)
        assert "ACTIVITY_REVOKED" in src


class TestOwnerTransferRevokes:
    """An owner transfer moves the boundary, so it must revoke.

    ``_WATCHED`` originally covered only tier, sharing and ``enabled``.
    ``helpers.apply_owner_transfer`` reassigns ``agent.user_id`` and touches
    none of those, so transferring a personally-owned exposed agent left the
    exposure live with ``run_as_user_id`` still pointing at the PREVIOUS owner:
    every later partner submission kept reading that person's private and
    group-shared data through an agent they no longer owned. Same silent
    widening FR-041 already caught for the tier flip, other half.
    """

    @staticmethod
    def _persistent(**kw):
        from sqlalchemy import inspect as sa_inspect

        agent = Agent(name="a", **kw)
        sa_inspect(agent)._commit_all({})
        return agent

    def test_owner_transfer_revokes(self):
        agent = self._persistent(user_id=uuid.uuid4(), is_company_managed=False)
        agent.user_id = uuid.uuid4()
        assert hooks._revocation_reason(agent) == REVOKED_OWNER_CHANGE

    def test_re_saving_the_same_owner_does_not_revoke(self):
        """``update_agent`` assigns ``user_id`` whenever it is in the payload,
        so an unchanged value must not drop a partner integration."""
        owner = uuid.uuid4()
        agent = self._persistent(user_id=owner, is_company_managed=False)
        agent.user_id = owner
        assert hooks._revocation_reason(agent) is None

    def test_company_managed_owner_change_does_not_revoke(self):
        """A company-managed agent's run-as identity is an administrator's
        explicit choice, not its stored owner — which is merely whoever seeded
        it. Moving that provenance field cannot alter the boundary, so revoking
        would break a partner over a change that does not affect them."""
        agent = self._persistent(user_id=uuid.uuid4(), is_company_managed=True)
        agent.user_id = uuid.uuid4()
        assert hooks._revocation_reason(agent) is None

    def test_tier_flip_still_wins_over_owner_backfill(self):
        """Company -> private sets ``user_id`` for legacy owner-less agents in
        the same update. The tier change is the more accurate reason, and
        ``_WATCHED`` order is what makes it the reported one."""
        agent = self._persistent(user_id=None, is_company_managed=True)
        agent.is_company_managed = False
        agent.user_id = uuid.uuid4()
        assert hooks._revocation_reason(agent) == REVOKED_TIER_CHANGE


class TestOwnerChangeConfirmsTheMismatch:
    """The owner-change reason is confirmed per-exposure before anything is
    torn down, through ``exposure.exposure_invalidated`` — the one place that
    comparison lives, and dead code until ``user_id`` joined ``_WATCHED``."""

    class _ExposureResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class _Session(_TxnSurface):
        """Answers the exposure lookup, then swallows the credential DELETE."""

        no_autoflush = nullcontext()

        def __init__(self, exposures):
            self.dirty = []
            self.new = []
            self._exposures = exposures
            self._calls = 0

            outer = TestOwnerChangeConfirmsTheMismatch

            def _execute(_stmt):
                self._calls += 1
                if self._calls == 1:
                    return outer._ExposureResult(self._exposures)
                return outer._ExposureResult([])

            self.execute = _execute

        def add(self, obj):
            self.new.append(obj)

        def get(self, _model: object, ident: object) -> object:
            return self.users.get(ident)

        users: dict[object, object] = {}

    @staticmethod
    def _exposure(run_as_user_id, agent_id):
        from app.models.a2a import A2AExposure

        return A2AExposure(
            agent_id=agent_id,
            card_token="tok",
            run_as_user_id=run_as_user_id,
        )

    def test_stale_run_as_user_is_revoked(self):
        old_owner, new_owner = uuid.uuid4(), uuid.uuid4()
        agent = Agent(name="a", user_id=new_owner, is_company_managed=False)
        agent.id = uuid.uuid4()
        exposure = self._exposure(old_owner, agent.id)
        session = self._Session([exposure])

        hooks._revoke_live_exposures(session, agent, REVOKED_OWNER_CHANGE)

        assert exposure.revoked_at is not None
        assert exposure.revoked_reason == REVOKED_OWNER_CHANGE

    def test_matching_run_as_user_is_left_alone(self):
        """Belt and braces: an exposure already pointing at the new owner has
        no boundary to move, so it survives."""
        owner = uuid.uuid4()
        agent = Agent(name="a", user_id=owner, is_company_managed=False)
        agent.id = uuid.uuid4()
        exposure = self._exposure(owner, agent.id)
        session = self._Session([exposure])

        hooks._revoke_live_exposures(session, agent, REVOKED_OWNER_CHANGE)

        assert exposure.revoked_at is None
        assert session.new == []

    def test_other_reasons_are_not_gated_on_a_mismatch(self):
        """A disabled agent's exposure still runs as its owner, so gating every
        reason on ``exposure_invalidated`` would revoke nothing at all."""
        owner = uuid.uuid4()
        agent = Agent(name="a", user_id=owner, is_company_managed=False)
        agent.id = uuid.uuid4()
        exposure = self._exposure(owner, agent.id)
        session = self._Session([exposure])

        hooks._revoke_live_exposures(session, agent, REVOKED_AGENT_DISABLED)

        assert exposure.revoked_reason == REVOKED_AGENT_DISABLED

    def test_disable_of_a_deactivated_run_as_keeps_the_exposure(self) -> None:
        """The deactivation cascade disables a departed account's private
        agents. Its exposures stop resolving through the ``is_active`` join
        already, and revoking them would change the card URL a partner holds
        when the account is reactivated (FR-007: cease resolving)."""
        from app.models.user import User

        owner = uuid.uuid4()
        agent = Agent(name="a", user_id=owner, is_company_managed=False)
        agent.id = uuid.uuid4()
        exposure = self._exposure(owner, agent.id)
        session = self._Session([exposure])
        session.users = {owner: User(id=owner, email="gone@test.local", is_active=False)}

        assert hooks._revoke_live_exposures(session, agent, REVOKED_AGENT_DISABLED) is False
        assert exposure.revoked_at is None
        assert session.new == []

    def test_disable_of_an_active_run_as_still_revokes(self) -> None:
        from app.models.user import User

        owner = uuid.uuid4()
        agent = Agent(name="a", user_id=owner, is_company_managed=False)
        agent.id = uuid.uuid4()
        exposure = self._exposure(owner, agent.id)
        session = self._Session([exposure])
        session.users = {owner: User(id=owner, email="here@test.local", is_active=True)}

        assert hooks._revoke_live_exposures(session, agent, REVOKED_AGENT_DISABLED) is True
        assert exposure.revoked_reason == REVOKED_AGENT_DISABLED


class TestAutomaticRevocationIsAccountable:
    """FR-041's other half: the automatic teardown is audited and the OWNER
    learns of it — not only the run-as user, who may be an admin-nominated
    bystander. Without the audit row, the trail showed a grant with no
    matching revocation and SC-012 could not be evidenced."""

    _Session = TestOwnerChangeConfirmsTheMismatch._Session
    _exposure = staticmethod(TestOwnerChangeConfirmsTheMismatch._exposure)

    def _revoke(self, *, owner_id, run_as_id):
        agent = Agent(name="a", user_id=owner_id, is_company_managed=True)
        agent.id = uuid.uuid4()
        exposure = self._exposure(run_as_id, agent.id)
        session = self._Session([exposure])
        hooks._revoke_live_exposures(session, agent, REVOKED_TIER_CHANGE)
        return session

    def test_an_audit_entry_names_the_triggering_change(self):
        from app.models.audit_log import AuditLog

        session = self._revoke(owner_id=uuid.uuid4(), run_as_id=uuid.uuid4())
        audits = [o for o in session.new if isinstance(o, AuditLog)]
        assert len(audits) == 1
        assert audits[0].details["reason"] == REVOKED_TIER_CHANGE
        # No human performed this; a human's revocation is audited by the API.
        assert audits[0].user_email == "system:a2a-auto-revocation"

    def test_owner_and_run_as_user_are_both_notified(self):
        from app.models.activity import Activity

        owner, run_as = uuid.uuid4(), uuid.uuid4()
        session = self._revoke(owner_id=owner, run_as_id=run_as)
        recipients = {
            o.user_id for o in session.new if isinstance(o, Activity)
        }
        assert recipients == {owner, run_as}

    def test_owner_who_is_the_run_as_user_is_notified_once(self):
        from app.models.activity import Activity

        both = uuid.uuid4()
        session = self._revoke(owner_id=both, run_as_id=both)
        activities = [o for o in session.new if isinstance(o, Activity)]
        assert len(activities) == 1

    def test_ownerless_legacy_agent_still_notifies_the_run_as_user(self):
        from app.models.activity import Activity

        run_as = uuid.uuid4()
        session = self._revoke(owner_id=None, run_as_id=run_as)
        recipients = {
            o.user_id for o in session.new if isinstance(o, Activity)
        }
        assert recipients == {run_as}


class TestRevocationAndRebindingInOneFlush:
    """One update that invalidates the boundary AND adds a binding.

    The two are not alternatives. Revoking and then stopping left the agent
    binding an A2A integration with nothing live — ``binds_a2a() == True`` and
    no exposure — which is the exact divergence the migration's converge step
    exists to clean up, reintroduced at runtime. It self-healed only on some
    later bindings edit, so any number of sharing-only edits left it stuck.

    Both must run, and the binding state has the last word: the old exposure is
    retired for good (its card URL never resolves again) and a fresh one is
    minted under the NEW boundary, which is what the operator asked for in that
    same request.
    """

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def __iter__(self):
            return iter(self._rows)

        def all(self):
            return self._rows

    class _Session(_TxnSurface):
        no_autoflush = nullcontext()

        def __init__(self, exposures, inbound_ids=()):
            self.dirty = []
            self.new = []
            self.added = []
            self._exposures = exposures
            self._inbound_ids = list(inbound_ids)

        def execute(self, _stmt):
            # Two questions reach this double now, and they must not share an
            # answer. The direction lookup is identified by the table it
            # selects from: answering it with exposures would leave the agent
            # reading as neither direction, and the exposure assertions below
            # would fail for a reason that has nothing to do with what this
            # test is about.
            if "a2a_integration_settings" in str(_stmt):
                return TestRevocationAndRebindingInOneFlush._Result(
                    [(i, "inbound") for i in self._inbound_ids]
                )
            # Every exposure lookup answers from the same list — including the
            # one the sync half makes AFTER the revocation, which is the
            # point: the database still reads ``revoked_at IS NULL`` for a row
            # revoked moments ago in this flush, and only the in-memory value
            # says otherwise.
            return TestRevocationAndRebindingInOneFlush._Result(self._exposures)

        def get(self, _model: object, _ident: object) -> None:
            # The run-as account lookup of a disable: not loaded, so active.
            return None

        def add(self, obj):
            self.added.append(obj)

    @staticmethod
    def _persistent(**kw):
        from sqlalchemy import inspect as sa_inspect

        agent = Agent(name="a", **kw)
        agent.id = uuid.uuid4()
        sa_inspect(agent)._commit_all({})
        return agent

    def _live(self, agent, run_as):
        return A2AExposure(
            agent_id=agent.id, card_token="old-token", run_as_user_id=run_as
        )

    def test_sharing_change_plus_new_binding_leaves_a_live_exposure(self):
        owner = uuid.uuid4()
        agent = self._persistent(
            user_id=owner, enabled=True, shared_group_ids=["g1"]
        )
        agent.integration_bindings = {}
        old = self._live(agent, owner)
        # The integration bound below faces inward, so exposure follows it.
        integration_id = uuid.uuid4()
        session = self._Session([old], inbound_ids=[integration_id])

        # One flush: sharing moves AND a binding is added.
        agent.shared_group_ids = ["g1", "g2"]
        agent.integration_bindings = {"oauth_a2a": [str(integration_id)]}
        session.dirty = [agent]
        hooks._before_flush(session, None, None)

        assert old.revoked_at is not None
        assert old.revoked_reason == REVOKED_SHARING_CHANGE

        minted = [o for o in session.added if isinstance(o, A2AExposure)]
        assert len(minted) == 1, "the new binding must produce a live exposure"
        assert minted[0].card_token != old.card_token, "the retired URL stays dead"
        assert minted[0].run_as_user_id == owner

    def test_a_disabled_agent_still_ends_with_no_exposure(self):
        """The one revocation that must NOT be followed by a re-mint: an agent
        nobody can reach has nothing to expose, whatever it binds."""
        owner = uuid.uuid4()
        agent = self._persistent(user_id=owner, enabled=True)
        agent.integration_bindings = {}
        old = self._live(agent, owner)
        session = self._Session([old])

        agent.enabled = False
        agent.integration_bindings = {"oauth_a2a": [str(uuid.uuid4())]}
        session.dirty = [agent]
        hooks._before_flush(session, None, None)

        assert old.revoked_reason == REVOKED_AGENT_DISABLED
        assert [o for o in session.added if isinstance(o, A2AExposure)] == []
