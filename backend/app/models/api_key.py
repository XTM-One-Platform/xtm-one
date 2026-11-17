# SPDX-License-Identifier: Apache-2.0

"""UserApiKey model – personal API keys (``fcp-...``).

Users create personal API keys in their profile. A key authenticates its owner
on the REST API, the OpenAI-compatible LLM proxy (``/v1``) and the MCP
endpoints (``/mcp/user``, ``/mcp/xtm``, the platform servers). The raw key is
shown only once at creation; we store a bcrypt hash and a short prefix for
display.

Scopes, expiry and per-key limits narrow what a key may do, never what
its owner may do: every column below is checked by the one resolution in
``app/services/api_key_access.py``. A key created before these columns existed has them all
unset and keeps full access.

Usage tracking: ``request_count`` and ``last_used_at`` are buffered in Redis on
every authenticated request and flushed here by the scheduler.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserApiKey(Base):
    __tablename__ = "user_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Human-readable label (e.g. "My Cursor key", "CI pipeline")
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # The first 8 chars of the key for display: "fcp-xxxx..."
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)

    # bcrypt hash of the full key
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_from_env: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    # ── Scopes ───────────────────────────────────────────────────
    # The surfaces the key may call (``ApiKeySurface`` values); NULL = every
    # surface, the access of a key created before scopes existed.
    surfaces: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    # Read-only: what "read" means on each surface is ``api_key_access``'s table.
    read_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    # The agents the key may reach (canonical UUID strings); NULL = no restriction.
    agent_ids: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)

    # ── Expiry and per-key limits ────────────────────────────────
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ``daily`` / ``monthly``; set exactly when ``quota_limit`` is.
    quota_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The smallest expiry notice (in days before expiry) the owner was sent:
    # durable, so each notice goes out once per key.
    expiry_notified_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set when an administrator revokes the key (``is_active`` then false).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Usage tracking ───────────────────────────────────────────────────
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    request_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
