# SPDX-License-Identifier: Apache-2.0

"""NetSuite change events for ``integration_event`` assignments (#1010, slice 2).

An assignment with ``{"provider": "netsuite", "event": <event>}`` is woken by
the changes of the NetSuite account its integration is connected to:

* ``vendor_bill_changed``: a vendor bill was created or changed;
* ``invoice_changed``: an invoice or a credit memo was created or changed;
* ``payment_changed``: a customer or vendor payment was created or changed;
* ``journal_entry_changed``: a journal entry or an intercompany journal was
  created or changed;
* ``accounting_period_changed``: an accounting period was closed, reopened,
  locked or unlocked.

NetSuite posts no change notification without a script of the customer's, so
the poller (``event_poller._poll_netsuite_changes``, every
``POLL_INTERVAL_SECONDS``) reads them with SuiteQL:

* transactions: the records of the event's types whose ``lastmodifieddate``
  is at or after the cursor, oldest first. The cursor is a NetSuite timestamp
  as SuiteQL writes it (``YYYY-MM-DD HH24:MI:SS``, the role's time zone): it
  is only ever compared with NetSuite's own values, so no time zone is
  guessed. Each payload's ``message_id`` names the record and its
  modification, so a record changed twice runs twice and a re-read runs once.
* accounting periods: a snapshot of every posting period's flags (closed, AP,
  AR and all locked), each poll diffed against the last one.

The first poll of an assignment stores the cursor (or the snapshot) and runs
nothing, so enabling a trigger never replays the history.

Filter keys (``filter_reason``; ``subsidiary_id`` and ``min_amount`` are also
applied in the SuiteQL so the read stays small): ``subsidiary_id`` (internal
ids), ``transaction_types`` (the tool names, e.g. ``vendor_payment``),
``status`` (status names or codes, case-insensitive), ``min_amount`` (absolute
amount in the transaction currency), ``change_kinds`` (``created``,
``updated``) and, for periods, ``states`` (``open``, ``closed``,
``ap_locked``, ``ar_locked``, ``all_locked``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from app.integrations.providers.netsuite import TRANSACTION_TYPES, account_host

PROVIDER = "netsuite"

VENDOR_BILL_CHANGED = "vendor_bill_changed"
INVOICE_CHANGED = "invoice_changed"
PAYMENT_CHANGED = "payment_changed"
JOURNAL_ENTRY_CHANGED = "journal_entry_changed"
ACCOUNTING_PERIOD_CHANGED = "accounting_period_changed"

#: The transaction types (tool names) each transaction event reads.
EVENT_TRANSACTION_TYPES: dict[str, tuple[str, ...]] = {
    VENDOR_BILL_CHANGED: ("vendor_bill",),
    INVOICE_CHANGED: ("invoice", "credit_memo"),
    PAYMENT_CHANGED: ("customer_payment", "vendor_payment"),
    JOURNAL_ENTRY_CHANGED: ("journal_entry", "intercompany_journal"),
}

TRANSACTION_EVENTS = frozenset(EVENT_TRANSACTION_TYPES)
EVENTS = frozenset({*TRANSACTION_EVENTS, ACCOUNTING_PERIOD_CHANGED})

#: NetSuite counts every request against the account's concurrency limit: a
#: finance change is acted on within five minutes.
POLL_INTERVAL_SECONDS = 300

#: The keys the filter reads (``filter_reason``).
FILTER_KEYS = ("subsidiary_id", "transaction_types", "status", "min_amount", "change_kinds", "states")

#: Records one poll reads (one more tells whether more are waiting).
PAGE = 50

#: The SuiteQL timestamp format of the cursor and of ``modified_at``.
TIMESTAMP_FORMAT = "YYYY-MM-DD HH24:MI:SS"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
#: The cursor of an account with no record of the event's types yet.
EPOCH = "1970-01-01 00:00:00"

PERIOD_STATES = ("open", "closed", "ap_locked", "ar_locked", "all_locked")

_RECORD_TYPE_NAMES = {code: name for name, code in TRANSACTION_TYPES.items()}


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return [str(value)] if str(value).strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip()]
    return []


def _ids(value: Any) -> list[int]:
    """The internal ids of a filter value; a value that is not one is left out."""
    out: list[int] = []
    for text in _strings(value):
        if text.strip().isdigit():
            out.append(int(text.strip()))
    return out


def _amount(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_timestamp(value: str) -> bool:
    return bool(_TIMESTAMP.match(value or ""))


def _record_codes(event_type: str, filter_config: Mapping[str, Any]) -> list[str]:
    names = EVENT_TRANSACTION_TYPES[event_type]
    wanted = {t.strip().lower() for t in _strings(filter_config.get("transaction_types"))}
    chosen = [n for n in names if n in wanted] if wanted else list(names)
    return [TRANSACTION_TYPES[n] for n in chosen]


def changes_query(event_type: str, filter_config: Mapping[str, Any] | None, since: str) -> str:
    """The SuiteQL of the records of *event_type* changed at or after *since*, oldest first.

    The read is bounded by the REST page (``limit``), not in the SQL.

    Raises ``ValueError`` on a cursor that is not a SuiteQL timestamp: the
    cursor is interpolated, so it is checked first, as every value is.
    """
    if event_type not in TRANSACTION_EVENTS:
        raise ValueError(f"{event_type} is not a NetSuite transaction event")
    if not is_timestamp(since):
        raise ValueError(f"the cursor must be a timestamp YYYY-MM-DD HH:MM:SS, got {since!r}")
    config = filter_config or {}
    codes = _record_codes(event_type, config) or [TRANSACTION_TYPES[n] for n in EVENT_TRANSACTION_TYPES[event_type]]
    where = [
        "t.type IN (" + ", ".join(f"'{c}'" for c in codes) + ")",
        f"t.lastmodifieddate >= TO_TIMESTAMP('{since}', '{TIMESTAMP_FORMAT}')",
    ]
    subsidiaries = _ids(config.get("subsidiary_id"))
    if subsidiaries:
        where.append("tl.subsidiary IN (" + ", ".join(str(i) for i in subsidiaries) + ")")
    minimum = _amount(config.get("min_amount"))
    if minimum is not None:
        where.append(f"ABS(t.foreigntotal) >= {abs(minimum)}")
    return (
        "SELECT t.id, t.tranid, t.type, t.status AS status_code, BUILTIN.DF(t.status) AS status, t.trandate, "
        "t.entity AS entity_id, BUILTIN.DF(t.entity) AS entity, t.foreigntotal AS amount, "
        "BUILTIN.DF(t.currency) AS currency, t.postingperiod AS period_id, BUILTIN.DF(t.postingperiod) AS period, "
        "tl.subsidiary AS subsidiary_id, BUILTIN.DF(tl.subsidiary) AS subsidiary, t.approvalstatus, t.memo, "
        f"TO_CHAR(t.createddate, '{TIMESTAMP_FORMAT}') AS created_at, "
        f"TO_CHAR(t.lastmodifieddate, '{TIMESTAMP_FORMAT}') AS modified_at "
        "FROM transaction t JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'T' WHERE "
        + " AND ".join(where)
        + " ORDER BY t.lastmodifieddate, t.id"
    )


def seed_query(event_type: str) -> str:
    """The SuiteQL of the newest modification of *event_type*'s records: the first cursor."""
    codes = [TRANSACTION_TYPES[n] for n in EVENT_TRANSACTION_TYPES[event_type]]
    return (
        f"SELECT TO_CHAR(MAX(t.lastmodifieddate), '{TIMESTAMP_FORMAT}') AS cursor FROM transaction t "
        "WHERE t.type IN (" + ", ".join(f"'{c}'" for c in codes) + ")"
    )


PERIODS_QUERY = (
    "SELECT ap.id, ap.periodname, ap.startdate, ap.enddate, ap.closed, ap.aplocked, ap.arlocked, ap.alllocked "
    "FROM accountingperiod ap WHERE ap.isyear = 'F' AND ap.isquarter = 'F' ORDER BY ap.startdate"
)


def _web_link(account_id: str, record_id: Any) -> str:
    return (
        f"https://{account_host(account_id)}.app.netsuite.com/app/accounting/transactions/transaction.nl?id={record_id}"
    )


def transaction_event(row: Mapping[str, Any], event_type: str, *, account_id: str) -> dict:
    """The payload of one changed transaction; its ``message_id`` names the record and the modification."""
    record_id = str(row.get("id") or "")
    modified = str(row.get("modified_at") or "")
    created = str(row.get("created_at") or "")
    code = str(row.get("type") or "")
    return {
        "provider": PROVIDER,
        "event": event_type,
        "message_id": f"netsuite:{account_host(account_id)}:{record_id}:{modified}" if record_id else "",
        "account_id": account_id,
        "record_id": record_id,
        "record_type": code,
        "transaction_type": _RECORD_TYPE_NAMES.get(code, code),
        "tranid": row.get("tranid"),
        "change_kind": "created" if created and created == modified else "updated",
        "status": row.get("status"),
        "status_code": row.get("status_code"),
        "trandate": row.get("trandate"),
        "entity": row.get("entity"),
        "entity_id": row.get("entity_id"),
        "amount": row.get("amount"),
        "currency": row.get("currency"),
        "subsidiary": row.get("subsidiary"),
        "subsidiary_id": row.get("subsidiary_id"),
        "period": row.get("period"),
        "period_id": row.get("period_id"),
        "approval_status": row.get("approvalstatus"),
        "memo": row.get("memo"),
        "created_at": created,
        "modified_at": modified,
        "web_link": _web_link(account_id, record_id) if record_id else "",
    }


def _flag(value: Any) -> bool:
    return str(value or "").strip().upper() in ("T", "TRUE", "Y", "1")


def period_state(row: Mapping[str, Any]) -> str:
    """The most restrictive state of a period: closed, all, AR or AP locked, else open."""
    if _flag(row.get("closed")):
        return "closed"
    if _flag(row.get("alllocked")):
        return "all_locked"
    if _flag(row.get("arlocked")) and _flag(row.get("aplocked")):
        return "all_locked"
    if _flag(row.get("arlocked")):
        return "ar_locked"
    if _flag(row.get("aplocked")):
        return "ap_locked"
    return "open"


def period_snapshot(rows: list[Mapping[str, Any]]) -> dict[str, str]:
    """Period id -> state, the form the poller keeps between two reads."""
    return {str(r.get("id")): period_state(r) for r in rows if r.get("id") is not None}


def dump_snapshot(snapshot: Mapping[str, str]) -> str:
    return json.dumps(dict(snapshot), sort_keys=True, separators=(",", ":"))


def load_snapshot(raw: Any) -> dict[str, str] | None:
    """The stored snapshot; ``None`` when there is none or it cannot be read."""
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None


def period_events(rows: list[Mapping[str, Any]], previous: Mapping[str, str], *, account_id: str) -> list[dict]:
    """One payload per period whose state differs from *previous* (a new period is not a change)."""
    events: list[dict] = []
    for row in rows:
        period_id = str(row.get("id") or "")
        if not period_id or period_id not in previous:
            continue
        state = period_state(row)
        before = previous[period_id]
        if state == before:
            continue
        events.append(
            {
                "provider": PROVIDER,
                "event": ACCOUNTING_PERIOD_CHANGED,
                "message_id": f"netsuite:{account_host(account_id)}:period:{period_id}:{before}>{state}",
                "account_id": account_id,
                "period_id": period_id,
                "period": row.get("periodname"),
                "start_date": row.get("startdate"),
                "end_date": row.get("enddate"),
                "previous_state": before,
                "state": state,
                "change_kind": "reopened" if state == "open" else state,
            }
        )
    return events


def filter_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any]) -> str | None:
    """Why the trigger's filter drops a NetSuite event, ``None`` when it runs."""
    if payload.get("event") == ACCOUNTING_PERIOD_CHANGED:
        states = {s.strip().lower() for s in _strings(filter_config.get("states"))}
        if states and str(payload.get("state") or "").lower() not in states:
            return "period state is not in states"
        return None
    subsidiaries = {str(i) for i in _ids(filter_config.get("subsidiary_id"))}
    if subsidiaries and str(payload.get("subsidiary_id") or "") not in subsidiaries:
        return "subsidiary is not in subsidiary_id"
    types = {t.strip().lower() for t in _strings(filter_config.get("transaction_types"))}
    if types and str(payload.get("transaction_type") or "").lower() not in types:
        return "transaction type is not in transaction_types"
    statuses = {s.strip().casefold() for s in _strings(filter_config.get("status"))}
    if statuses and not (
        {str(payload.get("status") or "").casefold(), str(payload.get("status_code") or "").casefold()} & statuses
    ):
        return "status is not in status"
    minimum = _amount(filter_config.get("min_amount"))
    if minimum is not None:
        amount = _amount(payload.get("amount"))
        if amount is None or abs(amount) < abs(minimum):
            return "amount is below min_amount"
    kinds = {k.strip().lower() for k in _strings(filter_config.get("change_kinds"))}
    if kinds and str(payload.get("change_kind") or "").lower() not in kinds:
        return "change kind is not in change_kinds"
    return None
