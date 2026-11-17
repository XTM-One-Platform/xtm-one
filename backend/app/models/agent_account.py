# SPDX-License-Identifier: Apache-2.0

"""Agent accounts: an agent operating a real user account of a messaging platform or a mailbox.

A channel bot is an application with its own identity; an agent account is a
person's account (``on_behalf``, the agent answers for them) or a service
account that IS the agent (``ai_user``, an AI colleague everyone knows is an
AI), reached through that account's own delegated OAuth integration.

What the agent answers and how is decided by the account's rules
(``services/agent_accounts/rules.py``), evaluated on every message the
provider (``services/agent_accounts/providers``) normalizes. Owner-only: the
account acts with its owner's integration token and agent, so it is never
shared; administrators reach every account.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentAccount(Base):
    __tablename__ = "agent_accounts"
    #: One enabled agent account per platform account, and per provider and
    #: integration: two would both answer, and compete for the
    #: provider's subscriptions. One integration may serve two providers - its
    #: Teams chats and its Outlook mailbox are different conversations.
    __table_args__ = (
        Index(
            "uq_agent_accounts_operated",
            "provider",
            "account_external_id",
            unique=True,
            postgresql_where=text("enabled AND account_external_id IS NOT NULL"),
        ),
        Index(
            "uq_agent_accounts_provider_integration",
            "provider",
            "integration_id",
            unique=True,
            postgresql_where=text("enabled AND integration_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The provider key of ``services/agent_accounts/providers`` (``microsoft_teams``, ``microsoft_outlook``).
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    #: The delegated integration signed in as the account. Kept when the
    #: integration is deleted: the account stops, its configuration stays.
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: ``on_behalf`` | ``ai_user``.
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="on_behalf")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: ``auto`` | ``approval`` | ``shadow`` - a rule may override it.
    response_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="approval")
    ai_label_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ai_label_text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: ``prefix`` | ``suffix``.
    ai_label_position: Mapped[str] = mapped_column(String(10), nullable=False, default="suffix")

    grace_period_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    owner_takeover_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Replies per conversation and hour, 0 = no limit.
    max_replies_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    #: Agent turns per hour, every conversation together, 0 = no limit:
    #: a mailbox starts a conversation with each new email.
    max_turns_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    #: The people a reply may reach besides the sender (a mailbox's
    #: reply-all); past it the message is left to the owner. 0 = no limit.
    max_reply_recipients: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default=text("10")
    )
    context_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    #: ``{"timezone", "mode": "inside"|"outside", "windows": [{"days", "start", "end"}]}``, NULL = always.
    active_hours: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Ordered rules, first match wins (``rules.normalize_rules``).
    rules: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    #: Reactions on the agent's answers read as ratings:
    #: ``{"enabled", "positive": [emoji], "negative": [emoji]}``, NULL = the
    #: defaults (``services.reaction_feedback.DEFAULT_CONFIG``).
    reaction_feedback: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    #: The owner is notified of each reply the account posts on its own.
    notify_on_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    #: What a mailbox files on each email the agent triaged - follow-up flag,
    #: read, categories - and whether a handed-over one notifies the owner
    #: (``triage.normalize_mailbox_actions``); NULL = ``triage.DEFAULT_MAILBOX_ACTIONS``.
    mailbox_actions: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    #: Where the signature of its replies comes from
    #: (``providers.SIGNATURE_SOURCES``): ``own``, ``integration`` or ``none``; NULL = ``own``.
    signature_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: The signature integration read when ``signature_source`` is ``integration``.
    #: No foreign key (``u5715sig01``): a deleted integration reads as gone, and
    #: the reply then carries the account's own signature.
    signature_integration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    #: The account as the provider names it, read from its token.
    account_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    #: ``push`` | ``poll`` | ``none``: how messages reach the platform now.
    transport: Mapped[str] = mapped_column(String(10), nullable=False, default="none")
    #: ``active`` | ``paused`` | ``error`` | ``setup``.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="setup")
    #: The last failure of a reply, or why the account was paused.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What the last sync found (``services/agent_accounts/problems.report``).
    sync_report: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class AgentAccountReply(Base):
    """One message the account's agent answered, drafted or chose to leave.

    The approval queue (``pending``) and the account's record of what it said
    (``sent``, ``shadow``, ``silent``, ``rejected``, ``expired``, ``failed``).
    """

    __tablename__ = "agent_account_replies"
    __table_args__ = (
        Index("ix_agent_account_replies_account_created", "account_id", "created_at"),
        Index("ix_agent_account_replies_user_status", "user_id", "status"),
        # A reaction names the platform message it is on: the answer it rates.
        Index(
            "ix_agent_account_replies_sent_message",
            "account_id",
            "sent_message_id",
            postgresql_where=text("sent_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_accounts.id", ondelete="CASCADE"), nullable=False
    )
    #: The account's owner, who approves.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The agent that wrote the answer (the account may change agents later):
    #: the agent a rating of it goes to. No key: one would lock
    #: ``agents`` in the migration, and a rating's own ``agent_id`` is one.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    conversation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(500), nullable=False)
    conversation_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thread_id: Mapped[str] = mapped_column(String(500), nullable=False)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    inbound_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: What the agent wrote, without the AI label.
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What was posted, the label included.
    sent_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rule_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rule_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Whether the label applies when an approved draft is sent.
    ai_label: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: What the provider needs to post into the conversation later.
    route: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``proactive`` for a message the agent sent through the account on its
    #: own (a chat run, an assignment, a tool call); NULL, read as
    #: ``reply``, for an answer to a message the account heard.
    kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: What produced a proactive send: the tool, the agent, the conversation or
    #: run it ran in, the recipients (``sent_log.record_proactive_send``). On
    #: any sent reply, ``delivery`` holds what the provider made of its text
    #: (a Teams reply: the ``@`` names mentioned and those left as text).
    origin: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class AgentAccountSubscription(Base):
    """A push subscription the provider holds for an account (a Graph subscription)."""

    __tablename__ = "agent_account_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The provider's id of the subscription, which every notification names.
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    resource: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: What the subscription is notified of, as the provider spells it
    #: (``created``, ``created,updated``): a change is a new subscription.
    change_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="created", server_default="created"
    )
    #: The random secret every notification must echo, Fernet-encrypted.
    client_state_enc: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AgentAccountDecision(Base):
    """What an account did with one message it heard: its activity log and analytics.

    One row per message (``uq_agent_account_decisions_message``): a message
    parked for a grace period is updated once the period ends (``waited``).
    Written and read by ``services/agent_accounts/activity.py`` only, kept
    ``activity.RETENTION_DAYS``.
    """

    __tablename__ = "agent_account_decisions"
    __table_args__ = (
        UniqueConstraint("account_id", "conversation_id", "message_id", name="uq_agent_account_decisions_message"),
        Index("ix_agent_account_decisions_account_created", "account_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_accounts.id", ondelete="CASCADE"), nullable=False
    )
    #: ``sent``, ``pending``, ``silent``, ``waiting``... or the decision's reason for a message left alone.
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    respond: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The message went through a grace period before its outcome.
    waited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conversation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(500), nullable=False)
    conversation_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preview: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mentions_account: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    addressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    thread_continuation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rule_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rule_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Who the sender was found to be, when a rule names groups (``Decision.sender``).
    sender_match: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    #: The agent's own one line on its triage of the message - why it answered,
    #: handed it to the owner or left it (``triage.Triage.note``).
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Who took a message another pipeline of the agent answers (``already_handled``):
    #: the claim's holder value (``inbound_claims``: ``assignment`` or
    #: ``agent_account:<id>``), worded by the frontend. NULL for any other outcome.
    holder: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: The trace of the turn that answered the message, its own or the
    #: turn it was merged into (``coalesced``). NULL when no turn ran.
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The reply the message produced; no foreign key, a reply goes with its account.
    reply_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: ``proactive`` for a message the agent sent on its own: the row
    #: is that message, its reply row the send. NULL, read as ``reply``, for a
    #: message the account heard.
    kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: When the message was written, as the platform says.
    message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
