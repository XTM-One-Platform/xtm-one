"""Teams mentions: the ``mentions`` argument of the message tools.

Teams highlights and notifies a mention only when the message carries both an
``<at id="N">name</at>`` tag in its HTML body and the matching entry of its
Graph ``mentions`` array. The message tools take ``mentions`` as data (a type,
an id, a display name), read the names left out from Graph, and hand the names
to :func:`app.services.teams_format.build_graph_chat_body`, which writes the
``<at>`` tags once the body is converted and sanitized.

A channel or team mention names the channel or the team the message is posted
in, never another: the post itself proves the account belongs to it, and a
Teams channel Scope(s) (``teams_scope``) bounds what a run may notify. A chat
mention ("Everyone" in Teams) names the group or meeting chat the message is
posted in the same way; a 1:1 chat has no such mention and is refused.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from app.core.utils import is_valid_uuid
from app.integrations.http import AutoRefreshClient
from app.services.teams_format import MentionPlacementError, build_graph_chat_body

from ._cards import attach_card
from ._common import _BASE_URL, _PROVIDER, MicrosoftContext
from ._teams import _channel_url, _team_url

#: Mention types of a channel message, and of a chat message.
CHANNEL_MENTION_TYPES = ("user", "channel", "team", "tag")
CHAT_MENTION_TYPES = ("user", "chat")

#: The name Teams shows for a mention of the whole chat.
CHAT_MENTION_NAME = "Everyone"

_PLACEMENT = (
    "Each mention opens the message on a line of its own, unless the message "
    'places it: write <at id="N"></at> where mention N (its 0-based position '
    "in this list) goes."
)

_SHAPE = 'mentions must be a list of objects such as [{"type": "user", "id": "jane@contoso.com"}].'


def mentions_schema(types: tuple[str, ...]) -> dict[str, Any]:
    """The ``mentions`` property of a message tool's input schema."""
    if types == CHAT_MENTION_TYPES:
        description = (
            "People, or the whole chat, to @mention: each is highlighted and "
            "notified. 'user' takes the person's Microsoft Entra object ID or "
            "email, display_name defaulting to their name; 'chat' mentions "
            "every member of the group or meeting chat posted in (no id; "
            "display_name defaults to Everyone; refused in a 1:1 chat). " + _PLACEMENT
        )
        id_description = "user: Microsoft Entra object ID or email; chat: omit (the chat posted in)"
    else:
        description = (
            "People, this channel, this team or a team tag to @mention: each is "
            "highlighted and notified. 'channel' and 'team' mention the channel "
            "and the team the message is posted in (no id needed); 'user' takes "
            "the person's Microsoft Entra object ID or email; 'tag' takes the "
            "tag ID or its display_name. display_name defaults to the name in "
            "Teams. A team's settings can turn off channel and team mentions. " + _PLACEMENT
        )
        id_description = (
            "user: Microsoft Entra object ID or email; tag: tag ID; channel / team: omit (the channel / team posted in)"
        )
    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(types)},
                "id": {"type": "string", "description": id_description},
                "display_name": {
                    "type": "string",
                    "description": "Name shown in the message (default: the real name)",
                },
            },
            "required": ["type"],
        },
    }


@dataclasses.dataclass(frozen=True)
class MentionRequest:
    """One entry of a tool's ``mentions`` argument, as the agent wrote it."""

    kind: str
    id: str = ""
    display_name: str = ""


@dataclasses.dataclass(frozen=True)
class Mention:
    """A mention whose Graph id and name are known."""

    kind: str
    id: str
    display_name: str

    def label(self) -> str:
        return f'{self.kind} "{self.display_name}"'

    def graph_entry(self, index: int) -> dict[str, Any]:
        """The Graph ``chatMessage.mentions`` entry of ``<at id="index">``."""
        if self.kind == "user":
            mentioned: dict[str, Any] = {
                "user": {
                    "id": self.id,
                    "displayName": self.display_name,
                    "userIdentityType": "aadUser",
                }
            }
        elif self.kind == "tag":
            mentioned = {
                "tag": {
                    "@odata.type": "#microsoft.graph.teamworkTagIdentity",
                    "id": self.id,
                    "displayName": self.display_name,
                }
            }
        else:
            mentioned = {
                "conversation": {
                    "id": self.id,
                    "displayName": self.display_name,
                    "conversationIdentityType": self.kind,
                }
            }
        return {"id": index, "mentionText": self.display_name, "mentioned": mentioned}


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def parse_mentions(raw: Any, types: tuple[str, ...]) -> tuple[list[MentionRequest], str | None]:
    """Read a tool's ``mentions`` argument, before any request is made."""
    if raw is None or raw == "" or raw == []:
        return [], None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [], f"Error: {_SHAPE}"
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        return [], f"Error: {_SHAPE}"
    requests: list[MentionRequest] = []
    for position, item in enumerate(raw):
        if not isinstance(item, Mapping):
            return [], f"Error: mention {position} is not an object; {_SHAPE}"
        kind = _text(item.get("type")).lower()
        if kind not in types:
            return [], (
                f"Error: mention {position} has type {item.get('type')!r}; this tool mentions: {', '.join(types)}."
            )
        mention_id = _text(item.get("id"))
        display_name = _text(item.get("display_name") or item.get("displayName"))
        if kind == "user" and not mention_id:
            return [], (
                f"Error: mention {position} (user) needs an id: the person's Microsoft Entra object ID or email."
            )
        if kind == "tag" and not (mention_id or display_name):
            return [], f"Error: mention {position} (tag) needs the tag's id or its display_name."
        requests.append(MentionRequest(kind, mention_id, display_name))
    return requests, None


class _Resolver:
    """Reads, once per call, the Graph names and ids the mentions leave out."""

    def __init__(
        self, ctx: MicrosoftContext, client: AutoRefreshClient, team_id: str, channel_id: str, chat_id: str = ""
    ):
        self.ctx = ctx
        self.client = client
        self.team_id = team_id
        self.channel_id = channel_id
        self.chat_id = chat_id
        self.chat_type: str | None = None
        self.names: dict[str, str] = {}
        self.tags: list[dict] | None = None

    async def _get(self, url: str, action: str, params: dict | None = None) -> tuple[dict, str | None]:
        resp, err = await self.ctx.request(self.client, "get", url, _PROVIDER, action, params=params)
        if err:
            return {}, err
        data = resp.json()
        return data if isinstance(data, dict) else {}, None

    async def _conversation_name(self, kind: str) -> tuple[str, str | None]:
        if kind in self.names:
            return self.names[kind], None
        url = _channel_url(self.team_id, self.channel_id) if kind == "channel" else _team_url(self.team_id)
        data, err = await self._get(url, f"read the {kind} name", {"$select": "id,displayName"})
        if err:
            return "", err
        name = _text(data.get("displayName"))
        if not name:
            return "", f"Error: Teams returned no name for the {kind}."
        self.names[kind] = name
        return name, None

    async def _resolve_chat(self, position: int, request: MentionRequest) -> tuple[Mention | None, str | None]:
        own = self.chat_id
        if request.id and request.id.lower() != own.lower():
            return None, (
                f"Error: mention {position} names another chat ({request.id}). A chat "
                f"mention can only name the chat the message is posted in ({own}); omit its id."
            )
        if self.chat_type is None:
            data, err = await self._get(
                f"{_BASE_URL}/chats/{quote(own, safe=':@')}", "read the chat type", {"$select": "id,chatType"}
            )
            if err:
                return None, f"{err} (mention {position}: the chat type decides whether Everyone can be mentioned)"
            self.chat_type = _text(data.get("chatType"))
        if self.chat_type.lower() == "oneonone":
            return None, (
                f"Error: mention {position} (chat) mentions everyone in a 1:1 chat, which "
                "Teams does not offer. Mention the person with type user instead. Nothing was posted."
            )
        return Mention("chat", own, request.display_name or CHAT_MENTION_NAME), None

    async def resolve(self, position: int, request: MentionRequest) -> tuple[Mention | None, str | None]:
        kind = request.kind
        if kind == "chat":
            return await self._resolve_chat(position, request)
        if kind in ("channel", "team"):
            own = self.channel_id if kind == "channel" else self.team_id
            if request.id and request.id.lower() != own.lower():
                return None, (
                    f"Error: mention {position} names another {kind} ({request.id}). A "
                    f"{kind} mention can only name the {kind} the message is posted in "
                    f"({own}); omit its id."
                )
            name = request.display_name
            if not name:
                name, err = await self._conversation_name(kind)
                if err:
                    return None, f"{err} (mention {position}; pass display_name to skip this lookup)"
            return Mention(kind, own, name), None
        if kind == "user":
            if request.display_name and is_valid_uuid(request.id):
                return Mention("user", request.id, request.display_name), None
            data, err = await self._get(
                f"{_BASE_URL}/users/{quote(request.id, safe='@')}",
                "read the mentioned person",
                {"$select": "id,displayName"},
            )
            if err:
                return None, (
                    f"{err} (mention {position}, user {request.id}: pass the person's "
                    "Microsoft Entra object ID with display_name to skip this lookup)"
                )
            user_id = _text(data.get("id"))
            name = request.display_name or _text(data.get("displayName"))
            if not user_id or not name:
                return None, f"Error: Microsoft Entra returned no id or name for {request.id} (mention {position})."
            return Mention("user", user_id, name), None
        return await self._resolve_tag(position, request)

    async def _resolve_tag(self, position: int, request: MentionRequest) -> tuple[Mention | None, str | None]:
        hint = (
            f"(mention {position}: reading a team's tags needs the TeamworkTag.Read "
            "permission; pass the tag's id and display_name to skip this lookup)"
        )
        if request.id and request.display_name:
            return Mention("tag", request.id, request.display_name), None
        if request.id:
            data, err = await self._get(
                f"{_team_url(self.team_id)}/tags/{quote(request.id, safe='')}",
                "read the mentioned tag",
                {"$select": "id,displayName"},
            )
            if err:
                return None, f"{err} {hint}"
            name = _text(data.get("displayName"))
            if not name:
                return None, f"Error: Teams returned no name for tag {request.id} (mention {position})."
            return Mention("tag", request.id, name), None
        if self.tags is None:
            tags, err = await self.ctx.collect_all(
                self.client, f"{_team_url(self.team_id)}/tags", "list the team's tags"
            )
            if err:
                return None, f"{err} {hint}"
            self.tags = tags
        wanted = request.display_name.casefold()
        matches = [tag for tag in self.tags if _text(tag.get("displayName")).casefold() == wanted]
        if len(matches) != 1:
            names = ", ".join(sorted(_text(tag.get("displayName")) for tag in self.tags)[:30]) or "none"
            found = "no" if not matches else "more than one"
            return None, (
                f"Error: the team has {found} tag named {request.display_name!r} "
                f"(mention {position}). Its tags: {names}."
            )
        return Mention("tag", _text(matches[0].get("id")), _text(matches[0].get("displayName"))), None


async def build_message(
    ctx: MicrosoftContext,
    client: AutoRefreshClient,
    message: str,
    content_type: str,
    requests: list[MentionRequest],
    *,
    team_id: str = "",
    channel_id: str = "",
    chat_id: str = "",
    card: dict | None = None,
) -> tuple[dict | None, list[Mention], str | None]:
    """The Graph chatMessage payload of *message* with its mentions and *card*.

    Without mentions or card the payload is the body alone, exactly as before.
    A card (from :func:`._cards.parse_card`) is attached below the body.
    Returns ``(payload, mentions, error)``; on an error nothing is to be sent.
    """
    if not requests:
        payload: dict[str, Any] = {"body": build_graph_chat_body(message, content_type)}
        if card is not None and (err := attach_card(payload, card)):
            return None, [], err
        return payload, [], None
    resolver = _Resolver(ctx, client, team_id, channel_id, chat_id)
    mentions: list[Mention] = []
    for position, request in enumerate(requests):
        mention, err = await resolver.resolve(position, request)
        if err:
            return None, [], err
        mentions.append(mention)
    try:
        body = build_graph_chat_body(
            message, content_type, mention_texts=[mention.display_name for mention in mentions]
        )
    except MentionPlacementError as exc:
        return None, [], f"Error: {exc}. Nothing was posted."
    payload = {"body": body, "mentions": [mention.graph_entry(i) for i, mention in enumerate(mentions)]}
    if card is not None and (err := attach_card(payload, card)):
        return None, [], err
    return payload, mentions, None


def _labels(mentions: list[Mention]) -> str:
    return ", ".join(mention.label() for mention in mentions)


def refused_note(mentions: list[Mention]) -> str:
    """What a refused post adds when it carried channel or team mentions."""
    if any(mention.kind in ("channel", "team") for mention in mentions):
        return " A team's settings can turn off channel and team mentions."
    return ""


def sent_note(mentions: list[Mention]) -> str:
    """The mentions a call sent, when Graph answers with no message to read."""
    return f" Mentions sent: {_labels(mentions)}." if mentions else ""


def posted_note(mentions: list[Mention], msg: dict, read_back_tool: str) -> str:
    """The mentions of a new message, as the message Graph returned holds them."""
    if not mentions:
        return ""
    kept_entries = msg.get("mentions") if isinstance(msg, dict) else None
    if not isinstance(kept_entries, list):
        return (
            f" Mentions sent: {_labels(mentions)}; Teams did not return them, "
            f"read the message back with {read_back_tool} to check."
        )
    kept_ids = {str(entry.get("id")) for entry in kept_entries if isinstance(entry, dict)}
    kept = [mention for index, mention in enumerate(mentions) if str(index) in kept_ids]
    lost = [mention for index, mention in enumerate(mentions) if str(index) not in kept_ids]
    note = f" Mentions created: {_labels(kept)}." if kept else ""
    if lost:
        note += (
            f" Warning: Teams did not keep {len(lost)} of {len(mentions)} mention(s) "
            f"({_labels(lost)}): they show as plain text and notify nobody."
        )
    return note
