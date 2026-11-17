# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Pydantic schemas of the agent-account endpoints.

Rules and active hours are carried as plain JSON and normalized by the rule
engine (``services/agent_accounts/rules.py``), the one place that knows what
they mean; these schemas only bound the scalar settings.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.services.agent_accounts.providers import get_provider
from app.services.reaction_feedback import ReactionFeedbackConfig

Mode = Literal["on_behalf", "ai_user"]
ResponseMode = Literal["auto", "approval", "shadow"]
LabelPosition = Literal["prefix", "suffix"]
#: ``providers.SIGNATURE_SOURCES``.
SignatureSource = Literal["own", "integration", "none"]


class AgentAccountWrite(BaseModel):
    """The editable settings; on update, an omitted field is left as it is."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    integration_id: str | None = None
    agent_id: str | None = None
    enabled: bool | None = None
    response_mode: ResponseMode | None = None
    ai_label_enabled: bool | None = None
    ai_label_text: str | None = Field(default=None, max_length=300)
    ai_label_position: LabelPosition | None = None
    grace_period_seconds: int | None = Field(default=None, ge=0, le=3600)
    owner_takeover_minutes: int | None = Field(default=None, ge=0, le=1440)
    max_replies_per_hour: int | None = Field(default=None, ge=0, le=1000)
    #: Agent turns per hour, every conversation together; 0 = no limit.
    max_turns_per_hour: int | None = Field(default=None, ge=0, le=1000)
    #: The people a reply may reach besides the sender; 0 = no limit.
    max_reply_recipients: int | None = Field(default=None, ge=0, le=1000)
    context_messages: int | None = Field(default=None, ge=0, le=50)
    active_hours: dict[str, Any] | None = None
    instructions: str | None = Field(default=None, max_length=4000)
    rules: list[dict[str, Any]] | None = None
    #: What reactions on the account's answers mean; validated here (422).
    reaction_feedback: ReactionFeedbackConfig | None = None
    #: Notify the owner of each reply the account posts on its own.
    notify_on_send: bool | None = None
    #: What a mailbox files on each email the agent triaged; normalized
    #: by ``triage.normalize_mailbox_actions`` (400 on a fault).
    mailbox_actions: dict[str, Any] | None = None
    #: Where the signature of its replies comes from, on a provider whose replies are signed.
    signature_source: SignatureSource | None = None
    #: The signature integration read when ``signature_source`` is ``integration``.
    signature_integration_id: str | None = None


class AgentAccountCreate(AgentAccountWrite):
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=50)
    mode: Mode = "on_behalf"
    integration_id: str
    agent_id: str


class AgentAccountUpdate(AgentAccountWrite):
    mode: Mode | None = None
    #: Explicitly clear the schedule (``active_hours: null`` alone means "unchanged").
    clear_active_hours: bool = False


class AgentAccountSyncTarget(BaseModel):
    """A conversation a sync problem affects: ``chats`` (every chat of the account), a mailbox's ``inbox``, or a ``channel``."""

    covers: str
    name: str | None = None


class AgentAccountSyncNote(BaseModel):
    """A reason the platform words itself, as a code the frontend renders in the viewer's language."""

    code: str
    values: dict[str, str | int] = {}


class AgentAccountSyncProblem(BaseModel):
    """One cause that keeps the account from hearing conversations (``problems.KINDS``)."""

    kind: str
    count: int = 0
    #: The permission a refused subscription needs.
    permission: str | None = None
    #: The agent account of the same owner, or the host of the other address, holding the conversation.
    holder: str | None = None
    targets: list[AgentAccountSyncTarget] = []
    #: The provider's own words, for the technical details.
    details: list[str] = []
    #: The platform's own reasons, rendered from the frontend catalogs (older reports have none).
    notes: list[AgentAccountSyncNote] = []


class AgentAccountSyncReport(BaseModel):
    checked_at: str | None = None
    #: The subscriptions the rules need, and those of them the provider holds.
    wanted: int = 0
    heard: int = 0
    problems: list[AgentAccountSyncProblem] = []


class AgentAccountResponse(BaseModel):
    id: str
    user_id: str
    owner_name: str | None = None
    name: str
    description: str | None = None
    provider: str
    provider_label: str | None = None
    integration_id: str | None = None
    integration_name: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    mode: str
    enabled: bool
    response_mode: str
    ai_label_enabled: bool
    ai_label_text: str | None = None
    ai_label_preview: str
    ai_label_position: str
    grace_period_seconds: int
    owner_takeover_minutes: int
    max_replies_per_hour: int
    max_turns_per_hour: int = 0
    max_reply_recipients: int = 10
    context_messages: int
    active_hours: dict[str, Any] | None = None
    instructions: str | None = None
    rules: list[dict[str, Any]]
    #: The setting in effect, the defaults when the account never set one.
    reaction_feedback: dict[str, Any]
    notify_on_send: bool = True
    #: The mailbox actions in effect, the defaults when never set; read on a mailbox only.
    mailbox_actions: dict[str, Any]
    #: Where the signature of its replies comes from.
    signature_source: str = "own"
    signature_integration_id: str | None = None
    signature_integration_name: str | None = None
    account_external_id: str | None = None
    account_display_name: str | None = None
    account_email: str | None = None
    transport: str
    status: str
    #: The last failure of a reply, or why the account was paused.
    last_error: str | None = None
    #: What the last sync found; ``None`` before the first one.
    sync_report: AgentAccountSyncReport | None = None
    last_event_at: str | None = None
    last_reply_at: str | None = None
    pending_count: int = 0
    subscription_count: int = 0
    #: When the first of its push subscriptions lapses (renewed a day before); ``None`` without one.
    subscription_expires_at: str | None = None
    #: Where its read of the platform stands, as the provider keeps it (a Gmail
    #: mailbox: the last history id read); ``None`` before the first read.
    read_cursor: str | None = None
    #: What the provider calls that cursor (``last history id`` on Gmail).
    read_cursor_label: str | None = None
    can_manage: bool = True
    created_at: str
    updated_at: str


class ProviderCatalogItem(BaseModel):
    key: str
    label: str
    integration_provider: str
    #: ``chat`` or ``mailbox``: how the editor words the account's conversations.
    kind: str = "chat"
    capabilities: dict[str, bool]
    push_reachable: bool
    #: The two modes' starting points on this provider.
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AgentAccountCatalog(BaseModel):
    providers: list[ProviderCatalogItem]
    #: The chat providers' starting points (each provider carries its own).
    presets: dict[str, dict[str, Any]]
    default_labels: dict[str, str]
    #: What a mailbox files on the emails the agent triaged, until the owner changes it.
    mailbox_defaults: dict[str, Any] = Field(default_factory=dict)
    #: The integration providers that hold email signatures, for the signature picker.
    signature_providers: list[str] = Field(default_factory=list)


class SimulatedEvent(BaseModel):
    conversation_type: Literal["direct", "group", "channel"] = "direct"
    conversation_id: str = Field(default="", max_length=500)
    sender_id: str = Field(default="", max_length=255)
    sender_email: str | None = Field(default=None, max_length=320)
    text: str = Field(default="Hello", max_length=8000)
    mentions_account: bool = False
    from_account: bool = False
    from_application: bool = False


class SimulatedState(BaseModel):
    thread_active: bool = False
    owner_active: bool = False
    replies_last_hour: int = Field(default=0, ge=0)
    #: The sender's XTM One groups; empty: read from the XTM One user of ``event.sender_email``.
    sender_group_ids: list[str] = Field(default_factory=list, max_length=200)


class SimulateRequest(BaseModel):
    """Settings to test (an unsaved draft of the editor) and a message to test them on."""

    settings: AgentAccountWrite = Field(default_factory=AgentAccountWrite)
    mode: Mode = "on_behalf"
    #: The provider whose starting points fill what the draft leaves out; a chat provider's by default.
    provider: str | None = Field(default=None, max_length=50)
    event: SimulatedEvent = Field(default_factory=SimulatedEvent)
    state: SimulatedState = Field(default_factory=SimulatedState)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str | None) -> str | None:
        """A provider the platform does not have is refused, never read as a chat provider."""
        if value and get_provider(value) is None:
            raise ValueError("Unknown provider.")
        return value


class SimulateResponse(BaseModel):
    decision: dict[str, Any]
    listening: dict[str, Any]
    #: Who the sender was found to be when a rule names groups: ``{known, emails, groups}``,
    #: the groups the rules name only.
    sender: dict[str, Any] | None = None


class PickerConversation(BaseModel):
    id: str
    name: str
    type: str
    team_id: str | None = None


class PickerPerson(BaseModel):
    id: str
    name: str
    email: str | None = None


class PickerIdentity(BaseModel):
    """The account an integration is signed in as, and the agent account already operating it."""

    external_id: str
    display_name: str
    email: str | None = None
    #: The caller's own agent account operating it (another user's is never named).
    operated_by_account_id: str | None = None
    operated_by_account_name: str | None = None
    operated_by_other_user: bool = False


class AgentAccountReplyResponse(BaseModel):
    id: str
    account_id: str
    account_name: str | None = None
    #: The account's provider, which says what its conversations are.
    provider: str | None = None
    status: str
    conversation_type: str
    conversation_name: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None
    inbound_text: str
    draft_text: str | None = None
    sent_text: str | None = None
    rule_name: str | None = None
    ai_label: bool
    error: str | None = None
    decided_at: str | None = None
    created_at: str
    #: ``reply`` (an answer to a message the account heard) or ``proactive``
    #: (a message the agent sent on its own through the account).
    kind: str = "reply"
    #: What produced a proactive send: the tool, the run's source, agent,
    #: conversation, run or assignment, the recipients.
    origin: dict[str, Any] | None = None
    #: Ratings of the posted answer, reactions included.
    rating_positive: int = 0
    rating_negative: int = 0


class ApproveReplyRequest(BaseModel):
    #: The reply as edited by its owner; omitted, the draft is sent as is.
    text: str | None = Field(default=None, max_length=28000)


class AgentAccountReplyPage(BaseModel):
    items: list[AgentAccountReplyResponse]
    total: int


class AgentAccountDecisionItem(BaseModel):
    """One message the account heard and what it did with it."""

    id: str
    at: str
    updated_at: str
    message_at: str | None = None
    #: What became of the message, the reply's status when it produced one.
    outcome: str
    #: The page's group of the outcome (``activity.GROUPS``).
    group: str
    reason: str | None = None
    respond: bool
    waited: bool
    conversation_type: str
    conversation_id: str
    conversation_name: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    preview: str
    mentions_account: bool
    addressed: bool
    thread_continuation: bool
    rule_id: str | None = None
    rule_name: str | None = None
    sender: dict[str, Any] | None = None
    #: The agent's own line on its triage of the message.
    note: str | None = None
    #: Who took a message another pipeline of the agent answers (``already_handled``):
    #: ``assignment`` or ``agent_account``, worded by the frontend.
    holder_kind: str | None = None
    #: The agent account that took it, when ``holder_kind`` is ``agent_account``.
    holder_account_id: str | None = None
    #: The trace of the turn that answered the message, its own or the one it was merged into.
    trace_id: str | None = None
    reply_id: str | None = None
    reply_status: str | None = None
    reply_text: str | None = None
    reply_error: str | None = None
    #: ``reply`` (a message the account heard) or ``proactive`` (a message
    #: the agent sent on its own through the account).
    kind: str = "reply"
    #: What produced a proactive send (``AgentAccountReplyResponse.origin``).
    origin: dict[str, Any] | None = None


class AgentAccountDecisionPage(BaseModel):
    items: list[AgentAccountDecisionItem]
    total: int


class ActivityBucket(BaseModel):
    at: str
    #: Messages by group (``activity.GROUPS``).
    counts: dict[str, int]


class ActivityConversation(BaseModel):
    conversation_id: str
    conversation_name: str | None = None
    conversation_type: str
    #: Someone who wrote there besides the account, to name a direct chat.
    with_name: str | None = None
    heard: int
    for_agent: int
    answered: int


class ActivitySender(BaseModel):
    #: The platform id, or the name for a sender the platform named only.
    sender: str
    sender_name: str | None = None
    heard: int
    for_agent: int
    answered: int


class ActivityRule(BaseModel):
    rule_id: str
    rule_name: str | None = None
    matched: int
    answered: int
    silent: int


class AgentAccountActivity(BaseModel):
    """What an account did over a period (``activity.activity_stats``)."""

    period: str
    #: ``hour`` or ``day``: the width of the timeline's buckets.
    bucket: str
    timezone: str
    since: str
    until: str
    heard: int
    #: Messages a rule answers (``Decision.respond``), whatever the agent then did.
    for_agent: int
    groups: dict[str, int]
    outcomes: dict[str, int]
    left_reasons: dict[str, int]
    by_conversation_type: dict[str, dict[str, int]]
    timeline: list[ActivityBucket]
    conversations: list[ActivityConversation]
    senders: list[ActivitySender]
    rules: list[ActivityRule]
    #: Replies created in the period, by status.
    replies: dict[str, int]
    #: From the message to the reply posted automatically.
    median_answer_seconds: float | None = None
    #: From the draft to its approval.
    median_approval_seconds: float | None = None
    ratings: dict[str, int]
