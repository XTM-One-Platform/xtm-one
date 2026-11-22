# SPDX-License-Identifier: Apache-2.0

"""Airwallex webhook deliveries: the event types and the signature check.

Airwallex signs each delivery of a webhook configured with a secret:
``x-timestamp`` is the send time in Unix milliseconds and ``x-signature`` the
hex HMAC-SHA256, keyed with the webhook secret, of the timestamp string
followed by the raw request body ("Listen for webhook events"). A delivery
older than :data:`MAX_AGE_SECONDS` is refused as a replay.

The payload is an Event object: ``id``, ``name`` (the event type, e.g.
``payout.transfer.paid``), ``account_id`` (the Airwallex account it belongs
to), ``org_id``, ``data.object`` (the resource) and ``created_at``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Any

from app.core.security import timing_safe_str_eq

SIGNATURE_HEADER = "x-signature"
TIMESTAMP_HEADER = "x-timestamp"
#: Oldest delivery accepted, in seconds, and the clock skew tolerated ahead.
MAX_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 60

#: The event types a treasury, close or FP&A agent listens to (API reference,
#: version 2026-08-21), with their trigger picker label.
EVENT_TYPES: dict[str, str] = {
    "deposit.settled": "Deposit settled",
    "deposit.pending": "Deposit pending",
    "deposit.rejected": "Deposit rejected",
    "deposit.reversed": "Deposit reversed",
    "balance.ga.top_up": "Global account collection settled to the wallet",
    "balance.va.top_up": "Virtual account deposit settled to the wallet",
    "balance.adjustment": "Balance adjustment",
    "payout.transfer.in_approval": "Transfer in approval",
    "payout.transfer.scheduled": "Transfer scheduled",
    "payout.transfer.processing": "Transfer processing",
    "payout.transfer.sent": "Transfer sent",
    "payout.transfer.paid": "Transfer paid",
    "payout.transfer.failed": "Transfer failed",
    "payout.transfer.cancelled": "Transfer cancelled",
    "payout.transfer.overdue": "Transfer overdue",
    "payout.transfer.approval_rejected": "Transfer rejected in approval",
    "payout.transfer.funding.failed": "Transfer funding failed",
    "payment.bank.statements.file": "Bank statement file ready",
    "conversion.scheduled": "Conversion booked",
    "conversion.settled": "Conversion settled",
    "conversion.overdue": "Conversion overdue",
    "conversion.cancelled": "Conversion cancelled",
    "issuing.card.active": "Card active",
    "issuing.card.blocked": "Card blocked",
    "issuing.card.lost": "Card reported lost",
    "issuing.card.stolen": "Card reported stolen",
    "issuing.card.low_remaining_transaction_limit": "Card low remaining limit",
    "invoice.payment.paid": "Invoice paid",
    "payment_intent.succeeded": "Payment received",
    "payment_intent.payment_failed": "Payment failed",
    "account.active": "Account active",
    "account.action_required": "Account action required (KYC)",
    "account.suspended": "Account suspended",
    "global_account.closed": "Global account closed",
}


def _digest(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def verify_signature(
    body: bytes,
    secret: str,
    signature: str,
    timestamp: str,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """``(True, "")`` for an authentic, fresh delivery, else ``(False, reason)``."""
    if not secret:
        return False, "no webhook secret configured"
    signature = (signature or "").strip().lower()
    timestamp = (timestamp or "").strip()
    if not signature:
        return False, f"missing {SIGNATURE_HEADER}"
    if not timestamp.isdigit():
        return False, f"missing or malformed {TIMESTAMP_HEADER}"
    if not timing_safe_str_eq(signature, _digest(secret, timestamp, body)):
        return False, "signature mismatch"
    sent = int(timestamp) / 1000.0
    current = time.time() if now is None else now
    if current - sent > MAX_AGE_SECONDS:
        return False, f"delivery older than {MAX_AGE_SECONDS} seconds (replay window)"
    if sent - current > MAX_FUTURE_SKEW_SECONDS:
        return False, f"{TIMESTAMP_HEADER} is in the future"
    return True, ""


def trigger_event(airwallex_event: str) -> str:
    """The trigger event key of an Airwallex event type: ``airwallex_`` and its dots as underscores.

    Airwallex names nest (``payout.transfer.failed`` and
    ``payout.transfer.funding.failed``), and a trigger selection matches every
    key below it across a ``.``: the flat key selects exactly one event, and
    the prefix keeps it apart from another provider's event of the same name.
    """
    return "airwallex_" + airwallex_event.replace(".", "_")


#: The trigger event key of each subscribable Airwallex event type.
TRIGGER_EVENTS: dict[str, str] = {event: trigger_event(event) for event in EVENT_TYPES}


def is_delivery(get_header: Callable[[str], Any], payload: Any) -> bool:
    """Whether a request is an Airwallex delivery: ``x-timestamp`` and an Event object."""
    if not isinstance(payload, dict) or not get_header(TIMESTAMP_HEADER):
        return False
    name = payload.get("name")
    return isinstance(name, str) and "." in name and "data" in payload and "account_id" in payload


def event_type(payload: Any) -> str:
    """The event type a delivery names (``name``)."""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("name") or ""
    return value if isinstance(value, str) else ""
