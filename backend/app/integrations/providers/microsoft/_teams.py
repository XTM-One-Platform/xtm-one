"""Teams helpers shared by the chat, channel and file tools.

ID checks and the Graph URL path segment a checked ID becomes, Teams
message links, and message rendering: in a listing, where a
message's text is cut only when it does not fit and then names the call that
reads it in full, and on its own, uncut.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from html import unescape as _html_unescape
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.parse import quote as _url_encode
from urllib.parse import unquote as _url_decode

from app.core.config import settings
from app.integrations.http import clip_text
from app.integrations.providers.graph_path import _validate_path_segment
from app.services.reaction_feedback import normalize_emoji

from ._common import (
    _BASE_URL,
    _PROVIDER,
    MicrosoftContext,
    _is_placeholder_id,
    _strip_html,
)
from ._voice_notes import listing_note

# Placeholder strings that LLM agents pass when they never resolved a real
# Microsoft Teams team/channel ID.  Sending them to Graph wastes a round-trip
# on a guaranteed 400 BadRequest ("na" passed as teamId and
# channelId returned "teamId needs to be a valid GUID.").
_INVALID_TEAMS_ID_PLACEHOLDERS: frozenset[str] = frozenset(
    {"n/a", "na", "none", "null", "unknown", "undefined", "placeholder", "-", "id", "channel_id", "team_id"}
)


def _is_placeholder_teams_id(value: str) -> bool:
    """True when ``value`` is empty, a known placeholder, or a template token."""
    return _is_placeholder_id(value, _INVALID_TEAMS_ID_PLACEHOLDERS)


def _is_teams_path_id(value: str) -> bool:
    """Whether a Teams ID stays one Graph URL path segment.

    The rule of ``graph_path``: no ``/``, ``\\``, ``?``, ``#``, ``%`` or
    whitespace, never ``.`` or ``..``. Every ``_validate_teams_*`` check and
    the Teams link parser refuse the same values, so an ID cannot move a
    request sent with the user's delegated token to another Graph endpoint.
    """
    return _validate_path_segment(value, "id") is None


def _teams_segment(value: object) -> str:
    """A Teams ID as one Graph URL path segment, percent-encoded.

    ``:`` and ``@`` are kept: a channel or chat ID (``19:...@thread.tacv2``)
    is sent as Teams writes it. An ID a tool takes is checked first
    (``_validate_teams_*``); one Graph returned is encoded as is.
    """
    return _url_encode(value.strip() if isinstance(value, str) else "", safe=":@")


def _thread_route(team_id: str, channel_id: str, root_id: str) -> dict[str, str] | None:
    """The channel thread a post or a reply went to, as an agent account routes it.

    *root_id* is the thread's root post: the post itself for a new message,
    the message replied to for a reply. ``None`` without one.
    """
    team, channel, root = (str(v or "").strip() for v in (team_id, channel_id, root_id))
    if not (team and channel and root):
        return None
    return {"kind": "channel", "team_id": team, "channel_id": channel, "root_id": root}


def _team_url(team_id: str) -> str:
    """The Graph URL of a team."""
    return f"{_BASE_URL}/teams/{_teams_segment(team_id)}"


def _channel_url(team_id: str, channel_id: str) -> str:
    """The Graph URL of a channel of a team."""
    return f"{_team_url(team_id)}/channels/{_teams_segment(channel_id)}"


def _channel_message_url(
    team_id: str,
    channel_id: str,
    message_id: str,
    parent_message_id: str,
    tool: str,
) -> tuple[str, str, str | None]:
    """Return ``(url, label, error)`` of a channel post, or of a thread reply when *parent_message_id* is set."""
    if err := _validate_teams_channel_ids(team_id, channel_id, tool):
        return "", "", err
    if err := _validate_teams_message_id(message_id, tool):
        return "", "", err
    messages = f"{_channel_url(team_id, channel_id)}/messages"
    parent = (parent_message_id or "").strip()
    if not parent:
        return f"{messages}/{_teams_segment(message_id)}", "channel message", None
    if err := _validate_teams_message_id(parent, tool):
        return "", "", err
    url = f"{messages}/{_teams_segment(parent)}/replies/{_teams_segment(message_id)}"
    return url, "channel reply", None


def _validate_teams_team_id(
    team_id: str,
    tool_name: str,
) -> str | None:
    """Validate ``team_id`` before making a Graph API call.

    Returns an actionable error string if the value is missing,
    whitespace-only, a known placeholder string, an unsubstituted
    template token that was never resolved to a real identifier, or not one
    URL path segment (:func:`_is_teams_path_id`).
    Returns ``None`` when the value looks usable.

    This guard prevents the tool from spending a Graph round-trip on a
    call that is guaranteed to fail with HTTP 400 (BadRequest) because the
    ID was never bound to a real Microsoft Teams GUID by the agent.
    """
    tid = team_id.strip() if isinstance(team_id, str) else ""
    if _is_placeholder_teams_id(tid) or not _is_teams_path_id(tid):
        return (
            f"Error: team_id is missing or invalid ({team_id!r}). "
            f"Use list_microsoft_teams_and_channels to find a valid team ID "
            f"before calling {tool_name}."
        )
    return None


def _validate_teams_channel_ids(
    team_id: str,
    channel_id: str,
    tool_name: str,
) -> str | None:
    """Validate ``team_id`` and ``channel_id`` before making a Graph API call.

    Like :func:`_validate_teams_team_id` but for tools that address a
    channel inside a team.  Returns an actionable error string when either
    value is missing or a placeholder, ``None`` when both look usable.
    """
    if err := _validate_teams_team_id(team_id, tool_name):
        return err
    cid = channel_id.strip() if isinstance(channel_id, str) else ""
    if _is_placeholder_teams_id(cid) or not _is_teams_path_id(cid):
        return (
            f"Error: channel_id is missing or invalid ({channel_id!r}). "
            f"Use list_microsoft_teams_and_channels to find a valid channel ID "
            f"before calling {tool_name}."
        )
    return None


def _validate_teams_message_id(
    message_id: str,
    tool_name: str,
) -> str | None:
    """Validate ``message_id`` before making a Graph API call.

    Returns an actionable error string if the value is missing, a known
    placeholder, an unsubstituted template token, or not one URL path
    segment. Returns ``None`` when the value looks usable.
    """
    mid = message_id.strip() if isinstance(message_id, str) else ""
    if _is_placeholder_id(mid, _INVALID_TEAMS_ID_PLACEHOLDERS | {"message_id"}) or not _is_teams_path_id(mid):
        return (
            f"Error: message_id is missing or invalid ({message_id!r}). "
            f"Use the message ID returned by send_microsoft_channel_message "
            f"or list_microsoft_channel_messages before calling {tool_name}."
        )
    return None


#: The reaction names Teams used before any emoji could be one, in Unicode.
_LEGACY_REACTIONS = {
    "like": "\U0001f44d",  # thumbs up
    "heart": "\u2764\ufe0f",  # red heart
    "laugh": "\U0001f606",  # laughing
    "surprised": "\U0001f62e",  # open mouth
    "sad": "\U0001f622",  # crying
    "angry": "\U0001f621",  # pouting
}


def teams_reaction_emoji(reaction_type: object) -> str | None:
    """A Teams ``reactionType`` as Unicode: a legacy name translated, an emoji as is.

    ``custom`` (an organization's own reaction) is no emoji: ``None``.
    """
    if not isinstance(reaction_type, str) or not reaction_type or reaction_type == "custom":
        return None
    return _LEGACY_REACTIONS.get(reaction_type.lower(), reaction_type)


def teams_reaction_type(reaction: object) -> str | None:
    """*reaction* as Graph's ``reactionType`` (Unicode), or ``None`` when it is no emoji.

    A Teams legacy name (``like``, ``heart``...) becomes its emoji, and the
    emoji rules of reaction feedback decide what is one. An emoji is
    sent as given, so a skin tone is kept and ``unsetReaction`` removes what
    ``setReaction`` set. Shared by the channel and the chat reaction tools.
    """
    emoji = teams_reaction_emoji(reaction.strip() if isinstance(reaction, str) else "")
    return emoji if normalize_emoji(emoji) is not None else None


def teams_reaction_refusal(tool: str, reaction: object) -> str:
    """The error a reaction tool answers for a *reaction* that is no single emoji."""
    return (
        f"Error: {tool} needs one emoji (for example \U0001f44d) or a Teams reaction name "
        f"(like, heart, laugh, surprised, sad, angry); got {str(reaction)[:40]!r}."
    )


def _validate_teams_chat_id(
    chat_id: str,
    tool_name: str,
) -> str | None:
    """Validate ``chat_id`` before making a Graph API call."""
    cid = chat_id.strip() if isinstance(chat_id, str) else ""
    if _is_placeholder_id(cid, _INVALID_TEAMS_ID_PLACEHOLDERS | {"chat_id"}) or not _is_teams_path_id(cid):
        return (
            f"Error: chat_id is missing or invalid ({chat_id!r}). "
            f"Use list_microsoft_chats to find a valid chat ID before calling {tool_name}."
        )
    return None


async def _soft_delete_message(
    ctx: MicrosoftContext,
    client: Any,
    message_url: str,
    message_id: str,
    label: str,
) -> str:
    """Soft-delete the Teams message at *message_url* and read it back.

    Graph answers ``softDelete`` with an empty ``204``, so the message is read
    again for the ``deletedDateTime`` Teams stamped on it. A refusal (someone
    else's message, a missing permission) is Graph's error as returned.
    """
    _, err = await ctx.request(client, "post", f"{message_url}/softDelete", _PROVIDER, f"delete {label}")
    if err:
        return err
    resp, read_err = await ctx.request(client, "get", message_url, _PROVIDER, f"read the deleted {label}")
    msg: dict = {}
    if resp is not None and not read_err:
        with contextlib.suppress(ValueError):
            body = resp.json()
            if isinstance(body, dict):
                msg = body
    deleted_at = msg.get("deletedDateTime")
    if deleted_at:
        return f"{label.capitalize()} deleted. Message ID: {msg.get('id') or message_id}. Deleted at: {deleted_at}."
    requested_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = f" ({read_err})" if read_err else ""
    return (
        f"{label.capitalize()} deletion accepted by Microsoft. Message ID: {message_id}. "
        f"Requested at: {requested_at}. Reading the message back did not show it deleted yet{reason}."
    )


def _format_chat_participants(members_list: list) -> str:
    """Render a chat's ``members`` list as ``"A, B, C (+N more)"``.

    Shared by ``list_microsoft_chats`` and
    ``list_all_microsoft_chat_messages`` so participant rendering can never
    drift between the chat-list and the compound all-chats views.
    """
    member_names = [
        m.get("displayName", "")
        for m in members_list
        if isinstance(m, dict) and m.get("displayName")
    ]
    members_str = ", ".join(member_names[:5])
    if len(member_names) > 5:
        members_str += f" (+{len(member_names) - 5} more)"
    return members_str


def _chat_message_block(msg: dict, text: str, *, include_message_id: bool) -> str:
    """One Teams chatMessage of a chat listing: timestamp, sender, *text* and its suffixes."""
    author_type, sender_name = _teams_message_author(msg)
    if author_type != "user":
        sender_name = f"{sender_name} ({author_type})"
    line = (
        f"[{msg.get('createdDateTime', '')}] {sender_name}:\n"
        f"{text}"
        f"{_format_message_attachments(msg)}"
        f"{listing_note(msg, 'get_microsoft_chat_message', text)}"
        f"{_format_message_mentions(msg)}"
    )
    if include_message_id:
        line += f"\n(Message ID: {msg.get('id', '')})"
    return line


def _format_chat_listing(
    chats: Sequence[tuple[str, Sequence[dict]]],
    *,
    include_message_id: bool = False,
) -> list[list[str]]:
    """The display blocks of each chat's messages, under one text budget.

    *chats* pairs a chat ID with its messages in display order. System events
    (member joins, renames, ...) are left out. Shared by
    ``list_microsoft_chat_messages`` and ``list_all_microsoft_chat_messages``
    so a message reads the same in both, and its text is cut only when it
    does not fit (:func:`_clip_listing_texts`).
    """
    kept = [
        (chat_id, [msg for msg in messages if msg.get("messageType", "message") != "systemEventMessage"])
        for chat_id, messages in chats
    ]
    texts = iter(
        _clip_listing_texts(
            [
                (_teams_message_text(msg), _chat_message_call(chat_id, str(msg.get("id") or "")))
                for chat_id, messages in kept
                for msg in messages
            ]
        )
    )
    return [
        [_chat_message_block(msg, next(texts), include_message_id=include_message_id) for msg in messages]
        for _, messages in kept
    ]


#: The message text one listing shows at most, all its messages together.
#: Well under the agent loop's cut of one tool result (15 % of the context
#: window, about 76,000 characters on a 128k-token model), which drops a
#: result's tail without saying which messages it held.
_TEAMS_LISTING_TEXT_BUDGET = 40_000

#: A message cut to share the listing budget still shows at least this much.
_TEAMS_LISTING_MIN_CHARS = 300


def _listing_text_cap(lengths: Sequence[int]) -> int:
    """The longest text one message of a listing shows.

    ``MICROSOFT_TEAMS_MESSAGE_TEXT_LIMIT`` while the listing's texts fit its
    budget. Past it, the largest cap under which they do: every message
    shorter than the cap is shown whole and the longer ones share the rest
    evenly, never below ``_TEAMS_LISTING_MIN_CHARS``.
    """
    limit = settings.microsoft_teams_message_text_limit
    sizes = sorted(min(length, limit) for length in lengths)
    remaining = _TEAMS_LISTING_TEXT_BUDGET
    for index, size in enumerate(sizes):
        share = remaining // (len(sizes) - index)
        if size > share:
            return max(share, min(_TEAMS_LISTING_MIN_CHARS, limit))
        remaining -= size
    return limit


def _clip_message_text(text: str, cap: int, read_call: str) -> str:
    """*text* whole when it fits *cap*, else cut at a word boundary and marked.

    The marker says how much is left and names *read_call*, the exact call
    that reads the message in full (:func:`app.integrations.http.clip_text`).
    """
    return clip_text(text, cap, read_call)


def _clip_listing_texts(entries: Sequence[tuple[str, str]]) -> list[str]:
    """The texts of one listing's messages as it shows them, in order.

    *entries* are ``(text, read_call)`` pairs: the message text
    (:func:`_teams_message_text`) and the call that reads that message in
    full. A text that fits the listing's cap is returned verbatim.
    """
    cap = _listing_text_cap([len(text) for text, _ in entries])
    return [_clip_message_text(text, cap, read_call) for text, read_call in entries]


def _channel_message_call(team_id: str, channel_id: str, message_id: str, parent_message_id: str = "") -> str:
    """The ``get_microsoft_channel_message`` call that reads one channel post or reply."""
    reply = f", parent_message_id={parent_message_id}" if parent_message_id else ""
    return (
        f"get_microsoft_channel_message(team_id={team_id}, channel_id={channel_id}, "
        f"message_id={message_id}{reply})"
    )


def _chat_message_call(chat_id: str, message_id: str) -> str:
    """The ``get_microsoft_chat_message`` call that reads one chat message."""
    return f"get_microsoft_chat_message(chat_id={chat_id}, message_id={message_id})"


#: Hosts of the links Teams writes for a message (classic and new Teams).
_TEAMS_LINK_HOSTS = frozenset({"teams.microsoft.com", "teams.cloud.microsoft"})


@dataclasses.dataclass(frozen=True)
class TeamsMessageLink:
    """What a Teams message link names.

    ``https://teams.microsoft.com/l/message/{conversation}/{message}?groupId=...&parentMessageId=...``:
    the conversation is a channel when the link carries ``groupId`` (the
    team), else a chat. ``parent_message_id`` is set for a thread reply only:
    Teams writes a post's own ID there.
    """

    conversation_id: str
    message_id: str
    team_id: str = ""
    parent_message_id: str = ""


def _parse_teams_message_link(url: object) -> tuple[TeamsMessageLink | None, str | None]:
    """``(link, None)`` for a Teams message link, ``(None, error)`` for anything else."""
    raw = url.strip() if isinstance(url, str) else ""
    parts = urlsplit(raw)
    segments = [_url_decode(segment) for segment in parts.path.split("/") if segment]
    query = parse_qs(parts.query)
    team_id = (query.get("groupId") or [""])[0]
    parent = (query.get("parentMessageId") or [""])[0]
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() not in _TEAMS_LINK_HOSTS
        or len(segments) != 4
        or [segment.lower() for segment in segments[:2]] != ["l", "message"]
        # Each ID goes into a Graph URL path: one path segment once decoded,
        # so a double-encoded ``%252F`` (read as ``%2F``) is refused too.
        or not all(_is_teams_path_id(segment) for segment in segments[2:])
        or not all(_is_teams_path_id(value) for value in (team_id, parent) if value)
    ):
        return None, (
            f"Error: {raw[:200]!r} is not a Teams message link "
            "(https://teams.microsoft.com/l/message/CONVERSATION_ID/MESSAGE_ID?...)."
        )
    return (
        TeamsMessageLink(
            conversation_id=segments[2],
            message_id=segments[3],
            team_id=team_id,
            parent_message_id="" if parent == segments[3] else parent,
        ),
        None,
    )


def _link_conflicts(given: dict[str, str], linked: dict[str, str], tool: str) -> str | None:
    """An error when an ID passed next to a link names another message."""
    conflicts = [key for key, value in given.items() if value and value.lower() != linked[key].lower()]
    if not conflicts:
        return None
    return (
        f"Error: {tool} got a Teams link and {', '.join(conflicts)} naming another message: "
        "pass the link alone, or the IDs alone."
    )


def _channel_message_target(
    team_id: object,
    channel_id: object,
    message_id: object,
    parent_message_id: object,
    url: object,
    tool: str,
) -> tuple[dict[str, str], str | None]:
    """The IDs of the channel post or reply a call names, from its IDs or its Teams link.

    Returns ``team_id`` / ``channel_id`` / ``message_id`` /
    ``parent_message_id``, or an error. An ID passed next to a link must name
    the same message: the Teams channel Scope(s) checks the IDs this returns.
    """
    given = {
        key: value.strip() if isinstance(value, str) else ""
        for key, value in (
            ("team_id", team_id),
            ("channel_id", channel_id),
            ("message_id", message_id),
            ("parent_message_id", parent_message_id),
        )
    }
    if not (isinstance(url, str) and url.strip()):
        return given, None
    link, err = _parse_teams_message_link(url)
    if link is None:
        return given, err
    if not link.team_id:
        return given, (
            "Error: this Teams link names a chat message, not a channel message (it has no groupId): "
            "read it with get_microsoft_chat_message(url=...)."
        )
    linked = {
        "team_id": link.team_id,
        "channel_id": link.conversation_id,
        "message_id": link.message_id,
        "parent_message_id": link.parent_message_id,
    }
    return linked, _link_conflicts(given, linked, tool)


def _chat_message_target(
    chat_id: object, message_id: object, url: object, tool: str
) -> tuple[dict[str, str], str | None]:
    """The ``chat_id`` / ``message_id`` a chat read names, from its IDs or its Teams link."""
    given = {
        key: value.strip() if isinstance(value, str) else ""
        for key, value in (("chat_id", chat_id), ("message_id", message_id))
    }
    if not (isinstance(url, str) and url.strip()):
        return given, None
    link, err = _parse_teams_message_link(url)
    if link is None:
        return given, err
    if link.team_id:
        return given, (
            "Error: this Teams link names a channel message (its groupId is a team): "
            "read it with get_microsoft_channel_message(url=...)."
        )
    linked = {"chat_id": link.conversation_id, "message_id": link.message_id}
    return linked, _link_conflicts(given, linked, tool)


def _format_message_reactions(msg: dict) -> str:
    """Render a Teams chatMessage's reactions as a suffix: each emoji, its count and who reacted."""
    reactors: dict[str, list[str]] = {}
    for reaction in msg.get("reactions") or []:
        if not isinstance(reaction, dict):
            continue
        emoji = teams_reaction_emoji(reaction.get("reactionType")) or reaction.get("displayName") or "custom"
        who = _teams_identity_name(reaction.get("user"))
        reactors.setdefault(emoji, []).append(who[1] if who else "someone")
    parts = [f"{emoji} {len(names)} ({', '.join(names)})" for emoji, names in reactors.items()]
    return "\n[reactions: " + "; ".join(parts) + "]" if parts else ""


def _format_message_detail(msg: dict, heading: str, details: Sequence[str] = (), suffix: str = "") -> str:
    """One Teams message read on its own: who and when, then its whole text.

    The text is :func:`_teams_message_text` uncut, followed by the files,
    pasted images and mentions a listing shows, the reactions, then *suffix*.
    *details* are extra header lines (a post's reply count).
    """
    author_type, sender_name = _teams_message_author(msg)
    message_type = msg.get("messageType") or "message"
    kind = author_type if message_type == "message" else f"{author_type}, {message_type}"
    lines = [heading, f"From: {sender_name} ({kind})", f"Created: {msg.get('createdDateTime', '')}"]
    for key, label in (("lastEditedDateTime", "Last edited"), ("deletedDateTime", "Deleted")):
        if msg.get(key):
            lines.append(f"{label}: {msg[key]}")
    if msg.get("importance") not in (None, "", "normal"):
        lines.append(f"Importance: {msg['importance']}")
    if msg.get("webUrl"):
        lines.append(f"Link: {msg['webUrl']}")
    lines.extend(details)
    return (
        "\n".join(lines)
        + "\n\n"
        + _teams_message_text(msg)
        + _format_message_attachments(msg)
        + _format_message_mentions(msg)
        + _format_message_reactions(msg)
        + suffix
    )

#: ``chatMessage.messageType`` values that are an event of the conversation,
#: not something a person, app or device said.
_TEAMS_EVENT_MESSAGE_TYPES = frozenset({"systemEventMessage", "chatEvent"})

#: The identity sets ``chatMessage.from`` may hold, in the order they are read.
_TEAMS_IDENTITY_KINDS = ("user", "application", "device")

#: Card fields read as text, wherever a card nests them (Adaptive Card
#: TextBlock / RichTextBlock runs, hero / thumbnail cards, Office 365
#: connector cards).
_TEAMS_CARD_TEXT_KEYS = (
    "title",
    "subtitle",
    "activityTitle",
    "activitySubtitle",
    "summary",
    "text",
    "activityText",
)


def _teams_identity_name(identity_set: dict | None) -> tuple[str, str] | None:
    """``(kind, display name)`` of the first identity a Graph identitySet holds."""
    for kind in _TEAMS_IDENTITY_KINDS:
        identity = (identity_set or {}).get(kind)
        if isinstance(identity, dict) and (identity.get("displayName") or identity.get("id")):
            return kind, identity.get("displayName") or f"{kind} {identity.get('id')}"
    return None


def _teams_message_author(msg: dict) -> tuple[str, str]:
    """``(author type, display name)`` of a Teams chatMessage.

    ``from`` holds one identity: ``user`` for a person, ``application`` for a
    bot, a connector or a workflow (Power Automate / Workflows, an incoming
    webhook), ``device`` for a device. An event of the conversation has no
    ``from``: it is ``system``, named after whoever initiated it.
    """
    found = _teams_identity_name(msg.get("from"))
    if found is not None:
        return found
    if msg.get("messageType") in _TEAMS_EVENT_MESSAGE_TYPES or msg.get("eventDetail"):
        initiator = _teams_identity_name((msg.get("eventDetail") or {}).get("initiator"))
        return "system", initiator[1] if initiator else "Teams"
    return "unknown", "Unknown sender"


def _teams_card_lines(node: Any, lines: list[str]) -> None:
    """Append the text of an Adaptive / hero / connector card, in reading order."""
    if isinstance(node, list):
        for item in node:
            _teams_card_lines(item, lines)
        return
    if not isinstance(node, dict):
        return
    for key in _TEAMS_CARD_TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            text = _strip_html(value).strip()
            if text and (not lines or lines[-1] != text):
                lines.append(text)
    for fact in node.get("facts") or []:
        if isinstance(fact, dict):
            label = fact.get("title") or fact.get("name") or ""
            value = fact.get("value") or ""
            if label or value:
                lines.append(f"{label}: {value}".strip(": "))
    for key, value in node.items():
        if key in _TEAMS_CARD_TEXT_KEYS or key == "facts":
            continue
        if isinstance(value, (list, dict)):
            _teams_card_lines(value, lines)


def _teams_attachment_text(attachment: dict) -> str:
    """A text rendering of a card or quoted-message attachment, ``""`` for a file."""
    content_type = str(attachment.get("contentType") or "")
    if content_type == "reference":
        return ""
    content: Any = attachment.get("content")
    if isinstance(content, str) and content.strip()[:1] in ("{", "["):
        with contextlib.suppress(ValueError):
            content = json.loads(content)
    if content_type == "messageReference" and isinstance(content, dict):
        sender = _teams_identity_name(content.get("messageSender"))
        preview = _strip_html(content.get("messagePreview") or "")
        return f"[quoting {sender[1] if sender else 'a message'}: {preview}]"
    if isinstance(content, (dict, list)):
        lines: list[str] = []
        _teams_card_lines(content, lines)
        if lines:
            kind = content_type.rsplit(".", 1)[-1] or "card"
            return f"[{kind} card] " + "\n".join(lines)
    if isinstance(content, str) and content.strip():
        return _strip_html(content)
    name = attachment.get("name")
    return f"[attachment: {name}]" if name else ""


def _teams_event_text(msg: dict) -> str:
    """What a system event of a Teams conversation records."""
    detail = msg.get("eventDetail") or {}
    event = str(detail.get("@odata.type") or "").rsplit(".", 1)[-1]
    event = event.removesuffix("EventMessageDetail")
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", event).lower() or "conversation event"
    parts = [f"System event: {words}"]
    members = [
        name
        for member in detail.get("members") or []
        if isinstance(member, dict) and (name := member.get("displayName"))
    ]
    if members:
        parts.append(f"members: {', '.join(members)}")
    for key, label in (
        ("channelDisplayName", "channel"),
        ("teamDisplayName", "team"),
        ("chatDisplayName", "chat"),
        ("callDuration", "duration"),
    ):
        if isinstance(detail.get(key), str) and detail[key]:
            parts.append(f"{label}: {detail[key]}")
    return "; ".join(parts)


def _teams_body_text(body: str) -> str:
    """The readable text of a chatMessage body, its layout kept.

    One line per paragraph, list item (``- ``) and table row, a blank line
    between blocks, and every entity decoded (``&nbsp;`` a space, ``&amp;``
    ``&``) by the shared HTML reader (``attachment_extract``). Collapsing the
    body to one line ran a list into its sentence ("Read live: - a - b"),
    and an agent replaying that context wrote its own lists the same way.
    """
    if not body:
        return ""
    if "<" in body and ">" in body:
        from app.services.attachment_extract import extract_html_document

        return extract_html_document(body).text.strip()
    return _html_unescape(body).replace("\xa0", " ").strip()


def _teams_message_text(msg: dict) -> str:
    """The text of a Teams chatMessage, whatever carries it.

    The HTML or text body, then each card a bot, a connector or a workflow
    posted (``attachments``: Adaptive Cards, hero cards, connector cards) and
    each quoted message - a card-only message has an empty body holding just
    ``<attachment id=...>`` placeholders. A system event is described from its
    ``eventDetail``, a deleted message says so.
    """
    if msg.get("deletedDateTime"):
        return "[message deleted]"
    text = _teams_body_text((msg.get("body") or {}).get("content") or "")
    if msg.get("messageType") in _TEAMS_EVENT_MESSAGE_TYPES or msg.get("eventDetail"):
        event = _teams_event_text(msg)
        return f"{event}\n{text}" if text else event
    parts = [part for part in (msg.get("subject"), text) if isinstance(part, str) and part.strip()]
    for attachment in msg.get("attachments") or []:
        if isinstance(attachment, dict) and (rendered := _teams_attachment_text(attachment)):
            parts.append(rendered)
    return "\n".join(parts) if parts else "[no text content]"


def _format_message_attachments(msg: dict) -> str:
    """Render a Teams chatMessage's file attachments as a compact suffix.

    Graph delivers files shared in a chat/channel message as ``attachments``
    entries with ``contentType: "reference"`` and a SharePoint/OneDrive
    ``contentUrl``.  Surfacing them lets the agent see that a file exists
    and read it via ``get_microsoft_file_by_url`` — previously attachments
    were silently dropped, so files shared in Teams were invisible.

    An image pasted into the message is not an attachment but a hosted
    content of the message, referenced from its HTML body: each is
    listed with the ids its download tool takes.

    Returns ``""`` when the message carries no file and no pasted image.
    """
    parts: list[str] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        content_url = att.get("contentUrl")
        if att.get("contentType") == "reference" and content_url:
            name = att.get("name") or "file"
            parts.append(f"{name} <{content_url}>")
    audio = [
        f"{att.get('name') or 'voice clip'} ({str(att.get('contentType'))[:40]})"
        for att in msg.get("attachments") or []
        if isinstance(att, dict) and str(att.get("contentType") or "").lower().startswith("audio/")
    ]
    suffix = ""
    if parts:
        suffix = (
            "\n[attached files — read with get_microsoft_file_by_url, save as a "
            "file with download_microsoft_file: "
            + ", ".join(parts)
            + "]"
        )
    if audio:
        suffix += "\n[audio attachments (voice clips): " + ", ".join(audio) + "]"
    images = _hosted_content_refs(msg)
    if images:
        suffix += (
            "\n[pasted images — save one as a file (and see it) with "
            + "; ".join(_hosted_content_call(ref) for ref in images)
            + "]"
        )
    return suffix


def _looks_empty(msg: dict) -> bool:
    """Whether a chat message shows no text, no file and no pasted image (a voice clip may hide there)."""
    return (
        not msg.get("deletedDateTime")
        and _teams_message_text(msg) == "[no text content]"
        and not _format_message_attachments(msg)
    )


def _format_unreferenced_hosted_contents(
    msg: dict, hosted: Sequence[dict], chat_id: str, message_id: str
) -> str:
    """The hosted contents of a chat message its body does not reference, as a suffix.

    A Teams voice clip is a hosted content the body has no ``<img>`` for, so
    neither the text nor the pasted images show it: each is listed with its
    media type and the call that saves it as a file. ``""`` when none.
    """
    referenced = {ref["hosted_content_id"] for ref in _hosted_content_refs(msg)}
    parts: list[str] = []
    for item in hosted:
        hosted_id = str(item.get("id") or "") if isinstance(item, dict) else ""
        if not hosted_id or hosted_id in referenced:
            continue
        kind = str(item.get("contentType") or "media type unknown")[:40]
        call = _hosted_content_call(
            {"chat_id": chat_id, "chat_message_id": message_id, "hosted_content_id": hosted_id}
        )
        parts.append(f"{kind}: {call}")
    if not parts:
        return ""
    return (
        "\n[hosted contents the text does not show (a voice clip is one) - save one as a file with "
        + "; ".join(parts)
        + "]"
    )


#: ``chatMessage.mentions[].mentioned`` identities, in the order they are read.
_MENTIONED_KINDS = (("user", "user"), ("tag", "tag"), ("application", "app"), ("device", "device"))


def _format_message_mentions(msg: dict) -> str:
    """Render a Teams chatMessage's mentions as a suffix.

    Each entry names what was mentioned and its id: ``channel`` / ``team`` /
    ``chat`` (Graph's ``conversation``, by its ``conversationIdentityType``;
    ``chat`` is Everyone in a chat), ``user``,
    ``tag``, ``app`` or ``device``, so an agent can read back whether a
    mention it sent was created. ``""`` when the message mentions nobody.
    """
    parts: list[str] = []
    for entry in msg.get("mentions") or []:
        if not isinstance(entry, dict):
            continue
        mentioned = entry.get("mentioned") if isinstance(entry.get("mentioned"), dict) else {}
        kind, target = "unknown", {}
        conversation = mentioned.get("conversation")
        if isinstance(conversation, dict):
            kind, target = conversation.get("conversationIdentityType") or "conversation", conversation
        else:
            for key, label in _MENTIONED_KINDS:
                if isinstance(mentioned.get(key), dict):
                    kind, target = label, mentioned[key]
                    break
        name = target.get("displayName") or entry.get("mentionText") or "?"
        target_id = target.get("id")
        parts.append(f'{kind} "{name}" ({target_id})' if target_id else f'{kind} "{name}"')
    return "\n[mentions: " + "; ".join(parts) + "]" if parts else ""


# An image pasted into a Teams message: ``<img src=".../hostedContents/{id}/$value">``
# in the message body, under the chat or channel message that holds it.
_HOSTED_CONTENT_RE = re.compile(
    r"https://graph\.microsoft\.com/(?:v1\.0|beta)/"
    r"(?:chats/(?P<chat_id>[^/\"'\s<>]+)/messages/(?P<chat_message_id>[^/\"'\s<>]+)"
    r"|teams/(?P<team_id>[^/\"'\s<>]+)/channels/(?P<channel_id>[^/\"'\s<>]+)"
    r"/messages/(?P<channel_message_id>[^/\"'\s<>]+)(?:/replies/(?P<reply_id>[^/\"'\s<>]+))?)"
    r"/hostedContents/(?P<hosted_content_id>[^/\"'\s<>]+)/\$value"
)


def _hosted_content_refs(msg: dict) -> list[dict[str, str]]:
    """The hosted contents (pasted images) a Teams message body references."""
    body = (msg.get("body") or {}).get("content") or ""
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _HOSTED_CONTENT_RE.finditer(_html_unescape(body)):
        ref = {key: _url_decode(value) for key, value in match.groupdict().items() if value}
        if ref["hosted_content_id"] in seen:
            continue
        seen.add(ref["hosted_content_id"])
        refs.append(ref)
    return refs


def _hosted_content_call(ref: dict[str, str]) -> str:
    """The download call that keeps one pasted image as a conversation file."""
    if "chat_id" in ref:
        return (
            f"download_microsoft_chat_message_image(chat_id={ref['chat_id']}, "
            f"message_id={ref['chat_message_id']}, hosted_content_id={ref['hosted_content_id']})"
        )
    if "reply_id" in ref:
        message = (
            f"message_id={ref['reply_id']}, parent_message_id={ref['channel_message_id']}"
        )
    else:
        message = f"message_id={ref['channel_message_id']}"
    return (
        f"download_microsoft_channel_message_image(team_id={ref['team_id']}, "
        f"channel_id={ref['channel_id']}, {message}, "
        f"hosted_content_id={ref['hosted_content_id']})"
    )
