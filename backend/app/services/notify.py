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

"""Notification delivery service – sends assignment results to the user's preferred channel.

Supports: web (Redis pub/sub → SSE), Teams, Slack, Google Chat, WhatsApp, email draft.
The delivery channel is determined by the assignment's trigger_config.notify_via
or falls back to web notification.

Notifications are ALSO persisted as Activity records so they survive SSE
disconnections.  Even if a user's browser tab is closed when the assignment
finishes, they will see the notification in their activity feed on next visit.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import TRUNCATE_ERROR
from app.core.redaction import redact_known_secrets_for_display
from app.core.redis import redis_client
from app.core.utils import format_exception_message, markdown_to_slack
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.services.assignment_helpers import is_interactive_run
from app.services.channel_notices import localized_reply, notice_language
from app.services.vault import reveal_field, reveal_json_field

# ``Activity.title`` is String(500). Both halves of the title are unbounded on
# their own — a user-named assignment and a gated batch of any size — so both
# are capped rather than trusted.
_ACTIVITY_TITLE_MAX = 500
_MAX_TOOLS_NAMED_IN_TITLE = 3


def _fit_activity_title(title: str) -> str:
    """Keep a title inside the column, losing the tail rather than the row."""
    if len(title) <= _ACTIVITY_TITLE_MAX:
        return title
    return title[: _ACTIVITY_TITLE_MAX - 1] + "\u2026"

logger = logging.getLogger(__name__)


async def deliver_notification(
    db: AsyncSession,
    run: AssignmentRun,
    assignment: Assignment,
    agent: Agent,
) -> None:
    """Deliver an assignment completion notification to the user.

    Sends a real-time push (Redis pub/sub → SSE) AND persists an Activity
    record for durability.  The Activity record ensures the notification is
    visible even if the user was not connected at the moment of delivery.
    """
    # Determine delivery channel from assignment config
    notify_via = (assignment.trigger_config or {}).get("notify_via", "web")
    if run.status == "failed":
        summary = run.error or f"Assignment '{assignment.name}' failed."
    else:
        summary = run.result_summary or f"Assignment '{assignment.name}' completed."
    # Read in a channel, a mailbox or a notification: a secret variable value
    # the run echoed, or its stored marker, is the display text.
    summary = redact_known_secrets_for_display(summary)

    # 1) Always persist as Activity record (durable, survives SSE disconnections)
    await _persist_notification_activity(db, run, assignment, agent, summary)

    # 2) Company-managed runs broadcast to ``activity:company`` exactly once,
    #    HERE, independent of the external delivery channel below. A successful
    #    teams/slack/whatsapp/email delivery never calls ``_notify_web``, so
    #    without this top-level broadcast a shared-run notification would never
    #    reach connected users for those channels. Doing it once here (and
    #    keeping every per-channel fallback personal-only, ``is_company=False``)
    #    guarantees a single company broadcast regardless of the channel.
    if run.is_company_managed:
        await _notify_web(
            str(run.user_id),
            agent.name,
            assignment.name,
            summary,
            is_company=True,
        )

    # 3) Send the targeted real-time push via the configured channel.
    if notify_via == "teams":
        await _notify_teams(db, agent, summary, run)
    elif notify_via == "slack":
        await _notify_slack(db, agent, summary, run)
    elif notify_via == "google_chat":
        await _notify_google_chat(db, agent, summary, run, assignment)
    elif notify_via == "whatsapp":
        await _notify_whatsapp(db, agent, summary, run)
    elif notify_via == "email":
        await _notify_email(db, agent, summary, run)
    elif not run.is_company_managed:
        # Web default — personal owner channel. Company-managed runs already
        # broadcast to ``activity:company`` in step 2 above.
        await _notify_web(
            str(run.user_id),
            agent.name,
            assignment.name,
            summary,
            is_company=False,
        )


async def notify_web_user(
    db: AsyncSession,
    user_id: str,
    *,
    title: str,
    summary: str | None = None,
    source_label: str | None = None,
    extra_data: dict | None = None,
    is_company: bool = False,
) -> None:
    """A generic notification-bell delivery (the flow Notify node).

    Durable Activity row + real-time SSE push — the same two legs assignment
    notifications ride. The Activity is FLUSHED, not committed: callers that
    run inside a transaction (the flow walker) own the commit, and the bell's
    poll picks the row up right after.
    """
    import uuid as _uuid

    try:
        activity = Activity(
            user_id=_uuid.UUID(str(user_id)),
            activity_type="notification",
            agent_name=source_label,
            title=title[:500],
            summary=summary[:TRUNCATE_ERROR] if summary else None,
            extra_data=extra_data or {},
        )
        db.add(activity)
        await db.flush()
        # To the person's linked phones too, once the caller commits.
        from app.services.mobile_push import queue_push

        queue_push(db, user_id=activity.user_id, ref_kind="activity", ref_id=activity.id)
    except Exception as e:
        logger.warning(
            "Failed to persist web notification for user %s: %s",
            user_id,
            format_exception_message(e),
        )
    await _notify_web(
        str(user_id),
        source_label or "Flow",
        title,
        summary or title,
        is_company=is_company,
    )


async def _persist_notification_activity(
    db: AsyncSession,
    run: AssignmentRun,
    assignment: Assignment,
    agent: Agent,
    summary: str,
) -> None:
    """Write a durable Activity record for the notification.

    For personal runs, an Activity is created for the owner. For
    company-managed runs we skip the personal Activity record and rely on
    the broadcast SSE notification (``activity:company``) to reach all
    connected users — keyed on ``is_company_managed`` rather than
    ``user_id``, because company-managed runs now always carry an owner /
    run-as ``user_id`` (so the old ``user_id IS NULL`` proxy no longer
    identifies them).

    **Exception: an autonomous run paused by the per-tool approval gate always
    gets a durable record**, even when company-managed. Only a gate pause
    (``pending_approvals``) takes it, so a ``request_human_input`` pause of an
    install that never opted into the gate is notified as before. The broadcast
    is ephemeral — it
    reaches whoever happens to be connected at that instant and nobody else. For
    a completed run that is acceptable, since the result is still on the run.
    For a *paused* one it is not: nothing further happens until a human acts, so
    a missed broadcast means the scheduled work silently stops forever. Since
    per-tool approval makes scheduled runs pause routinely, this stopped being a
    theoretical gap.
    """
    _awaiting_unattended = (
        run.status == "awaiting_human"
        and bool(getattr(run, "pending_approvals", None))
        and not is_interactive_run(run)
    )

    if (run.is_company_managed or not run.user_id) and not _awaiting_unattended:
        # Company-managed (or any owner-less run): rely on the
        # ``activity:company`` broadcast in ``_notify_web``; a personal
        # Activity for a single owner would not reach the other users a
        # company-managed run is meant to notify.
        return

    _actor_id = run.user_id
    if _actor_id is None and _awaiting_unattended:
        # A paused unattended run stops forever until a person acts, so the
        # broadcast — ephemeral, reaching only whoever happens to be connected
        # at that instant — is not enough on its own. With no run owner,
        # attribute to the assignment's owner: the closest identity to whoever
        # set the automation up, and the person who can answer it.
        _actor_id = getattr(assignment, "user_id", None)

    if _actor_id is None:
        # Nothing to attribute the Activity to, even now — an owner-less run of
        # an owner-less assignment. The broadcast is all we have.
        return

    try:
        status_label = "failed" if run.status == "failed" else "completed"
        if run.status == "awaiting_human":
            status_label = "awaiting input"

        # Name the tools awaiting approval in the title. "awaiting input"
        # alone does not tell the owner of an unattended run whether this needs
        # them now or can wait — and they were not watching when it paused, so
        # the popup they would otherwise read never reached them.
        pending = getattr(run, "pending_approvals", None) or []
        pending_tools = [p.get("tool_name") for p in pending if p.get("tool_name")]
        if pending_tools:
            # Named, but capped at both ends: a gated batch has no natural size
            # and neither does a user-named assignment, so an uncapped title
            # overflows ``Activity.title`` and the except below swallows the
            # DataError — losing the durable record this whole function exists
            # to guarantee.
            _named = pending_tools[:_MAX_TOOLS_NAMED_IN_TITLE]
            _rest = len(pending_tools) - len(_named)
            _tools = ", ".join(_named) + (f" and {_rest} more" if _rest else "")
            status_label = f"approval needed: {_tools}"

        # Both halves are unbounded, and the actionable half is the status
        # label — truncating the tail would drop exactly the tool names the
        # owner needs — so the assignment name is what yields when they do not
        # both fit. ``_fit_activity_title`` stays as the backstop.
        _label = f" \u2014 {status_label}"
        _name = assignment.name or ""
        _room = _ACTIVITY_TITLE_MAX - len(_label)
        if len(_name) > _room:
            _name = _name[: max(_room - 1, 0)] + "\u2026"

        activity = Activity(
            user_id=_actor_id,
            activity_type="notification",
            agent_id=agent.id,
            agent_name=agent.name,
            title=_fit_activity_title(f"{_name}{_label}"),
            summary=summary[:TRUNCATE_ERROR] if summary else None,
            extra_data={
                "run_id": str(run.id),
                "assignment_id": str(assignment.id),
                "assignment_name": assignment.name,
                "run_status": run.status,
                # Lets the client deep-link straight to the approval prompt
                # rather than making the owner hunt for the paused run.
                "pending_tools": pending_tools,
                "is_autonomous": not is_interactive_run(run),
            },
        )
        # The bell shows the run (not this row): the phone gets the run, with
        # Approve / Reject when it waits on tool approvals, once this commits.
        from app.services.mobile_push import queue_push

        queue_push(db, user_id=_actor_id, ref_kind="run", ref_id=run.id)
        db.add(activity)
        await db.commit()
    except Exception as e:
        logger.warning(
            "Failed to persist notification activity for run %s: %s", run.id, format_exception_message(e)
        )


async def _notify_web(
    user_id: str,
    agent_name: str,
    assignment_name: str,
    summary: str,
    *,
    is_company: bool = False,
) -> None:
    """Push to SSE activity stream.

    Company-managed runs broadcast to the ``activity:company`` channel so
    all connected users receive the notification; personal runs publish to
    the owner's ``activity:{user_id}`` channel. The company decision is
    driven by the explicit ``is_company`` flag (``run.is_company_managed``)
    rather than ``user_id`` being unset — company-managed runs now always
    carry an owner / run-as ``user_id``, so the old ``user_id``-absent proxy
    would misroute their notifications to a single user.
    """
    try:
        redis = await redis_client()
        payload = json.dumps(
            {
                "type": "notification",
                "agent_name": agent_name,
                "assignment_name": assignment_name,
                "summary": summary,
            }
        )
        if is_company:
            # Company-managed: broadcast to the shared channel so every
            # connected user sees it (not just the owner / run-as user).
            await redis.publish("activity:company", payload)
        elif user_id and user_id != "None":
            await redis.publish(f"activity:{user_id}", payload)
        else:
            # Owner-less personal run (shouldn't normally happen) — fall
            # back to the shared channel rather than dropping the event.
            await redis.publish("activity:company", payload)
    except Exception as e:
        logger.warning("Web notification failed: %s", format_exception_message(e))


async def _load_run_user(db: AsyncSession, run: AssignmentRun):
    """Load the run's owning user, or ``None``."""
    from app.models.user import User

    if not run.user_id:
        return None
    result = await db.execute(select(User).where(User.id == run.user_id))
    return result.scalar_one_or_none()


async def _notify_teams(
    db: AsyncSession, agent: Agent, message: str, run: AssignmentRun
) -> None:
    """Send a proactive Microsoft Teams message via the ``teams_bot`` channel bot.

    Uses the conversation reference stored in Redis when the user last
    messaged the bot.  Falls back to a web notification when the bot is not
    configured, the user never messaged it, or delivery fails.

    Channel bots live in the ``channel_bots`` table — the previous
    implementation looked them up in the ``integrations`` table (where
    ``teams_bot`` no longer exists) and minted the Bot Framework token from
    the tenant-less legacy endpoint, so Teams notifications never worked on
    current installs.
    """
    from app.channels.base import OutboundMessage
    from app.channels.teams import (
        TeamsChannel,
        _get_teams_creds,
        get_conversation_reference,
    )

    # Tenant-aware Bot Framework token from the connected teams_bot.
    creds = await _get_teams_creds()
    if not creds:
        await _notify_web(
            str(run.user_id),
            agent.name,
            "Teams notification",
            "Teams bot not configured — " + message,
            is_company=False,
        )
        return
    bot_token, app_id = creds

    user = await _load_run_user(db, run)
    if not user:
        return

    conv_ref = await get_conversation_reference(user.email)
    if not conv_ref:
        # No stored reference — user hasn't messaged the Teams bot yet
        await _notify_web(
            str(run.user_id),
            agent.name,
            "Teams notification",
            message,
            is_company=False,
        )
        logger.info(
            "Teams: no conversation reference for %s, fell back to web", user.email
        )
        return

    if run.status == "awaiting_human" and await _send_run_decision_card(db, run, user, conv_ref):
        return

    # TeamsChannel handles Markdown normalization, chunking against the
    # Teams activity size cap, and the required Activity.from field.
    channel = TeamsChannel(
        conv_ref["service_url"],
        conv_ref["conversation_id"],
        bot_token,
        bot_id=app_id,
    )
    shown = localized_reply(message, notice_language(user))
    result = await channel.send_message(
        OutboundMessage(text=f"**{agent.name}**\n\n{shown}")
    )
    if result is not None:
        logger.info("Teams notification sent for assignment run %s", run.id)
    else:
        logger.warning(
            "Teams proactive message failed for assignment run %s", run.id
        )
        await _notify_web(
            str(run.user_id),
            agent.name,
            "Teams notification",
            message,
            is_company=False,
        )


async def _send_run_decision_card(db: AsyncSession, run: AssignmentRun, user: object, conv_ref: dict[str, str]) -> bool:
    """Send the paused run's decision as its Teams card, answered in place (#842, S9, I.2).

    ``False`` when the run has no open decision or the card was not taken:
    the caller then sends its text as before.
    """
    from app.services.decision_card_delivery import open_decision, send_decision_card

    try:
        decision = await open_decision(db, "run", run.id)
        if decision is None:
            return False
        conversation = {
            "service_url": str(conv_ref["service_url"]),
            "conversation_id": str(conv_ref["conversation_id"]),
            "conversation_type": "",
        }
        return await send_decision_card(db, decision, conversation, viewer=user, language=notice_language(user))
    except Exception as exc:
        logger.warning("Teams decision card for run %s not sent: %s", run.id, format_exception_message(exc))
        return False


async def _send_run_slack_decision_card(
    db: AsyncSession, run: AssignmentRun, user: object, bot_token: str, slack_user_id: str
) -> bool:
    """Send the paused run's decision as its Slack card, answered in place (#842, S10, I.2).

    ``False`` when the run has no open decision or Slack refused the card: the
    caller then sends its text as before.
    """
    from app.services.decision_card_delivery import open_decision
    from app.services.slack_decision_cards import send_decision_card

    try:
        decision = await open_decision(db, "run", run.id)
        if decision is None:
            return False
        return await send_decision_card(db, decision, bot_token, slack_user_id, language=notice_language(user))
    except Exception as exc:
        logger.warning("Slack decision card for run %s not sent: %s", run.id, format_exception_message(exc))
        return False


async def _notify_slack(
    db: AsyncSession, agent: Agent, message: str, run: AssignmentRun
) -> None:
    """Send a Slack DM to the run's owner via the ``slack_bot`` channel bot.

    Resolves the user's Slack ID by email (``users.lookupByEmail``, requires
    the ``users:read.email`` scope) and DMs them — the previous
    implementation posted into a hardcoded ``#general`` channel and never
    checked the API response.
    """
    import httpx

    from app.channels.base import get_channel_bot

    bot = await get_channel_bot("slack_bot")
    bot_token = (await reveal_field(bot, "api_key_enc") if bot else None) or ""
    if not bot_token:
        await _notify_web(
            str(run.user_id),
            agent.name,
            "Slack notification",
            "Slack bot not configured — " + message,
            is_company=False,
        )
        return

    user = await _load_run_user(db, run)
    if not user:
        return

    headers = {"Authorization": f"Bearer {bot_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        lookup = await client.get(
            "https://slack.com/api/users.lookupByEmail",
            headers=headers,
            params={"email": user.email},
        )
        lookup_data = lookup.json()
        slack_user_id = (
            lookup_data.get("user", {}).get("id") if lookup_data.get("ok") else None
        )
        if not slack_user_id:
            logger.info(
                "Slack: could not resolve %s to a Slack user (%s), fell back to web",
                user.email,
                lookup_data.get("error", "unknown"),
            )
            await _notify_web(
                str(run.user_id),
                agent.name,
                "Slack notification",
                message,
                is_company=False,
            )
            return

        if run.status == "awaiting_human" and await _send_run_slack_decision_card(
            db, run, user, bot_token, slack_user_id
        ):
            return

        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            json={
                "channel": slack_user_id,
                "text": markdown_to_slack(
                    f"*{agent.name}*\n{localized_reply(message, notice_language(user))}"
                ),
            },
        )
        resp_data = resp.json()

    if resp_data.get("ok"):
        logger.info("Slack notification sent for assignment run %s", run.id)
    else:
        logger.warning(
            "Slack notification failed for assignment run %s: %s",
            run.id,
            resp_data.get("error", "unknown"),
        )
        await _notify_web(
            str(run.user_id),
            agent.name,
            "Slack notification",
            message,
            is_company=False,
        )


async def _notify_google_chat(
    db: AsyncSession, agent: Agent, message: str, run: AssignmentRun, assignment: Assignment
) -> None:
    """Send the run's result to Google Chat as the Chat app (#842, S11, I.4 path (d)).

    The target is ``trigger_config.notify_space`` (a space the app is in),
    else the app's DM with the run's owner (their linked Chat identity). A
    run paused on a decision goes as its cardsV2 card, answered in place;
    anything else, or a card Chat refused, as text. No channel bot, no space
    and no DM: the web notification, as for every channel.
    """
    from app.channels.base import OutboundMessage
    from app.channels.google_chat import GoogleChatChannel, _get_googlechat_creds
    from app.services import google_chat_decision_cards as gchat

    user = await _load_run_user(db, run)
    if not user:
        return
    creds = await _get_googlechat_creds()
    token = await GoogleChatChannel(creds["credentials_json"])._get_access_token() if creds else ""
    configured = str((assignment.trigger_config or {}).get("notify_space") or "")
    space = gchat.space_name(configured) if configured else None
    if space is None and token and (name := gchat.chat_user_name(user)):
        space = await gchat.direct_message_space(token, name)
    if not token or space is None:
        logger.info("Google Chat: no app token or no space for run %s, fell back to web", run.id)
        await _notify_web(str(run.user_id), agent.name, "Google Chat notification", message, is_company=False)
        return
    language = notice_language(user)
    if run.status == "awaiting_human" and await _send_run_google_chat_decision_card(db, run, token, space, language):
        return
    channel = GoogleChatChannel(creds["credentials_json"], space_name=space) if creds else None
    sent = (
        await channel.send_message(OutboundMessage(text=f"**{agent.name}**\n\n{localized_reply(message, language)}"))
        if channel
        else None
    )
    if sent:
        logger.info("Google Chat notification sent for assignment run %s", run.id)
        return
    logger.warning("Google Chat notification failed for assignment run %s", run.id)
    await _notify_web(str(run.user_id), agent.name, "Google Chat notification", message, is_company=False)


async def _send_run_google_chat_decision_card(
    db: AsyncSession, run: AssignmentRun, token: str, space: str, language: str
) -> bool:
    """Send the paused run's decision as its Google Chat card (#842, S11); ``False``: send the text."""
    from app.services.decision_card_delivery import open_decision
    from app.services.google_chat_decision_cards import send_decision_card

    try:
        decision = await open_decision(db, "run", run.id)
        if decision is None:
            return False
        return await send_decision_card(db, decision, token, space, language=language)
    except Exception as exc:
        logger.warning("Google Chat decision card for run %s not sent: %s", run.id, format_exception_message(exc))
        return False


async def _notify_whatsapp(
    db: AsyncSession, agent: Agent, message: str, run: AssignmentRun
) -> None:
    """Send a WhatsApp DM to the run's owner via the ``whatsapp_bot`` channel bot.

    The recipient phone number comes from the user's linked channel identity
    (``whatsapp:<phone>``), recorded automatically the first time they
    message the bot.  Falls back to a web notification when the bot is not
    configured, no identity is linked, or delivery fails — the previous
    implementation never sent anything while logging success.
    """
    from app.channels.base import OutboundMessage, get_channel_bot
    from app.channels.whatsapp import WhatsAppChannel

    bot = await get_channel_bot("whatsapp_bot")
    access_token = (await reveal_field(bot, "api_key_enc") if bot else None) or ""
    extra = (await reveal_json_field(bot, "extra_config_enc") or {}) if bot else {}
    phone_number_id = extra.get("phone_number_id", "")
    if not access_token or not phone_number_id:
        await _notify_web(
            str(run.user_id),
            agent.name,
            "WhatsApp notification",
            "WhatsApp bot not configured — " + message,
            is_company=False,
        )
        return

    user = await _load_run_user(db, run)
    if not user:
        return

    phone = next(
        (
            key.split(":", 1)[1]
            for key in (user.channel_identities or {})
            if key.startswith("whatsapp:")
        ),
        "",
    )
    if not phone:
        logger.info(
            "WhatsApp: no linked WhatsApp identity for %s, fell back to web",
            user.email,
        )
        await _notify_web(
            str(run.user_id),
            agent.name,
            "WhatsApp notification",
            message,
            is_company=False,
        )
        return

    channel = WhatsAppChannel(phone_number_id, access_token)
    result = await channel.send_message(
        OutboundMessage(
            text=f"*{agent.name}*\n\n{localized_reply(message, notice_language(user))}",
            channel_user_id=phone,
        )
    )
    if result:
        logger.info("WhatsApp notification sent for assignment run %s", run.id)
    else:
        logger.warning(
            "WhatsApp notification failed for assignment run %s", run.id
        )
        await _notify_web(
            str(run.user_id),
            agent.name,
            "WhatsApp notification",
            message,
            is_company=False,
        )


async def _notify_email(
    db: AsyncSession, agent: Agent, message: str, run: AssignmentRun
) -> None:
    """Draft an email notification (requires microsoft or google integration)."""
    # For now, fall back to web. Personal-only — company-managed runs already
    # broadcast to ``activity:company`` once in ``deliver_notification``.
    await _notify_web(
        str(run.user_id),
        agent.name,
        "Email notification",
        message,
        is_company=False,
    )
