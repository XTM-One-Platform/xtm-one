# SPDX-License-Identifier: Apache-2.0

"""Hooks API – register webhook listeners for internal platform events.

External systems can subscribe to platform events (assignment completions,
agent responses, etc.) via webhook URLs.  When an event fires, the
platform makes an HTTP POST to each registered hook with the event payload.

Admin-only for creation/deletion; hooks are company-wide.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.helpers import parse_path_id
from app.core.database import get_db
from app.models.user import User
from app.services.audit import log_audit
from app.services.event_bus import (
    AGENT_CHAT_RESPONSE,
    AGENT_CREATED,
    AGENT_DELETED,
    AGENT_UPDATED,
    ASSIGNMENT_RUN_COMPLETED,
    ASSIGNMENT_RUN_FAILED,
    ASSIGNMENT_RUN_STARTED,
    CHANNEL_MESSAGE_RECEIVED,
    CHANNEL_MESSAGE_SENT,
    INTEGRATION_CONNECTED,
    INTEGRATION_DISCONNECTED,
    INTEGRATION_ERROR,
    KNOWLEDGE_BASE_DOCUMENT_ADDED,
    KNOWLEDGE_BASE_UPDATED,
    MEMORY_PERSISTED,
    SUB_AGENT_TASK_COMPLETED,
    TOOL_EXECUTED,
    TOOL_FAILED,
    USER_CREATED,
    USER_LOGIN,
    Event,
    event_bus,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── In-memory hook storage ───────────────────────────────────────────
# For a production deployment you'd use a DB table; here we keep it
# lightweight in memory (persisted across restarts via the DB if desired).
# This approach avoids adding another migration for an extensibility
# feature that's easy to evolve later.

_hooks: dict[str, dict] = {}  # hook_id → {url, events, secret, created_at, ...}

# ─── Schemas ──────────────────────────────────────────────────────────


AVAILABLE_EVENTS: list[str] = sorted(
    [
        ASSIGNMENT_RUN_STARTED,
        ASSIGNMENT_RUN_COMPLETED,
        ASSIGNMENT_RUN_FAILED,
        AGENT_CHAT_RESPONSE,
        AGENT_CREATED,
        AGENT_UPDATED,
        AGENT_DELETED,
        INTEGRATION_CONNECTED,
        INTEGRATION_DISCONNECTED,
        INTEGRATION_ERROR,
        KNOWLEDGE_BASE_UPDATED,
        KNOWLEDGE_BASE_DOCUMENT_ADDED,
        CHANNEL_MESSAGE_RECEIVED,
        CHANNEL_MESSAGE_SENT,
        TOOL_EXECUTED,
        TOOL_FAILED,
        USER_LOGIN,
        USER_CREATED,
        MEMORY_PERSISTED,
        SUB_AGENT_TASK_COMPLETED,
    ]
)


class CreateHookRequest(BaseModel):
    url: HttpUrl
    events: list[str]  # list of event names or ["*"] for all
    secret: str | None = None  # optional shared secret for HMAC signing
    description: str | None = None


class HookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    description: str | None = None
    created_at: str


class EventTypeResponse(BaseModel):
    name: str
    category: str


# ─── Routes ───────────────────────────────────────────────────────────


@router.get("/events", response_model=list[EventTypeResponse])
async def list_event_types(
    user: User = Depends(get_current_user),
):
    """List all available event types that can be subscribed to."""
    result: list[EventTypeResponse] = []
    for name in AVAILABLE_EVENTS:
        category = name.split(".")[0] if "." in name else "other"
        result.append(EventTypeResponse(name=name, category=category))
    return result


@router.get("", response_model=list[HookResponse])
async def list_hooks(
    admin: User = Depends(require_admin),
):
    """List all registered webhook hooks (admin-only)."""
    return [
        HookResponse(
            id=hid,
            url=h["url"],
            events=h["events"],
            description=h.get("description"),
            created_at=h["created_at"],
        )
        for hid, h in _hooks.items()
    ]


@router.post("", response_model=HookResponse, status_code=status.HTTP_201_CREATED)
async def create_hook(
    body: CreateHookRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Register a new webhook hook for platform events (admin-only)."""
    # Validate event names
    for ev in body.events:
        if ev != "*" and ev not in AVAILABLE_EVENTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event type: {ev}. Use GET /hooks/events for available types.",
            )

    hook_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    hook = {
        "url": str(body.url),
        "events": body.events,
        "secret": body.secret,
        "description": body.description,
        "created_at": now,
        "created_by": str(admin.id),
    }
    _hooks[hook_id] = hook

    # Register event listeners
    _register_hook_listeners(hook_id, hook)

    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="hook",
        entity_id=uuid.UUID(hook_id),
        entity_name=str(body.url),
        summary=f"Registered webhook hook for events: {', '.join(body.events)}",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return HookResponse(
        id=hook_id,
        url=str(body.url),
        events=body.events,
        description=body.description,
        created_at=now,
    )


@router.delete("/{hook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hook(
    hook_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove a registered webhook hook (admin-only)."""
    if hook_id not in _hooks:
        raise HTTPException(status_code=404, detail="Hook not found")

    hook = _hooks.pop(hook_id)

    # Unregister event listeners
    _unregister_hook_listeners(hook_id, hook)

    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="hook",
        entity_id=parse_path_id(hook_id, not_found_detail="Hook not found"),
        entity_name=hook["url"],
        summary=f"Removed webhook hook for {hook['url']}",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


# ─── Listener management ─────────────────────────────────────────────

_hook_listener_fns: dict[str, list[tuple[str, object]]] = {}  # hook_id → [(event, fn)]
_HOOK_DISABLE_NOT_FOUND_THRESHOLD = 3
_HOOK_BACKOFF_BASE_SECONDS = 2
_HOOK_BACKOFF_MAX_SECONDS = 300
# Cap the backoff exponent. Non-404/410 failures (timeouts, 5xx, connection
# errors) never auto-disable a hook and only reset on success, so the failure
# counter can climb without bound for a permanently-broken target. The delay is
# already clamped to ``_HOOK_BACKOFF_MAX_SECONDS`` once the exponent reaches ~8
# (2 * 2**8 = 512 > 300), so capping it here keeps the shift a small int instead
# of computing an ever-growing big integer on every single event.
_HOOK_BACKOFF_MAX_EXPONENT = 16


def _record_hook_delivery_failure(hook: dict, *, status_code: int | None = None) -> int:
    """Track failure state and return the current consecutive failure count."""
    failures = int(hook.get("consecutive_failures", 0)) + 1
    hook["consecutive_failures"] = failures
    exponent = min(failures - 1, _HOOK_BACKOFF_MAX_EXPONENT)
    delay = min(
        _HOOK_BACKOFF_BASE_SECONDS * (2**exponent), _HOOK_BACKOFF_MAX_SECONDS
    )
    hook["next_attempt_at"] = datetime.now(UTC).timestamp() + delay

    if status_code in (404, 410):
        hook["consecutive_not_found"] = int(hook.get("consecutive_not_found", 0)) + 1
    else:
        hook["consecutive_not_found"] = 0

    return failures


def _reset_hook_delivery_state(hook: dict) -> None:
    hook["consecutive_failures"] = 0
    hook["consecutive_not_found"] = 0
    hook.pop("next_attempt_at", None)


def _register_hook_listeners(hook_id: str, hook: dict) -> None:
    """Create and register async listeners for a hook's subscribed events."""
    entries: list[tuple[str, object]] = []
    url = hook["url"]

    async def _dispatch(event: Event) -> None:
        """Fire-and-forget HTTP POST to the hook URL.

        Deliveries are serialized per hook via an ``asyncio.Lock`` held in the
        hook state. ``event_bus.emit(..., wait=False)`` schedules each listener
        as its own task, so a burst of events would otherwise spawn several
        concurrent ``_dispatch`` tasks for the same hook that all read the
        backoff gate *before* any of them records a failure — firing the very
        request storm this module exists to prevent. Holding the lock across the
        gate check + HTTP call + state update guarantees a single in-flight
        delivery per hook, and tasks that queued behind a failed delivery
        re-evaluate the (now-updated) backoff gate and skip.
        """
        active_hook = _hooks.get(hook_id)
        if not active_hook:
            return

        # Lazily create the per-hook lock. No extra guarding is needed: asyncio
        # is single-threaded and there is no ``await`` between the ``get`` and
        # the assignment, so concurrent tasks cannot create rival locks.
        lock = active_hook.get("_lock")
        if lock is None:
            lock = asyncio.Lock()
            active_hook["_lock"] = lock

        async with lock:
            # Re-fetch under the lock: the hook may have been disabled/removed
            # while we waited behind another (failed) delivery.
            active_hook = _hooks.get(hook_id)
            if not active_hook:
                return

            next_attempt_at = active_hook.get("next_attempt_at")
            if next_attempt_at and datetime.now(UTC).timestamp() < float(
                next_attempt_at
            ):
                return

            payload = {
                "event": event.name,
                "timestamp": event.timestamp.isoformat(),
                "data": event.data,
            }
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    headers = {"Content-Type": "application/json"}
                    if active_hook.get("secret"):
                        import hashlib
                        import hmac
                        import json

                        body_bytes = json.dumps(payload, default=str).encode()
                        sig = hmac.new(
                            active_hook["secret"].encode(),
                            body_bytes,
                            hashlib.sha256,
                        ).hexdigest()
                        headers["X-Hook-Signature"] = f"sha256={sig}"
                    resp = await client.post(url, json=payload, headers=headers)
                if 200 <= resp.status_code < 300:
                    _reset_hook_delivery_state(active_hook)
                    return

                failures = _record_hook_delivery_failure(
                    active_hook, status_code=resp.status_code
                )
                not_found_failures = int(active_hook.get("consecutive_not_found", 0))
                if not_found_failures >= _HOOK_DISABLE_NOT_FOUND_THRESHOLD:
                    logger.warning(
                        "Disabling hook %s after %d consecutive %d responses from %s",
                        hook_id,
                        not_found_failures,
                        resp.status_code,
                        url,
                    )
                    _hooks.pop(hook_id, None)
                    _unregister_hook_listeners(hook_id, active_hook)
                    return
                logger.warning(
                    "Hook delivery to %s returned HTTP %d (failure #%d, backing off)",
                    url,
                    resp.status_code,
                    failures,
                )
            except Exception:
                _record_hook_delivery_failure(active_hook)
                logger.warning(
                    "Failed to deliver hook event '%s' to %s",
                    event.name,
                    url,
                    exc_info=True,
                )

    for event_name in hook["events"]:
        event_bus.add_listener(event_name, _dispatch)
        entries.append((event_name, _dispatch))

    _hook_listener_fns[hook_id] = entries


def _unregister_hook_listeners(hook_id: str, hook: dict) -> None:
    """Remove all listeners for a hook."""
    entries = _hook_listener_fns.pop(hook_id, [])
    for event_name, fn in entries:
        event_bus.remove_listener(event_name, fn)
