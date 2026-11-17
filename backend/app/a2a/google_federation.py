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

"""Google federated partner authentication (inbound A2A).

The Google-side counterpart of :mod:`app.a2a.federation`, and it exists for
the same reason that one does: a partner that cannot re-run an OAuth exchange
against a third-party token URL ends up pasting a short-lived bearer into a
static header, and the integration dies when it expires. A Vertex / ADK agent
already holds a Google identity that Google refreshes forever. Accepting the
token minted for THAT identity means no secret is stored on either side and
nothing ever needs re-pasting.

Trust model — two pins decide everything, and the split of work between them
is NOT the same as the Entra module's:

- **audience**: the exact ``aud`` the partner's tokens carry. A Google ID
  token's audience is chosen by whoever mints it, so this proves nothing on
  its own; it is defence in depth, so a token the partner minted for some
  other service cannot be replayed here.
- **caller**: the ONE Google identity allowed to call — a service account
  address (``…@….iam.gserviceaccount.com``) or the numeric subject Google
  assigns it. **This pin carries the whole trust decision.**

That last point is the one real difference from Entra federation and the
reason it is stated this loudly. Entra's JWKS URL is built from the pinned
tenant, so only that directory's keys can ever verify a token here. Google
publishes ONE keyset for every principal on the planet, so a valid signature
proves only "some Google identity minted this". Nothing but the caller pin
separates the partner from any other Google account, which is why
:func:`parse_google_pins` refuses a configuration that lacks it — an
optional caller pin would be an open door with a green checkmark.

The pins live in an **integrations row** (provider
:data:`a2a.providers.GOOGLE_A2A_PROVIDER` with ``direction='inbound'``), and
an agent opts in through the SAME mechanism every other integration uses:
``Agent.integration_bindings`` (the agent configuration's Tools tab). The
provider contributes no tool an operator selects, so binding it to an agent
means very nearly one thing: "this agent's exposure admits this partner's
Google tokens". The exception is the one every inbound A2A binding carries,
``ask_the_caller``, which the external-task executor registers for the length
of a partner's task. The pins
are re-read and re-validated on every request — there is no cached acceptance
to wait out.

Verification is fail-closed and silent: every failure — unreachable JWKS, bad
signature, expired, wrong audience, wrong issuer, wrong caller, a binding
whose integration was deleted or edited into an invalid state — returns
``None``, and the task surface folds that into its uniform refusal. The
specific cause is logged here at WARNING for the operator debugging a
partner's setup, exactly as the Entra module does.

**One JWKS resolution per request, not one per bound config.** Because the
keyset URL is a constant rather than derived from a pin, the signature can be
checked ONCE and the result compared against each bound config. That is why
there is no ``narrow_by_claimed_*`` helper here: its Entra counterpart exists
only to stop an unauthenticated caller forcing one outbound fetch per bound
tenant, and with a fixed URL that cost does not exist.
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

#: Where Google publishes the keys that sign OIDC ID tokens. A CONSTANT, never
#: derived from the token: there is nothing in a Google token that could point
#: verification at a different keyset, which removes by construction the
#: attack the Entra module has to guard against by building its URL from the
#: stored tenant.
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

#: The two spellings Google uses for its own issuer. Which one a token carries
#: depends on how it was minted, not on anything the caller controls, so
#: refusing either form would break correct setups.
_GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

#: Google signs ID tokens with RS256 and nothing else. A fixed singleton list
#: is also the alg-confusion guard: a token whose header names any other
#: algorithm (``HS256`` keyed on the public key, ``none``) is refused by PyJWT
#: before its signature is even considered.
_GOOGLE_ALGORITHMS = ["RS256"]

#: Bound the auth path on a cold or broken JWKS cache. A fetch happens about
#: once an hour for the whole deployment; a partner request should never hang
#: 30 seconds on Google being unreachable when the answer is a refusal.
_JWKS_FETCH_TIMEOUT_SECONDS = 5

#: How many bound configs one agent's auth attempt will consider. Far cheaper
#: than the Entra cap (no per-config network call), but bounded for the same
#: reason: a list read from JSON should not decide how much work a request
#: does. Bindings beyond the cap are ignored WITH a warning — silently
#: honouring part of a config would read as "partner randomly refused".
_MAX_BOUND_CONFIGS = 8

#: An audience is an identifier — a URL, a URN, an app id — never free prose.
_AUDIENCE_MAX_LENGTH = 200

#: A service account address is bounded by Google's own limits well below
#: this; the cap only stops a pasted document becoming a pin.
_CALLER_MAX_LENGTH = 320


def is_google_issuer(issuer: object) -> bool:
    """Whether *issuer* is one of Google's own ``iss`` spellings.

    Exported so the A2A auth path can route a token to this mechanism by its
    UNVERIFIED issuer without reaching into a private constant. Answering
    "which federation should check this" is all it is ever used for — nothing
    here is a trust decision, and :func:`verify_google_token` re-checks the
    issuer against the same set after the signature holds.
    """
    return issuer in _GOOGLE_ISSUERS


def google_jwks_url() -> str:
    """Where the signing keys are fetched from.

    A function rather than a bare constant so the ``google_a2a`` connectivity
    probe tests exactly the fetch the auth path performs — a green test that
    reached a different URL would prove nothing.
    """
    return _GOOGLE_JWKS_URL


@dataclass(frozen=True)
class GooglePins:
    """The validated facts one inbound Google config pins.

    ``integration_id`` rides along because the caller that verified a token
    must know WHICH configuration admitted it: that integration is the
    partner's identity on the task surface
    (:func:`providers.partner_client_id`), and several may be bound to one
    agent. ``None`` only for pins parsed straight from a config dict (the
    save-time / probe path), where no row is in play.
    """

    audience: str
    caller: str
    integration_id: uuid.UUID | None = None


class GooglePinsError(ValueError):
    """A Google federation config that must not be trusted, with the reason.

    Raised by :func:`parse_google_pins` so the integration endpoints can
    surface the exact problem as a 422; the auth path catches it into the
    uniform refusal instead.
    """


def _caller_shape_problem(caller: str) -> str | None:
    """A caller pin no Google token could ever carry, or ``None``.

    Two shapes are legal, because two claims can carry the identity: the
    service account's address (``email``) and the numeric subject Google
    assigns it (``sub``). An operator reads whichever one their console
    shows, so both are accepted rather than forcing them to know which claim
    their tokens present.
    """
    if caller.isdigit():
        return None
    local, _, domain = caller.partition("@")
    if local and "." in domain and " " not in caller:
        return None
    return (
        f"The allowed caller {caller!r} is neither a service account address "
        "(name@project.iam.gserviceaccount.com) nor a numeric subject id — "
        "no Google token can carry it. Copy the exact value from the "
        "partner's service account."
    )


def parse_google_pins(extra_config: dict[str, Any] | None) -> GooglePins:
    """Validate an integration's ``extra_config`` into :class:`GooglePins`.

    The single definition of "configured": the integration endpoints call
    this to refuse an unusable config up front, and :func:`load_google_pins`
    calls it per request so an integration edited into an invalid state stops
    admitting tokens immediately — the two can never disagree about what a
    usable config is. Raises :class:`GooglePinsError` on any problem.

    Both pins are REQUIRED, and the caller one especially: see this module's
    docstring for why a config without it would admit any Google identity.
    """
    config = extra_config or {}

    audience = str(config.get("audience") or "").strip()
    if not audience:
        raise GooglePinsError("The integration is missing its audience.")
    if len(audience) > _AUDIENCE_MAX_LENGTH:
        raise GooglePinsError("The integration's audience is implausibly long.")

    caller = str(config.get("service_account") or "").strip()
    if not caller:
        raise GooglePinsError(
            "The integration is missing its allowed caller (service account). "
            "Google signs tokens for every identity with one keyset, so "
            "without this pin any Google account could call."
        )
    if len(caller) > _CALLER_MAX_LENGTH:
        raise GooglePinsError("The integration's allowed caller is implausibly long.")
    shape_problem = _caller_shape_problem(caller)
    if shape_problem:
        raise GooglePinsError(shape_problem)

    return GooglePins(audience=audience, caller=caller)


async def load_google_pins(
    db: AsyncSession, integration_id: uuid.UUID
) -> GooglePins | None:
    """Resolve one bound integration into its pins, for the auth path.

    Fail-closed and silent: a deleted row, a row of some other provider, an
    undecryptable or invalid config — each returns ``None`` (with the cause at
    WARNING) so the task surface keeps its uniform refusal. The provider check
    matters here: the agent's ``integration_bindings`` key is data, and a
    stale or mismatched entry under the ``google_a2a`` key must never turn
    some other integration's config into a trust anchor.

    The DIRECTION check matters for the same reason, and this is where the
    two federated mechanisms differ in exposure: an OUTBOUND Google
    registration stores a service account key of OURS, whose JSON carries a
    ``client_email``. Nothing here would read that as a pin — the field names
    do not overlap — but an outbound row has no business admitting callers at
    all, so it is refused on the direction rather than on the accident that
    its fields happen not to parse.
    """
    from sqlalchemy import select

    from app.models.a2a import DIRECTION_INBOUND
    from app.models.integration import Integration
    from app.services.vault import reveal_json_field

    integration = (
        await db.execute(
            select(Integration).where(Integration.id == integration_id)
        )
    ).scalar_one_or_none()
    if integration is None or integration.provider != providers.GOOGLE_A2A_PROVIDER:
        logger.warning(
            "A2A Google federation binding %s does not resolve to a %s "
            "integration; skipping",
            integration_id,
            providers.GOOGLE_A2A_PROVIDER,
        )
        return None

    settings_row = await providers.get_settings(db, integration_id)
    if settings_row is None or settings_row.direction != DIRECTION_INBOUND:
        logger.warning(
            "A2A Google federation binding %s is not an inbound registration; "
            "skipping",
            integration_id,
        )
        return None

    try:
        extra = await reveal_json_field(integration, "extra_config_enc")
        parsed = parse_google_pins(extra if isinstance(extra, dict) else None)
        return replace(parsed, integration_id=integration.id)
    except GooglePinsError as exc:
        logger.warning(
            "A2A Google federation integration %s is not usable: %s",
            integration_id,
            exc,
        )
        return None
    except Exception:
        logger.warning(
            "A2A Google federation integration %s has an unreadable config; "
            "refusing",
            integration_id,
        )
        return None


async def load_agent_google_pins(
    db: AsyncSession, agent_id: uuid.UUID
) -> list[GooglePins]:
    """Resolve every usable inbound Google config bound to *agent_id*.

    Reads the agent's ordinary ``integration_bindings`` under the
    ``google_a2a`` key — the Tools tab is the binding surface, there is no
    A2A-specific one — and returns the pins of each binding that survives
    :func:`load_google_pins`. An empty list means the agent never opted in, or
    every binding is outbound, stale or invalid; either way the caller
    refuses, and the difference is only visible in this module's WARNINGs.
    """
    from sqlalchemy import select

    from app.models.agent import Agent

    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if agent is None:
        return []
    ids = providers.bound_integration_ids(agent, providers.GOOGLE_A2A_PROVIDER)
    if len(ids) > _MAX_BOUND_CONFIGS:
        logger.warning(
            "Agent %s binds %d %s configs; only the first %d are honoured",
            agent_id,
            len(ids),
            providers.GOOGLE_A2A_PROVIDER,
            _MAX_BOUND_CONFIGS,
        )
        ids = ids[:_MAX_BOUND_CONFIGS]

    pins: list[GooglePins] = []
    for integration_id in ids:
        resolved = await load_google_pins(db, integration_id)
        if resolved is not None:
            pins.append(resolved)
    return pins


def _audiences(payload: dict[str, Any]) -> set[str]:
    """Every audience the token claims. ``aud`` may be a string or a list.

    Returned as a set so the pin comparison is a membership test rather than
    a shape-dependent branch. A token with no ``aud`` yields the empty set,
    which no pin can match — the refusal a missing audience deserves.
    """
    raw = payload.get("aud")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, (list, tuple)):
        return {str(a) for a in raw}
    return set()


def _matches_caller(payload: dict[str, Any], caller: str) -> bool:
    """Whether the verified claims name the pinned caller.

    Two claims can carry it, matching the two shapes
    :func:`_caller_shape_problem` accepts:

    * ``sub`` — the numeric subject, compared exactly;
    * ``email`` — the service account address, compared case-insensitively
      and ONLY when ``email_verified`` is true. Google always sets that flag
      on a service account's token, so requiring it costs a correct setup
      nothing while refusing the one shape where an address means less than
      it looks like it does.
    """
    if str(payload.get("sub") or "") == caller:
        return True
    email = str(payload.get("email") or "").strip().lower()
    if not email or email != caller.lower():
        return False
    return bool(payload.get("email_verified"))


async def verify_google_token(
    token: str, pins: list[GooglePins]
) -> tuple[dict[str, Any], GooglePins] | None:
    """Verify a Google-issued bearer against the configs bound to one agent.

    Returns ``(claims, the pins that admitted it)``, or ``None`` on any
    failure. Never raises: this runs inside ``_authenticate``, whose contract
    is a uniform refusal, so an exception escaping here would turn "bad
    credential" into a 500 that tells a prober something worked differently.

    Takes the whole LIST rather than one config at a time, which is the shape
    the constant keyset makes possible and the Entra module cannot use: the
    signature is checked once and the surviving claims are then compared
    against each pin. An agent binding five Google partners therefore costs
    one signature verification, not five.
    """
    if not pins:
        return None

    try:
        signing_key = await resolve_signing_key(
            google_jwks_url(),
            token,
            timeout=_JWKS_FETCH_TIMEOUT_SECONDS,
        )
    except (PyJWKClientError, jwt.PyJWTError) as exc:
        # PyJWKClient decodes the token header before it does any JWKS work,
        # so a structurally malformed bearer arrives here as a PyJWTError
        # (``DecodeError``), not a PyJWKClientError. Both must fold into the
        # uniform refusal, or "bad credential" becomes a 500.
        logger.warning("A2A Google token rejected: signing key not resolved: %s", exc)
        return None

    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=_GOOGLE_ALGORITHMS,
            # Audience is compared here rather than by PyJWT, because ONE
            # decode has to serve every bound config and they pin different
            # audiences. The check below is exact string membership — the
            # same test PyJWT would apply, minus the ability to pass only one
            # expected value per decode.
            leeway=JWT_CLOCK_SKEW_LEEWAY,
            options={"verify_exp": True, "verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        logger.warning("A2A Google token rejected: %s", exc)
        return None

    if payload.get("iss") not in _GOOGLE_ISSUERS:
        logger.warning(
            "A2A Google token rejected: issuer %r is not Google",
            payload.get("iss"),
        )
        return None

    presented = _audiences(payload)
    for candidate in pins:
        if candidate.audience not in presented:
            continue
        if not _matches_caller(payload, candidate.caller):
            continue
        return payload, candidate

    # One line naming both presented facts, because neither is diagnosable
    # from a console: the audience is chosen by the partner's minting code,
    # and which claim carries the caller depends on how the token was
    # requested. Safe to log — the signature and issuer are already verified,
    # so these came from a token Google genuinely signed.
    logger.warning(
        "A2A Google token rejected: no bound configuration pins aud=%r with "
        "caller sub=%r / email=%r — align the integration's audience and "
        "allowed caller with what the partner's tokens present",
        sorted(presented),
        payload.get("sub"),
        payload.get("email"),
    )
    return None
