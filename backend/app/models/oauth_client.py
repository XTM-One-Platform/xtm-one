# SPDX-License-Identifier: Apache-2.0

"""OAuth 2.1 client registrations for the platform authorization server.

Named ``MCPOAuthClient`` until the A2A partner grant started storing its
credentials in the same table. The class is resource-neutral now; the table
name is deliberately left at ``mcp_oauth_clients`` so this rename needs no
migration.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OAuthClient(Base):
    """An OAuth 2.1 client of this platform's authorization server.

    Two provenances share this table:

    - **MCP clients**, self-registered via ``POST /mcp/oauth/register``
      (RFC 7591), with ``a2a_exposure_id`` NULL.
    - **A2A partner credentials**, minted when an agent is exposed and
      revoked with it, bound to exactly one exposure.
    """

    __tablename__ = "mcp_oauth_clients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    client_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    client_secret_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)

    client_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    client_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    redirect_uris: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    grant_types: Mapped[list] = mapped_column(
        JSON, nullable=False, default=lambda: ["authorization_code"]
    )
    response_types: Mapped[list] = mapped_column(
        JSON, nullable=False, default=lambda: ["code"]
    )
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        String(50), nullable=False, default="none"
    )
    scope: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Set only for A2A partner credentials, which are minted when an agent is
    # exposed and revoked with it. Nullable is load-bearing: every
    # pre-existing MCP client self-registered through RFC 7591 DCR and has no
    # exposure, so a NOT NULL column here would break them all.
    #
    # Superseded by ``a2a_integration_id`` for credentials minted since the
    # partner relationship moved onto an integration; retained because rows
    # created by the old per-exposure flow are still valid credentials.
    a2a_exposure_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("a2a_exposures.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # An ``oauth_a2a`` integration's credential: one registration per PARTNER
    # rather than per agent, valid on every agent that binds it. That is the
    # accepted widening of the old per-exposure blast radius — a partner
    # integrating with three agents now holds one secret instead of three,
    # which is what makes the credential manageable at all when exposure
    # follows bindings.
    #
    # CASCADE: deleting the integration deletes the credential, so a removed
    # partner relationship cannot leave a usable secret behind.
    a2a_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
