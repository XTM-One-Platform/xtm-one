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

"""DocuSign Connect: real-time delivery of the DocuSign envelope events.

DocuSign Connect posts an envelope change to the integration's webhook URL
(``POST /api/hooks/integrations/{integration_id}``) as JSON (SIM, REST v2.1),
signed with the HMAC keys of its Connect configuration: each
``X-DocuSign-Signature-<n>`` header is the base64 HMAC-SHA256 of the raw body
under one key. The keys are stored on the integration (``connect_hmac_key``,
several separated by commas during a rotation); a delivery none of them signs
is refused, and a ``docusign`` integration with no key refuses every delivery.

A verified delivery becomes the payloads the poll builds
(``docusign.envelope_events``: same fields, same ``message_id``, same filter
keys) for every ``integration_event`` assignment reading this integration, and
each payload is claimed in the assignment's seen set the poll shares
(``slack_events.claim_seen``) before its run is created: an envelope change runs
once per assignment, whichever of Connect or the 15-minute poll reads it first.
A payload that cannot run now (AI unavailable, owner paused, queue full) is
released for the poll, the fallback. Replay-safe: a repeated delivery finds its
events claimed, and one generated before the claim window
(``SEEN_SET_TTL_SECONDS``) is ignored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import async_session
from app.core.redis import redis_client
from app.core.security import timing_safe_str_eq
from app.integrations.providers import docusign
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.integration import Integration
from app.services.agentic_gate import refusal_code
from app.services.assignment_engine import trigger_or_queue_assignment
from app.services.assignment_pause import AssignmentOwnerDeactivatedError, owner_not_deactivated
from app.services.integration_credentials import agent_acting_user_id
from app.services.integration_event_filters import (
    event_filter_reason,
    record_filtered_events,
    trigger_filter,
    without_filter_signals,
)
from app.services.integration_user_credentials import reader_clause
from app.services.slack_events import SEEN_SET_TTL_SECONDS, claim_seen, release_seen
from app.services.trigger_integration_access import (
    TriggerIntegrationVisibility,
    assignment_acting_user_id,
    log_unseen_trigger_integration,
    trigger_integration_id,
)

logger = logging.getLogger(__name__)

#: The integration's extra field holding the Connect HMAC keys.
HMAC_KEY_FIELD = "connect_hmac_key"
_SIGNATURE_HEADER_PREFIX = "x-docusign-signature-"
#: DocuSign signs with at most 100 keys per Connect configuration.
_MAX_SIGNATURES = 100
_KEY_SEPARATORS = re.compile(r"[\s,]+")
_RECIPIENT_COMPLETED_EVENT = "recipient-completed"


def connect_hmac_keys(extra: Mapping[str, Any] | None) -> list[str]:
    """The Connect HMAC keys stored on a DocuSign integration, in order."""
    raw = extra.get(HMAC_KEY_FIELD) if isinstance(extra, Mapping) else None
    if not isinstance(raw, str):
        return []
    return [key for key in _KEY_SEPARATORS.split(raw) if key]


def connect_signature(body: bytes, key: str) -> str:
    """What DocuSign writes in ``X-DocuSign-Signature-<n>`` for *body* under *key*."""
    return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


def _signatures(headers: Mapping[str, str]) -> list[str]:
    found: list[str] = []
    for name, value in headers.items():
        lowered = name.lower()
        if lowered.startswith(_SIGNATURE_HEADER_PREFIX) and lowered[len(_SIGNATURE_HEADER_PREFIX) :].isdigit() and value:
            found.append(value.strip())
    return found[:_MAX_SIGNATURES]


def verify_connect_signature(body: bytes, keys: list[str], headers: Mapping[str, str]) -> bool:
    """Whether one ``X-DocuSign-Signature-<n>`` of *headers* signs *body* under one of *keys*.

    No key or no signature header: ``False`` - a delivery is never accepted unverified.
    """
    signatures = _signatures(headers)
    if not keys or not signatures:
        return False
    expected = [connect_signature(body, key) for key in keys]
    return any(timing_safe_str_eq(signature, wanted) for signature in signatures for wanted in expected)


def delivery_too_old(payload: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Whether the delivery was generated before the claim window (a replay the seen set forgot).

    A delivery without ``generatedDateTime`` is judged by its events' claims alone.
    """
    generated = payload.get("generatedDateTime")
    if not generated:
        return False
    current = now or datetime.now(UTC)
    return docusign.event_instant(generated) < current - timedelta(seconds=SEEN_SET_TTL_SECONDS)


def _data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else {}


def connect_envelope(payload: Mapping[str, Any]) -> dict | None:
    """The delivery's envelope in the REST shape the poll reads.

    ``None`` when the delivery carries no envelope summary (Connect's "Include
    data" left empty): without it the payload could not be the poll's, so the
    poll reads that change instead.
    """
    data = _data(payload)
    summary = data.get("envelopeSummary")
    if not isinstance(summary, Mapping):
        return None
    envelope = dict(summary)
    envelope_id = str(envelope.get("envelopeId") or data.get("envelopeId") or "").strip()
    if not envelope_id:
        return None
    envelope["envelopeId"] = envelope_id
    return envelope


def connect_events(payload: Mapping[str, Any], event_type: str, *, environment: str = "production") -> list[dict]:
    """The payloads of *event_type* a Connect delivery carries, as the poll builds them.

    An envelope event when the summary has its status; ``recipient_completed``
    for the signer a ``recipient-completed`` delivery names.
    """
    if event_type not in docusign.ENVELOPE_EVENTS:
        return []
    envelope = connect_envelope(payload)
    if envelope is None:
        return []
    if event_type != docusign.RECIPIENT_COMPLETED:
        return docusign.envelope_events(envelope, event_type, environment=environment)
    if str(payload.get("event") or "").lower() != _RECIPIENT_COMPLETED_EVENT:
        return []
    recipient_id = str(_data(payload).get("recipientId") or "").strip()
    recipients = envelope.get("recipients") if isinstance(envelope.get("recipients"), Mapping) else {}
    signer = next(
        (
            s
            for s in recipients.get("signers") or []
            if isinstance(s, Mapping) and recipient_id and str(s.get("recipientId", "")) == recipient_id
        ),
        None,
    )
    if signer is None:
        # A carbon copy, or a summary without its recipients: not a signature the poll would run.
        return []
    return [
        docusign.envelope_event_payload(
            envelope, docusign.RECIPIENT_COMPLETED, environment=environment, recipient=dict(signer)
        )
    ]


def _answer(status: str, triggered: list[dict] | None = None, reason: str = "") -> dict:
    answer: dict[str, Any] = {"status": status, "triggered": triggered or [], "count": len(triggered or [])}
    if reason:
        answer["reason"] = reason
    return answer


async def connect_assignments(db, integration_id: str) -> list[tuple[Assignment, Agent]]:
    """The enabled DocuSign ``integration_event`` assignments reading *integration_id*, with their agent."""
    return await provider_assignments(db, integration_id, "docusign", docusign.ENVELOPE_EVENTS)


async def provider_assignments(db, integration_id: str, provider: str, events: Any) -> list[tuple[Assignment, Agent]]:
    """The enabled *provider* ``integration_event`` assignments of *events* reading *integration_id*.

    What a pushed delivery (DocuSign Connect, a Jira webhook) is dispatched to,
    by the poll's rule: the trigger's integration (``trigger_integration_id``),
    an enabled agent, and an integration the agent's owner may read; a run
    acting as someone who cannot see it is still delivered, and logged.
    """
    rows = await db.execute(
        select(Assignment).where(
            Assignment.trigger_type == "integration_event",
            Assignment.enabled.is_(True),
            owner_not_deactivated(),
        )
    )
    assignments = [
        wf
        for wf in rows.scalars().all()
        if (wf.trigger_config or {}).get("provider") == provider and (wf.trigger_config or {}).get("event") in events
    ]
    agent_ids = {wf.agent_id for wf in assignments if wf.agent_id}
    agents: dict = {}
    if agent_ids:
        agents = {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all()}
    readable: dict[Any, bool] = {}
    visibility = TriggerIntegrationVisibility()
    found: list[tuple[Assignment, Agent]] = []
    for wf in assignments:
        agent = agents.get(wf.agent_id)
        if agent is None or not agent.enabled:
            continue
        if str(trigger_integration_id(wf.trigger_config or {}, agent) or "") != str(integration_id):
            continue
        acting = agent_acting_user_id(agent)
        if acting not in readable:
            row = await db.execute(select(Integration.id).where(Integration.id == integration_id, reader_clause(acting)))
            readable[acting] = row.scalar_one_or_none() is not None
        if not readable[acting]:
            continue
        run_as = assignment_acting_user_id(wf, agent)
        if run_as is not None and await visibility.hides(db, run_as, integration_id):
            log_unseen_trigger_integration("assignment", wf.id, integration_id=integration_id, acting_user_id=run_as)
        found.append((wf, agent))
    return found


async def dispatch_connect_delivery(integration_id: str, extra: Mapping[str, Any], payload: Any) -> dict:
    """Start or queue a run per assignment for each envelope event a verified delivery carries.

    The caller verified the signature (``verify_connect_signature``). Answers
    what the webhook returns: the runs started, or why nothing was read.
    """
    if not isinstance(payload, Mapping) or connect_envelope(payload) is None:
        return _answer(
            "ignored",
            reason="not a DocuSign Connect envelope delivery (JSON, REST v2.1, with the envelope data and recipients)",
        )
    if delivery_too_old(payload):
        logger.info("DocuSign Connect delivery for integration %s older than the replay window: ignored", integration_id)
        return _answer("ignored", reason="delivery older than the replay window")
    environment = str(extra.get("environment") or "production").strip().lower() or "production"
    redis = await redis_client()
    triggered: list[dict] = []
    filtered: dict[Any, int] = {}
    async with async_session() as db:
        for wf, agent in await connect_assignments(db, integration_id):
            config = wf.trigger_config or {}
            event_type = config["event"]
            filter_config = trigger_filter(config)
            for event_payload in connect_events(payload, event_type, environment=environment):
                msg_id = event_payload.get("message_id") or ""
                # Claimed in the seen set the poll shares: the first reader runs it.
                if not msg_id or not await claim_seen(redis, wf.id, msg_id):
                    continue
                if filter_config and event_filter_reason(event_type, event_payload, filter_config) is not None:
                    filtered[wf.id] = filtered.get(wf.id, 0) + 1
                    continue
                without_filter_signals(event_payload)
                trigger_context = {
                    "type": "integration_event",
                    "provider": "docusign",
                    "event": event_type,
                    "integration_id": str(integration_id),
                    "message_id": msg_id,
                    "delivery": "docusign_connect",
                    "triggered_at": datetime.now(UTC).isoformat(),
                }
                try:
                    result = await trigger_or_queue_assignment(
                        wf, agent, event_payload, db=db, trigger_context=trigger_context, overflow="reject"
                    )
                except HTTPException as exc:
                    # AI unavailable, or the owner paused since the query: the poll reads it later.
                    await release_seen(redis, wf.id, msg_id)
                    if refusal_code(exc) is None and not isinstance(exc, AssignmentOwnerDeactivatedError):
                        raise
                    continue
                except ValueError:
                    # Disabled since the query picked it: nothing runs, the poll skips it too.
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
    logger.info(
        "DocuSign Connect delivery for integration %s (%s): %d run(s)", integration_id, payload.get("event"), len(triggered)
    )
    return _answer("ok", triggered)
