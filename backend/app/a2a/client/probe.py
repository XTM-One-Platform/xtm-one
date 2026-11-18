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

"""The health verdict, reported through the platform's existing probe seam.

FR-020. A registration's usability is not re-derived here — it is READ from
the row the verification path wrote. That is the whole point: the probe an
operator triggers from "Test Connection" and the pre-flight a delegation runs
must answer from the same value, or they will eventually disagree about the
same partner. Re-deriving it would be a second classification, which is the
failure FR-053 exists to prevent, on a different surface.

Never echoes a credential into ``message``: it is rendered in the UI and
written to the audit trail, a contract the probe seam already states.
"""

from __future__ import annotations

from typing import Any

from app.a2a.client import breaker
from app.integrations.connectivity import ProbeContext, ProbeResult
from app.models.a2a import DIRECTION_OUTBOUND, USABILITY_USABLE

#: What an operator should DO about each reason. A verdict without a next
#: action makes an operator guess, and the guesses are expensive: re-typing a
#: credential that was fine, or contacting a partner about our own config.
_WHAT_TO_DO = {
    "never_verified": "Not verified yet — use Test Connection to verify now.",
    "card_unfetchable": "The card URL did not answer. Check the URL and that the partner is reachable.",
    "card_malformed": "The card URL answered with something that is not an agent card.",
    "transport_unverified": "The card URL must be https on a connection we can verify.",
    "signature_invalid": "The partner's card signature does not verify. This cannot be overridden.",
    "no_common_protocol_version": "The partner speaks no A2A version this platform supports.",
    "no_common_transport": "The partner serves no A2A transport binding this platform speaks (this platform speaks JSON-RPC and HTTP+JSON, not gRPC).",
    "request_rejected_by_partner": "The partner rejected the request this platform sent. Not a card problem - the server log carries the partner's own message. Use Test Connection to clear this once the cause is fixed.",
    "credential_cannot_mint_token": "The stored credentials cannot produce a token. Re-enter them.",
    "credential_rejected_by_partner": "The partner rejected our token. Check the credentials with them.",
    "destination_not_permitted": "That destination is not one this platform will send to.",
    "destination_blocked_by_deployment": "This deployment's network policy refused the partner's address.",
    "origin_mismatch": "The partner's card points at a different origin than its card URL.",
    "card_change_unaccepted": "The partner changed its card. Review and accept the change to resume.",
    "signature_downgraded": "The partner stopped signing its card. Review and accept to resume.",
    "protocol_version_downgraded": "The partner stopped serving the agreed protocol version.",
}


async def _mint_refusal(integration: Any) -> str | None:
    """Mint a fresh token for an OAuth partner; the refusal detail, or None."""
    import httpx

    from app.a2a.client.registration import _VERIFY_MINTS_CREDENTIALS, test_credential

    if getattr(integration, "provider", None) not in _VERIFY_MINTS_CREDENTIALS:
        return None
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        verdict = await test_credential(integration, client)
    if verdict is None:
        return None
    return verdict.error_detail or "The partner refused the credential."


async def outbound_probe(ctx: ProbeContext) -> ProbeResult | None:
    """The verdict for an outbound registration, or ``None`` if inbound.

    Returning ``None`` rather than a result lets each provider's own inbound
    probe carry on unchanged — this is an addition to those, not a
    replacement, and an inbound registration answers from those alone.
    """
    from app.a2a.providers import get_settings, outbound_targets
    from app.api.v1.platform import CHAT_UNAVAILABLE_DETAIL, is_chat_enabled

    if ctx.db is None or ctx.integration is None:
        return None
    try:
        row = await get_settings(ctx.db, ctx.integration.id)
    except Exception:
        return None
    # ``getattr`` rather than attribute access: a session double may answer
    # every query with one canned result regardless of the statement, and
    # something that is not a settings row cannot tell us a direction. Falling
    # through to the inbound probe is the right outcome — this function is an
    # ADDITION to those, and an inbound registration answers from those alone.
    if getattr(row, "direction", None) != DIRECTION_OUTBOUND:
        return None

    # A2A is offered exactly while agentic chat is.
    if not await is_chat_enabled(ctx.db):
        return ProbeResult.failed(CHAT_UNAVAILABLE_DETAIL)

    targets = await outbound_targets(ctx.db, ctx.integration.id)
    if not targets:
        return ProbeResult.failed(
            "No partner agent is configured. Add at least one agent card URL "
            "in the configuration above."
        )

    # Test Connection tests the credential (#105): an OAuth partner serves its
    # card anonymously, so the stored verdicts below never tried the secret.
    # The refusal leads while some agent is usable; when none is, the per-agent
    # verdicts lead and the refusal is appended, so neither hides the other.
    refused = await _mint_refusal(ctx.integration)
    if refused is not None and any(t.usability == USABILITY_USABLE for t in targets):
        return ProbeResult.failed(refused)
    credential = f" Credential: {refused}" if refused is not None else ""

    # Aggregated across targets, and the arithmetic is the point: a partner
    # exposing three agents where one has stopped answering is neither
    # healthy nor broken, and reporting either would send an operator to the
    # wrong place. Reported as "2 of 3", naming the ones that are not working.
    # ``breaker.is_open``, not the raw column: the column is cleared only by
    # a success, so an expired pause would read as paused until one happened.
    working = [
        t for t in targets if t.usability == USABILITY_USABLE and not breaker.is_open(t)
    ]
    broken = [t for t in targets if t.usability != USABILITY_USABLE]

    def _name(target) -> str:
        return target.declared_name or target.card_url

    if len(working) == len(targets):
        versions = {t.agreed_protocol_version for t in targets if t.agreed_protocol_version}
        unsigned = [t for t in targets if t.signature_state != "verified"]
        detail = f"A2A {'/'.join(sorted(versions))}" if versions else "an agreed version"
        # Stated, not omitted: an unverified card is never presented as
        # verified, and silence would read as the stronger claim (FR-017).
        signed = (
            "all card signatures verified"
            if not unsigned
            else f"{len(unsigned)} of {len(targets)} cards unsigned — trusted on "
            "transport and the URL you supplied"
        )
        agents = "agent" if len(targets) == 1 else "agents"
        return ProbeResult.ok(
            f"Reaches {len(targets)} {agents} over {detail}; {signed}."
        )

    if working:
        trouble = [
            f"{_name(t)} ({_WHAT_TO_DO.get(t.unusable_reason or '', t.unusable_reason or 'paused')})"
            for t in targets
            if t not in working
        ]
        return ProbeResult.degraded(
            f"{len(working)} of {len(targets)} reachable. Not working: "
            + "; ".join(trouble)
        )

    if not broken:
        # Every agent usable, every one paused.
        # Distinct from unusable: the partners are fine as far as we know, we
        # are simply not hammering them. Reported as degraded so an operator
        # does not go looking for a configuration problem there isn't one of.
        return ProbeResult.degraded(
            f"All {len(targets)} paused after repeated failures. "
            "Test Connection to retry now."
        )

    first = broken[0]
    return ProbeResult.failed(
        (
            _WHAT_TO_DO.get(
                first.unusable_reason or "",
                f"Not usable ({first.unusable_reason or 'unknown reason'}).",
            )
            if len(targets) == 1
            else f"None of {len(targets)} agents is reachable. "
            + "; ".join(f"{_name(t)}: {t.unusable_reason or 'unknown'}" for t in broken)
        )
        + credential
    )
