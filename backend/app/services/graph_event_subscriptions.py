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

"""Microsoft Graph change-notification subscriptions for integration-event assignments.

An enabled ``integration_event`` assignment on the ``microsoft`` provider is
delivered by a Graph subscription when the deployment has a public
notification URL (``BASE_URL`` on https, not a loopback, private or ``.local``
host) and ``INTEGRATION_EVENT_GRAPH_SUBSCRIPTIONS_ENABLED`` is on:

========================  ===========================================  =======================
event                     resource                                     change type
========================  ===========================================  =======================
``new_email``             ``me/mailFolders('<folder>')/messages``,       created
                          else ``me/messages``
``email_flag_changed``    ``me/mailFolders('<folder>')/messages``,       updated
                          else ``me/messages``
``calendar_event_changed`` ``me/events``                               created, updated, deleted
``new_teams_message``     ``teams/{team}/channels/{channel}/messages`` created (one per channel)
``new_teams_reply``       ``teams/{team}/channels/{channel}/messages`` created (one per channel)
``new_teams_chat_message`` ``chats/{chat}/messages``                   created (one per chat)
``todo_task_changed``     ``me/todo/lists/{list}/tasks``               created, updated, deleted (one per list id)
========================  ===========================================  =======================

A mail trigger subscribes to what its poll reads: the filter's ``folder``, else
every folder (``me/messages``, as ``event_poller._mail_listing``), so a mail a
rule files outside the inbox is notified like one left in it. A Teams trigger
subscribes only when it names its channels (``chats``: its ``chat_ids``); one
reading every joined channel or chat stays polled.

The scheduler maintains them (:func:`maintain_graph_event_subscriptions`):
each is created with a random ``clientState`` recorded through
``store_graph_subscription``, which the notification endpoint verifies, and
renewed before it expires. While an assignment's ``graph_sub_wf:<id>`` record
lives - as long as its subscriptions do - the poller skips it. A subscription
Graph refuses, removes or lets lapse drops the record and the poller reads on:
the notification endpoint advances the poll cursor on every event it
delivers, and the seen set it shares with the poller runs each event once.
An assignment disabled, deleted or retargeted has its subscriptions deleted
on the next pass.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.egress import egress_client, egress_route
from app.core.security import decrypt_value
from app.core.utils import format_exception_message
from app.integrations.providers.microsoft import GRAPH_API
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.integration import Integration
from app.services.assignment_pause import owner_not_deactivated
from app.services.integration_credentials import agent_acting_user_id
from app.services.integration_event_filters import trigger_filter
from app.services.integration_notification_auth import (
    GRAPH_NOTIFICATION_PATH,
    graph_subscription_key,
    new_graph_client_state,
    store_graph_subscription,
)
from app.services.integration_user_credentials import connected_for_clause, reader_clause, resolve_token_holder
from app.services.relays import integration_egress_route
from app.services.trigger_integration_access import trigger_integration_id

logger = logging.getLogger(__name__)

SUBSCRIPTION_KEY_PREFIX = "graph_sub_wf:"
_INDEX_KEY = "graph_sub_wf_index"
_FAILURE_KEY_PREFIX = "graph_sub_fail:"
_RENEW_KEY_PREFIX = "graph_sub_renew:"
#: Set by a ``missed`` lifecycle notification: the poller reads the
#: subscribed assignment once, the seen set skipping what already ran.
CATCH_UP_KEY_PREFIX = "graph_sub_catchup:"
NOTIFIED_KEY_PREFIX = "event_notified:"
_MAINTENANCE_LOCK_KEY = "graph_sub_maintenance_lock"
_MAINTENANCE_DUE_KEY = "graph_sub_maintenance_due"

#: Under the 3-day maximum of a Teams chatMessage subscription (mail and
#: events allow 7): one lifetime for every resource.
SUBSCRIPTION_LIFETIME = timedelta(minutes=4230)
_RENEW_BEFORE = timedelta(hours=24)
_RETRY_AFTER_FAILURE_SECONDS = 3600
_MAINTENANCE_INTERVAL_SECONDS = 120
_MAINTENANCE_LOCK_SECONDS = 600
_NOTIFIED_TTL_SECONDS = 86400 * 30
_CATCH_UP_TTL_SECONDS = 86400

SUBSCRIBABLE_EVENTS = frozenset(
    {
        "new_email",
        "email_flag_changed",
        "calendar_event_changed",
        "new_teams_message",
        "new_teams_reply",
        "new_teams_chat_message",
        "todo_task_changed",
    }
)
_TEAMS_CHANNEL_EVENTS = frozenset({"new_teams_message", "new_teams_reply"})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan")
#: A To Do list id as Graph writes it (a long base64 token), not a display name.
_TODO_LIST_ID = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")


def todo_list_ids(filter_config: dict) -> list[str]:
    """The ``lists`` of a To Do trigger when every one is a list id, else ``[]``."""
    lists = [v.strip() for v in filter_config.get("lists") or [] if isinstance(v, str) and v.strip()]
    return lists if lists and all(_TODO_LIST_ID.match(v) for v in lists) else []


@dataclass(frozen=True)
class SubscriptionPlan:
    """The ``(resource, changeType)`` pairs an assignment subscribes to, or why it cannot."""

    resources: tuple[tuple[str, str], ...]
    reason: str | None = None


def _channels(filter_config: dict) -> list[str]:
    scope = filter_config.get("scope")
    channels = (scope or {}).get("teams_channel") if isinstance(scope, dict) else None
    channels = channels or filter_config.get("channels") or []
    return [c for c in channels if isinstance(c, str) and ":" in c]


def subscription_plan(trigger_config: Any) -> SubscriptionPlan:
    """What a Microsoft trigger subscribes to (see the module table)."""
    config = trigger_config if isinstance(trigger_config, dict) else {}
    event = config.get("event")
    filter_config = trigger_filter(config)
    folder = filter_config.get("folder")
    # Without a folder the poll reads every folder (``/me/messages``): the
    # subscription covers the same mail, never the inbox alone.
    mail_folder = (
        f"me/mailFolders('{quote(folder.strip(), safe='')}')/messages"
        if isinstance(folder, str) and folder.strip()
        else "me/messages"
    )
    if event == "new_email":
        return SubscriptionPlan(((mail_folder, "created"),))
    if event == "email_flag_changed":
        return SubscriptionPlan(((mail_folder, "updated"),))
    if event == "calendar_event_changed":
        return SubscriptionPlan((("me/events", "created,updated,deleted"),))
    if event in _TEAMS_CHANNEL_EVENTS:
        channels = _channels(filter_config)
        if not channels:
            return SubscriptionPlan((), "no channel is selected (a subscription names each channel)")
        return SubscriptionPlan(
            tuple(
                (f"teams/{team}/channels/{channel}/messages", "created")
                for team, channel in (c.split(":", 1) for c in channels)
            )
        )
    if event == "new_teams_chat_message":
        chats = [c for c in filter_config.get("chat_ids") or [] if isinstance(c, str) and c.strip()]
        if not chats:
            return SubscriptionPlan((), "no chat is selected in chat_ids (a subscription names each chat)")
        return SubscriptionPlan(tuple((f"chats/{chat.strip()}/messages", "created") for chat in chats))
    if event == "todo_task_changed":
        list_ids = todo_list_ids(filter_config)
        if not list_ids:
            return SubscriptionPlan(
                (), "lists does not name every task list by id (a subscription names each list): polled every minute"
            )
        return SubscriptionPlan(
            tuple((f"me/todo/lists/{quote(i, safe='')}/tasks", "created,updated,deleted") for i in list_ids)
        )
    return SubscriptionPlan((), f"{event or 'this event'} has no Graph subscription")


def public_notification_url() -> str | None:
    """The URL Graph posts notifications to, ``None`` when Graph could not reach it.

    ``BASE_URL`` on https whose host is neither a loopback, private or
    link-local address nor a local name - and the setting switched on.
    """
    if not settings.integration_event_graph_subscriptions_enabled:
        return None
    base = (settings.base_url or "").strip().rstrip("/")
    parsed = urlsplit(base)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None
    if host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
        return None
    return f"{base}{GRAPH_NOTIFICATION_PATH}"


def _unavailable_reason() -> str:
    if not settings.integration_event_graph_subscriptions_enabled:
        return "Graph subscriptions are switched off (INTEGRATION_EVENT_GRAPH_SUBSCRIPTIONS_ENABLED)"
    return "BASE_URL is not a public https URL Graph can post notifications to"


def _record_key(assignment_id: object) -> str:
    return f"{SUBSCRIPTION_KEY_PREFIX}{assignment_id}"


def _graph_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _fingerprint(plan: SubscriptionPlan, integration_id: object) -> str:
    raw = json.dumps([str(integration_id), sorted(plan.resources)])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _decoded(value: Any) -> Any:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


async def read_record(redis: Redis, assignment_id: object) -> dict | None:
    """The subscription record of an assignment, ``None`` when it is polled."""
    try:
        raw = _decoded(await redis.get(_record_key(assignment_id)))
    except Exception:
        return None
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        # A legacy marker (any value): still a subscription, as the poller reads it.
        return {"subscriptions": [], "legacy": True}
    return record if isinstance(record, dict) else None


def _expiry(record: dict) -> datetime | None:
    instants = []
    for sub in record.get("subscriptions") or []:
        try:
            instants.append(datetime.fromisoformat(sub["expires_at"]))
        except (KeyError, TypeError, ValueError):
            continue
    return min(instants) if instants else None


async def _write_record(redis: Redis, assignment_id: object, record: dict) -> None:
    expiry = _expiry(record)
    ttl = int((expiry - datetime.now(UTC)).total_seconds()) if expiry else 0
    if ttl <= 0:
        await redis.delete(_record_key(assignment_id))
        return
    await redis.set(_record_key(assignment_id), json.dumps(record), ex=ttl)
    await redis.sadd(_INDEX_KEY, str(assignment_id))


async def _graph(
    http: httpx.AsyncClient, method: str, path: str, token: str, *, json_body: dict | None = None
) -> httpx.Response | None:
    try:
        return await http.request(
            method,
            f"{GRAPH_API}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=json_body,
            timeout=30,
        )
    except Exception as exc:
        logger.warning("Graph %s %s failed: %s", method, path.split("?", 1)[0], format_exception_message(exc))
        return None


async def _delete_subscriptions(http: httpx.AsyncClient, token: str | None, subscriptions: list[dict]) -> None:
    for sub in subscriptions:
        sub_id = sub.get("id")
        if not isinstance(sub_id, str) or not sub_id:
            continue
        if token:
            resp = await _graph(http, "DELETE", f"subscriptions/{sub_id}", token)
            if resp is not None and resp.status_code not in (200, 204, 404):
                logger.info("Graph subscription %s not deleted (%s): it lapses on its own", sub_id, resp.status_code)


async def _retire(
    redis: Redis, http: httpx.AsyncClient, assignment_id: str, record: dict | None, token: str | None, why: str
) -> None:
    """Delete an assignment's subscriptions and record: it is polled from now on."""
    subscriptions = (record or {}).get("subscriptions") or []
    await _delete_subscriptions(http, token, subscriptions)
    for sub in subscriptions:
        if isinstance(sub.get("id"), str):
            await redis.delete(graph_subscription_key(sub["id"]))
    await redis.delete(_record_key(assignment_id), f"{_RENEW_KEY_PREFIX}{assignment_id}")
    await redis.srem(_INDEX_KEY, assignment_id)
    if subscriptions:
        logger.info("Graph subscriptions of assignment %s retired (%s): it is polled", assignment_id, why)


async def _note_failure(redis: Redis, assignment_id: str, reason: str) -> None:
    await redis.set(f"{_FAILURE_KEY_PREFIX}{assignment_id}", reason[:500], ex=_RETRY_AFTER_FAILURE_SECONDS)


async def graph_token_for(db: AsyncSession, wf: Assignment, agent: Agent) -> tuple[str, Any] | None:
    """The access token of the integration the trigger reads, and its egress route.

    The poller's rule: the trigger's integration, connected for and readable by
    the agent's acting user, through that user's own token on a per-user
    integration.
    """
    integration_id = trigger_integration_id(wf.trigger_config, agent)
    if not integration_id:
        return None
    acting = agent_acting_user_id(agent)
    integration = (
        await db.execute(
            select(Integration).where(
                Integration.id == integration_id,
                connected_for_clause(acting),
                reader_clause(acting),
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        return None
    holder = await resolve_token_holder(db, integration, acting)
    if not holder or not holder.access_token_enc:
        return None
    return decrypt_value(holder.access_token_enc), await integration_egress_route(db, integration)


async def _token_of_assignment(db: AsyncSession, assignment_id: str) -> tuple[str, Any] | None:
    """The Graph token of any assignment still stored (disabled or retargeted), best effort."""
    try:
        wf = await db.get(Assignment, uuid.UUID(assignment_id))
        agent = await db.get(Agent, wf.agent_id) if wf is not None else None
        return await graph_token_for(db, wf, agent) if wf is not None and agent is not None else None
    except Exception as exc:
        logger.debug("No Graph token to retire the subscriptions of %s: %r", assignment_id, exc)
        return None


async def _create(
    redis: Redis,
    http: httpx.AsyncClient,
    token: str,
    wf: Assignment,
    plan: SubscriptionPlan,
    notification_url: str,
    fingerprint: str,
) -> str | None:
    """Create every subscription of *plan*; ``None`` on success, else why not (nothing kept)."""
    from app.services.event_poller import seed_event_cursor

    created: list[dict] = []
    expires = datetime.now(UTC) + SUBSCRIPTION_LIFETIME
    for resource, change_type in plan.resources:
        client_state = new_graph_client_state()
        resp = await _graph(
            http,
            "POST",
            "subscriptions",
            token,
            json_body={
                "changeType": change_type,
                "notificationUrl": notification_url,
                "lifecycleNotificationUrl": notification_url,
                "resource": resource,
                "expirationDateTime": _graph_datetime(expires),
                "clientState": client_state,
            },
        )
        sub_id = resp.json().get("id") if resp is not None and resp.status_code in (200, 201) else None
        if not isinstance(sub_id, str) or not sub_id:
            detail = (
                f"Graph answered {resp.status_code}: {resp.text[:200]}" if resp is not None else "Graph unreachable"
            )
            await _delete_subscriptions(http, token, created)
            for sub in created:
                await redis.delete(graph_subscription_key(sub["id"]))
            return f"subscription to {resource} not created ({detail})"
        await store_graph_subscription(
            redis,
            sub_id,
            str(wf.id),
            client_state,
            ttl_seconds=int(SUBSCRIPTION_LIFETIME.total_seconds()),
        )
        created.append({"id": sub_id, "resource": resource, "expires_at": expires.isoformat()})
    # A lapse polls from here, never the latest page of the mailbox.
    await seed_event_cursor(redis, wf.id)
    now = datetime.now(UTC).isoformat()
    await _write_record(
        redis,
        wf.id,
        {
            "subscriptions": created,
            "event": (wf.trigger_config or {}).get("event"),
            "fingerprint": fingerprint,
            "created_at": now,
            "renewed_at": now,
        },
    )
    await redis.delete(f"{_FAILURE_KEY_PREFIX}{wf.id}")
    logger.info("Assignment %s delivered by %d Graph subscription(s)", wf.id, len(created))
    return None


async def _renew(redis: Redis, http: httpx.AsyncClient, token: str, assignment_id: str, record: dict) -> bool:
    """Extend every subscription of *record*; ``False`` when one is gone (the record is retired)."""
    expires = datetime.now(UTC) + SUBSCRIPTION_LIFETIME
    for sub in record.get("subscriptions") or []:
        resp = await _graph(
            http,
            "PATCH",
            f"subscriptions/{sub['id']}",
            token,
            json_body={"expirationDateTime": _graph_datetime(expires)},
        )
        if resp is None:
            return True
        if resp.status_code == 404:
            await _retire(redis, http, assignment_id, record, token, "Graph no longer holds it")
            return False
        if resp.status_code != 200:
            logger.warning(
                "Graph subscription %s of assignment %s not renewed (%s): polled once it lapses",
                sub["id"],
                assignment_id,
                resp.status_code,
            )
            return True
        sub["expires_at"] = expires.isoformat()
        await redis.expire(graph_subscription_key(sub["id"]), int(SUBSCRIPTION_LIFETIME.total_seconds()))
    record["renewed_at"] = datetime.now(UTC).isoformat()
    await _write_record(redis, assignment_id, record)
    await redis.delete(f"{_RENEW_KEY_PREFIX}{assignment_id}")
    return True


async def _due(redis: Redis) -> bool:
    """Whether a maintenance pass is due (every ``_MAINTENANCE_INTERVAL_SECONDS``, or asked for)."""
    return bool(await redis.set(_MAINTENANCE_DUE_KEY, "1", nx=True, ex=_MAINTENANCE_INTERVAL_SECONDS))


async def request_maintenance(redis: Redis) -> None:
    """Have the next scheduler tick run a pass (an assignment changed, a lifecycle event)."""
    try:
        await redis.delete(_MAINTENANCE_DUE_KEY)
    except Exception as exc:
        logger.debug("Could not ask for a Graph subscription pass: %r", exc)


async def maintain_graph_event_subscriptions(db: AsyncSession, *, force: bool = False) -> dict[str, int]:
    """Create, renew and retire the assignments' Graph subscriptions; the counts of each."""
    from app.core.redis import redis_client

    counts = {"created": 0, "renewed": 0, "retired": 0, "failed": 0}
    redis = await redis_client()
    if not force and not await _due(redis):
        return counts
    if not await redis.set(_MAINTENANCE_LOCK_KEY, "1", nx=True, ex=_MAINTENANCE_LOCK_SECONDS):
        return counts
    try:
        await _maintain(db, redis, counts)
    finally:
        await redis.delete(_MAINTENANCE_LOCK_KEY)
    return counts


async def _maintain(db: AsyncSession, redis: Redis, counts: dict[str, int]) -> None:
    notification_url = public_notification_url()
    rows = (
        (
            await db.execute(
                select(Assignment).where(
                    Assignment.trigger_type == "integration_event",
                    Assignment.enabled.is_(True),
                    owner_not_deactivated(),
                )
            )
        )
        .scalars()
        .all()
    )
    wanted = {
        str(wf.id): wf
        for wf in rows
        if (wf.trigger_config or {}).get("provider") == "microsoft"
        and (wf.trigger_config or {}).get("event") in SUBSCRIBABLE_EVENTS
    }
    agent_ids = {wf.agent_id for wf in wanted.values()}
    agents = (
        {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all()}
        if agent_ids
        else {}
    )
    indexed = {_decoded(member) for member in await redis.smembers(_INDEX_KEY)}

    async with egress_client() as http:
        # Subscriptions nothing wants any more: deleted with the token the
        # assignment still has, else left to lapse (their records go, so what
        # they would deliver is discarded).
        for assignment_id in sorted(indexed - (set(wanted) if notification_url else set())):
            record = await read_record(redis, assignment_id)
            access = await _token_of_assignment(db, assignment_id)
            with egress_route(access[1] if access else None):
                await _retire(
                    redis,
                    http,
                    assignment_id,
                    record,
                    access[0] if access else None,
                    "no longer wanted" if notification_url else _unavailable_reason(),
                )
            counts["retired"] += 1
        if notification_url is None:
            return

        for assignment_id, wf in wanted.items():
            agent = agents.get(wf.agent_id)
            if agent is None or not agent.enabled:
                continue
            plan = subscription_plan(wf.trigger_config)
            record = await read_record(redis, assignment_id)
            if record is not None and record.get("legacy"):
                continue
            fingerprint = _fingerprint(plan, trigger_integration_id(wf.trigger_config, agent))
            if plan.reason:
                if record is not None:
                    await _retire(redis, http, assignment_id, record, None, plan.reason)
                    counts["retired"] += 1
                await _note_failure(redis, assignment_id, plan.reason)
                continue
            renew_asked = bool(await redis.get(f"{_RENEW_KEY_PREFIX}{assignment_id}"))
            expiry = _expiry(record) if record else None
            stale = record is not None and record.get("fingerprint") != fingerprint
            due = record is not None and (renew_asked or expiry is None or expiry - datetime.now(UTC) < _RENEW_BEFORE)
            if record is not None and not stale and not due:
                continue
            if record is None and await redis.get(f"{_FAILURE_KEY_PREFIX}{assignment_id}"):
                continue
            try:
                access = await graph_token_for(db, wf, agent)
            except Exception as exc:
                access = None
                logger.warning("Assignment %s: no Graph token (%s)", assignment_id, format_exception_message(exc))
            if access is None:
                await _note_failure(redis, assignment_id, "the trigger's Microsoft integration has no usable token")
                counts["failed"] += 1
                continue
            token, route = access
            with egress_route(route):
                if stale:
                    await _retire(redis, http, assignment_id, record, token, "its trigger changed")
                    counts["retired"] += 1
                    record = None
                if record is not None:
                    if await _renew(redis, http, token, assignment_id, record):
                        counts["renewed"] += 1
                    continue
                failure = await _create(redis, http, token, wf, plan, notification_url, fingerprint)
            if failure is None:
                counts["created"] += 1
            else:
                await _note_failure(redis, assignment_id, failure)
                counts["failed"] += 1
                logger.warning("Assignment %s stays polled: %s", assignment_id, failure)


async def note_notified(redis: Redis, assignment_id: object) -> None:
    """Stamp the last notification an assignment received (``get_assignment`` reads it)."""
    try:
        await redis.set(
            f"{NOTIFIED_KEY_PREFIX}{assignment_id}", datetime.now(UTC).isoformat(), ex=_NOTIFIED_TTL_SECONDS
        )
    except Exception as exc:
        logger.debug("Could not stamp the notification of assignment %s: %r", assignment_id, exc)


async def handle_lifecycle_notification(redis: Redis, assignment_id: str, notification: dict) -> str:
    """Act on a Graph lifecycle notification of an assignment's subscription; what was done.

    ``reauthorizationRequired`` renews it on the next pass, ``subscriptionRemoved``
    drops the record (polled until a pass recreates it), ``missed`` has the
    poller read the assignment once.
    """
    event = str(notification.get("lifecycleEvent") or "")
    if event == "reauthorizationRequired":
        await redis.set(f"{_RENEW_KEY_PREFIX}{assignment_id}", "1", ex=_CATCH_UP_TTL_SECONDS)
        await request_maintenance(redis)
        return "renewal requested"
    if event == "subscriptionRemoved":
        record = await read_record(redis, assignment_id)
        subscriptions = (record or {}).get("subscriptions") or []
        removed = notification.get("subscriptionId")
        for sub in subscriptions:
            if isinstance(sub.get("id"), str):
                await redis.delete(graph_subscription_key(sub["id"]))
        await redis.delete(_record_key(assignment_id))
        await redis.srem(_INDEX_KEY, assignment_id)
        await request_maintenance(redis)
        logger.info("Graph removed subscription %s of assignment %s: it is polled", removed, assignment_id)
        return "polling resumed"
    if event == "missed":
        await redis.set(f"{CATCH_UP_KEY_PREFIX}{assignment_id}", "1", ex=_CATCH_UP_TTL_SECONDS)
        return "catch-up poll requested"
    return "ignored"


async def delivery_mode(redis: Redis, assignment: Any) -> dict:
    """How an integration-event assignment receives its events, for ``get_assignment``.

    ``mode`` is ``subscription`` or ``poll``, with the subscription's expiry or
    why it is polled, and the last poll and notification times. An event no
    poller reads (not in ``POLL_INTERVALS``) arrives only through the webhook
    receivers: ``mode`` is ``webhook`` then, never a poll it does not have.
    """
    from app.services.event_poller import POLL_INTERVALS, POLL_READ_KEY_PREFIX

    config = getattr(assignment, "trigger_config", None) or {}
    assignment_id = getattr(assignment, "id", None)
    info: dict[str, Any] = {}
    record = await read_record(redis, assignment_id)
    if record is not None:
        expiry = _expiry(record)
        info["mode"] = "subscription"
        info["subscriptions"] = len(record.get("subscriptions") or [])
        if expiry:
            info["expires_at"] = expiry.isoformat()
    elif config.get("event") not in POLL_INTERVALS:
        info["mode"] = "webhook"
    else:
        info["mode"] = "poll"
        info["interval_seconds"] = POLL_INTERVALS[config["event"]]
        if config.get("provider") == "microsoft" and config.get("event") in SUBSCRIBABLE_EVENTS:
            if public_notification_url() is None:
                info["reason"] = _unavailable_reason()
            else:
                plan = subscription_plan(config)
                failure = _decoded(await redis.get(f"{_FAILURE_KEY_PREFIX}{assignment_id}"))
                info["reason"] = plan.reason or failure or "the subscription is being created"
    try:
        # The interval stamp is written once a poll caught up; the read stamp at
        # every page, so a backlog read over several ticks shows its latest
        # page and says it is still catching up (#1718).
        stamped = _decoded(await redis.get(f"event_poll:{assignment_id}"))
        read = _decoded(await redis.get(f"{POLL_READ_KEY_PREFIX}{assignment_id}"))
        times = [float(value) for value in (stamped, read) if value]
        if times:
            info["last_poll_at"] = datetime.fromtimestamp(max(times), UTC).isoformat()
        if read and (not stamped or float(read) > float(stamped)):
            info["catching_up"] = True
        notified = _decoded(await redis.get(f"{NOTIFIED_KEY_PREFIX}{assignment_id}"))
        if notified:
            info["last_notification_at"] = notified
        if info.get("mode") == "poll":
            # Why the poller read nothing on its last pass (#1378).
            skipped = _decoded(await redis.get(f"event_poll_skip:{assignment_id}"))
            if skipped:
                info["poll_skipped"] = skipped
    except Exception as exc:
        logger.debug("Could not read the delivery times of assignment %s: %r", assignment_id, exc)
    return info


# ---------------------------------------------------------------------------
# Notifications: from a Graph resource to the event payload the poller builds
# ---------------------------------------------------------------------------

_CHANNEL_RESOURCE = re.compile(
    r"teams\('([^']+)'\)/channels\('([^']+)'\)/messages\('([^']+)'\)(?:/replies\('([^']+)'\))?",
    re.IGNORECASE,
)
_CHAT_RESOURCE = re.compile(r"chats\('([^']+)'\)/messages\('([^']+)'\)", re.IGNORECASE)
_TODO_RESOURCE = re.compile(r"todo/lists(?:\('([^']+)'\)|/([^/]+))/tasks(?:\('([^']+)'\)|/([^/]+))", re.IGNORECASE)


def parse_todo_resource(resource: str) -> tuple[str, str] | None:
    """``(list, task)`` of a To Do task notification's resource."""
    match = _TODO_RESOURCE.search(resource or "")
    if not match:
        return None
    return unquote(match.group(1) or match.group(2)), unquote(match.group(3) or match.group(4))


def parse_channel_resource(resource: str) -> tuple[str, str, str, str] | None:
    """``(team, channel, message, reply or "")`` of a channel message notification's resource."""
    match = _CHANNEL_RESOURCE.search(resource or "")
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3), match.group(4) or ""


def parse_chat_resource(resource: str) -> tuple[str, str] | None:
    """``(chat, message)`` of a chat message notification's resource."""
    match = _CHAT_RESOURCE.search(resource or "")
    return (match.group(1), match.group(2)) if match else None


async def _get(http: httpx.AsyncClient, token: str, path: str, params: dict | None = None) -> dict | None:
    try:
        resp = await http.get(
            f"{GRAPH_API}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
    except Exception as exc:
        logger.warning("Graph read of a notified resource failed: %s", format_exception_message(exc))
        return None
    if resp.status_code != 200:
        logger.info("Graph read of a notified resource answered %s", resp.status_code)
        return None
    data = resp.json()
    return data if isinstance(data, dict) else None


def _segment(value: str) -> str:
    return quote(value, safe=":@.")


async def _unreadable(redis: Redis, wf: Any) -> tuple[None, str]:
    """A notified resource Graph would not read back: the poller reads it on its
    next tick (``CATCH_UP_KEY_PREFIX``), the seen set skipping what already ran."""
    try:
        await redis.set(f"{CATCH_UP_KEY_PREFIX}{wf.id}", "1", ex=_CATCH_UP_TTL_SECONDS)
    except Exception as exc:
        logger.debug("Could not ask for a catch-up poll of assignment %s: %r", wf.id, exc)
    return None, ""


async def notified_event(
    event: str,
    notification: dict,
    *,
    wf: Any,
    token: str,
    redis: Redis,
    http: httpx.AsyncClient,
    integration_id: object,
) -> tuple[dict | None, str]:
    """The payload a notification delivers to *wf*, and the poll-cursor instant it reaches.

    ``(None, "")`` when the notification is not an event of the trigger: a
    reply to a root-message trigger, a mail update that did not change the
    flag, the agent's own post. A resource Graph will not read back is read
    by the poller on its next tick instead (``_unreadable``).
    """
    from app.services import event_poller as poller
    from app.services.agent_posts import is_agent_mail, is_agent_teams_post
    from app.services.teams_chat_posts import is_agent_teams_chat_post

    resource = str(notification.get("resource") or "")
    change_type = str(notification.get("changeType") or "")
    filter_config = trigger_filter(getattr(wf, "trigger_config", None))
    signals = bool(filter_config)

    if event in ("new_email", "email_flag_changed"):
        _base, select_fields = poller._mail_listing(wf)
        msg = await _get(
            http, token, resource, {"$select": select_fields + ",lastModifiedDateTime,sentDateTime,isDraft"}
        )
        if msg is None:
            return await _unreadable(redis, wf)
        # ``me/messages`` notifies a draft being saved too: not a mail received.
        if msg.get("isDraft") is True:
            return None, ""
        if await is_agent_mail(redis, integration_id, msg, wf.id):
            return None, ""
        if event == "new_email":
            return poller.microsoft_mail_payload(msg, signals=signals), str(msg.get("receivedDateTime") or "")
        changes = await poller.flag_changes(redis, wf.id, [msg])
        if not changes:
            return None, ""
        return poller.flag_change_payload(msg, changes[0][1], signals=signals), str(
            msg.get("lastModifiedDateTime") or ""
        )

    if event == "calendar_event_changed":
        if change_type == "deleted":
            event_id = str((notification.get("resourceData") or {}).get("id") or resource.rsplit("/", 1)[-1])
            stub = {"id": event_id, "lastModifiedDateTime": datetime.now(UTC).isoformat()}
            return poller.calendar_event_payload(stub, change_type="deleted"), ""
        changed = await _get(http, token, resource, {"$select": poller._CALENDAR_SELECT})
        if changed is None:
            return await _unreadable(redis, wf)
        return poller.calendar_event_payload(changed, change_type=change_type), str(
            changed.get("lastModifiedDateTime") or ""
        )

    if event in _TEAMS_CHANNEL_EVENTS:
        parts = parse_channel_resource(resource)
        if parts is None:
            return None, ""
        team_id, channel_id, message_id, reply_id = parts
        if bool(reply_id) != (event == "new_teams_reply"):
            return None, ""
        path = f"teams/{_segment(team_id)}/channels/{_segment(channel_id)}/messages/{_segment(message_id)}"
        msg = await _get(http, token, path + (f"/replies/{_segment(reply_id)}" if reply_id else ""))
        if msg is None:
            return await _unreadable(redis, wf)
        if msg.get("messageType", "message") != "message":
            return None, ""
        if await is_agent_teams_post(redis, team_id, channel_id, msg.get("id", ""), wf.id):
            return None, ""
        label = f"{team_id}:{channel_id}"
        if reply_id:
            payload = poller.teams_reply_payload(
                msg, team_id=team_id, channel_id=channel_id, channel_name=label, parent_id=message_id, signals=signals
            )
        else:
            name, user_id = poller._graph_author(msg)
            payload = {
                "text": (msg.get("body") or {}).get("content", ""),
                "from_name": name,
                "channel_name": label,
                "team_id": team_id,
                "channel_id": channel_id,
                "created_at": msg.get("createdDateTime", ""),
                "message_id": msg.get("id", ""),
                "trigger_type": "integration_event",
                "event": "new_teams_message",
                "provider": "microsoft",
            }
            if signals:
                payload[poller.FILTER_SIGNALS_KEY] = {"from_id": user_id}
        return payload, str(msg.get("createdDateTime") or "")

    if event == "todo_task_changed":
        todo_parts = parse_todo_resource(resource)
        if todo_parts is None:
            return None, ""
        list_id, task_id = todo_parts
        todo_list = await _get(http, token, f"me/todo/lists/{_segment(list_id)}") or {"id": list_id}
        list_name = str(todo_list.get("displayName") or "")
        if change_type == "deleted":
            stub = {"id": task_id, "lastModifiedDateTime": datetime.now(UTC).isoformat()}
            if await poller.is_own_todo_change(redis, wf, {"id": task_id, "lastModifiedDateTime": "deleted"}):
                return None, ""
            await poller.todo_change_kind(redis, wf.id, stub, deleted=True)
            stub["_list_wellknown"] = str(todo_list.get("wellknownListName") or "")
            return poller.todo_task_payload(
                stub, list_id=list_id, list_name=list_name, change_kind="deleted", signals=signals
            ), ""
        task = await _get(
            http,
            token,
            f"me/todo/lists/{_segment(list_id)}/tasks/{_segment(task_id)}",
            {"$select": poller._TODO_SELECT},
        )
        if task is None:
            return await _unreadable(redis, wf)
        # The state is kept for every change, the agent's own included.
        kind = await poller.todo_change_kind(redis, wf.id, task)
        if await poller.is_own_todo_change(redis, wf, task):
            return None, ""
        task["_list_wellknown"] = str(todo_list.get("wellknownListName") or "")
        return poller.todo_task_payload(
            task, list_id=list_id, list_name=list_name, change_kind=kind, signals=signals
        ), str(task.get("lastModifiedDateTime") or "")

    if event == "new_teams_chat_message":
        parts = parse_chat_resource(resource)
        if parts is None:
            return None, ""
        chat_id, message_id = parts
        msg = await _get(http, token, f"chats/{_segment(chat_id)}/messages/{_segment(message_id)}")
        if msg is None:
            return await _unreadable(redis, wf)
        if msg.get("messageType", "message") != "message":
            return None, ""
        if await is_agent_teams_chat_post(redis, chat_id, msg.get("id", ""), wf.id):
            return None, ""
        return poller.teams_chat_payload(msg, chat_id=chat_id, signals=signals), str(msg.get("createdDateTime") or "")

    return None, ""
