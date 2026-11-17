# SPDX-License-Identifier: Apache-2.0

"""Shared FastAPI dependencies – authentication & authorization."""

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import NoReturn

import jwt
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    check_password_policy,
    decode_access_token,
    is_confined_token,
    is_expired_session_token,
)
from app.core.security_options import security_option
from app.core.sessions import (
    REASON_LIFETIME,
    SessionEndedError,
    SessionEndReason,
    session_ended_detail,
    verify_session,
)
from app.core.utils import (
    client_address,
    client_address_is_forwarded,
    format_exception_message,
    forwarded_proto_claim,
    is_secure_transport,
)
from app.llm.provider_access import bind_acting_user
from app.models.user import User
from app.services.agentic_gate import refuse_ai_disabled, refuse_ai_disabled_for_group
from app.services.api_key_access import (
    ApiKeySurface,
    enforce_rest_scope,
    looks_like_api_key,
    principal_of,
    resolve_api_key,
)

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer()

# Stable prefix of the Terms-of-Use rejection ``detail``. The frontend sniffs
# it in ``lib/api.ts`` to tell this 403 apart from every other one, exactly as
# it already does for "Enterprise license required". Changing the wording is a
# contract change — update both sides together.
TERMS_OF_USE_REQUIRED_DETAIL = (
    "Terms of Use acceptance required. Accept the current Terms of Use to "
    "continue using this platform."
)
#: The machine ``code`` beside it, for clients that branch rather than sniff.
TERMS_OF_USE_REQUIRED_CODE = "terms_required"
#: The chat gate's licence refusal, ``code`` beside :func:`chat_unavailable_detail`.
CHAT_UNAVAILABLE_CODE = "chat_unavailable"


class CodedHTTPException(HTTPException):
    """An HTTP error with a machine ``code`` beside its string ``detail``.

    ``{"detail", "code"}``, rendered by ``main.py``, for refusals whose string
    ``detail`` clients already read.
    """

    def __init__(
        self, status_code: int, detail: str, *, code: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


#: The guarded surfaces are reachable before any rate limit, so without this
#: an unauthenticated caller sets the log volume. Keyed on the outcome too:
#: ``CREDENTIAL_TRANSPORT_STRICT`` changes at runtime from Settings > Access and security,
#: and a route served before it is switched on and refused after says both.
_reported_paths: set[tuple[str, str]] = set()

#: The whole client-visible refusal. The remediation is operator guidance and
#: goes to the log; an anonymous caller learns nothing about the deployment.
INSECURE_TRANSPORT_DETAIL = "https required"


def _report_unconfirmed_transport(request: Request, outcome: str) -> None:
    """Log, once per route per process, a request whose transport is unconfirmed."""
    # The route template, not the concrete URL: a parameterised route keyed on
    # the URL would hand a caller an unbounded set to grow.
    route = request.scope.get("route")
    key = getattr(route, "path", None) or request.url.path
    if (key, outcome) in _reported_paths:
        return
    _reported_paths.add((key, outcome))
    # ``peer`` is scope["client"] as uvicorn left it: the socket peer unless a
    # trusted proxy rewrote it from X-Forwarded-For, which only a request
    # carrying that header can have undergone. With ``x_forwarded_proto=https``
    # (a claim uvicorn did not apply, so in practice a peer it does not trust)
    # ``peer`` is the untrusted TLS-terminating proxy; with
    # ``x_forwarded_for=present`` and no claim it may be the client a trusted
    # proxy named, and is no address to trust. Read like every client address
    # (``client_address``): under FORWARDED_ALLOW_IPS="*" uvicorn copies the
    # header into it unchecked.
    logger.warning(
        "%s %s over unconfirmed transport (scheme=%s, peer=%s, x_forwarded_proto=%s, "
        "x_forwarded_for=%s); behind a TLS-terminating proxy, set FORWARDED_ALLOW_IPS to "
        "the proxy's address and have it send X-Forwarded-Proto",
        key,
        outcome,
        request.url.scheme,
        client_address(request.client.host) if request.client else None,
        forwarded_proto_claim(request) or "none",
        "present" if client_address_is_forwarded(request) else "absent",
    )


async def require_secure_transport(request: Request) -> None:
    """Refuse a route reached over a transport we cannot confirm encrypted.

    The A2A surface: its card is an unsigned trust anchor whose token
    travels in the URL, and its task and callback routes carry bearer
    credentials, so encrypted transport is the guarantee a partner relies on.
    The end-to-end suite's test-only peers are admitted here, and by the
    credential routes only under ``CREDENTIAL_TRANSPORT_STRICT``.
    """
    if is_secure_transport(request, allow_test_peers=True):
        return
    _report_unconfirmed_transport(request, "refused")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=INSECURE_TRANSPORT_DETAIL)


def refuses_credential_transport(request: Request) -> bool:
    """Whether a credential-issuing route must refuse this request.

    ``POST /runners/enroll`` returns a durable ``run-`` credential exactly once,
    ``POST /mcp/oauth/register`` a confidential client's ``client_secret``
    and ``POST /mcp/oauth/token`` issues bearers, to clients the server
    cannot hold to HTTPS itself - a runner binary or an MCP client has no HSTS.

    Off by default: the request is served and a transport it cannot confirm
    encrypted is only reported, once per route, exactly as before the switch
    existed. ``CREDENTIAL_TRANSPORT_STRICT`` (also in Settings > Access and security)
    turns that into the A2A surface's refusal, with its exemptions. Called
    before the route reads its body or mints anything.
    """
    if not security_option("credential_transport_strict"):
        if not is_secure_transport(request):
            _report_unconfirmed_transport(request, "served")
        return False
    if is_secure_transport(request, allow_test_peers=True):
        return False
    _report_unconfirmed_transport(request, "refused")
    return True


def refuse_insecure_credential_transport() -> NoReturn:
    """The terse ``403 https required`` of runner enrollment."""
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=INSECURE_TRANSPORT_DETAIL)


def credential_transport_route(refusal: Callable[[], Response]) -> type[APIRoute]:
    """A route class answering *refusal* when :func:`refuses_credential_transport` says so.

    A route class, not a dependency: FastAPI reads and decodes a declared body
    before it resolves any dependency, so a dependency would answer after a
    malformed body's 422. The check wraps FastAPI's handler and runs before
    the body is read. *refusal* returns the response or raises the
    ``HTTPException`` the app's handlers answer.
    """

    class CredentialTransportRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
            handler = super().get_route_handler()

            async def transport_first(request: Request) -> Response:
                if refuses_credential_transport(request):
                    return refusal()
                return await handler(request)

            return transport_first

    return CredentialTransportRoute


def validate_password(password: str) -> None:
    """Enforce the platform password policy, raising HTTP 400 on violation."""
    try:
        check_password_policy(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _active_platform_user(user: User) -> User:
    """Return *user*, refusing a deactivated account as the local-JWT branch does.

    The platform vouches for the email, not for the account's standing here: an
    account deactivated in XTM One can still be active on the platform that
    signed the token. Share grants survive deactivation only because a disabled
    account cannot authenticate, so this branch must refuse it too.
    """
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )
    return user


def _tag_issuing_platform(user: User, payload) -> User:
    """Tag *user* with the registered platform that signed its token.

    Transient, never persisted - the same trick as ``auth_credential_kind`` - so
    a route can tell WHICH registered platform is acting for the operator, not
    merely that one is: its issuer URL and its registration identifier, the
    identity that outlives a change of that URL.
    """
    user.platform_issuer = payload.iss
    user.platform_identifier = getattr(payload, "platform_identifier", None)
    return user


async def _read_memory_tombstone(user_id: uuid.UUID) -> str | None:
    """The deletion the memory tombstone of platform account *user_id* names,
    or None. Never raises: a tombstone that cannot be read is not lifted."""
    from app.core.redis import redis_client
    from app.core.utils import format_exception_message
    from app.services.memory_tombstones import account_tombstone, buried_by

    try:
        return await buried_by(await redis_client(), account_tombstone(str(user_id)))
    except Exception as exc:
        logger.warning(
            "Could not read the memory tombstone of platform account %s: %s",
            user_id,
            format_exception_message(exc),
        )
        return None


async def _lift_memory_tombstone(user_id: uuid.UUID, deletion: str) -> None:
    """A platform account re-created under the deterministic id of a deleted
    one starts clean: the deletion's memory tombstone would otherwise undo
    every memory write of the new account until it expires. Never raises."""
    from app.core.redis import redis_client
    from app.core.utils import format_exception_message
    from app.services.memory_tombstones import account_tombstone, lift

    try:
        await lift(await redis_client(), account_tombstone(str(user_id)), deletion)
    except Exception as exc:
        logger.warning(
            "Could not lift the memory tombstone of platform account %s: %s",
            user_id,
            format_exception_message(exc),
        )


async def _resolve_trusted_platform_jwt(token: str, db: AsyncSession) -> User | None:
    """Try to resolve a JWT issued by a trusted Filigran platform.

    Verifies the JWT signature using Ed25519 (EdDSA) against the issuer's
    JWKS, fetched at ``/xtm/auth/jwks`` on the URL this platform reaches the
    issuer on.  The issuer must be an active platform registration.

    Returns ``None`` if the JWT is not from a trusted platform issuer
    (i.e. it's likely a local Copilot JWT or API key instead), and raises
    401 when it resolves to a deactivated account.
    """
    from app.core.xtm_auth import verify_xtm_jwt

    # Quick pre-check: only attempt cross-platform verification for JWTs
    # that look like they have an EdDSA header.  This avoids unnecessary
    # DB queries for regular HS256 Copilot tokens.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None

    if header.get("alg") != "EdDSA":
        return None

    payload = await verify_xtm_jwt(token, db)
    if payload is None:
        return None

    # Resolve the user by email claim
    email = payload.email
    if not email:
        logger.warning("XTM JWT from '%s' missing email claim", payload.iss)
        return None

    # Case-insensitive DB lookup: lowercasing the input alone isn't enough
    # because legacy / SSO-provisioned rows may store mixed-case emails.
    # ``func.lower(User.email) == lowered_input`` matches regardless of how
    # the DB row was stored, aligning with the symmetric guarantee at the
    # local-auth registration sites (``register``,
    # ``register-with-invitation``, ``users.create_user``,
    # ``invitations.create_invitation``).  Note: the lowercase ``email`` we
    # pass to ``User(...)`` below preserves the lowercase-on-write
    # invariant for newly auto-created accounts.
    normalized_email = email.strip().lower()
    result = await db.execute(
        select(User).where(func.lower(User.email) == normalized_email)
    )
    user = result.scalar_one_or_none()

    if user:
        return _tag_issuing_platform(_active_platform_user(user), payload)

    # User doesn't exist — auto-create with minimal permissions.
    # A real DB row is required because conversations store user_id as a FK.
    from app.core.sso_helpers import get_sso_config

    cfg = await get_sso_config(db)
    sso_provider: str | None = None
    if not cfg.local_auth_enabled:
        if cfg.saml_enabled:
            sso_provider = "saml"
        elif cfg.oidc_enabled:
            sso_provider = "oidc"
        else:
            sso_provider = "sso"

    # INSERT ... ON CONFLICT DO NOTHING + re-SELECT (same idiom as
    # ``get_or_create_intent`` in app/api/v1/helpers.py): the
    # id is deterministic from (iss, email), so two concurrent callers for
    # the same not-yet-provisioned account both reach this branch and would
    # otherwise both INSERT the identical row. The loser's INSERT is a
    # no-op instead of raising ``UniqueViolationError`` on ``users_pkey``.
    # No ``index_elements`` — this absorbs a conflict on either unique
    # constraint on ``users`` (``id`` or ``email``): two *different* trusted
    # issuers auto-provisioning the same email concurrently compute
    # different uuid5 ids (the seed includes ``iss``), so their INSERTs
    # collide on ``email`` rather than ``id``. The re-SELECT below must
    # therefore key on the same normalized email used for the initial
    # lookup above (not on ``new_user_id``, which the losing caller's own
    # row may never have under that constraint) so it finds the winner's
    # row under either conflict shape.
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    new_user_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"platform:{payload.iss}:{normalized_email}",
    )
    # Read before the INSERT: a deletion of the row it creates can only bury
    # a tombstone once that row is committed, so this names an earlier deletion.
    deletion = await _read_memory_tombstone(new_user_id)
    inserted = await db.execute(
        pg_insert(User)
        .values(
            id=new_user_id,
            email=normalized_email,
            full_name=email.split("@")[0],
            role="user",
            is_active=True,
            sso_provider=sso_provider,
        )
        .on_conflict_do_nothing()
    )
    await db.commit()
    if deletion is not None and getattr(inserted, "rowcount", 0) == 1:
        await _lift_memory_tombstone(new_user_id, deletion)
    result = await db.execute(
        select(User).where(func.lower(User.email) == normalized_email)
    )
    return _tag_issuing_platform(_active_platform_user(result.scalar_one()), payload)


async def get_current_user_ungated(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]  # FastAPI injects it; direct callers may omit it
) -> User:
    """Authenticate via JWT, ``fcp-`` API key, or trusted platform JWT.

    This is authentication ONLY — it deliberately skips the Terms of Use
    acceptance gate. Depend on it directly from the few endpoints that must
    stay reachable while a user's acceptance is pending (``GET /auth/me`` and
    the ``/auth/terms*`` routes); everything else should depend on
    :func:`get_current_user`, which layers the gate on top.

    Each branch tags the resolved user with the transient
    ``auth_credential_kind`` attribute (never persisted, same trick as
    ``impersonated_by`` below) so the gate can tell an interactive session
    apart from a machine credential.

    A personal API key is resolved for the REST surface and held to its scopes
    on this request (``services/api_key_access.py``).
    """
    user = await _authenticate_bearer(credentials.credentials, db, request)
    # The AI work of this request is for that person, the calls that carry no
    # user of their own included: their LLM provider exclusions hold.
    bind_acting_user(user.id)
    return user


async def _authenticate_bearer(token: str, db: AsyncSession, request: Request | None) -> User:
    """The user *token* authenticates, by its kind (see :func:`get_current_user_ungated`)."""
    if looks_like_api_key(token):
        api_key_user = await resolve_api_key(token, db, surface=ApiKeySurface.REST, request=request)
        principal = principal_of(api_key_user)
        if principal is not None:
            await enforce_rest_scope(principal, request, db)
        return api_key_user

    # Trusted platform JWT (e.g. OpenCTI) — checked before local JWT
    # because both start with "ey" but use different signing keys.
    trusted_user = await _resolve_trusted_platform_jwt(token, db)
    if trusted_user is not None:
        trusted_user.auth_credential_kind = "platform_jwt"
        return trusted_user

    return await resolve_session_jwt(token, db)


def session_ended_error(reason: SessionEndReason) -> HTTPException:
    """The 401 of a session past its absolute lifetime or idle too long.

    A structured ``detail`` (``code: session_expired``, the reason) so the web
    app tells it from every other 401: it signs out and says why.
    """
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=session_ended_detail(reason))


async def resolve_session_jwt(token: str, db: AsyncSession) -> User:
    """The user a local session JWT authenticates, or a 401.

    The one set of session rules: the REST dependency above and every surface
    that must read its token itself (the activity stream, which an
    ``EventSource`` opens without an ``Authorization`` header) call this, so a
    token one of them refuses is refused by all. That includes the
    absolute lifetime and the idle timeout (``core/sessions.py``): the
    accepted session is tagged on the user as ``web_session``.
    """
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except jwt.ExpiredSignatureError as exc:
        if is_expired_session_token(token):
            raise session_ended_error(REASON_LIFETIME) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc

    # Confined tokens are NOT sessions. The MFA challenge token
    # (``scope``) and the resource tokens the authorization server mints
    # (``type``: MCP OAuth, A2A partner) are signed with the same secret and
    # carry the same ``sub``, so without this check any of them would
    # authenticate against every endpoint in the API. The shared predicate
    # rejects on the PRESENCE of either marker claim — a token carrying
    # ``"scope": ""`` (or any falsy value) is rejected too, and any future
    # confined token kind is closed by default.
    if is_confined_token(payload):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )

    try:
        web_session = await verify_session(payload, token)
    except SessionEndedError as exc:
        raise session_ended_error(exc.reason) from exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )
    # Reject tokens issued before the user's last password change, so changing
    # or resetting a password invalidates older sessions.
    if user.password_changed_at is not None:
        iat = payload.get("iat")
        if iat is None or iat < int(user.password_changed_at.timestamp()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired, please sign in again",
            )
    if payload.get("impersonated_by") is not None:
        user.impersonated_by = payload["impersonated_by"]
    user.auth_credential_kind = "session_jwt"
    user.web_session = web_session
    return user


# Process-local positive cache for the platform-wide Terms of Use acceptance.
# Once this process has SEEN the platform accept the currently
# published version, that fact can never become false again for the process
# lifetime: the accept endpoint only ever advances the acceptance, and the
# published version comes from env config, fixed at process start. Caching it
# removes the per-request singleton SELECT from every authenticated call on an
# already-accepted platform — the same "the common case costs nothing"
# property the per-user gate had. Never caches a negative: a pending platform
# re-reads on every request so an acceptance on ANY replica is picked up by
# every other replica on its next request.
_TOU_ACCEPTED_VERSION_CACHE: str | None = None


def reset_terms_of_use_cache() -> None:
    """Forget the cached platform acceptance (test isolation seam)."""
    global _TOU_ACCEPTED_VERSION_CACHE
    _TOU_ACCEPTED_VERSION_CACHE = None


async def platform_tou_accepted_version(db: AsyncSession) -> str | None:
    """The ToU version the platform has accepted (``None`` = never).

    Read-only (``peek_platform_settings`` — never creates the singleton) with
    the positive-result cache described on ``_TOU_ACCEPTED_VERSION_CACHE``.
    Read by the REST gate below.
    """
    global _TOU_ACCEPTED_VERSION_CACHE
    current = settings.terms_of_use_current_version
    if current and current == _TOU_ACCEPTED_VERSION_CACHE:
        return current

    from app.api.v1.platform_settings_helpers import peek_platform_settings

    ps = await peek_platform_settings(db)
    accepted = ps.tou_accepted_version if ps is not None else None
    if current and accepted == current:
        _TOU_ACCEPTED_VERSION_CACHE = current
    return accepted


async def enforce_terms_of_use(db: AsyncSession, user: User) -> None:
    """Reject requests until the platform has accepted the current ToU.

    Terms of Use are accepted **once for the whole platform** (by an admin, on
    behalf of the organisation), not per-user. The accepted version lives on the
    ``PlatformSettings`` singleton; this gate rejects every interactive session
    until that platform acceptance matches the current ToU version. A ToU
    version bump therefore re-opens the gate for everyone until an admin
    re-accepts once.

    Ordered so the common case is cheap: the config/attribute checks short-
    circuit first; the singleton lookup settles the common "already accepted"
    case and disappears entirely once the acceptance has been confirmed once
    (see ``platform_tou_accepted_version``); the license lookup is reached
    only while the platform is actually pending.

    Which credentials are gated
    ---------------------------
    Only interactive **session JWTs** (local login and SSO both mint one via
    ``create_access_token``). ``fcp-`` API keys, MCP keys, webhooks, channel
    bots and cross-platform JWTs from OpenCTI/OpenAEV are machine or
    embedded-edge credentials with nowhere to render a consent screen —
    gating them would turn a routine ToU version bump into an automation
    outage across every customer's assignments.

    Exempt accounts
    ---------------
    Users whose email is listed in ``TERMS_OF_USE_EXEMPT_EMAILS`` (the vendor
    accounts operating a SaaS instance) are never gated; the platform stays
    unaccepted for everyone else until a customer administrator accepts.

    Scope
    -----
    The gate only applies where Agentic AI is licensed
    (``is_agentic_ai_licensed``) — Community-Edition XTM One is never gated.
    Deliberately the licence, not the resolved AI state: this branch *skips* the
    gate, so switching AI off must not stop asking for acceptance.
    """
    if not settings.terms_of_use_active:
        return
    if getattr(user, "auth_credential_kind", None) != "session_jwt":
        return
    # Vendor operator accounts (``TERMS_OF_USE_EXEMPT_EMAILS``) work on the
    # platform without accepting the terms on the customer's behalf.
    if settings.terms_of_use_exempt(getattr(user, "email", None)):
        return

    accepted_version = await platform_tou_accepted_version(db)
    if accepted_version == settings.terms_of_use_current_version:
        return

    # The *licence* question, as this function's own docstring says. Reading the
    # resolved AI state here would skip the whole gate whenever an administrator
    # switched AI off, so nobody is asked to accept the terms.
    from app.api.v1.platform import is_agentic_ai_licensed

    if not await is_agentic_ai_licensed(db):
        return

    logger.info(
        "Request rejected (403): Terms of Use acceptance pending platform-wide "
        "(accepted=%r, current=%r); requested by user %s.",
        accepted_version,
        settings.terms_of_use_current_version,
        user.id,
    )
    raise CodedHTTPException(
        status.HTTP_403_FORBIDDEN,
        TERMS_OF_USE_REQUIRED_DETAIL,
        code=TERMS_OF_USE_REQUIRED_CODE,
    )


async def get_current_user(
    user: User = Depends(get_current_user_ungated),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Authenticate, then enforce Terms of Use acceptance.

    The default dependency for every authenticated endpoint. Because
    ``require_admin`` and the rest of the API chain through it, the gate needs
    no per-router wiring and no path allow-list: the handful of exempt
    endpoints opt out explicitly by depending on
    :func:`get_current_user_ungated` instead.
    """
    await enforce_terms_of_use(db, user)
    # A companion session locked behind its PIN answers nothing else (423):
    # the lock's own routes depend on ``get_current_user_ungated``.
    from app.services.app_lock import enforce as enforce_app_lock

    await enforce_app_lock(db, user)
    return user


async def get_current_user_unlocked(
    user: User = Depends(get_current_user_ungated),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Authenticate and enforce the companion's PIN lock, without the Terms of Use gate.

    For the routes the Terms of Use gate leaves open (the profile that carries
    the terms state, accepting them, handing the session to the Desktop app):
    a locked phone neither reads who it belongs to nor validates anything.
    """
    from app.services.app_lock import enforce as enforce_app_lock

    await enforce_app_lock(db, user)
    return user


async def get_current_runner(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    """Authenticate a native Runner daemon by its ``run-`` bearer credential.

    Used by the runner-facing transport endpoints (poll, result upload,
    heartbeat). Returns the :class:`~app.models.runner.Runner` row. The
    WebSocket path can't use ``HTTPBearer`` (no ``Authorization`` header on the
    JS/native WS handshake in every client), so it resolves the token from the
    query string via :func:`app.services.runners.resolve_runner_credential`
    directly — this dependency is HTTP-only.
    """
    from app.services.runners import resolve_runner_credential

    runner = await resolve_runner_credential(credentials.credentials, db)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked runner credential",
        )
    return runner


async def get_current_relay(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    """Authenticate an XTM One Relay by its ``rly-`` bearer credential.

    HTTP only (long poll, frames, heartbeat); the WebSocket resolves the token
    itself through :func:`app.services.relays.resolve_relay_credential`.
    """
    from app.services.relays import resolve_relay_credential

    relay = await resolve_relay_credential(credentials.credentials, db)
    if relay is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked relay credential",
        )
    return relay


async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Like ``get_current_user`` but rejects non-admin users with 403."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


#: The sentence the AI-disabled refusal carries.
AI_DISABLED_DETAIL = "AI is disabled on this deployment."

#: What a person is told when the AI service cannot answer for now: a provider
#: outage in chat, or a channel direct message while whether agentic AI may
#: run could not be read.
AI_TEMPORARILY_UNAVAILABLE_DETAIL = (
    "The AI service is temporarily unavailable. Please try again in a few moments."
)


async def refuse_if_group_ai_disabled(db: AsyncSession) -> None:
    """Refuse the person this request acts for when a group of theirs has AI off.

    The one request-side reader of ``Group.ai_disabled``, asked by the three AI
    gates right after the platform switch. The person is the one
    ``get_current_user_ungated`` bound for the request (``bind_acting_user``),
    so a gate declared AFTER the user dependency sees them and a public route,
    which authenticates nobody, sees none and is left to the platform switch;
    ``tests/test_group_ai_gate_order.py`` pins the order on every route. A
    caller with no database work to do - nobody bound, or no group switching AI
    off anywhere - costs nothing.
    """
    from app.llm.provider_access import acting_user_id
    from app.services.group_ai_access import member_of_disabled_group

    person = acting_user_id()
    if person is not None and await member_of_disabled_group(db, person):
        refuse_ai_disabled_for_group()


async def require_ai_enabled(db: AsyncSession = Depends(get_db)) -> None:
    """Refuse when AI is switched off. Says nothing about licensing.

    For execution endpoints that already have whatever licence gate they need
    and were missing only the AI question — triggering an assignment, syncing a
    knowledge-base source, running an evaluation. ``require_agentic_ai`` would
    also impose the Agentic-AI licence on routes that never required it.

    Disabling a button is not a gate (FR-018 is about affordances, FR-001 about
    the promise): a direct API call, an API token or another client reaches the
    endpoint regardless, and every one of these starts real model or embedding
    traffic.
    """
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        refuse_ai_disabled()
    await refuse_if_group_ai_disabled(db)


# The one sentence every licence refusal carries in XTM One mode. Names the two
# ways to activate Enterprise Edition (connect an EE platform, or install an
# Filigran license) and the two Filigran paths (trial, sales) so channel
# adapters and the chat UI can surface it verbatim.
ENTERPRISE_EDITION_REQUIRED_DETAIL = (
    "Enterprise Edition required. AI is not activated on this Community "
    "Edition deployment: connect an Enterprise Edition OpenCTI, OpenAEV or "
    "OpenCRQ platform, or install a Filigran license. Request a trial at "
    "https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ or "
    "contact sales at https://filigran.io/contact/"
)

# Its Copilot-mode counterpart. Copilot has no edition and no platform to
# connect - the license is the one way to activate it - so the XTM One
# sentence would send an unlicensed Copilot caller (the MCP agent endpoints
# and A2A sit outside the ``/api`` licence-wall middleware) down a path that
# changes nothing. Same trial and sales paths.
LICENSE_REQUIRED_DETAIL = (
    "A valid Filigran license is required. AI is not licensed on this "
    "deployment: install a Filigran license (paste the PEM in Settings or set "
    "ENTERPRISE_LICENSE). Request a trial at "
    "https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ or "
    "contact sales at https://filigran.io/contact/"
)

# The chat gate's own sentence: the same remedy, led by what the caller was
# trying to do - channel adapters surface this verbatim to a person who typed
# a message, so it must say "chat" before it says "license".
CHAT_UNAVAILABLE_PREFIX = "Chat is unavailable on this deployment. "
CHAT_UNAVAILABLE_ENTERPRISE_DETAIL = CHAT_UNAVAILABLE_PREFIX + ENTERPRISE_EDITION_REQUIRED_DETAIL


def licence_required_detail() -> str:
    """The sentence a licence refusal carries, for the mode this platform runs.

    Every licence gate raises through here (or :func:`chat_unavailable_detail`)
    so the remedy named is one that exists: Enterprise Edition's two activation
    paths in XTM One mode, the license alone in Copilot mode.
    """
    if settings.is_copilot_like:
        return LICENSE_REQUIRED_DETAIL
    return ENTERPRISE_EDITION_REQUIRED_DETAIL


def chat_unavailable_detail() -> str:
    """:func:`licence_required_detail`, led by what the caller was doing."""
    return CHAT_UNAVAILABLE_PREFIX + licence_required_detail()


async def refuse_chat_unavailable(db: AsyncSession) -> NoReturn:
    """The chat gate's licence refusal: ``chat_unavailable``, the reason logged."""
    # Name the reason so a 403 is diagnosable from the logs alone. The
    # gate is otherwise silent — only a bare access-log line with status
    # 403 would appear, hiding *why* chat is blocked (Community Edition:
    # no Filigran license and no EE-validated connected platform). Resolve
    # the edition for the log; fall back to "unknown" if that lookup
    # itself fails so the rejection is still recorded.
    from app.api.v1.platform import get_deployment_edition

    try:
        edition = await get_deployment_edition(db)
    except Exception:  # pragma: no cover - diagnostic best-effort
        edition = "unknown"
    logger.warning(
        "Chat request rejected (403): license gate closed "
        "[edition=%s]. Enterprise Edition (a Filigran license, or a "
        "connected Enterprise Edition platform) is required to enable "
        "agentic chat.",
        edition or "copilot",
    )
    raise CodedHTTPException(
        status.HTTP_403_FORBIDDEN,
        chat_unavailable_detail(),
        code=CHAT_UNAVAILABLE_CODE,
    )


async def require_agentic_ai_licensed(db: AsyncSession = Depends(get_db)) -> None:
    """The licence question only — for surfaces that must survive AI being off.

    Deterministic flows, their triggers, integration syncs and custom variables
    are promised to keep working with AI disabled (FR-014, ``UPGRADING.md``), so
    the endpoints that create, edit, activate, cancel and run them cannot sit
    behind :func:`require_agentic_ai`: that refuses on the resolved AI state and
    would 403 a schedule→HTTP→notify flow, and stop an operator cancelling a run
    already in flight.

    Whether a *particular* flow may run is answered per flow instead — by
    ``flow_triggers.flow_requires_ai`` on the automatic paths, and inline in
    ``run_flow`` for a manual one.
    """
    from app.api.v1.platform import is_agentic_ai_licensed

    if not await is_agentic_ai_licensed(db):
        logger.warning(
            "Request rejected (403): Enterprise Edition not active. "
            "This deployment runs Community Edition (no Filigran license and no "
            "Enterprise Edition platform connected; in Copilot mode, no valid "
            "license). See GET /api/v1/platform/config for the current state."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=licence_required_detail(),
        )


async def require_agentic_ai(db: AsyncSession = Depends(get_db)) -> None:
    """Reject writes when AI is disabled, or the deployment is not licensed.

    AI-disabled is checked first: it is deterministic and its answer is the more
    actionable one — "an admin switched this off here" rather than "buy a
    licence".
    """
    from app.api.v1.platform import is_agentic_ai_enabled
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        refuse_ai_disabled()
    await refuse_if_group_ai_disabled(db)

    if not await is_agentic_ai_enabled(db):
        logger.warning(
            "Request rejected (403): Enterprise Edition not active. "
            "This deployment runs Community Edition (no Filigran license and no "
            "Enterprise Edition platform connected; in Copilot mode, no valid "
            "license). See GET /api/v1/platform/config for the current state."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=licence_required_detail(),
        )


async def require_chat_enabled(db: AsyncSession = Depends(get_db)) -> None:
    """Reject chat API calls when chat is not available for this deployment.

    In XTM One this blocks Community Edition (no Filigran license, no EE
    platform) so users can't side-step the splash by hitting the API
    directly. The shipping behaviour is *navigable but chat-write
    gated*: the rest of the app (settings, configuration, existing
    chat history, dashboards) stays reachable for read-only
    exploration, and only chat *write* paths return 403 — composer /
    new-conversation in the UI plus every chat entry-point on the
    backend (REST ``POST /conversations`` & ``/messages``, the web
    chat's ``POST /web/chat/messages``, OpenAI-compatible LLM proxy, MCP agent
    endpoints, public widget). The gate applies to admins too because
    chat itself is the licensed surface — provisioning admin pages
    are never wired through this dependency in the first place.

    In Copilot mode this mirrors the existing license gate.

    Response shape
    --------------
    The 403 carries a ``detail`` string that names the upgrade paths
    (trial, contact sales) and embeds the canonical Filigran URLs so
    channel adapters and the chat UI can surface the message verbatim
    in a tooltip. The original spec mentioned "403 +
    Retry-After tooltip-friendly message"; we deliberately do **not**
    set a literal ``Retry-After`` header because the gate is
    license-based, not time-based — there is no fixed interval after
    which chat becomes available again, only an admin-driven license
    activation event. Surfacing a misleading retry duration would
    invite clients to back off and silently retry forever; the
    structured ``detail`` body and the `chat_enabled` flag in
    ``GET /api/v1/platform/config`` are the contract instead.
    """
    from app.api.v1.platform import is_chat_enabled
    from app.services.ai_state import is_ai_enabled

    # Same ordering as require_agentic_ai, and the same reason.
    if not await is_ai_enabled(db):
        refuse_ai_disabled()
    await refuse_if_group_ai_disabled(db)

    if not await is_chat_enabled(db):
        await refuse_chat_unavailable(db)


class AfterCommit:
    """Work a write route hands off until it has committed and returned.

    ``get_db`` commits on dependency teardown, after the response is sent, so a
    write route commits before it answers. A side effect that must follow that
    commit - a cache drop above all: run before it, a concurrent read re-caches
    the old state for the cache's whole lifetime - is added here where the route
    decides on it, and :func:`run_after_commit` runs it once the route has
    returned, before the response is sent. Register it only on a path that
    commits and returns: a route that raises skips it.
    """

    def __init__(self) -> None:
        self._actions: list[tuple[Callable[..., Awaitable[object]], tuple[object, ...]]] = []

    def add(self, action: Callable[..., Awaitable[object]], *args: object) -> None:
        self._actions.append((action, args))

    async def run(self) -> None:
        """Await every action in order; one that fails is logged, never raised."""
        actions, self._actions = self._actions, []
        for action, args in actions:
            try:
                await action(*args)
            except Exception as exc:
                logger.warning(
                    "After-commit action %s failed: %s",
                    getattr(action, "__qualname__", repr(action)),
                    format_exception_message(exc),
                )


async def run_after_commit() -> AsyncIterator[AfterCommit]:
    """The route's :class:`AfterCommit`, run once the route has returned.

    Always declared function-scoped - ``Depends(run_after_commit,
    scope="function")``: that exit runs after the route returned and before the
    response is sent, where the default request scope would run it after the
    send. The exception a raising route throws in at ``yield`` skips the run.
    """
    after_commit = AfterCommit()
    yield after_commit
    await after_commit.run()
