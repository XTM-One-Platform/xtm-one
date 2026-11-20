# SPDX-License-Identifier: Apache-2.0

"""Aikido webhook deliveries: the event types and the signature check.

Aikido signs each delivery with the HMAC signing secret created on its
webhooks settings page: ``X-Aikido-Webhook-Signature`` is the hex HMAC-SHA256
of the JSON payload, and the payload's ``dispatched_at`` epoch must be at most
30 seconds old ("Verifying incoming webhooks"). The digest is computed over
the raw body first (what the sender serialized) and, for a relay that
re-serialized it, over the compact JSON form Aikido's own example signs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from app.core.security import timing_safe_str_eq

SIGNATURE_HEADER = "X-Aikido-Webhook-Signature"
#: Oldest ``dispatched_at`` accepted, in seconds (Aikido's replay window).
MAX_AGE_SECONDS = 30
#: Tolerated clock skew for a ``dispatched_at`` in the future, in seconds.
MAX_FUTURE_SKEW_SECONDS = 30

#: Every event type ``POST /webhooks`` subscribes to (OpenAPI enum, 20 Nov 2026).
EVENT_TYPES: dict[str, str] = {
    "issue.open.created": "New open issue",
    "issue.open.created.open_source": "New open-source dependency issue",
    "issue.open.created.sast": "New SAST issue",
    "issue.snoozed": "Issue snoozed",
    "issue.ignored.manual": "Issue ignored by hand",
    "issue.closed": "Issue closed",
    "issue.unignored": "Issue unignored",
    "issue.unignored.manual": "Issue unignored by hand",
    "issue.severity.changed.manual": "Issue severity changed by hand",
    "issue.sla.breached": "Issue SLA breached",
    "ci.gate.failed": "CI gate failed",
    "ci.gate.passed": "CI gate passed",
    "configuration_issue.detected": "Configuration issue detected",
    "inbox.issues.request.created": "Request inbox: new request",
    "scan.image.finished": "Container image scan finished",
    "scan.domain.finished": "Domain scan finished",
    "zen.attack": "Zen: attack blocked or detected",
    "zen.attack_wave": "Zen: attack wave",
    "zen.outbound.discovered": "Zen: new outbound connection",
    "pentest.running": "Pentest running",
    "pentest.paused": "Pentest paused",
    "pentest.finished": "Pentest finished",
    "pentest.failed": "Pentest failed",
    "pentest.cancelled": "Pentest cancelled",
}


def _digests(body: bytes, secret: str) -> list[str]:
    key = secret.encode("utf-8")
    digests = [hmac.new(key, body, hashlib.sha256).hexdigest()]
    try:
        compact = json.dumps(json.loads(body), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return digests
    if compact != body:
        digests.append(hmac.new(key, compact, hashlib.sha256).hexdigest())
    return digests


def verify_signature(body: bytes, secret: str, signature: str, *, now: float | None = None) -> tuple[bool, str]:
    """``(True, "")`` for an authentic, fresh delivery, else ``(False, reason)``."""
    if not secret:
        return False, "no signing secret configured"
    signature = (signature or "").strip().lower()
    if not signature:
        return False, f"missing {SIGNATURE_HEADER}"
    if not any(timing_safe_str_eq(signature, digest) for digest in _digests(body, secret)):
        return False, "signature mismatch"
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False, "payload is not JSON"
    dispatched = payload.get("dispatched_at") if isinstance(payload, dict) else None
    if not isinstance(dispatched, (int, float)) or isinstance(dispatched, bool):
        return False, "payload carries no dispatched_at"
    current = time.time() if now is None else now
    if current - dispatched > MAX_AGE_SECONDS:
        return False, "delivery older than 30 seconds (replay window)"
    if dispatched - current > MAX_FUTURE_SKEW_SECONDS:
        return False, "dispatched_at is in the future"
    return True, ""


def trigger_event(aikido_event: str) -> str:
    """The trigger event key of an Aikido event type: its dots become underscores.

    Aikido names nest (``issue.open.created`` and ``issue.open.created.sast``,
    ``zen.attack`` and ``zen.attack_wave`` are separate subscriptions), and a
    trigger selection matches every key below it across a ``.``: kept dotted,
    an assignment on ``issue.open.created`` would also run on every SAST and
    open-source delivery. The flat key selects exactly one Aikido event.
    """
    return aikido_event.replace(".", "_")


#: The trigger event key of each subscribable Aikido event type.
TRIGGER_EVENTS: dict[str, str] = {event: trigger_event(event) for event in EVENT_TYPES}


def event_type(payload: Any) -> str:
    """The event type a delivery names (``event_type``, else ``type``)."""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("event_type") or payload.get("type") or ""
    return value if isinstance(value, str) else ""
