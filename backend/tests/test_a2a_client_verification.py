"""Fetching and verifying a partner's card — the defective-card matrix.

FR-011–FR-016, FR-051 / SC-003. This is where benchmark finding **F7** is
answered: no card signature-verification concept existed anywhere on this
platform before, and a client's verification must be fail-closed.

The matrix matters more than any single case. Each defect must produce its
OWN reason, because "unusable" without a reason sends an operator hunting,
and because the circuit breaker treats these differently: a bad signature is
durable and a timeout is not.
"""

from __future__ import annotations

import httpx
import pytest

from app.a2a.client import verification
from app.models.a2a import (
    SIGNATURE_UNSIGNED,
    SIGNATURE_VERIFIED,
    USABILITY_UNUSABLE,
    USABILITY_USABLE,
)
from tests.a2a_client_stubs import (
    GOOD_CARD,
    StubPartner,
    card_with,
    card_without,
    legacy_only_partner,
    no_common_version_partner,
    off_origin_partner,
    signed_card,
)

CARD_URL = "https://partner.example/agents/triage/card.json"


async def _verify(partner: StubPartner, *, card_url: str = CARD_URL, **kw):
    async with partner.client() as client:
        # ``resolve_dns=False``: the stub answers over a mock transport, so
        # there is no real host to resolve. The literal-address checks are
        # NOT skipped by this — they run either way — and the resolution path
        # has its own coverage in ``test_a2a_client_destinations.py``.
        return await verification.verify_card(
            card_url=card_url, http_client=client, resolve_dns=False, **kw
        )


class TestGoodCard:
    @pytest.mark.asyncio
    async def test_a_good_card_is_usable_and_reports_what_it_declares(self):
        verdict = await _verify(StubPartner())
        assert verdict.usability == USABILITY_USABLE
        assert verdict.unusable_reason is None
        assert verdict.card.name == GOOD_CARD["name"]
        assert verdict.card.skills == ["triage"]
        assert verdict.card.input_types == ["text/plain"]
        assert verdict.pinned_origin == "https://partner.example"

    @pytest.mark.asyncio
    async def test_it_agrees_the_highest_common_protocol_version(self):
        verdict = await _verify(StubPartner())
        assert verdict.agreed_version == "1.0"

    @pytest.mark.asyncio
    async def test_a_legacy_only_partner_is_reachable_on_the_older_version(self):
        # FR-051: refusing it would refuse a partner for a reason no operator
        # can act on — a customer cannot make their vendor serve a newer one.
        verdict = await _verify(legacy_only_partner())
        assert verdict.usability == USABILITY_USABLE
        assert verdict.agreed_version == "0.3"


class TestDefectiveCards:
    @pytest.mark.asyncio
    async def test_unfetchable_card(self):
        verdict = await _verify(StubPartner(card_status=503))
        assert verdict.usability == USABILITY_UNUSABLE
        assert verdict.unusable_reason == "card_unfetchable"

    @pytest.mark.asyncio
    async def test_malformed_card(self):
        verdict = await _verify(StubPartner(card_malformed=True))
        assert verdict.unusable_reason == "card_malformed"

    @pytest.mark.asyncio
    async def test_an_empty_capabilities_map_serialised_as_a_list_is_tolerated(self):
        # An empty map, as more than one serializer emits one.
        verdict = await _verify(StubPartner(card=card_with(capabilities=[])))
        assert verdict.usability == USABILITY_USABLE

    @pytest.mark.asyncio
    async def test_a_shape_the_projection_cannot_read_is_malformed_not_an_exception(self):
        # ``verify_card`` is documented never to raise.
        verdict = await _verify(StubPartner(card=card_with(supportedInterfaces=5)))
        assert verdict.usability == USABILITY_UNUSABLE
        assert verdict.unusable_reason == "card_malformed"

    @pytest.mark.asyncio
    async def test_unconfirmable_transport_is_refused_not_downgraded(self):
        # FR-012. The card URL itself is refused before a request is made, so
        # no credential is ever sent over a connection nobody can vouch for.
        verdict = await _verify(StubPartner(), card_url="http://partner.example/c.json")
        assert verdict.unusable_reason == "transport_unverified"

    @pytest.mark.asyncio
    async def test_a_destination_the_guard_forbids(self):
        verdict = await _verify(
            StubPartner(), card_url="https://169.254.169.254/card.json"
        )
        assert verdict.unusable_reason == "destination_not_permitted"

    @pytest.mark.asyncio
    async def test_no_common_protocol_version(self):
        verdict = await _verify(no_common_version_partner())
        assert verdict.unusable_reason == "no_common_protocol_version"

    @pytest.mark.asyncio
    async def test_off_origin_invocation_endpoint(self):
        # FR-034: a card cannot move us to an origin we never verified.
        verdict = await _verify(off_origin_partner())
        assert verdict.unusable_reason == "origin_mismatch"


class TestSignatures:
    @pytest.mark.asyncio
    async def test_an_unsigned_card_is_usable_and_recorded_as_such(self):
        # FR-016. Neither vendor target signs today, so refusing unsigned
        # cards would ship a feature that reaches nothing. What is NOT
        # acceptable is treating it as equivalent to verified.
        verdict = await _verify(StubPartner())
        assert verdict.usability == USABILITY_USABLE
        assert verdict.signature_state == SIGNATURE_UNSIGNED

    @pytest.mark.asyncio
    async def test_a_valid_signature_is_recorded_as_verified(self):
        # Real JWS over the SDK's own canonicalisation, and a real JWKS the
        # partner serves: a stub verifier would prove only that a parameter is
        # honoured, not that we agree with a partner's signer byte for byte.
        card, jwks = signed_card()
        verdict = await _verify(StubPartner(card=card, jwks=jwks))
        assert verdict.signature_state == SIGNATURE_VERIFIED
        assert verdict.usability == USABILITY_USABLE

    @pytest.mark.asyncio
    async def test_an_invalid_signature_is_fatal_with_no_override(self):
        # FR-015: in every case, with no operator override. Tampered AFTER
        # signing, so the signature is well-formed and simply does not match.
        card, jwks = signed_card()
        card = {**card, "name": "Impostor"}
        verdict = await _verify(StubPartner(card=card, jwks=jwks))
        assert verdict.usability == USABILITY_UNUSABLE
        assert verdict.unusable_reason == "signature_invalid"
        # And no signature_state is recorded: an invalid signature is not a
        # state the registration operates in, it is a refusal.
        assert verdict.signature_state is None

    @pytest.mark.asyncio
    async def test_keys_are_not_fetched_off_the_pinned_origin(self):
        # ``jku`` is partner-authored, so it is a request-forgery surface and
        # pinned like the card itself (FR-034). Not followed means no key, so
        # the card cannot be reported verified.
        card, jwks = signed_card(jku="https://evil.example/jwks.json")
        verdict = await _verify(StubPartner(card=card, jwks=jwks))
        assert verdict.signature_state == SIGNATURE_UNSIGNED

    @pytest.mark.asyncio
    async def test_keys_we_cannot_reach_are_not_a_tampered_card(self):
        """Unevaluable is not invalid.

        FR-015 is fatal because an invalid signature PROVES tampering. A key
        set that is down proves nothing, and refusing there would take a
        working partner offline for its hosting. A partner that verified
        before is still caught — by the signature-downgrade check.
        """
        card, _ = signed_card()
        verdict = await _verify(StubPartner(card=card, jwks=None))
        assert verdict.usability == USABILITY_USABLE
        assert verdict.signature_state == SIGNATURE_UNSIGNED

    @pytest.mark.asyncio
    async def test_a_signed_card_the_sdk_cannot_read_is_unsigned_not_an_exception(self):
        # The SDK refuses a bare-string skill that ``project_card`` accepts.
        # Unreadable is unevaluable, not invalid: unsigned, and still usable.
        card, jwks = signed_card()
        card = {**card, "skills": ["summarise"]}
        verdict = await _verify(StubPartner(card=card, jwks=jwks))
        assert verdict.usability == USABILITY_USABLE
        assert verdict.signature_state == SIGNATURE_UNSIGNED
        assert verdict.card is not None and verdict.card.skills == ["summarise"]

    def test_the_sdk_does_not_get_to_rewrite_the_card_it_verifies(self):
        # ``parse_agent_card`` mutates its argument, and the card is projected afterwards.
        import copy

        from app.a2a.client import sdk

        raw = card_with()
        before = copy.deepcopy(raw)
        assert sdk.verify_signature(raw, lambda _kid, _jku: None, ["ES256"]) is False
        assert raw == before

    @pytest.mark.asyncio
    async def test_a_signed_card_declaring_no_version_still_has_none_in_common(self):
        # The SDK defaults this to ``0.3.0``, which is not an offer.
        card, jwks = signed_card(card_without("protocolVersions"))
        verdict = await _verify(StubPartner(card=card, jwks=jwks))
        assert verdict.usability == USABILITY_UNUSABLE
        assert verdict.unusable_reason == "no_common_protocol_version"
        # The signature itself still verifies.
        assert verdict.signature_state == SIGNATURE_VERIFIED

    @pytest.mark.asyncio
    async def test_an_oversized_key_set_is_refused_before_it_is_read(self):
        # An endless stream: a reader that buffers first never returns; one that
        # enforces while reading stops within a chunk or two of the cap.
        card, _ = signed_card()

        class _Endless(httpx.AsyncByteStream):
            def __init__(self):
                self.pulled = 0

            async def __aiter__(self):
                while self.pulled < 10_000:
                    self.pulled += 1
                    yield b"x" * 1024

        stream = _Endless()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("jwks.json"):
                return httpx.Response(
                    200, stream=stream, headers={"content-type": "application/json"}
                )
            return httpx.Response(200, json=card)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            verdict = await verification.verify_card(
                card_url=CARD_URL, http_client=client, resolve_dns=False
            )
        cap_in_chunks = verification._JWKS_MAX_BYTES // 1024
        assert stream.pulled <= cap_in_chunks + 2, stream.pulled
        # No keys means nothing to check against: unsigned, and still usable.
        assert verdict.signature_state == SIGNATURE_UNSIGNED
        assert verdict.usability == USABILITY_USABLE

    @pytest.mark.asyncio
    async def test_a_declared_oversize_costs_no_bytes_at_all(self):
        card, _ = signed_card()
        pulled = {"n": 0}

        class _Counted(httpx.AsyncByteStream):
            async def __aiter__(self):
                pulled["n"] += 1
                yield b"{}"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("jwks.json"):
                return httpx.Response(
                    200,
                    stream=_Counted(),
                    headers={"content-length": str(verification._JWKS_MAX_BYTES + 1)},
                )
            return httpx.Response(200, json=card)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            verdict = await verification.verify_card(
                card_url=CARD_URL, http_client=client, resolve_dns=False
            )
        assert pulled["n"] == 0
        assert verdict.signature_state == SIGNATURE_UNSIGNED

    @pytest.mark.asyncio
    async def test_the_verifier_cannot_be_left_unwired(self):
        """No parameter to omit: an injected verifier is one a caller can skip,
        and skipping it reports every card unsigned rather than failing."""
        import inspect

        assert "verify_signature" not in inspect.signature(
            verification.verify_card
        ).parameters


class TestTolerance:
    @pytest.mark.asyncio
    async def test_an_absent_optional_field_does_not_make_a_card_malformed(self):
        # Both vendor surfaces are Preview and expected to move. A card
        # missing an optional field is a partner we can still reach.
        verdict = await _verify(StubPartner(card=card_with(description=None)))
        assert verdict.usability == USABILITY_USABLE

    @pytest.mark.asyncio
    async def test_a_card_with_no_declared_versions_has_none_in_common(self):
        # Not the same as tolerating it: FR-023 forbids relying on a default,
        # so a partner that advertises nothing is one we have not agreed a
        # version with.
        partner = StubPartner(card={k: v for k, v in GOOD_CARD.items() if k != "protocolVersions"})
        verdict = await _verify(partner)
        assert verdict.unusable_reason == "no_common_protocol_version"


class TestFingerprint:
    @pytest.mark.asyncio
    async def test_material_fields_change_the_fingerprint(self):
        base = (await _verify(StubPartner())).fingerprint
        for material in (
            {"url": "https://partner.example/rpc2"},
            {"defaultInputModes": ["application/json"]},
            {"defaultOutputModes": ["application/json"]},
            {"securitySchemes": {"apiKey": {"type": "apiKey"}}},
        ):
            changed = (await _verify(StubPartner(card=card_with(**material)))).fingerprint
            assert changed != base, material

    @pytest.mark.asyncio
    async def test_cosmetic_fields_do_not(self):
        # Suspending delegation because a partner reworded its description
        # would train an operator to click "accept" without reading.
        base = (await _verify(StubPartner())).fingerprint
        cosmetic = (
            await _verify(StubPartner(card=card_with(description="Reworded.")))
        ).fingerprint
        assert cosmetic == base

    @pytest.mark.asyncio
    async def test_losing_a_signature_changes_the_fingerprint(self):
        # FR-052: a partner that once proved its card and stops is the one
        # case where an unsigned card is evidence rather than the state of
        # the art, so it must read as a material change.
        card, jwks = signed_card()
        signed = (await _verify(StubPartner(card=card, jwks=jwks))).fingerprint
        unsigned = (await _verify(StubPartner())).fingerprint
        assert signed != unsigned


class TestACardMustDeclareWhereWorkGoes:
    async def test_a_card_with_no_url_is_malformed(self):
        # The endpoint is not optional: a card that declares none cannot be
        # tasked, and accepting it would leave delegation to improvise an
        # address — which is how every submission ended up POSTed at the
        # partner's own discovery document (HTTP 405).
        from tests.a2a_client_stubs import card_without

        verdict = await _verify(StubPartner(card=card_without("url")))
        assert verdict.usability == "unusable"
        assert verdict.unusable_reason == "card_malformed"
        assert "url" in (verdict.error_detail or "")

    async def test_the_declared_endpoint_is_carried_on_the_verdict(self):
        # apply_verdict persists it from here, so a verdict that dropped it
        # would leave a usable registration with nowhere to send work.
        verdict = await _verify(StubPartner())
        assert verdict.is_usable
        assert verdict.card is not None
        assert verdict.card.invocation_url == "https://partner.example/rpc"
