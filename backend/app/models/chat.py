# SPDX-License-Identifier: Apache-2.0

"""Chat models – conversations and messages across all channels.

A *Conversation* groups messages between a user and the copilot.
Each message records the channel it came from so responses can be
routed back to the right platform.

Channels:
  - web       → Chat widget in the Copilot frontend
  - slack     → Slack bot DM or channel thread
  - teams     → Microsoft Teams bot conversation
  - whatsapp  → WhatsApp Business Cloud API
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

logger = logging.getLogger(__name__)


class Conversation(Base):
    """A conversation thread between a user and the copilot."""

    __tablename__ = "conversations"
    # ``agent_id`` (current routed agent) is queried for per-agent chat counts;
    # ``original_agent_id``, ``user_id`` and ``created_at`` are indexed inline
    # via ``index=True``. The composite supports the conversation-list query
    # (a user's non-archived conversations, most-recently-updated first).
    __table_args__ = (
        Index("ix_conversations_agent_id", "agent_id"),
        Index(
            "ix_conversations_user_archived_updated",
            "user_id",
            "is_archived",
            "updated_at",
        ),
        # External-channel inbound routing looks up conversations by their
        # platform thread id on every incoming message (Slack/Teams/WhatsApp/
        # etc.); without an index this is a seq-scan on the hot conversations
        # table. Declared on the model so fresh (`create_all`) and stamped
        # installs also get it; the migration builds it for existing installs.
        Index("ix_conversations_channel_thread_id", "channel_thread_id"),
        # Listing a workspace's conversations. Declared on the model so fresh
        # (`create_all`) installs get it; the migration builds it
        # CONCURRENTLY for existing ones, because unlike `workspaces` this is
        # a populated, hot table.
        Index("ix_conversations_workspace_id", "workspace_id"),
        CheckConstraint(
            "link_access IN ('private', 'org_read')",
            name="ck_conversations_link_access_valid",
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

    # Channel that originated this conversation: web | slack | teams | whatsapp
    channel: Mapped[str] = mapped_column(String(30), nullable=False, default="web")

    # External thread/conversation ID on the originating platform
    # e.g. Slack thread_ts, Teams conversation ID, WhatsApp chat ID
    channel_thread_id: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Optional: lock the conversation to a specific agent. Mutated on
    # handover / transfer / re-routing.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )

    # The workspace this conversation belongs to. Optional and NOT
    # backfilled: every conversation that predates workspaces stays NULL, and
    # a new one only gets a workspace when the caller names it. "Unfiled" is a
    # real state, not a gap to be repaired — silently filing a user's whole
    # history into a container they never chose would be a worse default than
    # leaving it alone.
    #
    # SET NULL rather than RESTRICT, and this asymmetry with the tool tables
    # is deliberate. Losing the pointer costs a conversation nothing: the
    # thread and every message survive, merely unfiled. A workset is the
    # opposite — cascading it away strands a knowledge base, its documents and
    # its vectors — which is why that side must be RESTRICT and purged through
    # `purge_workset`. An unfiled conversation is a normal state, not a loss:
    # deleting a workspace leaves its conversations in no workspace, never in
    # a default nobody chose for them.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )

    # **Write-once** identifier of the agent the conversation was initially
    # routed to. Survives every subsequent handover / transfer / improvement-
    # loop rollback so audit trails and per-agent analytics can answer
    # "who started this conversation?" even after ``agent_id`` has changed.
    # Set together with ``agent_id`` on the first message and never mutated
    # after — application-layer enforcement via the SQLAlchemy
    # ``before_insert`` / ``before_update`` event listeners registered at
    # the bottom of this module (``_conversation_set_original_agent_on_insert``
    # and ``_conversation_protect_original_agent_on_update``), with a
    # column-level comment as a backstop signal for any future code that
    # reaches for it.
    # ``index=True`` is mirrored explicitly on the model (in addition to
    # the named index in the migration) so the fresh-DB bootstrap path
    # — ``Base.metadata.create_all`` + ``alembic stamp head`` in
    # ``app/core/migrations.py`` — also gets the index. Without this,
    # stamped installs would skip the migration body entirely and end
    # up missing the index that existing-install installs already have,
    # silently degrading future per-agent attribution queries to a
    # seq-scan on ``conversations``.
    original_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="write-once: first agent assigned to this conversation",
    )

    # Tracks the agent the user was talking to before a transfer_to_agent handover.
    # Set when a transfer occurs; cleared when the user returns to the source agent.
    transfer_source_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )

    # The agent this conversation was held with when that agent was deleted:
    # the soft reference the ``SET NULL`` of the columns above loses,
    # named by its ``agent_tombstones`` row. Written by the deletion only,
    # kept when the conversation is continued with another agent, so the
    # conversation tools still find "the conversation I had with A". No
    # foreign key and no index, like ``video_project_id``: every reader filters
    # the caller's own conversations, which the ``user_id`` index narrows first.
    deleted_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Presentation Studio: the deck this conversation was opened to
    # edit, side by side with its live preview. Set when the studio rail
    # creates the conversation and never rebound — a studio conversation is
    # about ONE deck for its whole life, which is what lets the chat layer
    # tell the agent "the presentation we are working on" without the user
    # pasting an id into every message. NULL for every ordinary conversation.
    #
    # ``SET NULL`` on deck deletion: the transcript is the user's and outlives
    # its subject. ``use_alter``: ``presentations.conversation_id`` already
    # points the other way (the deck knows the chat turn that produced it), so
    # the two tables form a cycle and this side is emitted as an ``ALTER``
    # after both tables exist — the migration adds it the same way.
    presentation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "presentations.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_conversations_presentation_id_presentations",
        ),
        nullable=True,
        index=True,
    )

    # Video Studio: the video this conversation was opened to edit,
    # beside its live preview - the presentation binding's twin, for the same
    # reasons. No foreign key and no index: ``conversations`` is the hottest
    # table, the studio lists its conversations among the caller's own (the
    # ``user_id`` index narrows first), and a conversation whose video was
    # deleted simply reads as an ordinary chat (the video no longer loads).
    video_project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Dashboard Studio: the custom dashboard this conversation was opened to
    # edit - the same binding, for the same reasons (no foreign key, no index).
    custom_dashboard_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Fleet page "Modify with AI": the fleet this conversation was opened to
    # change, beside its page - the same binding, for the same reasons (no
    # foreign key, no index).
    fleet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Human-readable title (auto-generated from first message)
    title: Mapped[str] = mapped_column(
        String(500), nullable=False, default="New conversation"
    )

    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)

    # Link-level access: how far the ``/chat?c=<id>`` URL reaches on its
    # own, independent of the named grants in ``conversation_shares``.
    #
    #   ``private``  – owner only (default). Pasting the URL grants nothing.
    #   ``org_read`` – any *authenticated* platform user who has the URL may
    #                  read the conversation.
    #
    # ``org_read`` is the tier that makes the URL pasteable into a Teams thread
    # or a Jira ticket without anybody having to be enumerated up front, which
    # is what sharing a link is for. It is emphatically **not** "no access
    # control": the reader still authenticates, is named in the audit trail,
    # and loses access the moment the owner sets this back to ``private``.
    # Anonymous access is not a value here and is not planned — the public
    # widget (``api/v1/public_chat.py``) is a separate, agent-scoped surface.
    #
    # Read-side consequence worth remembering: ``org_read`` conversations are
    # reachable *by URL only*. They are deliberately excluded from the
    # "shared with me" listing, because every user being able to open a
    # conversation is not the same as every user wanting it in their sidebar.
    link_access: Mapped[str] = mapped_column(
        String(20), nullable=False, default="private", server_default="private"
    )

    # Per-conversation tool overrides (General Assistant only).
    # JSON blob: {"builtin_tool_ids": [...], "custom_tool_ids": [...], "integration_ids": [...]}
    # When NULL the GA config defaults are used.
    tool_overrides_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Persisted conversation-compaction state.  JSON blob:
    # {"directives": str, "summary": str, "through_sequence": int,
    #  "compacted_count": int} — the Pinned Directives + Rolling Summary
    # produced by ``conversation_memory.manage_conversation_memory`` plus
    # the ``messages.sequence`` watermark of the newest message covered.
    # ``get_conversation_history`` rehydrates it (prepends the rendered
    # compaction message and loads only messages after the watermark) so
    # the summarizer runs once per compaction cycle instead of re-running
    # from scratch on every message past the threshold.  NULL = the
    # conversation has never been compacted.
    compaction_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # When the user last stopped this conversation's work (the Stop's cancel
    # cascade, ``sub_agent._cancel_partner_tasks``). A partner's answer can be
    # recorded just before a Stop and delivered after it; the wake compares
    # this with the delegation's submission and delivers nothing the user
    # stopped. Never read by a turn the user starts, so it blocks nothing
    # later. NULL = never stopped.
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="conversation",
        order_by="Message.created_at",
        cascade="all, delete-orphan",
    )


# ── ``original_agent_id`` write-once enforcement ────────────────────────
#
# Centralised here so every ``Conversation(...)`` constructor across the
# code base (REST chat, public chat, channel adapters, platform routes,
# tests) inherits the same write-once invariant without each call site
# having to remember to populate the column or guard mutations. Two
# listeners:
#
# * ``before_insert`` — if a row is inserted with ``agent_id`` set but
#   ``original_agent_id`` unset, mirror it. New conversations created
#   without an agent (e.g. the General Assistant route) stay NULL on
#   both columns; that case is intentional and is the read-side
#   responsibility of any caller that needs an attribution fallback.
#
# * ``before_update`` — if the row had a non-NULL ``original_agent_id``
#   before this update and the new value differs, the listener restores
#   the original value and emits a WARNING. We deliberately do NOT
#   raise: a hard exception inside a deep ORM flush is a worse
#   operator experience than the soft revert + audible log, and the
#   contract is "write-once" not "write-once-or-die". The WARNING is
#   loud enough to surface a regression in any code path that
#   accidentally tries to mutate the column.

def _conversation_set_original_agent_on_insert(_mapper, _connection, target: "Conversation") -> None:
    if target.original_agent_id is None and target.agent_id is not None:
        target.original_agent_id = target.agent_id


def _conversation_protect_original_agent_on_update(
    _mapper, _connection, target: "Conversation"
) -> None:
    # ``History.deleted`` carries the prior value(s) for any attribute
    # whose change SQLAlchemy is about to flush. If the column is
    # unchanged this round, ``deleted`` is empty and we noop.
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(target)
    history = state.attrs.original_agent_id.history
    if not history.deleted:
        return
    prior = history.deleted[0]
    if prior is None:
        # Was NULL before — promoting NULL → set is the legitimate
        # "first-time backfill" case (e.g. a row created before the
        # column existed). Permit it.
        return
    if target.original_agent_id != prior:
        logger.warning(
            "Conversation.original_agent_id is write-once — refusing "
            "mutation %r → %r on conversation %s and reverting to "
            "the prior value",
            prior,
            target.original_agent_id,
            target.id,
        )
        target.original_agent_id = prior


event.listen(Conversation, "before_insert", _conversation_set_original_agent_on_insert)
event.listen(Conversation, "before_update", _conversation_protect_original_agent_on_update)


class Message(Base):
    """A single message in a conversation."""

    __tablename__ = "messages"
    # ``conversation_id`` and ``created_at`` are indexed inline via
    # ``index=True``. ``role`` supports role breakdown queries; the composite
    # supports ordered message retrieval within a conversation.
    __table_args__ = (
        Index("ix_messages_role", "role"),
        Index("ix_messages_conversation_sequence", "conversation_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Role: user | assistant | system | tool
    role: Mapped[str] = mapped_column(String(20), nullable=False)

    # Message content (text, or JSON-stringified for structured content)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Channel that this particular message arrived on (may differ from conversation.channel
    # in edge cases, e.g. conversation started on web, user replies on Slack)
    channel: Mapped[str] = mapped_column(String(30), nullable=False, default="web")

    # External message ID on the platform (for deduplication / editing)
    channel_message_id: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # For tool-calling messages
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Metadata (e.g. attachments, skill info, token usage)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Local trace id written to ``llm_traces`` asynchronously — no FK to
    # avoid a race between the synchronous commit and the async dispatcher.
    llm_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )

    # Ordering within conversation
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    conversation: Mapped[Conversation] = relationship(
        "Conversation", back_populates="messages"
    )
    feedback_entries: Mapped[list["MessageFeedback"]] = relationship(
        "MessageFeedback",
        back_populates="message",
        cascade="all, delete-orphan",
    )


class MessageFeedback(Base):
    """Per-user explicit feedback (thumbs up/down) on assistant messages."""

    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "user_id",
            name="uq_message_feedback_message_user",
        ),
        CheckConstraint(
            "rating IN ('positive', 'negative')",
            name="ck_message_feedback_rating_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rating: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    message: Mapped[Message] = relationship("Message", back_populates="feedback_entries")


class ChatTurnCheckpoint(Base):
    """The tool work of a chat turn that has not stored its answer yet.

    At most one row per conversation, written by ``services/turn_checkpoint.py``
    while the turn runs and deleted in the transaction that stores its answer.
    A row that outlives its turn holds the work of a turn whose process died
    (or that ended on an error), which the next turn replays instead of doing
    it again.
    """

    __tablename__ = "chat_turn_checkpoints"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # ``chat_turn_stream.public_turn_id`` of the turn that wrote it.
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The completed tool rounds, in ``serialize_messages`` form.
    entries: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
