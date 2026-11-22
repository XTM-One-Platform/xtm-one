# SPDX-License-Identifier: Apache-2.0

"""Spendesk webhook deliveries: the event types and the signature check.

Spendesk signs each delivery with the secret of its webhook instance:
``x-spendesk-webhook-signature`` is the hex HMAC-SHA256 of the payload
("Using webhooks"). The digest is computed over the raw body first (what the
sender serialized) and, for a relay that re-serialized it, over its compact
JSON form. A delivery names ``event.entity`` and ``event.eventType`` and the
object id in ``data.id``; the same event can arrive more than once, so
``event.id`` is the deduplication key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from app.core.security import timing_safe_str_eq

SIGNATURE_HEADER = "x-spendesk-webhook-signature"

#: Every ``entity:eventType`` a webhook instance subscribes to (21 Nov 2026).
EVENT_TYPES: dict[str, str] = {
    "payables:created": "Payable created",
    "payables:prepared": "Payable prepared for accounting",
    "settlements:created": "Settlement created",
}


def trigger_event(entity: str, event_type: str) -> str:
    """The trigger event key of a Spendesk event: ``payables_created``."""
    return f"{entity}_{event_type}".replace(":", "_").replace(".", "_").lower()


#: The trigger event key of each subscribable Spendesk event.
TRIGGER_EVENTS: dict[str, str] = {event: trigger_event(*event.split(":", 1)) for event in EVENT_TYPES}


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


def verify_signature(body: bytes, secret: str, signature: str) -> tuple[bool, str]:
    """``(True, "")`` for an authentic delivery, else ``(False, reason)``."""
    if not secret:
        return False, "no signing secret configured"
    signature = (signature or "").strip().lower().removeprefix("sha256=")
    if not signature:
        return False, f"missing {SIGNATURE_HEADER}"
    if not any(timing_safe_str_eq(signature, digest) for digest in _digests(body, secret)):
        return False, "signature mismatch"
    return True, ""


def event_key(payload: Any) -> str:
    """The trigger key a delivery names, or ``""`` when it names none."""
    if not isinstance(payload, dict):
        return ""
    event = payload.get("event")
    if not isinstance(event, dict):
        return ""
    entity, kind = event.get("entity"), event.get("eventType")
    if not isinstance(entity, str) or not isinstance(kind, str) or not entity or not kind:
        return ""
    return trigger_event(entity, kind)
