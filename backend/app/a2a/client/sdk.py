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

"""The second — and only other — module permitted to import ``a2a_sdk``.

``app.a2a.protocol`` is the first, and stays the only **shaper**: two shapers
would eventually disagree about what v0.3 means, and the disagreement would
surface as a partner seeing a card that does not match the task surface it
describes. What lives HERE is the SDK's client half — card parsing and JWS
signature verification — a different concern from type and version shaping.

Deliberately NOT here: a wrapper around the SDK's ``A2ACardResolver`` —
fetching a card is a plain authenticated GET and the projection onto platform
types is ours. Signature verification is the one part not to reimplement: a
signed agent card is a detached JWS over an RFC 8785 canonicalisation, and a
hand-rolled verifier would have to match the SDK's canonicalisation byte for
byte to agree with the partner that produced the signature.

The invariant both modules keep: **no ``a2a_sdk`` type crosses out of
``app/a2a``**. Everything here returns platform types or plain data.
``tests/test_a2a_client_sdk_containment.py`` pins the two-module list.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# --- the containment boundary: a2a_sdk imports appear ONLY below this line ---
from a2a.utils.signing import (
    InvalidSignaturesError,
    NoSignatureError,
    create_signature_verifier,
)

from app.a2a.protocol import DEFAULT_TRANSPORT, normalise_transport

# --- and nowhere else in this file ------------------------------------------


class CardUnreadableBySdkError(Exception):
    """The SDK cannot parse this card, so its signature cannot be checked.

    Not :class:`CardSignatureInvalidError`: an invalid signature proves
    tampering; an unparseable card proves nothing about its signature.
    """


class CardSignatureInvalidError(Exception):
    """A signature was present and did not verify.

    Fatal with no operator override (FR-015), and deliberately distinct from
    "no signature at all": the SDK draws that line too, which is a good sign
    the distinction is the conventional one rather than a local compromise.
    """


@dataclass(frozen=True)
class PartnerInterface:
    """One endpoint a card declares, with the binding and versions it serves.

    A card does not declare "an endpoint": it declares a set of interfaces,
    each with its own transport binding and — in v1.0 — its own protocol
    version. All of them must be read: a partner may serve REST beside
    JSON-RPC, and the card's ordering is its preference, not this platform's
    choice.
    """

    url: str
    #: The canonical token from :mod:`app.a2a.protocol`, or ``None`` when the
    #: partner named a binding we do not recognise at all. Kept distinct from
    #: "recognised but unsupported" (``GRPC``) so the refusal can say which.
    transport: str | None
    #: Verbatim, for the operator-facing detail. A partner's own spelling is
    #: what they will search their own configuration for.
    declared_transport: str | None
    protocol_versions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PartnerCard:
    """What this platform reads from a partner's card, in platform terms.

    A plain dataclass rather than the SDK's ``AgentCard`` so no SDK type
    escapes this module. Only the fields the feature actually uses are
    carried: a projection is also what stops an unrelated SDK change from
    rippling into the verification logic.
    """

    name: str | None
    description: str | None
    skills: list[str] = field(default_factory=list)
    input_types: list[str] = field(default_factory=list)
    output_types: list[str] = field(default_factory=list)
    invocation_url: str | None = None
    #: Every interface the card declares, in the order the card presents them
    #: — NOT in the order this platform prefers. Choosing among them is the
    #: verification's job, because the choice depends on the pinned origin,
    #: which a projection cannot know.
    interfaces: list[PartnerInterface] = field(default_factory=list)
    auth_scheme: str | None = None
    protocol_versions: list[str] = field(default_factory=list)
    #: ``True`` when the card carried a signature that VERIFIED. A card with
    #: no signature is ``False``, not an error — neither supported target
    #: signs today, and refusing them would ship a feature that reaches
    #: nothing (FR-016). An INVALID signature never produces a card at all.
    signature_verified: bool = False
    #: Whether this partner says it will call back, from ``capabilities``.
    #: Read for the same reason the content types are: to know what an exchange
    #: with it can be, before one is attempted. Absent reads as ``False`` — a
    #: partner that says nothing offers nothing extra, and this platform never
    #: probes for a capability nor infers one from a call that happened to work.
    declares_callbacks: bool = False
    #: Where this partner says it lists the agents our credential reaches, or
    #: ``None`` when it declares no such extension. Read from
    #: ``capabilities.extensions`` — the spec's own way to advertise a
    #: capability it does not define — so discovery asks only partners that
    #: said they answer. See :mod:`app.a2a.extensions`.
    partner_discovery_path: str | None = None


def _declared_discovery_path(caps: dict[str, Any]) -> str | None:
    """The discovery path this card advertises, or ``None``.

    Matched on the extension URI and nothing else: the URI is the extension's
    NAME, so a partner implementing the same extension spells it identically
    however its endpoint is mounted. A declaration without a usable ``params``
    path still counts — it says the partner implements the extension, and the
    default path is what the extension's own definition specifies — so a
    partner that declares tersely is not read as declaring nothing.

    Everything here is a partner-authored value, so it is validated rather
    than trusted: a path that is not a single rooted path is dropped back to
    the default. A partner cannot use this to move us to another origin (the
    caller joins it to the origin it authenticated to) but it could otherwise
    aim the request at ``//evil.example`` — a protocol-relative URL, which
    most clients resolve as a different HOST.
    """
    from app.a2a.extensions import PARTNER_DISCOVERY_PATH, PARTNER_DISCOVERY_URI

    for entry in caps.get("extensions") or []:
        if not isinstance(entry, dict) or entry.get("uri") != PARTNER_DISCOVERY_URI:
            continue
        params = entry.get("params")
        declared = (params or {}).get("path") if isinstance(params, dict) else None
        if not isinstance(declared, str):
            return PARTNER_DISCOVERY_PATH
        declared = declared.strip()
        if not declared.startswith("/") or declared.startswith("//"):
            return PARTNER_DISCOVERY_PATH
        return declared
    return None


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    out = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            # Skills arrive as objects; their name is what an operator reads.
            named = item.get("name") or item.get("id")
            if named:
                out.append(str(named))
    return out


def _project_interfaces(
    raw: dict[str, Any], card_versions: list[str]
) -> list[PartnerInterface]:
    """Read every interface a card declares, in either version's shape.

    v0.3 spells this as ``url`` + ``preferredTransport``, with further
    endpoints under ``additionalInterfaces`` (``url`` / ``transport``). v1.0
    renamed the whole thing to ``supportedInterfaces``, whose entries carry
    ``protocolBinding`` and their own ``protocolVersion`` — a v1.0 card has no
    top-level ``url`` at all, so reading only that field refuses a valid card
    as malformed.

    The v0.3 primary comes first so that :attr:`PartnerCard.invocation_url`
    keeps meaning what it meant. Duplicates are dropped on first sight:
    ``a2a.card`` repeats the sole binding under ``additionalInterfaces``
    because the specification asks for that list to be complete when present,
    and every partner following the same rule would otherwise be read as
    offering two identical interfaces.
    """
    found: list[PartnerInterface] = []

    def add(url: Any, declared: Any, versions: list[str]) -> None:
        if not isinstance(url, str) or not url.strip():
            return
        transport = (
            normalise_transport(declared) if declared is not None else DEFAULT_TRANSPORT
        )
        found.append(
            PartnerInterface(
                url=url.strip(),
                transport=transport,
                declared_transport=declared if isinstance(declared, str) else None,
                protocol_versions=versions,
            )
        )

    add(raw.get("url") or raw.get("endpoint"), raw.get("preferredTransport"), card_versions)

    for entry in raw.get("supportedInterfaces") or []:
        if not isinstance(entry, dict):
            continue
        # Per-interface versions when declared, the card's list otherwise: an
        # interface that names no version is not a version-less endpoint, it is
        # one that serves what the card serves.
        versions = _as_list(
            entry.get("protocolVersion") or entry.get("protocolVersions")
        ) or card_versions
        add(entry.get("url"), entry.get("protocolBinding") or entry.get("transport"), versions)

    for entry in raw.get("additionalInterfaces") or []:
        if not isinstance(entry, dict):
            continue
        add(entry.get("url"), entry.get("transport") or entry.get("protocolBinding"), card_versions)

    unique: list[PartnerInterface] = []
    seen: set[tuple[str, str | None]] = set()
    for interface in found:
        key = (interface.url, interface.transport)
        if key not in seen:
            seen.add(key)
            unique.append(interface)
    return unique


def _merge_versions(
    card_versions: list[str], interfaces: list[PartnerInterface]
) -> list[str]:
    merged = list(card_versions)
    for interface in interfaces:
        for version in interface.protocol_versions:
            if version not in merged:
                merged.append(version)
    return merged


def project_card(raw: dict[str, Any], *, signature_verified: bool) -> PartnerCard:
    """Project a parsed card onto the fields this feature uses.

    Tolerant of absent fields on purpose: both target platforms' A2A surfaces
    are Preview and expected to move, and a card missing an optional field is
    a partner we can still reach — not a malformed one. What is NOT tolerated
    is a card that fails to parse at all, which the caller treats as
    ``card_malformed``.
    """
    caps = raw.get("capabilities")
    if not isinstance(caps, dict):
        # An empty map serialised as ``[]`` declares nothing; it is not malformed.
        caps = {}
    card_versions = _as_list(
        raw.get("protocolVersions") or raw.get("protocolVersion") or caps.get("protocolVersions")
    )
    interfaces = _project_interfaces(raw, card_versions)
    return PartnerCard(
        name=raw.get("name"),
        description=raw.get("description"),
        skills=_as_list(raw.get("skills")),
        input_types=_as_list(raw.get("defaultInputModes") or raw.get("inputModes")),
        output_types=_as_list(raw.get("defaultOutputModes") or raw.get("outputModes")),
        # The v0.3 primary endpoint, unchanged. Kept as its own field rather
        # than derived from ``interfaces`` because it is fingerprinted, and a
        # fingerprint that shifted under an upgrade would suspend every
        # registration on the deployment with ``card_change_unaccepted``.
        invocation_url=(
            raw.get("url") or raw.get("endpoint") or (interfaces[0].url if interfaces else None)
        ),
        interfaces=interfaces,
        auth_scheme=(
            next(iter((raw.get("securitySchemes") or {}).keys()), None)
            if isinstance(raw.get("securitySchemes"), dict)
            else None
        ),
        # Card-level first, then anything only an interface declares. A v1.0
        # card carries no card-level version at all — the version is per
        # interface — and ``detect_downgrade`` compares the agreed version
        # against this list, so omitting them would read a v1.0 partner as
        # having withdrawn the version we just agreed with it.
        protocol_versions=_merge_versions(card_versions, interfaces),
        signature_verified=signature_verified,
        # ``is True`` rather than truthiness: the value is partner-authored,
        # and a non-empty string or a number is not a declaration.
        declares_callbacks=caps.get("pushNotifications") is True,
        partner_discovery_path=_declared_discovery_path(caps),
    )


def verify_signature(raw_card: dict[str, Any], key_provider, algorithms) -> bool:
    """Verify a card's signature if it has one.

    Returns ``True`` when a signature verified, ``False`` when the card
    carries none. Raises :class:`CardSignatureInvalidError` when one is
    present and does not verify — the case with no operator override.

    The three outcomes map straight onto the SDK's own error split, which is
    why this function is short: ``NoSignatureError`` and
    ``InvalidSignaturesError`` are exactly FR-016 and FR-015. The fourth,
    :class:`CardUnreadableBySdkError`, means the SDK never looked.
    """
    from a2a.client.card_resolver import parse_agent_card

    # A copy: the parser mutates its argument (pops ``url``, ``protocolVersion``
    # and ``additionalInterfaces``, defaults a missing version to ``0.3.0``),
    # and the caller projects the same dict afterwards.
    try:
        parsed = parse_agent_card(copy.deepcopy(raw_card))
    except Exception as exc:
        # Stricter than ``project_card``, e.g. a skill spelled as a bare string.
        raise CardUnreadableBySdkError(f"{type(exc).__name__}: {exc}") from exc

    verifier = create_signature_verifier(key_provider, algorithms)
    try:
        verifier(parsed)
    except NoSignatureError:
        return False
    except InvalidSignaturesError as exc:
        raise CardSignatureInvalidError(str(exc)) from exc
    return True
