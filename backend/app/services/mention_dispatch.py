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

"""Answer every @mentioned agent of a chat message, one after another.

A message may name several agents. The conversation's agent answers first,
exactly as it does without mentions; each other mentioned agent then answers,
in the order it is mentioned, inside the SAME turn:

* the turn keeps the conversation's single-flight lock for the whole
  dispatch, so a mentioned agent is never a second concurrent agent loop;
* every agent that runs is one agentic action, charged before it runs
  by the chat quota gate its own ``run_agent_chat`` meets - with its own
  execution count, and its own credit-back when it fails - exactly as for a
  message it answered alone; a handover inside a run (consult, transfer)
  rides that run's action. A quota refusal ends the turn
  (:class:`TurnMetering`, :func:`quota_refused`);
* each reply is its own assistant message whose ``answered_by`` metadata names
  the agent that wrote it; a later agent reads the replies given earlier in the
  turn as labelled conversation messages (:func:`attributed_reply`), never as
  system prompt text;
* every agent that has others answering after it, the conversation's own
  included, reads who they are on the message it answers
  (:func:`turn_lineup`), so it gives its own part and never asks the
  user to pass the message on - again never as system prompt text. The
  agents after it travel as targets (``agents_after``) and are rendered by
  the run, which leaves out the agent actually answering;
* a mention of an agent the user may not chat with is ignored like an unknown
  one, a failing agent leaves a note while the others still answer, a cancel
  stops the agents not yet run, and the mentioned agents share one wall-clock
  budget (:data:`DISPATCH_BUDGET_SECONDS`);
* a message that reaches a turn already running is steering for the agent
  answering; the other agents it mentions ride the fold and answer it
  once the turn's own agents have, under the same rules
  (:func:`answer_folded_mentions`).

The chat entry points (web chat stream and REST) and the parent wake own streaming
and persistence; this module owns who answers, in which order, with which
history, and how a refusal, a failure, a cancel or the budget is worded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.llm.base import ChatMessage
from app.models.general_assistant_config import CHAT_MIN_LOOP_TIMEOUT_SECONDS
from app.services.channel_notices import DEFAULT_LANGUAGE, localized_reply, notice
from app.services.content_safety import prompt_label
from app.services.integration_credentials import UNAVAILABLE_INTEGRATIONS_META_KEY
from app.services.output_format import CONTENT_FORMAT_META_KEY

logger = logging.getLogger(__name__)

#: The chat send payload field carrying the mentioned agents, in mention order.
MENTIONED_AGENTS_FIELD = "mentioned_agent_ids"
#: Message metadata naming the agent that wrote a reply of a multi-agent turn.
ANSWERED_BY_KEY = "answered_by"
#: Message metadata flagging a note written in place of a mentioned agent.
MENTION_FAILED_KEY = "mention_failed"

#: Agents that may answer one message besides the conversation's own.
MAX_MENTIONED_AGENTS = 5
#: Ids read from one payload at most; anything past it is ignored.
_MAX_PARSED_IDS = 20
#: Wall-clock budget shared by the mentioned agents of one turn: one chat
#: loop's floor, so a turn naming several agents holds the conversation's
#: lock for at most one more loop than a turn naming none.
DISPATCH_BUDGET_SECONDS = CHAT_MIN_LOOP_TIMEOUT_SECONDS
#: How long a run may take to honour the budget's cancel before it is cut.
DISPATCH_GRACE_SECONDS = 30.0
#: How long a cut run is waited on once cancelled before it is left behind.
DISPATCH_ABANDON_SECONDS = 5.0

#: Runs left behind after a cut, held until they end so the loop does not drop them.
_ABANDONED_RUNS: set[asyncio.Future] = set()

#: Handover types a mentioned agent keeps: it may consult and delegate, never
#: transfer - a transfer would rebind the conversation to another agent.
MENTIONED_AGENT_HANDOVER_TYPES = frozenset({"consult", "delegate"})

#: ``quota_out`` reasons of a run the quota refused.
_QUOTA_REFUSED_REASONS = frozenset({"quota_exceeded", "quota_unavailable"})


class TurnRefusedError(Exception):
    """The quota refused a mentioned agent's action: no agent answers past it."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def quota_refused(quota: dict) -> bool:
    """Whether a run's metering outcome (its ``quota_out``) is a quota refusal."""
    return quota.get("reason") in _QUOTA_REFUSED_REASONS


@dataclass
class TurnMetering:
    """What the conversation agent's run of a multi-agent turn was charged.

    Every agent that runs on the turn is its own agentic action, charged by
    the chat quota gate of its own run. ``quota`` is the conversation
    agent's outcome (``quota_out`` of ``run_agent_chat`` / ``run_general_chat``),
    empty when that run never reached the gate (a missing or disabled agent).
    Its refusal ends the turn: the quota is the user's, so no mentioned agent
    could be charged either.
    """

    quota: dict = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        """The quota refused the conversation agent's run: nothing more runs on it."""
        return quota_refused(self.quota)


@dataclass(frozen=True)
class MentionTarget:
    """One mentioned agent: the row that answers, or the note that replaces it
    once the message already has its answering agents."""

    agent_id: str
    name: str | None
    icon: str | None = None
    agent: Any | None = None
    refusal: str | None = None
    #: Refused only because the message already has its answering agents.
    over_limit: bool = False

    def answered_by(self) -> dict[str, str] | None:
        """The attribution stored on the agent's message (``None`` when unknown)."""
        if not self.name:
            return None
        attribution = {"agent_id": self.agent_id, "agent_name": self.name}
        if self.icon:
            attribution["agent_icon"] = self.icon
        return attribution


@dataclass
class MentionReply:
    """What one agent (or the note in its place) said in the turn."""

    target: MentionTarget
    text: str
    failed: bool = False
    #: The trace ids of the run that wrote it (``load_latest_langfuse_ids``),
    #: set by the caller once the run ends; empty for a note no run wrote.
    trace: dict[str, str] = field(default_factory=dict, compare=False)
    #: The quota refused this agent's action: no agent answers after it.
    refused: bool = False
    #: The files the run that wrote it produced, set by the caller once the
    #: run ends; the reply's own message shows them.
    file_ids: list[str] = field(default_factory=list, compare=False)
    #: The integrations the run that wrote it could not use, set by
    #: the caller once the run ends; the reply's own message says so.
    unavailable_integrations: list[dict[str, str]] = field(default_factory=list, compare=False)
    #: How full the context of the run that wrote it was
    #: (``context_usage.context_usage_meta``), set by the caller once the run
    #: ends; the reply's own message stores it. Not part of :meth:`metadata`,
    #: which is attribution.
    context_usage: dict[str, Any] = field(default_factory=dict, compare=False)
    #: The markup the run wrote it in when not Markdown
    #: (``output_format.override_content_format``), set by the caller.
    content_format: str | None = field(default=None, compare=False)

    def metadata(self) -> dict[str, Any]:
        meta = reply_metadata(self.target, failed=self.failed)
        if self.unavailable_integrations:
            meta[UNAVAILABLE_INTEGRATIONS_META_KEY] = list(self.unavailable_integrations)
        if self.content_format:
            meta[CONTENT_FORMAT_META_KEY] = self.content_format
        return meta


def reply_metadata(target: MentionTarget, *, failed: bool = False) -> dict[str, Any]:
    """Message metadata attributing a reply of a multi-agent turn."""
    meta: dict[str, Any] = {}
    attribution = target.answered_by()
    if attribution:
        meta[ANSWERED_BY_KEY] = attribution
    if failed:
        meta[MENTION_FAILED_KEY] = True
    return meta


def attributed_reply(agent_name: str, text: str) -> str:
    """A reply as another agent reads it: labelled with the agent that wrote it."""
    return f"[Reply from {agent_name}]\n\n{text}"


#: Characters of an agent name the line-up keeps (``Agent.name`` holds 200).
_LINEUP_NAME_CHARS = 80
#: A mention key the line-up shows: the composer's ``@key`` grammar, bounded.
_LINEUP_KEY_RE = re.compile(r"[\w-]{1,64}")


def _lineup_entry(target: MentionTarget) -> str:
    """One agent of the line-up: its name on one line, quoted, and its ``@key``.

    The name is the agent owner's free text: made one safe line
    (``content_safety.prompt_label``) and JSON-quoted, so it can neither end
    the entry, open a line of its own nor pass for an untrusted-block marker.
    The key is shown only when it is one the composer could have inserted.
    """
    name = prompt_label(target.name, limit=_LINEUP_NAME_CHARS, fallback="Agent")
    entry = json.dumps(name, ensure_ascii=False)
    key = agent_mention_key(getattr(target.agent, "slug", None), target.name or "")
    if _LINEUP_KEY_RE.fullmatch(key):
        entry += f" (@{key})"
    return entry


def turn_lineup(after: list[MentionTarget] | None, *, answering_agent_id: Any = None) -> str | None:
    """What an agent of a multi-agent turn reads of the agents answering after it.

    ``after`` is the turn's targets past the agent answering now; a note in
    an agent's place (``agent is None``) is not one, and neither is the
    agent answering (``answering_agent_id``): a transfer may hand the message
    to an agent the user also mentioned, which then answers it here and is
    not asked again. ``None`` when nobody is left. Rendered by the chat run
    (``run_agent_chat`` / ``run_general_chat``), which appends it to the
    message the agent reads like a reference brief - never to the stored
    message, never to the system prompt. Without it the first agent, told
    nothing, offered to hand the message over or asked the user to, while
    the platform was about to ask them.
    """
    answering = str(answering_agent_id) if answering_agent_id else None
    entries = [_lineup_entry(t) for t in after or [] if t.agent is not None and t.agent_id != answering]
    if not entries:
        return None
    return (
        "[Agents answering after you in this turn]\n"
        f"The user's message also addresses {', '.join(entries)}. After your reply, "
        "each of them answers it in a reply of its own, in this order, reading the replies "
        "given before its own in this turn. "
        "Give your own part only: the message already reaches them, so there is "
        "nothing to forward or to ask the user to relay."
    )


def mention_note(agent_name: str | None, text: str) -> str:
    """A note written in place of an agent, as a later turn reads it.

    The platform wrote it, not the agent: labelled as a note so no agent reads
    a refusal or an error as words another agent said.
    """
    if agent_name:
        return f"[Note: {agent_name} did not answer]\n\n{text}"
    return f"[Note]\n\n{text}"


def history_label(meta: Any, text: str) -> str | None:
    """How a stored message of a multi-agent turn is read back, or None if unlabelled."""
    if not isinstance(meta, dict):
        return None
    name = answered_by_name(meta)
    if meta.get(MENTION_FAILED_KEY):
        return mention_note(name, text)
    if name:
        return attributed_reply(name, text)
    return None


def answered_by_name(meta: Any) -> str | None:
    """The agent name a message's metadata attributes it to, if any."""
    if not isinstance(meta, dict):
        return None
    attribution = meta.get(ANSWERED_BY_KEY)
    if not isinstance(attribution, dict):
        return None
    name = attribution.get("agent_name")
    return name if isinstance(name, str) and name.strip() else None


#: The @mention grammar of the web composer (``parseAgentMentionTokens`` in
#: ``frontend/src/lib/utils.ts``): the ``@`` must not follow a word character
#: or a hyphen, so an e-mail address never reads as a mention.
_MENTION_TOKEN_RE = re.compile(r"(?<![\w-])@([\w-]+)")

#: Bold markers of the channels whose native format is not Markdown's ``**``.
_CHANNEL_BOLD = {"slack": "*", "whatsapp": "*", "telegram": "*", "google_chat": "*"}


def parse_mention_tokens(text: str) -> list[str]:
    """The ``@key`` tokens of a message, lower-cased, in order, each once."""
    tokens: list[str] = []
    for match in _MENTION_TOKEN_RE.finditer(text or ""):
        token = match.group(1).lower()
        if token not in tokens:
            tokens.append(token)
    return tokens


def agent_mention_key(slug: str | None, name: str) -> str:
    """The key an agent is mentioned by (``agentMentionKey`` in the frontend)."""
    return (slug or re.sub(r"\s+", "-", name.lower())).lower()


async def visible_agents(db: AsyncSession, user_id: uuid.UUID) -> list[Any]:
    """The agents a user can mention by key: their library - own, group-shared
    and company-managed - for an admin too, like the web composer's picker,
    so a typed key never names a colleague's private agent."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.agent import Agent

    group_ids = await get_user_group_ids(db, user_id)
    return list((await db.execute(user_visible_query(Agent, user_id, group_ids))).scalars().all())


async def mentioned_agent_ids_from_text(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    text: str,
    admit: Callable[[Any], bool] | None = None,
) -> list[str]:
    """The agents a channel message mentions as ``@key`` tokens, in order.

    A messaging channel has no agent picker, so the mention is the typed key -
    the one the web composer inserts. Only agents the user can see and that
    ``admit`` accepts are candidates, so a key shared by several agents names
    the one that may answer; a token naming none of them is plain text (a
    person, a handle, a platform mention such as Slack's ``<@U123>``).
    """
    tokens = parse_mention_tokens(text)
    if not tokens:
        return []
    ids_by_key: dict[str, str] = {}
    for agent in await visible_agents(db, user_id):
        if admit is None or admit(agent):
            ids_by_key.setdefault(agent_mention_key(agent.slug, agent.name), str(agent.id))
    return [ids_by_key[token] for token in tokens if token in ids_by_key]


def compose_channel_reply(channel: str, replies: list[MentionReply]) -> str:
    """One channel message holding every reply of the turn, each under its agent.

    A channel adapter delivers one message per inbound message; the replies are
    still stored as separate messages. The label uses the channel's own bold
    syntax, since each agent already wrote its reply in that format. A reply
    with no attributed agent (the General Assistant's) is not labelled.
    """
    marker = _CHANNEL_BOLD.get(channel, "**")
    sections = []
    for reply in replies:
        if reply.target.name:
            sections.append(f"{marker}{reply.target.name}{marker}\n{reply.text}")
        else:
            sections.append(reply.text)
    return "\n\n".join(sections)


def parse_mentioned_agent_ids(raw: Any) -> list[str]:
    """The mentioned agent ids of a send payload, in mention order.

    Only a list of UUID strings is read (a string would iterate per character);
    duplicates keep their first position, so an agent mentioned twice answers
    once.
    """
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for value in raw[:_MAX_PARSED_IDS]:
        if not isinstance(value, str):
            continue
        try:
            canonical = str(uuid.UUID(value.strip()))
        except ValueError:
            continue
        if canonical not in ids:
            ids.append(canonical)
    return ids


def folded_mention_ids(raw: Any, answering_agent_id: Any) -> list[str]:
    """The agents a message folded into a running turn is owed an answer by.

    A message arriving while a turn runs is steering for the agent already
    answering; the other agents it names ride the fold
    (``SteeringPayload.mentioned_agent_ids``) and answer it once the
    turn's own agents have. Empty when it names no other agent, so the fold
    is the shape it always was.
    """
    answering = str(answering_agent_id) if answering_agent_id else None
    return [i for i in parse_mentioned_agent_ids(raw) if i != answering]


@dataclass
class FoldedMentions:
    """A message folded into a running turn, and the agents it mentions.

    The agent already answering reads the message as steering; the agents it
    mentions answer it after the turn's own (:func:`answer_folded_mentions`),
    with its text and attachments (``files``, resolved ``FileRef`` objects).
    ``payload`` is the steering payload it came in, handed back when the turn
    ends before asking them.
    """

    text: str
    agent_ids: list[str]
    files: list = field(default_factory=list)
    payload: Any = None


def folded_mentions(payload: Any, files: list | None = None) -> FoldedMentions | None:
    """What a drained steering payload owes, or ``None`` when it names no agent."""
    agent_ids = parse_mentioned_agent_ids(getattr(payload, "mentioned_agent_ids", None))
    if not agent_ids:
        return None
    return FoldedMentions(text=payload.text, agent_ids=agent_ids, files=list(files or []), payload=payload)


async def resolve_mention_targets(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    raw_ids: Any,
    answering_agent_id: str | None,
    language: str = DEFAULT_LANGUAGE,
) -> list[MentionTarget]:
    """The agents that answer after the conversation's own, in mention order.

    Only an agent the user can chat with answers: one a conversation with it
    would admit - visible to the user (own, group-shared or company-managed;
    admins see all), enabled, open to chat - on a deployment where chat is
    licensed. Any other mention is ignored the way an unknown one is: no
    reply, no note, the agent is not named. The conversation's own agent and
    repeated mentions are dropped. Past :data:`MAX_MENTIONED_AGENTS`, an agent
    that could have answered keeps its place with a note, in *language*.
    """
    ids = [i for i in parse_mentioned_agent_ids(raw_ids) if i != answering_agent_id]
    if not ids:
        return []

    from fastapi import HTTPException

    from app.api.v1.helpers import get_user_agent, get_user_group_ids
    from app.api.v1.platform import is_agentic_ai_licensed
    from app.models.user import User

    if not await is_agentic_ai_licensed(db):
        return []
    # ``User.is_admin`` is a property over ``role``; read the column.
    is_admin = (await db.scalar(select(User.role).where(User.id == user_id))) == "admin"
    group_ids = await get_user_group_ids(db, user_id)

    targets: list[MentionTarget] = []
    answering = 0
    for agent_id in ids:
        try:
            agent = await get_user_agent(db, user_id, agent_id, group_ids, is_admin=is_admin)
        except HTTPException:
            continue
        if not agent.enabled or agent.disable_chat:
            continue
        name = agent.name
        icon = getattr(agent, "icon", None) or None
        if answering >= MAX_MENTIONED_AGENTS:
            targets.append(
                MentionTarget(
                    agent_id=agent_id,
                    name=name,
                    icon=icon,
                    refusal=notice(
                        "mention_over_limit", language, name=name, limit=MAX_MENTIONED_AGENTS
                    ),
                    over_limit=True,
                )
            )
            continue
        answering += 1
        targets.append(MentionTarget(agent_id=agent_id, name=name, icon=icon, agent=agent))
    return targets


def build_mention_history(
    history: list[ChatMessage], user_message: str, earlier: list[MentionReply]
) -> list[ChatMessage]:
    """The history a mentioned agent reads: the thread, then the turn so far.

    The turn so far is the user's message followed by every reply already given
    in it, each labelled with its agent. The caller then passes the user's
    message again as the one this agent answers - the same full message every
    mentioned agent receives. Attachments stay on that final message only.
    """
    if not earlier:
        return list(history)
    return [
        *history,
        ChatMessage(role="user", content=user_message),
        *(
            ChatMessage(role="assistant", content=attributed_reply(reply.target.name or "Agent", reply.text))
            for reply in earlier
        ),
    ]


def folded_mention_history(
    history: list[ChatMessage], user_message: str, replies: list[MentionReply]
) -> list[ChatMessage]:
    """The thread after a message and its replies, as a folded message's agents read it.

    The message, then every reply it got, each labelled with its agent; a
    note is not read. Built from the history the turn loaded (compacted), not
    reloaded, and extended by each folded message answered after it.
    """
    return [
        *history,
        ChatMessage(role="user", content=user_message),
        *(
            ChatMessage(role="assistant", content=attributed_reply(reply.target.name or "Agent", reply.text))
            for reply in replies
            if not reply.failed
        ),
    ]


#: ``run_agent(agent, history, cancel, agents_after)``: ``agents_after`` is the
#: targets still to answer after this agent, handed to the run's
#: ``agents_after`` (rendered by :func:`turn_lineup`).
RunAgent = Callable[[Any, list[ChatMessage], asyncio.Event, list[MentionTarget]], Awaitable[str]]
OnStart = Callable[[MentionTarget, int, int], Awaitable[None]]
OnReply = Callable[[MentionReply], Awaitable[None]]


async def conversation_agent_target(db: AsyncSession, agent_id: Any) -> MentionTarget:
    """The conversation's own agent as the first answerer of a multi-agent turn.

    A conversation with no agent (the General Assistant) is not labelled: its
    reply reads as the assistant's, the mentioned agents' as theirs.
    """
    if not agent_id:
        return MentionTarget(agent_id="", name=None)
    from app.models.agent import Agent

    row = (
        await db.execute(select(Agent.name, Agent.icon).where(Agent.id == agent_id))
    ).one_or_none()
    if row is None:
        return MentionTarget(agent_id=str(agent_id), name=None)
    return MentionTarget(agent_id=str(agent_id), name=row[0], icon=row[1] or None)


async def run_on_own_session(
    agent: Any, run: Callable[[AsyncSession, Any], Awaitable[str]]
) -> str:
    """Run one mentioned agent on a database session of its own.

    The request's session is never handed to a mentioned run: a run that fails
    leaves its own session to roll back instead of poisoning the next agent's
    and the reply persistence's, and a run the dispatch left behind cannot use
    the request's session concurrently with it. The agent is re-loaded in the
    run's session; what the run wrote is committed when it returns.
    """
    from app.core.database import async_session
    from app.models.agent import Agent

    async with async_session() as own_db:
        own_agent = await own_db.get(Agent, agent.id)
        if own_agent is None:
            raise LookupError(f"{getattr(agent, 'name', 'The agent')} no longer exists")
        text = await run(own_db, own_agent)
        await own_db.commit()
        return text


async def _cancel_when(parent: asyncio.Event | None, child: asyncio.Event, seconds: float) -> None:
    """Set *child* when *parent* is set or after *seconds*, whichever comes first."""
    try:
        if parent is None:
            await asyncio.sleep(seconds)
        else:
            await asyncio.wait_for(parent.wait(), timeout=seconds)
    except TimeoutError:
        pass
    child.set()


def _timeout_note(targets: list[MentionTarget], index: int, language: str = DEFAULT_LANGUAGE) -> str:
    """The note written in place of the agent the budget ran out on."""
    fallback = notice("mention_agent", language)
    note = notice("mention_timeout", language, name=targets[index].name or fallback)
    skipped = [t.name or fallback for t in targets[index + 1 :] if t.agent is not None]
    if skipped:
        note += " " + notice("mention_not_asked", language, names=", ".join(skipped))
    return note


async def dispatch_mentions(
    targets: list[MentionTarget],
    *,
    history: list[ChatMessage],
    user_message: str,
    earlier: list[MentionReply],
    run_agent: RunAgent,
    cancel_event: asyncio.Event | None,
    on_start: OnStart | None = None,
    on_reply: OnReply | None = None,
    describe_error: Callable[[Exception], str] = format_exception_message,
    budget_seconds: float = DISPATCH_BUDGET_SECONDS,
    grace_seconds: float = DISPATCH_GRACE_SECONDS,
    abandon_seconds: float = DISPATCH_ABANDON_SECONDS,
    stopped: asyncio.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
    language: str = DEFAULT_LANGUAGE,
) -> list[MentionReply]:
    """Run the mentioned agents one after another and return their replies.

    ``earlier`` holds the replies already given in the turn (the conversation
    agent's) and grows with every reply that succeeds, so each agent reads the
    ones before it. ``run_agent(agent, history, cancel, agents_after)`` runs
    one agent and returns its answer; ``agents_after`` is the targets still to
    answer after it (:func:`turn_lineup`), and ``cancel`` is set when the user cancels the turn or the
    shared budget runs out, and a run that ignores it is cut after
    ``grace_seconds``: it is cancelled, waited on for at most
    ``abandon_seconds``, then left behind, and ``stopped`` is set the moment it
    is cut so the caller drops whatever it still emits. The dispatch never
    waits on a run that will not stop, so the turn lock is always released.

    A refused target yields its note without running. A run that raises, or
    answers nothing, yields a failure note and the next agent still answers. A
    run raising :class:`TurnRefusedError` (the quota refused its action)
    yields its refusal and stops the dispatch. A user cancel stops the
    dispatch after the running agent (whose reply is the loop's cancellation
    text); an exhausted budget stops it with a note naming the agents not
    asked. ``on_start`` is awaited before an agent runs and ``on_reply`` after
    every reply, notes included, in order. The notes are written in
    *language* (the reader's on a channel).
    """
    replies: list[MentionReply] = []

    async def _add(reply: MentionReply) -> None:
        replies.append(reply)
        if on_reply is not None:
            await on_reply(reply)

    deadline = clock() + budget_seconds
    for index, target in enumerate(targets):
        if cancel_event is not None and cancel_event.is_set():
            break
        if target.agent is None:
            await _add(MentionReply(target, target.refusal or "", failed=True))
            continue
        remaining = deadline - clock()
        if remaining <= 0:
            await _add(MentionReply(target, _timeout_note(targets, index, language), failed=True))
            break
        if on_start is not None:
            await on_start(target, index, len(targets))
        agent_cancel = asyncio.Event()
        watcher = asyncio.create_task(_cancel_when(cancel_event, agent_cancel, remaining))
        run_task = asyncio.ensure_future(
            run_agent(
                target.agent,
                build_mention_history(history, user_message, earlier),
                agent_cancel,
                targets[index + 1 :],
            )
        )
        timed_out = False
        failed = False
        refused = False
        text = ""
        try:
            try:
                finished = await _await_run(run_task, agent_cancel, remaining, grace_seconds)
            except asyncio.CancelledError:
                run_task.cancel()
                raise
            if finished:
                text = run_task.result()
            else:
                await _stop_run(run_task, target, stopped, abandon_seconds)
                if cancel_event is not None and cancel_event.is_set():
                    text = localized_reply(_cancelled_message(), language)
                else:
                    timed_out = True
        except TurnRefusedError as exc:
            # The quota refused this agent's action: the quota is the user's,
            # so no agent answers past it.
            refused = True
            text = localized_reply(exc.text, language)
        except Exception as exc:
            logger.warning(
                "Mentioned agent %s failed mid-turn: %s",
                target.agent_id,
                format_exception_message(exc),
                exc_info=True,
            )
            failed = True
            text = notice("mention_failed", language, name=target.name, reason=describe_error(exc))
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        if refused:
            await _add(MentionReply(target, text, failed=True, refused=True))
            break
        user_cancelled = cancel_event is not None and cancel_event.is_set()
        if not timed_out and not user_cancelled and agent_cancel.is_set() and _is_cancellation(text):
            # The run honoured the budget's cancel: it ran out of time.
            timed_out = True
        if timed_out:
            await _add(MentionReply(target, _timeout_note(targets, index, language), failed=True))
            break
        if not failed and not (text or "").strip():
            failed = True
            text = notice("mention_empty", language, name=target.name)
        reply = MentionReply(target, text, failed=failed)
        await _add(reply)
        if user_cancelled:
            break
        if not failed:
            earlier.append(reply)
    return replies


async def answer_folded_mentions(
    folded: list[FoldedMentions],
    *,
    db: AsyncSession,
    user_id: uuid.UUID,
    answering_agent_id: str | None,
    history: list[ChatMessage],
    run_agent_for: Callable[[FoldedMentions], RunAgent],
    cancel_event: asyncio.Event | None,
    deadline: float,
    on_start: OnStart | None = None,
    on_reply: OnReply | None = None,
    describe_error: Callable[[Exception], str] = format_exception_message,
    stopped: asyncio.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
    language: str = DEFAULT_LANGUAGE,
) -> list[MentionReply]:
    """Answer the messages folded into a turn with the agents they mention.

    ``folded`` is consumed from the front, in the order the messages arrived,
    and may still grow while it runs (a turn's bridge keeps draining). Each
    message is answered as if it had started the turn: its targets are
    resolved against ``answering_agent_id``, the conversation's agent now, and
    :func:`dispatch_mentions` runs them on ``run_agent_for(message)`` - its own
    text and attachments. They read ``history`` (the thread up to and with the
    turn's replies, :func:`folded_mention_history`) grown by each folded
    message answered before theirs, and share the turn's ``deadline``, its
    cancel and its metering: a user cancel or a refused charge ends it and
    leaves ``folded`` empty, since the agents not asked are not asked later
    either. A message whose agents cannot be resolved is skipped.
    """
    replies: list[MentionReply] = []
    thread = list(history)
    while folded:
        if cancel_event is not None and cancel_event.is_set():
            break
        message = folded.pop(0)
        try:
            targets = await resolve_mention_targets(
                db,
                user_id=user_id,
                raw_ids=message.agent_ids,
                answering_agent_id=answering_agent_id,
                language=language,
            )
        except Exception:
            logger.warning(
                "Could not resolve the agents a folded message mentions; they do not answer it",
                exc_info=True,
            )
            continue
        if not targets:
            continue
        answered = await dispatch_mentions(
            targets,
            history=thread,
            user_message=message.text,
            earlier=[],
            run_agent=run_agent_for(message),
            cancel_event=cancel_event,
            on_start=on_start,
            on_reply=on_reply,
            describe_error=describe_error,
            budget_seconds=deadline - clock(),
            stopped=stopped,
            clock=clock,
            language=language,
        )
        replies.extend(answered)
        if any(reply.refused for reply in answered):
            break
        thread = folded_mention_history(thread, message.text, answered)
    folded.clear()
    return replies


def _cancelled_message() -> str:
    from app.tools.agent_loop import _CANCELLED_MESSAGE

    return _CANCELLED_MESSAGE


def _is_cancellation(text: str) -> bool:
    return (text or "").strip() == _cancelled_message()


async def _await_run(
    run_task: asyncio.Future, cancel: asyncio.Event, remaining: float, grace_seconds: float
) -> bool:
    """Wait for one run; True when it finished, False when it must be cut.

    The run has the remaining budget, and ``grace_seconds`` more once
    ``cancel`` is set (by the user or by the budget) to wind down.
    """
    cancel_wait = asyncio.ensure_future(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {run_task, cancel_wait},
            timeout=remaining + grace_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if run_task not in done and cancel_wait in done:
            done, _ = await asyncio.wait({run_task}, timeout=grace_seconds)
        return run_task in done
    finally:
        cancel_wait.cancel()


def _forget_abandoned(task: asyncio.Future) -> None:
    _ABANDONED_RUNS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("An abandoned mentioned agent run ended with: %s", format_exception_message(task.exception()))


async def _stop_run(
    run_task: asyncio.Future,
    target: MentionTarget,
    stopped: asyncio.Event | None,
    abandon_seconds: float,
) -> None:
    """Cut a run that outlived its time: silence it, cancel it, never wait on it for long."""
    if stopped is not None:
        stopped.set()
    run_task.cancel()
    await asyncio.wait({run_task}, timeout=abandon_seconds)
    if run_task.done():
        if not run_task.cancelled():
            run_task.exception()
        return
    logger.warning(
        "Mentioned agent %s did not stop within %.0fs of being cancelled; leaving it behind",
        target.agent_id,
        abandon_seconds,
    )
    _ABANDONED_RUNS.add(run_task)
    run_task.add_done_callback(_forget_abandoned)
