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

"""A2A inbound server — the external-facing surface.

Two routes, both under ``/a2a/{card_token}``:

- ``GET  /a2a/{card_token}/.well-known/agent-card.json`` — anonymous discovery
- ``POST /a2a/{card_token}/jsonrpc`` — authenticated task operations

**Routing prerequisite**: ``"a2a"`` is in ``_API_NAMESPACES`` in ``main.py``.
Without it the SPA catch-all answers every unknown token with HTTP 200 +
``index.html`` instead of a JSON 404 — which both breaks a partner's
registration tooling on parse and turns the deliberately uniform "not found"
into a signal a caller can enumerate against.

Every negative outcome about *what exists here* returns the SAME 404: unknown
token, revoked exposure, disabled agent. Telling them apart would reconstruct
the listing of exposed agents the specification forbids. Two refusals are
deliberately stated instead — AI disabled, and Community Edition
(``require_chat_enabled``: A2A is offered exactly while agentic chat is, that
is on Enterprise Edition) — because both are already public on the unauthenticated
``GET /platform/config``, so hiding them would reveal nothing and only leave a
partner guessing.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a import protocol as proto
from app.a2a.audit import audit_actor
from app.a2a.card import build_agent_card, card_url
from app.a2a.exposure import get_exposure_by_token
from app.a2a.extensions import PARTNER_DISCOVERY_ROUTE
from app.api.deps import require_secure_transport
from app.core.config import settings
from app.core.database import get_db
from app.core.security import JWT_CLOCK_SKEW_LEEWAY
from app.core.utils import external_base
from app.models.agent import Agent

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/a2a",
    tags=["a2a"],
    dependencies=[Depends(require_secure_transport)],
)


def _not_found() -> JSONResponse:
    """The single uniform negative response — every call site uses THIS, so
    none can drift into a more "helpful" message."""
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


@router.get(PARTNER_DISCOVERY_ROUTE)
async def list_partner_agents(request: Request, db: AsyncSession = Depends(get_db)):
    """Every agent the presenting credential may reach, with its card URL.

    **Not part of the A2A specification**, and DECLARED as such: every card
    this deployment serves carries an ``AgentExtension`` naming this endpoint
    (``a2a.extensions``), which is the spec's own way to advertise a
    capability it does not define. A partner therefore learns discovery exists
    by reading a card rather than by probing a path and reading the 404.

    A card describes exactly one agent, so a partner exposing three publishes
    three cards and a client has no standard way to learn about the second
    and third. This closes that gap between two XTM One deployments; every
    other partner answers 404 and the client falls back to the URLs its
    operator supplied.

    It widens nothing. The caller is told only which agents ITS OWN credential
    already reaches — the same set the task surface would accept from it — so
    the answer is a restatement of an authorisation the partner already holds,
    not a directory of this deployment.
    """
    await _enforce_preauth_rate_limit(request)

    # Chat gate (AI on, Enterprise Edition), behind the rate limit like
    # every check that costs a round-trip; stated rather than folded into
    # ``_not_found()`` — see the module docstring.
    from app.api.deps import require_chat_enabled

    await require_chat_enabled(db)

    integration_id = await _credential_integration(request, db)
    if integration_id is None:
        # Uniform with the rest of the surface: a caller that does not
        # authenticate learns nothing about what exists here.
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    from app.a2a.exposure import get_live_exposure
    from app.a2a.providers import OAUTH_A2A_PROVIDER, agents_binding

    agents = await agents_binding(db, integration_id, OAUTH_A2A_PROVIDER)
    listed = []
    for agent in agents:
        exposure = await get_live_exposure(db, agent.id)
        if exposure is None:
            # Bound but not exposed — nothing for a partner to reach, so
            # naming it would advertise an address that answers 404.
            continue
        listed.append(
            {
                "name": agent.name,
                "card_url": card_url(external_base(request), exposure.card_token),
            }
        )
    return JSONResponse(content={"agents": listed})


async def _credential_integration(request: Request, db: AsyncSession):
    """The integration whose credential is presented, or ``None``.

    OAuth only, deliberately. An Entra-federated partner authenticates against
    pins held per EXPOSURE, and there is no exposure in a partner-level
    request to check them against — so rather than invent a second identity
    path, that mechanism keeps the operator-supplied list.
    """
    from app.a2a.tokens import A2A_TOKEN_TYPE
    from app.models.oauth_client import OAuthClient

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()

    import jwt

    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != A2A_TOKEN_TYPE:
        return None
    client_id = payload.get("client_id")
    if not client_id:
        return None
    client = (
        await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    ).scalar_one_or_none()
    if client is None or minted_before_rotation(payload, client):
        return None
    return getattr(client, "a2a_integration_id", None)


def minted_before_rotation(payload: dict[str, Any], client: Any) -> bool:
    """Whether a partner bearer predates its credential's last secret rotation.

    A regenerate keeps the client id and replaces the secret in place (#105),
    so the row a stale bearer names still exists: the rotation stamp is what
    retires it. ``iat`` is whole seconds, so the comparison is too: a bearer
    minted in the very second of a rotation is accepted rather than a fresh
    one refused.
    """
    rotated_at = getattr(client, "secret_rotated_at", None)
    if rotated_at is None:
        return False
    issued = payload.get("iat")
    if not isinstance(issued, int | float):
        return True
    return int(issued) < int(rotated_at.timestamp())


@router.get("/{card_token}/.well-known/agent-card.json")
async def get_agent_card(
    card_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    a2a_version: str | None = Header(default=None, alias=proto.VERSION_HEADER),
):
    """Serve one agent's card. Anonymous — no credential.

    Both target platforms fetch this without authentication when registering a
    third-party agent, so requiring one here would make the feature
    unreachable from the flow it exists to serve.
    """
    # Anonymous by design, so the per-registration limits cannot apply here;
    # per-IP is the only key there is. Raises a plain HTTP 429 — this route is
    # REST-shaped, not JSON-RPC, so the envelope requirement does not bind.
    await _enforce_preauth_rate_limit(request)

    # Version is negotiated BEFORE any lookup, so an unrecognised version
    # returns a plain client error that reveals nothing about what exists.
    try:
        version = proto.negotiate_version(a2a_version)
    except proto.UnsupportedVersionError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # Chat gate (AI on, Enterprise Edition), after the per-IP limit and
    # the transport check and before any lookup; stated rather than folded
    # into ``_not_found()`` — see the module docstring.
    from app.api.deps import require_chat_enabled

    await require_chat_enabled(db)

    exposure = await get_exposure_by_token(db, card_token)
    if exposure is None:
        return _not_found()

    agent = (
        await db.execute(select(Agent).where(Agent.id == exposure.agent_id))
    ).scalar_one_or_none()
    # A disabled or deleted agent is indistinguishable from an unknown token.
    if agent is None or not agent.enabled:
        return _not_found()

    # Proxy-aware, never raw ``request.base_url``: behind a TLS-terminating
    # proxy the raw value is ``http://``, and the card would hand every
    # partner a plaintext jsonrpc endpoint and token URL.
    base_url = external_base(request)
    return JSONResponse(
        content=build_agent_card(
            agent,
            exposure,
            base_url=base_url,
            version=version,
            app_version=settings.app_version,
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Task surface
# ─────────────────────────────────────────────────────────────────────────────

# Method names live in ``protocol._OPS_BY_METHOD`` — both the v0.3 vocabulary
# (``message/send``…) and the v1.0 one (``SendMessage``…). Dispatch below is
# on the canonical operation ``resolve_method`` returns, so neither
# vocabulary is privileged here.

#: Platform API keys are refused outright on this surface. They are unscoped
#: and non-expiring, so accepting one here would hand an external partner a
#: bearer of its owner's full identity across every surface the platform has.
_REJECTED_KEY_PREFIXES = ("fcp-", "mcp-")


async def _enforce_preauth_rate_limit(request: Request) -> None:
    """Rate-limit unauthenticated traffic per client IP, fail-closed.

    The per-registration limits below only bind once a credential has been
    verified; this is the cap on everything before that — the anonymous card
    fetch, and jsonrpc requests carrying a bad or absent token, each costing a
    jwt decode plus indexed lookups. Rate limiting is this feature's ONLY
    control on external load, so no part of the surface may sit outside it.

    It necessarily runs before authentication, so it initially charges every
    request — and the jsonrpc handler refunds the charge once a request
    authenticates, which is what confines this budget to the traffic named
    above. Without the refund this 60/min per-IP cap is the binding limit on
    legitimate partners, which Foundry exhausts in seconds of ``tasks/get``
    polling.
    """
    from app.core.rate_limit import enforce_rate_limit

    await enforce_rate_limit(
        request,
        "a2a:preauth",
        limit=settings.a2a_unauthenticated_rate_limit,
        window_seconds=settings.a2a_rate_limit_window_seconds,
        fail_closed=True,
    )


def _limit_response(req_id, exc: HTTPException) -> JSONResponse:
    """Shape a limiter refusal into the JSON-RPC envelope, FR-025.

    ``enforce_rate_limit`` raises HTTPException, which FastAPI renders as
    ``{"detail": ...}`` — free text on a surface whose contract says partner
    tooling branches on ``error.code``, never on prose. The HTTP status and
    the ``Retry-After`` header survive; only the body is reshaped.
    """
    throttled = exc.status_code == 429
    return JSONResponse(
        status_code=exc.status_code,
        headers=dict(exc.headers or {}),
        content=proto.jsonrpc_error(
            req_id,
            proto.RATE_LIMITED if throttled else proto.JSONRPC_INTERNAL_ERROR,
            str(exc.detail),
        ),
    )


async def _enforce_a2a_rate_limit(client_id: str, op: str | None) -> None:
    """Rate-limit per registration, genuinely fail-closed on this surface.

    Buckets on the canonical operation, not the method name, so both
    vocabularies of the same operation share one budget — ``SendMessage``
    must not get a second create allowance beside ``message/send``. An
    unresolved method (``op`` None) charges the read bucket: it is about to
    be refused, and reads are the cheap budget.

    ``settings.rate_limit_fail_mode`` defaults to ``"open"``, which skips the
    check when Redis is unreachable. On an internal surface that is a
    deliberate availability trade. Here it is not: this limiter is the ONLY
    control the feature places on external load, so failing open would remove
    it exactly when the platform is already degraded.

    Do not try to detect an outage by probing ``redis_client()`` first: that
    call performs no I/O, it just returns the shared client, so a probe cannot
    raise. The intent has to be expressed where the failure is actually
    caught, which is why ``fail_closed`` is passed through.
    """
    from app.core.rate_limit import enforce_rate_limit

    is_create = op == "send"
    limit = (
        settings.a2a_task_create_rate_limit
        if is_create
        else settings.a2a_task_read_rate_limit
    )
    await enforce_rate_limit(
        None,
        f"a2a:{'create' if is_create else 'read'}",
        limit=limit,
        window_seconds=settings.a2a_rate_limit_window_seconds,
        bucket=client_id,
        fail_closed=True,
    )


async def _audit_refusal(
    db: AsyncSession,
    request: Request,
    *,
    exposure: Any,
    agent: Any,
    client_id: str,
    reason: str,
    summary: str,
) -> None:
    """Leave an audit line for a refused authenticated submission.

    A refusal answered before any task exists leaves no run and no trace, so
    without this an operator reading the audit log sees a partner's accepted
    messages and never the ones it was told to send again. Best effort: the
    refusal is the answer, and a failing audit write must not turn it into a
    500.
    """
    from app.a2a.audit import audit_actor
    from app.services.audit import log_audit

    try:
        await log_audit(
            db,
            user=audit_actor(exposure.run_as_user_id, client_id),
            action="reject",
            entity_type="a2a_exposure",
            entity_id=exposure.id,
            entity_name=getattr(agent, "name", None),
            summary=summary,
            details={"client_id": client_id, "reason": reason},
            request=request,
        )
        await db.commit()
    except Exception:
        # Nothing half-written survives into the response: the audit row is
        # dropped with the failed transaction, the refusal still answers.
        await db.rollback()
        logger.warning("A2A refusal audit write failed (reason=%s)", reason, exc_info=True)


async def _authenticate(
    request: Request, db: AsyncSession, exposure
) -> tuple[str | None, str, str | None]:
    """Resolve the caller: ``(client_id, refusal_reason, provider)``.

    ``provider`` is the A2A provider whose credential form actually verified —
    the ONLY place that fact exists. The registration id cannot be trusted to
    carry it: on an exposure minted by the old explicit toggle, a federated
    caller deliberately reuses that exposure's OAuth-shaped registration (see
    :func:`_authenticate_entra`), so a genuine Foundry submission is recorded
    under an ``a2a-<random>`` id indistinguishable from an OAuth one. Reading
    the shape back later reports that partner's traffic as the other's.
    Stamped on the run instead, at the moment it is known.

    Returns the OAuth ``client_id`` bound to *this* exposure, with an empty
    reason. A credential issued for a different exposure resolves to ``(None,
    ...)``, so the caller receives the uniform not-found rather than a 403
    that would confirm the other agent exists.

    The reason distinguishes exactly one refusal — ``rejected_platform_key``
    versus plain ``unauthenticated`` — and only INTERNALLY, for the audit
    entry and the metric series (a platform key on this surface is the
    migration signal an operator watches for). The response the caller sees
    is identical in both cases; the distinction never crosses the wire.

    The token *kind* is checked positively rather than by excluding kinds this
    surface does not want. Every platform JWT is signed with the same secret,
    so accepting any well-formed token that happens to carry a ``client_id``
    would let a credential minted for another purpose task an agent here.

    Three credential forms resolve to a registration: the platform-minted A2A
    partner token (below), and — only for an agent that bound the matching
    federation integration — a Microsoft Entra token or a Google-issued one
    (``_authenticate_federated``). The first split needs no discriminator: a
    federated token is RS256-signed by an external issuer, so the platform
    decode necessarily fails on it, and a platform token never survives
    federated verification. Order favours the cheap check.
    """
    from app.a2a.providers import OAUTH_A2A_PROVIDER
    from app.a2a.tokens import A2A_TOKEN_TYPE
    from app.models.oauth_client import OAuthClient

    refused = (None, "unauthenticated", None)

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return refused
    token = header[7:].strip()
    if token.startswith(_REJECTED_KEY_PREFIXES):
        return None, "rejected_platform_key", None

    import jwt

    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    except jwt.PyJWTError:
        # Not ours — possibly the exposure's federated credential. This also
        # catches an EXPIRED platform token, which then fails federation too
        # (wrong signer) and lands on the same uniform refusal.
        return await _authenticate_federated(db, exposure, token)

    # Only an A2A partner token, never a platform session or an MCP resource
    # credential. The converse holds too: an A2A token's distinct type and its
    # ``scope`` claim keep it out of ``/mcp/*`` and ``/api/v1/*`` respectively.
    if payload.get("type") != A2A_TOKEN_TYPE:
        return refused

    client_id = payload.get("client_id")
    if not client_id:
        return refused

    client = (
        await db.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
    ).scalar_one_or_none()
    if (
        client is None
        or minted_before_rotation(payload, client)
        or not await _client_covers_exposure(db, client, exposure)
    ):
        return refused
    return client_id, "", OAUTH_A2A_PROVIDER


async def _client_covers_exposure(db: AsyncSession, client, exposure) -> bool:
    """Whether *client* is a credential for THIS exposure's agent.

    Two shapes are valid, and neither may admit the other's agents:

    * **per-integration** (``a2a_integration_id``) — an ``oauth_a2a``
      partner credential, valid on every agent that binds that integration.
      Checked against the agent's CURRENT bindings, so unbinding the
      integration stops the credential on the next request, exactly like
      unbinding the Entra config does.
    * **per-exposure** (``a2a_exposure_id``) — the older shape, minted when
      an agent was exposed by the explicit toggle. Still honoured so
      credentials already in partners' hands keep working, and still bound
      to precisely one exposure.

    A credential issued for a different agent resolves to False, so the
    caller returns the uniform not-found rather than a 403 that would
    confirm the other agent exists.
    """
    if client.a2a_exposure_id is not None:
        return client.a2a_exposure_id == exposure.id

    integration_id = getattr(client, "a2a_integration_id", None)
    if integration_id is None:
        return False

    agent_id = getattr(exposure, "agent_id", None)
    if agent_id is None:
        return False

    from app.a2a.providers import OAUTH_A2A_PROVIDER, bound_integration_ids

    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if agent is None:
        return False
    return integration_id in bound_integration_ids(agent, OAUTH_A2A_PROVIDER)


async def _authenticate_federated(
    db: AsyncSession, exposure, token: str
) -> tuple[str | None, str, str | None]:
    """Route a non-platform bearer to the federation that could admit it.

    Both federated mechanisms verify an RS256 token signed by an external
    issuer, so the platform decode has already failed by the time either is
    reachable. What separates them is the token's own ``iss``, read here
    WITHOUT verifying anything — and that is safe for the same reason
    ``federation.narrow_by_claimed_tenant`` is safe: this only chooses which
    pinned configuration does the checking. Every fact that admits a token
    still comes from a stored pin, so a forged ``iss`` at worst sends a token
    to the mechanism that was going to refuse it anyway.

    A token whose issuer is unreadable or is not Google's goes to the Entra
    path — the routing adds a destination, it does not move any token that
    already had one.
    """
    import jwt as _jwt

    from app.a2a import google_federation

    try:
        claimed_issuer = _jwt.decode(token, options={"verify_signature": False}).get(
            "iss"
        )
    except _jwt.PyJWTError:
        claimed_issuer = None

    if google_federation.is_google_issuer(claimed_issuer):
        return await _authenticate_google(db, exposure, token)
    return await _authenticate_entra(db, exposure, token)


async def _authenticate_google(
    db: AsyncSession, exposure, token: str
) -> tuple[str | None, str, str | None]:
    """The Google federated credential form — opt-in per agent.

    The opt-in is an inbound ``google_a2a`` integration bound through the
    agent's ordinary ``integration_bindings`` (the Tools tab); its pins —
    audience and allowed caller — are loaded and re-validated here on every
    request (``a2a.google_federation`` explains the pinning, and why the
    caller pin carries the whole trust decision). Several partners may be
    bound: one signature check serves them all, because Google's keyset URL
    is a constant rather than a per-partner value.

    No legacy-registration branch (see ``_authenticate_entra``, whose branch
    serves Foundry partners predating the integration model): this mechanism
    has no such history, so the derived identity is the only correct answer.
    """
    from app.a2a import google_federation, providers

    refused = (None, "unauthenticated", None)

    # getattr, not attribute access: unit tests drive this path with minimal
    # exposure stand-ins. No agent, no opt-in to look up.
    agent_id = getattr(exposure, "agent_id", None)
    if not agent_id:
        return refused

    # One cheap bindings read decides whether any JWKS-touching work can
    # happen at all: an agent that never opted in resolves to an empty list
    # and refuses before any network-touching code.
    pins_list = await google_federation.load_agent_google_pins(db, agent_id)
    verified = await google_federation.verify_google_token(token, pins_list)
    if verified is None:
        return refused

    _claims, pins = verified
    if pins.integration_id is None:
        # Only reachable for pins parsed outside the loader, which the auth
        # path never does. Refuse rather than mint an identity from nothing:
        # the registration id IS the partner on the task surface.
        return refused
    return (
        providers.partner_client_id(pins.integration_id, providers.GOOGLE_A2A_PROVIDER),
        "",
        providers.GOOGLE_A2A_PROVIDER,
    )


async def _authenticate_entra(
    db: AsyncSession, exposure, token: str
) -> tuple[str | None, str, str | None]:
    """The federated credential form — an Entra token, opt-in per agent.

    The opt-in is an ``entra_a2a`` integration bound through the agent's
    ordinary ``integration_bindings`` (the Tools tab); its pins — tenant,
    audience, allowed caller — are loaded and re-validated here on every
    request (``a2a.federation`` explains the pinning). Several partners may
    be bound: the token is tried against each usable config until one
    verifies. A token no config admits — or any token at all when the agent
    never bound one, or every bound integration has been deleted or edited
    into an invalid state — folds into the same uniform refusal as every
    other bad credential.

    On success this resolves to the SAME registration ``client_id`` the OAuth
    path returns, which is what makes federation an alternative *credential*
    rather than an alternative *identity*: one partner shares one rate
    budget, one task scope and one audit trail however they authenticate.

    That sharing is why the third element matters. This function always reports
    ``entra_a2a`` — including on the legacy branch below, where the id it
    returns is OAuth-shaped. The id says which BUDGET the call belongs to; only
    this says which credential form was presented, and conflating the two makes
    a Foundry submission show up as OAuth traffic wherever origin is displayed.
    """
    from app.a2a import federation, providers
    from app.models.oauth_client import OAuthClient

    refused = (None, "unauthenticated", None)

    # getattr, not attribute access: unit tests drive this path with minimal
    # exposure stand-ins. No agent, no opt-in to look up.
    agent_id = getattr(exposure, "agent_id", None)
    if not agent_id:
        return refused

    # One cheap bindings read decides whether any JWKS-touching work can
    # happen at all: an agent that never opted in resolves to an empty list
    # and refuses before any network-touching code.
    pins_list = await federation.load_agent_entra_pins(db, agent_id)
    # How many of those configs this token is actually checked against. The
    # token's own (unverified) tenant claim selects them; nothing about trust
    # moves, and the full list stands whenever the claim settles nothing. See
    # ``federation.narrow_by_claimed_tenant`` for why that is safe and why an
    # unauthenticated caller could otherwise force one JWKS fetch per binding.
    candidates = federation.narrow_by_claimed_tenant(token, pins_list)

    claims = None
    verified_by = None
    for pins in candidates:
        claims = await federation.verify_entra_token(
            token,
            tenant_id=pins.tenant_id,
            audience=pins.audience,
            allowed_client=pins.client_id,
        )
        if claims is not None:
            verified_by = pins.integration_id
            break
    if claims is None:
        return refused

    # A federated caller presents no minted credential, so its registration
    # identity comes from the integration that admitted it. Exposures created
    # by the old explicit toggle DID mint one; prefer it when present, so a
    # partner already in flight keeps its task scope, dedup window and rate
    # budget across this change instead of losing sight of its own tasks.
    #
    # ONLY when that borrowing cannot be ambiguous. A per-exposure registration
    # belongs to the AGENT, not to a partner, so with several federation
    # configs bound it would name whichever of them happened to verify — and
    # every partner sharing it would share one task scope (``tasks/get`` scopes
    # by ``client_id``, so one partner could read and cancel another's work),
    # one rate budget, and one dedup window keyed ``(client_id, message_id)``.
    # That is precisely the separation ``partner_client_id`` exists to give.
    # With more than one bound config the derived id is the only correct
    # answer, and continuity is not owed to a setup that never had it.
    #
    # ``pins_list``, NOT ``candidates``: ambiguity is a property of how many
    # partners are BOUND, not of how many this one token was checked against.
    # Narrowing by the claimed tenant routinely leaves one candidate on an
    # agent binding several, and keying off that would hand the shared legacy
    # id back to whichever partner called — restoring the collapse exactly.
    legacy = (
        await db.execute(
            select(OAuthClient).where(OAuthClient.a2a_exposure_id == exposure.id)
        )
    ).scalar_one_or_none()
    if legacy is not None and len(pins_list) == 1:
        return legacy.client_id, "", providers.ENTRA_A2A_PROVIDER
    if verified_by is None:
        return refused
    return (
        providers.partner_client_id(verified_by, providers.ENTRA_A2A_PROVIDER),
        "",
        providers.ENTRA_A2A_PROVIDER,
    )



@router.post("/{card_token}/jsonrpc")
async def a2a_jsonrpc(
    card_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    a2a_version: str | None = Header(default=None, alias=proto.VERSION_HEADER),
):
    """The A2A task surface: send, get, cancel.

    Every negative outcome below returns the SAME not-found envelope. An
    unknown task, another client's task, a revoked exposure and a bad
    credential are indistinguishable by design.
    """

    from app.a2a import metrics as a2a_metrics
    from app.a2a import tasks as task_svc
    from app.a2a.exposure import get_exposure_by_token
    from app.services.audit import log_audit

    # AI disabled — refuse the whole surface (FR-010). ``message/send``
    # runs an agent, so this is an entrypoint that starts AI work, reached by
    # another *agent* rather than a person: exactly the "integration-driven
    # calls" FR-010 names, and the same argument FR-029 makes for MCP.
    #
    # Stated plainly rather than folded into ``_not_found()``. That envelope
    # exists so an unknown task, another client's task, a revoked exposure and
    # a bad credential are indistinguishable — all of which reveal something
    # about *this deployment's data*. The AI state reveals nothing: it is
    # already public on the unauthenticated ``/platform/config``. And FR-010
    # requires that no entrypoint "fail obscurely instead", so a partner whose
    # integration worked yesterday is told why rather than left guessing.
    # Per-IP, BEFORE the body is even parsed: the per-registration limits below
    # cannot bind a caller who never authenticates, and this surface's rate
    # limiting is its only control on external load — so a flood of malformed
    # JSON or unsupported-version requests has to be charged too, or it sits
    # outside every limit this feature has.
    try:
        await _enforce_preauth_rate_limit(request)
    except HTTPException as exc:
        # FR-025 still wants the caller's own id echoed in the refusal, so the
        # body is read here on the refusal path only — best effort, because a
        # malformed body is exactly one of the cases this ordering exists to
        # charge, and it has no id to echo.
        throttled_id = None
        with suppress(Exception):
            peeked = await request.json()
            if isinstance(peeked, dict):
                throttled_id = peeked.get("id")
        return _limit_response(throttled_id, exc)

    # After the per-IP limit, not before it: the limiter above is this
    # surface's only control on external load, and gating first would have an
    # unauthenticated flood pay a cache-miss database round-trip ahead of being
    # throttled. Still before the body is parsed, the exposure resolved or any
    # task runs.
    from app.api.deps import require_chat_enabled
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        refuse_ai_disabled()

    # Then the chat gate, whose licence half pays the tier lookup; its own AI
    # check is a cache hit after the one above.
    await require_chat_enabled(db)

    # Refused on the declared size, before the body is read at all: Starlette
    # imposes no default cap, so without this an anonymous caller's single
    # multi-MB payload is fully buffered and parsed no matter what the rate
    # limit says. A chunked request declares no length and slips past this —
    # ``validate_submission``'s character cap is what bounds that one.
    declared_length = request.headers.get("content-length")
    if (
        declared_length
        and declared_length.isdigit()
        and int(declared_length) > settings.a2a_max_request_bytes
    ):
        return JSONResponse(
            status_code=413,
            content=proto.jsonrpc_error(
                None,
                proto.JSONRPC_INVALID_REQUEST,
                f"Request body exceeds {settings.a2a_max_request_bytes} bytes",
            ),
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content=proto.jsonrpc_error(
                None, proto.JSONRPC_PARSE_ERROR, "Malformed JSON"
            )
        )

    # Shape checks before any ``.get``: a well-formed JSON document that is
    # not a single request object (a batch array, a string, a number) must be
    # answered in the envelope, not with the AttributeError 500 it otherwise
    # raises. Batches are legal JSON-RPC 2.0 but not served on this surface.
    if not isinstance(body, dict):
        return JSONResponse(
            content=proto.jsonrpc_error(
                None,
                proto.JSONRPC_INVALID_REQUEST,
                "Request must be a single JSON-RPC request object",
            )
        )

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse(
            content=proto.jsonrpc_error(
                req_id, proto.JSONRPC_INVALID_PARAMS, "params must be an object"
            )
        )

    try:
        version = proto.negotiate_version(a2a_version)
    except proto.UnsupportedVersionError as exc:
        return JSONResponse(
            content=proto.jsonrpc_error(
                req_id, proto.VERSION_NOT_SUPPORTED, str(exc)
            )
        )

    # The METHOD NAME outranks the header from here on. The two versions
    # have disjoint method vocabularies, and the reference SDK's v1.0 client
    # sends no version header at all — so a caller invoking ``SendMessage``
    # has stated, more reliably than any header could, that a proto-JSON
    # ``SendMessageResponse`` is what its parser awaits. The header is still
    # validated above (a declared-but-unsupported version must refuse, not
    # guess), and still decides the shape of the card route, which has no
    # method name to read.
    resolved = proto.resolve_method(method)
    op: str | None = None
    if resolved is not None:
        op, version = resolved

    exposure = await get_exposure_by_token(db, card_token)
    if exposure is None:
        return JSONResponse(content=proto.not_found_error(req_id))

    client_id, refusal, credential_provider = await _authenticate(
        request, db, exposure
    )
    if client_id is None:
        a2a_metrics.record_refusal(refusal)
        if refusal == "rejected_platform_key":
            # FR-020: audited, because a platform key on this surface is
            # exactly the signal an operator needs to see during migration —
            # an internal integration still presenting its unscoped key where
            # only partner credentials belong. Anchored on the run-as user
            # (NOT NULL); the key itself is never recorded.
            await log_audit(
                db,
                user=audit_actor(exposure.run_as_user_id),
                action="reject",
                entity_type="a2a_exposure",
                entity_id=exposure.id,
                summary=(
                    "Rejected a platform API key presented to the external "
                    "A2A task surface; only partner credentials are accepted"
                ),
                details={"reason": "platform_api_key"},
                request=request,
            )
            await db.commit()
        # The RESPONSE is identical for every refusal — only the audit trail
        # and the metric label know the difference.
        return JSONResponse(
            status_code=401,
            content=proto.jsonrpc_error(
                req_id, proto.JSONRPC_INVALID_REQUEST, "Unauthenticated"
            ),
        )

    # Authenticated: this request belongs to the per-registration budget
    # below, so its pre-auth charge is returned. Without the refund the
    # 60/min per-IP cap silently binds every legitimate call too — Foundry
    # polls ``tasks/get`` at 8-10/s, exhausting it within seconds of a task
    # starting, and no raise of the per-registration limits can help because
    # the smaller bucket fires first.
    from app.core.rate_limit import refund_rate_limit

    await refund_rate_limit(request, "a2a:preauth")

    try:
        await _enforce_a2a_rate_limit(client_id, op)
    except HTTPException as exc:
        # FR-025: a throttled partner gets the machine-readable envelope
        # (RateLimitedError, Retry-After intact), not a bare HTTP detail.
        # Logged, never silent: a throttled submission leaves no run, and a
        # partner reporting refusals must be matchable to a line here. Not
        # audited: a client hammering the limit would flood the audit log.
        a2a_metrics.record_refusal("rate_limited")
        logger.warning(
            "A2A request throttled: client=%s op=%s status=%s retry_after=%s",
            client_id,
            op,
            exc.status_code,
            (exc.headers or {}).get("Retry-After"),
        )
        return _limit_response(req_id, exc)

    agent = (
        await db.execute(select(Agent).where(Agent.id == exposure.agent_id))
    ).scalar_one_or_none()
    if agent is None or not agent.enabled:
        return JSONResponse(content=proto.not_found_error(req_id))

    # ── send: v0.3 message/send · v1.0 SendMessage ────────────────────────
    # The two versions agree on the inbound envelope — the message sits
    # under ``params.message`` with a camelCase ``messageId`` in both — so
    # everything below is version-shared; only the RESPONSE shapes diverge.
    if op == "send":
        message = params.get("message") or {}
        if not isinstance(message, dict):
            return JSONResponse(
                content=proto.jsonrpc_error(
                    req_id, proto.JSONRPC_INVALID_PARAMS, "message must be an object"
                )
            )
        message_id = message.get("messageId") or message.get("message_id")
        parts = message.get("parts") or []
        if not isinstance(parts, list):
            return JSONResponse(
                content=proto.jsonrpc_error(
                    req_id, proto.JSONRPC_INVALID_PARAMS, "parts must be an array"
                )
            )
        if not message_id:
            return JSONResponse(
                content=proto.jsonrpc_error(
                    req_id, proto.JSONRPC_INVALID_PARAMS, "messageId is required"
                )
            )
        message_id = str(message_id)
        # Bounded on the way in, because the column is String(128): a longer
        # client-minted id otherwise fails at flush as a DataError — a 500
        # after the run row exists and the dedup claim is taken.
        if len(message_id) > 128:
            return JSONResponse(
                content=proto.jsonrpc_error(
                    req_id,
                    proto.JSONRPC_INVALID_PARAMS,
                    "messageId must be at most 128 characters",
                )
            )

        # A message naming a TASK is an answer to what that task asked. It
        # continues the same task rather than starting one: the client is
        # replying to a question, and a second task would make one exchange
        # look like two to everything that counts them.
        # Both placements, matching ``contextId`` below. Reading only the
        # message level lets a partner that puts it in ``params`` fall through
        # to ``create_task``: a different task id comes back for what it
        # believed was a follow-up, its quota is spent, and the task it was
        # answering is left asking forever.
        declared_task = (
            params.get("taskId")
            or message.get("taskId")
            or message.get("task_id")
        )
        if declared_task:
            try:
                asking = await task_svc.get_task(
                    db, uuid.UUID(str(declared_task)), client_id
                )
            except (ValueError, TypeError, AttributeError):
                asking = None
            if asking is None:
                # Never issued, or another client's: the uniform not-found
                # every other task lookup gives, so ownership is not probeable.
                a2a_metrics.record_refusal("task_not_found")
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id, proto.TASK_NOT_FOUND, "Task not found"
                    )
                )
            if asking.exposure_id != exposure.id:
                # One registration spans every card it reaches, so the partner
                # owns this task — but a continuation EXECUTES, and this card's
                # agent would run as the other card's run-as user. The exposure
                # is the identity boundary: the answer goes back where the
                # question came from.
                a2a_metrics.record_refusal("task_other_exposure")
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id,
                        proto.UNSUPPORTED_OPERATION,
                        "This task belongs to another agent card; answer it "
                        "there",
                    )
                )
            if asking.caller_question is None and asking.message_id == message_id:
                # This exact answer already landed. Resending a submission
                # returns the task it created rather than making a second, and
                # an answer is a submission — without this the retry reads as
                # "not waiting on an answer", which is true, and true because
                # this answer is what stopped it waiting.
                if proto.blocking_requested(params.get("configuration"), version):
                    await task_svc.wait_until_terminal(db, asking)
                return JSONResponse(
                    content=proto.jsonrpc_result(
                        req_id, await _render_task(db, asking, agent, version)
                    )
                )
            try:
                await task_svc.answer_question(
                    db,
                    task=asking,
                    exposure=exposure,
                    agent=agent,
                    client_id=client_id,
                    provider=credential_provider,
                    message_id=message_id,
                    parts=parts,
                )
            except task_svc.TaskNotAskingError as exc:
                # The client owns it and it is not asking anything — a fact
                # about its own task, so it gets the reason.
                a2a_metrics.record_refusal("task_not_asking")
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id, proto.UNSUPPORTED_OPERATION, str(exc)
                    )
                )
            except task_svc.TaskRejectedError as exc:
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id, proto.JSONRPC_INVALID_PARAMS, str(exc)
                    )
                )
            await db.commit()
            _spawn_task_run(asking.id)
            # An answer is a send like any other, so a partner that asked for a
            # blocking one gets it here too. Without this, answering a question
            # was the one submission that always returned immediately —
            # the inverse of the asymmetry that made a blocking send wait out
            # the cap on ``input-required``.
            if proto.blocking_requested(params.get("configuration"), version):
                await task_svc.wait_until_terminal(db, asking)
            return JSONResponse(
                content=proto.jsonrpc_result(
                    req_id, await _render_task(db, asking, agent, version)
                )
            )

        # A follow-up names a context, and the agent that context records is
        # what answers it — that is transfer's contract, which was
        # unimplementable while ``contextId`` grouped nothing. Three refusals
        # come first, and each returns before any run starts.
        from app.a2a import contexts as ctx_svc

        context = None
        queued = False
        declared_context = params.get("contextId") or message.get("contextId")
        if declared_context:
            context = await ctx_svc.resolve(db, str(declared_context), client_id)
            if context is None:
                # Never issued, or issued to another client: one answer for
                # both, so the existence of a context is not probeable. The
                # same uniform not-found ``tasks/get`` already returns.
                a2a_metrics.record_refusal("context_not_found")
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id, proto.TASK_NOT_FOUND, "Task not found"
                    )
                )

            if context.exposure_id != exposure.id:
                # The card is frozen on the context precisely so a follow-up
                # stays inside the contract the partner validated its request
                # against. Answered here, the context's agent would run under
                # this card's run-as user — an agent this card's owner never
                # consented to, chosen by a partner-supplied identifier.
                a2a_metrics.record_refusal("context_other_exposure")
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id,
                        proto.UNSUPPORTED_OPERATION,
                        "This conversation belongs to another agent card; "
                        "continue it there",
                    )
                )

            responsible = await ctx_svc.responsible_agent_if_reachable(
                db, context, client_id
            )
            if responsible is None:
                # The operator has unbound that agent from this partner since
                # the transfer. Responsibility recorded once is not consent
                # forever, so the follow-up is refused rather than answered by
                # an agent this partner no longer reaches.
                a2a_metrics.record_refusal("context_agent_unreachable")
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id,
                        proto.UNSUPPORTED_OPERATION,
                        "The agent responsible for this conversation is no "
                        "longer available to this client",
                    )
                )

            open_tasks = await ctx_svc.open_task_count(db, context)
            queued = open_tasks > 0
            if open_tasks >= ctx_svc.MAX_OPEN_TASKS_PER_CONTEXT:
                # A busy conversation QUEUES a follow-up (the executor runs it
                # once every task ahead of it is finished, so two never race
                # for who answers); only a full queue refuses, and as the
                # limit it is, with the wait a partner should observe.
                a2a_metrics.record_refusal("context_queue_full")
                retry_after = ctx_svc.CONTEXT_QUEUE_RETRY_AFTER_SECONDS
                limit = ctx_svc.MAX_OPEN_TASKS_PER_CONTEXT
                await _audit_refusal(
                    db,
                    request,
                    exposure=exposure,
                    agent=agent,
                    client_id=client_id,
                    reason="context_queue_full",
                    summary=(
                        f"Refused an A2A submission from client {client_id} "
                        f"to '{agent.name}': {limit} tasks already queued "
                        f"in this conversation"
                    ),
                )
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    content=proto.jsonrpc_error(
                        req_id,
                        proto.RATE_LIMITED,
                        f"Conversation queue limit reached: {limit} tasks are "
                        f"already waiting or running in this context; retry "
                        f"after {retry_after} seconds",
                        details=[
                            {
                                "limit": "context_queue",
                                "max_open_tasks": limit,
                                "retry_after_seconds": retry_after,
                            }
                        ],
                    ),
                )

            # The responsible agent answers, under the card the partner
            # originally addressed — a transfer moved who answers, never the
            # contract the partner validated its request against.
            agent = responsible

        # Quota gate for NEW submissions, FR-036: an already-exhausted
        # allowance surfaces as a structured protocol error at submission
        # time, not as a run that starts and then fails. Checked after the
        # dedup lookup so a retry of an accepted message still returns its
        # existing task, and skipped entirely on a quota-backend outage —
        # fail-open like the platform default, because the executor's own
        # metering (which also catches an exhaustion racing this gate) is
        # the layer that actually charges.
        from app.services.quota import QuotaExceededError, check_agentic_quota

        existing = await task_svc.find_existing_task(
            db, client_id, message_id
        )
        if existing is None:
            try:
                await check_agentic_quota(db, exposure.run_as_user_id)
            except QuotaExceededError as exc:
                a2a_metrics.record_refusal("quota_exhausted")
                await log_audit(
                    db,
                    user=audit_actor(exposure.run_as_user_id, client_id),
                    action="reject",
                    entity_type="a2a_exposure",
                    entity_id=exposure.id,
                    entity_name=agent.name,
                    summary=(
                        f"Refused an A2A submission from client {client_id} "
                        f"to '{agent.name}': usage allowance of the run-as "
                        f"user exhausted"
                    ),
                    details={"client_id": client_id},
                    request=request,
                )
                await db.commit()
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id, proto.QUOTA_EXHAUSTED, str(exc)
                    )
                )
            except Exception:  # quota backend outage: defer to the executor
                logger.warning(
                    "A2A quota pre-check unavailable; deferring to the "
                    "executor's own metering",
                    exc_info=True,
                )

        # Checked BEFORE the task exists. Refusing after ``create_task`` leaves
        # a task and a run that nothing will ever execute — the spawn below is
        # skipped by the early return — and the message id is claimed by then,
        # so the retry dedupes to the same dead task and the partner can never
        # submit that message at all.
        declared_push = proto.declared_push_config(params.get("configuration"))
        if declared_push:
            from app.a2a import push as push_svc

            try:
                await push_svc.validate_declared(declared_push)
            except push_svc.PushConfigRefusedError as exc:
                a2a_metrics.record_refusal("push_config_refused")
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id, proto.JSONRPC_INVALID_PARAMS, str(exc)
                    )
                )

        try:
            task, created = await task_svc.create_task(
                db,
                exposure=exposure,
                agent=agent,
                client_id=client_id,
                provider=credential_provider,
                message_id=message_id,
                parts=parts,
                context=context,
                version=version,
                queued=queued,
            )
        except task_svc.TaskRejectedError as exc:
            task = await task_svc.create_rejected_task(
                db,
                exposure=exposure,
                client_id=client_id,
                message_id=message_id,
                parts=parts,
                reason=exc.reason,
            )
            await db.commit()
            a2a_metrics.record_refusal(exc.reason)
            return JSONResponse(
                content=proto.jsonrpc_error(
                    req_id, proto.CONTENT_TYPE_NOT_SUPPORTED, str(exc)
                )
            )

        if created and declared_push:
            # A partner has no task id to register against until this call
            # returns, so the submission is where it says where to be told.
            # Already validated above; what is left is the per-task count,
            # which a task created moments ago cannot have reached.
            await push_svc.register_declared(
                db, task=task, client_id=client_id, declared=declared_push
            )

        await log_audit(
            db,
            # Anchored on the run-as user, never ``agent.user_id``: the audit
            # column is NOT NULL and a legacy company-managed agent's owner
            # can be NULL.
            user=audit_actor(exposure.run_as_user_id, client_id),
            action="trigger",
            entity_type="a2a_task",
            entity_id=task.id,
            entity_name=agent.name,
            summary=(
                f"External A2A client {client_id} submitted a task to "
                f"'{agent.name}' (runs as {exposure.run_as_user_id})"
            ),
            details={"client_id": client_id, "duplicate": not created},
            request=request,
        )
        a2a_metrics.record_submission(
            agent.is_company_managed, duplicate=not created
        )
        await db.commit()

        if created:
            _spawn_task_run(task.id)

        # A polling partner (Foundry) gets the task back immediately; a
        # blocking one holds this response until the task is terminal or the
        # cap expires. The wait applies to duplicates too: a retry of a
        # blocking send must behave like the send it retries, not answer
        # instantly with a state the caller will treat as final.
        if proto.blocking_requested(params.get("configuration"), version):
            await task_svc.wait_until_terminal(db, task)

        return JSONResponse(
            content=proto.jsonrpc_result(
                req_id,
                proto.send_result(
                    await _render_task(db, task, agent, version), version
                ),
            )
        )

    # ── get: v0.3 tasks/get · v1.0 GetTask ────────────────────────────────
    # ``params.id`` in both versions; the result is a bare Task in both, so
    # unlike send there is no envelope step.
    if op == "get":
        try:
            task = await task_svc.get_task(
                db, uuid.UUID(str(params.get("id"))), client_id
            )
        except (ValueError, TypeError):
            task = None
        if task is None:
            return JSONResponse(content=proto.not_found_error(req_id))
        return JSONResponse(
            content=proto.jsonrpc_result(
                req_id, await _render_task(db, task, agent, version)
            )
        )

    # ── cancel: v0.3 tasks/cancel · v1.0 CancelTask ───────────────────────
    if op == "cancel":
        from app.a2a.client.delegation import spawn_run_cancel

        try:
            task = await task_svc.get_task(
                db, uuid.UUID(str(params.get("id"))), client_id
            )
        except (ValueError, TypeError):
            task = None
        if task is None:
            return JSONResponse(content=proto.not_found_error(req_id))
        # Partners are told after the commit below; ``cascade_cancel`` says why.
        cancelled_runs: list = []
        if not await task_svc.request_cancel(db, task, cancelled_out=cancelled_runs):
            return JSONResponse(
                content=proto.jsonrpc_error(
                    req_id, proto.TASK_NOT_CANCELABLE, "Task is already terminal"
                )
            )
        await db.commit()
        spawn_run_cancel(cancelled_runs)
        # A cancel drives the run terminal itself, so the executor's own
        # announcement is skipped — without this a partner registered for
        # notification is never told the task ended.
        _spawn_announce(task.id)
        return JSONResponse(
            content=proto.jsonrpc_result(
                req_id, await _render_task(db, task, agent, version)
            )
        )

    if op in proto.PUSH_OPERATIONS:
        from app.a2a import push as push_svc

        # Every one of the four addresses a task, and a task this client does
        # not own is not found — the same answer tasks/get gives, so a partner
        # cannot enumerate another's work through this surface.
        # ``id`` is the TASK id on this surface, exactly as it is for
        # ``tasks/get`` and ``tasks/cancel`` above: every one of the four
        # extends TaskIdParams. Reading it as a config id instead made ``list``
        # answer not-found for a partner that spelled it that way, put
        # ``delete`` out of reach entirely — its config id arrives under its
        # own key, so ``id`` was the task id it then lost — and had ``get``
        # look a task id up among configurations.
        config_id = params.get("pushNotificationConfigId")
        declared_config = params.get("pushNotificationConfig")
        if declared_config is not None and not isinstance(declared_config, dict):
            # Partner-supplied and unparsed until here. ``or {}`` rescues a
            # falsy value and nothing else, so a string or a list reaches
            # ``.get`` and raises where a protocol error belongs.
            return JSONResponse(
                content=proto.jsonrpc_error(
                    req_id,
                    proto.JSONRPC_INVALID_PARAMS,
                    "pushNotificationConfig must be an object",
                )
            )
        task_id = (
            params.get("taskId")
            or params.get("id")
            or (declared_config or {}).get("taskId")
        )

        if op == "push_get" and config_id:
            config = await push_svc.get(db, config_id=str(config_id), client_id=client_id)
            if config is None:
                return JSONResponse(content=proto.not_found_error(req_id))
            return JSONResponse(
                content=proto.jsonrpc_result(req_id, push_svc.render(config, version))
            )

        # ``(db, task_id, client_id)``, and the id parsed — the client is a
        # predicate in the query, not the thing being looked up.
        try:
            task = await task_svc.get_task(
                db, uuid.UUID(str(task_id)), client_id
            )
        except (ValueError, TypeError, AttributeError):
            task = None
        if task is None:
            return JSONResponse(content=proto.not_found_error(req_id))

        if op == "push_set":
            try:
                config = await push_svc.register_declared(
                    db,
                    task=task,
                    client_id=client_id,
                    declared=declared_config or {},
                )
            except push_svc.PushConfigRefusedError as exc:
                a2a_metrics.record_refusal("push_config_refused")
                await db.commit()
                return JSONResponse(
                    content=proto.jsonrpc_error(
                        req_id, proto.JSONRPC_INVALID_PARAMS, str(exc)
                    )
                )
            await db.commit()
            return JSONResponse(
                content=proto.jsonrpc_result(req_id, push_svc.render(config, version))
            )

        if op == "push_list":
            configs = await push_svc.list_for_task(db, task=task, client_id=client_id)
            return JSONResponse(
                content=proto.jsonrpc_result(
                    req_id, [push_svc.render(c, version) for c in configs]
                )
            )

        if op == "push_get":
            # Reached only without a config id — the task's own registration,
            # which is what a partner that registered one holds. Answered here
            # rather than left to fall through to the delete tail below.
            configs = await push_svc.list_for_task(db, task=task, client_id=client_id)
            if not configs:
                return JSONResponse(content=proto.not_found_error(req_id))
            return JSONResponse(
                content=proto.jsonrpc_result(
                    req_id, push_svc.render(configs[0], version)
                )
            )

        # push_delete
        config = await push_svc.get(db, config_id=str(config_id or ""), client_id=client_id)
        if config is None:
            return JSONResponse(content=proto.not_found_error(req_id))
        await push_svc.delete(db, config=config)
        await db.commit()
        return JSONResponse(content=proto.jsonrpc_result(req_id, {}))

    return JSONResponse(
        content=proto.jsonrpc_error(
            req_id, proto.UNSUPPORTED_OPERATION, f"Unsupported method: {method}"
        )
    )


#: Strong references to in-flight background runs. ``asyncio`` keeps only weak
#: references to tasks, so a bare ``create_task`` can be collected mid-run.
_BACKGROUND_RUNS: set = set()


def _spawn_task_run(task_id) -> None:
    """Execute a task's run detached from the request that created it.

    Kept referenced: asyncio holds only weak references, so a bare
    ``create_task`` can be collected mid-run.

    Counted as a background turn for the shutdown drain: outside any request,
    uvicorn's drain never cancels it, so a deployment killed the process
    under it with no write. Its supervisor died with it, the lease went stale,
    and the partner read "abandoned, execution lease expired" a full lease
    later. Cancelled by the drain, the executor's ``CancelledError`` branch
    fails the task at once with the platform-restart reason the partner can
    act on (``platform_interruption``).
    """
    import asyncio

    from app.a2a import tasks as task_svc
    from app.core.shutdown_finalizers import track_background_turn

    _bg = asyncio.create_task(task_svc.execute_task_run(task_id))
    _BACKGROUND_RUNS.add(_bg)
    _bg.add_done_callback(_BACKGROUND_RUNS.discard)
    track_background_turn(_bg)


def _spawn_announce(task_id) -> None:
    """Publish a task's state detached from the request that ended it.

    A callback is an accelerator whose floor is the partner polling, so its
    delivery has no business on the response path: the per-callback timeout
    multiplies by however many the task registered, and a partner's own dead
    endpoint would hold its own response for the product.
    """
    import asyncio

    from app.a2a import tasks as task_svc

    _bg = asyncio.create_task(task_svc.announce_state_by_id(task_id))
    _BACKGROUND_RUNS.add(_bg)
    _bg.add_done_callback(_BACKGROUND_RUNS.discard)


async def _render_task(db, task, agent, version: str) -> dict:
    """Project a stored task into the wire shape for *version*."""
    import uuid as _uuid

    from app.a2a import tasks as task_svc

    state = await task_svc.resolve_state(db, task)
    # Per-version spelling: v0.3 takes ``A2AState``'s own values, v1.0's
    # proto-JSON parser accepts only the proto enum names — see the
    # ``A2AState`` docstring for why the SDK's parsers, not its enum types,
    # settle this.
    status: dict = {"state": proto.state_wire(state, version)}
    if state is proto.A2AState.FAILED:
        # A failed task carries its stored reason as a status message, so a
        # partner can tell quota exhaustion from a crash — FR-036 requires at
        # least that distinction to be machine-visible.
        detail = await task_svc.failure_detail(db, task)
        if detail:
            status["message"] = proto.status_message(
                detail, version, message_id=str(_uuid.uuid4())
            )
    elif state is proto.A2AState.INPUT_REQUIRED and task.caller_question:
        # The question IS the status message — where a client reads what is
        # being asked of it. The task's own answer, if it produced a partial
        # one before asking, is not sent: it would read as a result.
        status["message"] = proto.status_message(
            task.caller_question, version, message_id=str(_uuid.uuid4())
        )
    artifacts = (
        await task_svc.build_artifacts(db, task, agent, version)
        if state is proto.A2AState.COMPLETED
        else []
    )
    rendered: dict = {
        "id": str(task.id),
        "contextId": str(task.context_id),
        "status": status,
        "artifacts": artifacts,
    }
    if proto.is_legacy(version):
        # v0.3's ``message/send`` result is a Task-or-Message union that
        # strict parsers (Foundry) discriminate by a REQUIRED ``kind``. The
        # reference python SDK defaults it, which is how its round-trip tests
        # pass without one — the wire still has to carry it.
        rendered["kind"] = "task"
    if artifacts:
        # The completed result is ALSO mirrored into ``history`` and
        # ``status.message`` as an agent Message. Artifacts are where the
        # spec puts the output, but Foundry does not read them: a completed
        # task whose answer lived only in artifacts reached its user as "no
        # answer returned". Mirrored for BOTH versions, not just the one
        # Foundry speaks: where a v1.0 client reads the result from is
        # exactly as unverified as Foundry's behaviour was, and the proto
        # Task carries both fields. The SAME message object backs all three
        # locations, so nothing is exposed twice that was not already
        # exposed once, and the copies cannot drift.
        result_message = proto.agent_message(
            [part for a in artifacts for part in a.get("parts", [])],
            version,
            message_id=str(_uuid.uuid4()),
            task_id=str(task.id),
            context_id=str(task.context_id),
        )
        rendered["history"] = [result_message]
        status["message"] = result_message
    return rendered
