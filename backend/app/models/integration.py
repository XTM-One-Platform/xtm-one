# SPDX-License-Identifier: Apache-2.0

"""Integration model – full credential storage (encrypted at rest).

Each integration stores everything needed to connect to an external service:
  - OAuth app credentials (client_id, client_secret) – per-integration, not global
  - OAuth user tokens (access_token, refresh_token)
  - API keys (for non-OAuth services)
  - Extra config (JSON) for provider-specific settings

Supports multiple integrations of the same provider per user, each with
a unique name (e.g. "M365", "Gmail").

Company-managed integrations (is_company_managed=True) are shared with all
users but only admin-editable. A company-managed (or group / user shared) OAuth
integration shares the account it is connected with, unless its owner opted
out (``oauth_share_acknowledged``) - then each person authorizes their
own account on it (``credential_mode = "per_user"``).

All secrets are Fernet-encrypted before storage.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.integration_tool_slug import derive_tool_slug


def _default_tool_slug(context) -> str | None:
    """The insert default of ``tool_slug``: the slug the display name yields.

    Without an insert context (a caller filling defaults by hand) there is no
    name to derive from: ``None``, which the tool build reads as "derive it".
    """
    if context is None:
        return None
    params = context.get_current_parameters()
    return derive_tool_slug(params.get("name"), params.get("provider") or "", params.get("id") or "")


class Integration(Base):
    __tablename__ = "integrations"
    # ``user_id`` is indexed inline via ``index=True``. The rest support
    # catalog/activity queries (provider, status), company-managed visibility
    # filtering, group-share membership lookups (GIN over the JSON array) and
    # the OAuth token-refresh sweep (partial index over refreshable rows only).
    __table_args__ = (
        Index("ix_integrations_provider", "provider"),
        Index("ix_integrations_status", "status"),
        Index("ix_integrations_is_company_managed", "is_company_managed"),
        Index(
            "ix_integrations_shared_group_ids_jsonb_gin",
            text("(shared_group_ids::jsonb)"),
            postgresql_using="gin",
        ),
        # Per-user grant lookup, parity with the group index above.
        Index(
            "ix_integrations_shared_user_ids_jsonb_gin",
            text("(shared_user_ids::jsonb)"),
            postgresql_using="gin",
        ),
        Index(
            "ix_integrations_status_auth_type_token_expires",
            "status",
            "auth_type",
            "token_expires_at",
            postgresql_where=text("refresh_token_enc IS NOT NULL"),
        ),
        Index(
            "ix_integrations_relay_group_id",
            "relay_group_id",
            postgresql_where=text("relay_group_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # The relay group every HTTP call of this integration goes through:
    # its tools, its connectivity test, its event pollers and its
    # OAuth refresh. NULL = direct egress from the platform. RESTRICT: a group
    # in use cannot be deleted out from under the integrations routed to it,
    # which would silently send their traffic direct.
    relay_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "relay_groups.id",
            ondelete="RESTRICT",
            name="fk_integrations_relay_group_id_relay_groups",
        ),
        nullable=True,
    )

    # Company-managed integrations are shared with all users; only admins can edit.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Built-in integrations are seeded by the platform and cannot be edited or deleted.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Group sharing: list of Group UUIDs this entity is shared with.
    # Members of these groups can see (but not edit) the entity.
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user sharing + company-managed narrowing. Same shape as the
    # group columns above, keyed by User UUID; see ``app.models.agent`` for the
    # rationale and ``app.api.v1.helpers.user_visible_filter`` for enforcement.
    #
    # Note for this model specifically: a user grant shares an OAuth
    # integration like a group does. ``app.services.integration_credentials``
    # lets the grantee use its token once the owner's consent is recorded
    # (``oauth_share_acknowledged``, the sharing choice its owner made); where
    # that rule applies, visibility alone never reaches a token without it.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # The grants above that an agent's sharing gave (#2715):
    # ``{"user:<id>" | "group:<id>": [agent ids]}``. A grant with no origin was
    # given by hand and no agent unshare removes it; one several agents gave
    # goes with the last of them (``app.services.agent_integration_shares``).
    agent_share_origins: Mapped[dict[str, list[str]] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # OAuth tokens are personal to whoever connected them; this records the
    # owner's consent to share theirs. Sharing the integration (group,
    # per-user grant, company-managed) records it when its holder chooses to -
    # the UI's default - and everyone it is shared with then uses the connected
    # account; the opt-out removes the stored token instead, and a REST share
    # that makes no choice keeps the baseline. Written only by
    # ``integration_credentials.apply_oauth_share_consent`` and the OAuth
    # callback; ``app.services.integration_credentials`` is the one rule a
    # gated token reader applies to decide whether a non-owner may use it, a
    # binding being used as bound at run time.
    oauth_share_acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Who connected the stored OAuth token when it is NOT the owner's:
    # an owner transfer keeps the previous owner's token on the row. NULL means
    # the owner's own - every token stored by the owner's authorization. Shown
    # on the integration so the owner can reconnect with their own account;
    # written only by ``integration_credentials.record_token_owner_on_transfer``
    # and cleared by the OAuth callback. No foreign key: nothing reads it as a
    # join, and a deleted user must not take the row's history with it.
    oauth_token_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, default=None
    )
    # ``shared`` (the account above is lent) or ``per_user`` (kept by its holder;
    # everyone else connects their own). Stored, so re-authorizing never
    # changes it; written only by ``apply_oauth_share_consent``.
    credential_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="shared", default="shared"
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # google | microsoft | github | slack | notion | etc.
    auth_type: Mapped[str] = mapped_column(String(20), nullable=False, default="oauth")
    # oauth | api_key | bot_token | platform_managed

    # Human-readable name
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="Default")

    # The tool-name suffix of this instance: when an agent binds several
    # integrations of one provider, its tools are exposed as ``<tool>__<slug>``.
    # Derived once from ``name`` at insert (``derive_tool_slug``) and never on a
    # rename, so the tool names agents, skills and assignment gates cite survive
    # one; changed only explicitly through the integrations API. NULL (a row the
    # backfill has not reached) falls back to the name at tool build.
    tool_slug: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=_default_tool_slug
    )

    # Optional description — surfaced to agents as context for integration tools
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- OAuth app credentials (per-integration, NOT from env) ---
    client_id_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whether the two fields above were copied from the admin's OAuth defaults
    # (``oauth_defaults``) rather than supplied for this integration:
    # an integration on the admin's app gets its events through the
    # provider-level webhook, one on its own app needs its own. The copy
    # leaves nothing else behind. NULL = not recorded (rows created before the
    # column existed), which readers treat as the provider-wide
    # ``has_oauth_defaults`` flag; every insert records False unless the create
    # path copied the defaults, and replacing the client id records False.
    uses_admin_oauth_credentials: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=False
    )

    # --- OAuth user tokens ---
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- API key (for non-OAuth providers) ---
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Extra config (JSON, for provider-specific settings) ---
    # e.g. {"phone_number_id": "...", "verify_token": "..."} for WhatsApp
    # e.g. {"signing_secret": "..."} for Slack bot
    # e.g. {"tenant_id": "..."} for Microsoft
    extra_config_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OAuth metadata
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The scopes the stored token was granted, space-separated, from the token
    # endpoint's ``scope`` field (app.integrations.granted_scopes); NULL when
    # the provider did not say.
    granted_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provider-specific OAuth URLs (optional overrides)
    # Allows each integration to use different OAuth endpoints (e.g. different tenant)
    authorize_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    token_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Tags – free-form labels for filtering and organisation
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Scope config: disambiguates when multiple integrations of the same
    # provider exist.  Set programmatically (platform registration / seeding),
    # not user-editable via UI.
    #   {"match_key": "base_url",
    #    "resolve_from_header": "X-Platform-URL",
    #    "resolve_from_platform": "platform_url"}
    scope_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="disconnected")

    # The result of the last connectivity test (POST /integrations/{id}/test):
    # ``untested`` once a credential is typed and never tested, ``ok`` or
    # ``error`` after a test. NULL on rows from before it, read as before.
    # Display only: ``status`` keeps deciding whether the tools can run.
    connection_test_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Why the status last left ``connected`` (the refresh sweep's verdict,
    # #1665) and when that verdict was recorded; both NULL on rows from before
    # them. Read only while ``status`` is ``error`` / ``reauthorize``
    # (``app.services.integration_status``): a reconnect makes them history.
    status_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status_reason_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Whether this integration's tools pause for human approval.
    #
    # A single toggle for the whole integration, not per tool: there are ~2,350
    # integration tool definitions across ~110 providers, and per-tool curation
    # is deferred until the end-to-end flow has been proven on one of our own
    # integrations. Defaults to True so a newly connected integration gates
    # rather than silently acting on a third-party system.
    #
    # ``server_default`` as well as ``default``, matching the neighbouring
    # ``oauth_share_acknowledged`` and for the same reason: a Python-side
    # default only fires on an ORM flush, so a raw INSERT, a bulk load, or the
    # ``_ensure_model_columns`` safety net would otherwise hit the NOT NULL
    # constraint — and for a gating flag, "no value" must resolve to gated at the
    # database level, not just when SQLAlchemy happens to be in the path.
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )

    # The Email signature setting of an integration whose tools send email
    # (``services/email_signature_settings.py``): whether what it sends
    # is signed, and the signature integration read for it. NULL = picked when
    # the tools are built (the one bound alongside, else the one its user may
    # use). A plain column, no foreign key: a deleted signature integration
    # reads as gone. An agent's binding may override both
    # (``Agent.integration_signatures``).
    signature_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    signature_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, default=None
    )

    # Usage tracking – incremented on every tool call by an agent
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    last_called_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
