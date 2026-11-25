"""Microsoft Teams channel tools: teams, channels, channel messages and members."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.integrations.http import AutoRefreshClient
from app.services import chat_reply_guard
from app.services.agent_posts import (
    record_teams_channel_post,
)
from app.services.teams_chat_posts import REPLY_GUARD_CHANNEL, thread_conversation_key
from app.tools.base import CallableToolExecutor, SendGuard, ToolDefinition, WriteCoalescing, WriteReadBack

from ._cards import card_note, card_schema, card_sent_note, nothing_to_post, parse_card
from ._common import (
    _BASE_URL,
    _PROVIDER,
    MicrosoftContext,
)
from ._kept_files import file_attachments, keep_file_attachments, kept_files_note
from ._mentions import (
    CHANNEL_MENTION_TYPES,
    build_message,
    mentions_schema,
    parse_mentions,
    posted_note,
    refused_note,
    sent_note,
)
from ._teams import (
    _channel_message_call,
    _channel_message_target,
    _channel_message_url,
    _channel_url,
    _clip_listing_texts,
    _format_message_attachments,
    _format_message_detail,
    _format_message_mentions,
    _looks_empty,
    _soft_delete_message,
    _team_url,
    _teams_message_author,
    _teams_message_text,
    _teams_segment,
    _thread_route,
    _validate_teams_channel_ids,
    _validate_teams_team_id,
    teams_reaction_refusal,
    teams_reaction_type,
)
from ._voice_notes import audio_hosted_contents, heard_hosted_lines, heard_lines, listing_note, with_heard
from .teams_members import CHANNEL_MEMBER_READ_PERMISSION, _permission_hint

logger = logging.getLogger(__name__)


# Concurrency cap for parallel Teams ``/replies`` fetches inside
# ``list_microsoft_channel_messages``.  Five matches the Graph API
# "concurrent request per app" guidance band that keeps us comfortably
# below the throttling threshold for typical workloads, while still
# cutting wall-time for a 10-thread fan-out by ~5x vs. the pre-fix
# sequential pattern.  Module-level (rather than inside ``build_tools``)
# so tests can monkeypatch it for fixture-defined load scenarios.
_CHANNEL_REPLY_FETCH_CONCURRENCY = 5

#: Pages of 50 replies ``get_microsoft_channel_message`` reads to count a
#: post's replies; a longer thread is reported as "more than" what was read.
_THREAD_REPLY_PAGES = 10

#: Newest replies of a post whose IDs ``get_microsoft_channel_message`` names.
_THREAD_REPLY_IDS_SHOWN = 10


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the teams channels tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request

    # ── Teams Tools ─────────────────────────────────────────────────────
    # Session-scoped cache for list_microsoft_teams_and_channels.
    # Within a single assignment run the LLM often calls this tool more
    # than once (e.g. first to discover channel IDs, later when planning
    # which messages to read).  Caching the result avoids re-issuing the
    # entire batch of per-team /channels Graph calls on every invocation,
    # which would otherwise compound 429 Retry-After delays and consume a
    # disproportionate share of the run's time budget.
    # The lock single-flights concurrent duplicate calls (the agent loop
    # executes parallel tool batches with asyncio.gather) so two
    # simultaneous invocations can't both miss the cache and both issue
    # the full N+1 Graph batch.
    # The generation counter makes invalidation win over an in-flight
    # listing: create_microsoft_channel bumps it, and a listing that
    # started before the bump skips the cache write so it cannot
    # resurrect its stale pre-create snapshot for the rest of the session.
    _teams_channels_result: str | None = None
    _teams_channels_lock = asyncio.Lock()
    _teams_channels_generation = 0

    async def list_microsoft_teams_and_channels() -> str:
        """List all Microsoft Teams teams the user belongs to and their channels.

        Returns team names, IDs, and all channels within each team.  Use
        the returned team_id + channel_id to read channel messages.  The
        result is cached for the session (invalidated when a channel is
        created), so repeat calls are free — do NOT re-call this tool
        hoping for fresh data after a partial failure.
        """
        nonlocal _teams_channels_result
        if _teams_channels_result is not None:
            return _teams_channels_result

        async with _teams_channels_lock:
            # Double-checked: a concurrent duplicate call may have
            # populated the cache while we waited for the lock.
            if _teams_channels_result is not None:
                return _teams_channels_result

            # Snapshot the invalidation generation: if a channel is
            # created while this listing is in flight, the bump makes us
            # skip the cache write below so the stale pre-create snapshot
            # is never resurrected.
            generation_at_start = _teams_channels_generation

            async with _client() as client:
                # Step 1: List joined teams
                resp, err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/me/joinedTeams",
                    _PROVIDER,
                    "list teams",
                    params={"$select": "id,displayName,description"},
                )
                if err:
                    # Deliberately NOT cached: a full failure should stay
                    # retryable once the transient condition clears.
                    return err

                teams = resp.json().get("value", [])
                if not teams:
                    # Cache the empty result too — otherwise repeat calls
                    # would keep hitting /me/joinedTeams even though the
                    # tool contract promises cached repeat calls.
                    no_teams = "No Microsoft Teams teams found for this user."
                    if _teams_channels_generation == generation_at_start:
                        _teams_channels_result = no_teams
                    return no_teams

                # Step 2: Fetch channels for all teams CONCURRENTLY.
                # Sequential fetching means a single 429 Retry-After delay
                # (typically 30 s) stalls every remaining team in the list.
                # Running the fetches in parallel (bounded by the shared
                # _api_sem semaphore) reduces wall-time from
                # O(N × retry_delay) to O(ceil(N / _MICROSOFT_CONCURRENCY)
                # × retry_delay) — a meaningful saving when a user belongs
                # to many teams and the Graph API is throttling.
                async def _fetch_team_channels(team: dict) -> list[str]:
                    team_id = team.get("id", "")
                    team_name = team.get("displayName", "Untitled")
                    ch_resp, ch_err = await _throttled_request(
                        client,
                        "get",
                        f"{_team_url(team_id)}/channels",
                        _PROVIDER,
                        "list channels",
                        params={
                            "$select": "id,displayName,description,membershipType"
                        },
                    )
                    team_lines: list[str] = [f"## Team: {team_name} (ID: {team_id})"]
                    if ch_err:
                        team_lines.append("  (Could not list channels)")
                        return team_lines
                    channels = ch_resp.json().get("value", [])
                    if not channels:
                        team_lines.append("  (No channels)")
                        return team_lines
                    for ch in channels:
                        ch_type = ch.get("membershipType", "standard")
                        team_lines.append(
                            f"  - {ch.get('displayName', 'Untitled')} (ID: {ch.get('id', '')}, type: {ch_type})"
                        )
                    team_lines.append("")
                    return team_lines

                fetch_results = await asyncio.gather(
                    *(_fetch_team_channels(team) for team in teams),
                    return_exceptions=True,
                )

                lines: list[str] = []
                for idx, res in enumerate(fetch_results):
                    if isinstance(res, BaseException):
                        if not isinstance(res, Exception):
                            # CancelledError / KeyboardInterrupt / SystemExit
                            # are control-flow signals, not per-team failures —
                            # re-raise so cancellation propagates instead of
                            # being recorded as a placeholder row.
                            raise res
                        team_id = teams[idx].get("id", "?")
                        team_name = teams[idx].get("displayName", "Untitled")
                        logger.warning(
                            "Could not list channels for team %s (%s): %r",
                            team_name,
                            team_id,
                            res,
                        )
                        lines.append(f"## Team: {team_name} (ID: {team_id})")
                        lines.append("  (Could not list channels)")
                        lines.append("")
                    else:
                        lines.extend(res)

                listing = "\n".join(lines)
                # Skip the cache write if an invalidation (channel
                # creation) happened while this listing was in flight —
                # the snapshot no longer reflects the true channel set.
                if _teams_channels_generation == generation_at_start:
                    _teams_channels_result = listing
                return listing

    async def list_microsoft_channel_messages(
        team_id: str,
        channel_id: str,
        hours: int = 24,
        count: int = 50,
        max_reply_threads: int = 10,
        max_replies_per_thread: int = 20,
    ) -> str:
        """Read recent messages from a Microsoft Teams channel,
        including threaded replies.

        ``count`` controls the number of ROOT (top-level) messages
        returned — replies are tracked separately and do NOT consume
        the root budget.  This preserves the pre-PR contract for
        existing callers that pass ``count=50`` expecting up to 50
        root messages back.

        Replies are fetched for at most ``max_reply_threads`` of the
        most-recent root messages (default 10) — this is the N+1
        guard.  Without a cap, a channel with 50 active threads would
        trigger 50 sequential ``GET .../replies`` calls and frequently
        hit Graph API throttling.

        Per-thread reply count is capped at ``max_replies_per_thread``
        (default 20) so a single hot thread cannot dominate the
        output.

        Reply fetches run concurrently inside a small semaphore
        (``_CHANNEL_REPLY_FETCH_CONCURRENCY``) so a 10-thread fan-out
        completes in ~2-3 round-trips instead of 10 sequential ones,
        without overwhelming the Graph API.
        """
        # Fail fast if team_id / channel_id are placeholder strings that were
        # never resolved to real Microsoft Teams identifiers (the team ID is
        # a GUID, the channel ID looks like "19:...@thread.tacv2"). Sending
        # placeholders (e.g. "na", "n/a") produces a Graph HTTP 400 BadRequest
        # and wastes the assignment's tool budget —
        if err := _validate_teams_channel_ids(team_id, channel_id, "list_microsoft_channel_messages"):
            return err

        # Defensive bounds on caller-provided knobs so an LLM passing
        # an absurd value can't either hang the worker (huge fan-out)
        # or silently disable threads (zero / negative).
        count = max(1, min(count, 200))
        max_reply_threads = max(0, min(max_reply_threads, 50))
        max_replies_per_thread = max(0, min(max_replies_per_thread, 50))

        since = datetime.now(UTC) - timedelta(hours=hours)
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        async with _client() as client:
            root_messages: list[dict] = []
            messages_url = f"{_channel_url(team_id, channel_id)}/messages"
            url: str | None = messages_url
            # Note: $orderby is not supported on the Teams channel messages
            # endpoint — sort client-side instead.
            params: dict | None = {
                "$top": str(min(count, 50)),
            }

            async def _fetch_thread_replies(root_message_id: str) -> list[dict]:
                """Fetch up to ``max_replies_per_thread`` replies for one
                thread.  Returns reply records in
                ``{message, thread_root_id, is_reply=True}`` shape.

                On error the helper logs a WARNING and returns an empty
                list — partial reply coverage is better than failing
                the entire read because one thread had an auth glitch
                or hit a transient 5xx.
                """
                if max_replies_per_thread == 0:
                    return []
                gathered: list[dict] = []
                replies_url: str | None = (
                    f"{messages_url}/{_teams_segment(root_message_id)}/replies"
                )
                replies_params: dict | None = {
                    "$top": str(min(max_replies_per_thread, 50))
                }
                while replies_url and len(gathered) < max_replies_per_thread:
                    reply_kw: dict = {}
                    if replies_params:
                        reply_kw["params"] = replies_params
                    replies_resp, replies_err = await _throttled_request(
                        client,
                        "get",
                        replies_url,
                        _PROVIDER,
                        "read channel message replies",
                        **reply_kw,
                    )
                    if replies_err:
                        logger.warning(
                            "Could not read replies for channel message %s: %s",
                            root_message_id,
                            replies_err,
                        )
                        break

                    replies_data = replies_resp.json()
                    replies = replies_data.get("value", [])
                    found_older_reply = False
                    for reply in replies:
                        created = reply.get("createdDateTime", "")
                        if created and created >= since_iso:
                            gathered.append(
                                {
                                    "message": reply,
                                    "thread_root_id": root_message_id,
                                    "is_reply": True,
                                }
                            )
                            if len(gathered) >= max_replies_per_thread:
                                break
                        elif created:
                            # Replies returned newest-first; once we
                            # see one older than the window we can
                            # stop paging this thread.
                            found_older_reply = True

                    if found_older_reply or len(gathered) >= max_replies_per_thread:
                        break

                    replies_url = replies_data.get("@odata.nextLink")
                    replies_params = None
                return gathered

            # --- Step 1: collect ROOT messages within the window ---
            while url and len(root_messages) < count:
                kw: dict = {}
                if params:
                    kw["params"] = params
                resp, err = await _throttled_request(
                    client,
                    "get",
                    url,
                    _PROVIDER,
                    "read channel messages",
                    **kw,
                )
                if err:
                    return err

                data = resp.json()
                page_messages = data.get("value", [])

                # Filter messages within the time window
                found_older = False
                for msg in page_messages:
                    created = msg.get("createdDateTime", "")
                    if created and created >= since_iso:
                        root_messages.append(
                            {
                                "message": msg,
                                "thread_root_id": msg.get("id", ""),
                                "is_reply": False,
                            }
                        )
                        if len(root_messages) >= count:
                            break
                    elif created:
                        found_older = True

                # The API returns newest-first by default; if we saw
                # messages older than the window we can stop paging.
                if found_older:
                    break

                url = data.get("@odata.nextLink")
                params = None

            # --- Step 2: fetch replies CONCURRENTLY for the first
            # ``max_reply_threads`` root messages (newest-first, since
            # the root list is in the API's newest-first order). ---
            reply_messages: list[dict] = []
            if max_reply_threads > 0 and max_replies_per_thread > 0:
                eligible_roots = [
                    r for r in root_messages if r.get("thread_root_id")
                ][:max_reply_threads]
                if eligible_roots:
                    sem = asyncio.Semaphore(_CHANNEL_REPLY_FETCH_CONCURRENCY)

                    async def _bounded(root_id: str) -> list[dict]:
                        async with sem:
                            return await _fetch_thread_replies(root_id)

                    # ``return_exceptions=True`` so a single failed
                    # thread cannot abort the gather — the per-call
                    # exception path already logs a WARNING and
                    # returns ``[]``, but we add a defensive
                    # safety-net here in case a NEW exception type
                    # ever leaks out of ``safe_request``.
                    fetch_results = await asyncio.gather(
                        *(
                            _bounded(r["thread_root_id"])
                            for r in eligible_roots
                        ),
                        return_exceptions=True,
                    )
                    for res in fetch_results:
                        if isinstance(res, BaseException):
                            if not isinstance(res, Exception):
                                # CancelledError / KeyboardInterrupt /
                                # SystemExit must propagate, not be
                                # downgraded to a skipped thread.
                                raise res
                            logger.warning(
                                "Reply fetch task raised unexpectedly: %r", res
                            )
                            continue
                        reply_messages.extend(res)

            all_messages = root_messages + reply_messages

            if not all_messages:
                return f"No messages found in channel in the last {hours} hour(s)."

            # Sort chronologically (oldest first) — replies are
            # interleaved with their nearby root messages by timestamp,
            # which mirrors how a user reads the channel.
            all_messages.sort(
                key=lambda item: (item.get("message") or {}).get(
                    "createdDateTime", "1970-01-01T00:00:00Z"
                )
            )

            # Each text whole when the listing has room for it, else cut and
            # naming the exact call that reads that message in full.
            texts = _clip_listing_texts(
                [
                    (
                        _teams_message_text(item.get("message") or {}),
                        _channel_message_call(
                            team_id.strip(),
                            channel_id.strip(),
                            str((item.get("message") or {}).get("id") or ""),
                            item.get("thread_root_id", "") if item.get("is_reply") else "",
                        ),
                    )
                    for item in all_messages
                ]
            )
            lines: list[str] = []
            for item, text in zip(all_messages, texts, strict=True):
                msg = item.get("message") or {}
                # Who posted it (a person, an app or workflow, a device, or a
                # system event) and its text from the body AND its cards: a
                # workflow's card has an empty body.
                author_type, sender_name = _teams_message_author(msg)
                message_type = msg.get("messageType") or "message"
                kind = author_type if message_type == "message" else f"{author_type}, {message_type}"
                prefix = (
                    f"[reply to {item.get('thread_root_id', '')}] "
                    if item.get("is_reply")
                    else ""
                )
                lines.append(
                    f"{prefix}[{msg.get('createdDateTime', '')}] {sender_name} ({kind}):\n"
                    f"{text}"
                    f"{_format_message_attachments(msg)}"
                    f"{listing_note(msg, 'get_microsoft_channel_message', text)}"
                    f"{_format_message_mentions(msg)}\n"
                    f"(Message ID: {msg.get('id', '')})\n"
                )

            return (
                f"Found {len(root_messages)} root message(s) "
                f"and {len(reply_messages)} reply(ies) in the last {hours}h:\n\n"
                + "\n---\n".join(lines)
            )

    async def _thread_replies(client: AutoRefreshClient, message_url: str) -> tuple[list[dict], bool, str | None]:
        """A post's replies, newest first: ``(replies, more, error)``.

        At most ``_THREAD_REPLY_PAGES`` pages are read; ``more`` says the
        thread holds more replies than that.
        """
        replies: list[dict] = []
        url: str | None = f"{message_url}/replies"
        params: dict | None = {"$top": "50"}
        for _ in range(_THREAD_REPLY_PAGES):
            kw: dict = {"params": params} if params else {}
            resp, err = await _throttled_request(client, "get", url, _PROVIDER, "read channel message replies", **kw)
            if err:
                return [], False, err
            data = resp.json()
            replies.extend(reply for reply in data.get("value", []) if isinstance(reply, dict))
            url = data.get("@odata.nextLink")
            params = None
            if not url:
                return replies, False, None
        return replies, True, None

    async def get_microsoft_channel_message(
        team_id: str = "",
        channel_id: str = "",
        message_id: str = "",
        parent_message_id: str = "",
        url: str = "",
    ) -> str:
        """Read one Microsoft Teams channel post or thread reply in full.

        Addressed by its IDs or by its Teams link (``url``), whose ``groupId``
        and ``parentMessageId`` name the team and the thread. Nothing is cut:
        the whole text, what a listing appends to it, its reactions and, for
        a post, its replies. A voice note in the message comes back
        transcribed, as ``[voice note, 0:20, transcribed] ...``, or with why
        it could not be heard.
        """
        tool = "get_microsoft_channel_message"
        target, err = _channel_message_target(team_id, channel_id, message_id, parent_message_id, url, tool)
        if err:
            return err
        team, channel, mid, parent = (
            target["team_id"],
            target["channel_id"],
            target["message_id"],
            target["parent_message_id"],
        )
        message_url, label, err = _channel_message_url(team, channel, mid, parent, tool)
        if err:
            return err
        async with _client() as client:
            resp, err = await _throttled_request(client, "get", message_url, _PROVIDER, f"read {label}")
            if err:
                return err
            msg = resp.json()
            heard = await heard_lines(ctx, client, msg)
            if _looks_empty(msg):
                # A voice clip recorded in Teams is a hosted content the body
                # does not show: one more read, for an empty-looking message only.
                hosted_resp, hosted_err = await _throttled_request(
                    client, "get", f"{message_url}/hostedContents", _PROVIDER, "list the message's hosted contents"
                )
                if not hosted_err and hosted_resp is not None:
                    clips, _rest = audio_hosted_contents((hosted_resp.json() or {}).get("value") or [])
                    heard += await heard_hosted_lines(ctx, client, message_url, clips, already=len(heard))
            if parent:
                heading = f"Teams channel reply {mid} in the thread of {parent} (team_id: {team}, channel_id: {channel})"
                return with_heard(_format_message_detail(msg, heading), heard)
            replies, more, replies_err = await _thread_replies(client, message_url)
        heading = f"Teams channel post {mid} (team_id: {team}, channel_id: {channel})"
        if replies_err:
            return with_heard(_format_message_detail(msg, heading, [f"Replies: not read ({replies_err})"]), heard)
        count = f"more than {len(replies)}" if more else str(len(replies))
        suffix = ""
        if replies:
            shown = ", ".join(
                f"{reply.get('id', '')} ({_teams_message_author(reply)[1]}, {reply.get('createdDateTime', '')})"
                for reply in replies[:_THREAD_REPLY_IDS_SHOWN]
            )
            if len(replies) > _THREAD_REPLY_IDS_SHOWN:
                shown += ", ..."
            suffix = (
                f"\n[replies, newest first: {shown} - read one in full with "
                f"{_channel_message_call(team, channel, 'REPLY_ID', mid)}, REPLY_ID being one of these IDs]"
            )
        return with_heard(_format_message_detail(msg, heading, [f"Replies: {count}"], suffix), heard)

    # ── Teams Channel Write Tools ──────────────────────────────────────

    async def send_microsoft_channel_message(
        team_id: str,
        channel_id: str,
        message: str = "",
        content_type: str = "text",
        mentions: list | str | None = None,
        card: dict | str | None = None,
    ) -> str:
        """Send a message to a Microsoft Teams channel.

        Posts a new message in the specified channel.  Use
        ``list_microsoft_teams_and_channels`` first to find the ``team_id``
        and ``channel_id``.  Supports plain text (default) or HTML content,
        ``mentions`` of people, the channel, the team or a team tag, and an
        Adaptive Card (``card``) below the text.
        """
        if err := _validate_teams_channel_ids(team_id, channel_id, "send_microsoft_channel_message"):
            return err
        requests, err = parse_mentions(mentions, CHANNEL_MENTION_TYPES)
        if err:
            return err
        adaptive_card, err = parse_card(card, full_width=True)
        if err:
            return err
        if err := nothing_to_post(message, adaptive_card, requests):
            return err
        async with _client() as client:
            payload, sent, err = await build_message(
                ctx,
                client,
                message,
                content_type,
                requests,
                team_id=team_id,
                channel_id=channel_id,
                card=adaptive_card,
            )
            if err:
                return err
            resp, err = await _throttled_request(
                client,
                "post",
                f"{_channel_url(team_id, channel_id)}/messages",
                _PROVIDER,
                "send channel message",
                json=payload,
            )
            if err:
                await ctx.log_account_send(
                    "microsoft_teams",
                    "send_microsoft_channel_message",
                    conversation_id=channel_id,
                    conversation_type="channel",
                    text=message,
                    error=err,
                )
                return err + refused_note(sent)
            msg = resp.json()
            await record_teams_channel_post(team_id, channel_id, msg)
            sent_id = str(msg.get("id") or "") if isinstance(msg, dict) else ""
            await ctx.log_account_send(
                "microsoft_teams",
                "send_microsoft_channel_message",
                conversation_id=channel_id,
                conversation_type="channel",
                text=message,
                message_id=sent_id or None,
                thread_route=_thread_route(team_id, channel_id, sent_id),
            )
            return (
                f"Message sent to channel. Message ID: {msg.get('id', '')}. "
                f"Sent at: {msg.get('createdDateTime', '')}."
                + posted_note(sent, msg, "list_microsoft_channel_messages")
                + card_note(payload, msg, "list_microsoft_channel_messages")
            )

    async def update_microsoft_channel_message(
        team_id: str,
        channel_id: str,
        message_id: str,
        message: str = "",
        content_type: str = "text",
        parent_message_id: str = "",
        mentions: list | str | None = None,
        card: dict | str | None = None,
    ) -> str:
        """Edit a previously sent Microsoft Teams channel message.

        Sends the whole message again (its body, and its card when ``card``
        is given) for a message the signed-in user sent. A file the message
        carries (sent by ``send_microsoft_channel_file``) is kept on it. Use
        the ``message_id`` returned by ``send_microsoft_channel_message`` (or
        listed by ``list_microsoft_channel_messages``). For a thread
        *reply*, also pass ``parent_message_id`` (the root message ID).
        """
        url, _, err = _channel_message_url(
            team_id, channel_id, message_id, parent_message_id, "update_microsoft_channel_message"
        )
        if err:
            return err
        requests, err = parse_mentions(mentions, CHANNEL_MENTION_TYPES)
        if err:
            return err
        adaptive_card, err = parse_card(card, full_width=True)
        if err:
            return err
        if err := nothing_to_post(message, adaptive_card, requests):
            return err
        async with _client() as client:
            payload, sent, err = await build_message(
                ctx,
                client,
                message,
                content_type,
                requests,
                team_id=team_id,
                channel_id=channel_id,
                card=adaptive_card,
            )
            if err:
                return err
            # Graph replaces the message whole: its file cards are sent back.
            current, read_err = await _throttled_request(client, "get", url, _PROVIDER, "read channel message")
            files = file_attachments(current.json() if current is not None and not read_err else None)
            keep_file_attachments(payload, files)
            resp, err = await _throttled_request(
                client,
                "patch",
                url,
                _PROVIDER,
                "update channel message",
                json=payload,
            )
            if err:
                return err + refused_note(sent)
            # Graph answers an edit with 204 No Content: there is no message to read.
            msg = resp.json() if resp is not None and resp.status_code != 204 else {}
            return (
                f"Channel message updated. Message ID: {msg.get('id', message_id)}. "
                f"Last edited: {msg.get('lastModifiedDateTime', '')}."
                + sent_note(sent)
                + card_sent_note(payload)
                + kept_files_note(files, read_err)
            )

    async def delete_microsoft_channel_message(
        team_id: str,
        channel_id: str,
        message_id: str,
        parent_message_id: str = "",
    ) -> str:
        """Delete a Microsoft Teams channel message the signed-in user sent.

        Soft-deletes the message (Graph ``softDelete``): Teams shows it as
        deleted. For a thread *reply*, also pass ``parent_message_id`` (the
        root message ID).
        """
        url, label, err = _channel_message_url(
            team_id, channel_id, message_id, parent_message_id, "delete_microsoft_channel_message"
        )
        if err:
            return err
        async with _client() as client:
            return await _soft_delete_message(ctx, client, url, message_id.strip(), label)

    async def _set_channel_message_reaction(
        tool: str,
        action: str,
        team_id: str,
        channel_id: str,
        message_id: str,
        reaction: str,
        parent_message_id: str,
    ) -> str:
        url, label, err = _channel_message_url(team_id, channel_id, message_id, parent_message_id, tool)
        if err:
            return err
        reaction_type = teams_reaction_type(reaction)
        if reaction_type is None:
            return teams_reaction_refusal(tool, reaction)
        verb = "added to" if action == "setReaction" else "removed from"
        async with _client() as client:
            _, err = await _throttled_request(
                client,
                "post",
                f"{url}/{action}",
                _PROVIDER,
                f"{action} on {label}",
                json={"reactionType": reaction_type},
            )
        if err:
            return err
        return f"Reaction {reaction_type} {verb} {label} {message_id.strip()}."

    async def add_microsoft_channel_message_reaction(
        team_id: str,
        channel_id: str,
        message_id: str,
        reaction: str,
        parent_message_id: str = "",
    ) -> str:
        """React to a Microsoft Teams channel message or thread reply as the signed-in user."""
        return await _set_channel_message_reaction(
            "add_microsoft_channel_message_reaction",
            "setReaction",
            team_id,
            channel_id,
            message_id,
            reaction,
            parent_message_id,
        )

    async def remove_microsoft_channel_message_reaction(
        team_id: str,
        channel_id: str,
        message_id: str,
        reaction: str,
        parent_message_id: str = "",
    ) -> str:
        """Remove the signed-in user's reaction from a Microsoft Teams channel message or thread reply."""
        return await _set_channel_message_reaction(
            "remove_microsoft_channel_message_reaction",
            "unsetReaction",
            team_id,
            channel_id,
            message_id,
            reaction,
            parent_message_id,
        )

    async def reply_microsoft_channel_message(
        team_id: str,
        channel_id: str,
        message_id: str,
        message: str = "",
        content_type: str = "text",
        mentions: list | str | None = None,
        card: dict | str | None = None,
    ) -> str:
        """Reply to a specific message in a Microsoft Teams channel.

        Posts a reply in the thread of the specified message.  Use
        ``list_microsoft_channel_messages`` to find the ``message_id``.
        """
        root_url, _, err = _channel_message_url(team_id, channel_id, message_id, "", "reply_microsoft_channel_message")
        if err:
            return err
        requests, err = parse_mentions(mentions, CHANNEL_MENTION_TYPES)
        if err:
            return err
        adaptive_card, err = parse_card(card, full_width=True)
        if err:
            return err
        if err := nothing_to_post(message, adaptive_card, requests):
            return err
        # One reply per inbound message: a run answering a thread reply
        # another run already answered is refused here, logged and counted.
        guard = await chat_reply_guard.claim(
            REPLY_GUARD_CHANNEL, thread_conversation_key(team_id.strip(), channel_id.strip(), message_id.strip())
        )
        if guard.refused:
            return guard.refusal(edit_tool="update_microsoft_channel_message")
        async with _client() as client:
            payload, sent, err = await build_message(
                ctx,
                client,
                message,
                content_type,
                requests,
                team_id=team_id,
                channel_id=channel_id,
                card=adaptive_card,
            )
            if err:
                await guard.release()
                return err
            resp, err = await _throttled_request(
                client,
                "post",
                f"{root_url}/replies",
                _PROVIDER,
                "reply to channel message",
                json=payload,
            )
            if err:
                await guard.release()
                await ctx.log_account_send(
                    "microsoft_teams",
                    "reply_microsoft_channel_message",
                    conversation_id=channel_id,
                    conversation_type="channel",
                    text=message,
                    error=err,
                )
                return err + refused_note(sent)
            reply = resp.json()
            await guard.confirm()
            await ctx.log_account_send(
                "microsoft_teams",
                "reply_microsoft_channel_message",
                conversation_id=channel_id,
                conversation_type="channel",
                text=message,
                message_id=reply.get("id") if isinstance(reply, dict) else None,
                thread_route=_thread_route(team_id, channel_id, message_id),
            )
            return (
                f"Reply sent in channel thread. Reply ID: {reply.get('id', '')}. "
                f"Sent at: {reply.get('createdDateTime', '')}."
                + posted_note(sent, reply, "list_microsoft_channel_messages")
                + card_note(payload, reply, "list_microsoft_channel_messages")
                + chat_reply_guard.answered_note(guard)
            )

    async def create_microsoft_channel(
        team_id: str,
        display_name: str,
        description: str = "",
        membership_type: str = "standard",
    ) -> str:
        """Create a new channel in a Microsoft Teams team.

        ``membership_type`` can be ``standard`` (visible to everyone),
        ``private`` (invite-only), or ``shared`` (cross-team).
        """
        if err := _validate_teams_team_id(team_id, "create_microsoft_channel"):
            return err
        nonlocal _teams_channels_result, _teams_channels_generation
        async with _client() as client:
            payload: dict = {
                "displayName": display_name,
                "membershipType": membership_type,
            }
            if description:
                payload["description"] = description
            resp, err = await _throttled_request(
                client,
                "post",
                f"{_team_url(team_id)}/channels",
                _PROVIDER,
                "create channel",
                json=payload,
            )
            if err:
                return err
            ch = resp.json()
            # The channel list changed — drop the session cache so the next
            # list_microsoft_teams_and_channels call reflects the new
            # channel, and bump the generation so an in-flight listing
            # (started before this create) skips its cache write instead
            # of resurrecting the stale pre-create snapshot.
            _teams_channels_result = None
            _teams_channels_generation += 1
            return (
                f"Channel created: '{ch.get('displayName', '')}' "
                f"(ID: {ch.get('id', '')}, type: {ch.get('membershipType', '')})."
            )

    async def list_microsoft_channel_members(team_id: str, channel_id: str) -> str:
        """List members of a Microsoft Teams channel."""
        if err := _validate_teams_channel_ids(team_id, channel_id, "list_microsoft_channel_members"):
            return err
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_channel_url(team_id, channel_id)}/members",
                _PROVIDER,
                "list channel members",
            )
            if err:
                # A private or shared channel's roster needs its own delegated
                # permission (#2188): name it on a denial, as the member writes do.
                return _permission_hint(err, CHANNEL_MEMBER_READ_PERMISSION)
            members = resp.json().get("value", [])
            if not members:
                return "No members found in this channel."
            lines: list[str] = []
            for m in members:
                roles = ", ".join(m.get("roles", [])) or "member"
                lines.append(
                    f"{m.get('displayName', 'Unknown')} <{m.get('email', 'N/A')}> — {roles}"
                )
            return f"Channel has {len(members)} member(s):\n" + "\n".join(lines)

    executor.register("list_microsoft_teams_and_channels", list_microsoft_teams_and_channels)
    executor.register("list_microsoft_channel_messages", list_microsoft_channel_messages)
    executor.register("get_microsoft_channel_message", get_microsoft_channel_message)
    executor.register("send_microsoft_channel_message", send_microsoft_channel_message)
    executor.register("update_microsoft_channel_message", update_microsoft_channel_message)
    executor.register("delete_microsoft_channel_message", delete_microsoft_channel_message)
    executor.register("add_microsoft_channel_message_reaction", add_microsoft_channel_message_reaction)
    executor.register("remove_microsoft_channel_message_reaction", remove_microsoft_channel_message_reaction)
    executor.register("reply_microsoft_channel_message", reply_microsoft_channel_message)
    executor.register("create_microsoft_channel", create_microsoft_channel)
    executor.register("list_microsoft_channel_members", list_microsoft_channel_members)

    return [
        # ── Teams – Channels ──
        ToolDefinition(
            name="list_microsoft_teams_and_channels",
            description="List all Microsoft Teams teams the user belongs to and their channels. Returns team names, IDs, and channels (with IDs and membership type). Use the team_id and channel_id to read/send channel messages. The result is cached for the session and refreshed automatically after you create a channel — never re-call it to 'refresh' after a partial failure.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="list_microsoft_channel_messages",
            description=(
                "Read recent messages from a Microsoft Teams channel, "
                "including threaded replies, each with its ID and the "
                "mentions it holds. ``count`` controls the "
                "number of ROOT messages returned (replies are tracked "
                "separately and do NOT consume the root budget). "
                "Replies are fetched for at most ``max_reply_threads`` "
                "of the most-recent root messages — increase only when "
                "you specifically need wider thread coverage, since "
                "every additional thread costs one extra Graph API "
                "request (parallelised but still subject to "
                "throttling). Use list_microsoft_teams_and_channels "
                "first to find the team_id and channel_id. A message "
                "too long for the listing ends with '[truncated, N more "
                "characters: read it in full with "
                "get_microsoft_channel_message(...)]', and that exact "
                "call reads it whole; a message without the marker is "
                "listed whole."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {
                        "type": "string",
                        "description": "The Microsoft Teams team ID (from list_microsoft_teams_and_channels)",
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "The channel ID within the team (from list_microsoft_teams_and_channels)",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Look back N hours (default 24)",
                        "default": 24,
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "Max ROOT (top-level) messages to return "
                            "(default 50, hard-capped at 200). "
                            "Replies are counted separately and do not "
                            "consume this budget."
                        ),
                        "default": 50,
                    },
                    "max_reply_threads": {
                        "type": "integer",
                        "description": (
                            "Fetch replies for at most this many of "
                            "the most-recent root messages (default "
                            "10, hard-capped at 50). Set to 0 to "
                            "disable reply fetching entirely (root "
                            "messages only)."
                        ),
                        "default": 10,
                    },
                    "max_replies_per_thread": {
                        "type": "integer",
                        "description": (
                            "Maximum replies to fetch from each "
                            "selected thread (default 20, hard-capped "
                            "at 50). Prevents one hot thread from "
                            "dominating the output."
                        ),
                        "default": 20,
                    },
                },
                "required": ["team_id", "channel_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_channel_message",
            description=(
                "Read ONE Microsoft Teams channel post or thread reply in full: its whole "
                "text (cards, quoted messages and system events included), author, times, "
                "reactions, attached files, pasted images, mentions and link, and for a post "
                "its reply count and the IDs of its newest replies. Use it for a message "
                "list_microsoft_channel_messages marks '[truncated, ...]' (make the call the "
                "marker names), or for a Teams message link someone shares: pass it as url "
                "and the team, channel, message and thread are read from it. Without a "
                "link, a thread reply needs parent_message_id (the post's message ID)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {
                        "type": "string",
                        "description": "The Microsoft Teams team ID (omit with url)",
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "The channel ID within the team (omit with url)",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "ID of the post or reply to read (omit with url)",
                    },
                    "parent_message_id": {
                        "type": "string",
                        "description": (
                            "The post's message ID when reading a thread reply. "
                            "Omit for a post, and with url."
                        ),
                        "default": "",
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
            name="send_microsoft_channel_message",
            description=(
                "Send a message to a Microsoft Teams channel as the signed-in "
                "user. Use list_microsoft_teams_and_channels first to find "
                "the team_id and channel_id. Write the body in Markdown "
                "(**bold**, lists, headings, links) - the platform converts "
                "it to Teams HTML. Do not wrap the body in HTML tags. To "
                "@mention people, the channel, the team or a tag, pass "
                "mentions - typing @name in the text notifies nobody. To "
                "change a message already sent, use "
                "update_microsoft_channel_message with the returned message "
                "ID - do not post a follow-up. To post a file (a PDF, a "
                "deck) with the message, use send_microsoft_channel_file. To "
                "post an Adaptive Card, pass card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {
                        "type": "string",
                        "description": "The Microsoft Teams team ID",
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "The channel ID within the team",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "Message body in Markdown. Do NOT wrap in HTML "
                            "tags and do NOT entity-encode tags "
                            "(&lt;p&gt;...&lt;/p&gt;) - both leak as visible "
                            "markup in Teams. The platform converts Markdown "
                            "to Teams HTML."
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
                    "mentions": mentions_schema(CHANNEL_MENTION_TYPES),
                    "card": card_schema(),
                },
                "required": ["team_id", "channel_id"],
            },
            # The same message to the same recipients twice is refused.
            send_guard=SendGuard(recipients=("team_id", "channel_id"), body=("message", "card")),
            read_back=WriteReadBack(
                kind="chat_message",
                id_pattern=r"Message ID: ([^\s.]+)",
                recipients=("team_id", "channel_id"),
                body=("message",),
                shown_arguments=("message",),
            ),
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="update_microsoft_channel_message",
            description=(
                "Edit a Microsoft Teams channel message the signed-in user "
                "already sent. Replaces the body in place - do not post a "
                "follow-up. Use the message_id returned by "
                "send_microsoft_channel_message (or listed by "
                "list_microsoft_channel_messages). For a thread reply, also "
                "pass parent_message_id (the root message ID). Write the new "
                "body in Markdown - the platform converts it to Teams HTML. "
                "The edit sends the whole message: pass card again to keep a "
                "card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {
                        "type": "string",
                        "description": "The Microsoft Teams team ID",
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "The channel ID within the team",
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
                    "parent_message_id": {
                        "type": "string",
                        "description": (
                            "Root message ID when editing a thread reply. "
                            "Omit for a top-level channel post."
                        ),
                        "default": "",
                    },
                    "mentions": mentions_schema(CHANNEL_MENTION_TYPES),
                    "card": card_schema(),
                },
                "required": ["team_id", "channel_id", "message_id"],
            },
            # An edit sends the whole message: two edits of one message in a
            # round are one, the last body.
            coalesce=WriteCoalescing(
                identity=("message_id", "team_id", "channel_id", "parent_message_id"),
                mode="replace",
            ),
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="delete_microsoft_channel_message",
            description=(
                "Delete a Microsoft Teams channel message or thread reply the "
                "signed-in user sent (Teams then shows it as deleted). Use the "
                "message_id returned by send_microsoft_channel_message or "
                "reply_microsoft_channel_message (or listed by "
                "list_microsoft_channel_messages). For a thread reply, also "
                "pass parent_message_id (the root message ID). Microsoft "
                "refuses to delete another person's message; the refusal is "
                "returned as is."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {
                        "type": "string",
                        "description": "The Microsoft Teams team ID",
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "The channel ID within the team",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message or reply to delete (from send, reply or list)",
                    },
                    "parent_message_id": {
                        "type": "string",
                        "description": (
                            "Root message ID when deleting a thread reply. "
                            "Omit for a top-level channel post."
                        ),
                        "default": "",
                    },
                },
                "required": ["team_id", "channel_id", "message_id"],
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
                        "team_id": {
                            "type": "string",
                            "description": "The Microsoft Teams team ID",
                        },
                        "channel_id": {
                            "type": "string",
                            "description": "The channel ID within the team",
                        },
                        "message_id": {
                            "type": "string",
                            "description": "ID of the message or reply (from send, reply or list)",
                        },
                        "reaction": {
                            "type": "string",
                            "description": (
                                "One emoji, for example 👍 or 🔁, or a Teams reaction name: like, heart, "
                                "laugh, surprised, sad, angry"
                            ),
                        },
                        "parent_message_id": {
                            "type": "string",
                            "description": "Root message ID for a thread reply. Omit for a top-level channel post.",
                            "default": "",
                        },
                    },
                    "required": ["team_id", "channel_id", "message_id", "reaction"],
                },
                source="integration:microsoft",
                priority=1,
            )
            for name, description in (
                (
                    "add_microsoft_channel_message_reaction",
                    "React with one emoji to a Microsoft Teams channel message or thread reply, as the "
                    "signed-in user. Any message of the channel can get a reaction, not only the user's own. "
                    "For a thread reply, also pass parent_message_id (the root message ID).",
                ),
                (
                    "remove_microsoft_channel_message_reaction",
                    "Remove the signed-in user's emoji reaction from a Microsoft Teams channel message or "
                    "thread reply. For a thread reply, also pass parent_message_id (the root message ID).",
                ),
            )
        ),
        ToolDefinition(
            name="reply_microsoft_channel_message",
            description=(
                "Reply to a specific message in a Microsoft Teams channel "
                "thread as the signed-in user. Use "
                "list_microsoft_channel_messages to find the message_id. "
                "Write the body in Markdown - the platform converts it to "
                "Teams HTML. Do not wrap the body in HTML tags. To @mention, "
                "pass mentions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {
                        "type": "string",
                        "description": "The Microsoft Teams team ID",
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "The channel ID within the team",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "The ID of the message to reply to (from list_microsoft_channel_messages)",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "Reply body in Markdown. Do NOT wrap in HTML "
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
                    "mentions": mentions_schema(CHANNEL_MENTION_TYPES),
                    "card": card_schema(),
                },
                "required": ["team_id", "channel_id", "message_id"],
            },
            source="integration:microsoft",
            priority=1,
            # Artifact markers in the message are sent as links (outbound_markers).
            outbound_text=("message",),
        ),
        ToolDefinition(
            name="create_microsoft_channel",
            description="Create a new channel in a Microsoft Teams team. Supports standard (visible to all), private (invite-only), and shared (cross-team) channel types.",
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {
                        "type": "string",
                        "description": "The Microsoft Teams team ID",
                    },
                    "display_name": {"type": "string", "description": "Channel name"},
                    "description": {
                        "type": "string",
                        "description": "Channel description",
                        "default": "",
                    },
                    "membership_type": {
                        "type": "string",
                        "enum": ["standard", "private", "shared"],
                        "description": "Channel type (default 'standard')",
                        "default": "standard",
                    },
                },
                "required": ["team_id", "display_name"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="list_microsoft_channel_members",
            description="List members of a Microsoft Teams channel with their roles.",
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {
                        "type": "string",
                        "description": "The Microsoft Teams team ID",
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "The channel ID within the team",
                    },
                },
                "required": ["team_id", "channel_id"],
            },
            source="integration:microsoft",
            priority=2,
        ),
    ]
