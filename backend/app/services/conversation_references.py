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

"""Conversations referenced from a chat message with ``@``.

The composer's @ menu lists conversations next to agents: picking one inserts
``@<key>`` in the text and sends the conversation's id in
``referenced_conversation_ids``. The contract, for every chat entry point (the
web chat's streamed and REST send, the embedded chat of OpenCTI / OpenAEV):

* **What is offered**: `search_reference_candidates` - the conversations the
  user can open that are worth pointing at: their own (web chat and embedded
  chat) and those shared with them by name, searched by title, most recent
  first. ``org_read`` conversations are left out, for the reason the sidebar's
  shared list leaves them out: a link anyone may follow is not an invitation.
* **What is accepted**: `resolve_referenced_conversations` - only what the
  SENDER can open (`chat.conversation_access`, the one read rule), never the
  conversation being written in, at most `MAX_REFERENCED_CONVERSATIONS`. What
  is not accepted is said in the brief, never dropped silently.
* **What is stored**: ``conversation_refs`` on the user message - the id, the
  title and the key the message was sent with. Never the content: a reader of
  this conversation sees the title (the message shows its key anyway) and can
  open the other conversation only if they may.
* **What the agent reads**:

  - with the message, `build_reference_brief`: the referenced conversation's
    rolling summary, pinned directives and last messages, fenced as untrusted
    content (another person may have written them) - never system prompt text;
  - on every turn of a conversation holding references, the
    ``read_conversation`` tool (`builtin_tools/conversation_reader.py`), which
    reads only those conversations and re-checks the reader's access on each
    call, so a share revoked meanwhile is honoured at once.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select

from app.core.utils import safe_uuid

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.chat import Conversation
    from app.models.user import User

#: The field a send carries, in the web chat's send and every REST body.
REFERENCED_CONVERSATIONS_FIELD = "referenced_conversation_ids"
#: The key of the user message's metadata holding what it referenced.
CONVERSATION_REFS_META_KEY = "conversation_refs"
#: Conversations one message may reference; the rest are named as skipped.
MAX_REFERENCED_CONVERSATIONS = 5
#: Ids read off a request before anything is resolved (a bound on a hostile list).
_MAX_PARSED_IDS = 20
#: The channels the @ menu offers the user's own conversations from: the web
#: chat and the embedded chat (``platform.PLATFORM_CHAT_CHANNEL``). External
#: channel threads (Slack, Teams, ...) are read on the agent's Chats tab.
REFERENCE_CHANNELS = ("web", "api")
#: Suggestions the @ menu asks for at most.
MAX_REFERENCE_CANDIDATES = 20
#: Referenced conversations the reader tool's description names.
MAX_LISTED_REFERENCES = 10

_KEY_MAX = 48
_TITLE_MAX = 120
#: The brief: what one referenced conversation costs the message, at most.
_BRIEF_MESSAGES = 4
_BRIEF_MESSAGE_CHARS = 600
_BRIEF_SUMMARY_CHARS = 1500
_BRIEF_DIRECTIVES_CHARS = 800
#: Messages scanned to find the brief's last ones (hidden and empty rows skipped).
_BRIEF_SCAN = 20


def conversation_reference_key(title: str | None, conversation_id: uuid.UUID | str) -> str:
    """The ``@key`` a conversation is tagged with: its title as a slug.

    Accents are folded (``Réunion`` -> ``reunion``) and anything but letters
    and digits becomes a hyphen, so the key reads as the agent mention grammar
    does (``[\\w-]+``). A title with no Latin letter or digit at all (a
    Japanese title) falls back to ``conversation-`` and the id's first six
    characters. The key is a label for the person reading the message; the
    conversation is always identified by its id.
    """
    folded = unicodedata.normalize("NFKD", title or "")
    plain = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    if len(slug) > _KEY_MAX:
        cut = slug[:_KEY_MAX]
        # Cut at a word boundary when there is one.
        slug = (cut.rsplit("-", 1)[0] if "-" in cut else cut).strip("-")
    return slug or f"conversation-{str(conversation_id)[:6]}"


def _clean_title(title: str | None) -> str:
    text = " ".join((title or "").split())
    if len(text) > _TITLE_MAX:
        text = text[: _TITLE_MAX - 1].rstrip() + "…"
    return text or "Untitled conversation"


def parse_referenced_conversation_ids(raw: Any) -> list[uuid.UUID]:
    """The conversation ids a request names, in order, de-duplicated.

    Anything that is not a list of id strings reads as no reference: a client
    that never sends the field and a malformed one both get the plain message.
    """
    if not isinstance(raw, list):
        return []
    ids: list[uuid.UUID] = []
    for item in raw[:_MAX_PARSED_IDS]:
        if not isinstance(item, str):
            continue
        parsed = safe_uuid(item)
        if parsed is not None and parsed not in ids:
            ids.append(parsed)
    return ids


@dataclass(frozen=True)
class ReferencedConversation:
    """One conversation a message references, as its sender may open it."""

    id: uuid.UUID
    title: str
    key: str
    is_own: bool
    owner_name: str | None
    agent_name: str | None
    updated_at: datetime | None

    def meta(self) -> dict[str, str]:
        return {"conversation_id": str(self.id), "title": self.title, "key": self.key}


@dataclass
class ReferenceResolution:
    """What a send's ``referenced_conversation_ids`` came to."""

    references: list[ReferencedConversation] = field(default_factory=list)
    #: Ids that name nothing the sender can open (gone, never shared, revoked).
    unavailable: int = 0
    #: Ids past `MAX_REFERENCED_CONVERSATIONS`.
    over_limit: int = 0

    def __bool__(self) -> bool:
        return bool(self.references or self.unavailable or self.over_limit)

    def meta(self) -> list[dict[str, str]]:
        return [ref.meta() for ref in self.references]


async def _load_referenced(
    db: AsyncSession, user: User, ids: list[uuid.UUID]
) -> dict[uuid.UUID, ReferencedConversation]:
    """The conversations among *ids* that *user* can open, by id."""
    from app.api.v1.chat import conversation_access
    from app.models.agent import Agent
    from app.models.chat import Conversation
    from app.models.user import User as UserModel

    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Conversation, Agent.name)
            .outerjoin(Agent, Agent.id == Conversation.agent_id)
            .where(Conversation.id.in_(ids))
        )
    ).all()
    readable: list[tuple[Conversation, str | None]] = []
    for conv, agent_name in rows:
        if await conversation_access(db, user, conv) is not None:
            readable.append((conv, agent_name))
    owner_ids = {conv.user_id for conv, _ in readable if conv.user_id and conv.user_id != user.id}
    owner_names: dict[uuid.UUID, str] = {}
    if owner_ids:
        for owner_id, full_name, email in (
            await db.execute(
                select(UserModel.id, UserModel.full_name, UserModel.email).where(
                    UserModel.id.in_(owner_ids)
                )
            )
        ).all():
            owner_names[owner_id] = full_name or email
    return {
        conv.id: ReferencedConversation(
            id=conv.id,
            title=_clean_title(conv.title),
            key=conversation_reference_key(conv.title, conv.id),
            is_own=conv.user_id == user.id,
            owner_name=None if conv.user_id == user.id else owner_names.get(conv.user_id),
            agent_name=agent_name,
            updated_at=conv.updated_at,
        )
        for conv, agent_name in readable
    }


async def resolve_referenced_conversations(
    db: AsyncSession,
    user: User | uuid.UUID,
    raw: Any,
    *,
    current_conversation_id: uuid.UUID | str | None,
) -> ReferenceResolution:
    """Resolve a send's ``referenced_conversation_ids`` against what *user* may open.

    *user* is the sender, or their id (the web chat's turn runner holds only the id).
    The conversation being written in is ignored (a message never needs to
    point at its own thread); the rest is kept in request order, up to
    `MAX_REFERENCED_CONVERSATIONS`, and what is refused is counted so the
    brief can say so.
    """
    current = safe_uuid(str(current_conversation_id)) if current_conversation_id else None
    ids = [cid for cid in parse_referenced_conversation_ids(raw) if cid != current]
    if not ids:
        return ReferenceResolution()
    kept, over = ids[:MAX_REFERENCED_CONVERSATIONS], ids[MAX_REFERENCED_CONVERSATIONS:]
    if isinstance(user, uuid.UUID):
        from app.models.user import User as UserModel

        sender = await db.get(UserModel, user)
        if sender is None:
            return ReferenceResolution(unavailable=len(kept), over_limit=len(over))
        user = sender
    loaded = await _load_referenced(db, user, kept)
    references = [loaded[cid] for cid in kept if cid in loaded]
    return ReferenceResolution(
        references=references,
        unavailable=len(kept) - len(references),
        over_limit=len(over),
    )


def with_conversation_refs(metadata_json: str | None, resolution: ReferenceResolution) -> str | None:
    """*metadata_json* with the message's ``conversation_refs`` added."""
    if not resolution.references:
        return metadata_json
    meta: dict[str, Any] = {}
    if metadata_json:
        try:
            parsed = json.loads(metadata_json)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            meta = parsed
    meta[CONVERSATION_REFS_META_KEY] = resolution.meta()
    return json.dumps(meta)


def read_conversation_refs(meta: Any) -> list[dict[str, str]]:
    """The ``conversation_refs`` of a stored message's metadata, validated."""
    if not isinstance(meta, dict):
        return []
    raw = meta.get(CONVERSATION_REFS_META_KEY)
    if not isinstance(raw, list):
        return []
    refs: list[dict[str, str]] = []
    for item in raw[:MAX_REFERENCED_CONVERSATIONS]:
        if not isinstance(item, dict):
            continue
        cid = safe_uuid(str(item.get("conversation_id") or ""))
        if cid is None:
            continue
        title = item.get("title")
        key = item.get("key")
        refs.append(
            {
                "conversation_id": str(cid),
                "title": _clean_title(title if isinstance(title, str) else None),
                "key": key
                if isinstance(key, str) and re.fullmatch(r"[a-z0-9-]{1,64}", key)
                else conversation_reference_key(title if isinstance(title, str) else None, cid),
            }
        )
    return refs


def visible_message_filter(conversation_id: uuid.UUID) -> Any:
    """The messages of a conversation a reader sees: user and assistant text.

    Tool rows, assistant rows that only called tools and hidden background-task
    wakes (``parent_wake``) are left out, as the conversation list counts them.
    """
    from app.models.chat import Message
    from app.services.chat_messages import message_shown_clause

    return and_(
        Message.conversation_id == conversation_id,
        Message.role.in_(("user", "assistant")),
        Message.content != "",
        message_shown_clause(),
    )


def message_speaker(role: str, metadata_json: str | None, agent_name: str | None) -> str:
    """Who wrote a message, as the brief and the reader tool label it."""
    if role == "user":
        return "user"
    from app.services.mention_dispatch import answered_by_name

    meta: Any = None
    if metadata_json:
        try:
            meta = json.loads(metadata_json)
        except (TypeError, ValueError):
            meta = None
    name = answered_by_name(meta) if isinstance(meta, dict) else None
    return f"assistant ({name or agent_name})" if (name or agent_name) else "assistant"


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()} [... {len(text) - limit} more characters]"


async def _brief_body(db: AsyncSession, ref: ReferencedConversation) -> tuple[str, int]:
    """The fenced half of a reference's brief, and its visible message count."""
    from app.models.chat import Conversation, Message
    from app.services.conversation_memory import parse_compaction_state

    total = (
        await db.execute(
            select(func.count()).select_from(Message).where(visible_message_filter(ref.id))
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(Message.role, Message.content, Message.metadata_json)
            .where(visible_message_filter(ref.id))
            .order_by(Message.sequence.desc())
            .limit(_BRIEF_SCAN)
        )
    ).all()
    last = list(reversed(rows[:_BRIEF_MESSAGES]))
    state = parse_compaction_state(
        (
            await db.execute(
                select(Conversation.compaction_state_json).where(Conversation.id == ref.id)
            )
        ).scalar_one_or_none()
    )
    lines = [f"Title: {ref.title}"]
    if ref.owner_name:
        lines.append(f"Shared with the user by: {ref.owner_name}")
    if state and state.get("directives", "").strip():
        lines += ["", "Directives the user set there:", _cut(state["directives"], _BRIEF_DIRECTIVES_CHARS)]
    if state and state.get("summary", "").strip():
        lines += ["", "Summary of its earlier messages:", _cut(state["summary"], _BRIEF_SUMMARY_CHARS)]
    if last:
        lines += ["", f"Its last {len(last)} of {total} messages:"]
        for role, content, metadata_json in last:
            speaker = message_speaker(role, metadata_json, ref.agent_name)
            lines.append(f"[{speaker}] {_cut(content or '', _BRIEF_MESSAGE_CHARS)}")
    else:
        lines += ["", "It holds no message yet."]
    return "\n".join(lines), total


async def build_reference_brief(db: AsyncSession, resolution: ReferenceResolution) -> str | None:
    """The block appended to the CURRENT user message for its references.

    It rides on the message the agent reads, never on the stored message nor
    in the system prompt: what the user wrote is kept as written, and only the
    turn that carries the reference pays for the brief. Each conversation's
    words are fenced as untrusted content; outside the fences there are only
    keys, ids and dates the platform wrote.
    """
    if not resolution:
        return None
    from app.services.content_safety import wrap_untrusted

    parts = ["[Conversations the user referenced in this message]"]
    for ref in resolution.references:
        body, total = await _brief_body(db, ref)
        updated = f", last updated {ref.updated_at:%Y-%m-%d}" if ref.updated_at else ""
        whose = "the user's own" if ref.is_own else "shared with the user"
        parts.append(
            f"@{ref.key} is the conversation {ref.id} ({whose}, {total} messages{updated})."
        )
        parts.append(wrap_untrusted(body, source="a referenced conversation"))
        parts.append(
            f'To read all of it, call read_conversation(conversation_id="{ref.id}").'
        )
    if resolution.unavailable:
        parts.append(
            f"[Note: {resolution.unavailable} referenced conversation(s) could not be "
            "opened: they no longer exist or the user cannot open them.]"
        )
    if resolution.over_limit:
        parts.append(
            f"[Note: a message references at most {MAX_REFERENCED_CONVERSATIONS} "
            f"conversations; {resolution.over_limit} more were not read.]"
        )
    return "\n\n".join(parts)


@dataclass(frozen=True)
class StoredReference:
    """A reference as a user message stored it: the conversation and the
    ``@key`` the message was sent with - never the conversation's live title,
    which only a reader allowed to open it may learn."""

    id: uuid.UUID
    key: str


async def stored_references(
    db: AsyncSession, conversation_id: uuid.UUID | str | None
) -> list[StoredReference]:
    """The references the user messages of *conversation_id* hold.

    Newest reference first, each conversation once, with the key its newest
    reference was sent with. One query over the conversation's own user
    messages (``messages.conversation_id`` index). No access check: what the
    caller may open is `readable_references`'s to decide.
    """
    from app.models.chat import Message

    conv_id = safe_uuid(str(conversation_id)) if conversation_id else None
    if conv_id is None:
        return []
    rows = (
        await db.execute(
            select(Message.metadata_json)
            .where(
                Message.conversation_id == conv_id,
                Message.role == "user",
                Message.metadata_json.contains(f'"{CONVERSATION_REFS_META_KEY}"'),
            )
            .order_by(Message.sequence.desc())
            .limit(200)
        )
    ).scalars()
    refs: list[StoredReference] = []
    for metadata_json in rows:
        try:
            meta = json.loads(metadata_json or "")
        except (TypeError, ValueError):
            continue
        for ref in read_conversation_refs(meta):
            cid = uuid.UUID(ref["conversation_id"])
            if cid != conv_id and all(r.id != cid for r in refs):
                refs.append(StoredReference(id=cid, key=ref["key"]))
    return refs


async def referenced_conversation_ids(
    db: AsyncSession, conversation_id: uuid.UUID | str | None
) -> list[uuid.UUID]:
    """The ids of `stored_references`, newest first."""
    return [ref.id for ref in await stored_references(db, conversation_id)]


async def stored_references_safe(
    db: AsyncSession, conversation_id: uuid.UUID | str | None
) -> list[StoredReference]:
    """`stored_references`, or none when it cannot be read.

    For the turn deciding whether to carry ``read_conversation``: a failed read
    costs the turn the tool, never the turn. Run in a savepoint so a failed
    statement does not abort the turn's transaction.
    """
    if not conversation_id:
        return []
    try:
        async with db.begin_nested():
            return await stored_references(db, conversation_id)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Could not list the conversations referenced in %s", conversation_id, exc_info=True
        )
        return []


async def readable_references(
    db: AsyncSession, user: User | uuid.UUID | None, refs: list[StoredReference]
) -> list[StoredReference]:
    """*refs* the *user* can open now (`chat.conversation_access`), in order.

    What is not readable - deleted, never shared, a share revoked since the
    reference was made - is dropped without a trace: neither its existence
    nor its title reaches whoever asked.
    """
    from app.api.v1.chat import conversation_access
    from app.models.chat import Conversation
    from app.models.user import User as UserModel

    if not refs or user is None:
        return []
    reader = user if not isinstance(user, uuid.UUID) else await db.get(UserModel, user)
    if reader is None:
        return []
    rows = (
        await db.execute(select(Conversation).where(Conversation.id.in_([r.id for r in refs])))
    ).scalars().all()
    readable: set[uuid.UUID] = set()
    for conv in rows:
        if await conversation_access(db, reader, conv) is not None:
            readable.add(conv.id)
    return [ref for ref in refs if ref.id in readable]


#: Words of a search the title must hold at most (a bound on the statement).
_MAX_QUERY_WORDS = 8


def _query_words(query: str | None) -> list[str]:
    """The words of an @ menu search: split on spaces, hyphens and underscores.

    The composer sends what follows ``@``, where a hyphen is the only way to
    type a second word (a space ends the token): ``q3-threat`` must find
    "Q3 threat report" as ``q3 threat`` would.
    """
    return [word for word in re.split(r"[\s_-]+", query or "") if word][:_MAX_QUERY_WORDS]


async def search_reference_candidates(
    db: AsyncSession,
    user: User,
    *,
    query: str | None,
    limit: int,
    exclude: uuid.UUID | None,
) -> list[dict[str, Any]]:
    """What the @ menu offers: conversations *user* can open, by title.

    Their own (web chat and embedded chat, not archived) and those shared with
    them by name or through a group, most recently updated first, each holding
    at least one message. The search is `_query_words`: every word of the
    query, case-insensitive, somewhere in the title.
    """
    from app.api.v1.users import escape_like
    from app.models.chat import Conversation, Message
    from app.models.conversation_share import ConversationShare
    from app.models.group import GroupMember
    from app.models.user import User as UserModel

    member_group_ids = select(GroupMember.group_id).where(GroupMember.user_id == user.id)
    shared_ids = select(ConversationShare.conversation_id).where(
        or_(
            ConversationShare.shared_with_user_id == user.id,
            ConversationShare.shared_with_group_id.in_(member_group_ids),
        )
    )
    has_message = (
        select(Message.id).where(visible_message_filter(Conversation.id)).exists()
    )
    conditions = [
        or_(
            and_(
                Conversation.user_id == user.id,
                Conversation.channel.in_(REFERENCE_CHANNELS),
            ),
            and_(
                Conversation.id.in_(shared_ids),
                Conversation.channel == "web",
                Conversation.user_id != user.id,
            ),
        ),
        Conversation.is_archived.is_(False),
        has_message,
    ]
    if exclude is not None:
        conditions.append(Conversation.id != exclude)
    # What the user types after ``@`` is a key in the making: ``q3-threat``
    # for "Q3 threat report". Each word of it must be in the title.
    for word in _query_words(query):
        conditions.append(Conversation.title.ilike(f"%{escape_like(word)}%", escape="\\"))
    rows = (
        await db.execute(
            select(Conversation, UserModel.full_name, UserModel.email)
            .outerjoin(UserModel, UserModel.id == Conversation.user_id)
            .where(*conditions)
            .order_by(Conversation.updated_at.desc())
            .limit(max(1, min(limit, MAX_REFERENCE_CANDIDATES)))
        )
    ).all()
    return [
        {
            "id": str(conv.id),
            "title": _clean_title(conv.title),
            "key": conversation_reference_key(conv.title, conv.id),
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            "is_own": conv.user_id == user.id,
            "owner_name": None if conv.user_id == user.id else (full_name or email),
        }
        for conv, full_name, email in rows
    ]
