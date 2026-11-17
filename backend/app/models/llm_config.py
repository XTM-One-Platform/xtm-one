# SPDX-License-Identifier: Apache-2.0

"""LLM provider configuration model – platform-wide encrypted API keys.

LLM providers are configured once by admins and shared across all users.
The ``user_id`` field is retained as an optional audit trail ("created by").

Each provider stores a list of ``available_models`` that admins have enabled
for use by agents and the LLM proxy.  The ``default_model`` is the fallback
when no explicit model is requested.

A provider may be restricted to some groups and users, so a team runs
on its own API key: who may use it, and how a run picks it, is decided in ONE
place, ``app.llm.provider_access``.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LLMProviderConfig(Base):
    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        # An integration feeds at most one config: the check before a create
        # or an adoption is not atomic with its insert, so the database holds
        # the rule (the service answers its violation with a 409).
        Index(
            "uq_llm_provider_configs_source_integration_id",
            "source_integration_id",
            unique=True,
            postgresql_where=text("source_integration_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Audit trail: who created this config (nullable — platform-wide, not per-user)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # anthropic | openai | gemini
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet-encrypted
    default_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Models the admin has enabled for agents / proxy usage
    available_models: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(default=False)

    # Per-model context window overrides (tokens).
    # JSON dict mapping model name → context window size.
    # Example: {"gpt-4o": 128000, "gpt-4": 8192}
    # When a model is not in the dict, the provider class default is used.
    context_windows: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Per-model max output token overrides.
    # JSON dict mapping model name → max output tokens.
    # Example: {"gpt-4o": 16384, "claude-sonnet-5": 64000}
    # When a model is not in the dict, the provider class default is used.
    max_output_tokens: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # True when this provider was seeded from environment variables (LLM_PROVIDER_*_API_KEY).
    # Env-seeded providers cannot be deleted via the UI and their API key is
    # re-applied from the env on every boot. Their model catalog is env-pinned
    # too, unless the operator opts into admin curation with
    # LLM_PROVIDER_MODELS_EDITABLE=true (see ``app.llm.env_models``).
    is_from_env: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false(), default=False
    )

    # Snapshot of the LLM_PROVIDER_{PROVIDER}_MODELS list applied at the last
    # boot (env-seeded rows only, NULL otherwise / before the first seed).
    # The delta-merge in editable mode diffs the current env value against
    # this snapshot so that models added to / removed from the env var
    # propagate while every other admin edit to ``available_models`` sticks.
    env_models: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Restricted to the groups and users listed below. Unrestricted
    # (the default) = usable by everyone, as before. Read only through
    # ``app.llm.provider_access``; the id lists hold canonical UUID strings.
    access_restricted: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false(), default=False
    )
    shared_group_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    shared_user_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Never used for the groups and users listed below, whatever the
    # restriction above grants: a member of an excluded group is excluded.
    # Independent of ``access_restricted`` (an open provider may exclude
    # some people); read only through ``app.llm.provider_access``.
    excluded_group_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    excluded_user_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # The integration this config was populated from - a company-managed
    # OpenRouter integration (``services/openrouter_llm_provider.py``). Its API
    # key is copied here and kept in step on every edit of the integration, for
    # as long as it stays company-managed. NULL for a config created in
    # Settings, and once the integration is deleted: the config keeps running
    # on the key it holds. Unique while set (``__table_args__``).
    source_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "integrations.id",
            ondelete="SET NULL",
            # Named as the migration names it, so create_all and an upgraded
            # database carry the same constraint.
            name="fk_llm_provider_configs_source_integration_id_integrations",
        ),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
