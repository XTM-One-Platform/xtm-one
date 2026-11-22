"""Pennylane changelog events for integration_event assignments (#1009)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import event_poller, pennylane_events
from app.services.integration_event_filters import event_filter_reason, filter_expression_error

NOW = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)


class _Redis:
    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None, nx=False):
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)


def _wf(event: str = "supplier_invoice_created", filter_config: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        trigger_config={"provider": "pennylane", "event": event, "filter": filter_config or {}},
    )


def _change(record_id: int, operation: str, at: str) -> dict:
    return {"id": record_id, "operation": operation, "processed_at": at, "updated_at": at, "created_at": at}


def _supplier_invoice(**extra) -> dict:
    record = {
        "id": 7,
        "label": "Cloud hosting",
        "invoice_number": "F-2026-07",
        "amount": "1200.50",
        "currency_amount": "1200.50",
        "currency": "EUR",
        "payment_status": "to_be_paid",
        "paid": False,
        "supplier": {"id": 42, "url": "https://app.pennylane.com/api/external/v2/suppliers/42"},
        "date": "2026-11-20",
        "deadline": "2026-12-20",
    }
    record.update(extra)
    return record


def test_catalog_declares_every_event_as_polling_with_filter_keys():
    events = {e["event"]: e for e in PROVIDER_CATALOG["pennylane"]["available_events"]}
    assert set(events) == set(pennylane_events.EVENTS)
    assert {e["mechanism"] for e in events.values()} == {"polling"}
    for entry in events.values():
        assert set(entry["filter_keys"]) == set(pennylane_events.FILTER_KEYS)


def test_every_event_is_polled_every_five_minutes():
    for event in pennylane_events.EVENTS:
        assert event_poller.POLL_INTERVALS[event] == pennylane_events.POLL_INTERVAL_SECONDS == 300


def test_since_is_clamped_to_the_changelog_retention():
    assert pennylane_events.clamp_since("2026-01-01T00:00:00Z", now=NOW) == "2026-10-26T00:00:00.000000Z"
    assert pennylane_events.clamp_since("2026-11-21T08:00:00Z", now=NOW) == "2026-11-21T08:00:00.000000Z"


def test_selected_keeps_the_event_operations_once_per_record():
    entries = [
        _change(1, "insert", "2026-11-21T08:00:00Z"),
        _change(2, "update", "2026-11-21T08:01:00Z"),
        _change(1, "update", "2026-11-21T08:02:00Z"),
        _change(3, "delete", "2026-11-21T08:03:00Z"),
        {"operation": "insert"},
    ]
    assert [e["id"] for e in pennylane_events.selected(entries, "supplier_invoice_created")] == [1]
    updated = pennylane_events.selected(entries, "supplier_invoice_updated")
    assert [(e["id"], e["processed_at"]) for e in updated] == [(2, "2026-11-21T08:01:00Z"), (1, "2026-11-21T08:02:00Z")]
    paid = pennylane_events.selected(entries, "customer_invoice_paid")
    assert [e["id"] for e in paid] == [2, 1]


def test_record_event_carries_amounts_parties_and_a_stable_id():
    payload = pennylane_events.record_event(
        _supplier_invoice(), _change(7, "insert", "2026-11-21T08:00:00Z"), "supplier_invoice_created", company_id="99"
    )
    assert payload is not None
    assert payload["message_id"] == "pennylane:99:supplier_invoice_created:7"
    assert payload["provider"] == "pennylane"
    assert payload["amount"] == 1200.5
    assert payload["supplier_id"] == 42
    assert payload["status"] == "to_be_paid"
    assert "customer_id" not in payload


def test_an_update_runs_once_per_change():
    first = pennylane_events.record_event(
        _supplier_invoice(), _change(7, "update", "2026-11-21T08:00:00Z"), "supplier_invoice_updated"
    )
    second = pennylane_events.record_event(
        _supplier_invoice(), _change(7, "update", "2026-11-21T09:00:00Z"), "supplier_invoice_updated"
    )
    assert first and second and first["message_id"] != second["message_id"]


def test_customer_invoice_paid_needs_a_paid_invoice_and_runs_once():
    change = _change(5, "update", "2026-11-21T08:00:00Z")
    unpaid = {"id": 5, "paid": False, "status": "upcoming", "amount": "10", "customer": {"id": 3}}
    assert pennylane_events.record_event(unpaid, change, "customer_invoice_paid") is None
    paid = dict(unpaid, paid=True, status="paid")
    event = pennylane_events.record_event(paid, change, "customer_invoice_paid")
    later = pennylane_events.record_event(paid, _change(5, "update", "2026-11-21T09:00:00Z"), "customer_invoice_paid")
    assert event and later and event["message_id"] == later["message_id"] == "pennylane:default:customer_invoice_paid:5"
    assert event["customer_id"] == 3


def test_transaction_matched_needs_matched_invoices_and_reruns_when_they_change():
    record = {"id": 11, "amount": "-50", "journal": {"id": 2}, "bank_account": {"id": 4}}
    change = _change(11, "update", "2026-11-21T08:00:00Z")
    assert pennylane_events.record_event(record, change, "transaction_matched") is None
    one = pennylane_events.record_event(record, change, "transaction_matched", matched_invoice_ids=[8])
    two = pennylane_events.record_event(record, change, "transaction_matched", matched_invoice_ids=[9, 8])
    assert one and two
    assert one["message_id"] == "pennylane:default:transaction_matched:11:8"
    assert two["message_id"] == "pennylane:default:transaction_matched:11:8,9"
    assert two["matched_invoice_ids"] == [8, 9]
    assert two["journal_id"] == 2 and two["bank_account_id"] == 4


def test_filter_reason_reads_the_amount_party_and_status_keys():
    event = pennylane_events.record_event(
        _supplier_invoice(), _change(7, "insert", "2026-11-21T08:00:00Z"), "supplier_invoice_created"
    )
    assert event is not None
    reason = pennylane_events.filter_reason
    assert reason(event, {"min_amount": 1000}) is None
    assert reason(event, {"min_amount": "5000"}) == "amount is under min_amount"
    assert reason(event, {"max_amount": 100}) == "amount is over max_amount"
    assert reason(event, {"min_amount": "lots"}) == "min_amount is not a number"
    assert reason(event, {"supplier_id": [42, 43]}) is None
    assert reason(event, {"supplier_id": "43"}) == "supplier_id is not in supplier_id"
    assert reason(event, {"status": "TO_BE_PAID"}) is None
    assert reason(event, {"currency": "USD"}) == "currency is not in currency"
    assert reason(event, {"invoice_number": {"starts_with": "F-2026"}}) is None
    assert reason(event, {"invoice_number": {"starts_with": "X"}}) is not None


def test_the_event_filter_routes_pennylane_events():
    event = pennylane_events.record_event(
        _supplier_invoice(), _change(7, "insert", "2026-11-21T08:00:00Z"), "supplier_invoice_created"
    )
    assert event is not None
    assert event_filter_reason("supplier_invoice_created", event, {"min_amount": 2000}) == "amount is under min_amount"
    assert event_filter_reason("supplier_invoice_created", event, {"supplier_id": 42}) is None
    assert filter_expression_error({"min_amount": 1000, "supplier_id": [42]}) is None


@pytest.mark.asyncio
async def test_first_poll_starts_from_now():
    redis = _Redis()
    wf = _wf()
    events = await event_poller._poll_pennylane_changes("tok", "supplier_invoice_created", wf, redis)
    assert events == []
    assert redis.values[f"event_cursor:{wf.id}"]


@pytest.mark.asyncio
async def test_poll_moves_the_cursor_past_every_entry_and_reads_the_selected_ones(monkeypatch):
    wf = _wf("supplier_invoice_created")
    redis = _Redis({f"event_cursor:{wf.id}": "2026-11-21T07:00:00Z"})
    asked: dict = {}

    async def _list(credential, event_type, since, *, limit):
        asked.update(credential=credential, since=since, limit=limit)
        return (
            [
                _change(8, "update", "2026-11-21T08:02:00Z"),
                _change(7, "insert", "2026-11-21T08:01:00Z"),
            ],
            False,
            None,
        )

    async def _events(credential, event_type, changes):
        return [{"message_id": f"m{c['id']}", "id": c["id"]} for c in changes]

    monkeypatch.setattr(pennylane_events, "list_changes", _list)
    monkeypatch.setattr(pennylane_events, "change_events", _events)
    events = await event_poller._poll_pennylane_changes("99||tok", "supplier_invoice_created", wf, redis)
    assert [e["id"] for e in events] == [7]
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-21T08:02:00Z"
    assert asked == {"credential": "99||tok", "since": "2026-11-21T07:00:00Z", "limit": 50}


@pytest.mark.asyncio
async def test_a_failed_poll_keeps_the_cursor(monkeypatch):
    wf = _wf()
    redis = _Redis({f"event_cursor:{wf.id}": "2026-11-21T07:00:00Z"})

    async def _list(*_args, **_kwargs):
        return [], False, "Error: Pennylane refused read the supplier_invoices changelog (403: scope missing)."

    monkeypatch.setattr(pennylane_events, "list_changes", _list)
    assert await event_poller._poll_pennylane_changes("tok", "supplier_invoice_created", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-21T07:00:00Z"


@pytest.mark.asyncio
async def test_change_events_reads_each_record_and_skips_a_deleted_one(monkeypatch):
    from app.integrations.providers.pennylane import _common

    paths: list[str] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    async def _get(self, client, path, params=None, action=""):
        paths.append(path)
        if path.endswith("/404"):
            return None, "Error: Pennylane found nothing (404)."
        return _supplier_invoice(id=int(path.rsplit("/", 1)[1])), None

    monkeypatch.setattr(_common.PennylaneContext, "client", lambda self: _Client())
    monkeypatch.setattr(_common.PennylaneContext, "get", _get)
    events = await pennylane_events.change_events(
        "99||tok",
        "supplier_invoice_created",
        [_change(7, "insert", "2026-11-21T08:00:00Z"), _change(404, "insert", "2026-11-21T08:01:00Z")],
    )
    assert paths == ["supplier_invoices/7", "supplier_invoices/404"]
    assert [e["message_id"] for e in events] == ["pennylane:99:supplier_invoice_created:7"]
