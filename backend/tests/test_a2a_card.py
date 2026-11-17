"""Agent Card projection and version shaping.

The card is what both target platforms read to configure themselves, so a
fact stated here is applied automatically while a fact stated only in prose
depends on a human reading it. That is why capabilities, output modes and the
exposure's tool narrowing all have to be *in* the card.
"""

import uuid

import pytest

from app.a2a.card import build_agent_card


class _Agent:
    def __init__(self, *, output_format="markdown", builtin=None):
        self.id = uuid.uuid4()
        self.name = "Threat Triage"
        self.description = "Triages inbound threat reports"
        self.output_format = output_format
        self.output_schema = None
        self.builtin_tool_ids = builtin or []
        self.custom_tool_ids = []
        self.mcp_server_ids = []
        self.knowledge_base_ids = []
        self.integration_bindings = {}


class _Exposure:
    def __init__(self):
        self.card_token = "tok-abc"
        self.run_as_user_id = uuid.uuid4()


def _card(agent=None, exposure=None, version="0.3"):
    return build_agent_card(
        agent or _Agent(),
        exposure or _Exposure(),
        base_url="https://xtm.example.com",
        version=version,
        app_version="1.2.3",
    )


class TestCapabilities:
    @pytest.mark.parametrize("version", ["1.0", "0.3"])
    def test_push_notifications_declared_supported(self, version):
        """A task can stay open while delegated work runs, which is the case
        the protocol defines push notifications for. Both versions spell the
        field identically, so neither needs its own branch."""
        assert _card(version=version)["capabilities"]["pushNotifications"] is True

    @pytest.mark.parametrize("version", ["1.0", "0.3"])
    def test_streaming_declared_unsupported(self, version):
        """A partner reads this before it decides how to call, and consuming an
        event stream is not built — declaring it would have partners open one
        against a surface that answers no events."""
        assert _card(version=version)["capabilities"]["streaming"] is False


class TestPartnerDiscoveryExtension:
    """The one non-standard thing this deployment serves, declared the way
    A2A says to declare non-standard things.

    A2A has no method that enumerates agents, so a partner exposing three
    publishes three cards and a client has no specified way to learn about the
    second and third. This deployment serves an endpoint for it — and
    ``AgentCapabilities.extensions`` is the spec's own way to advertise a
    capability it does not define, which is what turns a blind probe into a
    negotiated one.
    """

    @pytest.mark.parametrize("version", ["1.0", "0.3"])
    def test_declared_in_capabilities_under_both_versions(self, version):
        # ``extensions`` is spelled identically in v0.3 and v1.0, which is why
        # the projection needs no per-version branch for it — pinned here so a
        # future divergence is caught in the card rather than by a partner.
        from app.a2a.extensions import PARTNER_DISCOVERY_URI

        declared = _card(version=version)["capabilities"]["extensions"]
        assert [e["uri"] for e in declared] == [PARTNER_DISCOVERY_URI]

    @pytest.mark.parametrize("version", ["1.0", "0.3"])
    def test_it_is_never_required(self, version):
        """A client that ignores it loses a convenience, not interoperability.

        ``required: true`` would tell every partner it must implement a
        non-standard endpoint to talk to us at all — untrue, and the opposite
        of what declaring an optional capability is for.
        """
        declared = _card(version=version)["capabilities"]["extensions"][0]
        assert declared["required"] is False

    def test_it_survives_a_v03_partner_parsing_the_card(self):
        """Asserting on our own dict cannot catch a field a partner drops.

        Round-tripped through the SDK's own v0.3 model, like the security
        requirement above, so a mis-shaped extension entry fails here rather
        than being silently discarded by the partner that reads it.
        """
        from a2a.compat.v0_3 import types as v03

        from app.a2a.extensions import PARTNER_DISCOVERY_PATH, PARTNER_DISCOVERY_URI

        parsed = v03.AgentCard.model_validate(_card(version="0.3"))
        extension = parsed.capabilities.extensions[0]
        assert extension.uri == PARTNER_DISCOVERY_URI
        assert extension.required is False
        assert extension.params == {"path": PARTNER_DISCOVERY_PATH}
        assert extension.description

    def test_extensions_is_a_real_field_of_the_v1_card(self):
        """The v1.0 counterpart, checked against the SDK's descriptor — the
        same reason the security requirement is checked this way rather than
        round-tripped: the v1.0 model is protobuf and rejects unknown fields
        outright."""
        from a2a import types as v10

        names = {f.json_name for f in v10.AgentCapabilities.DESCRIPTOR.fields}
        assert "extensions" in names

    def test_the_uri_names_the_extension_not_the_deployment(self):
        """An extension URI is a NAME. Two deployments implementing this must
        spell it identically or neither recognises the other, so it must not
        be built from ``base_url``."""
        from app.a2a.extensions import PARTNER_DISCOVERY_URI

        def _uri(base):
            return build_agent_card(
                _Agent(),
                _Exposure(),
                base_url=base,
                version="0.3",
                app_version="1.2.3",
            )["capabilities"]["extensions"][0]["uri"]

        assert _uri("https://one.example") == _uri("https://two.example")
        assert "one.example" not in PARTNER_DISCOVERY_URI

    def test_the_advertised_path_is_the_one_we_actually_serve(self):
        """The drift this declaration would otherwise invite.

        A card that advertises a path is a promise; the promise is only worth
        making if moving the endpoint breaks the build rather than the
        partner. Read off the DECORATED route — prefix included — so renaming
        the route or remounting the router fails here.
        """
        from app.a2a.extensions import PARTNER_DISCOVERY_PATH
        from app.api.v1.a2a_server import list_partner_agents, router

        served = [
            route.path
            for route in router.routes
            if getattr(route, "endpoint", None) is list_partner_agents
        ]
        assert served == [PARTNER_DISCOVERY_PATH]
        advertised = _card()["capabilities"]["extensions"][0]["params"]["path"]
        assert advertised == PARTNER_DISCOVERY_PATH

    def test_the_client_calls_the_path_the_card_advertises(self):
        """The third reader. Our outbound half calls this endpoint on a
        partner, so a hard-coded copy there would be a fourth spelling of one
        path — and the one nobody would think to update.

        The constant is used where the destination is DECIDED
        (``_discovery_path``, which prefers what the partner declared and
        falls back to it); the caller just uses what it returns. Both are read
        so neither can grow a literal."""
        import inspect

        from app.a2a.client import registration
        from app.a2a.extensions import PARTNER_DISCOVERY_PATH

        decides = inspect.getsource(registration._discovery_path)
        assert "PARTNER_DISCOVERY_PATH" in decides
        for func in (registration._discovery_path, registration.discover_partner_agents):
            assert PARTNER_DISCOVERY_PATH not in inspect.getsource(func)


class TestContentModes:
    def test_output_modes_declared_separately_from_input(self):
        card = _card()
        assert card["defaultInputModes"] != card["defaultOutputModes"]

    def test_structured_output_only_for_a_configured_agent(self):
        plain = _card(_Agent(output_format="markdown"))
        assert "application/json" not in plain["defaultOutputModes"]
        json_agent = _card(_Agent(output_format="json"))
        assert "application/json" in json_agent["defaultOutputModes"]

    def test_structured_input_is_always_accepted(self):
        # A partner may send structured data to any agent; only *producing*
        # it depends on configuration.
        assert "application/json" in _card()["defaultInputModes"]


class TestSkills:
    """The agent's own built-ins, unnarrowed: both target platforms configure
    themselves from this document, and external work is bounded by the run-as
    identity rather than by a tool list."""

    def test_skills_are_the_agents_own_builtins(self):
        agent = _Agent(builtin=["web_fetch", "browser", "generate_file"])
        ids = [s["id"] for s in _card(agent)["skills"]]
        assert ids == ["web_fetch", "browser", "generate_file"]

    def test_management_capable_tools_are_advertised_like_any_other(self):
        """The run can use ``agent_builder``, so the card says so."""
        agent = _Agent(builtin=["agent_builder", "web_fetch"])
        ids = [s["id"] for s in _card(agent)["skills"]]
        assert ids == ["agent_builder", "web_fetch"]

    def test_toolless_agent_still_advertises_something(self):
        # A partner cannot register an agent whose card describes nothing.
        skills = _card(_Agent(builtin=[]))["skills"]
        assert len(skills) == 1
        assert skills[0]["id"] == "converse"


class TestVersionShaping:
    def test_legacy_uses_url_preferred_transport_and_additional_interfaces(self):
        card = _card(version="0.3")
        # ``preferredTransport`` is REQUIRED in v0.3 — Foundry's parser
        # rejects the whole card when it is absent.
        assert card["preferredTransport"] == "JSONRPC"
        assert card["additionalInterfaces"][0]["transport"] == "JSONRPC"
        assert "supportedInterfaces" not in card

    def test_v1_uses_supported_interfaces_and_protocol_binding(self):
        card = _card(version="1.0")
        assert "supportedInterfaces" in card
        assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"

    def test_the_undeclared_card_carries_both_spellings(self):
        # Both platforms fetch without a header and require different names,
        # so one document has to carry both.
        card = _card(version="1.0")
        assert card["url"] == card["supportedInterfaces"][0]["url"]
        assert card["preferredTransport"] == "JSONRPC"
        assert card["additionalInterfaces"] == [
            {"url": card["url"], "transport": "JSONRPC"}
        ]
        assert card["security"] == card["securityRequirements"]

    def test_a_declared_v03_fetch_gets_v03_names_alone(self):
        # Declared, so there is nothing to satisfy but that version.
        card = _card(version="0.3")
        assert "supportedInterfaces" not in card
        assert "securityRequirements" not in card

    def test_endpoint_is_built_from_the_card_token(self):
        card = _card(version="1.0")
        assert card["supportedInterfaces"][0]["url"].endswith("/a2a/tok-abc/jsonrpc")


class TestSecurityAndForwardCompatibility:
    @pytest.mark.parametrize("version", ["1.0", "0.3"])
    def test_declares_oauth_client_credentials(self, version):
        flows = _card(version=version)["securitySchemes"][
            "oauth2ClientCredentials"
        ]["flows"]
        assert "clientCredentials" in flows

    def test_v03_spells_the_requirement_list_security(self):
        """Each version carries its own name for the list, and only that one.

        A reader handed the other version's name does not see an unknown
        field - it sees NO security requirement, i.e. it is told the agent
        needs no credential, and registers the partner unauthenticated.
        """
        card = _card(version="0.3")
        assert card["security"] == [{"oauth2ClientCredentials": []}]
        assert "securityRequirements" not in card

    def test_v1_spells_it_security_requirements(self):
        card = _card(version="1.0")
        assert card["securityRequirements"] == [{"oauth2ClientCredentials": []}]

    def test_v03_requirement_survives_a_partner_parsing_the_card(self):
        """Asserting on our own key name cannot catch a wrong key name.

        This round-trips the v0.3 card through the SDK's own model, so a field
        the partner's parser silently drops fails here. It is the regression
        test for the bug this replaced: ``securityRequirements`` parsed as
        ``security=None`` and vanished on round-trip.
        """
        from a2a.compat.v0_3 import types as v03

        parsed = v03.AgentCard.model_validate(_card(version="0.3"))
        assert parsed.security == [{"oauth2ClientCredentials": []}]
        assert "security" in parsed.model_dump(by_alias=True, exclude_none=True)

    def test_v1_requirement_key_is_a_real_field_of_the_v1_card(self):
        """The v1.0 counterpart, checked against the SDK's descriptor.

        Not a whole-card round-trip: the v1.0 model is protobuf and rejects
        unknown fields outright, which would make this assert strictness rather
        than the key name it is here to pin.
        """
        from a2a import types as v10

        names = {f.json_name for f in v10.AgentCard.DESCRIPTOR.fields}
        assert "securityRequirements" in names
        assert "security" not in names
        assert "securityRequirements" in _card(version="1.0")

    def test_no_signature_this_phase(self):
        assert "signature" not in _card()
        assert "signatures" not in _card()

    def test_no_tenant_field(self):
        """The platform has no tenant entity; mapping the protocol's opaque
        routing hint onto is_company_managed would invent a divergent second
        tenancy concept."""
        assert "tenant" not in _card()

    def test_card_is_an_explicit_projection_not_a_model_dump(self):
        """FR-018: adding a signature later must change no existing field.
        A projection containing only known keys is what makes that true - and
        is also what stops an internal field leaking into a public document."""
        allowed = {
            "name", "description", "version", "protocolVersion", "capabilities",
            "defaultInputModes", "defaultOutputModes", "skills",
            "securitySchemes", "securityRequirements", "security",
            "additionalInterfaces", "preferredTransport",
            "supportedInterfaces", "url",
        }
        for version in ("1.0", "0.3"):
            assert set(_card(version=version)) <= allowed
        # Nothing from the internal model leaked through.
        assert "output_schema" not in _card()
        assert "builtin_tool_ids" not in _card()
