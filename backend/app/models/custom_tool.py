# SPDX-License-Identifier: Apache-2.0

"""CustomTool model – stores user-created custom tools with JavaScript functions.

Custom tools allow users to define their own tool logic with a name,
description, input schema (variables), and a JavaScript function body.
Agents can bind custom tools just like MCP servers or knowledge bases.

Supports ``is_company_managed`` toggle: when True, the tool is shared
with all users and only admins can edit it.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CustomTool(Base):
    """A user-defined custom tool with a JavaScript function body."""

    __tablename__ = "custom_tools"
    # ``user_id`` is indexed inline. The company-managed flag is filtered on by
    # every list endpoint's three-tier visibility query.
    __table_args__ = (
        Index("ix_custom_tools_is_company_managed", "is_company_managed"),
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

    # Human-friendly label shown in the UI
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # What the tool does (shown to the LLM)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Optional icon URL for the tool
    icon_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Input schema: JSON Schema object defining the tool's parameters
    # e.g. {"type": "object", "properties": {"keyword": {"type": "string", "description": "..."}}, "required": ["keyword"]}
    input_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # The function body to execute (JavaScript or Python).
    # Variables from input_schema are injected as `$variable_name` (JS)
    # or `variable_name` (Python).
    js_function: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Language of the function body: "javascript" (default) or "python".
    language: Mapped[str] = mapped_column(String(20), default="javascript")

    # Tags – free-form labels for filtering and organisation
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Company-managed tools are shared with all users; only admins can edit.
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

    # When True, this tool is hidden from chat tool pickers (Profile, GA session)
    # but remains available for agent configuration and assignment execution.
    hide_from_chat: Mapped[bool] = mapped_column(Boolean, default=False)

    # Execution timeout in seconds (default 300 = 5 min)
    timeout: Mapped[int] = mapped_column(Integer, default=300)

    # Declared tests (``services/custom_tool_tests.py``). ``test_cases`` are
    # data cases (inputs, dry run, expectations); ``test_code`` holds test
    # functions run by the stdlib runner of the tool's language.
    test_cases: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    test_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The outcome of the last test run: "passed", "failed" or "error".
    last_test_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_test_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_test_results: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Repository sync (``services/custom_tool_sources.py``). A tool with a
    # ``source_id`` is managed by that source: its content is read-only and
    # follows the repository. No foreign key: one on this hot table would lock
    # it at migration time, and the code treats a missing source as detached.
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    # The tool's directory in the repository: its identity within the source.
    source_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The commit whose version of the tool is active.
    source_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sha256 of the tool's files at that commit: an unchanged tool is skipped.
    source_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set when the sync disabled the tool because its directory left the
    # repository; it is what lets the sync enable it again when the directory
    # comes back, and never a tool someone disabled by hand.
    source_removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
