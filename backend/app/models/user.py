# SPDX-License-Identifier: Apache-2.0

"""User model – authentication, role, preferences."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    and_,
    not_,
    or_,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # SSO login (`auth_sso._get_or_create_sso_user`) does a hot-path
        # equality lookup on `(sso_provider, sso_subject)` for every
        # authentication. Without an index this degrades to a sequential
        # scan under load and trips the PostgreSQL `statement_timeout`.
        Index(
            "ix_users_sso_provider_subject",
            "sso_provider",
            "sso_subject",
        ),
        # An email names one account whatever its case. Every path but SSO
        # lowercased it and the plain UNIQUE on ``email`` is case-sensitive,
        # so this is what keeps ``jane@x`` and ``Jane@x`` apart.
        Index("ix_users_email_lower", text("lower(email)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(320), unique=True, nullable=False, index=True
    )
    hashed_password: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # nullable for SSO-only users
    # Set whenever the password changes; local JWTs issued before this instant
    # are rejected (see app.api.deps.get_current_user), invalidating old sessions.
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # e.g. "CEO", "CTO"
    # The department the identity source reports: the SSO attribute
    # named in ``PlatformSettings.department_settings``, refreshed at every
    # sign-in, or an administrator import. The first link of the department
    # resolution chain (``services/departments.py``).
    directory_department: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    timezone: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # IANA tz, e.g. "America/New_York"
    theme: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # "dark" (default/NULL) or "light"
    # UI language override. "auto" (default) inherits the platform language
    # (PlatformSettings.platform_language), which itself defaults to browser
    # detection. Concrete values are locale codes like "en-us", "fr-fr".
    language: Mapped[str] = mapped_column(
        String(10), nullable=False, default="auto", server_default="auto"
    )
    # CTEM command center (XTM One home) per-user preferences.
    # ctem_vertical: which exposure vertical the dashboard shows (cyber/fimi/fraud).
    # ctem_splash_seen: once the user has entered the command center, the CE
    # onboarding splash is never shown again on subsequent logins.
    ctem_vertical: Mapped[str] = mapped_column(
        String(20), nullable=False, default="cyber", server_default="cyber"
    )
    ctem_splash_seen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # ctem_dashboard_layouts: the command center this person built, per vertical
    # (``{vertical: {"items": [{i, x, y, w, h}], "hidden": [id]}}``, widget ids of
    # ``services/dashboard_layouts.CTEM_DASHBOARD_WIDGETS``). NULL, or a
    # vertical it does not name, = the default layout.
    ctem_dashboard_layouts: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # ctem_fleet_invite_dismissed: what the person saw when they closed the
    # command center's "Add agentic work to this page" card (#9566),
    # ``{"fleets": [slug], "modules": [name]}``; the card comes back when a
    # fleet or a module outside these lists appears. NULL = never closed.
    ctem_fleet_invite_dismissed: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # organization_context_notice_dismissed: the organization context fields
    # that were missing when this administrator closed the command center's
    # "organization context is incomplete" notice (#11586), ``[path]``. The
    # notice comes back only when a field outside this list becomes missing.
    # NULL = never closed. Briefs keep their own notice, never dismissible.
    organization_context_notice_dismissed: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # ctem_dashboard_fleet_ids: the fleets the command center's fleets row shows
    # for this person, in order (#11464): built-in or their own. NULL = the
    # built-in CTEM fleets; [] = none (every one hidden).
    ctem_dashboard_fleet_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # copilot_dashboard_fleet_ids: the fleets the Copilot home's Fleets widget
    # shows for this person, in order (#12063), independent of the command
    # center's. NULL = every fleet they can see; [] = none (every one hidden).
    copilot_dashboard_fleet_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # copilot_dashboard_layout: the Copilot dashboard this person built
    # (``{"items": [{i, x, y, w, h}], "hidden": [id]}``, widget ids of
    # ``services/dashboard_layouts.COPILOT_DASHBOARD_WIDGETS``). NULL = the
    # default layout.
    copilot_dashboard_layout: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # Terms of Use acceptance is NOT per-user: it binds the whole
    # organisation and is stored ONCE on the ``PlatformSettings`` singleton
    # (``tou_accepted_version`` / ``tou_accepted_at`` / ``tou_accepted_by_user_id``),
    # with the audit log as the who/when/version trail. The former per-user
    # ``tou_accepted_version`` / ``tou_accepted_at`` columns are not mapped:
    # migration c2797toudrop1 drops them with the ``terms_acceptances``
    # table, unless the upgrade running it may have started on 1.260804.0 -
    # 1.260809.0, whose replicas still read them during that rollout; they
    # then stay, inert.
    # TOTP two-factor authentication. Only ``mfa_enabled`` gates the
    # login flow: a secret can exist while the user is mid-enrollment (scanned
    # the QR but never confirmed a code), and that half-finished state must NOT
    # lock them out. ``POST /auth/mfa/enable`` is what flips the flag, and only
    # after a code minted from this very secret verified.
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Base32 TOTP secret, Fernet-encrypted at rest (``security.encrypt_value``)
    # exactly like integration credentials — a database dump must not hand out
    # working second factors. Never returned by the API after enrollment.
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    mfa_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Highest TOTP time-step already spent by a successful verification.
    # Replay guard: a 6-digit code stays valid for its whole 30 s step (plus the
    # drift window), so without this an attacker who observes one code — over a
    # shoulder, in a phishing proxy, in a log — can replay it for the remainder
    # of that window. Verification refuses any step <= this value. BigInteger
    # because the step counter is unix-time/30, and Postgres INTEGER would
    # overflow in 2038.
    mfa_last_used_step: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, default=None
    )
    # Single-use recovery codes, stored as SHA-256 hashes (same rationale as
    # ``security.hash_token``: the raw codes carry 100+ bits of entropy and are
    # looked up by exact value, so bcrypt buys nothing over a fast digest).
    # Shape: ``[{"hash": "<sha256 hex>", "used_at": null | "<iso8601>"}]``.
    # Used codes are kept (not deleted) so the audit trail can show how many of
    # the set have been burned.
    mfa_recovery_codes: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # Per-user CTEM analyst overrides (the agentic personalization tier). Mirror
    # the PlatformSettings shape but take precedence over it: a user can point a
    # CTEM stage (or the Exposure Briefing Analyst) at their OWN agent per
    # vertical, overriding the admin-set platform default. Resolution is
    # user override -> platform default -> seeded analyst (see ctem_metrics).
    # ctem_stage_agent_ids: {vertical: {stage: "<agent uuid>"}}.
    # ctem_briefing_agent_ids: {vertical: "<agent uuid>"}.
    # NULL/missing = inherit the platform default. EE-gated.
    ctem_stage_agent_ids: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    ctem_briefing_agent_ids: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When and by what the account was last deactivated (``admin``, ``scim``,
    # ``sso``, ``system``; ``services/user_deactivation.py``); cleared on
    # reactivation. The cascade's record is ``user_deactivations``.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    deactivation_source: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    # Set by a SCIM DELETE: the identity provider removed the account. A soft
    # state on top of the deactivation; nothing is purged.
    deprovisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    # A break-glass or service account an administrator flagged: the agent
    # user-admin tools (``builtin_tools/user_admin.py``) refuse to deactivate it.
    deactivation_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Role: "admin" or "user"
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")

    # SSO / cross-platform provider that created this account (null = local).
    # Stores short labels for SSO ("oidc", "saml") or full issuer URLs for
    # cross-platform JWT provisioning (e.g. "https://opencti.example.com").
    sso_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sso_subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The identity provider's id of the account when SCIM provisions it
    # (``externalId``, ``api/v1/scim.py``); NULL for an account SCIM never saw.
    scim_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    # When SCIM first created, linked or updated the account, ``externalId``
    # or not (``externalId`` is optional): its identity provider manages it
    # from then on (:func:`provisioning_source`). NULL for one SCIM never saw.
    scim_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    # LLM preferences (per-user overrides)
    preferred_llm_provider: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    preferred_llm_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    preferred_llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Default knowledge base – always available to all user's agents (in addition to agent-specific KBs)
    default_knowledge_base_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Default MCP servers – always available to all user's agents
    default_mcp_server_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Default chat agent – replaces General Assistant when user lands in chat
    default_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    #: Who set ``default_agent_id``: ``user`` (the person, from their profile)
    #: or ``platform`` (provisioning wired the CTEM Assistant for someone with
    #: none). NULL on a row written before the column existed: read by
    #: ``platform_insights.default_agents``, which tells the two apart there.
    default_agent_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: When ``default_agent_id`` was last set; NULL when unknown.
    default_agent_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The custom dashboard this person chose as their dashboard: shown on
    # ``/dashboard`` (and on the home page when it is the dashboard) instead of
    # the platform's own. Only one of their own dashboards (the preferences
    # route checks it); ``SET NULL`` when it is deleted, which brings back the
    # platform's dashboard. ``use_alter``: ``custom_dashboards.user_id`` already
    # points the other way, so this side is added after both tables exist.
    home_dashboard_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "custom_dashboards.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_users_home_dashboard_id_custom_dashboards",
        ),
        nullable=True,
        index=True,
    )

    # Per-user General Assistant tool/resource overrides.
    # When NULL → platform defaults from GeneralAssistantConfig are used.
    # When set (even to empty list/dict) → the user's choice takes precedence.
    ga_builtin_tool_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    ga_custom_tool_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    ga_integration_bindings: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    ga_mcp_server_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    ga_knowledge_base_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # The browser profiles the person's General Assistant may open with its
    # ``browser`` tool. The person's own choice only, never a platform
    # default: a profile is a login. NULL / empty = none.
    ga_browser_profile_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user channel agent overrides for private/DM conversations.
    # Structure: {"<channel_type>": "<agent_id>", ...}  e.g. {"slack": "uuid", "teams": "uuid"}
    # When set, the user's agent is prioritised for DM conversations on that channel.
    channel_agent_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # MCP endpoint – user-selected agents to expose on /mcp/user.
    # When NULL → all visible agents are exposed (default).
    # When set (even to empty list) → only listed agents are exposed.
    mcp_exposed_agent_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Allow-all mode for the per-tool approval gate: the wholesale form
    # of answering "Yes, always" to every tool. When on, this user's runs and
    # chats never pause for tool approval; each call is recorded with an
    # explicit ``auto_run_user_allows_all`` outcome. Standing per-tool
    # approvals are kept (not deleted) so switching back restores the previous
    # posture.
    approve_all_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Platform Health opt-out (#8254): Platform Health reads none of this
    # user's memory lanes and conversations (``services/health/consent.py``).
    health_content_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # The Platform Health companion (#8261): ``{"enabled": bool, "pet": slug}``,
    # off when unset (``services/health/companion.py``).
    concierge_companion: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # The bell shows the work items waiting for this person (#9437), on by
    # default; off = none of them in the bell (the dashboard notice and the
    # Operations "Waiting for me" view still list them).
    notify_waiting_work_items: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # The composer's reply suggestion after a chat turn
    # (``services/chat_reply_suggestions.py``), on by default; off = none is
    # generated for this person, whatever the platform switch says.
    reply_suggestions_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Favorite agents – the ids this user starred on /agents. Purely a per-user
    # UI preference: the agent list pins them to the top of whatever sort/filter
    # is active, and orders the pinned block by that same sort — so this is an
    # insertion-ordered set, not a display order. NULL or an empty list both
    # mean "no favorites" (unlike the selection columns above, there is no third
    # "inherit" state to encode).
    favorite_agent_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # OAuth-share reminders this user closed (kept server-side).
    # The dashboard reminds an OWNER that their personal OAuth token is shared
    # (group / user / company-wide with ``oauth_share_acknowledged``) until they close
    # it; this is the set of THEIR integration ids they closed it for. Stored on
    # the user rather than in the browser so the choice follows them across
    # devices. ``PUT /settings/preferences`` only keeps ids naming an OAuth
    # integration this user owns whose token is currently acknowledged as
    # shared, and ``integrations._forget_oauth_share_dismissal`` drops an id the
    # moment that acknowledgement is reset, so sharing the same integration
    # again brings the reminder back. NULL and [] both mean "nothing closed".
    oauth_share_dismissed_integration_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # The workspace groups of the chat sidebar this user opened (#9179): the
    # group keys (workspace ids) shown expanded, every other group collapsed.
    # Stored on the user so the choice follows them across devices. NULL = never
    # chosen: every group starts collapsed (seeded workspaces would otherwise
    # push the unfiled conversations below the fold). A pure UI preference.
    chat_sidebar_expanded_workspaces: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Cross-channel identity linking: maps external platform identifiers to
    # this Copilot user, enabling recognition across channels without email.
    # Structure: {"<channel_type>:<platform_id>": true, ...}
    #   e.g. {"slack:U01ABC123": true, "teams:29:1abc-def": true,
    #          "whatsapp:+1234567890": true, "telegram:123456789": true}
    # The router checks this map during user resolution as a fallback when
    # email-based lookup fails.  Entries are added via the admin API or
    # auto-linked when a user first interacts on an external channel.
    channel_identities: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # Quota configuration (user-level overrides — highest priority).
    # Same JSON structure as GeneralAssistantConfig.quota_config.
    # NULL = no quotas at user level (inherits from group or platform).
    quota_config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def is_platform_admin(user: object) -> bool:
    """Whether *user* is the ``ADMIN_EMAIL`` account, the platform administrator provisioned at startup.

    The one rule: ``GET /users`` reports it (``is_platform_admin``) and the ID
    token carries it as ``xtm_super_admin``. It follows the email, so whoever
    holds that address is the platform administrator: no email change moves it
    (``admin_email_change_refusal``).
    """
    from app.core.config import settings

    return getattr(user, "email", None) == settings.admin_email


def admin_email_change_refusal(user: object | None, email: str) -> str | None:
    """Why *user*'s email may not become *email*, or ``None`` when it may.

    The one rule for every email change (``PUT /users/{user_id}/email``, SCIM,
    ``POST /settings/email-change``):
    platform administrator standing follows ``ADMIN_EMAIL`` and the boot finds
    the account by it, so the account holding it keeps it and no other account
    takes it. *user* is ``None`` for an account being created; *email* is the
    new, normalized address, different from the current one.
    """
    from app.core.config import settings

    if user is not None and is_platform_admin(user):
        return "The platform admin account's email is set by ADMIN_EMAIL and cannot be changed"
    if settings.admin_email and email == settings.admin_email.strip().lower():
        return "This address is the platform admin's, set by ADMIN_EMAIL: it cannot be given to an account"
    return None


def provisioning_source(user: object) -> str:
    """``scim`` for an account its identity provider manages, else ``sso`` or ``local``.

    The one rule (the users API, the user admin tools, the self-service email
    change): SCIM manages an account it created, linked or updated, which
    ``scim_linked_at`` records whether or not the provider sent the optional
    ``externalId``. :func:`provisioning_source_clause` is its SQL form.
    """
    if getattr(user, "scim_external_id", None) or getattr(user, "scim_linked_at", None):
        return "scim"
    return "sso" if getattr(user, "sso_provider", None) else "local"


def provisioning_source_clause(source: str) -> ColumnElement[bool]:
    """The accounts :func:`provisioning_source` reads as *source* (``scim``, ``sso``, ``local``), as a filter on ``User``."""
    scim = or_(User.scim_external_id.is_not(None), User.scim_linked_at.is_not(None))
    if source == "scim":
        return scim
    sso = User.sso_provider.is_not(None)
    return and_(not_(scim), sso if source == "sso" else not_(sso))
