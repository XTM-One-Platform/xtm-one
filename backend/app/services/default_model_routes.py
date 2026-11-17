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

"""The default model routes of the platform and the event raised when one moves.

A route is one default of Settings > AI and models > Models: the agent
default chat model, the routing, fallback and decision models, the image and
the embedding defaults. Each is a provider config (``*_provider_id``) and a
model on ``PlatformSettings``.

``platform.default_model_route.changed`` is raised once per route whose
provider or model changed, whatever wrote it: the Settings page, the
``update_platform_ai_settings`` tool, a provider deletion (an administrator's
or the prune of an env provider) or the boot seed. The change is read from
the ORM at flush (the values before and after the transaction) and published
after the outer commit, so a rolled back write raises nothing and a save that
changes nothing raises nothing. A writer names its cause with
:func:`mark_cause`; an unnamed write is ``system``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_ROUTE_CHANGED_EVENT = "platform.default_model_route.changed"

#: route -> (provider id field, provider key field or None, model field)
ROUTES: dict[str, tuple[str, str | None, str]] = {
    "agent_default": ("agent_default_provider_id", "agent_default_provider", "agent_default_model"),
    "routing": ("routing_provider_id", None, "routing_model"),
    "fallback": ("fallback_provider_id", None, "fallback_model"),
    "decision": ("decision_provider_id", None, "decision_model"),
    "image": ("image_provider_id", "image_provider", "image_model"),
    "embedding": ("embedding_provider_id", "embedding_provider", "embedding_model"),
}

CAUSES = ("settings_page", "agent_tool", "provider_deleted", "env_provider_prune", "system")

_CAUSE_KEY = "default_model_route_cause"
_ACTOR_KEY = "default_model_route_actor"
_PENDING_KEY = "default_model_route_changes"

_publish_tasks: set[asyncio.Task[None]] = set()


def mark_cause(db: Any, cause: str, actor_user_id: Any = None) -> None:
    """Name the cause (one of :data:`CAUSES`) of the route writes of *db*'s transaction.

    The first writer to name it wins: the tool calls the Settings page handler,
    which names ``settings_page`` after the tool named ``agent_tool``.
    """
    if cause not in CAUSES:
        raise ValueError(f"unknown default model route cause {cause!r}")
    # A worker process that never imported app.main still raises the event.
    register_default_route_listeners()
    session = getattr(db, "sync_session", db)
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return  # not an ORM session (a test double): nothing is flushed through it
    if _CAUSE_KEY not in info:
        info[_CAUSE_KEY] = cause
        info[_ACTOR_KEY] = str(actor_user_id) if actor_user_id else None


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def route_state(ps: Any, route: str, provider_key: str | None = None) -> dict[str, str | None]:
    """``{provider_id, provider, model}`` of *route* on *ps* (no secret: ids, keys, model names)."""
    id_field, key_field, model_field = ROUTES[route]
    return {
        "provider_id": _text(getattr(ps, id_field, None)),
        "provider": _text(getattr(ps, key_field, None)) if key_field else provider_key,
        "model": _text(getattr(ps, model_field, None)),
    }


def change_payload(
    route: str,
    before: dict[str, str | None],
    after: dict[str, str | None],
    *,
    cause: str,
    actor_user_id: str | None,
) -> dict[str, Any]:
    """The payload of ``platform.default_model_route.changed``."""
    return {
        "route": route,
        "before": before,
        "after": after,
        "cause": cause,
        "actor_user_id": actor_user_id,
        "object_type": "default_model_route",
        "object_id": route,
        "participant_agent_ids": [],
    }


def _committed_and_current(state: Any, field: str) -> tuple[Any, Any, bool]:
    """(value as loaded, value now, changed) of *field* on an ORM *state*."""
    attr = state.attrs[field]
    history = attr.history
    if not history.has_changes():
        value = history.unchanged[0] if history.unchanged else None
        return value, value, False
    before = history.deleted[0] if history.deleted else None
    after = history.added[0] if history.added else None
    return before, after, True


def _provider_key(session: Session, provider_id: Any) -> str | None:
    if not provider_id:
        return None
    from app.models.llm_config import LLMProviderConfig

    try:
        with session.no_autoflush:
            cfg = session.get(LLMProviderConfig, provider_id)
    except Exception:  # a key is a courtesy of the payload, never a reason to fail a flush
        logger.debug("Provider key of %s not resolved", provider_id, exc_info=True)
        return None
    return getattr(cfg, "provider", None) if cfg is not None else None


def _before_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
    from app.models.platform_settings import PlatformSettings

    for obj in session.dirty:
        if not isinstance(obj, PlatformSettings):
            continue
        state = inspect(obj)
        pending: dict[str, dict[str, Any]] = session.info.setdefault(_PENDING_KEY, {})
        for route, fields in ROUTES.items():
            id_field, key_field, model_field = fields
            values = {f: _committed_and_current(state, f) for f in fields if f}
            if not any(changed for _b, _a, changed in values.values()):
                continue
            before = {
                "provider_id": _text(values[id_field][0]),
                "provider": _text(values[key_field][0]) if key_field else _provider_key(session, values[id_field][0]),
                "model": _text(values[model_field][0]),
            }
            after = {
                "provider_id": _text(values[id_field][1]),
                "provider": _text(values[key_field][1]) if key_field else _provider_key(session, values[id_field][1]),
                "model": _text(values[model_field][1]),
            }
            # Several flushes in one transaction: the first before, the last after.
            entry = pending.setdefault(route, {"before": before})
            entry["after"] = after


def _after_commit(session: Session) -> None:
    with contextlib.suppress(Exception):
        if session.in_nested_transaction():
            return  # a SAVEPOINT release, not the durable commit
    pending = session.info.pop(_PENDING_KEY, None) or {}
    cause = session.info.pop(_CAUSE_KEY, None) or "system"
    actor = session.info.pop(_ACTOR_KEY, None)
    changes = [
        change_payload(route, entry["before"], entry["after"], cause=cause, actor_user_id=actor)
        for route, entry in pending.items()
        if entry["before"]["provider_id"] != entry["after"]["provider_id"]
        or entry["before"]["model"] != entry["after"]["model"]
    ]
    if not changes:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Default model route changes committed outside an event loop: not published")
        return
    from app.services.platform_events import publish_many

    task = loop.create_task(publish_many((DEFAULT_ROUTE_CHANGED_EVENT, c) for c in changes))
    _publish_tasks.add(task)
    task.add_done_callback(_publish_tasks.discard)


def _after_soft_rollback(session: Session, previous_transaction: Any) -> None:
    # An outer rollback drops what its transaction would have raised.
    if previous_transaction.parent is None:
        session.info.pop(_PENDING_KEY, None)
        session.info.pop(_CAUSE_KEY, None)
        session.info.pop(_ACTOR_KEY, None)


def register_default_route_listeners() -> None:
    """Install the listeners on every session. Idempotent (startup may call it twice)."""
    for name, fn in (
        ("before_flush", _before_flush),
        ("after_commit", _after_commit),
        ("after_soft_rollback", _after_soft_rollback),
    ):
        if not event.contains(Session, name, fn):
            event.listen(Session, name, fn)
