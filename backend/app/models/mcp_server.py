# SPDX-License-Identifier: Apache-2.0

"""MCPServer model – stores user-configured MCP server connections."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MCPServer(Base):
    """Persisted configuration for an MCP server.

    Each user can register multiple MCP servers. An agent uses the servers
    its own ``Agent.mcp_server_ids`` names; the server carries no list of
    agents.

    Environment variables (``env_enc``) are stored **encrypted** because they
    typically contain secrets like ``GITHUB_TOKEN``.

    Remote transports (sse / streamable_http) support two authentication modes:
      * **headers** (default) – user provides static HTTP headers (e.g. ``Authorization: Bearer …``).
      * **oauth** – the standard OAuth 2.0 authorization-code flow.  The
        resulting access token is automatically injected as a Bearer header at
        runtime.  Client credentials are optional: with no ``client_id_enc``
        the platform discovers the authorization server and registers itself
        dynamically (RFC 7591) on the first authorization, so a server can be
        connected from its URL alone.
    """

    __tablename__ = "mcp_servers"
    # ``user_id`` is indexed inline. The company-managed flag is filtered on by
    # every list endpoint's three-tier visibility query.
    __table_args__ = (
        Index("ix_mcp_servers_is_company_managed", "is_company_managed"),
        Index(
            "ix_mcp_servers_relay_group_id",
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

    # The relay group a remote transport (sse / streamable_http) connects
    # through. NULL = direct. A stdio server runs on the platform and
    # is never routed. RESTRICT: a group in use cannot be deleted.
    relay_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "relay_groups.id",
            ondelete="RESTRICT",
            name="fk_mcp_servers_relay_group_id_relay_groups",
        ),
        nullable=True,
    )

    # Human-friendly label shown in the UI
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Transport type: "stdio" | "sse" | "streamable_http"
    transport: Mapped[str] = mapped_column(String(20), nullable=False, default="stdio")

    # --- stdio transport fields ---
    command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    args: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Encrypted JSON blob of env vars (may contain secrets like GITHUB_TOKEN)
    env_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- sse / streamable_http transport fields ---
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    headers_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Authentication mode for remote transports ---
    # "headers" (default, existing) or "oauth"
    auth_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="headers"
    )

    # --- OAuth fields (used when auth_mode == "oauth") ---
    client_id_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the current access token was received: with
    # ``token_expires_at`` it gives the token's lifetime, which sets when the
    # refresh sweep renews it (``token_refresh.mcp_refresh_due_at``). NULL on a
    # token stored before the column existed: refreshed 15 minutes ahead.
    token_issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorize_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    token_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # RFC 7591 dynamic client registration endpoint. Discovered from the
    # server's OAuth metadata (or configured by hand) and used to mint a
    # ``client_id`` when none was provisioned.
    registration_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # RFC 8707 canonical resource identifier, sent as the ``resource`` parameter
    # on the authorize and token requests. The MCP spec requires it so an access
    # token issued for this server cannot be replayed against another one.
    oauth_resource: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Protected-resource metadata URL learned from a ``401 WWW-Authenticate:
    # Bearer resource_metadata="…"`` challenge. RFC 9728 lets it point at a
    # host or path no well-known probe derived from ``url`` would reach, so
    # the challenge value is authoritative and outranks any guess.
    oauth_metadata_url: Mapped[str | None] = mapped_column(
        String(2000), nullable=True
    )
    # Set when ``client_id_enc`` was obtained by dynamic registration rather
    # than typed in. Such a client is disposable: if the authorization server
    # later forgets it (``invalid_client``), it is simply re-registered.
    dcr_registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oauth_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="disconnected"
    )

    # ``agent_ids`` (a list of agents this server was meant for) is no longer
    # mapped: nothing ever read it, an agent's servers are its own
    # ``mcp_server_ids``. Migrated databases keep the nullable column, unread,
    # because the previous release still selects it during a rolling deploy;
    # a later release drops it.

    # --- Discovered tools (persisted after test/refresh) ---
    # JSON list of {name, description} dicts from the MCP server's tool catalog.
    discovered_tools: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # When the tools were last refreshed from the server.
    tools_discovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Deep Investigation source designation ---
    # NULL = this server is NOT an investigation source, and that is the
    # default for every existing and future server. Opt-in by construction: an
    # MCP tool can have side effects — `create_issue`, `send_message`, a
    # deploy — and `discovered_tools` records only a name and a description,
    # nothing that distinguishes a lookup from a write. Inferring the tool from
    # either would let an investigation mutate the world while reporting that
    # it read it, so a human names the tool that may be called.
    investigation_tool_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    # The argument the subject is passed as. NULL means "derive it from the
    # tool's input schema when the tool is called"; a tool whose schema makes
    # that ambiguous fails the step with a message saying so, rather than
    # guessing an argument name and sending the subject somewhere unintended.
    investigation_argument: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )

    # Tags – free-form labels for filtering and organisation
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Company-managed servers are shared with all users; only admins can edit.
    # When True, user_id is NULL.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

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
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Public catalog – when True, listed in the Filigran AI Catalog.
    is_public_catalog: Mapped[bool] = mapped_column(Boolean, default=False)

    # When True, this server is hidden from chat tool pickers (Profile, GA session)
    # but remains available for agent configuration and assignment execution.
    hide_from_chat: Mapped[bool] = mapped_column(Boolean, default=False)

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
