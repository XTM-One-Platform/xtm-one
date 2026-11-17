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

"""Microsoft Entra federated partner authentication (inbound A2A).

Why this exists: neither target platform can drive the card's OAuth
client-credentials flow against a third-party token URL, so in practice a
partner pastes a minted 60-minute JWT into a static header and the
integration dies every hour. Azure Foundry's "Microsoft Entra" connection
avoids that entirely — Azure mints a token for the caller's own identity and
refreshes it forever. Accepting that token here gives the partner automatic
credential refresh with no static secret stored on either side.

Trust model — three facts decide everything:

- **tenant**: whose directory signed the token. The JWKS URL is built from
  this stored value and NEVER from the token's ``iss``, so a crafted token
  cannot point verification at an attacker-controlled keyset, and the JWKS
  client cache stays bounded by configured tenants.
- **audience**: the ``aud`` the token must carry — the Entra app
  registration that represents this platform to the partner's tenant.
- **client**: the Entra application (client) id of the one identity allowed
  to call. Entra will happily sign a token for any principal in the tenant
  that can request the audience; this pin is what narrows "the tenant" to
  "the partner".

The three pins live in an **integrations row** (provider
:data:`a2a.providers.ENTRA_A2A_PROVIDER`, configured on the Integrations page
beside the Microsoft Entra ID tool integration), and an agent opts in through the
SAME mechanism every other integration uses: ``Agent.integration_bindings`` (the
agent configuration's Tools tab). There is deliberately no A2A-specific
binding surface — the provider contributes no tool an operator selects, so
binding it to an agent means very nearly one thing, "this agent's exposure
admits this partner's Entra tokens". The exception is the one every inbound
A2A binding carries, ``ask_the_caller``, which the external-task executor
registers for the length of a partner's task. One partner relationship is therefore configured once and
bound to any number of agents, several partners may be bound to one agent,
and re-pinning after the partner's caller identity churns is one
integration edit rather than a per-agent sweep. The pins are re-read and
re-validated on every request — there is no cached acceptance to wait out.

Binding authority is agent-edit authority (the Tools tab), the same
``writable_agent_error`` gate that authorizes exposing the agent at all.
Resolution here runs with no user and anchors trust on the row's provider,
not on anyone's visibility: the run-as user being unable to *see* the
integration must not switch a partner's credentials off.

Verification is fail-closed and silent: every failure — unreachable JWKS,
bad signature, expired, wrong audience, wrong issuer, wrong caller, a
binding whose integration was deleted or edited into an invalid state —
returns ``None``, and the task surface folds that into its uniform
refusal. The specific cause is logged here at WARNING for the operator
debugging a partner's setup, mirroring ``verify_xtm_jwt``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import jwt
from jwt import PyJWKClientError

from app.a2a import providers
from app.core.jwks import resolve_signing_key
from app.core.security import JWT_CLOCK_SKEW_LEEWAY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Audiences every app in a tenant can mint tokens for. Pinning the caller
#: still holds, but a token the partner minted for an unrelated Graph call
#: must not double as a credential here — refuse the configuration rather
#: than trusting every operator to know this Entra subtlety. Enforced both
#: when a config is bound (422, so the operator learns early) and on every
#: authentication (fail closed, so a later edit cannot sneak one in).
SHARED_AUDIENCES = frozenset(
    {
        "https://graph.microsoft.com",
        "00000003-0000-0000-c000-000000000000",
        "https://management.azure.com",
        "https://management.core.windows.net",
    }
)

#: Keys for BOTH token versions a tenant can mint are published here — the
#: v1.0 discovery endpoint serves the same keyset, so one URL suffices.
_ENTRA_JWKS_URL = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"

#: The two spellings of "issued by this tenant". Which one a token carries is
#: chosen by the *resource app's* ``accessTokenAcceptedVersion``, i.e. by
#: how the customer configured their app registration — not by the caller —
#: so refusing either form would break setups that are correct on the Azure
#: side. Both encode the tenant, and only these two exact strings (built
#: from the STORED tenant) are accepted.
_ENTRA_ISSUERS = (
    "https://login.microsoftonline.com/{tenant}/v2.0",
    "https://sts.windows.net/{tenant}/",
)

#: Entra signs access tokens with RS256 and nothing else. A fixed singleton
#: list is also the alg-confusion guard: a token whose header names any
#: other algorithm (``HS256`` keyed on the public key, ``none``) is refused
#: by PyJWT before its signature is even considered.
_ENTRA_ALGORITHMS = ["RS256"]

#: Bound the auth path on a cold or broken JWKS cache. A fetch happens about
#: once an hour per tenant; a partner request should never hang 30 seconds
#: on Microsoft being unreachable when the answer is going to be a refusal.
_JWKS_FETCH_TIMEOUT_SECONDS = 5

def tenant_jwks_url(tenant_id: uuid.UUID | str) -> str:
    """The discovery URL for *tenant_id*'s signing keys.

    The ONE spelling of where trust is fetched from, shared by the verifier
    and the ``entra_a2a`` connectivity probe — the probe must test exactly
    the fetch the auth path will perform, or a green test proves nothing.
    """
    return _ENTRA_JWKS_URL.format(tenant=tenant_id)


#: The claims that can name the calling application, across token versions:
#: ``appid`` is v1.0's application id, ``azp`` is v2.0's, ``oid``/``sub``
#: identify the service principal (what an operator most easily reads off a
#: managed identity's portal page). Matching any of them against the pinned
#: id keeps the admin's job "paste the GUID Azure shows you" rather than
#: "know which claim your token version uses".
_CALLER_CLAIMS = ("azp", "appid", "oid", "sub")


async def verify_entra_token(
    token: str,
    *,
    tenant_id: uuid.UUID,
    audience: str,
    allowed_client: uuid.UUID,
) -> dict[str, Any] | None:
    """Verify an Entra-issued bearer against one exposure's pinned facts.

    Returns the verified claims, or ``None`` on any failure. Never raises:
    this runs inside ``_authenticate``, whose contract is a uniform refusal,
    so an exception escaping here would turn "bad credential" into a 500
    that tells a prober something worked differently.
    """
    tenant = str(tenant_id)

    try:
        signing_key = await resolve_signing_key(
            tenant_jwks_url(tenant),
            token,
            timeout=_JWKS_FETCH_TIMEOUT_SECONDS,
        )
    except (PyJWKClientError, jwt.PyJWTError) as exc:
        # PyJWKClient decodes the token's header before it does any JWKS work,
        # so a structurally malformed bearer arrives here as a PyJWTError
        # (``DecodeError``), not a PyJWKClientError. Both must fold into the
        # uniform refusal, or "bad credential" becomes a 500.
        logger.warning("A2A Entra token rejected: signing key not resolved: %s", exc)
        return None

    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=_ENTRA_ALGORITHMS,
            audience=audience,
            leeway=JWT_CLOCK_SKEW_LEEWAY,
            options={"verify_exp": True, "verify_aud": True},
        )
    except jwt.InvalidAudienceError:
        # The one mismatch neither side can see from a portal, and the one no
        # connectivity test can catch (proving an audience needs a credential
        # in the partner's tenant, which this design deliberately never
        # stores) — so THIS log line is where a wrong audience pin gets
        # diagnosed. Naming the presented claim is safe: PyJWT verifies the
        # signature before validating claims, so the ``aud`` below comes from
        # a token genuinely signed by the pinned tenant.
        presented = jwt.decode(token, options={"verify_signature": False}).get(
            "aud"
        )
        logger.warning(
            "A2A Entra token rejected: audience mismatch — the token carries "
            "aud=%r, the pinned audience is %r; align the federation "
            "integration's audience with what the partner's tokens present",
            presented,
            audience,
        )
        return None
    except jwt.PyJWTError as exc:
        logger.warning("A2A Entra token rejected: %s", exc)
        return None

    # Issuer is compared by hand against the two exact tenant-derived
    # spellings rather than through PyJWT's ``issuer=`` parameter, whose
    # support for multiple values varies by version — this check must not
    # silently weaken on a dependency bump.
    if payload.get("iss") not in {i.format(tenant=tenant) for i in _ENTRA_ISSUERS}:
        logger.warning(
            "A2A Entra token rejected: issuer %r is not tenant %s",
            payload.get("iss"),
            tenant,
        )
        return None

    # Entra GUID claims are lowercase, but the comparison normalizes anyway:
    # a pin that fails on letter case would be indistinguishable from a real
    # refusal in the one place an operator can't see either value.
    pinned = str(allowed_client).lower()
    presented = {str(payload.get(c, "")).lower() for c in _CALLER_CLAIMS}
    if pinned not in presented:
        logger.warning(
            "A2A Entra token rejected: caller %s is not the pinned client",
            payload.get("azp") or payload.get("appid") or payload.get("oid"),
        )
        return None

    return payload


@dataclass(frozen=True)
class EntraPins:
    """The three validated facts one federation integration pins.

    ``integration_id`` rides along because the caller that verified a token
    must know WHICH configuration admitted it: that integration is the
    partner's identity on the task surface (``providers.partner_client_id``),
    and several may be bound to one agent. ``None`` only for pins parsed
    straight from a config dict (the save-time / probe path), where no row is
    in play.
    """

    tenant_id: uuid.UUID
    client_id: uuid.UUID
    audience: str
    integration_id: uuid.UUID | None = None


class EntraPinsError(ValueError):
    """A federation config that must not be trusted, with the reason.

    Raised by :func:`parse_entra_pins` so the binding endpoint can surface
    the exact problem as a 422; the auth path catches it into the uniform
    refusal instead.
    """


#: Mirrors the old per-exposure column bound; an audience is an identifier
#: (Application ID URI or GUID), never free prose.
_AUDIENCE_MAX_LENGTH = 200


def parse_entra_pins(extra_config: dict[str, Any] | None) -> EntraPins:
    """Validate an integration's ``extra_config`` into :class:`EntraPins`.

    The single definition of "configured": the binding endpoint calls this
    to refuse an unusable integration up front, and ``load_entra_pins``
    calls it per request so an integration edited into an invalid state
    stops admitting tokens immediately — the two can never disagree on what
    a usable config is. Raises :class:`EntraPinsError` on any problem.
    """
    config = extra_config or {}

    def _guid(field: str, label: str) -> uuid.UUID:
        raw = str(config.get(field) or "").strip()
        if not raw:
            raise EntraPinsError(f"The integration is missing its {label}.")
        try:
            return uuid.UUID(raw)
        except ValueError:
            raise EntraPinsError(
                f"The integration's {label} is not a GUID."
            ) from None

    tenant_id = _guid("tenant_id", "partner tenant (directory) ID")
    client_id = _guid("client_id", "allowed caller (client) ID")

    audience = str(config.get("audience") or "").strip()
    if not audience:
        raise EntraPinsError("The integration is missing its audience.")
    if len(audience) > _AUDIENCE_MAX_LENGTH:
        raise EntraPinsError("The integration's audience is implausibly long.")
    if audience.rstrip("/").lower() in SHARED_AUDIENCES:
        raise EntraPinsError(
            "This audience is shared by every application in the tenant; "
            "use a dedicated app registration representing this platform."
        )

    return EntraPins(tenant_id=tenant_id, client_id=client_id, audience=audience)


#: How many bound federation configs one agent's auth attempt will consider.
#: Each candidate can cost a JWKS fetch on a bad token, so the list is
#: bounded rather than trusted to stay small; real setups bind one to three
#: partners. Bindings beyond the cap are ignored WITH a warning — silently
#: honouring only part of a config would read as "partner randomly refused".
_MAX_BOUND_CONFIGS = 8


async def load_entra_pins(
    db: AsyncSession, integration_id: uuid.UUID
) -> EntraPins | None:
    """Resolve one bound integration into its pins, for the auth path.

    Fail-closed and silent, like :func:`verify_entra_token`: a deleted row,
    a row of some other provider, an undecryptable or invalid config — each
    returns ``None`` (with the cause at WARNING) so the task surface keeps
    its uniform refusal. The provider check matters here: the agent's
    ``integration_bindings`` key is data, and a stale or mismatched entry
    under the ``entra_a2a`` key must never turn some other integration's
    config into a trust anchor.
    """
    from sqlalchemy import select

    from app.models.integration import Integration
    from app.services.vault import reveal_json_field

    integration = (
        await db.execute(
            select(Integration).where(Integration.id == integration_id)
        )
    ).scalar_one_or_none()
    if integration is None or integration.provider != providers.ENTRA_A2A_PROVIDER:
        logger.warning(
            "A2A Entra federation binding %s does not resolve to an %s "
            "integration; skipping",
            integration_id,
            providers.ENTRA_A2A_PROVIDER,
        )
        return None

    try:
        extra = await reveal_json_field(integration, "extra_config_enc")
        parsed = parse_entra_pins(extra if isinstance(extra, dict) else None)
        return replace(parsed, integration_id=integration.id)
    except EntraPinsError as exc:
        logger.warning(
            "A2A Entra federation integration %s is not usable: %s",
            integration_id,
            exc,
        )
        return None
    except Exception:
        logger.warning(
            "A2A Entra federation integration %s has an unreadable config; "
            "refusing",
            integration_id,
        )
        return None


def narrow_by_claimed_tenant(token: str, pins: list[EntraPins]) -> list[EntraPins]:
    """Order-preserving narrowing of *pins* by the token's UNVERIFIED ``tid``.

    Purely a candidate filter, never a trust decision. Every fact that admits a
    token still comes from the stored pin: the JWKS URL is built from the
    stored tenant, the issuer is compared against that same stored tenant, and
    the audience and caller are pinned. Reading an unverified claim to decide
    WHICH pinned tenant to try cannot widen any of that — the worst a forged
    ``tid`` achieves is having its token checked against fewer configs than it
    would otherwise have been, all of which would have refused it.

    It exists because verification is expensive on the way in.
    :func:`verify_entra_token` resolves a signing key before it can reject
    anything, and an unknown ``kid`` makes ``PyJWKClient`` refetch the keyset
    even though it is cached — so a garbage bearer against an agent binding
    several partners costs one outbound fetch per config. The card token that
    addresses this surface is not a secret (the partner is given it in a URL),
    so that cost is reachable by anyone. Narrowing makes the honest case
    exactly one resolution.

    Falls back to the full list when the claim is absent, unreadable, or names
    a tenant nothing pins — never fewer candidates than would have verified.
    The adversarial case therefore still reaches the cap, and it is the
    unauthenticated per-IP rate limit that bounds it; this only stops honest
    traffic paying for that ceiling.
    """
    if len(pins) < 2:
        return pins
    try:
        claimed = jwt.decode(token, options={"verify_signature": False}).get("tid")
    except jwt.PyJWTError:
        return pins
    if not claimed:
        return pins
    claimed = str(claimed).strip().lower()
    return [p for p in pins if str(p.tenant_id).lower() == claimed] or pins


async def load_agent_entra_pins(
    db: AsyncSession, agent_id: uuid.UUID
) -> list[EntraPins]:
    """Resolve every usable federation config bound to *agent_id*.

    Reads the agent's ordinary ``integration_bindings`` under the
    ``entra_a2a`` key — the Tools tab is the binding surface, there is no
    A2A-specific one — and returns the pins of each binding that survives
    :func:`load_entra_pins`. An empty list means the agent never opted in,
    or every binding has gone stale or invalid; either way the caller
    refuses, and the difference is only visible in this module's WARNINGs.
    """
    from sqlalchemy import select

    from app.models.agent import Agent

    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if agent is None:
        return []
    ids = providers.bound_integration_ids(agent, providers.ENTRA_A2A_PROVIDER)
    if len(ids) > _MAX_BOUND_CONFIGS:
        logger.warning(
            "Agent %s binds %d %s configs; only the first %d are honoured",
            agent_id,
            len(ids),
            providers.ENTRA_A2A_PROVIDER,
            _MAX_BOUND_CONFIGS,
        )
        ids = ids[:_MAX_BOUND_CONFIGS]

    pins: list[EntraPins] = []
    for integration_id in ids:
        resolved = await load_entra_pins(db, integration_id)
        if resolved is not None:
            pins.append(resolved)
    return pins
