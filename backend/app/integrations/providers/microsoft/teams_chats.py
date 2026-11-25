"""Microsoft Teams chat tools: chats and chat messages."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.integrations.http import format_entity_list
from app.services import chat_reply_guard
from app.services.teams_chat_posts import REPLY_GUARD_CHANNEL, chat_conversation_key, record_teams_chat_post
from app.services.teams_format import build_graph_chat_body
from app.services.teams_late_reply import RECENT_MESSAGES, message_to_quote, with_quote
from app.tools.base import CallableToolExecutor, SendGuard, ToolDefinition, WriteCoalescing, WriteReadBack

from ._cards import card_note, card_schema, card_sent_note, nothing_to_post, parse_card
from ._common import (
    _BASE_URL,
    _PROVIDER,
    MicrosoftContext,
    _coerce_int,
    _parse_graph_timestamp,
)
from ._kept_files import file_attachments, keep_file_attachments, kept_files_note
from ._mentions import (
    CHAT_MENTION_TYPES,
    build_message,
    mentions_schema,
    parse_mentions,
    posted_note,
    sent_note,
)
from ._teams import (
    _chat_message_target,
    _format_chat_listing,
    _format_chat_participants,
    _format_message_detail,
    _format_unreferenced_hosted_contents,
    _looks_empty,
    _soft_delete_message,
    _teams_segment,
    _validate_teams_chat_id,
    _validate_teams_message_id,
    teams_reaction_refusal,
    teams_reaction_type,
)
from ._voice_notes import audio_hosted_contents, heard_hosted_lines, heard_lines, with_heard

logger = logging.getLogger(__name__)

#: Graph's ``$top`` ceiling on a chat's messages: a larger value is refused
#: ("limit of 50 for Top exceeded").
_CHAT_MESSAGES_PAGE = 50
#: The most messages ``list_microsoft_chat_messages`` reads, page by page.
_CHAT_MESSAGES_MAX = 200


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the teams chats tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request

    # ── Teams Chat Tools ───────────────────────────────────────────────

    async def list_microsoft_chats(count: int = 20) -> str:
        """List the user's recent Microsoft Teams chats (1:1 and group).

        Returns chat IDs, topics, types, and participants.  Use the
        returned ``chat_id`` to read messages or send new ones.
        """
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/chats",
                _PROVIDER,
                "list chats",
                params={"$top": str(count), "$expand": "members"},
            )
            if err:
                return err
            chats = resp.json().get("value", [])

            def _fmt_chat(ch: dict) -> str:
                chat_type = ch.get("chatType", "unknown")
                topic = ch.get("topic") or "(no topic)"
                members_str = _format_chat_participants(ch.get("members", []))
                return (
                    f"Chat ID: {ch.get('id', '')}\n"
                    f"Type: {chat_type} | Topic: {topic}\n"
                    f"Participants: {members_str}\n"
                    f"Last updated: {ch.get('lastUpdatedDateTime', '')}"
                )

            return format_entity_list(chats, _fmt_chat, noun="chat")

    async def list_microsoft_chat_messages(chat_id: str, count: int = 20) -> str:
        """Read recent messages from a Microsoft Teams chat.

        Retrieves the most recent messages from a 1:1 or group chat.
        Use ``list_chats`` first to find the ``chat_id``. Graph serves at most
        ``_CHAT_MESSAGES_PAGE`` messages a page (a larger ``$top`` is refused):
        *count* is clamped to ``_CHAT_MESSAGES_MAX`` and read page by page.
        """
        if err := _validate_teams_chat_id(chat_id, "list_microsoft_chat_messages"):
            return err
        count = max(1, min(_coerce_int(count, 20), _CHAT_MESSAGES_MAX))
        async with _client() as client:
            messages: list[dict] = []
            next_url: str | None = f"{_BASE_URL}/me/chats/{_teams_segment(chat_id)}/messages"
            params: dict | None = {
                "$top": str(min(count, _CHAT_MESSAGES_PAGE)),
                "$orderby": "createdDateTime desc",
            }
            while next_url and len(messages) < count:
                kwargs: dict = {"params": params} if params else {}
                resp, err = await _throttled_request(client, "get", next_url, _PROVIDER, "read chat messages", **kwargs)
                if err:
                    return err
                data = resp.json()
                messages.extend(data.get("value", []))
                next_url = data.get("@odata.nextLink")
                params = None  # the nextLink carries the query
            messages = messages[:count]
            if not messages:
                return "No messages in this chat."
            # Reverse to show chronological order
            messages.reverse()
            (lines,) = _format_chat_listing([(chat_id, messages)], include_message_id=True)
            if not lines:
                return "No user messages in this chat (only system events)."
            return f"Found {len(lines)} message(s):\n\n" + "\n---\n".join(lines)

    async def list_all_microsoft_chat_messages(
        count: int = 20,
        messages_per_chat: int = 10,
        hours: int = 0,
    ) -> str:
        """Fetch recent messages from ALL of the user's Microsoft Teams chats in one call.

        This compound tool avoids the O(N) iteration pattern of calling
        ``list_microsoft_chats`` followed by one ``list_microsoft_chat_messages``
        per chat.  It lists all chats first, then concurrently fetches messages
        from each one inside a shared semaphore, and returns the full picture in
        a single LLM iteration.

        Use this when you need to monitor or digest messages across multiple
        chats at once (e.g. "Monitor & Answer Deal Chat" assignments).  For
        reading a *specific* chat you already know, prefer
        ``list_microsoft_chat_messages(chat_id)``.

        Parameters
        ----------
        count:
            Maximum number of chats to include (default 20, max 50).
        messages_per_chat:
            Maximum messages to fetch per chat (default 10, max 50).
        hours:
            When > 0, only include messages created in the last N hours.
            When 0 (default), returns the most recent ``messages_per_chat``
            messages per chat regardless of age.
        """
        # Tool arguments arrive untyped from the LLM — coerce before clamping
        # so a string "20" (or None) can't TypeError the whole tool call.
        count = max(1, min(_coerce_int(count, 20), 50))
        messages_per_chat = max(1, min(_coerce_int(messages_per_chat, 10), 50))
        hours = max(0, _coerce_int(hours, 0))

        since_dt: datetime | None = None
        if hours > 0:
            since_dt = datetime.now(UTC) - timedelta(hours=hours)

        # One shared client for the chat listing AND every per-chat fetch —
        # a per-chat AutoRefreshClient would open up to ``count`` separate
        # httpx connection pools and, on an expired token, fire one refresh
        # attempt per chat instead of one for the whole tool call (mirrors
        # ``list_microsoft_channel_messages``'s shared-client fan-out).
        async with _client() as client:
            # Step 1: list chats. ``$orderby`` pins Graph to
            # most-recent-message-first — the endpoint's default order is the
            # user's "last action" on each chat, which for large accounts
            # (hundreds of chats, ``$top`` capped at 50) could silently drop
            # the very chats a monitoring assignment cares about.
            #
            # ``$expand=members`` must stay in its PLAIN form: the /me/chats
            # endpoint rejects a nested select (``members($select=...)``)
            # with 400 "Unsupported query parameters", and ``$select``
            # itself is not in the endpoint's supported OData parameters
            # (only $expand, $top, $filter, $orderby are — see Graph's
            # chat-list reference). Plain ``$expand=members`` is the exact
            # shape ``list_microsoft_chats`` has always used in production.
            #
            # Graph caps pages at 25 chats when ``$expand=members`` is
            # present (documented known issue), so follow ``@odata.nextLink``
            # until ``count`` chats are collected — otherwise count > 25
            # would silently truncate.
            base_params = {
                "$top": str(count),
                "$orderby": "lastMessagePreview/createdDateTime desc",
            }
            chats: list[dict] = []
            next_url: str | None = f"{_BASE_URL}/me/chats"
            next_params: dict[str, str] | None = {
                **base_params,
                "$expand": "members",
            }
            for _page in range(5):  # count <= 50 / 25-per-page => 2 pages max
                if not next_url or len(chats) >= count:
                    break
                resp, err = await _throttled_request(
                    client,
                    "get",
                    next_url,
                    _PROVIDER,
                    "list chats (all messages)",
                    params=next_params,
                )
                if err and not chats and next_params is not None:
                    # Safety net: if Graph ever rejects this documented query
                    # shape too, as it rejects a nested select,
                    # degrade to a member-less listing (participants blank)
                    # instead of failing the whole tool call again.
                    logger.warning(
                        "list_all_microsoft_chat_messages: expanded chat "
                        "listing rejected, retrying without $expand: %s",
                        err,
                    )
                    resp, err = await _throttled_request(
                        client,
                        "get",
                        next_url,
                        _PROVIDER,
                        "list chats (all messages)",
                        params=base_params,
                    )
                if err:
                    if chats:
                        # A later page failing should not throw away the
                        # chats already collected — degrade to a partial list.
                        logger.warning(
                            "list_all_microsoft_chat_messages: chat paging "
                            "stopped early after %d chat(s): %s",
                            len(chats),
                            err,
                        )
                        break
                    return err
                payload = resp.json()
                chats.extend(payload.get("value", []))
                # The nextLink already encodes the original query parameters.
                next_url = payload.get("@odata.nextLink")
                next_params = None
            chats = chats[:count]

            if not chats:
                return "No Microsoft Teams chats found."

            # Step 2: fetch messages from each chat concurrently.
            # ``_throttled_request`` already throttles via ``_api_sem``; we
            # just fire all coroutines at once and let the semaphore pace them.

            async def _fetch_chat(chat: dict) -> tuple[list[dict], str | None]:
                """Return (messages, error_str) for one chat."""
                chat_id = chat.get("id", "")
                r, e = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/me/chats/{_teams_segment(chat_id)}/messages",
                    _PROVIDER,
                    "read chat messages (all)",
                    params={
                        "$top": str(messages_per_chat),
                        "$orderby": "createdDateTime desc",
                    },
                )
                if e:
                    return [], e
                msgs = r.json().get("value", [])
                # Optional time filter applied client-side — the chat
                # messages endpoint does not support ``$filter``. Compare
                # parsed datetimes (not raw strings): Graph mixes fractional
                # and whole-second formats, which do not sort lexicographically.
                if since_dt is not None:
                    msgs = [
                        m
                        for m in msgs
                        if (ts := _parse_graph_timestamp(m.get("createdDateTime")))
                        is not None
                        and ts >= since_dt
                    ]
                return msgs, None

            results = await asyncio.gather(
                *(_fetch_chat(ch) for ch in chats),
                return_exceptions=True,
            )

        # ``asyncio.gather`` preserves input order, so ``results[i]`` always
        # belongs to ``chats[i]`` — zip them back together so an exception
        # result can still be attributed to its chat in the output.
        outcomes: list[tuple[dict, list[dict], str | None]] = []
        for chat, res in zip(chats, results, strict=True):
            if isinstance(res, BaseException):
                # Tool callers never see server logs — surface the failure
                # in the returned content so monitoring assignments can
                # detect it instead of the chat silently vanishing.
                logger.warning(
                    "list_all_microsoft_chat_messages: task raised unexpectedly: %r",
                    res,
                )
                outcomes.append((chat, [], f"unexpected error: {res!r}"))
            else:
                messages, fetch_err = res
                # Reverse to show chronological order (oldest → newest)
                outcomes.append((chat, list(reversed(messages)), fetch_err))
        # One text budget for the messages of every chat.
        chat_lines = _format_chat_listing([(chat.get("id", ""), messages) for chat, messages, _ in outcomes])

        sections: list[str] = []
        total_messages = 0
        chats_with_messages = 0
        for (chat, messages, fetch_err), lines in zip(outcomes, chat_lines, strict=True):
            chat_type = chat.get("chatType", "unknown")
            topic = chat.get("topic") or "(no topic)"
            members_str = _format_chat_participants(chat.get("members", []))

            header = (
                f"### Chat: {topic} [{chat_type}]\n"
                f"Participants: {members_str}\n"
                f"Chat ID: {chat.get('id', '')}"
            )

            if fetch_err:
                sections.append(f"{header}\n(Could not fetch messages: {fetch_err})")
                continue

            if not messages:
                time_note = f" in the last {hours}h" if hours > 0 else ""
                sections.append(f"{header}\n(No messages{time_note}.)")
                continue

            if lines:
                total_messages += len(lines)
                chats_with_messages += 1
                sections.append(header + "\n" + "\n---\n".join(lines))
            else:
                sections.append(f"{header}\n(No user messages — only system events.)")

        if not sections:
            return "No messages found across any chats."

        time_note = f" (last {hours}h)" if hours > 0 else ""
        # ``len(results)`` counts every chat attempted — including the ones
        # whose fetch failed and only produced an error placeholder section.
        summary = (
            f"Found {total_messages} message(s) in {chats_with_messages} of "
            f"{len(results)} chat(s){time_note}:\n\n"
        )
        return summary + "\n\n---\n\n".join(sections)

    async def get_microsoft_chat_message(chat_id: str = "", message_id: str = "", url: str = "") -> str:
        """Read one Microsoft Teams chat message (1:1 or group) in full.

        Addressed by its IDs or by its Teams link (``url``). Nothing is cut:
        the whole text, what a listing appends to it and its reactions. A
        voice note in the message comes back transcribed, as
        ``[voice note, 0:20, transcribed] ...``, or with why it could not be
        heard.
        """
        tool = "get_microsoft_chat_message"
        target, err = _chat_message_target(chat_id, message_id, url, tool)
        if err:
            return err
        chat, mid = target["chat_id"], target["message_id"]
        if err := _validate_teams_chat_id(chat, tool) or _validate_teams_message_id(mid, tool):
            return err
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/chats/{_teams_segment(chat)}/messages/{_teams_segment(mid)}",
                _PROVIDER,
                "read chat message",
            )
            if err:
                return err
            msg = resp.json()
            heard = await heard_lines(ctx, client, msg)
            suffix = ""
            if _looks_empty(msg):
                # A voice clip is a hosted content the body does not show:
                # one more read, for an empty-looking message only. The
                # clips are heard; any other hosted content keeps its pointer.
                message_url = f"{_BASE_URL}/chats/{_teams_segment(chat)}/messages/{_teams_segment(mid)}"
                hosted_resp, hosted_err = await _throttled_request(
                    client,
                    "get",
                    f"{message_url}/hostedContents",
                    _PROVIDER,
                    "list the message's hosted contents",
                )
                if not hosted_err and hosted_resp is not None:
                    hosted = (hosted_resp.json() or {}).get("value") or []
                    clips, rest = audio_hosted_contents(hosted)
                    heard += await heard_hosted_lines(ctx, client, message_url, clips, already=len(heard))
                    suffix = _format_unreferenced_hosted_contents(msg, rest, chat, mid)
        return with_heard(
            _format_message_detail(msg, f"Teams chat message {mid} (chat_id: {chat})", suffix=suffix), heard
        )

    async def send_microsoft_chat_message(
        chat_id: str,
        message: str = "",
        content_type: str = "text",
        mentions: list | str | None = None,
        card: dict | str | None = None,
    ) -> str:
        """Send a message in a Microsoft Teams chat (1:1 or group).

        Use ``list_chats`` to find the ``chat_id``. ``mentions`` @mentions
        people or the whole chat; ``card`` posts an Adaptive Card below the text.
        """
        if err := _validate_teams_chat_id(chat_id, "send_microsoft_chat_message"):
            return err
        requests, err = parse_mentions(mentions, CHAT_MENTION_TYPES)
        if err:
            return err
        adaptive_card, err = parse_card(card)
        if err:
            return err
        if err := nothing_to_post(message, adaptive_card, requests):
            return err
        # One reply per inbound message: a run answering a chat message
        # another run already answered is refused here, logged and counted.
        guard = await chat_reply_guard.claim(REPLY_GUARD_CHANNEL, chat_conversation_key(chat_id.strip()))
        if guard.refused:
            return guard.refusal(edit_tool="update_microsoft_chat_message")
        async with _client() as client:
            payload, sent, err = await build_message(
                ctx, client, message, content_type, requests, chat_id=chat_id.strip(), card=adaptive_card
            )
            if err:
                await guard.release()
                return err
            quoted = await _late_reply_quote(client, chat_id.strip(), guard)
            if quoted is not None and payload is not None:
                payload = with_quote(payload, quoted)
            resp, err = await _throttled_request(
                client,
                "post",
                f"{_BASE_URL}/me/chats/{_teams_segment(chat_id)}/messages",
                _PROVIDER,
                "send chat message",
                json=payload,
            )
            if err:
                await guard.release()
                await ctx.log_account_send(
                    "microsoft_teams", "send_microsoft_chat_message", conversation_id=chat_id, text=message, error=err
                )
                return err
            msg = resp.json()
            await guard.confirm()
            await record_teams_chat_post(chat_id, msg)
            await ctx.log_account_send(
                "microsoft_teams",
                "send_microsoft_chat_message",
                conversation_id=chat_id,
                text=message,
                message_id=msg.get("id") if isinstance(msg, dict) else None,
            )
            return (
                f"Chat message sent. Message ID: {msg.get('id', '')}. "
                f"Sent at: {msg.get('createdDateTime', '')}."
                + posted_note(sent, msg, "list_microsoft_chat_messages")
                + card_note(payload, msg, "list_microsoft_chat_messages")
                + chat_reply_guard.answered_note(guard)
                + (f" Quotes message {quoted.get('id')}: a newer message came in after it." if quoted else "")
            )

    async def _late_reply_quote(client: Any, chat_id: str, guard: chat_reply_guard.Claim) -> dict[str, Any] | None:
        """The message this turn answers, when a newer one came in after it (``teams_late_reply``).

        Only a run bound to this chat's turn reads anything: two Graph reads,
        the signed-in user's id and the chat's latest messages. A failed read
        posts the reply as before, without a quote.
        """
        turn = guard.turn
        if turn is None or not turn.inbound_ids:
            return None
        me, err = await _throttled_request(
            client, "get", f"{_BASE_URL}/me", _PROVIDER, "read the signed-in user", params={"$select": "id"}
        )
        if err or me is None:
            return None
        recent, err = await _throttled_request(
            client,
            "get",
            f"{_BASE_URL}/me/chats/{_teams_segment(chat_id)}/messages",
            _PROVIDER,
            "read the chat's latest messages",
            params={"$top": str(RECENT_MESSAGES), "$orderby": "createdDateTime desc"},
        )
        if err or recent is None:
            return None
        try:
            account_id = str((me.json() or {}).get("id") or "")
            messages = (recent.json() or {}).get("value") or []
        except ValueError:
            return None
        return message_to_quote(messages, turn.inbound_ids, account_id=account_id or None)

    async def update_microsoft_chat_message(
        chat_id: str,
        message_id: str,
        message: str = "",
        content_type: str = "text",
        mentions: list | str | None = None,
        card: dict | str | None = None,
    ) -> str:
        """Edit a previously sent Microsoft Teams chat message (1:1 or group).

        Sends the whole message again (its body, and its card when ``card``
        is given) for a message the signed-in user sent. A file the message
        carries (sent by ``send_microsoft_chat_file``) is kept on it. Use the
        ``message_id`` returned by ``send_microsoft_chat_message`` (or listed
        by ``list_microsoft_chat_messages``).
        """
        if err := _validate_teams_chat_id(chat_id, "update_microsoft_chat_message"):
            return err
        if err := _validate_teams_message_id(
            message_id, "update_microsoft_chat_message"
        ):
            return err
        requests, err = parse_mentions(mentions, CHAT_MENTION_TYPES)
        if err:
            return err
        adaptive_card, err = parse_card(card)
        if err:
            return err
        if err := nothing_to_post(message, adaptive_card, requests):
            return err
        async with _client() as client:
            payload, sent, err = await build_message(
                ctx, client, message, content_type, requests, chat_id=chat_id.strip(), card=adaptive_card
            )
            if err:
                return err
            message_url = f"{_BASE_URL}/chats/{_teams_segment(chat_id)}/messages/{_teams_segment(message_id)}"
            # Graph replaces the message whole: its file cards are sent back.
            current, read_err = await _throttled_request(client, "get", message_url, _PROVIDER, "read chat message")
            files = file_attachments(current.json() if current is not None and not read_err else None)
            keep_file_attachments(payload, files)
            resp, err = await _throttled_request(
                client,
                "patch",
                message_url,
                _PROVIDER,
                "update chat message",
                json=payload,
            )
            if err:
                return err
            # Graph answers an edit with 204 No Content: there is no message to read.
            msg = resp.json() if resp is not None and resp.status_code != 204 else {}
            return (
                f"Chat message updated. Message ID: {msg.get('id', message_id)}. "
                f"Last edited: {msg.get('lastModifiedDateTime', '')}."
                + sent_note(sent)
                + card_sent_note(payload)
                + kept_files_note(files, read_err)
            )

    async def delete_microsoft_chat_message(chat_id: str, message_id: str) -> str:
        """Delete a Microsoft Teams chat message (1:1 or group) the signed-in user sent.

        Soft-deletes the message (Graph ``softDelete``): Teams shows it as
        deleted.
        """
        tool = "delete_microsoft_chat_message"
        if err := _validate_teams_chat_id(chat_id, tool):
            return err
        if err := _validate_teams_message_id(message_id, tool):
            return err
        async with _client() as client:
            # softDelete is only exposed under /users/{id}/chats, not /me/chats.
            me_resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me",
                _PROVIDER,
                "resolve current user",
                params={"$select": "id"},
            )
            if err:
                return err
            user_id = (me_resp.json() or {}).get("id", "")
            if not user_id:
                return "Error: Microsoft did not return the signed-in user's ID; nothing was deleted."
            url = (
                f"{_BASE_URL}/users/{_teams_segment(user_id)}/chats/{_teams_segment(chat_id)}"
                f"/messages/{_teams_segment(message_id)}"
            )
            return await _soft_delete_message(ctx, client, url, message_id.strip(), "chat message")

    async def _set_chat_message_reaction(
        tool: str,
        action: str,
        chat_id: str,
        message_id: str,
        reaction: str,
    ) -> str:
        if err := _validate_teams_chat_id(chat_id, tool):
            return err
        if err := _validate_teams_message_id(message_id, tool):
            return err
        reaction_type = teams_reaction_type(reaction)
        if reaction_type is None:
            return teams_reaction_refusal(tool, reaction)
        url = f"{_BASE_URL}/chats/{_teams_segment(chat_id)}/messages/{_teams_segment(message_id)}/{action}"
        verb = "added to" if action == "setReaction" else "removed from"
        async with _client() as client:
            _, err = await _throttled_request(
                client,
                "post",
                url,
                _PROVIDER,
                f"{action} on chat message",
                json={"reactionType": reaction_type},
            )
        if err:
            return err
        return f"Reaction {reaction_type} {verb} chat message {message_id.strip()}."

    async def add_microsoft_chat_message_reaction(chat_id: str, message_id: str, reaction: str) -> str:
        """React to a Microsoft Teams chat message (1:1 or group) as the signed-in user."""
        return await _set_chat_message_reaction(
            "add_microsoft_chat_message_reaction", "setReaction", chat_id, message_id, reaction
        )

    async def remove_microsoft_chat_message_reaction(chat_id: str, message_id: str, reaction: str) -> str:
        """Remove the signed-in user's reaction from a Microsoft Teams chat message (1:1 or group)."""
        return await _set_chat_message_reaction(
            "remove_microsoft_chat_message_reaction", "unsetReaction", chat_id, message_id, reaction
        )

    async def create_microsoft_chat(
        members: str,
        topic: str = "",
        message: str = "",
    ) -> str:
        """Create a new Microsoft Teams chat.

        For a **1:1 chat**: provide one email address in ``members``.
        For a **group chat**: provide multiple comma-separated email
        addresses in ``members``.  Optionally include an initial
        ``message`` and a ``topic`` (group chats only).

        The authenticated user is added automatically.
        """
        member_emails = [m.strip() for m in members.split(",") if m.strip()]
        if not member_emails:
            return "Error: at least one member email is required."

        # Determine chat type: oneOnOne for 1 member, group for 2+
        chat_type = "oneOnOne" if len(member_emails) == 1 else "group"

        async with _client() as client:
            # Resolve current user's ID — the Graph API requires a
            # concrete ``users('{id}')`` entity reference for
            # ``user@odata.bind``; the ``/me`` alias is not accepted.
            me_resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me",
                _PROVIDER,
                "resolve current user",
                params={"$select": "id"},
            )
            if err:
                return err
            my_id = me_resp.json().get("id", "")

            # Build members list with concrete user references
            member_objects: list[dict] = [
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{_BASE_URL}/users('{my_id}')",
                },
            ]
            for email in member_emails:
                member_objects.append(
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"{_BASE_URL}/users('{email}')",
                    }
                )

            payload: dict = {
                "chatType": chat_type,
                "members": member_objects,
            }
            if topic and chat_type == "group":
                payload["topic"] = topic

            resp, err = await _throttled_request(
                client,
                "post",
                f"{_BASE_URL}/chats",
                _PROVIDER,
                "create chat",
                json=payload,
            )
            if err:
                return err
            chat = resp.json()
            chat_id = chat.get("id", "")

            result = f"Chat created. Chat ID: {chat_id}. Type: {chat_type}. Members: {', '.join(member_emails)}."

            # Send initial message if provided
            if message and chat_id:
                _, msg_err = await _throttled_request(
                    client,
                    "post",
                    f"{_BASE_URL}/me/chats/{_teams_segment(chat_id)}/messages",
                    _PROVIDER,
                    "send initial message",
                    json={"body": build_graph_chat_body(message)},
                )
                if not msg_err:
                    result += " Initial message sent."
                else:
                    result += f" (Initial message failed: {msg_err})"

            return result

    executor.register("list_microsoft_chats", list_microsoft_chats)
    executor.register("list_microsoft_chat_messages", list_microsoft_chat_messages)
    executor.register("list_all_microsoft_chat_messages", list_all_microsoft_chat_messages)
    executor.register("get_microsoft_chat_message", get_microsoft_chat_message)
    executor.register("send_microsoft_chat_message", send_microsoft_chat_message)
    executor.register("update_microsoft_chat_message", update_microsoft_chat_message)
    executor.register("delete_microsoft_chat_message", delete_microsoft_chat_message)
    executor.register("add_microsoft_chat_message_reaction", add_microsoft_chat_message_reaction)
    executor.register("remove_microsoft_chat_message_reaction", remove_microsoft_chat_message_reaction)
    executor.register("create_microsoft_chat", create_microsoft_chat)

    return [
        # ── Teams – Chats ──
        ToolDefinition(
            name="list_microsoft_chats",
            description="List the user's recent Microsoft Teams chats (1:1 and group). Returns chat IDs, types, topics, and participants. Use the chat_id to read or send messages.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max chats to return (default 20)",
                        "default": 20,
                    }
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="list_microsoft_chat_messages",
            description=(
                "Read recent messages from a Microsoft Teams chat (1:1 or group). Use list_chats "
                "first to find the chat_id. A message too long for the listing ends with "
                "'[truncated, N more characters: read it in full with get_microsoft_chat_message(...)]', "
                "and that exact call reads it whole; a message without the marker is listed whole."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The chat ID (from list_chats)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max messages to return (default 20, at most 200)",
                        "default": 20,
                    },
                },
                "required": ["chat_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="list_all_microsoft_chat_messages",
            description=(
                "Fetch recent messages from ALL of the user's Microsoft Teams chats in a "
                "single call. Use this instead of calling list_microsoft_chats + "
                "list_microsoft_chat_messages per chat — it lists all chats and "
                "concurrently reads messages from each one, returning everything in one "
                "shot. Ideal for 'monitor all chats' or 'answer questions across chats' "
                "assignments. For a specific known chat use list_microsoft_chat_messages. "
                "A message too long for the listing ends with '[truncated, N more characters: "
                "read it in full with get_microsoft_chat_message(...)]', and that exact call "
                "reads it whole; a message without the marker is listed whole."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max number of chats to include (default 20, max 50)",
                        "default": 20,
                    },
                    "messages_per_chat": {
                        "type": "integer",
                        "description": "Max messages to fetch per chat (default 10, max 50)",
                        "default": 10,
                    },
                    "hours": {
                        "type": "integer",
                        "description": (
                            "When > 0, only include messages from the last N hours. "
                            "When 0 (default), returns the most recent messages_per_chat "
                            "messages regardless of age."
                        ),
                        "default": 0,
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_chat_message",
            description=(
                "Read ONE Microsoft Teams chat message (1:1 or group) in full: its whole text "
                "(cards and quoted messages included), author, times, reactions, attached "
                "files, pasted images and mentions. Use it for a message a chat listing marks "
                "'[truncated, ...]' (make the call the marker names), or for a Teams message "
                "link someone shares: pass it as url and the chat and message are read from it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The chat ID (from list_chats; omit with url)",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to read (omit with url)",
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "A Teams message link (https://teams.microsoft.com/l/message/...), "
                            "instead of the IDs"
                        ),
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="send_microsoft_chat_message",
            description=(
                "Send a message in a Microsoft Teams chat (1:1 or group) as "
                "the signed-in user. Use list_chats first to find the "
                "chat_id, or create_chat to start a new conversation. Write "
                "the body in Markdown - the platform converts it to Teams "
                "HTML. Do not wrap the body in HTML tags. To @mention "
                "people, pass mentions - typing @name in the text notifies "
                "nobody. To change a message already sent, use "
                "update_microsoft_chat_message with the returned message ID "
                "- do not post a follow-up. To post an Adaptive Card, pass card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The chat ID (from list_chats or create_chat)",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "Message body in Markdown. Do NOT wrap in HTML "
                            "tags and do NOT entity-encode tags "
                            "(&lt;p&gt;...&lt;/p&gt;) - both leak as visible "
                            "markup in Teams."
                        ),
                    },
                    "content_type": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "description": (
                            "Advisory only. Markdown is auto-converted and "
                            "HTML is auto-detected regardless of this value. "
                            "Prefer omitting it and sending Markdown."
                        ),
                        "default": "text",
                    },
                    "mentions": mentions_schema(CHAT_MENTION_TYPES),
                    "card": card_schema(),
                },
                "required": ["chat_id"],
            },
            # The same message to the same recipients twice is refused.
            send_guard=SendGuard(recipients=("chat_id",), body=("message", "card")),
            # Its [[FILE:id]] markers are sent as Teams files (_chat_file_markers.py).
            outbound_keeps_file_markers=True,
            read_back=WriteReadBack(
                kind="chat_message",
                id_pattern=r"Message ID: ([^\s.]+)",
                recipients=("chat_id",),
                body=("message",),
                shown_arguments=("message",),
            ),
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="update_microsoft_chat_message",
            description=(
                "Edit a Microsoft Teams chat message (1:1 or group) the "
                "signed-in user already sent. Replaces the body in place - "
                "do not post a follow-up. Use the message_id returned by "
                "send_microsoft_chat_message (or listed by "
                "list_microsoft_chat_messages). Write the new body in "
                "Markdown - the platform converts it to Teams HTML. The edit "
                "sends the whole message: pass card again to keep a card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The chat ID (from list_chats or create_chat)",
                    },
                    "message_id": {
                        "type": "string",
                        "description": (
                            "ID of the message to edit (from send or list)"
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "Replacement body in Markdown. Do NOT wrap in "
                            "HTML tags and do NOT entity-encode tags."
                        ),
                    },
                    "content_type": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "description": (
                            "Advisory only. Markdown is auto-converted and "
                            "HTML is auto-detected regardless of this value. "
                            "Prefer omitting it and sending Markdown."
                        ),
                        "default": "text",
                    },
                    "mentions": mentions_schema(CHAT_MENTION_TYPES),
                    "card": card_schema(),
                },
                "required": ["chat_id", "message_id"],
            },
            # An edit sends the whole message: two edits of one message in a
            # round are one, the last body.
            coalesce=WriteCoalescing(identity=("message_id", "chat_id"), mode="replace"),
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="delete_microsoft_chat_message",
            description=(
                "Delete a Microsoft Teams chat message (1:1 or group) the "
                "signed-in user sent (Teams then shows it as deleted). Use the "
                "message_id returned by send_microsoft_chat_message (or listed "
                "by list_microsoft_chat_messages). Microsoft refuses to delete "
                "another person's message; the refusal is returned as is."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The chat ID (from list_chats or create_chat)",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to delete (from send or list)",
                    },
                },
                "required": ["chat_id", "message_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        *(
            ToolDefinition(
                name=name,
                description=description,
                input_schema={
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "string",
                            "description": "The chat ID (from list_chats or create_chat)",
                        },
                        "message_id": {
                            "type": "string",
                            "description": "ID of the message (from send or list)",
                        },
                        "reaction": {
                            "type": "string",
                            "description": (
                                "One emoji, for example 👍 or 🔁, or a Teams reaction name: like, heart, "
                                "laugh, surprised, sad, angry"
                            ),
                        },
                    },
                    "required": ["chat_id", "message_id", "reaction"],
                },
                source="integration:microsoft",
                priority=1,
            )
            for name, description in (
                (
                    "add_microsoft_chat_message_reaction",
                    "React with one emoji to a Microsoft Teams chat message (1:1 or group), as the "
                    "signed-in user. Any message of a chat the user belongs to can get a reaction, not only "
                    "the user's own; Microsoft's refusal (a chat the user is not in) is returned as is.",
                ),
                (
                    "remove_microsoft_chat_message_reaction",
                    "Remove the signed-in user's emoji reaction from a Microsoft Teams chat message (1:1 or group).",
                ),
            )
        ),
        ToolDefinition(
            name="create_microsoft_chat",
            description="Create a new Microsoft Teams chat. For a 1:1 chat, provide one email address. For a group chat, provide multiple comma-separated emails. Optionally include an initial message and a topic (group chats only). The authenticated user is added automatically.",
            input_schema={
                "type": "object",
                "properties": {
                    "members": {
                        "type": "string",
                        "description": "Comma-separated email addresses of chat members (1 email = 1:1 chat, 2+ = group chat)",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Chat topic (group chats only)",
                        "default": "",
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional initial message to send in the chat",
                        "default": "",
                    },
                },
                "required": ["members"],
            },
            source="integration:microsoft",
            priority=1,
        ),
    ]
