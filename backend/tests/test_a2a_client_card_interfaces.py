"""What a card declares, and which of its interfaces gets chosen.

A2A defines three transport bindings — JSON-RPC, gRPC and HTTP+JSON — and a
card declares a SET of interfaces, each with its own binding and, in v1.0, its
own protocol version. A client that reads only a top-level ``url`` handles
exactly one card shape: a v0.3 JSON-RPC one. Vertex AI Agent Engine publishes
no top-level ``url``, HTTP+JSON only, and a patch-level version string.

The wire that talks to the chosen interface is
``test_a2a_client_rest_transport``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.a2a.client import verification
from app.a2a.client.sdk import project_card
from app.a2a.client.verification import Refusal, Selection, select_interface
from app.a2a.protocol import TRANSPORT_JSONRPC, TRANSPORT_REST
from app.models.a2a import (
    UNUSABLE_NO_COMMON_TRANSPORT,
    UNUSABLE_ORIGIN_MISMATCH,
    USABILITY_USABLE,
)
from tests.a2a_client_stubs import StubPartner

ORIGIN = "https://partner.example"
A2A_BASE = f"{ORIGIN}/agents/triage/a2a"
CARD_URL = f"{A2A_BASE}/v1/card"

#: What a v1.0 REST partner publishes: one interface, its binding and its
#: version on the interface rather than on the card.
REST_CARD = {
    "name": "Vertex Triage Agent",
    "description": "Triages an alert.",
    "supportedInterfaces": [
        {"url": A2A_BASE, "protocolBinding": "HTTP+JSON", "protocolVersion": "0.3"}
    ],
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "capabilities": {"streaming": False, "pushNotifications": False},
}


def _card(**overrides):
    out = dict(REST_CARD)
    out.update(overrides)
    return out


class TestChoosingAnInterface:
    def _select(self, card, origin=ORIGIN):
        return select_interface(project_card(card, signature_verified=False), origin)

    def test_a_v1_card_declares_its_endpoint_on_an_interface(self):
        # The v1.0 shape: no top-level ``url``, everything on
        # ``supportedInterfaces``.
        chosen = self._select(REST_CARD)
        assert isinstance(chosen, Selection)
        assert chosen == Selection(url=A2A_BASE, transport=TRANSPORT_REST, version="0.3")

    def test_json_rpc_wins_when_a_partner_offers_both(self):
        # Preference is OURS, not the card's: this partner says it would
        # rather serve REST, and JSON-RPC is still chosen because it is the
        # binding this platform also serves and therefore exercises most.
        chosen = self._select(
            {
                "url": f"{ORIGIN}/rest",
                "preferredTransport": "HTTP+JSON",
                "protocolVersions": ["0.3"],
                "additionalInterfaces": [
                    {"url": f"{ORIGIN}/rpc", "transport": "JSONRPC"}
                ],
            }
        )
        assert chosen == Selection(
            url=f"{ORIGIN}/rpc", transport=TRANSPORT_JSONRPC, version="0.3"
        )

    def test_a_binding_we_do_not_speak_gets_its_own_reason(self):
        chosen = self._select(
            {
                "supportedInterfaces": [
                    {"url": f"{ORIGIN}/grpc", "protocolBinding": "GRPC",
                     "protocolVersion": "1.0"}
                ]
            }
        )
        assert isinstance(chosen, Refusal)
        # Not ``no_common_protocol_version``: 1.0 IS a version we speak, and
        # an operator sent looking for a version setting would find nothing
        # that helped.
        assert chosen.reason == UNUSABLE_NO_COMMON_TRANSPORT
        assert "GRPC" in chosen.detail

    def test_the_transport_spelling_is_read_tolerantly(self):
        for spelling in ("HTTP+JSON", "http+json", "REST", "HTTP_JSON"):
            chosen = self._select(
                _card(
                    supportedInterfaces=[
                        {"url": A2A_BASE, "protocolBinding": spelling,
                         "protocolVersion": "0.3"}
                    ]
                )
            )
            assert chosen == Selection(
                url=A2A_BASE, transport=TRANSPORT_REST, version="0.3"
            ), spelling

    def test_a_patch_level_version_is_a_major_minor_match(self):
        # What both vendors actually publish: the reference SDK defaults
        # ``protocolVersion`` to a patch-level string, so this is the common
        # case rather than an edge one.
        chosen = self._select(
            _card(
                supportedInterfaces=[
                    {"url": A2A_BASE, "protocolBinding": "HTTP+JSON",
                     "protocolVersion": "0.3.0"}
                ]
            )
        )
        assert chosen == Selection(url=A2A_BASE, transport=TRANSPORT_REST, version="0.3")

    def test_origin_is_checked_before_the_binding(self):
        # A partner offering a REST interface we like, somewhere we never
        # verified. The refusal must name the origin, not the transport —
        # otherwise a partner could earn a gentler diagnosis by also offering
        # an attractive binding.
        chosen = self._select(
            {
                "supportedInterfaces": [
                    {"url": "https://elsewhere.example/a2a",
                     "protocolBinding": "HTTP+JSON", "protocolVersion": "0.3"}
                ]
            }
        )
        assert isinstance(chosen, Refusal)
        assert chosen.reason == UNUSABLE_ORIGIN_MISMATCH

    def test_an_interface_version_can_differ_from_the_card(self):
        # v1.0 declares the version per interface, so two endpoints on one
        # card may legitimately disagree — and the one we can speak wins.
        chosen = self._select(
            {
                "protocolVersions": ["9.9"],
                "supportedInterfaces": [
                    {"url": f"{ORIGIN}/new", "protocolBinding": "HTTP+JSON",
                     "protocolVersion": "9.9"},
                    {"url": A2A_BASE, "protocolBinding": "HTTP+JSON",
                     "protocolVersion": "0.3"},
                ],
            }
        )
        assert chosen == Selection(url=A2A_BASE, transport=TRANSPORT_REST, version="0.3")


class TestVerifyingARestCard:
    @pytest.mark.asyncio
    async def test_the_verdict_carries_the_chosen_binding(self):
        partner = StubPartner(card=REST_CARD)
        async with partner.client() as client:
            verdict = await verification.verify_card(
                card_url=CARD_URL, http_client=client, resolve_dns=False
            )
        assert verdict.usability == USABILITY_USABLE
        assert verdict.transport == TRANSPORT_REST
        assert verdict.invocation_url == A2A_BASE
        assert verdict.agreed_version == "0.3"

    @pytest.mark.asyncio
    async def test_applying_it_records_the_binding_beside_the_endpoint(self):
        partner = StubPartner(card=REST_CARD)
        async with partner.client() as client:
            verdict = await verification.verify_card(
                card_url=CARD_URL, http_client=client, resolve_dns=False
            )
        row = SimpleNamespace(
            usability=None, unusable_reason=None, last_verified_at=None,
            last_verification_error=None, card_url=None, invocation_url=None,
            transport=None, pinned_origin=None, agreed_protocol_version=None,
            signature_state=None, card_fingerprint=None,
            accepted_card_fingerprint=None, partner_discovery_path=None,
            declared_name=None, declared_description=None, declared_skills=None,
            declared_input_types=None, declared_output_types=None,
        )
        verification.apply_verdict(row, verdict, card_url=CARD_URL)
        assert row.transport == TRANSPORT_REST
        assert row.invocation_url == A2A_BASE
        # The endpoint is NOT the card URL: one is a discovery document, the
        # other is where work is sent.
        assert row.card_url == CARD_URL


