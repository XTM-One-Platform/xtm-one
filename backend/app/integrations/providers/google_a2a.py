# SPDX-License-Identifier: Apache-2.0

"""Google Agent Platform A2A — connectivity probe for both directions.

The catalog entry exists because neither sibling mechanism's credential flow
reaches Vertex: outbound OAuth presents a client-credentials bearer, while
Vertex wants a JWT signed with a service account's own key, exchanged for a
``cloud-platform``-scoped token. That is a different credential family, not a
different direction on an existing one.

**Inbound is the Google analogue of Foundry A2A** (see
``app.a2a.google_federation``): the partner presents its OWN Google-issued
token and this platform verifies it against pins. Not redundant with
``oauth_a2a`` inbound, which requires this platform to mint a secret and the
partner to hold it; this one stores no secret on either side and lets Google
refresh the caller's own credential forever. Exactly the distinction that
puts ``entra_a2a`` beside ``oauth_a2a``.

Contributes no tools of its own here. The delegation tool is shared across all
three mechanisms (``a2a.client.tool``) because FR-045 requires identical
delegation behaviour and three copies would diverge.

What the INBOUND probe can honestly verify, in dependency order:

1. the pins parse — the SAME validation the auth path applies per request;
2. Google's signing keys are reachable from this deployment — the exact JWKS
   fetch ``verify_google_token`` performs;
3. the config is actually bound to an exposed agent — valid pins guarding
   nothing is the quiet misconfiguration this catches.

What it cannot verify: whether the audience and allowed-caller pins are the
RIGHT ones. Proving those needs a token from the partner's own identity, which
this design deliberately never stores. Note the asymmetry with the Entra
probe, which additionally rejects a malformed audience: an Entra ``aud`` must
be a GUID or an Application ID URI, so a bare word is provably a typo. A
Google ID token's audience is an arbitrary string chosen by whoever mints it,
so there is no shape a probe could refuse without refusing correct setups.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from app.a2a.client.probe import outbound_probe as _outbound_probe
from app.a2a.google_federation import (
    GooglePinsError,
    google_jwks_url,
    parse_google_pins,
)
from app.a2a.providers import GOOGLE_A2A_PROVIDER, agents_binding
from app.core.egress import egress_client
from app.integrations.connectivity import (
    ProbeContext,
    ProbeResult,
    register_connectivity,
)

logger = logging.getLogger(__name__)

PROVIDER = GOOGLE_A2A_PROVIDER

#: Matches the auth path's JWKS fetch bound: the probe must not report
#: "unreachable" under a laxer deadline than the request that matters.
_JWKS_TIMEOUT_SECONDS = 5

#: Appended to every green verdict: a passing test must not read as more than
#: it proved. Both pins are unprovable from this side, and the caller one is
#: the pin that carries the trust decision — so a green tick on a wrong caller
#: pin would be the most misleading verdict this probe could give.
_UNPROVABLE = (
    " Whether the audience and allowed-caller pins are the RIGHT ones is "
    "proven only by the partner's first real token — a mismatch is named in "
    "the server log."
)


async def _fetch_google_keys() -> tuple[str | None, int]:
    """``(problem, key_count)`` for Google's JWKS. ``problem`` is a full
    user-facing sentence, or ``None`` when keys came back."""
    try:
        async with egress_client(timeout=_JWKS_TIMEOUT_SECONDS) as client:
            resp = await client.get(google_jwks_url())
    except httpx.HTTPError as exc:
        return (
            "Could not reach Google for its token signing keys "
            f"({exc.__class__.__name__}) — the auth path needs "
            "www.googleapis.com reachable from this deployment.",
            0,
        )
    if resp.status_code != 200:
        return (
            f"Google answered HTTP {resp.status_code} for its signing keys — "
            "partner tokens cannot be verified until that endpoint is "
            "reachable.",
            0,
        )
    try:
        keys = resp.json().get("keys") or []
    except ValueError:
        keys = []
    if not keys:
        return ("Google published no signing keys.", 0)
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

    # The parse is the assertion: it is the SAME validation the auth path
    # applies per request, so a config it accepts is one whose tokens the task
    # surface will consider. Nothing is read off the result — unlike the Entra
    # probe, which goes on to build a JWKS URL from the pinned tenant. Here
    # that URL is a constant, so the pins have nothing left to contribute.
    try:
        parse_google_pins(ctx.extra or None)
    except GooglePinsError as exc:
        return ProbeResult.failed(f"Configuration problem: {exc}")

    problem, key_count = await _fetch_google_keys()
    if problem:
        return ProbeResult.failed(problem)
    reachable = (
        f"Google's signing keys are reachable "
        f"({key_count} key{'' if key_count == 1 else 's'})."
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
