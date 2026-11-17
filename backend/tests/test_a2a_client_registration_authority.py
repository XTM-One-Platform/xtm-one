"""Who may manage a partner registration, and what the surface refuses.

FR-002 / FR-003. Authority is the platform's EXISTING integration authority —
owner, administrator, or a configuration-tier group member — and no new
permission concept is introduced. These tests pin that the outbound surface
reuses it rather than inventing a parallel check, which is the thing a new
management endpoint most easily gets wrong.
"""

from __future__ import annotations

import inspect

from app.api.v1 import a2a_client


class TestReusedAuthority:
    def test_the_read_surface_uses_the_platform_helpers(self):
        # Not a new permission model: ``get_user_integration`` enforces
        # visibility and ``can_manage`` enforces the configuration tier —
        # the same pair every other integration surface uses.
        src = inspect.getsource(a2a_client._load)
        assert "get_user_integration" in src
        assert "can_manage" in src

    def test_every_route_loads_through_that_check(self):
        # A route reaching the settings row directly would bypass authority
        # while looking correct. Asserted structurally because the mistake is
        # an omission, and an omission has no test of its own to fail.
        for name in (
            "get_verification_state",
            "list_delegations",
            "verify_now",
            "accept_card_change",
        ):
            src = inspect.getsource(getattr(a2a_client, name))
            assert "_load(db, user, integration_id" in src, name

    def test_only_the_direction_lookup_is_relaxed_to_visibility(self):
        # A viewer already sees the integration's status, and refusing them
        # its verification state with a 403 broke the detail page for every
        # non-admin on a company-managed partner. That is the ONE read the
        # page-level lookup makes; everything else keeps authority.
        assert "manage=False" in inspect.getsource(a2a_client.get_verification_state)
        for name in ("list_delegations", "verify_now", "accept_card_change"):
            assert "manage=False" not in inspect.getsource(getattr(a2a_client, name)), name
        assert "if manage and not can_manage" in inspect.getsource(a2a_client._load)

    def test_the_delegation_history_is_not_readable_by_a_mere_viewer(self):
        # It names the agents that delegated, and the join resolving that
        # name applies no agent visibility — agents are scoped in their own
        # right (own / user-shared / group-shared / company-managed). On a
        # company-managed registration, visibility alone would report a
        # private agent's name and timings to every user.
        src = inspect.getsource(a2a_client.list_delegations)
        assert "Agent.name" in src, "the join this guards is gone; re-check the gate"
        assert "_load(db, user, integration_id)" in src

    def test_every_route_follows_agentic_chat(self):
        # A2A is offered exactly while agentic chat is, and the gate is the
        # platform's own ``require_chat_enabled`` declared once on the router
        # rather than a check of this module's own — the inbound admin router
        # does the same.
        from app.api.deps import require_chat_enabled

        assert any(
            d.dependency is require_chat_enabled for d in a2a_client.router.dependencies
        )


class TestSurfaceScope:
    def test_an_inbound_registration_has_no_verification_state(self):
        # This surface reports on the OUTBOUND direction. Answering for an
        # inbound one would make it a second place to read exposure state,
        # disagreeing with the first the moment either changed.
        src = inspect.getsource(a2a_client._load)
        assert "DIRECTION_OUTBOUND" in src
        assert "404" in src

    def test_a_non_a2a_integration_is_not_found(self):
        assert "ALL_A2A_PROVIDERS" in inspect.getsource(a2a_client._load)

    def test_there_is_no_expose_revoke_or_direction_route(self):
        # Two paths to one state disagree the moment either is used, so
        # there is no expose/revoke pair beside the binding hook. Direction
        # is set through the ordinary integration update, for that reason.
        paths = {r.path for r in a2a_client.router.routes}
        assert not any(
            word in p for p in paths for word in ("expose", "revoke", "direction")
        )

    def test_the_surface_stays_small(self):
        # Read the state, re-verify, accept a change, list what was sent.
        # Named rather than counted: a bare count tells whoever adds the fifth
        # route to bump a number, where naming them makes them ask whether the
        # new one belongs on a surface this deliberately narrow.
        paths = {r.path.rsplit("/", 1)[-1] for r in a2a_client.router.routes}
        assert paths == {"{integration_id}", "verify", "accept-card", "delegations"}


class TestRefusals:
    def test_an_invalid_signature_cannot_be_accepted(self):
        # FR-015 allows no operator override, and an "accept" that cleared a
        # bad signature would be that override wearing a different name.
        src = inspect.getsource(a2a_client.accept_card_change)
        assert "signature_invalid" in src

    def test_acceptance_is_pinned_to_the_observed_fingerprint(self):
        # A trust decision applies to ONE card, not to whichever arrives
        # next: accepting a stale fingerprint would accept a card the
        # operator never read.
        src = inspect.getsource(a2a_client.accept_card_change)
        assert "body.fingerprint != row.card_fingerprint" in src
