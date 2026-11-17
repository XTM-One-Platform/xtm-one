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

"""Microsoft Teams as an agent-account provider, through the account's delegated Graph token.

Listening: one Graph subscription to every chat of the account
(``/users/{id}/chats/getAllMessages``, delegated ``Chat.Read``) and one per
channel the rules reach (``/teams/{team}/channels/{channel}/messages``,
delegated ``ChannelMessage.Read.All``, thread replies included), filtered on
the account's mentions where mentions alone decide. Notifications carry no
resource data: the message is read back with the account's token, so a
notification only says where to look. Without push (no public HTTPS address),
chats are polled through ``/me/chats`` ordered by their last message.

Answering: ``POST /chats/{id}/messages`` (``ChatMessage.Send``) and
``POST /teams/{team}/channels/{channel}/messages/{root}/replies``
(``ChannelMessage.Send``), the body normalized to Teams HTML.

Reactions: a reaction changes the message it is on, so the
subscriptions that can carry it ask for ``created,updated``. Graph grants one
subscription per application and chat or channel: a channel listened to for
mentions only keeps its mention filter, which the agent's own answers never
match, and the answers posted there are checked periodically instead
(``reactions_pushed``). A reaction is read back from the message's
``reactions``, its legacy names (``like``, ``heart``...) turned into Unicode.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote as _url_encode

from app.core.egress import egress_route
from app.integrations.file_delivery import FetchError, fetch_file
from app.integrations.providers.microsoft._drive import _encode_drive_filename, _encode_share_id
from app.integrations.providers.microsoft._teams import (
    _hosted_content_refs,
    _teams_message_author,
    _teams_message_text,
    teams_reaction_emoji,
)
from app.integrations.providers.microsoft.files import _chat_share_recipients, _ungranted_recipients
from app.services.agent_accounts.events import (
    AccountAttachment,
    AccountEvent,
    ContextMessage,
    ConversationOption,
)
from app.services.agent_accounts.providers import (
    DesiredSubscription,
    InboundNotification,
    ProviderCapabilities,
    ProviderError,
    ProviderSession,
    ReplyFile,
    UnheardConversation,
    UpdatedMessage,
    with_file_lines,
)
from app.services.agent_accounts.providers.microsoft_graph import (
    GRAPH,
    MicrosoftGraphProvider,
    _graph_error,
    _parse_time,
    _segment,
    _TTLCache,
)
from app.services.agent_accounts.rules import ListeningPlan
from app.services.agent_accounts.teams_mentions import may_mention, members_of, resolve_reply_mentions
from app.services.reaction_feedback import PlatformReaction
from app.services.teams_format import MentionPlacementError, build_graph_chat_body
from app.services.teams_late_reply import RECENT_MESSAGES, message_to_quote, with_quote

logger = logging.getLogger(__name__)

#: Graph keeps a chatMessage subscription at most 4320 minutes; this leaves a margin.
SUBSCRIPTION_MINUTES = 4230
#: Channels listened to when a rule answers in channels without naming them.
MAX_DISCOVERED_CHANNELS = 50
#: Chats a poll reads messages of, newest activity first.
POLL_CHATS = 50
POLL_MESSAGES_PER_CHAT = 20
PICKER_LIMIT = 200
#: Members of a chat or channel a reply's ``@`` tokens are matched against at most.
MAX_MENTION_MEMBERS = 500
#: Replies of a followed thread read at most per read.
FOLLOWED_REPLIES_READ = 100
#: Files and pasted images of one message handed to the agent at most.
MAX_MESSAGE_ATTACHMENTS = 10
#: The text of a message whose content the account cannot read (no text, no file
#: and no hosted content: a voice clip Graph does not serve), stated to the agent.
UNREADABLE_MESSAGE_TEXT = (
    "[a message with no readable text or file: it may be a voice clip or an attachment "
    "this account cannot read; say so and ask for it as text]"
)

_CHAT_RESOURCE = re.compile(r"chats\('([^']+)'\)/messages\('([^']+)'\)", re.IGNORECASE)
_CHANNEL_RESOURCE = re.compile(
    r"teams\('([^']+)'\)/channels\('([^']+)'\)/messages\('([^']+)'\)(?:/replies\('([^']+)'\))?",
    re.IGNORECASE,
)
_CHAT_TYPES = {"oneOnOne": "direct", "group": "group", "meeting": "group"}
#: What a subscription asks for: the account's new messages, and with
#: reactions the changes to existing ones.
CHANGE_CREATED = "created"
CHANGE_WITH_REACTIONS = "created,updated"
#: The delegated permissions the subscriptions need, as an Entra ID administrator grants them.
CHAT_PERMISSION = "Chat.Read"
CHANNEL_PERMISSION = "ChannelMessage.Read.All"


_chats = _TTLCache()
_channels = _TTLCache()
_emails = _TTLCache()


def reference_attachment(item: dict) -> dict[str, str] | None:
    """The ``reference`` attachment of a Teams file card for a driveItem.

    The Graph contract ``send_microsoft_chat_file`` follows: the id is the
    GUID inside the item's eTag, the content URL its webDavUrl (else webUrl).
    ``None`` when the item has neither, so no card can render.
    """
    guid = re.search(r"\{([0-9a-fA-F-]{36})\}", str(item.get("eTag") or ""))
    content_url = str(item.get("webDavUrl") or item.get("webUrl") or "")
    if not guid or not content_url:
        return None
    return {
        "id": guid.group(1),
        "contentType": "reference",
        "contentUrl": content_url,
        "name": str(item.get("name") or "file"),
    }


def _author_id(msg: dict) -> str:
    user = (msg.get("from") or {}).get("user") or {}
    return str(user.get("id") or "")


def _mentions(msg: dict, user_id: str) -> bool:
    for mention in msg.get("mentions") or []:
        mentioned = (mention or {}).get("mentioned") or {}
        if str(((mentioned.get("user") or {}).get("id")) or "") == user_id:
            return True
    return False


def _message_url(msg: dict, route: dict[str, Any]) -> str | None:
    """The Graph URL of *msg* where *route* says it was posted; ``None`` when an id is unsafe."""
    message_id = str(msg.get("id") or "")
    try:
        if route.get("kind") == "chat":
            return (
                f"{GRAPH}/chats/{_segment(str(route.get('chat_id') or ''), 'chat id')}"
                f"/messages/{_segment(message_id, 'message id')}"
            )
        root_id = str(route.get("root_id") or "")
        base = (
            f"{GRAPH}/teams/{_segment(str(route.get('team_id') or ''), 'team id')}"
            f"/channels/{_segment(str(route.get('channel_id') or ''), 'channel id')}"
            f"/messages/{_segment(root_id, 'message id')}"
        )
        return base if message_id == root_id else f"{base}/replies/{_segment(message_id, 'message id')}"
    except ProviderError:
        return None


def _message_attachments(msg: dict, route: dict[str, Any]) -> tuple[AccountAttachment, ...]:
    """The files shared in *msg* and the images pasted into it, as the account downloads them.

    A shared file is a ``reference`` attachment whose ``contentUrl`` is its
    SharePoint / OneDrive address, read through Graph's ``/shares`` endpoint
    (``Files.Read.All``); a pasted image is a hosted content of the message
    its HTML body references (``Chat.Read`` / ``ChannelMessage.Read.All``),
    taken only when the reference names this very message. Cards and quoted
    messages are text, already in the event's ``text``. A voice clip or an
    audio file is taken whether it comes as an ``audio/*`` attachment on a
    Graph address or as a hosted content: the pipeline transcribes it
    (``voice_notes``). Any other attachment kind is logged by its content
    type only, so an unread kind is visible without its content.
    """
    found: list[AccountAttachment] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("contentType") or "")
        if kind.lower().startswith("audio/"):
            audio_url = str(att.get("contentUrl") or "")
            if audio_url.startswith(f"{GRAPH}/"):
                found.append(
                    AccountAttachment(
                        name=str(att.get("name") or "").strip() or "teams-voice-note",
                        url=audio_url,
                        content_type=kind.split(";", 1)[0].strip().lower(),
                    )
                )
            else:
                logger.info("Teams message %s: an audio attachment has no Graph address", msg.get("id"))
            continue
        if kind != "reference":
            if kind and not kind.startswith("application/vnd.microsoft.card") and kind != "messageReference":
                logger.info("Teams message %s: attachment of type %s not read", msg.get("id"), kind[:80])
            continue
        content_url = str(att.get("contentUrl") or "")
        if not content_url.lower().startswith("https://"):
            continue
        name = str(att.get("name") or "").strip() or "file"
        found.append(
            AccountAttachment(
                name=name,
                url=f"{GRAPH}/shares/{_encode_share_id(content_url)}/driveItem/content",
                content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            )
        )
    message_url = _message_url(msg, route)
    message_id = str(msg.get("id") or "")
    for ref in _hosted_content_refs(msg) if message_url else []:
        own = ref.get("chat_message_id") or ref.get("reply_id") or ref.get("channel_message_id")
        if own != message_id:
            continue
        hosted = ref["hosted_content_id"]
        found.append(
            AccountAttachment(
                # The extension comes from the media type the download declares.
                name=f"teams-image-{re.sub(r'[^A-Za-z0-9]', '', hosted)[-8:] or 'pasted'}",
                url=f"{message_url}/hostedContents/{_url_encode(hosted, safe='')}/$value",
            )
        )
    return tuple(found[:MAX_MESSAGE_ATTACHMENTS])


#: The HTML tags of a message body, by name only (``<attachment>``, ``<audio>``...).
_BODY_TAG = re.compile(r"<\s*([A-Za-z][A-Za-z0-9:-]*)")


def message_shape(msg: dict) -> str:
    """What an empty-looking message is made of, without its content: for the log.

    The message type, the tag names of its HTML body and the content type of
    each attachment, so an unread kind (a voice clip Graph serves in a shape
    this provider does not read) is visible in the log, never what it says.
    """
    body = (msg.get("body") or {}).get("content") or ""
    tags = sorted({tag.lower() for tag in _BODY_TAG.findall(str(body))})
    kinds = [
        str(att.get("contentType") or "?")[:60] for att in msg.get("attachments") or [] if isinstance(att, dict)
    ]
    return (
        f"messageType={msg.get('messageType') or 'message'} body_tags={','.join(tags) or '-'} "
        f"attachments={','.join(kinds) or '-'}"
    )


def unreferenced_hosted_contents(
    msg: dict, route: dict[str, Any], hosted: list[dict], found: tuple[AccountAttachment, ...]
) -> tuple[AccountAttachment, ...]:
    """The hosted contents of *msg* its body does not reference, as attachments.

    A Teams voice clip is kept in Teams' own storage as a hosted content of
    the message, and its body carries no ``<img src=".../hostedContents/...">``
    for it: ``_message_attachments`` cannot see it. *hosted* is the message's
    ``hostedContents`` collection; each item not already in *found* becomes an
    attachment whose download declares its media type, so the pipeline tells a
    voice note (``voice_notes.is_audio``) from any other content.
    """
    message_url = _message_url(msg, route)
    if not message_url:
        return ()
    known = {att.url for att in found}
    extra: list[AccountAttachment] = []
    for item in hosted:
        hosted_id = str(item.get("id") or "")
        if not hosted_id:
            continue
        url = f"{message_url}/hostedContents/{_url_encode(hosted_id, safe='')}/$value"
        if url in known:
            continue
        declared = str(item.get("contentType") or "").split(";", 1)[0].strip().lower()
        tail = re.sub(r"[^A-Za-z0-9]", "", hosted_id)[-8:] or "content"
        audio = declared.startswith("audio/")
        extra.append(
            AccountAttachment(
                name=f"teams-voice-note-{tail}" if audio else f"teams-content-{tail}",
                url=url,
                content_type=declared or "application/octet-stream",
            )
        )
    return tuple(extra)


class MicrosoftTeamsProvider(MicrosoftGraphProvider):
    key = "microsoft_teams"
    label = "Microsoft Teams"
    platform = "teams"
    capabilities = ProviderCapabilities(
        direct=True, group=True, channel=True, push=True, poll=True, reactions=True, threads=True,
        verified_sender=True,
    )
    subscription_lifetime_minutes = SUBSCRIPTION_MINUTES
    reply_mention_hint = (
        "To mention a member of this conversation, write @ and their full name as Teams shows it "
        "(@Jane Doe), their email (@jane.doe@example.com) or, when no one else shares it, their "
        "first name (@Jane): it becomes a real mention. A name that matches nobody, or several "
        "people, stays plain text."
    )

    # -- names --------------------------------------------------------------

    async def _chat_info(self, session: ProviderSession, chat_id: str) -> dict[str, str]:
        cached = _chats.get(chat_id)
        if cached is not None:
            return cached
        chat = await self._call(
            session,
            "GET",
            f"{GRAPH}/chats/{_segment(chat_id, 'chat id')}",
            params={"$select": "id,chatType,topic"},
            allow_missing=True,
        ) or {}
        info = {"type": _CHAT_TYPES.get(str(chat.get("chatType") or ""), "group"), "topic": chat.get("topic") or ""}
        _chats.put(chat_id, info)
        return info

    async def _channel_name(self, session: ProviderSession, team_id: str, channel_id: str) -> str:
        key = f"{team_id}:{channel_id}"
        cached = _channels.get(key)
        if cached is not None:
            return cached
        team = await self._call(
            session, "GET", f"{GRAPH}/teams/{_segment(team_id, 'team id')}",
            params={"$select": "displayName"}, allow_missing=True,
        ) or {}
        channel = await self._call(
            session, "GET",
            f"{GRAPH}/teams/{_segment(team_id, 'team id')}/channels/{_segment(channel_id, 'channel id')}",
            params={"$select": "displayName"}, allow_missing=True,
        ) or {}
        name = " > ".join(p for p in (team.get("displayName"), channel.get("displayName")) if p) or channel_id
        _channels.put(key, name)
        return name

    async def _addresses_of(self, session: ProviderSession, user_id: str) -> tuple[str, ...]:
        """The sender's mail address and sign-in name (they may differ), the mail first."""
        if not user_id:
            return ()
        cached = _emails.get(user_id)
        if cached is not None:
            return cached
        try:
            user = await self._call(
                session, "GET", f"{GRAPH}/users/{_segment(user_id, 'user id')}",
                params={"$select": "mail,userPrincipalName"}, allow_missing=True,
            ) or {}
        except ProviderError:
            # Not remembered: the next message asks again.
            return ()
        addresses: list[str] = []
        for value in (user.get("mail"), user.get("userPrincipalName")):
            if isinstance(value, str) and value.strip() and value.strip().lower() not in {a.lower() for a in addresses}:
                addresses.append(value.strip())
        _emails.put(user_id, tuple(addresses))
        return tuple(addresses)

    # -- normalization ------------------------------------------------------

    async def _event(
        self,
        session: ProviderSession,
        msg: dict,
        *,
        conversation_type: str,
        conversation_id: str,
        conversation_name: str | None,
        thread_id: str,
        route: dict[str, Any],
    ) -> AccountEvent | None:
        if msg.get("messageType", "message") != "message" or msg.get("deletedDateTime"):
            return None
        account_id = session.account_external_id or ""
        sender = msg.get("from") or {}
        author_kind, author_name = _teams_message_author(msg)
        sender_id = _author_id(msg)
        from_account = bool(sender_id) and sender_id == account_id
        from_application = author_kind != "user" or not sender
        addresses = () if from_account or from_application else await self._addresses_of(session, sender_id)
        text = _teams_message_text(msg) if msg.get("body") or msg.get("attachments") else ""
        attachments: tuple[AccountAttachment, ...] = ()
        if not (from_account or from_application):
            attachments = _message_attachments(msg, route)
            if text.strip() in ("", "[no text content]") and not attachments:
                attachments = await self._hosted_attachments(session, msg, route, attachments)
                if not attachments:
                    # Never an empty turn: Teams sends no empty message, so
                    # what it holds is content this account cannot read.
                    text = UNREADABLE_MESSAGE_TEXT
        return AccountEvent(
            provider=self.key,
            conversation_type=conversation_type,
            conversation_id=conversation_id,
            thread_id=thread_id,
            message_id=str(msg.get("id") or ""),
            sender_id=sender_id,
            sender_name=author_name,
            sender_email=addresses[0] if addresses else None,
            sender_emails=addresses,
            text=text,
            created_at=_parse_time(msg.get("createdDateTime")),
            conversation_name=conversation_name or (author_name if conversation_type == "direct" else None),
            mentions_account=bool(account_id) and _mentions(msg, account_id),
            from_account=from_account,
            from_application=from_application,
            route=route,
            attachments=attachments,
        )

    async def _hosted_attachments(
        self,
        session: ProviderSession,
        msg: dict,
        route: dict[str, Any],
        found: tuple[AccountAttachment, ...],
    ) -> tuple[AccountAttachment, ...]:
        """The hosted contents of a message that shows no text and no file (a voice clip).

        One Graph read of the message's ``hostedContents``, made only for such
        a message; the shape is logged (``message_shape``), never the content.
        A refused or failed read leaves the message as it was.
        """
        logger.info("Teams message %s has no text and no file: %s", msg.get("id"), message_shape(msg))
        message_url = _message_url(msg, route)
        if not message_url:
            return found
        try:
            hosted = await self._collect(
                session, f"{message_url}/hostedContents", None, MAX_MESSAGE_ATTACHMENTS
            )
        except ProviderError as exc:
            logger.info("Teams message %s: its hosted contents could not be listed: %s", msg.get("id"), exc)
            return found
        extra = unreferenced_hosted_contents(msg, route, hosted, found)
        if extra:
            logger.info("Teams message %s: %d hosted content(s) not referenced by its body", msg.get("id"), len(extra))
        return (found + extra)[:MAX_MESSAGE_ATTACHMENTS]

    async def _chat_event(self, session: ProviderSession, chat_id: str, msg: dict) -> AccountEvent | None:
        info = await self._chat_info(session, chat_id)
        return await self._event(
            session,
            msg,
            conversation_type=info["type"],
            conversation_id=chat_id,
            conversation_name=info["topic"] or None,
            thread_id=chat_id,
            route={"kind": "chat", "chat_id": chat_id},
        )

    async def _channel_event(
        self, session: ProviderSession, team_id: str, channel_id: str, msg: dict
    ) -> AccountEvent | None:
        root_id = str(msg.get("replyToId") or msg.get("id") or "")
        return await self._event(
            session,
            msg,
            conversation_type="channel",
            conversation_id=channel_id,
            conversation_name=await self._channel_name(session, team_id, channel_id),
            thread_id=f"{channel_id}:{root_id}",
            route={"kind": "channel", "team_id": team_id, "channel_id": channel_id, "root_id": root_id},
        )

    # -- push ---------------------------------------------------------------

    async def desired_subscriptions(
        self, session: ProviderSession, plan: ListeningPlan, *, reactions: bool = False
    ) -> list[DesiredSubscription]:
        return (await self.desired_listening(session, plan, reactions=reactions))[0]

    async def desired_listening(
        self, session: ProviderSession, plan: ListeningPlan, *, reactions: bool = False
    ) -> tuple[list[DesiredSubscription], list[UnheardConversation]]:
        account_id = session.account_external_id
        if not account_id:
            return [], []
        mention_filter = f"$filter=mentions/any(u: u/mentioned/user/id eq '{_segment(account_id, 'user id')}')"
        # A mention filter also filters the changes: those subscriptions stay ``created``.
        unfiltered = CHANGE_WITH_REACTIONS if reactions else CHANGE_CREATED
        # Refused with the changes, a subscription still hears the new messages.
        fallback = CHANGE_CREATED if reactions else None
        wanted: list[DesiredSubscription] = []
        if plan.chats:
            wanted.append(
                DesiredSubscription(
                    f"/users/{account_id}/chats/getAllMessages", unfiltered,
                    covers="chats", permission=CHAT_PERMISSION, fallback_change_type=fallback,
                )
            )
        named: set[str] = set()

        def channel(resource: str, mentions_only: bool, name: str | None) -> DesiredSubscription:
            if mentions_only:
                resource, change_type, fallback_change_type = f"{resource}?{mention_filter}", CHANGE_CREATED, None
            else:
                change_type, fallback_change_type = unfiltered, fallback
            return DesiredSubscription(
                resource, change_type, covers="channel", name=name, permission=CHANNEL_PERMISSION,
                fallback_change_type=fallback_change_type,
            )

        unheard: list[UnheardConversation] = []
        for target in plan.channels:
            if not target.team_id:
                logger.warning("Agent account channel %s has no team: not listened to", target.id[:40])
                unheard.append(
                    UnheardConversation(
                        target.name or target.id,
                        "The rule names this channel without its team: pick it again in the rule.",
                        note="channel_without_team",
                    )
                )
                continue
            named.add(target.id)
            resource = f"/teams/{_segment(target.team_id, 'team id')}/channels/{_segment(target.id, 'channel id')}/messages"
            name = target.name or _channels.get(f"{target.team_id}:{target.id}")
            wanted.append(channel(resource, target.mentions_only, name))
        if plan.all_channels:
            listed: list[str] = []
            discovered = [
                option
                for option in await self._account_channels(session, None, unlisted=listed)
                if option.id not in named and option.team_id
            ]
            for option in discovered[:MAX_DISCOVERED_CHANNELS]:
                resource = f"/teams/{option.team_id}/channels/{option.id}/messages"
                wanted.append(channel(resource, plan.all_channels_mentions_only, option.name))
            for option in discovered[MAX_DISCOVERED_CHANNELS:]:
                unheard.append(
                    UnheardConversation(
                        option.name,
                        f"A rule answering in every channel follows the first {MAX_DISCOVERED_CHANNELS} channels "
                        "of the account: name this channel in a rule to hear it.",
                        note="past_channel_cap",
                        note_values={"limit": MAX_DISCOVERED_CHANNELS},
                    )
                )
            for team in listed:
                unheard.append(
                    UnheardConversation(
                        team,
                        "Microsoft Graph refused to list the channels of this team to the account.",
                        covers="team",
                        note="team_channels_unlisted",
                    )
                )
        return wanted, unheard

    def subscription_slot(self, resource: str) -> str:
        # Graph grants one subscription per application and chat or channel,
        # whatever ``$filter`` it carries; a listed resource may drop the slash.
        return resource.split("?", 1)[0].strip("/").lower()

    async def event_from_notification(
        self, session: ProviderSession, notification: InboundNotification
    ) -> AccountEvent | None:
        resource = notification.payload.get("resource")
        if not isinstance(resource, str) or notification.payload.get("changeType") not in (None, "created"):
            return None
        channel = _CHANNEL_RESOURCE.search(resource)
        if channel:
            team_id, channel_id, root_id, reply_id = channel.groups()
            base = (
                f"{GRAPH}/teams/{_segment(team_id, 'team id')}/channels/{_segment(channel_id, 'channel id')}"
                f"/messages/{_segment(root_id, 'message id')}"
            )
            url = f"{base}/replies/{_segment(reply_id, 'message id')}" if reply_id else base
            msg = await self._call(session, "GET", url, allow_missing=True)
            if not msg:
                return None
            # The thread comes from Graph's own copy, never from the notification.
            if reply_id and str(msg.get("replyToId") or "") != root_id:
                return None
            return await self._channel_event(session, team_id, channel_id, msg)
        chat = _CHAT_RESOURCE.search(resource)
        if chat:
            chat_id, message_id = chat.groups()
            msg = await self._call(
                session,
                "GET",
                f"{GRAPH}/chats/{_segment(chat_id, 'chat id')}/messages/{_segment(message_id, 'message id')}",
                allow_missing=True,
            )
            if not msg:
                return None
            return await self._chat_event(session, chat_id, msg)
        return None

    # -- the message answered ------------------------------------------

    def message_reference(self, event: AccountEvent) -> dict[str, str]:
        """The ids the Microsoft Teams tools take to act on *event* (a reaction).

        A chat message: ``chat_id``. A channel message: ``team_id`` and
        ``channel_id``, plus ``parent_message_id`` for a thread reply, the
        argument names of ``add_microsoft_chat_message_reaction`` and
        ``add_microsoft_channel_message_reaction``.
        """
        route = event.route or {}
        if route.get("kind") == "chat" and route.get("chat_id"):
            return {"chat_id": str(route["chat_id"])}
        if route.get("kind") == "channel" and route.get("team_id") and route.get("channel_id"):
            reference = {"team_id": str(route["team_id"]), "channel_id": str(route["channel_id"])}
            root_id = str(route.get("root_id") or "")
            if root_id and root_id != event.message_id:
                reference["parent_message_id"] = root_id
            return reference
        return {}

    # -- reactions ---------------------------------------------------

    def updated_message(self, notification: InboundNotification) -> UpdatedMessage | None:
        resource = notification.payload.get("resource")
        if notification.payload.get("changeType") != "updated" or not isinstance(resource, str):
            return None
        channel = _CHANNEL_RESOURCE.search(resource)
        if channel:
            _team_id, channel_id, root_id, reply_id = channel.groups()
            return UpdatedMessage(conversation_id=channel_id, message_id=reply_id or root_id)
        chat = _CHAT_RESOURCE.search(resource)
        if chat:
            chat_id, message_id = chat.groups()
            return UpdatedMessage(conversation_id=chat_id, message_id=message_id)
        return None

    def reactions_pushed(self, route: dict[str, Any], subscriptions: list[tuple[str, str]]) -> bool:
        heard = [resource for resource, change_type in subscriptions if "updated" in change_type.split(",")]
        if route.get("kind") == "channel":
            wanted = f"/teams/{route.get('team_id')}/channels/{route.get('channel_id')}/messages"
            return wanted in heard
        return any(resource.endswith("/chats/getAllMessages") for resource in heard)

    async def read_reactions(
        self, session: ProviderSession, route: dict[str, Any], message_id: str
    ) -> list[PlatformReaction] | None:
        if route.get("kind") == "channel":
            url = (
                f"{GRAPH}/teams/{_segment(str(route.get('team_id') or ''), 'team id')}"
                f"/channels/{_segment(str(route.get('channel_id') or ''), 'channel id')}"
                f"/messages/{_segment(str(route.get('root_id') or ''), 'message id')}"
                f"/replies/{_segment(message_id, 'message id')}"
            )
        else:
            url = (
                f"{GRAPH}/chats/{_segment(str(route.get('chat_id') or ''), 'chat id')}"
                f"/messages/{_segment(message_id, 'message id')}"
            )
        msg = await self._call(session, "GET", url, allow_missing=True)
        if not msg or msg.get("deletedDateTime"):
            return None
        out: list[PlatformReaction] = []
        for reaction in msg.get("reactions") or []:
            if not isinstance(reaction, dict):
                continue
            user = ((reaction.get("user") or {}).get("user")) or {}
            user_id = str(user.get("id") or "")
            emoji = teams_reaction_emoji(reaction.get("reactionType"))
            if user_id and emoji:
                out.append(PlatformReaction(user_id=user_id, emoji=emoji))
        return out

    # -- poll ---------------------------------------------------------------

    async def poll(
        self, session: ProviderSession, plan: ListeningPlan, cursor: str | None
    ) -> tuple[list[AccountEvent], str | None]:
        if not plan.chats:
            return [], cursor
        now = datetime.now(UTC)
        if cursor is None:
            # A new cursor starts now: the account's history is never replayed.
            return [], now.isoformat()
        since = _parse_time(cursor)
        chats = await self._call(
            session,
            "GET",
            f"{GRAPH}/me/chats",
            params={
                "$expand": "lastMessagePreview",
                "$orderby": "lastMessagePreview/createdDateTime desc",
                "$top": str(POLL_CHATS),
            },
        ) or {}
        events: list[AccountEvent] = []
        newest = since
        for chat in chats.get("value") or []:
            preview = (chat or {}).get("lastMessagePreview") or {}
            if _parse_time(preview.get("createdDateTime")) <= since:
                break
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                continue
            _chats.put(
                chat_id,
                {"type": _CHAT_TYPES.get(str(chat.get("chatType") or ""), "group"), "topic": chat.get("topic") or ""},
            )
            page = await self._call(
                session,
                "GET",
                f"{GRAPH}/chats/{_segment(chat_id, 'chat id')}/messages",
                params={"$top": str(POLL_MESSAGES_PER_CHAT)},
            ) or {}
            for msg in page.get("value") or []:
                created = _parse_time((msg or {}).get("createdDateTime"))
                if created <= since:
                    continue
                newest = max(newest, created)
                event = await self._chat_event(session, chat_id, msg)
                if event is not None:
                    events.append(event)
        events.sort(key=lambda e: e.created_at)
        return events, newest.isoformat()

    async def thread_messages_since(
        self, session: ProviderSession, route: dict[str, Any], since: datetime
    ) -> list[AccountEvent]:
        if route.get("kind") != "channel":
            return []
        team_id = _segment(str(route.get("team_id") or ""), "team id")
        channel_id = _segment(str(route.get("channel_id") or ""), "channel id")
        root_id = _segment(str(route.get("root_id") or ""), "message id")
        replies = await self._collect(
            session,
            f"{GRAPH}/teams/{team_id}/channels/{channel_id}/messages/{root_id}/replies",
            {"$top": "50"},
            FOLLOWED_REPLIES_READ,
        )
        events: list[AccountEvent] = []
        for msg in replies:
            # The thread comes from Graph's own copy of each reply.
            if _parse_time(msg.get("createdDateTime")) <= since or str(msg.get("replyToId") or "") != root_id:
                continue
            event = await self._channel_event(session, team_id, channel_id, msg)
            if event is not None:
                events.append(event)
        events.sort(key=lambda e: e.created_at)
        return events

    # -- answering ----------------------------------------------------------

    async def _thread_messages(self, session: ProviderSession, event: AccountEvent, limit: int) -> list[dict]:
        route = event.route
        if route.get("kind") == "channel":
            base = (
                f"{GRAPH}/teams/{_segment(str(route['team_id']), 'team id')}"
                f"/channels/{_segment(str(route['channel_id']), 'channel id')}"
                f"/messages/{_segment(str(route['root_id']), 'message id')}"
            )
            root = await self._call(session, "GET", base, allow_missing=True)
            replies = await self._collect(session, f"{base}/replies", {"$top": "50"}, max(limit, 50))
            return ([root] if root else []) + replies
        chat_id = _segment(str(route.get("chat_id") or event.conversation_id), "chat id")
        page = await self._call(
            session, "GET", f"{GRAPH}/chats/{chat_id}/messages", params={"$top": str(min(50, max(limit + 1, 2)))}
        ) or {}
        return [m for m in page.get("value") or [] if isinstance(m, dict)]

    async def fetch_context(
        self, session: ProviderSession, event: AccountEvent, limit: int
    ) -> list[ContextMessage]:
        if limit <= 0:
            return []
        account_id = session.account_external_id or ""
        out: list[ContextMessage] = []
        for msg in await self._thread_messages(session, event, limit):
            if msg.get("messageType", "message") != "message" or msg.get("deletedDateTime"):
                continue
            created = _parse_time(msg.get("createdDateTime"))
            if created > event.created_at or str(msg.get("id")) == event.message_id:
                continue
            _kind, name = _teams_message_author(msg)
            out.append(
                ContextMessage(
                    message_id=str(msg.get("id") or ""),
                    sender_name=name,
                    text=_teams_message_text(msg),
                    created_at=created,
                    from_account=bool(account_id) and _author_id(msg) == account_id,
                )
            )
        out.sort(key=lambda m: m.created_at)
        return out[-limit:]

    async def account_messages_since(self, session: ProviderSession, event: AccountEvent) -> list[str]:
        account_id = session.account_external_id or ""
        if not account_id:
            return []
        return [
            str(msg.get("id"))
            for msg in await self._thread_messages(session, event, 20)
            if _author_id(msg) == account_id
            # A deleted post or a system event is nobody answering the message.
            and msg.get("messageType", "message") == "message"
            and not msg.get("deletedDateTime")
            and _parse_time(msg.get("createdDateTime")) > event.created_at
        ]

    @staticmethod
    def _reply_url(route: dict[str, Any]) -> str:
        if route.get("kind") == "channel":
            return (
                f"{GRAPH}/teams/{_segment(str(route.get('team_id') or ''), 'team id')}"
                f"/channels/{_segment(str(route.get('channel_id') or ''), 'channel id')}"
                f"/messages/{_segment(str(route.get('root_id') or ''), 'message id')}/replies"
            )
        return f"{GRAPH}/chats/{_segment(str(route.get('chat_id') or ''), 'chat id')}/messages"

    async def _members(self, session: ProviderSession, route: dict[str, Any]) -> list[dict[str, Any]]:
        """The members of the chat or channel *route* posts in."""
        if route.get("kind") == "channel":
            url = (
                f"{GRAPH}/teams/{_segment(str(route.get('team_id') or ''), 'team id')}"
                f"/channels/{_segment(str(route.get('channel_id') or ''), 'channel id')}/members"
            )
        else:
            url = f"{GRAPH}/chats/{_segment(str(route.get('chat_id') or ''), 'chat id')}/members"
        return await self._collect(session, url, None, MAX_MENTION_MEMBERS)

    async def _reply_body(self, session: ProviderSession, route: dict[str, Any], text: str) -> dict[str, Any]:
        """The Graph message of *text*, its ``@`` tokens of members as real mentions.

        Only a text with an ``@`` token reads the members; one that cannot be
        read leaves every token as written. What matched and what did not is
        kept in ``session.delivery`` for the pipeline to record.
        """
        if not may_mention(text):
            return {"body": build_graph_chat_body(text)}
        try:
            members = members_of(await self._members(session, route), session.account_external_id or "")
        except ProviderError as exc:
            logger.info("The members of a Teams reply's conversation could not be read: %s", exc)
            members = []
        resolved = resolve_reply_mentions(text, members)
        session.delivery.update(resolved.report())
        if not resolved.mentions:
            return {"body": build_graph_chat_body(text)}
        try:
            body = build_graph_chat_body(
                resolved.text, mention_texts=[mention.display_name for mention in resolved.mentions]
            )
        except MentionPlacementError as exc:
            logger.info("The mentions of a Teams reply were left as text: %s", exc)
            session.delivery.update(
                mentions_resolved=[], mentions_unresolved=[f"@{m.display_name}" for m in resolved.mentions]
            )
            return {"body": build_graph_chat_body(text)}
        return {
            "body": body,
            "mentions": [mention.graph_entry(index) for index, mention in enumerate(resolved.mentions)],
        }

    async def _late_reply_quote(self, session: ProviderSession, route: dict[str, Any]) -> dict[str, Any] | None:
        """The chat message the reply answers, when a newer one came in after it (``teams_late_reply``).

        A chat route only, with the answered id known: one read of the chat's
        latest messages. A failed read posts the reply as before, without a quote.
        """
        answered = session.answers_message_id
        if route.get("kind") != "chat" or not answered:
            return None
        chat_id = _segment(str(route.get("chat_id") or ""), "chat id")
        try:
            page = (
                await self._call(
                    session, "GET", f"{GRAPH}/chats/{chat_id}/messages", params={"$top": str(RECENT_MESSAGES)}
                )
                or {}
            )
        except ProviderError as exc:
            logger.info("The latest messages of a Teams chat could not be read before a reply: %s", exc)
            return None
        messages = [m for m in page.get("value") or [] if isinstance(m, dict)]
        quoted = message_to_quote(messages, [answered], account_id=session.account_external_id or None)
        if quoted is not None:
            session.delivery["quoted_message_id"] = str(quoted.get("id") or "")
        return quoted

    async def send_reply(self, session: ProviderSession, route: dict[str, Any], text: str) -> str:
        body = await self._reply_body(session, route, text)
        quoted = await self._late_reply_quote(session, route)
        if quoted is not None:
            body = with_quote(body, quoted)
        sent = await self._call(session, "POST", self._reply_url(route), payload=body) or {}
        message_id = sent.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ProviderError("Microsoft Graph accepted the message but returned no id.")
        return message_id

    # -- files of a reply ---------------------------------------------

    async def _put_bytes(self, session: ProviderSession, url: str, data: bytes, content_type: str) -> dict:
        """Upload *data* to the drive path *url* as the account; the driveItem."""
        try:
            with egress_route(session.route):
                async with session.client(timeout=120) as client:
                    resp = await client.put(
                        url,
                        headers={**session.headers, "Content-Type": content_type or "application/octet-stream"},
                        content=data,
                    )
        except Exception as exc:
            raise ProviderError(f"Microsoft Graph could not be reached: {type(exc).__name__}", kind="unavailable") from None
        if resp.status_code >= 400:
            raise _graph_error(resp)
        try:
            item = resp.json()
        except ValueError:
            raise ProviderError("Microsoft Graph answered the upload with a body that is not JSON.") from None
        return item if isinstance(item, dict) else {}

    async def _share_with_chat(self, session: ProviderSession, chat_id: str, item_url: str) -> None:
        """Give every member of the chat but the account read access to the item, and check it.

        The grant of ``send_microsoft_chat_file``: invited read-only,
        signed in, no email, the permissions read back. Raises when a member
        is left without access.
        """
        members = await self._collect(session, f"{GRAPH}/chats/{chat_id}/members", None, 500)
        recipients = _chat_share_recipients(members, session.account_external_id or "")
        if not recipients:
            return
        await self._call(
            session,
            "POST",
            f"{item_url}/invite",
            payload={
                "recipients": [recipient.drive_recipient() for recipient in recipients],
                "roles": ["read"],
                "requireSignIn": True,
                "sendInvitation": False,
            },
        )
        permissions = await self._collect(session, f"{item_url}/permissions", None, 1000)
        missing = _ungranted_recipients(recipients, permissions)
        if missing:
            raise ProviderError(f"Microsoft lists no access for {', '.join(missing)}.")

    async def _upload_reply_file(self, session: ProviderSession, route: dict[str, Any], file: ReplyFile) -> dict:
        """Store *file* where Teams keeps the files of the conversation, readable by its members.

        A chat: the account's OneDrive, folder "Microsoft Teams Chat Files",
        shared with the chat's members. A channel: the channel's Files folder,
        whose SharePoint site the team's members already read.
        """
        name = _encode_drive_filename(file.name)
        if route.get("kind") == "channel":
            team = _segment(str(route.get("team_id") or ""), "team id")
            channel = _segment(str(route.get("channel_id") or ""), "channel id")
            folder = await self._call(session, "GET", f"{GRAPH}/teams/{team}/channels/{channel}/filesFolder") or {}
            drive_id = str((folder.get("parentReference") or {}).get("driveId") or "")
            folder_id = str(folder.get("id") or "")
            if not drive_id or not folder_id:
                raise ProviderError("The channel's Files folder could not be found.")
            base = f"{GRAPH}/drives/{_segment(drive_id, 'drive id')}/items"
            put_url = f"{base}/{_segment(folder_id, 'folder id')}:/{name}:/content"
        else:
            base = f"{GRAPH}/me/drive/items"
            put_url = f"{GRAPH}/me/drive/root:/Microsoft Teams Chat Files/{name}:/content"
        uploaded = await self._put_bytes(session, put_url, file.data, file.content_type)
        item_id = _segment(str(uploaded.get("id") or ""), "drive item id")
        item_url = f"{base}/{item_id}"
        item = await self._call(session, "GET", item_url, params={"$select": "id,name,eTag,webUrl,webDavUrl"}) or {}
        if route.get("kind") != "channel":
            await self._share_with_chat(session, _segment(str(route.get("chat_id") or ""), "chat id"), item_url)
        return item

    async def send_reply_files(
        self, session: ProviderSession, route: dict[str, Any], text: str, files: list[ReplyFile]
    ) -> str:
        """Post *text* with each file as a Teams file card.

        A file that cannot be stored, shared with the members or carded is
        named by one plain line instead; no file delivered posts the text alone.
        """
        attachments: list[dict[str, str]] = []
        undelivered: list[str] = []
        for file in files:
            if not file.data:
                undelivered.append(file.name)
                continue
            try:
                attachment = reference_attachment(await self._upload_reply_file(session, route, file))
            except ProviderError as exc:
                logger.info("A file of an agent account's Teams reply is named, not attached: %s", exc)
                undelivered.append(file.name)
                continue
            if attachment is None:
                undelivered.append(file.name)
            else:
                attachments.append(attachment)
        caption = with_file_lines(text, undelivered)
        if not attachments:
            return await self.send_reply(session, route, caption)
        message = await self._reply_body(session, route, caption) if caption else {
            "body": {"contentType": "html", "content": ""}
        }
        tags = "".join(f'<attachment id="{a["id"]}"></attachment>' for a in attachments)
        payload = {
            **message,
            "body": {"contentType": "html", "content": message["body"]["content"] + tags},
            "attachments": attachments,
        }
        quoted = await self._late_reply_quote(session, route)
        if quoted is not None:
            payload = with_quote(payload, quoted)
        sent = await self._call(session, "POST", self._reply_url(route), payload=payload) or {}
        message_id = sent.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ProviderError("Microsoft Graph accepted the message but returned no id.")
        return message_id

    async def download_attachment(
        self, session: ProviderSession, attachment: AccountAttachment, max_bytes: int
    ) -> tuple[bytes, str]:
        # Only Graph's own addresses get the account's token; a shared file's
        # content answers with a redirect to a pre-authenticated location,
        # followed without the token (``fetch_file``).
        if not attachment.url.startswith(f"{GRAPH}/"):
            raise ProviderError("The attachment is not a Microsoft Graph address.")
        with egress_route(session.route):
            async with session.client(timeout=60) as client:
                fetched = await fetch_file(
                    client,
                    "GET",
                    attachment.url,
                    provider="Microsoft Teams",
                    action=f"download {attachment.name}",
                    max_bytes=max_bytes,
                    follow_redirects=True,
                )
        if isinstance(fetched, FetchError):
            raise ProviderError(str(fetched).removeprefix("Error: "))
        return fetched.data, fetched.content_type

    # -- editor -------------------------------------------------------------

    async def _account_channels(
        self, session: ProviderSession, limit: int | None, *, unlisted: list[str] | None = None
    ) -> list[ConversationOption]:
        """The channels of the account's teams, at most *limit* (``None``: every one).

        A team whose channels Graph refuses to list is skipped, its name added
        to *unlisted* when given.
        """
        teams = await self._collect(session, f"{GRAPH}/me/joinedTeams", {"$select": "id,displayName"}, 100)
        options: list[ConversationOption] = []
        for team in teams:
            team_id = str(team.get("id") or "")
            if not team_id:
                continue
            if limit is not None and len(options) >= limit:
                break
            try:
                channels = await self._collect(
                    session,
                    f"{GRAPH}/teams/{_segment(team_id, 'team id')}/channels",
                    {"$select": "id,displayName"},
                    100,
                )
            except ProviderError as exc:
                logger.info("Skipped the channels of a team the account cannot list: %s", exc)
                if unlisted is not None:
                    unlisted.append(str(team.get("displayName") or team_id))
                continue
            for channel in channels:
                channel_id = str(channel.get("id") or "")
                if not channel_id:
                    continue
                name = f"{team.get('displayName') or team_id} > {channel.get('displayName') or channel_id}"
                _channels.put(f"{team_id}:{channel_id}", name)
                options.append(ConversationOption(id=channel_id, name=name, type="channel", team_id=team_id))
                if limit is not None and len(options) >= limit:
                    break
        return options

    async def list_conversations(
        self, session: ProviderSession, query: str | None = None
    ) -> list[ConversationOption]:
        account_id = session.account_external_id or ""
        chats = await self._collect(
            session, f"{GRAPH}/me/chats", {"$expand": "members", "$top": "50"}, PICKER_LIMIT
        )
        options: list[ConversationOption] = []
        for chat in chats:
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                continue
            kind = _CHAT_TYPES.get(str(chat.get("chatType") or ""), "group")
            others = [
                str(m.get("displayName") or "")
                for m in chat.get("members") or []
                if isinstance(m, dict) and str(m.get("userId") or "") != account_id and m.get("displayName")
            ]
            name = chat.get("topic") or ", ".join(others[:4]) or chat_id
            options.append(ConversationOption(id=chat_id, name=name, type=kind))
        options.extend(await self._account_channels(session, PICKER_LIMIT))
        if query:
            needle = query.strip().lower()
            options = [o for o in options if needle in o.name.lower()]
        return options[:PICKER_LIMIT]
