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

"""Pennylane events for ``integration_event`` assignments (#1009).

An assignment with ``{"provider": "pennylane", "event": <event>}`` is woken by
the changes of the Pennylane company its integration reaches, read from the
Company API v2 changelogs (``/changelogs/<resource>``, four weeks kept;
Pennylane sends no outbound webhook to a company token):

* ``supplier_invoice_created`` / ``supplier_invoice_updated``;
* ``customer_invoice_created`` / ``customer_invoice_updated``;
* ``customer_invoice_paid``: a customer invoice reads ``paid`` (once per invoice);
* ``transaction_created``;
* ``transaction_matched``: a bank transaction is matched to invoices (again
  when the set of matched invoices changes).

The poll (``event_poller._poll_pennylane_changes``) reads the changelog entries
processed since its cursor, oldest first, then the record each one names, so
the payload carries the amounts, the parties and the status a filter reads.
The first poll starts from now. Each change runs once (``message_id``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

PROVIDER = "pennylane"
#: Pennylane allows 25 requests per 5 seconds per token; a poll reads one
#: changelog page and one record per change.
POLL_INTERVAL_SECONDS = 300
#: Pennylane keeps four weeks of changelog; an older start date is refused (422).
RETENTION = timedelta(days=27)

SUPPLIER_INVOICE_CREATED = "supplier_invoice_created"
SUPPLIER_INVOICE_UPDATED = "supplier_invoice_updated"
CUSTOMER_INVOICE_CREATED = "customer_invoice_created"
CUSTOMER_INVOICE_UPDATED = "customer_invoice_updated"
CUSTOMER_INVOICE_PAID = "customer_invoice_paid"
TRANSACTION_CREATED = "transaction_created"
TRANSACTION_MATCHED = "transaction_matched"

#: event -> (changelog resource, record path, changelog operations it reads).
_EVENTS: dict[str, tuple[str, str, frozenset[str]]] = {
    SUPPLIER_INVOICE_CREATED: ("supplier_invoices", "supplier_invoices", frozenset({"insert"})),
    SUPPLIER_INVOICE_UPDATED: ("supplier_invoices", "supplier_invoices", frozenset({"update"})),
    CUSTOMER_INVOICE_CREATED: ("customer_invoices", "customer_invoices", frozenset({"insert"})),
    CUSTOMER_INVOICE_UPDATED: ("customer_invoices", "customer_invoices", frozenset({"update"})),
    CUSTOMER_INVOICE_PAID: ("customer_invoices", "customer_invoices", frozenset({"insert", "update"})),
    TRANSACTION_CREATED: ("transactions", "transactions", frozenset({"insert"})),
    TRANSACTION_MATCHED: ("transactions", "transactions", frozenset({"insert", "update"})),
}
EVENTS = frozenset(_EVENTS)

#: The filter keys this module reads itself; any other key goes to the
#: generic payload filter (``integration_event_filters.payload_filter_reason``).
FILTER_KEYS = (
    "min_amount",
    "max_amount",
    "supplier_id",
    "customer_id",
    "journal_id",
    "bank_account_id",
    "status",
    "currency",
)


def changelog_path(event_type: str) -> str:
    return f"changelogs/{_EVENTS[event_type][0]}"


def clamp_since(since: str, *, now: datetime | None = None) -> str:
    """*since* as an RFC 3339 instant inside Pennylane's changelog retention."""
    floor = (now or datetime.now(UTC)) - RETENTION
    instant = parse_instant(since)
    if instant < floor:
        instant = floor
    return instant.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_instant(value: Any) -> datetime:
    """A Pennylane timestamp as an aware datetime; the oldest one possible when unreadable."""
    raw = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def list_changes(
    credential: str, event_type: str, since: str, *, limit: int
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """``(entries, more, error)``: the changelog entries of *event_type*'s resource processed since *since*.

    Every entry of the page, whatever its operation: the cursor moves past
    them all, and :func:`selected` keeps the ones the event reads.
    """
    from app.integrations.providers.pennylane._common import PennylaneContext

    ctx = PennylaneContext.from_token(credential)
    async with ctx.client() as client:
        page, err = await ctx.page(
            client,
            changelog_path(event_type),
            params={"start_date": clamp_since(since)},
            limit=limit,
            action=f"read the {_EVENTS[event_type][0]} changelog",
        )
    if err:
        return [], False, err
    return page.items, bool(page.next_cursor), None


def selected(entries: Iterable[Mapping[str, Any]], event_type: str) -> list[dict[str, Any]]:
    """The changelog entries *event_type* reads (its operations, an id), one per record, newest kept."""
    operations = _EVENTS[event_type][2]
    latest: dict[Any, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("id") is None or entry.get("operation") not in operations:
            continue
        latest.pop(entry["id"], None)
        latest[entry["id"]] = dict(entry)
    return list(latest.values())


def _ref_id(value: Any) -> int | None:
    if isinstance(value, Mapping) and value.get("id") is not None:
        try:
            return int(value["id"])
        except (TypeError, ValueError):
            return None
    return None


def _amount(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def record_event(
    record: Mapping[str, Any],
    change: Mapping[str, Any],
    event_type: str,
    *,
    company_id: str = "",
    matched_invoice_ids: Iterable[int] = (),
) -> dict[str, Any] | None:
    """The payload of one change, ``None`` when the record does not make it the event."""
    if event_type == CUSTOMER_INVOICE_PAID and not record.get("paid"):
        return None
    matched = sorted({int(i) for i in matched_invoice_ids})
    if event_type == TRANSACTION_MATCHED and not matched:
        return None
    resource = _EVENTS[event_type][0]
    record_id = record.get("id", change.get("id"))
    changed_at = str(change.get("processed_at") or change.get("updated_at") or "")
    company = company_id or "default"
    if event_type == CUSTOMER_INVOICE_PAID:
        unique = f"{record_id}"
    elif event_type == TRANSACTION_MATCHED:
        unique = f"{record_id}:{','.join(str(i) for i in matched)}"
    elif event_type.endswith("_created"):
        unique = f"{record_id}"
    else:
        unique = f"{record_id}:{changed_at}"
    status = record.get("payment_status") if resource == "supplier_invoices" else record.get("status")
    payload: dict[str, Any] = {
        "message_id": f"pennylane:{company}:{event_type}:{unique}" if record_id is not None else "",
        "provider": PROVIDER,
        "event": event_type,
        "resource": resource,
        "operation": change.get("operation"),
        "changed_at": changed_at,
        "company_id": company_id,
        "id": record_id,
        "label": record.get("label"),
        "invoice_number": record.get("invoice_number"),
        "status": status,
        "accounting_status": record.get("accounting_status"),
        "paid": record.get("paid"),
        "amount": _amount(record.get("amount")),
        "currency_amount": _amount(record.get("currency_amount")),
        "currency": record.get("currency"),
        "remaining_amount_with_tax": _amount(record.get("remaining_amount_with_tax")),
        "date": record.get("date"),
        "deadline": record.get("deadline"),
        "supplier_id": _ref_id(record.get("supplier")),
        "customer_id": _ref_id(record.get("customer")),
        "journal_id": _ref_id(record.get("journal")),
        "bank_account_id": _ref_id(record.get("bank_account")),
        "public_file_url": record.get("public_file_url"),
    }
    if resource == "transactions":
        payload["matched_invoice_ids"] = matched
    return {k: v for k, v in payload.items() if v is not None}


async def change_events(credential: str, event_type: str, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The payloads of *changes*, each read with its record; a record gone (deleted, 404) is skipped."""
    from app.integrations.providers.pennylane._common import PennylaneContext

    ctx = PennylaneContext.from_token(credential)
    record_path = _EVENTS[event_type][1]
    out: list[dict[str, Any]] = []
    async with ctx.client() as client:
        for change in changes:
            record, err = await ctx.get(
                client, f"{record_path}/{change['id']}", action=f"read {record_path} {change['id']}"
            )
            if err or not isinstance(record, dict):
                continue
            matched: list[int] = []
            if event_type == TRANSACTION_MATCHED:
                items, err, _ = await ctx.all_items(client, f"transactions/{change['id']}/matched_invoices", cap=200)
                if err:
                    continue
                matched = [int(i["id"]) for i in items or [] if str(i.get("id", "")).isdigit()]
            event = record_event(record, change, event_type, company_id=ctx.company_id, matched_invoice_ids=matched)
            if event is not None:
                out.append(event)
    return out


def _strings(value: Any) -> set[str]:
    if value is None or value == "":
        return set()
    items = value if isinstance(value, list | tuple | set) else [value]
    return {str(v).strip().casefold() for v in items if str(v).strip()}


def filter_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any]) -> str | None:
    """Why the trigger's filter drops a Pennylane event, ``None`` when it runs.

    ``min_amount`` / ``max_amount`` compare the amount in euros (tax included);
    the id and status keys take one value or a list. The other keys of the
    filter go to the generic payload filter.
    """
    from app.services.integration_event_filters import payload_filter_reason

    amount = payload.get("amount")
    for key, below in (("min_amount", True), ("max_amount", False)):
        bound = filter_config.get(key)
        if bound in (None, ""):
            continue
        limit = _amount(bound)
        if limit is None:
            return f"{key} is not a number"
        if not isinstance(amount, int | float) or (amount < limit if below else amount > limit):
            return f"amount is {'under' if below else 'over'} {key}"
    for key in ("supplier_id", "customer_id", "journal_id", "bank_account_id", "status", "currency"):
        wanted = _strings(filter_config.get(key))
        if wanted and str(payload.get(key) if payload.get(key) is not None else "").casefold() not in wanted:
            return f"{key} is not in {key}"
    rest = {k: v for k, v in filter_config.items() if k not in FILTER_KEYS}
    return payload_filter_reason(payload, rest) if rest else None
