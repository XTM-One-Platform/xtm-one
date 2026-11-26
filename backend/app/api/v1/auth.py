# SPDX-License-Identifier: Apache-2.0

"""Auth endpoints – local register/login, SSO info, /me."""

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_user,
    get_current_user_ungated,
    get_current_user_unlocked,
    validate_password,
)
from app.api.v1.helpers import get_user_group_ids
from app.core.config import settings
from app.core.database import get_db
from app.core.feature_flags import is_feature_enabled, resolve_enabled_feature_flags
from app.core.rate_limit import enforce_rate_limit
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password_async,
    verify_password_async,
)
from app.core.sessions import WebSession, record_activity
from app.core.utils import format_exception_message
from app.models.user import User
from app.services import ctem_access, token_store
from app.services.audit import log_audit
from app.services.group_ai_access import disabling_group_names
from app.services.group_managers import managed_groups
from app.services.platform_access_notices import registered_destination
from app.services.relays import relays_active

if TYPE_CHECKING:
    from app.services.mfa import MfaChallenge

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str


class RegisterWithInvitationRequest(BaseModel):
    token: str
    password: str
    full_name: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class ValidatePasswordResetResponse(BaseModel):
    valid: bool
    email: str | None = None
    sso_only: bool = False


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    # OAuth token type label, not a secret.


class RegisterWithInvitationResponse(TokenResponse):
    #: Where the web app sends the person once registered: the path a registered
    #: platform invited them to, on its public URL; ``None`` to stay in XTM One.
    redirect_url: str | None = None


class LoginResponse(BaseModel):
    """Result of ``POST /auth/login``.

    Two shapes behind one model, distinguished by ``mfa_required``:

    * MFA off — ``access_token`` is set and the caller is signed in. Byte-for-byte
      what this endpoint returned before 2FA existed, so every existing client,
      script and e2e test keeps working untouched.
    * MFA on — ``access_token`` is ``None`` and ``mfa_token`` carries a
      short-lived, ``scope: "mfa"`` challenge that is ONLY accepted by
      ``POST /auth/mfa/verify``. Nothing usable as a session is handed out until
      the second factor is proven.
    """

    access_token: str | None = None
    token_type: str = "bearer"  # noqa: S105
    mfa_required: bool = False
    mfa_token: str | None = None


class SessionStatusResponse(BaseModel):
    """When the caller's web session ends (``core/sessions.py``).

    ``kind`` is ``other`` for a credential that is not a web session (an API
    key, a platform token): neither deadline applies to it. ``server_time``
    lets the web app time its warning and its check on the server's clock.
    """

    kind: str
    server_time: datetime
    #: When the absolute lifetime ends.
    expires_at: datetime | None = None
    #: The idle timeout in force, in seconds; 0 when there is none.
    idle_timeout_seconds: int = 0
    #: When the session ends unless its user does something.
    idle_expires_at: datetime | None = None
    #: False while Redis cannot be read: the idle timeout is not enforced then.
    idle_enforced: bool = True


class TermsOfUseState(BaseModel):
    """Platform-wide Terms of Use acceptance state.

    Terms of Use are accepted ONCE for the whole platform (by an admin, on
    behalf of the organisation), not per-user. Shipped inline on
    ``GET /auth/me`` (and returned standalone by the ``/auth/terms`` routes) so
    the frontend gate can decide what to render on boot without a second
    round-trip.
    """

    # Whether the gate applies to this deployment at all: the env config is
    # on AND the Agentic AI license is active. False on Community Edition.
    required: bool = False
    # The currently published document identifier (``TERMS_OF_USE_VERSION``).
    version: str = ""
    # Where the document itself lives (``TERMS_OF_USE_URL``).
    url: str = ""
    # True when the PLATFORM has accepted THIS version. Always True when
    # ``required`` is False, so the frontend only needs `!accepted` to gate.
    accepted: bool = True
    # When the platform accepted the current version (ISO 8601, UTC), if ever.
    accepted_at: str | None = None
    # The version the platform last accepted — may be an older one after a bump.
    accepted_version: str | None = None
    # Whether the CURRENT user may accept on behalf of the whole platform. Only
    # admins can; non-admins see a "waiting for an administrator" state instead
    # of an accept button.
    can_accept: bool = False


class TermsAcceptRequest(BaseModel):
    """Body of ``POST /auth/terms/accept``.

    Carrying the version makes acceptance explicit about *what* was accepted:
    a tab left open across a ToU bump would otherwise silently record consent
    to a document the user never saw. Mismatches are rejected with 409.
    """

    version: str


class MfaStatusResponse(BaseModel):
    """Per-user TOTP state, inlined on ``GET /auth/me``."""

    # Whether the second factor is active and enforced at login.
    enabled: bool = False
    # Whether this account CAN enroll. True for every account — the factor is
    # XTM One's own, layered on top of whichever way the first factor was
    # proven (password or IdP assertion). Kept as an explicit field rather than
    # assumed by the client so a future platform-level policy can withdraw
    # enrollment without a frontend change.
    available: bool = False
    # Whether ``POST /auth/mfa/disable`` will demand the account password.
    # False for SSO-only accounts, which have none — the disable form hides the
    # field rather than asking for something the user cannot supply.
    requires_password_to_disable: bool = False
    confirmed_at: str | None = None
    # Unused recovery codes left. Drives the "you are running low" nudge; the
    # codes themselves are unrecoverable after enrollment.
    recovery_codes_remaining: int = 0


class ManagedGroupRef(BaseModel):
    id: str
    name: str


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    title: str | None = None
    timezone: str | None = None
    theme: str | None = None
    # UI language override: "auto" (inherit platform language) or a
    # supported locale code (e.g. "fr-fr").
    language: str = "auto"
    # True when a group of theirs switches AI off (`services/group_ai_access.py`),
    # and the first such group by name. The platform-wide switch is not here:
    # it is `ai_enabled` on `/platform/config`.
    ai_disabled_by_group: bool = False
    ai_disabled_group: str | None = None
    # The person's CTEM level (`services/ctem_access.py`): `admin`, `decide`
    # (read and act on system signals and cases, answer their gates), `view`
    # (read only), or null (the signals they record and the cases they see).
    ctem_access: str | None = None
    # CTEM command center (XTM One home) per-user preferences.
    ctem_vertical: str = "cyber"
    ctem_splash_seen: bool = False
    # The command center this person built, per vertical; a vertical it does
    # not name (or null) is drawn with the default layout.
    ctem_dashboard_layouts: dict[str, Any] | None = None
    # What the person saw when they closed the command center's fleets
    # invitation (#9566): {"fleets": [slug], "modules": [name]}; null = never.
    ctem_fleet_invite_dismissed: dict[str, Any] | None = None
    # The organization context fields missing when the person closed the
    # command center's incomplete-context notice (#11586); null = never.
    organization_context_notice_dismissed: list[str] | None = None
    # The fleets this person's command center shows (#11464), in order; null =
    # the built-in CTEM fleets.
    ctem_dashboard_fleet_ids: list[str] | None = None
    # The fleets this person's Copilot home shows (#12063); null = every fleet.
    copilot_dashboard_fleet_ids: list[str] | None = None
    # The Copilot dashboard this person built; null = the default layout.
    copilot_dashboard_layout: dict[str, Any] | None = None
    role: str
    sso_provider: str | None = None
    default_agent_id: str | None = None
    # The custom dashboard shown on /dashboard instead of the platform's (null: the platform's).
    home_dashboard_id: str | None = None
    # Per-user General Assistant overrides (null = use platform defaults)
    ga_builtin_tool_ids: list[str] | None = None
    ga_custom_tool_ids: list[str] | None = None
    ga_integration_bindings: dict[str, list[str]] | None = None
    ga_mcp_server_ids: list[str] | None = None
    ga_knowledge_base_ids: list[str] | None = None
    # The browser profiles the General Assistant may open.
    ga_browser_profile_ids: list[str] | None = None
    # Per-user channel agent overrides for DM conversations
    channel_agent_config: dict[str, str] | None = None
    # MCP endpoint – user-selected agents to expose on /mcp/user
    mcp_exposed_agent_ids: list[str] | None = None
    # Favorite agents (starred on /agents) — pinned to the top of the list.
    # NULL and [] both mean "none"; the frontend treats them identically.
    favorite_agent_ids: list[str] | None = None
    # Own integration ids whose "personal OAuth token is shared" dashboard
    # reminder this user closed. NULL and [] both mean "none".
    oauth_share_dismissed_integration_ids: list[str] | None = None
    # The chat sidebar's workspace groups shown expanded; NULL = never chosen,
    # every group collapsed (#9179).
    chat_sidebar_expanded_workspaces: list[str] | None = None
    # The bell shows the work items waiting for this person (#9437).
    notify_waiting_work_items: bool = True
    # The composer's reply suggestion after a chat turn; whether one is
    # generated also needs the platform switch and a routing model.
    reply_suggestions_enabled: bool = True
    # Group memberships (IDs) of this user. Lets the frontend compute
    # group-shared manage rights with the SAME intersection semantics as the
    # backend (``helpers.get_group_permission`` only counts a shared group the
    # viewer actually belongs to), instead of guessing from an entity's
    # ``shared_group_permissions`` map alone.
    group_ids: list[str] | None = None
    # The groups this user manages: whose members' AI usage they read
    # in LLM Analytics, Traces and Sessions (`services/group_managers.py`).
    managed_groups: list[ManagedGroupRef] = []
    # Impersonation fields (populated when admin is impersonating)
    impersonating: bool = False
    impersonated_by_id: str | None = None
    impersonated_by_name: str | None = None
    # Terms of Use acceptance state — drives the blocking consent gate the
    # frontend wraps the whole authenticated route tree in.
    terms_of_use: TermsOfUseState = TermsOfUseState()
    # TOTP two-factor state. Shipped inline so Profile → Security can
    # render the correct card on boot without a second round-trip.
    mfa: MfaStatusResponse = MfaStatusResponse()

    model_config = {"from_attributes": True}


class AuthConfigResponse(BaseModel):
    """Public auth configuration – tells the frontend what login methods are available."""

    base_url: str = ""
    local_enabled: bool = True
    registration_enabled: bool = False
    allowed_domains: list[str] = []
    oidc_enabled: bool = False
    oidc_display_name: str = ""
    saml_enabled: bool = False
    saml_display_name: str = ""
    # Server-enforced minimum password length. Surfaced here so the frontend
    # password-policy validator + placeholder copy stay in sync with the
    # server-side ``check_password_policy`` floor — without this hop the
    # client would happily reject (or accept) passwords with a length the
    # server disagrees with the moment an operator tunes
    # ``MIN_PASSWORD_LENGTH``.  The full policy (letter + digit) is still
    # enforced server-side; this field only covers the length floor that
    # forms display in their placeholder + inline error copy.
    min_password_length: int = 8
    # Resolved feature-flag ids for this platform instance (from the
    # ENABLED_FEATURES env var — see app/core/feature_flags.py). Sent as
    # part of this already-public, boot-time endpoint so the frontend can
    # gate UI without a dedicated request. Never includes "*" itself — it
    # is expanded to every known flag id so the frontend never needs to
    # special-case the wildcard.
    enabled_features: list[str] = []
    # Whether financial cost amounts are available for observability views.
    # When false, the frontend masks cost values in trace/analytics UI.
    cost_data_available: bool = True
    # Whether the native Runner subsystem (XTM One Desktop, ``computer_use``
    # agent tools) is enabled on this deployment. Gates the Desktop page, its
    # sidebar-footer and user-menu entries and the ``app.desktop.open`` palette
    # command together, so none of them shows against a backend where every
    # runner endpoint 404s.
    runners_enabled: bool = True
    # Whether XTM One Relays are on (``RELAYS_ENABLED``, true by
    # default): gates the Relays page and the relay route field of
    # integrations, MCP servers and flow HTTP nodes.
    relays_enabled: bool = True
    # Whether the presentation layout check runs on this deployment
    # (``PRESENTATION_LAYOUT_CHECK_ENABLED``). Gates the viewer's ``Layout
    # check`` button so it never opens a panel whose only content is the 404
    # of ``POST /presentations/{id}/layout-check`` on a no-browser install.
    presentation_layout_check_enabled: bool = True
    # Whether the waiting arcade is on (``ARCADE_ENABLED``). Gates the
    # take-control affordance of the chat's waiting panel, the leaderboard and
    # the ``arcade.*`` palette commands, so none of them shows against a
    # backend where every ``/arcade`` route 404s.
    arcade_enabled: bool = True
    # The frontend build this server serves (``branding.get_frontend_build_id``,
    # the ``<meta name="build-id">`` of its ``index.html``; empty without a
    # built frontend). A page that booted a different build offers a reload:
    # a bundle from before an upgrade misreads what the new server
    # sends it until it is reloaded.
    build_id: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _determine_role(db: AsyncSession) -> str:
    """First user gets admin, subsequent users get user role."""
    count = await db.scalar(select(func.count()).select_from(User))
    if count == 0:
        return "admin"
    return "user"


def _build_token(user: User, *, continues: User | None = None) -> str:
    """Create a session JWT for a user.

    The token deliberately carries no ``role`` claim — authorization is always
    re-derived server-side from the database (see ``require_admin``), and the
    frontend reads the authoritative role from ``GET /auth/me``. Shipping the
    role in the token would only invite client-side spoofing of admin UI.

    A new session, unless *continues* is the caller authenticated with one: the
    token then carries that session on - its sign-in time and its idle deadline
    (``core/sessions.py``) - instead of starting a fresh lifetime.
    """
    session = getattr(continues, "web_session", None) if continues is not None else None
    return create_access_token(str(user.id), continues=session.claims if session is not None else None)


async def _build_terms_state(db: AsyncSession, user: User) -> TermsOfUseState:
    """Resolve the platform-wide Terms of Use state for the viewer.

    Mirrors the server-side gate in ``app.api.deps.enforce_terms_of_use``
    exactly — same env config, same ``is_agentic_ai_enabled`` scope check, same
    ``PlatformSettings`` acceptance read — so the UI can never show a gate the
    API would let through, or vice versa. ``can_accept`` reflects whether *this*
    viewer may accept on behalf of the platform (admins only). The license
    lookup is skipped entirely when the env config already says the gate is
    off, and the acceptance read (a read-only ``peek`` — this path must not
    create the singleton) is skipped whenever the gate does not apply.
    """
    can_accept = bool(getattr(user, "is_admin", False))
    current = settings.terms_of_use_current_version
    # An exempt account (``TERMS_OF_USE_EXEMPT_EMAILS``) is never shown the
    # gate, the same decision ``enforce_terms_of_use`` takes on the API.
    if not settings.terms_of_use_active or settings.terms_of_use_exempt(getattr(user, "email", None)):
        return TermsOfUseState(
            required=False,
            version=current,
            url=settings.terms_of_use_url,
            accepted=True,
            can_accept=can_accept,
        )

    # The *licence* question, not the AI toggle: this branch waives the terms,
    # so reading the resolved AI state here would stop showing them whenever an
    # administrator switched AI off.
    from app.api.v1.platform import is_agentic_ai_licensed

    if not await is_agentic_ai_licensed(db):
        # ``not required`` collapses to "accepted" so the frontend gate is a
        # single `!accepted` test on every deployment, CE included — and the
        # platform acceptance read is skipped (CE is never gated).
        return TermsOfUseState(
            required=False,
            version=current,
            url=settings.terms_of_use_url,
            accepted=True,
            can_accept=can_accept,
        )

    from app.api.v1.platform_settings_helpers import peek_platform_settings

    ps = await peek_platform_settings(db)
    accepted_version = ps.tou_accepted_version if ps is not None else None
    return TermsOfUseState(
        required=True,
        version=current,
        url=settings.terms_of_use_url,
        accepted=accepted_version == current,
        accepted_at=_iso_or_none(ps.tou_accepted_at) if ps is not None else None,
        accepted_version=accepted_version,
        can_accept=can_accept,
    )


def _iso_or_none(value: datetime | None) -> str | None:
    """ISO-8601 rendering for an optional timestamp column."""
    return value.isoformat() if value is not None else None


def _mfa_available(user: User) -> bool:
    """Whether *user* may enroll a TOTP second factor.

    Every account can, including SSO-only ones. The second factor is XTM One's
    own, layered on top of whichever way the *first* factor was proven — a
    password here, or an assertion from an identity provider. Both login paths
    (``POST /auth/login`` and the OIDC/SAML callbacks) route through
    ``mfa.challenge_required`` before a session is issued, so an enrolled
    account is covered on whichever one it actually uses.

    That deliberately does not assume the IdP handles MFA: plenty of
    deployments federate to a provider whose MFA policy they do not control
    (a customer tenant, a partner IdP, a legacy directory), and being able to
    enforce a second factor at the application boundary is the whole point of
    the request. Where the IdP *does* enforce one, this stacks — the user is
    free not to enroll.

    Kept as a function rather than inlined because it is also what decides
    whether the enrollment UI appears at all (via ``/auth/me``).
    """
    return True


def _build_mfa_state(user: User) -> MfaStatusResponse:
    """Resolve the TOTP state block for *user*.

    ``getattr`` throughout so a User-like object without the newer columns
    (older test fixtures, partial stubs) degrades to "MFA off" instead of
    raising — the same defensive read the other recent user columns get.
    """
    from app.services.mfa import count_unused_recovery_codes

    return MfaStatusResponse(
        enabled=bool(getattr(user, "mfa_enabled", False)),
        available=_mfa_available(user),
        # Mirrors the server-side branch in ``mfa_disable`` exactly, so the form
        # can never ask for a password the endpoint will ignore, or omit one it
        # will demand.
        requires_password_to_disable=bool(getattr(user, "hashed_password", None)),
        confirmed_at=_iso_or_none(getattr(user, "mfa_confirmed_at", None)),
        recovery_codes_remaining=count_unused_recovery_codes(
            getattr(user, "mfa_recovery_codes", None)
        ),
    )


# ---------------------------------------------------------------------------
# Public: auth configuration (no auth required)
# ---------------------------------------------------------------------------


@router.get("/config", response_model=AuthConfigResponse)
async def auth_config(db: AsyncSession = Depends(get_db)):
    """Return which login methods are enabled (called by frontend login page)."""
    from app.core.security_options import security_option
    from app.core.sso_helpers import get_sso_config
    from app.services.branding import get_frontend_build_id

    cfg = await get_sso_config(db)
    return AuthConfigResponse(
        base_url=settings.base_url,
        local_enabled=cfg.local_auth_enabled,
        registration_enabled=cfg.registration_enabled,
        allowed_domains=cfg.allowed_domain_list,
        oidc_enabled=cfg.oidc_enabled,
        oidc_display_name=cfg.oidc_display_name,
        saml_enabled=cfg.saml_enabled,
        saml_display_name=cfg.saml_display_name,
        min_password_length=security_option("min_password_length"),
        enabled_features=resolve_enabled_feature_flags(),
        cost_data_available=settings.observability_cost_data_enabled,
        runners_enabled=settings.runners_enabled,
        relays_enabled=relays_active(),
        presentation_layout_check_enabled=settings.presentation_layout_check_enabled,
        arcade_enabled=settings.arcade_enabled,
        build_id=get_frontend_build_id(),
    )


# ---------------------------------------------------------------------------
# Local auth
# ---------------------------------------------------------------------------


@router.post(
    "/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    from app.core.email_domains import email_domain_allowed
    from app.core.sso_helpers import get_sso_config

    cfg = await get_sso_config(db)

    # Block if local auth is disabled (but always allow first-user registration)
    user_count = await db.scalar(select(func.count()).select_from(User))
    if not cfg.local_auth_enabled and user_count > 0:
        raise HTTPException(
            status_code=403,
            detail="Local authentication is disabled. Please use SSO to sign in.",
        )

    # Check if registration is enabled (always allow if no users exist yet = first user)
    if not cfg.registration_enabled and user_count > 0:
        raise HTTPException(status_code=403, detail="Account registration is disabled")

    validate_password(body.password)

    # Normalise the email so identity comparisons (exists check below,
    # ``register-with-invitation``, admin user-creation, and the
    # invitation issuance check in ``invitations.create_invitation``) all
    # agree on the canonical form.  Postgres ``UNIQUE(text)`` is
    # case-sensitive, so storing the raw input would allow mixed-case
    # duplicates to slip past the unique constraint — even though the
    # IntegrityError fallback below would still 409, those duplicates
    # would already exist in the DB and confuse downstream lookups
    # (login by email, OAuth/SSO email linking, audit log search).
    email = str(body.email).strip().lower()

    # An address exempt from the Terms of Use gate is never claimed by an
    # unverified sign-up: an administrator, an invitation or SSO creates it.
    if settings.terms_of_use_exempt(email):
        raise HTTPException(status_code=403, detail="This email address cannot be registered")

    # Domain validation (from DB settings)
    domain_list = cfg.allowed_domain_list
    if not email_domain_allowed(email, domain_list):
        raise HTTPException(
            status_code=403,
            detail=f"Registration restricted to: {', '.join(domain_list)}",
        )

    # Case-insensitive exists check matches the issuance-time check in
    # ``invitations.create_invitation`` and the admin path in
    # ``users.create_user``.  The IntegrityError handler below is still
    # the authoritative race-resolution primitive against ``UNIQUE(email)``
    # — this check is best-effort and prevents the common case before
    # touching the DB.
    exists = await db.execute(
        select(User).where(func.lower(User.email) == email)
    )
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    role = await _determine_role(db)

    user = User(
        email=email,
        hashed_password=await hash_password_async(body.password),
        full_name=body.full_name,
        role=role,
    )

    if settings.is_xtm_one:
        from app.models.agent import Agent

        ctem_r = await db.execute(
            select(Agent.id).where(Agent.slug == "ctem-assistant").limit(1)
        )
        ctem_id = ctem_r.scalar_one_or_none()
        if ctem_id:
            user.default_agent_id = ctem_id
            user.default_agent_source = "platform"
            user.default_agent_set_at = datetime.now(UTC)

    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        # The exists-check above is best-effort: two concurrent ``/register``
        # calls for the same email both pass it, then the DB ``UNIQUE(email)``
        # constraint atomically resolves the race.  Treat the loser the same
        # way the manual check does (409 ``Email already registered``)
        # instead of leaking a 500.  Mirrors ``register_with_invitation``.
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from exc
    # Every new account joins "CTEM Viewers" once, at its creation (#2441).
    from app.services.ctem_groups import join_viewers_at_creation

    await join_viewers_at_creation(db, user)
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Registered account '{user.email}'",
        details={
            "email": user.email,
            "full_name": user.full_name,
        },
        request=request,
    )
    # Commit before handing the token back, not on ``get_db`` teardown (after
    # the response is sent): the client's very next request carries it, and a
    # user row that has not landed yet answers that request 401.
    await db.commit()
    token = _build_token(user)
    return TokenResponse(access_token=token)


@router.post(
    "/register-with-invitation",
    response_model=RegisterWithInvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_with_invitation(
    body: RegisterWithInvitationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Register a new account by consuming an admin-generated invitation.

    The invitation is bound to an email and carries the role to assign — the
    invitee cannot change the email. A valid invite is sufficient authorization,
    so this bypasses the global ``registration_enabled`` / ``local_auth_enabled``
    gates (unlike :func:`register`).
    """
    await enforce_rate_limit(request, "register-invitation", limit=10, window_seconds=60)

    invitation = await token_store.get_invitation(body.token)
    if invitation is None:
        raise HTTPException(
            status_code=410, detail="This invitation link is invalid or has expired"
        )

    # ``token_store.create_invitation`` normalises the email (``.strip().lower()``)
    # at storage time, so the value read back here is already in the
    # canonical form.  Normalise again as defence-in-depth in case a
    # legacy Redis payload predates that storage-side normalisation
    # (rolling deployments / mixed-version replays).
    email = (invitation["email"] or "").strip().lower()
    role = invitation["role"]

    validate_password(body.password)

    # Case-insensitive exists check matches the issuance-time check in
    # ``invitations.create_invitation``: Postgres ``UNIQUE(text)`` is
    # case-sensitive so two rows with the same email but different case
    # would both insert successfully, and a user registered as
    # ``Foo@Example.com`` would silently slip past this guard on a
    # ``foo@example.com`` invite (then collide later at SSO / login on
    # case-different inputs).  ``func.lower(User.email)`` is supported by
    # the index on ``users.email`` via expression matching on the lowered
    # form for the typical case, and is correct semantically even when
    # Postgres falls back to a seq scan.
    exists = await db.execute(
        select(User).where(func.lower(User.email) == email)
    )
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    # Read before the user row is written: a failure here only loses the redirect.
    try:
        redirect_url = await registered_destination(db, invitation.get("destination"))
    except Exception as exc:
        await db.rollback()
        redirect_url = None
        logger.warning(
            "Could not resolve the destination of an invitation for %s; registering without a redirect: %s",
            email,
            format_exception_message(exc),
        )

    user = User(
        email=email,
        hashed_password=await hash_password_async(body.password),
        full_name=body.full_name,
        role=role,
    )

    if settings.is_xtm_one:
        from app.models.agent import Agent

        ctem_r = await db.execute(
            select(Agent.id).where(Agent.slug == "ctem-assistant").limit(1)
        )
        ctem_id = ctem_r.scalar_one_or_none()
        if ctem_id:
            user.default_agent_id = ctem_id
            user.default_agent_source = "platform"
            user.default_agent_set_at = datetime.now(UTC)

    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Lost a race against a concurrent registration for the same email.
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from exc
    # Every new account joins "CTEM Viewers" once, at its creation (#2441).
    from app.services.ctem_groups import join_viewers_at_creation

    await join_viewers_at_creation(db, user)

    # Audit the registration first so the row is part of the same transaction
    # as the user creation — a single commit point keeps both atomic.
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Registered account '{user.email}' via invitation with role '{user.role}'",
        details={"email": user.email, "role": user.role},
        request=request,
    )

    # Commit user + audit explicitly BEFORE consuming the invite. Otherwise a
    # transient DB error inside ``get_db()``'s closing commit would roll back
    # the user row while leaving the Redis key already deleted, burning the
    # invite. Committing first means: if the commit fails we still hold a
    # valid invite (Redis was never touched), and if the commit succeeds we
    # consume atomically next.
    await db.commit()

    # Consume the invite atomically with ``GETDEL`` (Redis 6.2+). At this
    # point the user already exists and is committed, so even if the consume
    # races against another registration call, the second caller is blocked
    # earlier by the ``UNIQUE(email)`` constraint flush above. The atomic
    # consume is defence-in-depth: it guarantees a single Redis operation
    # for the read+delete, with no window where a second caller could replay
    # the link before we delete it.
    #
    # Treat the consume as best-effort because the user row is already
    # committed: a transient Redis outage (or any other failure in
    # ``consume_invitation``) must not turn a successful registration into a
    # 500 that locks the caller out of the account they just created — this
    # endpoint deliberately bypasses ``local_auth_enabled``, so a 500 here
    # could leave the user with no way back in on SSO-only platforms.  The
    # DB ``UNIQUE(email)`` constraint is the primary single-use backstop,
    # so a lingering key in Redis cannot be replayed: the next caller
    # would 409 at ``db.flush()`` above.  The key will also TTL out.
    try:
        consumed = await token_store.consume_invitation(body.token)
    except Exception as exc:
        # Catch-all is intentional and the rationale lives in the comment
        # above — the user row is committed, so any consume failure must
        # log + continue rather than 500-and-lock-out the caller.
        consumed = None
        logger.warning(
            "consume_invitation failed for %s after user was committed "
            "(user_id=%s); leaving Redis key to TTL out — replay is blocked by "
            "UNIQUE(users.email). Underlying error: %s",
            email,
            user.id,
            format_exception_message(exc),
        )
    if consumed is None:
        # Already consumed by a concurrent caller, or the consume itself
        # failed (see except above).  Our user creation already succeeded
        # so this is purely informational; there is nothing left to clean
        # up. Logging at INFO so an operator can correlate against the
        # audit log if they ever investigate apparent invite reuse.
        logger.info(
            "Invitation token already consumed for %s — concurrent registration "
            "winner or consume error. No action needed; user row is committed.",
            email,
        )

    token = _build_token(user)
    return RegisterWithInvitationResponse(access_token=token, redirect_url=redirect_url)


async def _dispatch_password_reset(user_id: uuid.UUID, email: str) -> None:
    """Create a reset token and email the link (runs in the background).

    Done off the request path so the ``forgot-password`` response time does not
    depend on whether the account exists (no timing-based enumeration).

    Refuses to mint a Redis token when SMTP is unconfigured AND we're not
    running in debug mode: in that combination the link can never be
    delivered (no email, no log line) and the token would sit in Redis with
    a TTL until it expires.  ``forgot-password`` runs unauthenticated and
    is only IP-rate-limited (5/min/IP), so an unconfigured SMTP setup +
    repeated requests would accumulate single-use secrets in Redis that
    can never be used.  Checking BEFORE ``create_password_reset`` avoids
    that waste.  Debug builds still mint the token so developers can
    grab the link from server logs.
    """
    from app.services.email import is_email_configured, password_reset_email
    from app.services.email import send_email as _send_email

    if not is_email_configured() and not settings.debug:
        logger.warning(
            "SMTP not configured — cannot email password reset to %s "
            "(no Redis token minted; set DEBUG=true to surface the link in "
            "server logs instead)",
            email,
        )
        return

    from app.core.security_options import security_option

    ttl_hours = security_option("password_reset_expire_hours")
    raw_token, _ = await token_store.create_password_reset(
        user_id=user_id,
        created_by_id=None,
        ttl_hours=ttl_hours,
    )
    # Reset link targets the frontend ``/reset-password`` route — use
    # ``frontend_url`` for split-origin deployments, fall back to
    # ``base_url`` for single-origin (matches ``oauth_callbacks`` /
    # ``auth_sso`` and the matching admin-issued reset path in
    # ``users.py::send_password_reset``).
    frontend_base = (settings.frontend_url or settings.base_url).rstrip("/")
    link = f"{frontend_base}/reset-password?token={raw_token}"
    if is_email_configured():
        html, text = password_reset_email(link, ttl_hours)
        await _send_email(to=email, subject="Reset your password", html=html, text=text)
    else:
        # Reached only when ``settings.debug`` is True (the gate above
        # short-circuits the non-debug + unconfigured combination).
        logger.warning(
            "SMTP not configured — password reset link for %s: %s", email, link
        )


@router.post("/forgot-password")
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Self-service password reset request (no auth required).

    If an active account exists, a single-use reset link is emailed (in the
    background). Always returns the same generic 200 so the endpoint cannot be
    used to enumerate accounts (by response, status, or timing).
    """
    await enforce_rate_limit(request, "forgot-password", limit=5, window_seconds=60)

    # Case-insensitive lookup so users invited via the new lowercase-
    # normalizing flows (``register``, ``register-with-invitation``,
    # ``invitations.create_invitation``, ``users.create_user``) can request a
    # reset by typing their email in any case.  Without ``func.lower`` the
    # Postgres ``=`` operator on the ``text`` column would silently fail to
    # match ``user@example.com`` against an input of ``User@Example.com`` —
    # the symmetric guarantee the registration paths already provide via
    # ``func.lower(User.email) == lowered_input``.
    result = await db.execute(
        select(User).where(func.lower(User.email) == body.email.strip().lower())
    )
    user = result.scalar_one_or_none()
    # SSO-only users cannot reset password — auth is managed by their IdP.
    if user is not None and user.is_active and not (user.sso_provider and not user.hashed_password):
        background.add_task(_dispatch_password_reset, user.id, user.email)

    return {
        "status": "ok",
        "message": "If an account exists for that email, a reset link has been sent.",
    }


@router.get("/reset-password/{token}", response_model=ValidatePasswordResetResponse)
async def validate_password_reset(token: str, db: AsyncSession = Depends(get_db)):
    """Validate a password reset token (no auth required).

    Returns the target account's email so the reset page can show whose
    password is being set. Returns ``valid: false`` for unknown/expired/used.
    """
    data = await token_store.get_password_reset(token)
    if data is None:
        return ValidatePasswordResetResponse(valid=False)

    user = await db.get(User, uuid.UUID(data["user_id"]))
    if user is None or not user.is_active:
        return ValidatePasswordResetResponse(valid=False)
    # SSO-only users cannot reset password — auth is managed by their IdP.
    if user.sso_provider and not user.hashed_password:
        return ValidatePasswordResetResponse(valid=False, sso_only=True)
    return ValidatePasswordResetResponse(valid=True, email=user.email)


@router.post("/reset-password", response_model=LoginResponse)
async def reset_password(
    body: ResetPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    """Consume a password reset token and set a new password (no auth required).

    Signs the user in afterwards — but only past the same second factor a normal
    login would demand. Skipping it here would make the reset link a
    complete bypass of two-factor: whoever controls the mailbox resets the
    password and walks straight in, which is precisely the threat the second
    factor exists to stop. The reset still succeeds either way; what is withheld
    is the session.
    """
    await enforce_rate_limit(request, "reset-password", limit=10, window_seconds=60)

    # Validate password policy BEFORE consuming the token so a weak-password
    # rejection doesn't burn the link. We re-validate after consume too only
    # implicitly — the value is identical and we never round-trip it.
    validate_password(body.new_password)

    # Atomically read-and-delete the token (``GETDEL``, Redis 6.2+).  Two
    # concurrent requests with the same token can no longer both pass: only
    # one wins the consume and the other gets ``None`` → 410.  Without this,
    # the previous get-then-flush-then-delete shape allowed both callers to
    # read the same payload and race on the password update.
    data = await token_store.consume_password_reset(body.token)
    if data is None:
        raise HTTPException(
            status_code=410, detail="This reset link is invalid or has expired"
        )

    user = await db.get(User, uuid.UUID(data["user_id"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="Account not found")

    # SSO-only users cannot set a local password. The token was already
    # consumed above — this branch should be unreachable in practice because
    # ``forgot-password`` and the admin-issued reset paths both refuse to
    # mint a token for SSO-only accounts. Keep the guard as defence-in-depth
    # against a misconfigured account flipping to SSO-only between mint and
    # consume.
    if user.sso_provider and not user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="This account uses SSO for authentication. Password reset is not available — please sign in via your identity provider.",
        )

    user.hashed_password = await hash_password_async(body.new_password)
    user.password_changed_at = datetime.now(UTC)
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="password",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Reset password via link ({user.email})",
        request=request,
    )
    # Commit before answering, not on ``get_db`` teardown (after the response
    # is sent): whatever comes next - the session below, the challenge, or a
    # sign-in with the new password - would otherwise race the transaction.
    await db.commit()

    from app.services.mfa import challenge_required, create_challenge_token

    if challenge_required(user):
        # The password is set and committed; only the session is withheld. The
        # challenge is minted after ``password_changed_at`` so it survives the
        # freshness check in ``mfa_verify`` that this very reset just armed.
        return LoginResponse(
            mfa_required=True,
            mfa_token=create_challenge_token(user.id, method="local"),
        )

    return LoginResponse(access_token=_build_token(user))


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    await enforce_rate_limit(request, "login", limit=10, window_seconds=60)

    from app.core.sso_helpers import get_sso_config

    cfg = await get_sso_config(db)
    if not cfg.local_auth_enabled:
        raise HTTPException(
            status_code=403,
            detail="Local authentication is disabled. Please use SSO to sign in.",
        )

    # Case-insensitive lookup so users invited via the new lowercase-
    # normalizing flows can log in regardless of the case they type into
    # the form.  Mirrors the symmetric guarantee at the registration sites
    # (``register``, ``register-with-invitation``, ``users.create_user``,
    # ``invitations.create_invitation``).  Without ``func.lower`` the
    # Postgres ``=`` operator on the ``text`` column would treat
    # ``user@example.com`` and ``User@Example.com`` as distinct identities
    # — locking the user out of an account that was just normalized at
    # registration.
    result = await db.execute(
        select(User).where(func.lower(User.email) == body.email.strip().lower())
    )
    user = result.scalar_one_or_none()
    if (
        not user
        or not user.hashed_password
        or not await verify_password_async(body.password, user.hashed_password)
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account is disabled")

    # Second factor. The password is proven, but nothing usable as a
    # session is issued yet: the caller gets a scoped, 5-minute challenge token
    # that only ``POST /auth/mfa/verify`` accepts. The SSO callbacks apply the
    # same ``challenge_required`` predicate to the assertion they just
    # validated, so an enrolled account is covered on either path.
    from app.services.mfa import challenge_required

    if challenge_required(user):
        return LoginResponse(
            mfa_required=True,
            mfa_token=await begin_mfa_challenge(
                db, user, request=request, login_method="local"
            ),
        )

    await log_audit(
        db,
        user=user,
        action="login",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Logged in ({user.email})",
        details={
            "login_method": "local",
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    token = _build_token(user)
    return LoginResponse(access_token=token)


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------


@router.get("/me", response_model=UserResponse)
async def me(
    request: Request,
    # Ungated on purpose: this response CARRIES the Terms of Use state the
    # frontend gate renders from, so it has to stay reachable while the user's
    # acceptance is still pending. A companion locked behind its PIN
    # does not read it (``services/app_lock.py``).
    user: User = Depends(get_current_user_unlocked),
    db: AsyncSession = Depends(get_db),
):
    # Check JWT for impersonation claim
    impersonating = False
    impersonated_by_id: str | None = None
    impersonated_by_name: str | None = None

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            payload = decode_access_token(auth_header[7:])
            admin_id_str = payload.get("impersonated_by")
            if admin_id_str:
                result = await db.execute(
                    select(User).where(User.id == uuid.UUID(admin_id_str))
                )
                admin_user = result.scalar_one_or_none()
                if admin_user:
                    impersonating = True
                    impersonated_by_id = str(admin_user.id)
                    impersonated_by_name = admin_user.full_name
        except Exception as exc:
            logger.warning("Failed to resolve impersonation claim: %s", format_exception_message(exc))

    group_ids = await get_user_group_ids(db, user.id)
    terms_of_use = await _build_terms_state(db, user)
    ai_groups = await disabling_group_names(db, user.id)

    return UserResponse(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        title=user.title,
        timezone=user.timezone,
        theme=user.theme,
        language=getattr(user, "language", None) or "auto",
        ai_disabled_by_group=bool(ai_groups),
        ai_disabled_group=ai_groups[0] if ai_groups else None,
        ctem_access=await ctem_access.level(db, user),
        # Coalesce: pre-existing rows may carry NULL if the column was added
        # nullable by a model-reflection step before the backfill migration, and
        # use getattr so a User-like object without these newer attributes (older
        # test fixtures, partial stubs) degrades to the defaults instead of 500ing.
        ctem_vertical=getattr(user, "ctem_vertical", None) or "cyber",
        ctem_splash_seen=bool(getattr(user, "ctem_splash_seen", False)),
        ctem_dashboard_layouts=getattr(user, "ctem_dashboard_layouts", None),
        ctem_fleet_invite_dismissed=getattr(user, "ctem_fleet_invite_dismissed", None),
        organization_context_notice_dismissed=getattr(user, "organization_context_notice_dismissed", None),
        ctem_dashboard_fleet_ids=getattr(user, "ctem_dashboard_fleet_ids", None),
        copilot_dashboard_fleet_ids=getattr(user, "copilot_dashboard_fleet_ids", None),
        copilot_dashboard_layout=getattr(user, "copilot_dashboard_layout", None),
        role=user.role,
        sso_provider=user.sso_provider,
        default_agent_id=str(user.default_agent_id) if user.default_agent_id else None,
        # Off, the platform's home: a stored custom one is not offered.
        home_dashboard_id=(
            str(user.home_dashboard_id)
            if getattr(user, "home_dashboard_id", None) and is_feature_enabled("DASHBOARD_STUDIO")
            else None
        ),
        ga_builtin_tool_ids=user.ga_builtin_tool_ids,
        ga_custom_tool_ids=user.ga_custom_tool_ids,
        ga_integration_bindings=user.ga_integration_bindings,
        ga_mcp_server_ids=user.ga_mcp_server_ids,
        ga_knowledge_base_ids=user.ga_knowledge_base_ids,
        # getattr like the fields below: User-like stubs predating the column read as none.
        ga_browser_profile_ids=getattr(user, "ga_browser_profile_ids", None),
        channel_agent_config=user.channel_agent_config,
        mcp_exposed_agent_ids=user.mcp_exposed_agent_ids,
        # getattr for the same reason as the CTEM fields above: User-like stubs
        # predating this column must degrade to "no favorites", not 500.
        favorite_agent_ids=getattr(user, "favorite_agent_ids", None),
        oauth_share_dismissed_integration_ids=getattr(
            user, "oauth_share_dismissed_integration_ids", None
        ),
        chat_sidebar_expanded_workspaces=getattr(
            user, "chat_sidebar_expanded_workspaces", None
        ),
        notify_waiting_work_items=getattr(user, "notify_waiting_work_items", True) is not False,
        reply_suggestions_enabled=getattr(user, "reply_suggestions_enabled", True) is not False,
        group_ids=group_ids,
        managed_groups=[
            ManagedGroupRef(id=group.id, name=group.name) for group in await managed_groups(db, user.id)
        ],
        impersonating=impersonating,
        impersonated_by_id=impersonated_by_id,
        impersonated_by_name=impersonated_by_name,
        terms_of_use=terms_of_use,
        mfa=_build_mfa_state(user),
    )


# ---------------------------------------------------------------------------
# Session lifetime and idle timeout
#
# Ungated like ``/me``: the consent screen times out like any other page.
# ---------------------------------------------------------------------------

#: The web app signals activity at most every 15 seconds (``lib/session.ts``).
SESSION_ACTIVITY_PER_MINUTE = 20


def _epoch(seconds: int | None) -> datetime | None:
    return None if seconds is None else datetime.fromtimestamp(seconds, UTC)


def _session_status(web_session: WebSession | None) -> SessionStatusResponse:
    now = datetime.now(UTC)
    if web_session is None:
        return SessionStatusResponse(kind="other", server_time=now)
    return SessionStatusResponse(
        kind="session",
        server_time=now,
        expires_at=_epoch(web_session.expires_at),
        idle_timeout_seconds=web_session.idle_timeout,
        idle_expires_at=_epoch(web_session.idle_expires_at),
        idle_enforced=web_session.idle_enforced,
    )


@router.get("/session", response_model=SessionStatusResponse)
async def get_session_status(user: User = Depends(get_current_user_ungated)):
    """When the caller's session ends. Reading it is not activity: it moves no deadline."""
    return _session_status(getattr(user, "web_session", None))


@router.post("/session/activity", response_model=SessionStatusResponse)
async def record_session_activity(
    user: User = Depends(get_current_user_ungated),
    db: AsyncSession = Depends(get_db),
):
    """The person did something in the web app: their session's idle deadline moves on.

    The web app sends it on a click, a key, a scroll or a return to the tab -
    never on its own refreshes - at most every 15 seconds. It moves the idle
    deadline of the session that sends it, which the dependency has just
    accepted (an ended session is never revived), and never its absolute
    lifetime.
    """
    web_session = getattr(user, "web_session", None)
    if web_session is None:
        return _session_status(None)
    await enforce_rate_limit(
        None,
        "session-activity",
        limit=SESSION_ACTIVITY_PER_MINUTE,
        window_seconds=60,
        bucket=web_session.activity_key,
        quiet=True,
    )
    # The same signal keeps an unlocked companion unlocked (``services/app_lock.py``):
    # the person is using it, not the app polling on its own.
    from app.services.app_lock import extend as extend_app_unlock

    await extend_app_unlock(db, user)
    await db.commit()
    return _session_status(await record_activity(web_session))


# ---------------------------------------------------------------------------
# Terms of Use acceptance gate
#
# All three routes skip the gate (``get_current_user_ungated``, or
# ``get_current_user_unlocked`` where a locked companion must not act): they are
# the escape hatch the gate itself leaves open, so depending on the gated
# ``get_current_user`` would make acceptance impossible (403 on the very call
# that clears the 403).
# ---------------------------------------------------------------------------


@router.get("/terms", response_model=TermsOfUseState)
async def get_terms(
    user: User = Depends(get_current_user_ungated),
    db: AsyncSession = Depends(get_db),
):
    """Return the current Terms of Use state for the calling user."""
    return await _build_terms_state(db, user)


@router.post("/terms/accept", response_model=TermsOfUseState)
async def accept_terms(
    body: TermsAcceptRequest,
    request: Request,
    user: User = Depends(get_current_user_unlocked),
    db: AsyncSession = Depends(get_db),
):
    """Record platform-wide acceptance of the current Terms of Use.

    Terms of Use bind the whole organisation, so a SINGLE acceptance clears the
    gate for every user. Only an admin may accept, on behalf of the platform;
    the accepted version / timestamp / accepting user are stored on the
    ``PlatformSettings`` singleton, and the audit log is the who/when/version
    compliance trail. Idempotent: re-accepting the current version leaves the
    original acceptance evidence untouched.
    """
    current = settings.terms_of_use_current_version
    if not settings.terms_of_use_active:
        # Nothing to accept — the deployment has the gate switched off. Report
        # the (already unblocked) state rather than writing a record against a
        # version this platform does not publish.
        return await _build_terms_state(db, user)

    if not getattr(user, "is_admin", False):
        # Terms of Use are accepted on behalf of the organisation; a regular
        # user cannot bind the whole platform.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only an administrator can accept the Terms of Use on behalf "
                "of the platform."
            ),
        )

    if body.version != current:
        # A tab left open across a version bump: refuse rather than silently
        # record consent to a document the admin never had in front of them.
        # The client refetches ``/auth/terms`` and re-renders.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Terms of Use version mismatch: this platform publishes "
                f"'{current}'. Reload and accept the current version."
            ),
        )

    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.models.platform_settings import PlatformSettings

    ps = await get_platform_settings(db)
    # Advance the platform acceptance ATOMICALLY: the UPDATE only matches while
    # the stored version IS DISTINCT FROM the current one, so of two admins
    # racing this endpoint exactly one advances the version and becomes the
    # recorded accepting admin — the loser (and any idempotent re-submission)
    # matches zero rows, leaves the original who/when evidence untouched, and
    # writes no duplicate audit entry. A plain read-check-write here would let
    # both requests pass the check and overwrite each other's evidence.
    accepted_at = datetime.now(UTC)
    result = await db.execute(
        update(PlatformSettings)
        .where(
            PlatformSettings.id == ps.id,
            PlatformSettings.tou_accepted_version.is_distinct_from(current),
        )
        .values(
            tou_accepted_version=current,
            tou_accepted_at=accepted_at,
            tou_accepted_by_user_id=user.id,
        )
        # The manual attribute sync below keeps the identity-mapped instance
        # coherent, so skip the ORM's own (extra-query) synchronization.
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        # The bulk UPDATE bypasses the identity map — sync the loaded instance
        # so ``_build_terms_state`` (which re-reads through the session) sees
        # the values this request just wrote.
        ps.tou_accepted_version = current
        ps.tou_accepted_at = accepted_at
        ps.tou_accepted_by_user_id = user.id
        await log_audit(
            db,
            user=user,
            action="accept",
            entity_type="terms_of_use",
            entity_name=current,
            summary=f"Accepted Terms of Use version {current} for the platform",
            details={"tou_version": current, "tou_url": settings.terms_of_use_url},
            request=request,
        )
        await db.commit()
    return await _build_terms_state(db, user)


@router.post("/terms/decline", response_model=TermsOfUseState)
async def decline_terms(
    request: Request,
    user: User = Depends(get_current_user_unlocked),
    db: AsyncSession = Depends(get_db),
):
    """Record that the user declined the current Terms of Use.

    Audit-only — there is no "declined" state to store, because declining
    changes nothing: the user stays gated. The client clears its token
    immediately after and lands back on the login page. Exists so a refusal is
    visible in the audit trail rather than indistinguishable from a user who
    simply closed the tab.
    """
    if settings.terms_of_use_active:
        current = settings.terms_of_use_current_version
        await log_audit(
            db,
            user=user,
            action="decline",
            entity_type="terms_of_use",
            entity_name=current,
            summary=f"Declined Terms of Use version {current}",
            details={"tou_version": current},
            request=request,
        )
        await db.commit()
    return await _build_terms_state(db, user)


# ---------------------------------------------------------------------------
# Password change (local accounts only)
# ---------------------------------------------------------------------------


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.put("/password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the current user's password (local accounts only)."""
    if user.sso_provider and not user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="SSO accounts cannot change password here — use your identity provider.",
        )
    if not user.hashed_password or not await verify_password_async(
        body.current_password, user.hashed_password
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    validate_password(body.new_password)
    user.hashed_password = await hash_password_async(body.new_password)
    user.password_changed_at = datetime.now(UTC)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="password",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Changed password ({user.email})",
        details={
            "password_changed": True,
        },
        request=request,
    )
    # Commit before answering, not on ``get_db`` teardown (after the response
    # is sent): a sign-in with the new password, or an older session that must
    # now be refused, would otherwise race the transaction.
    await db.commit()
    # Issue a fresh token (iat now) so the caller's own session survives while
    # any other/older sessions are invalidated. The same session: its sign-in
    # time is kept, a password change does not extend its lifetime.
    return {"status": "ok", "access_token": _build_token(user, continues=user)}


# ---------------------------------------------------------------------------
# TOTP two-factor authentication
#
# Enrollment is deliberately two-phase: ``setup`` mints a secret and hands back
# the QR, ``enable`` only flips ``mfa_enabled`` once a code generated from that
# very secret verifies. Anything less would let a mis-scanned QR — or a user who
# closes the tab halfway — lock an account out of a factor nobody can produce.
#
# Every verification path in this section (``enable``, ``verify``, ``disable``,
# ``recovery-codes``) funnels through ``_verify_second_factor`` so the replay
# guard, the recovery-code branch and the failure audit trail can never drift
# apart between endpoints.
# ---------------------------------------------------------------------------


class MfaSetupResponse(BaseModel):
    """Payload of ``POST /auth/mfa/setup`` — everything needed to enroll.

    ``secret`` is returned in the clear exactly once, here, and only to the
    already-authenticated owner: authenticator apps that cannot use a camera
    (KeePassXC on a desktop, a VM, a locked-down phone) need the manual entry
    key, and there is no second chance to read it later.
    """

    secret: str
    otpauth_uri: str
    qr_code: str
    # Echoed so the enrollment screen can label the manual-entry fields
    # correctly for apps that ask for them (KeePassXC, Aegis, FreeOTP).
    digits: int
    period: int
    algorithm: str


class MfaCodeRequest(BaseModel):
    """A 6-digit TOTP code, or a recovery code, depending on the endpoint."""

    code: str


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str


class MfaDisableRequest(BaseModel):
    """Turning MFA off requires every factor the account actually has.

    For a local account that means both: a password alone would let anyone who
    walks up to an unlocked, already signed-in browser strip the second factor
    and keep long-term access; a code alone would let someone holding only the
    device do it.

    ``password`` is optional because an SSO-only account has none to give. Those
    users confirm with a code alone — the strongest proof they can produce here,
    since re-proving the SSO factor would mean bouncing them through the IdP and
    back, and the session they are already holding is that same proof.
    """

    password: str | None = None
    code: str


class MfaRecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


async def _load_user_secret(user: User) -> str | None:
    """Decrypt the stored TOTP secret for *user* (``None`` if absent/unreadable)."""
    from app.services.mfa import decrypt_secret

    return decrypt_secret(getattr(user, "mfa_secret", None))


# The 401 detail of a wrong second factor, whichever factor was tried.
MFA_INVALID_CODE_DETAIL = "Invalid verification code. Please try again."


class MfaInvalidCodeError(HTTPException):
    """The 401 of a wrong second factor.

    Still an ``HTTPException`` for the JSON routes; its own type lets a caller
    that answers a wrong code differently from an expired challenge (the MCP
    OAuth consent page keeps the challenge for a retry) tell them apart
    without reading the detail.
    """

    def __init__(self) -> None:
        super().__init__(status_code=401, detail=MFA_INVALID_CODE_DETAIL)


class MfaCompletion(NamedTuple):
    """What ``complete_mfa_challenge`` signed in, and how."""

    user: User
    challenge: "MfaChallenge"
    # The factor that verified (``"totp"`` / ``"recovery"``), or ``None`` when
    # two-factor was turned off while the challenge was in flight and no code
    # was checked. What an audit entry may claim about the second factor.
    factor: str | None

# Audit ``action`` per calling flow. ``action`` is an indexed, filterable field
# in the audit UI; ``details`` is not. Writing a literal "login" here for every
# flow would file a mistyped code during *disable* — or a recovery code spent
# while regenerating — as a login event, which is exactly the noise that would
# drown the signal this feature exists to raise: repeated failed second factors
# during LOGIN, with no matching successful login, is someone holding a password
# but not the device. Mapped to the verb vocabulary ``log_audit`` documents
# rather than a synthetic ``mfa_*`` namespace, since ``entity_type="mfa"``
# already carries that half.
_AUDIT_ACTION_BY_CONTEXT = {
    "login": "login",
    # The MCP OAuth consent page's password sign-in: a login too, told
    # apart from the web app's in ``details``.
    "mcp-oauth": "login",
    "enable": "enable",
    "disable": "disable",
    "recovery-codes": "update",
}


async def _lock_mfa_state(db: AsyncSession, user: User) -> None:
    """Re-read *user* under a row lock held until the caller commits.

    Every route that reads the two-factor state and then writes it takes this
    first, so its preconditions (enabled or not, which secret, which step or
    recovery codes are spent) are checked against the latest committed row and
    no concurrent request can change them before the route commits. Pending
    changes are flushed first so the refresh cannot discard them; taking the
    lock again in the same transaction is a no-op.
    """
    await db.flush()
    await db.refresh(user, with_for_update=True)


async def _verify_second_factor(
    db: AsyncSession,
    user: User,
    code: str,
    *,
    request: Request,
    context: str,
    allow_recovery: bool = True,
) -> str:
    """Verify *code* as a TOTP code or (optionally) a recovery code.

    Returns the factor kind that succeeded (``"totp"`` / ``"recovery"``) and
    persists the associated state — the spent time-step, or the burned recovery
    code — as part of the caller's transaction, which the caller must commit
    before it answers (``get_db`` commits only after the response is sent).
    Raises HTTP 401 on failure, after writing a failure entry to the audit log.

    Both branches return the SAME 401 detail. Distinguishing "wrong code" from
    "that recovery code was already used" would tell an attacker which of the
    two guesses was closer, and telling a user their recovery code is spent is
    not worth handing that out.

    Audit entries are attributed to the calling flow via
    ``_AUDIT_ACTION_BY_CONTEXT`` — see the note there on why the flow belongs in
    ``action`` and not only in ``details``.
    """
    from app.services.mfa import consume_recovery_code, verify_totp

    audit_action = _AUDIT_ACTION_BY_CONTEXT.get(context, "login")

    # A concurrent attempt with the same code waits here and checks it against
    # the step or recovery code this one consumed, never against the state
    # both read before either wrote.
    await _lock_mfa_state(db, user)

    secret = await _load_user_secret(user)
    if secret is not None:
        step = verify_totp(
            secret, code, last_used_step=getattr(user, "mfa_last_used_step", None)
        )
        if step is not None:
            # Persisting the step is what makes the guard real — an observed
            # code stays valid for the rest of its window otherwise.
            user.mfa_last_used_step = step
            await db.flush()
            return "totp"

    if allow_recovery:
        updated = consume_recovery_code(
            getattr(user, "mfa_recovery_codes", None), code
        )
        if updated is not None:
            # Reassign rather than mutate: ``mfa_recovery_codes`` is a plain
            # JSON column with no mutation tracking, so an in-place edit would
            # never be flushed and the code would silently stay spendable.
            user.mfa_recovery_codes = updated
            await db.flush()
            from app.services.mfa import count_unused_recovery_codes

            remaining = count_unused_recovery_codes(updated)
            await log_audit(
                db,
                user=user,
                action=audit_action,
                entity_type="mfa",
                entity_id=user.id,
                entity_name=user.full_name,
                summary=(
                    f"Used a recovery code ({user.email}) — {remaining} remaining"
                ),
                details={"context": context, "recovery_codes_remaining": remaining},
                request=request,
            )
            return "recovery"

    await log_audit(
        db,
        user=user,
        action=audit_action,
        entity_type="mfa",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Failed second-factor verification ({user.email})",
        details={"context": context, "outcome": "invalid_code"},
        request=request,
    )
    # Commit the failure record explicitly: the 401 below aborts the request
    # before ``get_db()``'s closing commit runs, so without this the audit trail
    # would record successes only — precisely the half that needs no alerting.
    await db.commit()
    raise MfaInvalidCodeError()


async def begin_mfa_challenge(
    db: AsyncSession, user: User, *, request: Request, login_method: str
) -> str:
    """Record that *user*'s password was accepted and mint the code challenge.

    The first half of a password sign-in for an account with two-factor
    enabled, shared by ``POST /auth/login`` and the MCP OAuth consent page
    so both withhold the session the same way. ``login_method`` names
    the surface in the audit entry; the challenge itself says ``local``, since
    a password proved the first factor either way.
    """
    from app.services.mfa import create_challenge_token

    # Audited as its own event: an attacker who has the password but not
    # the device produces a trail of these with no matching "Logged in",
    # which is exactly the signal a defender wants to alert on.
    await log_audit(
        db,
        user=user,
        action="login",
        entity_type="mfa",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Password accepted, awaiting second factor ({user.email})",
        details={"login_method": login_method, "stage": "password"},
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return create_challenge_token(user.id, method="local")


async def complete_mfa_challenge(
    db: AsyncSession,
    mfa_token: str,
    code: str,
    *,
    request: Request,
    context: str = "login",
) -> MfaCompletion:
    """Check a challenge token plus a code; return the user it signs in.

    The one completion of a two-factor sign-in, behind ``POST /auth/mfa/verify``
    and the MCP OAuth consent page, so a second surface can never hand
    out a credential on a weaker check. Raises HTTP 401 on any failure (a wrong
    code as ``MfaInvalidCodeError``) and 429 past the rate limit. The consumed
    step or recovery code is flushed, not committed: the caller commits before
    it hands anything out.
    """
    await enforce_rate_limit(request, "mfa-verify", limit=10, window_seconds=60)

    from app.services.mfa import MfaChallengeError, decode_challenge_token

    try:
        challenge = decode_challenge_token(mfa_token)
    except MfaChallengeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = await db.get(User, challenge.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    await _lock_mfa_state(db, user)
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # A password change between the two halves of the login invalidates the
    # challenge, exactly as it invalidates outstanding session tokens in
    # ``get_current_user_ungated``. Without this a challenge minted with the old
    # password would still complete after a reset. Harmless on the SSO path,
    # where ``password_changed_at`` is NULL for accounts that never had one.
    if (
        user.password_changed_at is not None
        and challenge.issued_at < int(user.password_changed_at.timestamp())
    ):
        raise HTTPException(
            status_code=401, detail="Invalid or expired verification session"
        )
    factor: str | None = None
    if not getattr(user, "mfa_enabled", False):
        # MFA was turned off (or reset by an admin) while the challenge was in
        # flight. The first factor already succeeded, so completing the login is
        # correct — there is simply no second factor left to check.
        logger.info(
            "MFA challenge completed for user %s whose MFA is no longer enabled "
            "— issuing a session on the strength of the first factor alone.",
            user.id,
        )
    else:
        factor = await _verify_second_factor(
            db, user, code, request=request, context=context
        )
    return MfaCompletion(user=user, challenge=challenge, factor=factor)


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Begin TOTP enrollment: mint a secret and return the QR to scan.

    Re-callable while enrollment is unconfirmed — a user who lost the tab or
    scanned into the wrong app just starts over, and each call replaces the
    pending secret. Once MFA is active the endpoint refuses: rotating a live
    secret has to go through disable → setup so the user is never left holding a
    device whose codes no longer work.
    """
    from app.services.mfa import (
        TOTP_ALGORITHM,
        TOTP_DIGITS,
        TOTP_PERIOD_SECONDS,
        encrypt_secret,
        generate_secret,
        provisioning_uri,
        qr_data_uri,
    )

    await _lock_mfa_state(db, user)
    if getattr(user, "mfa_enabled", False):
        raise HTTPException(
            status_code=409,
            detail=(
                "Two-factor authentication is already enabled. Disable it first "
                "to enroll a new device."
            ),
        )

    await enforce_rate_limit(
        request, "mfa-setup", limit=10, window_seconds=60, bucket=str(user.id)
    )

    secret = generate_secret()
    user.mfa_secret = encrypt_secret(secret)
    # A fresh secret starts with a clean replay guard, otherwise a step spent
    # against a previous enrollment would reject the first code of this one.
    user.mfa_last_used_step = None
    await db.flush()
    # Committed before the secret leaves: ``enable`` checks the code against
    # the stored secret, so it must not be able to read the previous one.
    await db.commit()

    uri = provisioning_uri(secret, account_name=user.email)
    return MfaSetupResponse(
        secret=secret,
        otpauth_uri=uri,
        qr_code=qr_data_uri(uri),
        digits=TOTP_DIGITS,
        period=TOTP_PERIOD_SECONDS,
        algorithm=TOTP_ALGORITHM,
    )


@router.post("/mfa/enable", response_model=MfaRecoveryCodesResponse)
async def mfa_enable(
    body: MfaCodeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm enrollment with a live code, then activate MFA.

    Returns the recovery codes — the only time they are ever readable.
    """
    from app.services.mfa import generate_recovery_codes

    await _lock_mfa_state(db, user)
    if getattr(user, "mfa_enabled", False):
        raise HTTPException(
            status_code=409, detail="Two-factor authentication is already enabled."
        )
    if not getattr(user, "mfa_secret", None):
        raise HTTPException(
            status_code=409,
            detail="Start enrollment first (POST /auth/mfa/setup).",
        )

    await enforce_rate_limit(
        request, "mfa-enable", limit=10, window_seconds=60, bucket=str(user.id)
    )

    # ``allow_recovery=False``: no recovery codes exist yet, and the whole point
    # of this call is to prove the *authenticator* was enrolled correctly.
    await _verify_second_factor(
        db, user, body.code, request=request, context="enable", allow_recovery=False
    )

    plaintext, stored = generate_recovery_codes()
    user.mfa_enabled = True
    user.mfa_confirmed_at = datetime.now(UTC)
    user.mfa_recovery_codes = stored
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="enable",
        entity_type="mfa",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Enabled two-factor authentication ({user.email})",
        details={"method": "totp", "recovery_codes_issued": len(plaintext)},
        request=request,
    )
    # Commit before answering (``get_db`` commits after the response): the
    # spent step and the recovery codes handed out must be on record, and it
    # releases the row lock ``_lock_mfa_state`` took.
    await db.commit()
    return MfaRecoveryCodesResponse(recovery_codes=plaintext)


@router.post("/mfa/verify", response_model=TokenResponse)
async def mfa_verify(
    body: MfaVerifyRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    """Exchange a login challenge token plus a code for a real session token.

    The completion step for BOTH login paths: a local password login
    and an OIDC/SAML callback both mint the same challenge, so both finish here.
    The token carries which one it was, purely so the audit entry names it.

    Unauthenticated by design — the caller holds no session yet; that is the
    entire point. The challenge token is what carries "the first factor was
    already proven", and it is scoped so it can do nothing else.
    """
    user, challenge, factor = await complete_mfa_challenge(
        db, body.mfa_token, body.code, request=request
    )

    await log_audit(
        db,
        user=user,
        action="login",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Logged in ({user.email})",
        # False only when two-factor was turned off mid-flight: no code was checked.
        details={"login_method": challenge.method, "mfa": factor is not None},
        request=request,
    )
    # Commit before the session is issued (``get_db`` commits after the
    # response): the consumed step or recovery code must be on record first,
    # or a failed teardown commit would leave the code usable again.
    await db.commit()
    return TokenResponse(access_token=_build_token(user))


@router.post("/mfa/disable", response_model=MfaStatusResponse)
async def mfa_disable(
    body: MfaDisableRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn MFA off. Requires the account password AND a valid code."""
    await _lock_mfa_state(db, user)
    if not getattr(user, "mfa_enabled", False):
        raise HTTPException(
            status_code=409, detail="Two-factor authentication is not enabled."
        )

    await enforce_rate_limit(
        request, "mfa-disable", limit=10, window_seconds=60, bucket=str(user.id)
    )

    # Only demand a password from accounts that have one. Checking it first
    # keeps a wrong password from burning a recovery code in the branch below.
    if user.hashed_password and not (
        body.password
        and await verify_password_async(body.password, user.hashed_password)
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    await _verify_second_factor(db, user, body.code, request=request, context="disable")

    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_confirmed_at = None
    user.mfa_last_used_step = None
    user.mfa_recovery_codes = None
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="disable",
        entity_type="mfa",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Disabled two-factor authentication ({user.email})",
        details={"method": "totp"},
        request=request,
    )
    # Commit before answering (``get_db`` commits after the response), which
    # also releases the row lock ``_lock_mfa_state`` took.
    await db.commit()
    return _build_mfa_state(user)


@router.post("/mfa/recovery-codes", response_model=MfaRecoveryCodesResponse)
async def mfa_regenerate_recovery_codes(
    body: MfaCodeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Issue a fresh set of recovery codes, invalidating the previous one.

    Requires a live TOTP code specifically (``allow_recovery=False``): letting a
    recovery code mint ten more would turn a single leaked code into permanent
    access that survives every rotation.
    """
    from app.services.mfa import generate_recovery_codes

    await _lock_mfa_state(db, user)
    if not getattr(user, "mfa_enabled", False):
        raise HTTPException(
            status_code=409, detail="Two-factor authentication is not enabled."
        )

    await enforce_rate_limit(
        request, "mfa-recovery-codes", limit=5, window_seconds=60, bucket=str(user.id)
    )

    await _verify_second_factor(
        db,
        user,
        body.code,
        request=request,
        context="recovery-codes",
        allow_recovery=False,
    )

    plaintext, stored = generate_recovery_codes()
    user.mfa_recovery_codes = stored
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="mfa",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Regenerated two-factor recovery codes ({user.email})",
        details={"recovery_codes_issued": len(plaintext)},
        request=request,
    )
    # Commit before answering (``get_db`` commits after the response): the
    # previous set must stop working before the new one is shown, and this
    # releases the row lock ``_lock_mfa_state`` took.
    await db.commit()
    return MfaRecoveryCodesResponse(recovery_codes=plaintext)
