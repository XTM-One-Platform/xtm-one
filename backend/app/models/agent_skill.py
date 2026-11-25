# SPDX-License-Identifier: Apache-2.0

"""AgentSkill model – a shared, reusable skill following the Agent Skills spec.

A skill can be created in three ways:
1. **By URL** – a URL to a SKILL.md file (typically following
   https://agentskills.io/specification).  The platform fetches the content,
   parses optional YAML frontmatter (name, description, compatibility), and
   caches the body.  The URL may point into a **private** GitHub repository by
   binding a GitHub integration via ``source_integration_id``;
   the fetch is then authenticated with that integration's credential and
   tracks the ``source_ref`` branch's HEAD (default branch when unset).
2. **By pasting content** – the user provides the markdown content directly.
3. **By uploading a file** – a ``.md`` / ``.txt`` file is uploaded and its
   content is stored directly.

In all cases the content is cached in the DB so it can be injected into agent
system prompts at runtime.

Skills are **top-level shared entities** (like Integrations, MCP Servers, KBs):
- Users create skills that can be attached to any of their agents.
- Admins can make skills company-managed (shared with all users).
- Skills can be shared with specific groups via ``shared_group_ids``.

Agents reference skills via a ``skill_ids`` JSON column on the Agent model.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Tag of a built-in skill that belongs to a fleet's own agents (Platform Health's
#: specialists, #1943): still bound to them and injected at run time, but absent from the
#: Skills catalogue and the skill pickers (``GET /skills`` with the default scope).
INTERNAL_SKILL_TAG = "internal"


def is_internal_skill(tags: object) -> bool:
    """Whether a skill's *tags* mark it internal (:data:`INTERNAL_SKILL_TAG`)."""
    return isinstance(tags, list) and INTERNAL_SKILL_TAG in tags


class AgentSkill(Base):
    """A reusable skill definition available as a shared entity.

    Skills can originate from a URL (fetched & cached), pasted content, or an
    uploaded file.  When ``url`` is set the content can be refreshed from the
    remote source; when ``url`` is NULL the skill was created via direct input.
    """

    __tablename__ = "agent_skills"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Company-managed skills are shared with all users; only admins can edit.
    # When True, user_id is NULL.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Group sharing: list of Group UUIDs this entity is shared with.
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

    # The URL where the skill definition lives (e.g. raw GitHub URL to SKILL.md).
    # NULL when the skill was created via direct content input or file upload.
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional GitHub Integration whose (already Fernet-encrypted) credential
    # authenticates the fetch of ``url`` — this is what makes a SKILL.md in a
    # **private** repository usable as a skill source.  NULL keeps
    # the historical behaviour: a single anonymous GET, unchanged for public
    # URLs.  No secret is ever stored on this row; only the reference to the
    # integration that already holds one.  ``ON DELETE SET NULL`` so deleting
    # the integration degrades the skill to unauthenticated fetches (which then
    # fail loudly) rather than cascading away the skill itself.
    source_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Branch / tag / commit to track, overriding whatever ref the URL carries.
    # NULL (the default) means "the repository's default branch", i.e. the
    # contents API is called without a ``ref`` and returns that branch's HEAD —
    # the "always the latest merged version" contract.  Set it to follow a
    # non-default branch, or to a commit SHA to deliberately pin a version.
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Metadata – extracted from YAML frontmatter or user-provided
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Compatibility notes – from YAML frontmatter ``compatibility`` field.
    # Describes environment requirements (e.g. "Requires GitHub integration").
    # Used during relevance assessment at runtime.
    compatibility: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Licence - the Agent Skills spec's frontmatter ``license`` field: the
    # licence name and copyright, or a reference to a bundled licence file.
    # Metadata about the skill, never part of what the agent reads: shown in
    # the skill's Catalog tab and carried by every copy (export, clone).
    license: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cached content – the full markdown body (instructions) fetched from the URL
    content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # When the content was last successfully fetched from the URL
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Number of consecutive 4xx failures during auto-refresh.  Resets to 0 on
    # a successful refresh or when the URL is changed/cleared.  When this
    # counter reaches ``skill_refresh_quarantine_threshold`` the skill is
    # excluded from further auto-refresh attempts so a permanently-broken URL
    # stops consuming daily HTTP budget.
    refresh_failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Number of consecutive *authentication* failures during auto-refresh.
    # Deliberately a separate counter from
    # ``refresh_failure_count``: an expired or under-scoped credential is a
    # recoverable misconfiguration, not a permanently broken URL, so it must
    # never feed the quarantine logic — it would read as "this URL is dead"
    # when the real fix is rotating a token.  Resets to 0 on a successful
    # refresh, and on any credential/URL change.  Crossing
    # ``skill_refresh_auth_fail_alert_threshold`` raises an operator-facing
    # ERROR log (and surfaces in the skill UI) without disabling the skill.
    auth_failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Tags – free-form labels for filtering and organisation
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Public catalog – when True, listed in the Filigran AI Catalog.
    is_public_catalog: Mapped[bool] = mapped_column(Boolean, default=False)

    # When True, this skill is hidden from chat contexts
    # but remains available for agent configuration.
    hide_from_chat: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Provenance of a fork (``app.services.skill_fork``): ``{skill_id, slug,
    #: name, is_builtin, version_number, forked_at}`` of the source at fork
    #: time. NULL on a skill that was not forked.
    forked_from: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
