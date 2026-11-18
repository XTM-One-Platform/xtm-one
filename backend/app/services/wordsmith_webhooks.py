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

"""Wordsmith webhook endpoints for ``integration_event`` assignments (#160, slice 3).

An enabled assignment with ``{"provider": "wordsmith", "event": <event type>}``
gets a Wordsmith webhook endpoint created under the credential its integration
resolves for the assignment's acting user:

* created with ``POST /webhook_endpoints`` on the integration's receiver
  (``/api/hooks/integrations/{integration_id}``), subscribed to the trigger's
  event type, ``integration`` ``{"platform": "other", "flow_name": <assignment>}``
  and ``audience`` from ``trigger_config.audience`` (``user`` by default,
  ``customer`` for an organisation endpoint, forced for an organisation-only
  event such as ``alert.triggered``); the secret Wordsmith returns once
  is stored encrypted in the state, never shown;
* checked every :data:`RECHECK_SECONDS` (``GET``): a disabled or failing endpoint
  is turned back on, a missing or revoked one is created again;
* deleted when the assignment is disabled, deleted, retargeted or no longer
  reads Wordsmith.

A delivery carries ``Wordsmith-Signature: t=...,v1=...`` (HMAC-SHA256 over
``"{t}.{body}"``, one ``v1`` per active secret while one rotates) and
``Wordsmith-Webhook-Id``: :func:`verify_signature` checks it against the
secret of the endpoint the header names, within :data:`TOLERANCE_SECONDS`.
Events are deduplicated per assignment by ``Wordsmith-Event-Id`` in the seen
set; after a gap the scheduler pass reads ``GET /events`` from the last event
seen (:func:`catch_up`), so a delivery Wordsmith gave up on still runs once.

Events carry IDs only: each run's payload names the read tool that fetches the
content (:func:`follow_up`).

The state lives in ``trigger_config.wordsmith_webhook`` (``endpoint_id``,
``secret_enc``, ``url``, ``event_types``, ``audience``, ``status``, ``error``,
``checked_at``, ``caught_up_at``, ``integration_id``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.security import timing_safe_str_eq

logger = logging.getLogger(__name__)

PROVIDER = "wordsmith"
STATE_KEY = "wordsmith_webhook"
#: Wordsmith signs ``{t}.{body}`` and refuses a timestamp more than 60 s off.
TOLERANCE_SECONDS = 60
#: A created endpoint is checked again this often.
RECHECK_SECONDS = 6 * 3600
#: A creation that failed is tried again this often.
RETRY_SECONDS = 3600
#: The event log is read for missed deliveries this often.
CATCH_UP_SECONDS = 900
#: Pages of ``GET /events`` read per assignment and pass (100 events a page).
CATCH_UP_MAX_PAGES = 5

#: Every Wordsmith event type, as ``GET /event_types`` lists them.
EVENTS: tuple[str, ...] = (
    "operation.succeeded",
    "operation.failed",
    "operation.cancelled",
    "task.created",
    "task.assigned",
    "task.status_changed",
    "task.comment_added",
    "review.completed",
    "input_request.created",
    "input_request.resolved",
    "repository.documents_added",
    "repository.indexing_completed",
    "report.run_completed",
    "alert.triggered",
    "webhook_endpoint.test",
)
#: Organisation endpoints only (``audience`` ``customer``).
ORGANISATION_ONLY = frozenset({"alert.triggered"})
#: One person's work: never on an organisation endpoint.
PERSONAL = frozenset(
    {
        "operation.succeeded",
        "operation.failed",
        "operation.cancelled",
        "input_request.created",
        "input_request.resolved",
    }
)

#: The read tool (and its operation) that fetches what an event names.
_FOLLOW_UP: Mapping[str, str] = {
    "operation": "get_wordsmith_operations operation=get_operation",
    "task": "get_wordsmith_tasks operation=get_task",
    "review": "get_wordsmith_reviews operation=get_review",
    "input_request": "get_wordsmith_sessions operation=get_session",
    "repository": "get_wordsmith_repositories operation=get_repository",
    "report": "get_wordsmith_reports operation=get_report",
    "alert": "get_wordsmith_events operation=list_audit_events",
    "webhook_endpoint": "get_wordsmith_webhook_endpoints operation=get_webhook_endpoint",
}

Call = Callable[[str, str, Any, Mapping[str, Any] | None], Awaitable[tuple[Any, int, str | None]]]


# ── Signature ────────────────────────────────────────────────────────────────


def signature(secret: str, timestamp: int | str, body: bytes) -> str:
    """The ``v1`` value Wordsmith sends for *body* signed at *timestamp*."""
    signed = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None, secrets: list[str], *, now: float | None = None) -> bool:
    """*header* carries a ``v1`` of *body* signed with one of *secrets*, within the tolerance."""
    if not header or not secrets:
        return False
    timestamp = ""
    candidates: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            timestamp = value.strip()
        elif key == "v1" and value.strip():
            candidates.append(value.strip())
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - signed_at) > TOLERANCE_SECONDS or not candidates:
        return False
    expected = [signature(secret, signed_at, body) for secret in secrets if secret]
    # Every pair is compared: no early exit on the first match. The candidate
    # comes from a request header (latin-1 decoded, possibly non-ASCII), so the
    # comparison goes through timing_safe_str_eq, which never raises on it.
    matched = False
    for candidate in candidates:
        for value in expected:
            matched = timing_safe_str_eq(candidate, value) or matched
    return matched


# ── Events ───────────────────────────────────────────────────────────────────


def follow_up(event_type: str) -> str:
    """The read tool that fetches what an event of *event_type* names."""
    return _FOLLOW_UP.get(event_type.split(".", 1)[0], "get_wordsmith_events operation=get_event")


def event_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run payload of one Wordsmith event, ``None`` when it is not one.

    The envelope's IDs and statuses are lifted to the top level so the
    trigger's filter keys (``type``, ``status``, ``agent_id``, ``assignee_id``,
    ``task_id``...) read them; ``data`` is kept whole.
    """
    event_id = event.get("id")
    event_type = event.get("type")
    if not isinstance(event_id, str) or not event_id or not isinstance(event_type, str) or not event_type:
        return None
    resource = event.get("resource") if isinstance(event.get("resource"), Mapping) else {}
    actor = event.get("actor") if isinstance(event.get("actor"), Mapping) else {}
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    payload: dict[str, Any] = {
        key: value for key, value in data.items() if isinstance(value, (str, int, float, bool)) or value is None
    }
    payload.update(
        {
            "provider": PROVIDER,
            "event_id": event_id,
            "message_id": event_id,
            "type": event_type,
            "created_at": event.get("created_at"),
            "resource_type": resource.get("type"),
            "resource_id": resource.get("id"),
            "actor_kind": actor.get("kind"),
            "actor_id": actor.get("id"),
            "data": dict(data),
            "follow_up_tool": follow_up(event_type),
        }
    )
    return payload


def subscribed_types(event_type: str) -> list[str]:
    return [event_type] if event_type in EVENTS else []


def audience_of(config: Mapping[str, Any]) -> str:
    """The endpoint audience: forced to ``customer`` for an organisation-only event, so no form asks for it."""
    if str(config.get("event") or "") in ORGANISATION_ONLY:
        return "customer"
    return "customer" if str(config.get("audience") or "").strip().lower() == "customer" else "user"


def audience_error(event_type: str, audience: str) -> str | None:
    """Why Wordsmith would refuse *event_type* on an endpoint of *audience*."""
    if audience == "customer" and event_type in PERSONAL:
        return f"{event_type} is one person's work: an organisation endpoint cannot receive it"
    if audience != "customer" and event_type in ORGANISATION_ONLY:
        return f"{event_type} reaches organisation endpoints only: set trigger_config.audience to customer"
    return None


def due(state: Any, now: datetime, seconds: int | None = None) -> bool:
    if not isinstance(state, Mapping):
        return True
    try:
        checked = datetime.fromisoformat(str(state.get("checked_at")))
    except ValueError:
        return True
    wait = (
        seconds if seconds is not None else (RECHECK_SECONDS if state.get("status") == "registered" else RETRY_SECONDS)
    )
    return now - checked >= timedelta(seconds=wait)


# ── Endpoint reconciliation (no database) ────────────────────────────────────


async def _delete(call: Call, endpoint_id: Any) -> str | None:
    if not endpoint_id:
        return None
    _resp, status, err = await call("DELETE", f"/webhook_endpoints/{endpoint_id}", None, None)
    return None if status == 404 else err


async def reconcile(
    call: Call,
    state: Mapping[str, Any] | None,
    *,
    url: str,
    event_types: list[str],
    audience: str,
    flow_name: str,
    flow_id: str,
    now: datetime,
    encrypt: Callable[[str], str],
) -> dict[str, Any]:
    """Create, re-enable or replace the endpoint so it matches (*url*, *event_types*, *audience*)."""
    held = dict(state or {})
    endpoint_id = held.get("endpoint_id")
    same = (
        endpoint_id
        and held.get("secret_enc")
        and held.get("url") == url
        and held.get("event_types") == event_types
        and held.get("audience") == audience
    )
    if same:
        resp, status, err = await call("GET", f"/webhook_endpoints/{endpoint_id}", None, None)
        current = resp if isinstance(resp, Mapping) else {}
        if err is None and current.get("status") == "enabled":
            return {**held, "status": "registered", "error": None, "checked_at": now.isoformat()}
        reason = str(current.get("disabled_reason") or "")
        if err is None and reason not in {"credential_revoked", "owner_inactive"}:
            _resp, _status, err = await call("PATCH", f"/webhook_endpoints/{endpoint_id}", {"enabled": True}, None)
            if err is None:
                return {**held, "status": "registered", "error": None, "checked_at": now.isoformat()}
        elif status not in (404, 410) and err is not None:
            # Wordsmith unreachable: keep the endpoint, look again later.
            return {**held, "status": "error", "error": err, "checked_at": now.isoformat()}
    await _delete(call, endpoint_id)
    body = {
        "url": url,
        "event_types": event_types,
        "audience": audience,
        "description": f"XTM One: {flow_name}"[:500],
        "integration": {"platform": "other", "flow_id": flow_id[:200], "flow_name": flow_name[:200]},
        "send_test_event": False,
    }
    resp, _status, err = await call("POST", "/webhook_endpoints", body, None)
    created = resp if isinstance(resp, Mapping) else {}
    secret = str(created.get("secret") or "")
    if err is not None or not created.get("id") or not secret:
        return {
            "status": "error",
            "error": err or "Wordsmith created no webhook endpoint",
            "endpoint_id": None,
            "checked_at": now.isoformat(),
        }
    return {
        "status": "registered",
        "error": None,
        "endpoint_id": str(created["id"]),
        "secret_enc": encrypt(secret),
        "url": url,
        "event_types": event_types,
        "audience": audience,
        "checked_at": now.isoformat(),
        "caught_up_at": now.isoformat(),
    }


async def catch_up(
    call: Call,
    *,
    event_types: list[str],
    audience: str,
    since: str,
    max_pages: int = CATCH_UP_MAX_PAGES,
) -> tuple[list[dict[str, Any]], str | None]:
    """The events of *event_types* since *since*, oldest first, from the 30-day event log."""
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(max_pages):
        query: dict[str, Any] = {"types": ",".join(event_types), "limit": 100}
        if audience == "customer":
            query["audience"] = "customer"
        if cursor:
            query["cursor"] = cursor
        else:
            query["created_after"] = since
        resp, _status, err = await call("GET", "/events", None, query)
        if err is not None:
            return events, err
        page = resp if isinstance(resp, Mapping) else {}
        events.extend(item for item in page.get("items") or [] if isinstance(item, Mapping))
        cursor = page.get("cursor") if isinstance(page.get("cursor"), str) else None
        if not cursor:
            break
    return events, None


# ── Database side ─────────────────────────────────────────────────────────────


async def _connection(db: Any, assignment: Any, agent: Any) -> tuple[Call, str] | str:
    """``(call, integration_id)`` or the reason it cannot be built."""
    from app.integrations.providers.wordsmith import WordsmithClient
    from app.models.integration import Integration
    from app.services.fleet_run_as import NO_RUN_AS_REASON, provisioning_user_id
    from app.services.integration_tools import _get_token
    from app.services.trigger_integration_access import trigger_integration_id

    integration_id = trigger_integration_id(assignment.trigger_config or {}, agent)
    if not integration_id:
        return "no Wordsmith integration bound to the assignment or its agent"
    integration = await db.get(Integration, uuid.UUID(str(integration_id)))
    if integration is None or integration.provider != PROVIDER:
        return f"Wordsmith integration {integration_id} not found"
    user_id = provisioning_user_id(assignment, agent)
    if user_id is None:
        return NO_RUN_AS_REASON
    token = await _get_token(db, str(integration_id), PROVIDER, user_id=str(user_id), integ=integration)
    client = WordsmithClient(token or "")
    if not client.credential:
        return "no Wordsmith credential could be resolved for the integration"

    async def call(method: str, path: str, body: Any, query: Mapping[str, Any] | None) -> tuple[Any, int, str | None]:
        resp, err = await client.request(
            method, path, body=body, query=dict(query) if query else None, action=f"{method} {path.split('?')[0]}"
        )
        status = resp.status_code if resp is not None else 0
        if err or resp is None:
            return None, status, err or "no response"
        try:
            return (resp.json() if resp.content else {}), status, None
        except ValueError:
            return {}, status, None

    return call, str(integration_id)


def _with_state(config: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(config)
    if state is None:
        updated.pop(STATE_KEY, None)
    else:
        updated[STATE_KEY] = state
    return updated


def _wanted_reason(assignment: Any, agent: Any, config: Mapping[str, Any]) -> str | None:
    if not assignment.enabled or not bool(getattr(agent, "enabled", True)):
        return "disabled"
    if config.get("provider") != PROVIDER:
        return "not a Wordsmith trigger"
    event_type = str(config.get("event") or "")
    if event_type not in EVENTS:
        return f"unknown Wordsmith event type {event_type!r}"
    return audience_error(event_type, audience_of(config))


async def provision_one(db: Any, assignment: Any, agent: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Reconcile one assignment's endpoint; writes and returns its new state (``None`` = nothing held)."""
    from app.core.config import settings
    from app.core.security import encrypt_value
    from app.services.jira_webhooks import public_receiver_url

    now = now or datetime.now(UTC)
    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY) if isinstance(config.get(STATE_KEY), dict) else None
    unwanted = _wanted_reason(assignment, agent, config)
    conn = await _connection(db, assignment, agent)
    if unwanted is not None:
        if not state:
            if config.get("provider") == PROVIDER and unwanted != "disabled":
                new_state: dict[str, Any] | None = {"status": "error", "error": unwanted, "checked_at": now.isoformat()}
                assignment.trigger_config = _with_state(config, new_state)
                return new_state
            return None
        if isinstance(conn, str):
            new_state = {**state, "status": "orphaned", "error": conn, "checked_at": now.isoformat()}
        else:
            err = await _delete(conn[0], state.get("endpoint_id"))
            new_state = (
                None if err is None else {**state, "status": "error", "error": err, "checked_at": now.isoformat()}
            )
            if new_state is None and config.get("provider") == PROVIDER and unwanted != "disabled":
                new_state = {"status": "error", "error": unwanted, "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    if isinstance(conn, str):
        new_state = {**(state or {}), "status": "error", "error": conn, "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    call, integration_id = conn
    url = public_receiver_url(settings.base_url, integration_id)
    if url is None:
        await _delete(call, (state or {}).get("endpoint_id"))
        new_state = {
            "status": "error",
            "error": "BASE_URL is not a public https URL Wordsmith can call",
            "checked_at": now.isoformat(),
        }
    else:
        new_state = await reconcile(
            call,
            state,
            url=url,
            event_types=subscribed_types(str(config["event"])),
            audience=audience_of(config),
            flow_name=str(getattr(assignment, "name", "") or "XTM One assignment"),
            flow_id=str(assignment.id),
            now=now,
            encrypt=encrypt_value,
        )
    new_state["integration_id"] = integration_id
    assignment.trigger_config = _with_state(config, new_state)
    return new_state


async def deprovision(db: Any, assignment: Any) -> None:
    """Best-effort removal before an assignment is deleted; never raises."""
    from app.models.agent import Agent

    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY)
    if not isinstance(state, dict) or not state.get("endpoint_id"):
        return
    try:
        agent = await db.get(Agent, assignment.agent_id)
        conn = await _connection(db, assignment, agent)
        if isinstance(conn, str):
            logger.warning("Wordsmith webhook endpoint of assignment %s not removed: %s", assignment.id, conn)
            return
        err = await _delete(conn[0], state["endpoint_id"])
        if err:
            logger.warning("Wordsmith webhook endpoint of assignment %s not removed: %s", assignment.id, err)
    except Exception as exc:
        logger.warning("Wordsmith webhook endpoint of assignment %s not removed: %s", assignment.id, exc)


async def _catch_up_one(db: Any, assignment: Any, agent: Any, now: datetime) -> int:
    """Run the events the endpoint missed since ``caught_up_at``; returns how many ran."""
    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY)
    if not isinstance(state, dict) or state.get("status") != "registered":
        return 0
    if not due({"checked_at": state.get("caught_up_at")}, now, CATCH_UP_SECONDS):
        return 0
    conn = await _connection(db, assignment, agent)
    if isinstance(conn, str):
        return 0
    since = str(state.get("caught_up_at") or (now - timedelta(seconds=CATCH_UP_SECONDS)).isoformat())
    events, err = await catch_up(
        conn[0],
        event_types=list(state.get("event_types") or []),
        audience=str(state.get("audience") or "user"),
        since=since,
    )
    if err is not None:
        logger.info("Wordsmith event log of assignment %s not read: %s", assignment.id, err)
        return 0
    ran = 0
    for event in events:
        ran += len(await _dispatch_event(db, assignment, agent, event, delivery="wordsmith_catch_up"))
    newest = max((str(e.get("created_at") or "") for e in events), default="")
    assignment.trigger_config = _with_state(config, {**state, "caught_up_at": newest or now.isoformat()})
    return ran


async def maintain(db: Any, *, now: datetime | None = None) -> int:
    """Scheduler pass: reconcile every due Wordsmith assignment and catch up; returns how many changed."""
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
        reads_wordsmith = config.get("provider") == PROVIDER
        if not reads_wordsmith and STATE_KEY not in config:
            continue
        try:
            if not (reads_wordsmith and assignment.enabled and not due(config.get(STATE_KEY), now)):
                before = config.get(STATE_KEY) or {}
                after = await provision_one(db, assignment, agent, now=now) or {}
                if before.get("status") != after.get("status") or before.get("endpoint_id") != after.get("endpoint_id"):
                    changed += 1
            await _catch_up_one(db, assignment, agent, now)
        except Exception as exc:
            logger.warning("Wordsmith webhook endpoint of assignment %s not reconciled: %s", assignment.id, exc)
            continue
    await db.commit()
    return changed


def endpoint_secret(assignment: Any) -> str:
    """The stored signing secret of the assignment's endpoint, ``""`` when none."""
    from app.core.security import decrypt_value

    state = (assignment.trigger_config or {}).get(STATE_KEY)
    if not isinstance(state, Mapping) or not state.get("secret_enc"):
        return ""
    try:
        return decrypt_value(str(state["secret_enc"])) or ""
    except Exception:
        return ""


async def _dispatch_event(
    db: Any, assignment: Any, agent: Any, event: Mapping[str, Any], *, delivery: str
) -> list[dict[str, Any]]:
    """Start or queue one run of *assignment* for *event*, once per event id."""
    from fastapi import HTTPException

    from app.core.redis import redis_client
    from app.services.agentic_gate import refusal_code
    from app.services.assignment_engine import trigger_or_queue_assignment
    from app.services.assignment_pause import AssignmentOwnerDeactivatedError
    from app.services.integration_event_filters import event_filter_reason, record_filtered_events, trigger_filter
    from app.services.slack_events import claim_seen, release_seen

    config = assignment.trigger_config or {}
    payload = event_payload(event)
    if payload is None or payload["type"] != config.get("event"):
        return []
    redis = await redis_client()
    msg_id = str(payload["message_id"])
    if not await claim_seen(redis, assignment.id, msg_id):
        return []
    filter_config = trigger_filter(config)
    if filter_config and event_filter_reason(str(config["event"]), payload, filter_config) is not None:
        await record_filtered_events(assignment.id, 1)
        return []
    context = {
        "type": "integration_event",
        "provider": PROVIDER,
        "event": config["event"],
        "integration_id": str((config.get(STATE_KEY) or {}).get("integration_id") or ""),
        "message_id": msg_id,
        "delivery": delivery,
        "follow_up_tool": payload["follow_up_tool"],
        "triggered_at": datetime.now(UTC).isoformat(),
    }
    try:
        result = await trigger_or_queue_assignment(
            assignment, agent, payload, db=db, trigger_context=context, overflow="reject"
        )
    except HTTPException as exc:
        await release_seen(redis, assignment.id, msg_id)
        if refusal_code(exc) is None and not isinstance(exc, AssignmentOwnerDeactivatedError):
            raise
        return []
    except Exception:
        await release_seen(redis, assignment.id, msg_id)
        raise
    if result.status in ("queue_full", "dropped"):
        await release_seen(redis, assignment.id, msg_id)
        return []
    return [{"assignment": assignment.name, "agent": agent.name, "event": config["event"], "run_id": result.run_id}]


async def dispatch_delivery(
    integration_id: str, headers: Mapping[str, str], body: bytes, payload: Any
) -> dict[str, Any]:
    """Verify one webhook delivery against its endpoint's secret, then run each assignment it names.

    Raises 401 when no assignment of the integration holds the endpoint the
    ``Wordsmith-Webhook-Id`` header names, or its secret does not sign the body.
    """
    from fastapi import HTTPException

    from app.core.database import async_session
    from app.services.docusign_connect import provider_assignments

    webhook_id = str(headers.get("wordsmith-webhook-id") or "")
    signature_header = headers.get("wordsmith-signature")
    if not webhook_id or not signature_header:
        raise HTTPException(status_code=401, detail="Missing Wordsmith webhook signature")
    triggered: list[dict[str, Any]] = []
    verified = False
    async with async_session() as db:
        for wf, agent in await provider_assignments(db, integration_id, PROVIDER, EVENTS):
            state = (wf.trigger_config or {}).get(STATE_KEY) or {}
            if str(state.get("endpoint_id") or "") != webhook_id:
                continue
            if not verify_signature(body, signature_header, [endpoint_secret(wf)]):
                continue
            verified = True
            if isinstance(payload, Mapping):
                triggered.extend(await _dispatch_event(db, wf, agent, payload, delivery="wordsmith_webhook"))
    if not verified:
        raise HTTPException(status_code=401, detail="Invalid Wordsmith webhook signature")
    logger.info("Wordsmith webhook delivery for integration %s: %d run(s)", integration_id, len(triggered))
    return {"status": "ok", "triggered": triggered, "count": len(triggered)}
