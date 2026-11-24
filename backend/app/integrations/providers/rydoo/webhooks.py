# SPDX-License-Identifier: Apache-2.0

"""Rydoo expense webhook deliveries: the event types and how one is recognised.

Rydoo's expense webhook ("Expense webhook" guide) posts one JSON object per
status change of an expense: ``eventTimestamp``, ``expenseId``, ``userId``,
``companyId``, ``eventType`` and ``correlationId``. Rydoo activates it on
request (the company's CSM or connect@rydoo.com) for a URL the company gives;
it documents no signature and no custom header, so the integration webhook
URL carries the integration's ``webhook_token`` (a secret the company
chooses) as ``?token=``. A delivery without that token, or on an integration
with no token configured, is refused (``api/v1/webhooks.py``). When webhooks
are not activated, the exported-expenses tool is the polling fallback.
"""

from __future__ import annotations

from typing import Any, TypeGuard

from app.core.security import timing_safe_header_eq

#: The extra-config key of the token every delivery must carry.
TOKEN_FIELD = "webhook_token"  # noqa: S105 - a config key, not a secret

#: The query parameter of the webhook URL that carries it.
TOKEN_PARAM = "token"  # noqa: S105 - a query parameter name, not a secret

#: Every expense event the webhook sends (Rydoo guide, 22 May 2026).
EVENT_TYPES: dict[str, str] = {
    "ExpenseSubmitted": "Expense submitted",
    "ExpenseRecalled": "Expense recalled",
    "ExpenseDeleted": "Expense deleted",
    "ExpenseApproved": "Expense approved",
    "ExpenseControlled": "Expense controlled",
    "ExpenseUnapproved": "Expense unapproved",
    "ExpenseRejected": "Expense rejected",
}

#: The keys a delivery always carries.
_ENVELOPE = ("eventType", "expenseId", "companyId")


def trigger_event(rydoo_event: str) -> str:
    """The trigger event key of a Rydoo event type: ``rydoo_`` and its snake form.

    ``ExpenseApproved`` -> ``rydoo_expense_approved``: prefixed so no other
    provider's ``expense_approved`` is selected by it.
    """
    out = []
    for i, ch in enumerate(rydoo_event):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return f"rydoo_{''.join(out)}"


#: The trigger event key of each Rydoo event type.
TRIGGER_EVENTS: dict[str, str] = {event: trigger_event(event) for event in EVENT_TYPES}


def is_delivery(payload: object) -> TypeGuard[dict[str, Any]]:
    """A Rydoo expense webhook delivery: its envelope keys and a known event type."""
    if not isinstance(payload, dict):
        return False
    if not all(isinstance(payload.get(key), str) and payload.get(key) for key in _ENVELOPE):
        return False
    return payload["eventType"] in EVENT_TYPES


def token_matches(presented: str, configured: str) -> bool:
    """Whether a delivery's token is the integration's, in constant time.

    Empty on either side never matches.
    """
    return bool(presented) and bool(configured) and timing_safe_header_eq(presented, configured)
