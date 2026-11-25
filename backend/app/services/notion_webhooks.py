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

"""Notion native webhooks for ``integration_event`` assignments (#2185).

Notion pushes its own events to the subscription an admin creates in the
connection's Webhooks tab, pointing at the integration's receiver
(``/api/hooks/integrations/{integration_id}``). No polling: an event exists
only when Notion delivers it.

* **Handshake.** Notion first POSTs ``{"verification_token": ...}``. The
  receiver keeps it on the integration (``extra_config``
  :data:`TOKEN_FIELD`, encrypted) when none is stored yet, and answers 200;
  the admin reads it on the integration page and pastes it into Notion's
  *Verify* form. A second handshake while a token is stored is refused (401):
  the admin clears the field to verify a new subscription.
* **Signature.** Every event carries ``X-Notion-Signature: sha256=<hex>``, the
  HMAC-SHA256 of the raw body keyed with the verification token
  (:func:`verify_signature`); a delivery without a stored token, or whose
  signature does not match, is refused.
* **Events.** :data:`EVENTS`: ``comment.created``, ``page.created``,
  ``page.properties_updated``, ``page.content_updated``,
  ``data_source.content_updated``. Delivery is at most once with retries
  (``attempt_number`` 1 to 8): each assignment claims the event ``id`` in the
  seen set, so a retried delivery runs once.
* **Filter.** The event envelope (``entity.id``, ``data.page_id``,
  ``data.parent.id``, ``workspace_id``, ``authors.0.type``) plus, for
  ``comment.created``, ``mentions_bot``: whether the comment mentions the
  integration's own bot user, read through the comments API only when a
  trigger filter asks for it (the payload carries ids only).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.security import timing_safe_str_eq

logger = logging.getLogger(__name__)

PROVIDER = "notion"
#: The ``extra_config`` key holding the subscription's verification token.
TOKEN_FIELD = "webhook_verification_token"  # noqa: S105 - a config key name, not a secret
SIGNATURE_HEADER = "x-notion-signature"
#: The Notion event types an ``integration_event`` trigger may name.
EVENTS: tuple[str, ...] = (
    "comment.created",
    "page.created",
    "page.properties_updated",
    "page.content_updated",
    "data_source.content_updated",
)
#: The filter key computed from the comments API for ``comment.created``.
MENTIONS_BOT_KEY = "mentions_bot"
FILTER_KEYS: dict[str, str] = {
    "entity.id": "The id of the page, comment or data source the event is about",
    "entity.type": "The kind of object: page, comment, data_source, block",
    "data.page_id": "The page a comment belongs to (comment.created)",
    "data.parent.id": "The parent page or data source of the object",
    "workspace_id": "The Notion workspace that sent the event",
    "authors.0.type": "Who caused the event: person or bot",
    MENTIONS_BOT_KEY: (
        "comment.created only: true keeps the comments that mention the integration's own bot "
        "(read through the comments API)"
    ),
}

CommentReader = Callable[[str], Awaitable[tuple[Mapping[str, Any] | None, str | None]]]


def signature(token: str, body: bytes) -> str:
    """The ``X-Notion-Signature`` value Notion sends for *body* signed with *token*."""
    return "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None, token: str | None) -> bool:
    """*header* is the signature of *body* with *token* (constant-time); no token refuses."""
    if not token or not header:
        return False
    # The header is request-derived (latin-1 decoded, may be non-ASCII):
    # timing_safe_str_eq compares bytes, so crafted input is a mismatch, not a 500.
    return timing_safe_str_eq(header.strip(), signature(token, body))


def handshake_token(payload: Any) -> str | None:
    """The ``verification_token`` of a subscription handshake, ``None`` for an event."""
    if not isinstance(payload, Mapping) or "type" in payload:
        return None
    token = payload.get("verification_token")
    return token if isinstance(token, str) and token.strip() else None


def stored_token(extra: Mapping[str, Any] | None) -> str | None:
    """The verification token stored on the integration, ``None`` when none is."""
    raw = (extra or {}).get(TOKEN_FIELD)
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def event_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run payload of one Notion event, ``None`` when it is not one this platform reads."""
    kind = event.get("type")
    event_id = event.get("id")
    if kind not in EVENTS or not isinstance(event_id, str) or not event_id:
        return None
    raw_entity = event.get("entity")
    raw_data = event.get("data")
    entity: Mapping[str, Any] = raw_entity if isinstance(raw_entity, Mapping) else {}
    data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else {}
    authors = [dict(a) for a in event.get("authors") or [] if isinstance(a, Mapping)]
    read_tool = "list_notion_comments" if kind == "comment.created" else "get_notion_page"
    if kind == "data_source.content_updated":
        read_tool = "query_notion_database"
    return {
        "provider": PROVIDER,
        "event": kind,
        "message_id": f"notion:{event_id}",
        "id": event_id,
        "timestamp": event.get("timestamp"),
        "workspace_id": event.get("workspace_id"),
        "workspace_name": event.get("workspace_name"),
        "subscription_id": event.get("subscription_id"),
        "attempt_number": event.get("attempt_number"),
        "entity": dict(entity),
        "data": dict(data),
        "authors": authors,
        "read_with": f"Notion sends ids only: read the current state with {read_tool} (entity {entity.get('id')}).",
    }


def comment_mentions(comment: Mapping[str, Any], bot_id: str) -> bool:
    """Whether the comment's rich text mentions the user *bot_id*."""
    want = bot_id.replace("-", "")
    for item in comment.get("rich_text") or []:
        if not isinstance(item, Mapping) or item.get("type") != "mention":
            continue
        raw_mention = item.get("mention")
        mention: Mapping[str, Any] = raw_mention if isinstance(raw_mention, Mapping) else {}
        raw_user = mention.get("user")
        user: Mapping[str, Any] = raw_user if isinstance(raw_user, Mapping) else {}
        if str(user.get("id") or "").replace("-", "") == want:
            return True
    return False


def _needs_mention(filter_config: Mapping[str, Any]) -> bool:
    return MENTIONS_BOT_KEY in filter_config


def _answer(status: str, triggered: list[dict[str, Any]] | None = None, reason: str = "") -> dict[str, Any]:
    answer: dict[str, Any] = {"status": status, "triggered": triggered or [], "count": len(triggered or [])}
    if reason:
        answer["reason"] = reason
    return answer


async def _mention_reader(db: Any, integration_id: str, assignment: Any, agent: Any) -> CommentReader | str:
    """Read a comment and the bot's own user id with the integration's token, or why it cannot."""
    import uuid

    from app.core.egress import egress_client
    from app.integrations.providers.notion._common import NOTION_API, NOTION_VERSION
    from app.models.integration import Integration
    from app.services.fleet_run_as import NO_RUN_AS_REASON, provisioning_user_id
    from app.services.integration_tools import _get_token

    integration = await db.get(Integration, uuid.UUID(str(integration_id)))
    if integration is None or integration.provider != PROVIDER:
        return f"Notion integration {integration_id} not found"
    user_id = provisioning_user_id(assignment, agent)
    if user_id is None:
        return NO_RUN_AS_REASON
    token = await _get_token(db, str(integration_id), PROVIDER, user_id=str(user_id), integ=integration)
    if not token:
        return "no Notion credential could be resolved for the integration"
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}

    async def read(comment_id: str) -> tuple[Mapping[str, Any] | None, str | None]:
        async with egress_client(timeout=15) as client:
            me = await client.get(f"{NOTION_API}/users/me", headers=headers)
            comment = await client.get(f"{NOTION_API}/comments/{comment_id}", headers=headers)
        if me.status_code != 200 or comment.status_code != 200:
            return None, f"Notion answered HTTP {me.status_code}/{comment.status_code}"
        body = comment.json()
        bot_id = str((me.json() or {}).get("id") or "")
        return {"comment": body, "bot_id": bot_id}, None

    return read


async def mentions_bot(read: CommentReader, comment_id: str) -> bool | None:
    """``mentions_bot`` of a comment, ``None`` when it cannot be read."""
    found, err = await read(comment_id)
    if err or not found:
        logger.info("Notion comment %s not read for the mention filter: %s", comment_id, err)
        return None
    comment = found.get("comment")
    bot_id = str(found.get("bot_id") or "")
    if not isinstance(comment, Mapping) or not bot_id:
        return None
    return comment_mentions(comment, bot_id)


async def dispatch_delivery(integration_id: str, payload: Any) -> dict[str, Any]:
    """Start or queue a run per assignment of the delivery's event (the caller verified the signature)."""
    from fastapi import HTTPException

    from app.core.database import async_session
    from app.core.redis import redis_client
    from app.services.agentic_gate import refusal_code
    from app.services.assignment_engine import trigger_or_queue_assignment
    from app.services.assignment_pause import AssignmentOwnerDeactivatedError
    from app.services.docusign_connect import provider_assignments
    from app.services.integration_event_filters import (
        event_filter_reason,
        record_filtered_events,
        trigger_filter,
    )
    from app.services.slack_events import claim_seen, release_seen

    if not isinstance(payload, Mapping):
        return _answer("ignored", reason="not a Notion webhook event (a JSON object)")
    base = event_payload(payload)
    if base is None:
        return _answer("ignored", reason="not a Notion event type this platform reads")
    event = base["event"]
    redis = await redis_client()
    triggered: list[dict[str, Any]] = []
    filtered: dict[Any, int] = {}
    async with async_session() as db:
        for wf, agent in await provider_assignments(db, integration_id, PROVIDER, {event}):
            run_payload = dict(base)
            msg_id = run_payload["message_id"]
            filter_config = trigger_filter(wf.trigger_config or {})
            if filter_config and event == "comment.created" and _needs_mention(filter_config):
                reader = await _mention_reader(db, integration_id, wf, agent)
                flag = None if isinstance(reader, str) else await mentions_bot(reader, str(base["entity"].get("id")))
                run_payload[MENTIONS_BOT_KEY] = flag
            if filter_config and event_filter_reason(event, run_payload, filter_config) is not None:
                filtered[wf.id] = filtered.get(wf.id, 0) + 1
                continue
            if not await claim_seen(redis, wf.id, msg_id):
                continue
            trigger_context = {
                "type": "integration_event",
                "provider": PROVIDER,
                "event": event,
                "integration_id": str(integration_id),
                "message_id": msg_id,
                "delivery": "notion_webhook",
                "triggered_at": datetime.now(UTC).isoformat(),
            }
            try:
                result = await trigger_or_queue_assignment(
                    wf, agent, run_payload, db=db, trigger_context=trigger_context, overflow="reject"
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
            triggered.append({"assignment": wf.name, "agent": agent.name, "event": event, "run_id": result.run_id})
    for assignment_id, count in filtered.items():
        await record_filtered_events(assignment_id, count)
    logger.info("Notion delivery for integration %s (%s): %d run(s)", integration_id, event, len(triggered))
    return _answer("ok", triggered)
