"""XTM One tasking XTM One — the target testable without a vendor account.

FR-045 / SC-001. The third supported target is "any partner accepting OAuth
client-credentials", and this platform's own inbound surface issues exactly
that credential shape. That makes self-federation the only one of the three
exercisable end to end here, and the only one that runs both halves of the
A2A work in a single test.

What is asserted is the pairing, not a live socket: that the inbound surface
serves what the outbound half expects, and that the two directions cannot be
configured into the same integration by accident. A genuine loopback needs a
running server and belongs in the quickstart, which says so.
"""

from __future__ import annotations

import inspect

import pytest

from app.a2a.providers import (
    ALL_A2A_PROVIDERS,
    ENTRA_A2A_PROVIDER,
    GOOGLE_A2A_PROVIDER,
    OAUTH_A2A_PROVIDER,
)
from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND


class TestTheThreeTargets:
    def test_all_three_mechanisms_exist(self):
        assert {
            OAUTH_A2A_PROVIDER,
            ENTRA_A2A_PROVIDER,
            GOOGLE_A2A_PROVIDER,
        } == ALL_A2A_PROVIDERS

    def test_each_declares_the_directions_it_supports(self):
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        assert PROVIDER_CATALOG[OAUTH_A2A_PROVIDER]["a2a_directions"] == [
            "inbound",
            "outbound",
        ]
        assert PROVIDER_CATALOG[ENTRA_A2A_PROVIDER]["a2a_directions"] == [
            "inbound",
            "outbound",
        ]
        # Both directions. Inbound Google is not the same state as OAuth A2A
        # inbound: OAuth A2A needs this platform to mint a secret and the
        # partner to hold it, while the inbound Google mode stores no secret
        # on either side and lets Google refresh the caller's own credential
        # forever. The same distinction puts Foundry A2A beside OAuth A2A.
        assert PROVIDER_CATALOG[GOOGLE_A2A_PROVIDER]["a2a_directions"] == [
            "inbound",
            "outbound",
        ]

    def test_identical_delegation_behaviour_across_all_three(self):
        # FR-045: no per-target difference visible to a calling agent. One
        # shared tool builder and one delegation module, so the claim is
        # structural rather than something three code paths must agree on.
        from app.a2a.client import tool
        from app.services.integration_tools import _build_a2a_delegation_tool

        assert "build_delegation_tool" in inspect.getsource(_build_a2a_delegation_tool)
        src = inspect.getsource(tool.build_delegation_tool)
        for mechanism in ("oauth", "entra", "google", "vertex", "foundry"):
            assert mechanism not in src.lower()


class TestTheSelfFederationPairing:
    def test_our_inbound_surface_issues_what_our_outbound_half_consumes(self):
        # The whole basis of the third target. Inbound OAuth A2A MINTS a
        # client id and secret that a partner exchanges for a bearer;
        # outbound OAuth A2A presents exactly that pair at a token endpoint.
        from app.a2a.client.credentials import _mint_oauth_client_credentials

        minted = inspect.getsource(_mint_oauth_client_credentials)
        assert "grant_type" in minted
        assert "client_credentials" in minted

        from app.integrations.providers import oauth_a2a

        assert "client-credentials" in (oauth_a2a.__doc__ or "").lower() or (
            "client id" in (oauth_a2a.__doc__ or "").lower()
        )

    def test_our_own_card_is_served_where_the_outbound_half_can_read_it(self):
        # Our inbound card endpoint is anonymous, which is why FR-014 requires
        # credentials only "where the target requires them". Registering our
        # own agent as a partner must not fail for want of a credential the
        # endpoint ignores.
        from app.a2a.client.registration import _CARD_NEEDS_CREDENTIALS

        assert OAUTH_A2A_PROVIDER not in _CARD_NEEDS_CREDENTIALS
        assert GOOGLE_A2A_PROVIDER in _CARD_NEEDS_CREDENTIALS
        assert ENTRA_A2A_PROVIDER in _CARD_NEEDS_CREDENTIALS


class TestOneIntegrationCannotServeBoth:
    @pytest.mark.parametrize("direction", [DIRECTION_INBOUND, DIRECTION_OUTBOUND])
    def test_a_direction_is_exactly_one_value(self, direction):
        from app.models.a2a import A2AIntegrationSettings

        row = A2AIntegrationSettings(integration_id=None, direction=direction)
        # No state in which both are true: self-federation needs TWO
        # integrations, one facing each way, which is the point of the rule.
        assert row.is_inbound != row.is_outbound

    @pytest.mark.asyncio
    async def test_both_is_refused_at_creation(self):
        from app.a2a.providers import create_settings

        with pytest.raises(ValueError, match="inbound or outbound"):
            await create_settings(object(), None, "both")


class TestTheQuickstartOwnsTheLiveLoop:
    def test_the_runnable_loopback_is_documented(self):
        # This file asserts the pairing; a genuine end-to-end loop needs a
        # running server and lives in the quickstart. Pinned so the two do
        # not both assume the other covers it.
        import pathlib

        quickstart = (
            pathlib.Path(__file__).resolve().parents[2]
            / "specs/009-a2a-client-outbound/quickstart.md"
        )
        text = quickstart.read_text(encoding="utf-8")
        assert "own inbound surface" in text
        assert "vendor account" in text


class TestTheDirectionReachesTheUi:
    """An outbound integration must be creatable through the product.

    Dropping the direction fields from the response model makes the creation
    form unable to offer a direction — the whole outbound feature unreachable
    while every backend test passes. A zero-call-site sweep does not catch
    this: it looks for code nothing calls, and what goes missing here is a
    field crossing the API boundary. These tests watch that boundary.
    """

    def test_the_response_model_carries_the_direction_fields(self):
        from app.api.v1.schemas.integration import ProviderCatalogItem

        fields = set(ProviderCatalogItem.model_fields)
        assert "a2a_directions" in fields, (
            "without this the catalog endpoint drops it and the creation form "
            "cannot offer a direction"
        )
        assert "variant_selector" in fields and "variants" in fields

    @pytest.mark.parametrize(
        "provider", [OAUTH_A2A_PROVIDER, ENTRA_A2A_PROVIDER, GOOGLE_A2A_PROVIDER]
    )
    def test_the_endpoint_actually_passes_the_direction_through(self, provider):
        """Read the ENDPOINT's constructor, not a hand-built model.

        A test that constructs the object differently from the code under
        test asserts its own construction — coverage of a path it never
        touches.
        """
        import inspect

        from app.api.v1.integrations import list_provider_catalog

        constructor = inspect.getsource(list_provider_catalog)
        assert "a2a_directions=val.get(" in constructor, (
            "the catalog endpoint lists its fields explicitly — a field "
            "missing from that list never reaches the UI"
        )
        assert "variant_selector=val.get(" in constructor
        assert "variants=variants" in constructor

    @pytest.mark.parametrize(
        "provider", [OAUTH_A2A_PROVIDER, ENTRA_A2A_PROVIDER, GOOGLE_A2A_PROVIDER]
    )
    def test_each_mechanism_declares_an_outbound_direction(self, provider):
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        entry = PROVIDER_CATALOG[provider]
        assert "outbound" in (entry.get("a2a_directions") or []), provider
        # And the outbound direction must ask for something, or an operator
        # would create a registration with no card location.
        assert ((entry.get("variants") or {}).get("outbound") or {}).get("extra_fields")

    def test_a_non_a2a_provider_carries_neither(self):
        # The fields are A2A-only; leaking them onto every provider would put
        # a direction control on integrations that have no direction.
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        entry = PROVIDER_CATALOG["github"]
        assert entry.get("a2a_directions") is None
        assert entry.get("variants") is None

    def test_the_create_request_accepts_a_direction(self):
        from app.api.v1.schemas.integration import CreateIntegrationRequest

        assert "a2a_direction" in CreateIntegrationRequest.model_fields

    def test_every_outbound_mechanism_asks_for_a_card_location(self):
        # Without it there is nothing to verify, and the registration would be
        # created unusable with no way for the operator to see why.
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        for provider in ALL_A2A_PROVIDERS:
            outbound = (
                (PROVIDER_CATALOG[provider].get("variants") or {}).get("outbound") or {}
            ).get("extra_fields") or {}
            assert "card_urls" in outbound, provider


class TestCatalogVisibilityFollowsChat:
    """The catalog does not gate A2A at all — it lists it like anything else.

    Hiding the tiles made A2A the one mechanism an operator could not
    discover. What refuses creation is unchanged, and pinned in
    ``test_a2a_follows_chat``; the client tells A2A apart by
    ``a2a_directions``, which only A2A carries.
    """

    def test_the_catalog_has_no_a2a_special_case(self):
        import inspect

        from app.api.v1.integrations import list_provider_catalog

        src = inspect.getsource(list_provider_catalog)
        # No filtering, no second copy of the tier, no per-provider flag.
        for gating in ("hidden_providers", "is_chat_enabled", "ALL_A2A_PROVIDERS"):
            assert gating not in src, gating

    def test_a2a_directions_is_what_marks_them(self):
        """The client's only signal, so it must be on every mechanism and on
        nothing else — otherwise a tile asks the wrong gate."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        carrying = {
            key for key, val in PROVIDER_CATALOG.items() if val.get("a2a_directions")
        }
        assert carrying == {
            OAUTH_A2A_PROVIDER,
            ENTRA_A2A_PROVIDER,
            GOOGLE_A2A_PROVIDER,
        }, carrying
