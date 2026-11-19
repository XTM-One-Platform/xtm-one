# SPDX-License-Identifier: Apache-2.0

"""One person's own OAuth account on a ``per_user`` MCP server.

A token holder, as ``IntegrationUserCredential`` is for an integration: its
own tokens, expiry and status; every other column (the OAuth client, the
endpoints, the ``resource``, the configured scopes, the relay group) is read
from its server, so the refresh code works on a server row or a holder alike.
Contract: ``dev-docs/mcp-server-user-credentials.md``.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mcp_server import MCPServer


class MCPServerUserCredential(Base):
    __tablename__ = "mcp_server_user_credentials"
    __table_args__ = (
        UniqueConstraint("server_id", "user_id", name="uq_mcp_server_user_credentials_server_user"),
        Index("ix_mcp_server_user_credentials_user_id", "user_id"),
        # The refresh sweep, as ``ix_integration_user_credentials_status_token_expires``.
        Index(
            "ix_mcp_server_user_credentials_status_token_expires",
            "oauth_status",
            "token_expires_at",
            postgresql_where=text("refresh_token_enc IS NOT NULL"),
        ),
    )

    #: Read by the token lifecycle to tell a holder from a server row.
    is_user_credential = True

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # --- The account (Fernet) ---
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The scopes the token was granted. The scopes requested stay the server's
    # ``scopes``, read through the delegation below.
    granted_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ``connected`` / ``error`` / ``disconnected``, as ``MCPServer.oauth_status``.
    oauth_status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Loaded with the row: the holder reads its configuration from it, and an
    # async session cannot lazy-load it later.
    server: Mapped[MCPServer] = relationship(MCPServer, lazy="joined", innerjoin=True)


#: The holder's own columns; every other ``MCPServer`` column is read from the server.
OWN_COLUMNS = frozenset(
    {
        "id",
        "user_id",
        "access_token_enc",
        "refresh_token_enc",
        "token_expires_at",
        "token_issued_at",
        "oauth_status",
        "created_at",
        "updated_at",
    }
)


def _server_column(name: str) -> property:
    def _read(self: MCPServerUserCredential):
        return getattr(self.server, name)

    _read.__name__ = name
    return property(_read, doc=f"The server's ``{name}`` (read-only).")


for _column in MCPServer.__table__.columns:
    if _column.key not in OWN_COLUMNS:
        setattr(MCPServerUserCredential, _column.key, _server_column(_column.key))
del _column
