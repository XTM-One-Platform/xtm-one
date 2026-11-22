# SPDX-License-Identifier: Apache-2.0

"""Odoo events for ``integration_event`` assignments: polled, no webhook.

Odoo has no outbound webhook in its external API (automation rules can call
one, but only an administrator sets them up, per database). What every
record does carry is ``create_date`` and ``write_date`` (UTC), so XTM One polls
(``event_poller._poll_odoo_records``, every ``POLL_INTERVAL_SECONDS``):

* ``journal_entry_posted``: a journal entry (``account.move``) is posted, or a
  posted entry changes (one run per change, keyed on its ``write_date``);
* ``vendor_bill_created``: a vendor bill or refund (``in_invoice``,
  ``in_refund``) is created;
* ``payment_registered``: a payment (``account.payment``) is created.

Filter keys, applied in the Odoo domain so the read stays small and again on
the payload (:func:`filter_reason`): ``company`` (ids or names), ``journal``
(ids, codes or names) and ``state`` (Odoo states: draft, posted, cancel...).
The first poll starts from now: enabling a trigger never replays history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from ._client import OdooClient, OdooCredential, OdooError
from ._render import m2o, money

PROVIDER = "odoo"

#: One poll a minute: Odoo's rate limit is per user, the read is one search.
POLL_INTERVAL_SECONDS = 60

#: The records one poll reads after the cursor (one more tells if there are more).
PAGE = 50

#: Odoo writes its datetimes as ``YYYY-MM-DD HH:MM:SS`` in UTC.
ODOO_DATETIME = "%Y-%m-%d %H:%M:%S"

_MOVE_FIELDS = [
    "name",
    "ref",
    "date",
    "state",
    "move_type",
    "journal_id",
    "company_id",
    "partner_id",
    "amount_total",
    "amount_residual",
    "currency_id",
    "invoice_date",
    "invoice_date_due",
    "payment_state",
    "create_date",
    "write_date",
]
_PAYMENT_FIELDS = [
    "name",
    "date",
    "state",
    "payment_type",
    "partner_type",
    "journal_id",
    "company_id",
    "partner_id",
    "amount",
    "currency_id",
    "memo",
    "create_date",
    "write_date",
]


#: Event -> (model, the datetime field its cursor reads, the fixed domain, fields, label).
EVENT_SPECS: dict[str, tuple[str, str, list[Any], list[str], str]] = {
    "journal_entry_posted": ("account.move", "write_date", [["state", "=", "posted"]], _MOVE_FIELDS, "journal entry"),
    "vendor_bill_created": (
        "account.move",
        "create_date",
        [["move_type", "in", ["in_invoice", "in_refund"]]],
        _MOVE_FIELDS,
        "vendor bill",
    ),
    "payment_registered": ("account.payment", "create_date", [], _PAYMENT_FIELDS, "payment"),
}
EVENTS = frozenset(EVENT_SPECS)

#: The filter keys an assignment may set, with what they hold (the catalog's ``filter_keys``).
FILTER_KEYS: dict[str, str] = {
    "company": "Only records of these companies (ids or names)",
    "journal": "Only records of these journals (ids, codes or names)",
    "state": "Only records in these Odoo states (draft, posted, cancel; payments: in_process, paid...)",
}


def now_cursor() -> str:
    return datetime.now(UTC).strftime(ODOO_DATETIME)


def _values(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    return [str(v).strip() for v in items if str(v).strip()]


def _ids(values: Iterable[str]) -> list[int]:
    return [int(v) for v in values if v.isdigit()]


def poll_domain(event_type: str, since: str, filter_config: Mapping[str, Any] | None) -> list[Any]:
    """The search domain: the event's records stamped at or after *since*, narrowed by the filter.

    A company or journal named by id narrows in the domain; one named by name
    or code is matched on the payload (:func:`filter_reason`), whose names Odoo
    answers.
    """
    _, stamp, fixed, _, _ = EVENT_SPECS[event_type]
    domain: list[Any] = [[stamp, ">=", since], *fixed]
    flt = filter_config or {}
    companies = _values(flt.get("company"))
    if companies and len(_ids(companies)) == len(companies):
        domain.append(["company_id", "in", _ids(companies)])
    journals = _values(flt.get("journal"))
    if journals and len(_ids(journals)) == len(journals):
        domain.append(["journal_id", "in", _ids(journals)])
    states = _values(flt.get("state"))
    if states:
        domain.append(["state", "in", states])
    return domain


def record_event(record: Mapping[str, Any], event_type: str, *, credential: OdooCredential) -> dict[str, Any]:
    """The payload one record hands to the assignment, ``message_id`` naming this change once."""
    model, stamp, _, _, label = EVENT_SPECS[event_type]
    record_id = record.get("id")
    company_id, company = m2o(record.get("company_id"))
    journal_id, journal = m2o(record.get("journal_id"))
    partner_id, partner = m2o(record.get("partner_id"))
    _, currency = m2o(record.get("currency_id"))
    stamped = str(record.get(stamp) or "")
    state = record.get("state") or ""
    amount = record.get("amount_total") if model == "account.move" else record.get("amount")
    entity: dict[str, Any] = {
        "type": model,
        "id": record_id,
        "name": record.get("name") or "",
        "reference": record.get("ref") or record.get("memo") or "",
        "date": record.get("date") or "",
        "state": state,
        "company_id": company_id,
        "company": company,
        "journal_id": journal_id,
        "journal": journal,
        "partner_id": partner_id,
        "partner": partner,
        "amount": money(amount),
        "currency": currency,
        "created_at": record.get("create_date") or "",
        "changed_at": record.get("write_date") or "",
        "instance_url": credential.base_url,
        "database": credential.database,
    }
    for key in ("move_type", "invoice_date", "invoice_date_due", "payment_state", "payment_type", "partner_type"):
        if record.get(key) not in (None, False):
            entity[key] = record.get(key)
    if model == "account.move":
        entity["amount_residual"] = money(record.get("amount_residual"))
    change = f"@{stamped}" if stamp == "write_date" else ""
    return {
        "message_id": f"odoo:{credential.database}:{model}:{record_id}{change}" if record_id else "",
        "provider": PROVIDER,
        "event": event_type,
        "occurred_at": stamped,
        "company": company,
        "company_id": company_id,
        "journal": journal,
        "journal_id": journal_id,
        "state": state,
        "entity": entity,
        "summary": (
            f"Odoo {label} {entity['name'] or record_id} ({state}) {entity['amount']} {currency} "
            f"for {partner or 'no partner'}, {company or 'no company'}"
        ),
    }


def _matches(wanted: list[str], record_id: Any, *names: Any) -> bool:
    folded = {w.casefold() for w in wanted}
    candidates = {str(record_id)} if record_id not in (None, "") else set()
    candidates.update(str(n).casefold() for n in names if n)
    return bool(folded & candidates)


def filter_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any] | None) -> str | None:
    """Why the filter drops an Odoo *payload*; ``None`` when it runs.

    ``company`` and ``journal`` match an id or a name (a journal also its
    code, the leading word Odoo shows), case-insensitively; ``state`` the
    record's state. A key with no value is no filter.
    """
    flt = filter_config or {}
    companies = _values(flt.get("company"))
    if companies and not _matches(companies, payload.get("company_id"), payload.get("company")):
        return "company is not in company"
    journals = _values(flt.get("journal"))
    if journals:
        name = str(payload.get("journal") or "")
        if not _matches(journals, payload.get("journal_id"), name, name.split(" ", 1)[0]):
            return "journal is not in journal"
    states = _values(flt.get("state"))
    if states and str(payload.get("state") or "").casefold() not in {s.casefold() for s in states}:
        return "state is not in state"
    return None


async def list_changed_records(
    token: str, event_type: str, since: str, filter_config: Mapping[str, Any] | None, *, limit: int = PAGE + 1
) -> tuple[list[dict[str, Any]], OdooCredential, str | None]:
    """``(records, credential, error)``: the event's records stamped at or after *since*, oldest first.

    The companies are the filter's ids, else every company the user may
    switch to (an event of any of them is an event), else the defaults of the
    integration.
    """
    cred = OdooCredential.parse(token)
    missing = cred.missing()
    if missing:
        return [], cred, f"the Odoo integration lacks {missing}"
    model, stamp, _, fields, label = EVENT_SPECS[event_type]
    flt = filter_config or {}
    companies = _ids(_values(flt.get("company")))
    try:
        async with OdooClient(credential=cred) as cli:
            if not companies and not cred.company_ids:
                rows = await cli.search_read("res.company", [], ["id"], action="list the companies")
                companies = [int(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")]
            records = await cli.search_read(
                model,
                poll_domain(event_type, since, flt),
                fields,
                limit=limit,
                order=f"{stamp} asc, id asc",
                company_ids=companies,
                action=f"poll new {label} records",
            )
    except OdooError as exc:
        return [], cred, str(exc).removeprefix("Error: ")
    return [r for r in records if isinstance(r, dict) and r.get(stamp)], cred, None


def stamp_of(record: Mapping[str, Any], event_type: str) -> str:
    return str(record.get(EVENT_SPECS[event_type][1]) or "")
