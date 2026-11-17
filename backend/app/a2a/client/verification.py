# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Fetch a partner's card, verify it fail-closed, and produce one verdict.

Where benchmark finding **F7** is answered. It is explicit that a client's
verification must be **fail-closed**: anything that cannot be completed
confidently blocks delegation rather than warning and proceeding (FR-013).
This module is the only place that concept lives.

The split here is deliberate. :func:`verify_card` does the network work and
returns a :class:`Verdict` without touching the database, so the whole
defective-card matrix is testable against a stub with no session in sight.
:func:`apply_verdict` and :func:`accept_card` are the only things that write.
A module that did both at once would make every matrix case a database test.

**What "fail-closed" means concretely**, since it is easy to nod at: every
path out of :func:`verify_card` that is not a fully successful verification
returns ``usability=unusable`` with a reason from the closed set. There is no
path that returns "probably fine".
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.a2a.client.destinations import (
    DestinationRefusedError,
    assert_allowed_async,
    assert_same_origin,
)
from app.a2a.client.sdk import (
    CardSignatureInvalidError,
    CardUnreadableBySdkError,
    PartnerCard,
    PartnerInterface,
    project_card,
)
from app.a2a.protocol import (
    SUPPORTED_TRANSPORTS,
    negotiate_outbound_version,
    only_newer_than,
    still_offers,
    transport_supported,
)
from app.models.a2a import (
    SIGNATURE_UNSIGNED,
    SIGNATURE_VERIFIED,
    UNUSABLE_CARD_CHANGE_UNACCEPTED,
    UNUSABLE_CARD_MALFORMED,
    UNUSABLE_CARD_UNFETCHABLE,
    UNUSABLE_NO_COMMON_TRANSPORT,
    UNUSABLE_NO_COMMON_VERSION,
    UNUSABLE_ORIGIN_MISMATCH,
    UNUSABLE_SIGNATURE_DOWNGRADED,
    UNUSABLE_SIGNATURE_INVALID,
    UNUSABLE_VERSION_DOWNGRADED,
    USABILITY_UNUSABLE,
    USABILITY_USABLE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    """The single outcome of one verification.

    Immutable because two consumers read it — the caller that writes it to the
    registration, and the delegation pre-flight that reads what was written.
    One of them mutating it would be a disagreement created after the fact,
    which is the shape of bug FR-053 exists to prevent.
    """

    usability: str
    unusable_reason: str | None = None
    signature_state: str | None = None
    agreed_version: str | None = None
    #: The interface CHOSEN among those the card declares - endpoint and
    #: binding together, because they are one decision. Carried on the
    #: verdict rather than read back off the card by the caller: the choice
    #: depends on the pinned origin and on what this platform speaks, and a
    #: second party re-deriving it could choose differently.
    invocation_url: str | None = None
    transport: str | None = None
    pinned_origin: str | None = None
    fingerprint: str | None = None
    card: PartnerCard | None = None
    error_detail: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.usability == USABILITY_USABLE


def _unusable(reason: str, detail: str | None = None, **kw) -> Verdict:
    return Verdict(
        usability=USABILITY_UNUSABLE, unusable_reason=reason, error_detail=detail, **kw
    )


def fingerprint_card(card: PartnerCard) -> str:
    """A stable hash over the card's MATERIAL fields only.

    Material means a change to it suspends delegation (FR-019). Everything
    else on a card is cosmetic: suspending because a partner reworded its
    description would train an operator to click "accept" without reading,
    which is worse than not suspending at all.

    Signature PRESENCE is material, its value is not: a partner rotating its
    signing key has not changed what it does, while a partner that stops
    signing has withdrawn a proof it was making (FR-052).
    """
    material: dict[str, Any] = {
        "invocation_url": card.invocation_url,
        "auth_scheme": card.auth_scheme,
        # Sorted: a partner reordering its content types has not changed
        # which ones it accepts.
        "input_types": sorted(card.input_types),
        "output_types": sorted(card.output_types),
        "signature_present": card.signature_verified,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class Refusal:
    """Why no interface the card declares can be used."""

    reason: str
    detail: str


@dataclass(frozen=True)
class Selection:
    """The interface a verification chose, and the version agreed on it."""

    url: str
    transport: str
    version: str


def select_interface(card: PartnerCard, pinned_origin: str) -> Selection | Refusal:
    """Choose one of the card's interfaces, or say why none will do.

    Three filters, in this order, because each is cheaper and more certain
    than the next - and because the ORDER decides which reason an operator is
    given for a card that fails several of them at once:

    1. **Origin.** An interface off the origin the card was fetched from is
       discarded rather than followed (FR-034). First, so a partner cannot
       earn a gentler diagnosis by also offering a transport we like.
    2. **Transport.** A binding this platform does not speak is not an
       endpoint, whatever it serves.
    3. **Version**, per interface - v1.0 declares it per interface, so the
       answer can legitimately differ between two endpoints on one card.

    Preference runs over :data:`SUPPORTED_TRANSPORTS` (ours), not over the
    card's ordering. A partner's ``preferredTransport`` states what it would
    rather serve; which binding this platform is better at is our question,
    and the card is consulted only for what is on offer.
    """
    if not card.interfaces:
        return Refusal(
            UNUSABLE_CARD_MALFORMED,
            "The card declares no endpoint - neither 'url' nor "
            "'supportedInterfaces' - so there is nowhere to send work.",
        )

    on_origin: list[PartnerInterface] = []
    first_off_origin: str | None = None
    for interface in card.interfaces:
        try:
            assert_same_origin(interface.url, pinned_origin)
        except DestinationRefusedError as exc:
            first_off_origin = first_off_origin or str(exc)
            continue
        on_origin.append(interface)

    if not on_origin:
        return Refusal(
            UNUSABLE_ORIGIN_MISMATCH, first_off_origin or "off-origin endpoint"
        )

    speakable = [i for i in on_origin if transport_supported(i.transport)]
    if not speakable:
        offered = ", ".join(
            sorted(
                {
                    i.declared_transport or i.transport or "an unnamed binding"
                    for i in on_origin
                }
            )
        )
        return Refusal(
            UNUSABLE_NO_COMMON_TRANSPORT,
            f"Partner serves {offered}; this platform speaks "
            f"{', '.join(SUPPORTED_TRANSPORTS)}.",
        )

    for transport in SUPPORTED_TRANSPORTS:
        for interface in speakable:
            if interface.transport != transport:
                continue
            agreed = negotiate_outbound_version(interface.protocol_versions)
            if agreed is not None:
                return Selection(url=interface.url, transport=transport, version=agreed)

    offered = sorted({v for i in speakable for v in i.protocol_versions})
    return Refusal(
        UNUSABLE_NO_COMMON_VERSION, f"Partner offers {offered or 'no versions'}"
    )



#: Asymmetric only. A symmetric algorithm here would let a partner sign its own
#: card with a key we published, which is the algorithm-confusion attack the
#: SDK's ``algorithms`` argument exists to prevent.
_SIGNATURE_ALGORITHMS = [
    "RS256", "RS384", "RS512",
    "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512",
    "EdDSA",
]

#: A key set is small; a partner pointing us at something large is a partner
#: spending our memory.
_JWKS_MAX_BYTES = 64 * 1024


def _signature_headers(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The protected header of each signature the card carries."""
    out = []
    for entry in raw.get("signatures") or []:
        protected = (entry or {}).get("protected") if isinstance(entry, dict) else None
        if not isinstance(protected, str):
            continue
        try:
            padded = protected + "=" * (-len(protected) % 4)
            header = json.loads(base64.urlsafe_b64decode(padded).decode())
        except Exception:
            logger.debug("a2a.client: unreadable signature header on a card", exc_info=True)
            continue
        if isinstance(header, dict):
            out.append(header)
    return out


async def _fetch_jwks(
    url: str, *, http_client, pinned_origin: str, resolve_dns: bool
) -> dict[str, Any] | None:
    """One key set, or ``None`` when we will not or cannot fetch it.

    The ``jku`` comes out of the card's own protected header, so it is a
    partner-controlled URL the backend would fetch — the same request-forgery
    surface as the card location, and guarded the same way. Pinned as well as
    guarded: a card whose keys live on another origin is a card asking us to
    trust a third party it named (FR-034).
    """
    try:
        assert_same_origin(url, pinned_origin)
        await assert_allowed_async(url, resolve=resolve_dns)
    except DestinationRefusedError:
        return None
    try:
        async with http_client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            # Enforced while reading: a cap checked after buffering has already
            # spent the memory it protects.
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > _JWKS_MAX_BYTES:
                return None
            buffered = bytearray()
            async for chunk in response.aiter_bytes():
                buffered += chunk
                if len(buffered) > _JWKS_MAX_BYTES:
                    return None
        body = json.loads(bytes(buffered))
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def _card_signature_state(
    raw: dict[str, Any], *, http_client, pinned_origin: str, resolve_dns: bool
) -> str:
    """``verified`` or ``unsigned``; raises on a signature that does not verify.

    The keys are fetched BEFORE handing anything to the SDK, because its
    verifier is synchronous and its key provider may not await. Anything we
    decline to fetch simply yields no key, which the SDK counts as a signature
    that did not verify rather than as an absent one — failing closed.
    """
    from jwt import InvalidKeyError, PyJWKSet

    headers = _signature_headers(raw)
    if not headers:
        return SIGNATURE_UNSIGNED  # FR-016: absent is not invalid.

    keys: dict[str | None, Any] = {}
    for jku in dict.fromkeys(h.get("jku") for h in headers if h.get("jku")):
        jwks = await _fetch_jwks(
            jku,
            http_client=http_client,
            pinned_origin=pinned_origin,
            resolve_dns=resolve_dns,
        )
        if not jwks:
            continue
        try:
            key_set = PyJWKSet.from_dict(jwks)
        except Exception:
            logger.debug("a2a.client: unreadable JWKS at %s", jku, exc_info=True)
            continue
        for key in key_set.keys:
            keys.setdefault(key.key_id, key)

    if not keys:
        # Nothing to check against is not the same as checked and wrong. FR-015
        # is fatal because an invalid signature PROVES the card was tampered
        # with; a key set that is down, or on an origin we will not follow,
        # proves nothing, and refusing there would take a working partner
        # offline for its own hosting choice. Recorded as unsigned, which is
        # what we can honestly claim — and a partner that verified before is
        # still caught, by the signature-downgrade check.
        return SIGNATURE_UNSIGNED

    def key_provider(kid, _jku):
        key = keys.get(kid) or (next(iter(keys.values()), None) if kid is None else None)
        if key is None:
            # A PyJWT error, deliberately: the SDK's verifier catches those per
            # signature and moves on, so an unavailable key fails THAT
            # signature instead of escaping a function that must not raise.
            raise InvalidKeyError(f"no key for kid={kid!r}")
        return key

    from app.a2a.client.sdk import verify_signature

    try:
        verified = verify_signature(raw, key_provider, _SIGNATURE_ALGORITHMS)
    except CardUnreadableBySdkError as exc:
        # Like an unreachable key set: unevaluable is not invalid. A partner
        # that verified before is still caught by the downgrade check.
        logger.warning(
            "a2a.client: signed card unreadable by the SDK, signature not checked — %s",
            exc,
        )
        return SIGNATURE_UNSIGNED
    return SIGNATURE_VERIFIED if verified else SIGNATURE_UNSIGNED


async def verify_card(
    *,
    card_url: str,
    http_client,
    headers: dict[str, str] | None = None,
    resolve_dns: bool = True,
) -> Verdict:
    """Fetch and verify one partner card. Never raises; always returns a verdict.

    Never raising is the point: a caller that has to distinguish "returned
    unusable" from "raised" would have two failure paths for one question, and
    the one it forgot would be the one that mattered.
    """
    # 1. Destination, BEFORE any request. An operator-supplied URL fetched
    #    with stored credentials is a request-forgery surface, and refusing
    #    here means no credential is ever sent to a destination we would not
    #    have allowed (FR-012, FR-035).
    try:
        pinned_origin = await assert_allowed_async(card_url, resolve=resolve_dns)
    except DestinationRefusedError as exc:
        # The exception carries its own closed-set reason. Deriving one here
        # by inspecting the message would be a second classification of the
        # same failure, disagreeing with the first the moment either changed.
        return _unusable(exc.reason, str(exc))

    # 2. Fetch.
    try:
        response = await http_client.get(card_url, headers=headers or {})
    except Exception as exc:
        return _unusable(UNUSABLE_CARD_UNFETCHABLE, f"{type(exc).__name__}: {exc}")
    if response.status_code != 200:
        return _unusable(
            UNUSABLE_CARD_UNFETCHABLE, f"HTTP {response.status_code} from card URL"
        )

    # 3. Parse.
    try:
        raw = response.json()
    except Exception as exc:
        return _unusable(UNUSABLE_CARD_MALFORMED, f"{type(exc).__name__}: {exc}")
    if not isinstance(raw, dict):
        return _unusable(UNUSABLE_CARD_MALFORMED, "Card is not a JSON object")

    # 4. Signature. An INVALID one is fatal with no operator override
    #    (FR-015); an ABSENT one is accepted and recorded as such (FR-016).
    #
    #    Resolved HERE rather than injected. An injected verifier is one a
    #    caller can omit, and omitting it does not fail — it silently reports
    #    every card as unsigned, which is the shape a security control must
    #    not have.
    try:
        signature_state = await _card_signature_state(
            raw,
            http_client=http_client,
            pinned_origin=pinned_origin,
            resolve_dns=resolve_dns,
        )
    except CardSignatureInvalidError as exc:
        # No signature_state on the verdict: an invalid signature is not a
        # state the registration operates in, it is a refusal.
        return _unusable(UNUSABLE_SIGNATURE_INVALID, str(exc))

    # 4b. Projection. Partner-authored values, so a shape the projection
    #     cannot read is a malformed verdict — this function never raises,
    #     and nothing above it catches one.
    try:
        card = project_card(
            raw, signature_verified=signature_state == SIGNATURE_VERIFIED
        )
    except Exception as exc:
        return _unusable(
            UNUSABLE_CARD_MALFORMED,
            f"{type(exc).__name__}: {exc}",
            signature_state=signature_state,
        )

    # 5-7. Endpoint, transport and version - ONE decision, made together.
    #      Three sequential checks against three separate card fields work
    #      only for the one card shape that carries all three at the top
    #      level: a v0.3 JSON-RPC card. A v1.0 card declares no top-level
    #      ``url`` and no top-level version (both live on
    #      ``supportedInterfaces``), and a partner serving REST beside
    #      JSON-RPC has two of each. See :func:`select_interface`.
    chosen = select_interface(card, pinned_origin)
    if isinstance(chosen, Refusal):
        return _unusable(
            chosen.reason, chosen.detail, signature_state=signature_state
        )

    return Verdict(
        usability=USABILITY_USABLE,
        signature_state=signature_state,
        agreed_version=chosen.version,
        invocation_url=chosen.url,
        transport=chosen.transport,
        pinned_origin=pinned_origin,
        fingerprint=fingerprint_card(card),
        card=card,
    )


def detect_downgrade(settings, verdict: Verdict) -> Refusal | None:
    """A capability the partner PROVED and has now withdrawn, if any.

    FR-052, and the distinction that makes it worth a rule of its own: an
    unsigned card is the state of the art, but a card that once carried a
    verifying signature and now does not is *evidence* — the partner stopped
    doing something it demonstrably could. Same for a protocol version we
    agreed on and it no longer offers.

    Both suspend rather than silently renegotiating downward. Renegotiating
    would be the platform quietly accepting weaker terms on an operator's
    behalf, which is exactly what an operator would want to be asked about.
    An UPGRADE — a newer version appearing — needs no permission and does not
    reach here.
    """
    if (
        settings.signature_state == SIGNATURE_VERIFIED
        and verdict.signature_state == SIGNATURE_UNSIGNED
    ):
        return Refusal(
            UNUSABLE_SIGNATURE_DOWNGRADED,
            "This partner's card used to carry a signature that verified, and "
            "now carries none.",
        )
    if (
        settings.agreed_protocol_version
        and verdict.card is not None
        and not still_offers(
            verdict.card.protocol_versions, settings.agreed_protocol_version
        )
        # Newer versions only is an upgrade off the agreed one, not a
        # withdrawal of it; ``apply_verdict`` renegotiates.
        and not only_newer_than(
            verdict.card.protocol_versions, settings.agreed_protocol_version
        )
    ):
        # Compared through ``still_offers``, never by exact string:
        # negotiation normalises a partner's patch level (``0.3.0`` -> ``0.3``)
        # and stores the normalised value, so an exact comparison would report
        # a withdrawal the partner has not made.
        return Refusal(
            UNUSABLE_VERSION_DOWNGRADED,
            f"Agreed A2A {settings.agreed_protocol_version}, and the partner's "
            f"card now offers "
            f"{verdict.card.protocol_versions or 'no versions'}.",
        )
    return None


#: The suspensions an operator's acceptance lifts, and the only ones: each was
#: imposed by this platform on a card that otherwise verified. Every other
#: reason is a fact about the partner that no click on this side can change.
ACCEPTANCE_LIFTS: frozenset[str] = frozenset(
    {
        UNUSABLE_CARD_CHANGE_UNACCEPTED,
        UNUSABLE_SIGNATURE_DOWNGRADED,
        UNUSABLE_VERSION_DOWNGRADED,
    }
)


def accept_card(settings) -> None:
    """Record that an operator accepted the card currently observed, and resume.

    The second writer of ``usability`` beside :func:`apply_verdict`, because
    acceptance must take effect now: pre-flight reads ``usability`` before it
    compares fingerprints, and the tool builder skips an unusable target.
    Lifts only a suspension from :data:`ACCEPTANCE_LIFTS`; ``apply_verdict``
    wrote the verifying card's fields before it suspended, so the result is
    the row a re-verification of the same card would produce.
    """
    settings.accepted_card_fingerprint = settings.card_fingerprint
    if settings.unusable_reason in ACCEPTANCE_LIFTS:
        settings.usability = USABILITY_USABLE
        settings.unusable_reason = None
        settings.last_verification_error = None


def apply_verdict(settings, verdict: Verdict, *, card_url: str | None = None) -> None:
    """Write one verdict onto a registration.

    ``usability`` is WRITTEN here and read everywhere else, never inferred at
    read time — that is what stops the health surface and the delegation
    pre-flight disagreeing about the same registration. One other function
    writes it, :func:`accept_card`, and only to lift a suspension this one
    imposed.

    Three outcomes for a card that verifies: unchanged (carry on), materially
    changed (suspend until accepted), or downgraded (suspend, with a reason
    naming what was withdrawn).
    """
    settings.usability = verdict.usability
    settings.unusable_reason = verdict.unusable_reason
    settings.last_verified_at = datetime.now(UTC)
    settings.last_verification_error = verdict.error_detail
    if card_url is not None:
        settings.card_url = card_url
    if verdict.is_usable:
        downgrade = detect_downgrade(settings, verdict)
        settings.signature_state = verdict.signature_state
        settings.agreed_protocol_version = verdict.agreed_version
        settings.pinned_origin = verdict.pinned_origin
        # Where work is SENT, as opposed to where the card was FETCHED, and
        # HOW to speak to it. Written here because this is the moment both
        # were decided - against the pinned origin, and against what this
        # platform speaks. Delegation must never re-derive either.
        if verdict.invocation_url is not None:
            settings.invocation_url = verdict.invocation_url
            settings.transport = verdict.transport
        settings.card_fingerprint = verdict.fingerprint
        if verdict.card is not None:
            # Written on every successful verification, INCLUDING when the
            # card no longer declares it — a partner that drops the extension
            # must stop being asked, and only writing the present case would
            # make the flag one-way.
            settings.partner_discovery_path = verdict.card.partner_discovery_path
            settings.declared_name = verdict.card.name
            settings.declared_description = verdict.card.description
            settings.declared_skills = verdict.card.skills
            settings.declared_input_types = verdict.card.input_types
            settings.declared_output_types = verdict.card.output_types
            # Same one-way argument as the discovery path above: a partner
            # that stops declaring callbacks must stop being registered with.
            settings.declares_callbacks = verdict.card.declares_callbacks
        # First successful verification accepts the card it saw. A partner is
        # not "changed" the first time we look at it, and demanding an
        # acceptance click on registration would be a gate that fires on every
        # registration and assures nothing.
        if settings.accepted_card_fingerprint is None:
            settings.accepted_card_fingerprint = verdict.fingerprint
        elif downgrade is not None:
            # Suspended, not merely flagged: delegation stops until an actor
            # with authority accepts what the partner withdrew. The detail says
            # WHAT was withdrawn - a reason alone sent an operator to ask the
            # partner about a change the partner had not made.
            settings.usability = USABILITY_UNUSABLE
            settings.unusable_reason = downgrade.reason
            settings.last_verification_error = downgrade.detail
        elif settings.card_fingerprint != settings.accepted_card_fingerprint:
            settings.usability = USABILITY_UNUSABLE
            settings.unusable_reason = UNUSABLE_CARD_CHANGE_UNACCEPTED
