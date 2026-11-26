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

"""Drata workflow webhooks for ``integration_event`` assignments (#2185, Drata slice).

Drata has no event subscription API: its events leave through the **Send
webhook** step of a workflow (Workflows > Create workflow, a Control, Evidence,
Risk or Personnel trigger). A Drata administrator points that step at the
integration's webhook URL (``POST /api/hooks/integrations/{integration_id}``),
adds the custom header ``Authorization: Bearer <token>`` with the integration's
``webhook_token`` and selects the properties the body carries. Drata delivers
through Svix, whose signing secret Drata keeps, so the bearer token is the
proof: a delivery without it, or with another, is refused (401), and an
integration with no token refuses every delivery.

The body is flat: the base properties ``event_type``, ``timestamp``,
``organization_id`` and ``workspace``, then the object's properties
(``control_*``, ``evidence_*``, ``risk_*``, ``personnel_*``) and the trigger's
own. One trigger event per object family (``control_changed``,
``evidence_changed``, ``risk_changed``, ``personnel_changed``); the filter key
``event_type`` narrows it to Drata's trigger names. A retried delivery keeps
its ``svix-id``, the run's message id, so it runs once.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

PROVIDER = "drata"
#: The integration's extra field holding the bearer token of the Send webhook step.
TOKEN_FIELD = "webhook_token"  # noqa: S105 - the field name, not a secret
#: Svix's delivery id, kept across the retries of one message.
MESSAGE_ID_HEADER = "svix-id"

logger = logging.getLogger(__name__)

#: trigger event -> (Drata trigger names, label, what happened).
EVENT_SPECS: dict[str, tuple[frozenset[str], str, str]] = {
    "control_changed": (
        frozenset(
            {
                "CONTROL_DETAILS_UPDATED",
                "CONTROL_READINESS_CHANGED_TO_NOT_READY",
                "CONTROL_READINESS_CHANGED_TO_READY",
                "CONTROL_EVIDENCE_LINKED",
                "CONTROL_EVIDENCE_UNLINKED",
                "CONTROL_POLICY_LINKED",
                "CONTROL_POLICY_UNLINKED",
                "CONTROL_TEST_MAPPED",
                "CONTROL_TEST_UNMAPPED",
                "CONTROL_REQUIREMENT_MAPPED",
                "CONTROL_REQUIREMENT_UNMAPPED",
                "CONTROL_TEST_CHANGED_TO_PASS",
                "CONTROL_TEST_CHANGED_TO_FAIL",
                "CONTROL_OWNER_UPDATED",
                "CONTROL_MARKED_IN_SCOPE",
                "CONTROL_MARKED_OUT_OF_SCOPE",
            }
        ),
        "Drata control changed",
        "a Drata workflow on a control (readiness, evidence, policy, test, requirement, owner or scope) sends its webhook",
    ),
    "evidence_changed": (
        frozenset(
            {
                "NEW_ARTIFACT_UPLOADED",
                "LINKED_TO_CONTROL",
                "UNLINKED_FROM_CONTROL",
                "RENEWAL_PAST_DUE",
                "UPCOMING_RENEWAL",
            }
        ),
        "Drata evidence changed",
        "a Drata workflow on evidence (new artifact, control link or unlink, renewal due or past due) sends its webhook",
    ),
    "risk_changed": (
        frozenset({"INHERENT_SCORE_UPDATED", "RESIDUAL_SCORE_UPDATED", "TREATMENT_OPTION_UPDATED"}),
        "Drata risk changed",
        "a Drata workflow on a risk (inherent or residual score, treatment option) sends its webhook",
    ),
    "personnel_changed": (
        frozenset({"OUT_OF_COMPLIANCE", "STATUS_CHANGED"}),
        "Drata personnel changed",
        "a Drata workflow on personnel (out of compliance, employment status) sends its webhook",
    ),
}
EVENTS = frozenset(EVENT_SPECS)
_BY_DRATA_EVENT = {name: event for event, (names, _label, _what) in EVENT_SPECS.items() for name in names}
#: The property prefix of each family, the fallback when Drata adds a trigger name.
_BY_PREFIX = {"control_": "control_changed", "evidence_": "evidence_changed", "risk_": "risk_changed"}

FILTER_KEYS: dict[str, str] = {
    "event_type": "Drata's trigger name (CONTROL_EVIDENCE_LINKED, RENEWAL_PAST_DUE, STATUS_CHANGED...)",
    "organization_id": "The Drata organization that sent the event",
    "workspace.id": "The Drata workspace the object belongs to",
}

DESCRIPTION_SUFFIX = (
    "Delivered by the Send webhook step of a Drata workflow, with the integration's webhook token as "
    "an Authorization: Bearer header. Carries event_type and the properties the step selects. Each delivery runs once."
)


def catalog_events() -> list[dict[str, Any]]:
    """The ``available_events`` of the ``drata`` catalog entry."""
    return [
        {
            "event": event,
            "label": label,
            "description": f"Triggers when {what}. {DESCRIPTION_SUFFIX}",
            "mechanism": "webhook",
            "filter_keys": dict(FILTER_KEYS),
        }
        for event, (_names, label, what) in EVENT_SPECS.items()
    ]


def event_of(payload: Mapping[str, Any]) -> str | None:
    """The trigger event of a delivery: its ``event_type``, else the family of its properties."""
    name = str(payload.get("event_type") or "").strip().upper()
    if name in _BY_DRATA_EVENT:
        return _BY_DRATA_EVENT[name]
    if name.startswith("CONTROL_"):
        return "control_changed"
    keys = [str(key) for key in payload]
    if any(key.startswith("personnel_") for key in keys):
        return "personnel_changed"
    for prefix, event in _BY_PREFIX.items():
        if any(key.startswith(prefix) for key in keys):
            return event
    return None


def webhook_tokens(extra: Mapping[str, Any]) -> list[str]:
    """The configured webhook tokens (several separated by commas while rotating)."""
    raw = extra.get(TOKEN_FIELD) if isinstance(extra, Mapping) else None
    if not isinstance(raw, str):
        return []
    return [token.strip() for token in raw.split(",") if token.strip()]


def verify_bearer(authorization: str | None, tokens: list[str]) -> bool:
    """Whether ``Authorization: Bearer <token>`` names one of *tokens* (constant time)."""
    if not tokens or not authorization:
        return False
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return False
    from app.core.security import timing_safe_str_eq

    presented = value.strip()
    matched = False
    for token in tokens:
        matched |= timing_safe_str_eq(presented, token)
    return matched


def message_id(payload: Mapping[str, Any], svix_id: str | None) -> str:
    """The run's message id: Svix's delivery id, else a digest of the body (stable across retries)."""
    if svix_id and svix_id.strip():
        return f"drata:{svix_id.strip()}"
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]
    return f"drata:{digest}"


def event_payload(payload: Mapping[str, Any], event: str, svix_id: str | None) -> dict[str, Any]:
    """The run payload of one delivery: the body as sent, the event and its message id."""
    return {
        **{str(key): value for key, value in payload.items()},
        "provider": PROVIDER,
        "event": event,
        "event_type": str(payload.get("event_type") or ""),
        "message_id": message_id(payload, svix_id),
    }


def _answer(status: str, triggered: list[dict[str, Any]] | None = None, reason: str = "") -> dict[str, Any]:
    answer: dict[str, Any] = {"status": status, "triggered": triggered or [], "count": len(triggered or [])}
    if reason:
        answer["reason"] = reason
    return answer


async def dispatch_delivery(integration_id: str, payload: Any, svix_id: str | None = None) -> dict[str, Any]:
    """Start or queue a run per assignment of the delivery's event (the caller verified the token)."""
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
        without_filter_signals,
    )
    from app.services.slack_events import claim_seen, release_seen

    if not isinstance(payload, Mapping):
        return _answer("ignored", reason="not a Drata workflow webhook delivery (a JSON object)")
    event = event_of(payload)
    if event is None:
        return _answer("ignored", reason="not a Drata control, evidence, risk or personnel event")
    redis = await redis_client()
    triggered: list[dict[str, Any]] = []
    filtered: dict[Any, int] = {}
    async with async_session() as db:
        for wf, agent in await provider_assignments(db, integration_id, PROVIDER, {event}):
            run_payload = event_payload(payload, event, svix_id)
            msg_id = run_payload["message_id"]
            if not await claim_seen(redis, wf.id, msg_id):
                continue
            filter_config = trigger_filter(wf.trigger_config or {})
            if filter_config and event_filter_reason(event, run_payload, filter_config) is not None:
                filtered[wf.id] = filtered.get(wf.id, 0) + 1
                continue
            without_filter_signals(run_payload)
            trigger_context = {
                "type": "integration_event",
                "provider": PROVIDER,
                "event": event,
                "integration_id": str(integration_id),
                "message_id": msg_id,
                "delivery": "drata_webhook",
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
    logger.info("Drata delivery for integration %s (%s): %d run(s)", integration_id, event, len(triggered))
    return _answer("ok", triggered)
