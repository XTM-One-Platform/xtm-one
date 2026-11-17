# SPDX-License-Identifier: Apache-2.0

"""The code interpreter's sandbox workspace: its row and its manifest.

One workspace per conversation, or per assignment run: the files a
``code_interpreter`` call leaves under ``workspace/`` and the next call finds
there. The bytes live in the object store under
``sandbox-workspaces/<workspace id>/<sha256>`` (one object per distinct
content); these rows say which path holds which content.

Keyed on the conversation or the run with no foreign key, so creating the
tables locks no existing one: a workspace whose conversation or run is gone
is deleted by the sweep (``services/sandbox_workspace/gc.py``), its objects
with it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class SandboxWorkspace(Base):
    """A conversation's (or an assignment run's) durable sandbox directory."""

    __tablename__ = "sandbox_workspaces"
    __table_args__ = (
        CheckConstraint(
            "(conversation_id IS NULL) <> (assignment_run_id IS NULL)",
            name="ck_sandbox_workspaces_one_scope",
        ),
        Index("uq_sandbox_workspaces_conversation", "conversation_id", unique=True),
        Index("uq_sandbox_workspaces_run", "assignment_run_id", unique=True),
        Index("ix_sandbox_workspaces_last_used", "last_used_at"),
        Index("ix_sandbox_workspaces_user", "user_id"),
        Index("ix_sandbox_workspaces_work_item", "work_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The person the workspace belongs to; ``None`` for a company-managed
    #: run, which has no file owner (it is then reached by its run alone).
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assignment_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The work item the inbox dispatched the run for (#9868): while the item
    #: is open, the workspace outlives its run and the next run of the same
    #: item by the same agent for the same person adopts it
    #: (``WorkspaceStore.open``). No foreign key, like the two scope columns.
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    #: When the sweep last removed something of it (objects no path holds).
    last_gc_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ``session: true`` kernels started again while one was expected alive.
    kernel_restarts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Bytes the sandbox read from / wrote to the workspace, every call summed.
    bytes_read: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: The ``session: true`` kernel last started for it: ``{"host", "kernel_id",
    #: "started_at", "backend"}``; ``None`` when none was.
    kernel_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class SandboxWorkspaceFile(Base):
    """One path of a workspace and the content it holds."""

    __tablename__ = "sandbox_workspace_files"
    __table_args__ = (Index("ix_sandbox_workspace_files_sha", "workspace_id", "sha256"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sandbox_workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Relative to ``workspace/``, ``/``-separated (``paths.normalize``).
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The file's modification time in the sandbox, seconds since the epoch.
    mtime: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
