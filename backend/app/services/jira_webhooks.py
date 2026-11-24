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

"""Jira dynamic webhooks for ``integration_event`` assignments (#11036, slice 2).

The poll (``jira_events``, every 60 s) reads every Jira event. On top of it, an
enabled assignment with ``{"provider": "jira"}`` gets a Jira dynamic webhook
(OAuth 2.0 apps, ``manage:jira-webhook``) registered under the account its
integration is connected as, so Jira calls XTM One the moment the change is
made:

* registered with ``POST /rest/api/3/webhook`` on the integration's receiver
  (``/api/hooks/integrations/{integration_id}``), with the Jira events the
  trigger's event needs (:func:`webhook_events`) and a JQL filter derived from
  the trigger's filter (:func:`webhook_jql`: only the clauses Jira evaluates on
  a webhook, ``project``, ``issuetype``, ``status``, ``assignee``, with ``IN``);
* refreshed (``PUT /rest/api/3/webhook/refresh``) once less than
  :data:`REFRESH_BEFORE` is left of its 30-day life;
* deleted (``DELETE /rest/api/3/webhook``) when the assignment is disabled,
  deleted, retargeted or no longer reads Jira.

A filter Jira cannot narrow on a webhook (no project, assignee, type or status)
registers nothing: the webhook would deliver every change of the site. The
filter's free ``jql`` is not part of the webhook: a delivery is checked against
it by one search on the issue key (:func:`jql_matches`) before any run. A
registration Jira refuses keeps the poll, with the reason in the state.

A delivery carries ``Authorization: Bearer <JWT>`` signed (HS256) with the
OAuth app's client secret (:func:`verify_delivery`). It is mapped to the
payloads the poll builds (:func:`delivery_events`, through
``jira_events.issue_events``), with the same ``message_id``: the assignment's
seen set runs each change once, whichever path reads it first. Once a delivery
reached an assignment, its poll slows to :data:`SAFETY_POLL_SECONDS`
(:func:`poll_interval`), a safety net for a missed delivery.

The state lives in ``trigger_config.jira_webhook`` (``webhook_ids``, ``jql``,
``events``, ``expires_at``, ``status``, ``error``, ``checked_at``,
``integration_id``).
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from app.services import jira_events

logger = logging.getLogger(__name__)

STATE_KEY = "jira_webhook"

#: Jira keeps a dynamic webhook 30 days; it is refreshed this long before.
REFRESH_BEFORE = timedelta(days=7)
#: A registered webhook is checked again this often (refresh, filter change).
RECHECK_SECONDS = 6 * 3600
#: A registration that failed is tried again this often.
RETRY_SECONDS = 3600
#: The poll interval of an assignment a delivery reached within the last day.
SAFETY_POLL_SECONDS = 900
#: How long a delivery marks an assignment as served by its webhook.
DELIVERED_TTL_SECONDS = 86400

_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan")

#: The Jira webhook events each trigger event needs.
_WEBHOOK_EVENTS: Mapping[str, tuple[str, ...]] = {
    jira_events.ISSUE_CREATED: ("jira:issue_created",),
    jira_events.ISSUE_ASSIGNED: ("jira:issue_updated",),
    jira_events.ISSUE_UPDATED: ("jira:issue_updated",),
    jira_events.ISSUE_TRANSITIONED: ("jira:issue_updated",),
    jira_events.COMMENT_CREATED: ("comment_created",),
}

Call = Callable[[str, str, Any], Awaitable[tuple[Any, str | None]]]


def delivered_key(assignment_id: Any) -> str:
    return f"jira_webhook_delivered:{assignment_id}"


def webhook_events(event_type: str) -> list[str]:
    return list(_WEBHOOK_EVENTS.get(event_type, ()))


def webhook_jql(filter_config: Mapping[str, Any] | None, *, self_account_id: str = "") -> str | None:
    """The webhook's JQL filter, ``None`` when the filter does not narrow the site.

    Only the clauses Jira evaluates on a dynamic webhook; ``jql`` (free JQL)
    stays a poll-only filter. ``self`` needs the connected account's id.
    """
    config = filter_config if isinstance(filter_config, Mapping) else {}
    clauses: list[str] = []
    assignees = jira_events._strings(config.get("assignee_account_id"))
    if assignees:
        named = [a for a in assignees if a.casefold() != jira_events.SELF]
        if len(named) < len(assignees):
            if not self_account_id:
                return None
            named.append(self_account_id)
        clauses.append(jira_events._jql_in("assignee", named))
    for key, field in (("project_key", "project"), ("issue_type", "issuetype"), ("status", "status")):
        values = jira_events._strings(config.get(key))
        if values:
            clauses.append(jira_events._jql_in(field, values))
    return " AND ".join(clauses) or None


def public_receiver_url(base_url: str, integration_id: str) -> str | None:
    """The receiver Jira posts to, ``None`` when Jira could not reach ``BASE_URL``."""
    base = (base_url or "").strip().rstrip("/")
    parsed = urlsplit(base)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
        return None
    return f"{base}/api/hooks/integrations/{integration_id}"


def verify_delivery(authorization: str | None, client_secret: str) -> bool:
    """The delivery's bearer JWT is signed (HS256) with the OAuth client secret and not expired."""
    import jwt

    from app.core.security import JWT_CLOCK_SKEW_LEEWAY

    if not client_secret or not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return False
    try:
        jwt.decode(
            token.strip(),
            client_secret,
            algorithms=["HS256"],
            options={"verify_aud": False, "require": ["exp"]},
            leeway=JWT_CLOCK_SKEW_LEEWAY,
        )
    except jwt.PyJWTError:
        return False
    return True


def _iso(value: Any) -> str:
    """A webhook timestamp (epoch milliseconds) in the format the poll's payloads carry."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    return str(value or "")


def delivery_events(payload: Mapping[str, Any], event_type: str) -> list[dict[str, Any]]:
    """The poll's payloads of *event_type* one webhook delivery carries (same ``message_id``)."""
    issue = payload.get("issue")
    if not isinstance(issue, Mapping) or not issue.get("key"):
        return []
    webhook_event = str(payload.get("webhookEvent") or "")
    if webhook_event not in webhook_events(event_type):
        return []
    fields = dict(issue.get("fields") or {}) if isinstance(issue.get("fields"), Mapping) else {}
    at = _iso(payload.get("timestamp"))
    shaped: dict[str, Any] = {"key": issue.get("key"), "id": issue.get("id"), "self": issue.get("self")}
    comment = payload.get("comment")
    if event_type == jira_events.COMMENT_CREATED:
        if not isinstance(comment, Mapping):
            return []
        fields["comment"] = {"comments": [comment]}
    changelog = payload.get("changelog")
    if isinstance(changelog, Mapping) and changelog.get("id"):
        shaped["changelog"] = {
            "histories": [
                {
                    "id": changelog.get("id"),
                    "created": at,
                    "author": payload.get("user"),
                    "items": changelog.get("items") or [],
                }
            ]
        }
    shaped["fields"] = fields
    return jira_events.issue_events(shaped, event_type, since=datetime.min.replace(tzinfo=UTC))


def poll_interval(default: int, delivered: bool) -> int:
    """The poll interval of a Jira assignment: slowed once its webhook delivers."""
    return max(default, SAFETY_POLL_SECONDS) if delivered else default


def due(state: Any, now: datetime) -> bool:
    if not isinstance(state, Mapping):
        return True
    try:
        checked = datetime.fromisoformat(str(state.get("checked_at")))
    except ValueError:
        return True
    wait = RECHECK_SECONDS if state.get("status") == "registered" else RETRY_SECONDS
    return now - checked >= timedelta(seconds=wait)


async def _delete(call: Call, ids: list[Any]) -> str | None:
    if not ids:
        return None
    _resp, err = await call("delete", "webhook", {"webhookIds": ids})
    return err


async def reconcile(
    call: Call,
    state: Mapping[str, Any] | None,
    *,
    url: str,
    jql: str,
    events: list[str],
    now: datetime,
) -> dict[str, Any]:
    """Register, refresh or replace the webhook so it matches (*url*, *jql*, *events*)."""
    held = dict(state or {})
    ids = list(held.get("webhook_ids") or [])
    same = ids and held.get("url") == url and held.get("jql") == jql and held.get("events") == events
    if same and held.get("status") == "registered":
        try:
            expires = datetime.fromisoformat(str(held.get("expires_at")))
        except ValueError:
            expires = now
        if expires - now > REFRESH_BEFORE:
            return {**held, "checked_at": now.isoformat()}
        resp, err = await call("put", "webhook/refresh", {"webhookIds": ids})
        if err is None and isinstance(resp, Mapping) and resp.get("expirationDate"):
            return {
                **held,
                "expires_at": _iso_expiry(resp["expirationDate"], now),
                "error": None,
                "checked_at": now.isoformat(),
            }
        # Refused (deleted on the Jira side, expired past recovery): registered again below.
    await _delete(call, ids)
    resp, err = await call("post", "webhook", {"url": url, "webhooks": [{"jqlFilter": jql, "events": events}]})
    results = resp.get("webhookRegistrationResult") if isinstance(resp, Mapping) else None
    created = [r.get("createdWebhookId") for r in results or [] if isinstance(r, Mapping) and r.get("createdWebhookId")]
    if err is not None or not created:
        errors = [e for r in results or [] if isinstance(r, Mapping) for e in r.get("errors") or []]
        reason = err or "; ".join(str(e) for e in errors) or "Jira registered no webhook"
        return {"status": "error", "error": reason, "webhook_ids": [], "checked_at": now.isoformat()}
    return {
        "status": "registered",
        "error": None,
        "webhook_ids": created,
        "url": url,
        "jql": jql,
        "events": events,
        "expires_at": (now + timedelta(days=30)).isoformat(),
        "checked_at": now.isoformat(),
    }


def _iso_expiry(value: Any, now: datetime) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    parsed = jira_events.parse_instant(value)
    return (parsed if parsed.year > 1 else now + timedelta(days=30)).isoformat()


# ── Database side ─────────────────────────────────────────────────────────────


async def _connection(db: Any, assignment: Any, agent: Any) -> tuple[Call, str, str] | str:
    """``(call, integration_id, self_account_id)`` or the reason it cannot be built."""
    from app.integrations.providers.jira._common import api3, jira_headers, jira_request, parse_jira_token
    from app.models.integration import Integration
    from app.services.fleet_run_as import NO_RUN_AS_REASON, provisioning_user_id
    from app.services.integration_tools import _get_token
    from app.services.trigger_integration_access import trigger_integration_id

    integration_id = trigger_integration_id(assignment.trigger_config or {}, agent)
    if not integration_id:
        return "no Jira integration bound to the assignment or its agent"
    integration = await db.get(Integration, uuid.UUID(str(integration_id)))
    if integration is None or integration.provider != jira_events.PROVIDER:
        return f"Jira integration {integration_id} not found"
    if getattr(integration, "auth_type", "oauth") != "oauth":
        return "dynamic webhooks need the Jira OAuth 2.0 connection"
    user_id = provisioning_user_id(assignment, agent)
    if user_id is None:
        return NO_RUN_AS_REASON
    token = await _get_token(db, str(integration_id), jira_events.PROVIDER, user_id=str(user_id), integ=integration)
    base, access = parse_jira_token(token or "")
    if not base or not access:
        return "no Jira token could be resolved for the integration"
    headers = jira_headers(access)

    async def call(method: str, path: str, body: Any) -> tuple[Any, str | None]:
        resp, err = await jira_request(
            method, f"{api3(base)}/{path}", f"{method} the webhook", headers=headers, json=body
        )
        if err or resp is None:
            return None, err or "no response"
        return (resp.json() if resp.content else {}), None

    me = await jira_events.self_account_id(base, headers)
    return call, str(integration_id), me


def _with_state(config: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(config)
    if state is None:
        updated.pop(STATE_KEY, None)
    else:
        updated[STATE_KEY] = state
    return updated


async def provision_one(
    db: Any, assignment: Any, agent: Any, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Reconcile one assignment's webhook; writes and returns its new state (``None`` = nothing held)."""
    from app.core.config import settings

    now = now or datetime.now(UTC)
    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY) if isinstance(config.get(STATE_KEY), dict) else None
    event_type = str(config.get("event") or "")
    wanted = (
        settings.integration_event_jira_webhooks_enabled
        and assignment.enabled
        and bool(getattr(agent, "enabled", True))
        and config.get("provider") == jira_events.PROVIDER
        and bool(webhook_events(event_type))
    )
    conn = await _connection(db, assignment, agent)
    if not wanted:
        if not state:
            return None
        new_state: dict[str, Any] | None
        if isinstance(conn, str):
            new_state = {**state, "status": "orphaned", "error": conn, "checked_at": now.isoformat()}
        else:
            err = await _delete(conn[0], list(state.get("webhook_ids") or []))
            new_state = (
                None if err is None else {**state, "status": "error", "error": err, "checked_at": now.isoformat()}
            )
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    if isinstance(conn, str):
        new_state = {**(state or {}), "status": "error", "error": conn, "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    call, integration_id, me = conn
    from app.services.integration_event_filters import trigger_filter

    url = public_receiver_url(settings.base_url, integration_id)
    jql = webhook_jql(trigger_filter(config), self_account_id=me)
    if url is None or jql is None:
        reason = (
            "BASE_URL is not a public https URL Jira can call"
            if url is None
            else "the filter does not narrow the site (assignee, project, issue type or status): polled only"
        )
        await _delete(call, list((state or {}).get("webhook_ids") or []))
        new_state = {"status": "polled", "error": reason, "webhook_ids": [], "checked_at": now.isoformat()}
    else:
        new_state = await reconcile(call, state, url=url, jql=jql, events=webhook_events(event_type), now=now)
    new_state["integration_id"] = integration_id
    new_state["account_id"] = me
    assignment.trigger_config = _with_state(config, new_state)
    return new_state


async def deprovision(db: Any, assignment: Any) -> None:
    """Best-effort removal before an assignment is deleted; never raises."""
    from app.models.agent import Agent

    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY)
    if not isinstance(state, dict) or not state.get("webhook_ids"):
        return
    try:
        agent = await db.get(Agent, assignment.agent_id)
        conn = await _connection(db, assignment, agent)
        if isinstance(conn, str):
            logger.warning("Jira webhook of assignment %s not removed: %s", assignment.id, conn)
            return
        err = await _delete(conn[0], list(state["webhook_ids"]))
        if err:
            logger.warning("Jira webhook of assignment %s not removed: %s", assignment.id, err)
    except Exception as exc:
        logger.warning("Jira webhook of assignment %s not removed: %s", assignment.id, exc)


async def maintain(db: Any, *, now: datetime | None = None) -> int:
    """Scheduler pass: reconcile every due Jira assignment; returns how many changed."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.assignment import Assignment

    now = now or datetime.now(UTC)
    rows = (
        await db.execute(
            select(Assignment, Agent)
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(Assignment.trigger_type == "integration_event")
        )
    ).all()
    changed = 0
    for assignment, agent in rows:
        config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
        reads_jira = config.get("provider") == jira_events.PROVIDER
        if not reads_jira and STATE_KEY not in config:
            continue
        if reads_jira and assignment.enabled and not due(config.get(STATE_KEY), now):
            continue
        before = config.get(STATE_KEY)
        try:
            after = await provision_one(db, assignment, agent, now=now)
        except Exception as exc:
            logger.warning("Jira webhook of assignment %s not reconciled: %s", assignment.id, exc)
            continue
        if (before or {}).get("status") != (after or {}).get("status") or (before or {}).get("webhook_ids") != (
            after or {}
        ).get("webhook_ids"):
            changed += 1
    await db.commit()
    return changed


async def jql_matches(call: Call, issue_key: str, jql: str) -> bool | None:
    """Whether the issue matches the filter's free *jql*; ``None`` when Jira could not say.

    A webhook is registered with the structured clauses only (:func:`webhook_jql`),
    so a delivery is checked against the free JQL by one search on the issue key.
    """
    if not issue_key:
        return None
    body = {"jql": f"issuekey = {jira_events._jql_quote(issue_key)} AND ({jql})", "fields": ["id"], "maxResults": 1}
    resp, err = await call("post", "search/jql", body)
    if err or not isinstance(resp, Mapping):
        return None
    return bool(resp.get("issues"))


async def _delivery_jql_verdict(db: Any, assignment: Any, agent: Any, issue_key: str, jql: str) -> bool | None:
    conn = await _connection(db, assignment, agent)
    if isinstance(conn, str):
        logger.warning("Jira delivery for assignment %s left to the poll: %s", assignment.id, conn)
        return None
    verdict = await jql_matches(conn[0], issue_key, jql)
    if verdict is None:
        logger.warning("Jira delivery for assignment %s left to the poll: the jql check failed", assignment.id)
    return verdict


async def dispatch_delivery(integration_id: str, payload: Any) -> dict[str, Any]:
    """Start or queue a run per Jira assignment for each event a verified delivery carries.

    The caller verified the bearer JWT (:func:`verify_delivery`). The
    assignments are those the poll reads for the integration
    (``docusign_connect.connect_assignments``' rule), narrowed to the ones whose
    webhook Jira matched when the delivery names them (``matchedWebhookIds``).
    A filter's free ``jql``, which the webhook cannot carry, is checked by one
    search on the issue (:func:`jql_matches`): no match counts the events as
    filtered; a check that fails leaves the change to the poll, which applies it.
    """
    from fastapi import HTTPException

    from app.core.database import async_session
    from app.core.redis import redis_client
    from app.services.agentic_gate import refusal_code
    from app.services.assignment_engine import trigger_or_queue_assignment
    from app.services.assignment_pause import AssignmentOwnerDeactivatedError
    from app.services.docusign_connect import provider_assignments
    from app.services.integration_event_filters import (
        FILTER_SIGNALS_KEY,
        event_filter_reason,
        record_filtered_events,
        trigger_filter,
        without_filter_signals,
    )
    from app.services.slack_events import claim_seen, release_seen

    if not isinstance(payload, Mapping) or not isinstance(payload.get("issue"), Mapping):
        return {"status": "ignored", "reason": "not a Jira issue webhook delivery", "triggered": [], "count": 0}
    # A CTEM case whose ticket this is moves at once (#917); the sync pull is the safety net.
    from app.services import ctem_ticketing

    await ctem_ticketing.apply_jira_delivery(integration_id, dict(payload))
    matched = {str(i) for i in payload.get("matchedWebhookIds") or []}
    redis = await redis_client()
    triggered: list[dict[str, Any]] = []
    filtered: dict[Any, int] = {}
    async with async_session() as db:
        for wf, agent in await provider_assignments(db, integration_id, jira_events.PROVIDER, jira_events.EVENTS):
            config = wf.trigger_config or {}
            held = {str(i) for i in (config.get(STATE_KEY) or {}).get("webhook_ids") or []}
            if matched and not matched & held:
                continue
            event_type = config["event"]
            filter_config = trigger_filter(config)
            # The connected account, read by the provisioning pass (``GET /myself``).
            me = str((config.get(STATE_KEY) or {}).get("account_id") or "")
            events = delivery_events(payload, event_type)
            extra_jql = str((filter_config or {}).get("jql") or "").strip()
            if events and extra_jql:
                issue_key = str(payload["issue"].get("key") or "")
                verdict = await _delivery_jql_verdict(db, wf, agent, issue_key, extra_jql)
                if verdict is None:
                    continue
                if not verdict:
                    await redis.set(delivered_key(wf.id), "1", ex=DELIVERED_TTL_SECONDS)
                    # Claimed like every other filtered event, so the poll never re-emits
                    # a change the free jql rejected once the issue matches later.
                    for event_payload in events:
                        if await claim_seen(redis, wf.id, event_payload["message_id"]):
                            filtered[wf.id] = filtered.get(wf.id, 0) + 1
                    continue
            for event_payload in events:
                msg_id = event_payload["message_id"]
                await redis.set(delivered_key(wf.id), "1", ex=DELIVERED_TTL_SECONDS)
                if getattr(wf, "ignore_self_triggered_events", False) and jira_events.is_self_change(event_payload, me):
                    continue
                if not await claim_seen(redis, wf.id, msg_id):
                    continue
                if me:
                    event_payload[FILTER_SIGNALS_KEY] = {"jira_self_account_id": me}
                if filter_config and event_filter_reason(event_type, event_payload, filter_config) is not None:
                    filtered[wf.id] = filtered.get(wf.id, 0) + 1
                    without_filter_signals(event_payload)
                    continue
                without_filter_signals(event_payload)
                context = {
                    "type": "integration_event",
                    "provider": jira_events.PROVIDER,
                    "event": event_type,
                    "integration_id": str(integration_id),
                    "message_id": msg_id,
                    "delivery": "jira_webhook",
                    "triggered_at": datetime.now(UTC).isoformat(),
                }
                try:
                    result = await trigger_or_queue_assignment(
                        wf, agent, event_payload, db=db, trigger_context=context, overflow="reject"
                    )
                except HTTPException as exc:
                    await release_seen(redis, wf.id, msg_id)
                    if refusal_code(exc) is None and not isinstance(exc, AssignmentOwnerDeactivatedError):
                        raise
                    continue
                except ValueError:
                    await release_seen(redis, wf.id, msg_id)
                    continue
                except Exception:
                    await release_seen(redis, wf.id, msg_id)
                    raise
                if result.status in ("queue_full", "dropped"):
                    await release_seen(redis, wf.id, msg_id)
                    continue
                triggered.append(
                    {"assignment": wf.name, "agent": agent.name, "event": event_type, "run_id": result.run_id}
                )
    for assignment_id, count in filtered.items():
        await record_filtered_events(assignment_id, count)
    logger.info("Jira webhook delivery for integration %s: %d run(s)", integration_id, len(triggered))
    return {"status": "ok", "triggered": triggered, "count": len(triggered)}
