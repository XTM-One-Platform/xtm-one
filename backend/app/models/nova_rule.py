# SPDX-License-Identifier: Apache-2.0

"""NovaRule model — active prompt-safety rules (Nova framework).

A **NovaRule** is a detection rule written in the Nova ``.nov`` format
(YARA-inspired: ``keywords`` / ``condition`` sections, plus ``meta``), stored
so the platform can run it as an ACTIVE pre-LLM guard on a user prompt —
unlike ``agent.guardrails``, which is passive system-prompt text.

Scope for the MVP: **keyword/regex matching only** (the ``keywords:`` and
``condition:`` sections). The ``semantics:`` and ``llm:`` tiers of the Nova
format are parsed-and-ignored for now, so a rule stays valid as those tiers
land later. Each rule carries its own **action** — ``block`` (refuse the
prompt before any LLM call) or ``log`` (flag + audit, let it through) — which
is the "action per rule" policy the product decided on.

Storage mirrors the other user-authored, three-tier-visible artifacts
(``CustomTool`` / ``Prompt``): own / group-shared / company-managed. Platform
rules are company-managed; a flow's ``capability.guardrails`` node references
these by id (a later increment).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# What a match does. `block` refuses the prompt before the LLM call; `log`
# flags + audits without blocking (the current scan_user_input behaviour).
NOVA_ACTION_BLOCK = "block"
NOVA_ACTION_LOG = "log"
NOVA_ACTIONS = (NOVA_ACTION_BLOCK, NOVA_ACTION_LOG)


class NovaRule(Base):
    """One Nova detection rule (``.nov`` text) with a block/log action."""

    __tablename__ = "nova_rules"
    __table_args__ = (
        Index("ix_nova_rules_is_company_managed", "is_company_managed"),
        Index("ix_nova_rules_enabled", "enabled"),
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

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # The rule's category (from the .nov `meta.category`, mirrored here for
    # listing/grouping and for the audit event). Free-form, e.g. "Jailbreak".
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="")

    # Per-rule policy: block refuses the prompt, log only flags it.
    action: Mapped[str] = mapped_column(
        String(20), nullable=False, default=NOVA_ACTION_LOG
    )

    # The `.nov` rule source. Validated by the Nova-compatible parser on write.
    rule_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Company-managed rules apply platform-wide; only admins edit them.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user sharing + company-managed narrowing. Same shape as the
    # group columns above, keyed by User UUID; see ``app.models.agent`` for the
    # rationale and ``app.api.v1.helpers.user_visible_filter`` for enforcement.
    # Required, not optional: that helper is generic over every entity carrying
    # ``shared_group_ids`` and references these columns unconditionally, so a
    # shared entity without them raises ``AttributeError`` at query-build time
    # on the non-admin branch — which is how every flow endpoint 500ed for
    # non-admin users once the builder shipped unflagged.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
