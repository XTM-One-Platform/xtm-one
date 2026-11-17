# SPDX-License-Identifier: Apache-2.0

"""Entra A2A federation provider bootstrap — connectivity probe.

The ``entra_a2a`` provider stores inbound trust configuration (see
``app.a2a.federation``) and contributes no BOUND tools, so it is not in
``PROVIDER_TOOLS`` — the one tool an inbound binding brings,
``ask_the_caller``, comes from the external-task executor instead; importing this module (see ``api.v1.integrations``)
registers the probe that makes the integration page's "Test Connection"
button meaningful for it — without one, the generic credential-shaped test
can only report a misleading failure for a provider that stores no
credential on purpose.

What the probe can honestly verify, in dependency order:

1. the pins parse — the SAME validation the auth path applies per request;
2. the pinned tenant EXISTS and its signing keys are reachable from this
   deployment — the exact JWKS fetch ``verify_entra_token`` performs, and
   the classic quiet misconfiguration (a typo'd tenant GUID looks fine
   until the partner's first 401);
3. the audience pin has a shape some Entra token could actually carry — a
   real audience is a GUID or an Application ID URI (``api://…``,
   ``https://…``, ``urn:…``; v1.0 tokens may prefix ``spn:``), so a bare
   word is a typo no partner token will ever match;
4. the config is actually bound to an exposed agent — valid pins guarding
   nothing is the other quiet misconfiguration.

What it cannot verify: whether a well-shaped audience (or the allowed
caller) is the RIGHT one. Proving those needs a credential in the partner's
tenant, which this design deliberately never stores — only the partner's
first real token exercises them, and a mismatch is then named precisely in
the server WARNING log (``a2a.federation``). The green verdict says so
rather than letting itself be read as "federation fully proven".
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select

from app.a2a.client.probe import outbound_probe as _outbound_probe
from app.a2a.federation import (
    EntraPinsError,
    parse_entra_pins,
    tenant_jwks_url,
)
from app.a2a.providers import ENTRA_A2A_PROVIDER, agents_binding
from app.core.egress import egress_client
from app.integrations.connectivity import (
    ProbeContext,
    ProbeResult,
    register_connectivity,
)

PROVIDER = ENTRA_A2A_PROVIDER

#: Matches the auth path's JWKS fetch bound: the probe must not report
#: "unreachable" under a laxer deadline than the request that matters.
_JWKS_TIMEOUT_SECONDS = 5

#: Appended to every green verdict: a passing test must not read as more
#: than it proved, or a wrong (but well-shaped) audience pin looks healthy
#: right up to the partner's first 401.
_UNPROVABLE = (
    " Whether the audience and allowed-caller pins are the RIGHT ones is "
    "proven only by the partner's first real token — a mismatch is named in "
    "the server log."
)


def _audience_shape_problem(audience: str) -> str | None:
    """A pin no Entra token could ever carry as its ``aud`` — the raw typo.

    Deliberately generous: a GUID, any scheme-qualified URI, ``urn:`` names
    and the v1.0 ``spn:`` prefix all pass. Only a value outside every legal
    App ID URI shape fails, so an exotic-but-real registration is never
    refused while ``foundry-a2a`` (a name, not an identifier) is caught
    here instead of at the partner's first 401.
    """
    candidate = audience.strip()
    try:
        uuid.UUID(candidate)
        return None
    except ValueError:
        pass
    lowered = candidate.lower()
    if "://" in candidate or lowered.startswith(("urn:", "spn:")):
        return None
    return (
        f"The audience {candidate!r} is neither a GUID nor an Application "
        "ID URI (api://…, https://…, urn:…) — no Entra token can carry it "
        "as its aud claim. Copy the exact value from the partner's app "
        "registration."
    )


async def _fetch_tenant_keys(tenant_id: uuid.UUID) -> tuple[str | None, int]:
    """``(problem, key_count)`` for the tenant's JWKS. ``problem`` is a full
    user-facing sentence, or ``None`` when keys came back."""
    try:
        async with egress_client(timeout=_JWKS_TIMEOUT_SECONDS) as client:
            resp = await client.get(tenant_jwks_url(tenant_id))
    except httpx.HTTPError as exc:
        return (
            "Could not reach Microsoft Entra for the partner tenant's signing "
            f"keys ({exc.__class__.__name__}) — the auth path needs "
            "login.microsoftonline.com reachable from this deployment.",
            0,
        )
    if resp.status_code != 200:
        # Entra answers 400 (AADSTS90002) for a GUID that is not a tenant —
        # the typo this probe exists to catch before the partner's first 401.
        return (
            f"Microsoft Entra answered HTTP {resp.status_code} for tenant "
            f"{tenant_id} — check the partner tenant (directory) ID.",
            0,
        )
    try:
        keys = resp.json().get("keys") or []
    except ValueError:
        keys = []
    if not keys:
        return (f"Tenant {tenant_id} published no signing keys.", 0)
    return None, len(keys)


async def check_connectivity(ctx: ProbeContext) -> ProbeResult:
    # An OUTBOUND registration answers from the stored health verdict rather
    # than from these pins, which describe the inbound direction only. See
    # ``a2a.client.probe`` for why it is delegated rather than re-derived.
    outbound = await _outbound_probe(ctx)
    if outbound is not None:
        return outbound

    from app.api.v1.platform import CHAT_UNAVAILABLE_DETAIL, is_chat_enabled

    # A2A is offered exactly while agentic chat is. No session (a probe with
    # no integration context) means nothing to ask; the routes enforce it.
    if ctx.db is not None and not await is_chat_enabled(ctx.db):
        return ProbeResult.failed(CHAT_UNAVAILABLE_DETAIL)

    try:
        pins = parse_entra_pins(ctx.extra or None)
    except EntraPinsError as exc:
        return ProbeResult.failed(f"Configuration problem: {exc}")

    shape_problem = _audience_shape_problem(pins.audience)
    if shape_problem:
        return ProbeResult.failed(shape_problem)

    problem, key_count = await _fetch_tenant_keys(pins.tenant_id)
    if problem:
        return ProbeResult.failed(problem)
    reachable = (
        f"Partner tenant {pins.tenant_id} is reachable "
        f"({key_count} signing key{'' if key_count == 1 else 's'})."
    )

    # The binding half needs the row; provider unit tests may call without it.
    if ctx.db is None or ctx.integration is None:
        return ProbeResult.ok(reachable + _UNPROVABLE)

    from app.models.a2a import A2AExposure

    bound = await agents_binding(ctx.db, ctx.integration.id, PROVIDER)
    if not bound:
        return ProbeResult.degraded(
            f"{reachable} No agent binds this configuration yet — bind it "
            "in an agent's Tools tab."
        )

    exposed = (
        (
            await ctx.db.execute(
                select(A2AExposure).where(
                    A2AExposure.agent_id.in_([a.id for a in bound]),
                    A2AExposure.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    agents_part = f"Bound to {len(bound)} agent{'' if len(bound) == 1 else 's'}"
    if not exposed:
        # Binding normally exposes on the spot, so reaching here means the
        # exposure was refused or later revoked: a disabled agent, one with no
        # owner to run as, or a tier/sharing/owner change that revoked it. The
        # implicit-exposure hook only fires on a bindings edit, so none of
        # those recover on their own — re-saving the binding is the remedy.
        return ProbeResult.degraded(
            f"{reachable} {agents_part}, but none is exposed — check the "
            "agents are enabled and have an owner, then re-save the binding "
            "in the agent's Tools tab to expose them."
        )
    return ProbeResult.ok(
        f"{reachable} {agents_part}, {len(exposed)} exposed." + _UNPROVABLE
    )


register_connectivity(PROVIDER, check_connectivity)
