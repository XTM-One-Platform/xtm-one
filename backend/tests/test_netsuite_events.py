"""NetSuite change events for integration_event assignments (#1010, slice 2)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers import netsuite
from app.services import event_poller, netsuite_events
from app.services.integration_event_filters import event_filter_reason

CREDENTIAL = "1234567_SB1||tba||ck||cs||tid||secret"


class _Redis:
    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value


def _wf(event: str = "vendor_bill_changed", filter_config: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        trigger_config={"provider": "netsuite", "event": event, "filter": filter_config or {}},
    )


def _row(record_id: str, modified: str, created: str = "2026-11-01 08:00:00", **extra) -> dict:
    row = {
        "id": record_id,
        "tranid": f"BILL-{record_id}",
        "type": "VendBill",
        "status_code": "VendBill:A",
        "status": "Open",
        "trandate": "11/20/2026",
        "entity_id": "77",
        "entity": "Acme Supplies",
        "amount": "1200.50",
        "currency": "EUR",
        "period_id": "201",
        "period": "Nov 2026",
        "subsidiary_id": "3",
        "subsidiary": "Filigran SAS",
        "approvalstatus": "2",
        "memo": "",
        "created_at": created,
        "modified_at": modified,
    }
    row.update(extra)
    return row


def test_catalog_declares_every_event_as_polling():
    events = {e["event"]: e for e in PROVIDER_CATALOG["netsuite"]["available_events"]}
    assert set(events) == set(netsuite_events.EVENTS)
    assert {e["mechanism"] for e in events.values()} == {"polling"}
    for event in netsuite_events.TRANSACTION_EVENTS:
        assert set(events[event]["filter_keys"]) == {
            "subsidiary_id",
            "transaction_types",
            "status",
            "min_amount",
            "change_kinds",
        }
    assert set(events["accounting_period_changed"]["filter_keys"]) == {"states"}


def test_every_event_is_polled_every_five_minutes():
    for event in netsuite_events.EVENTS:
        assert event_poller.POLL_INTERVALS[event] == netsuite_events.POLL_INTERVAL_SECONDS == 300


def test_event_transaction_types_are_known_record_types():
    for names in netsuite_events.EVENT_TRANSACTION_TYPES.values():
        for name in names:
            assert name in netsuite.TRANSACTION_TYPES


def test_changes_query_reads_the_event_types_from_the_cursor_oldest_first():
    sql = netsuite_events.changes_query(
        "payment_changed", {"subsidiary_id": ["3", "x", 4], "min_amount": -500}, "2026-11-20 09:15:00"
    )
    assert "t.type IN ('CustPymt', 'VendPymt')" in sql
    assert "t.lastmodifieddate >= TO_TIMESTAMP('2026-11-20 09:15:00', 'YYYY-MM-DD HH24:MI:SS')" in sql
    assert "tl.subsidiary IN (3, 4)" in sql
    assert "ABS(t.foreigntotal) >= 500.0" in sql
    assert sql.endswith("ORDER BY t.lastmodifieddate, t.id")


def test_changes_query_narrows_to_the_filter_types_of_the_event():
    sql = netsuite_events.changes_query("invoice_changed", {"transaction_types": "credit_memo"}, "2026-11-20 09:15:00")
    assert "t.type IN ('CustCred')" in sql
    # A type outside the event is ignored: the event's own types are read.
    sql = netsuite_events.changes_query("invoice_changed", {"transaction_types": "vendor_bill"}, "2026-11-20 09:15:00")
    assert "t.type IN ('CustInvc', 'CustCred')" in sql


@pytest.mark.parametrize("cursor", ["", "2026-11-20T09:15:00Z", "2026-11-20 09:15:00' OR 1=1 --"])
def test_changes_query_refuses_a_cursor_that_is_not_a_timestamp(cursor):
    with pytest.raises(ValueError):
        netsuite_events.changes_query("vendor_bill_changed", {}, cursor)


def test_transaction_event_names_the_record_and_its_modification():
    payload = netsuite_events.transaction_event(
        _row("42", "2026-11-20 09:16:00"), "vendor_bill_changed", account_id="1234567_SB1"
    )
    assert payload["message_id"] == "netsuite:1234567-sb1:42:2026-11-20 09:16:00"
    assert payload["transaction_type"] == "vendor_bill"
    assert payload["change_kind"] == "updated"
    assert payload["subsidiary_id"] == "3" and payload["currency"] == "EUR" and payload["period"] == "Nov 2026"
    assert payload["web_link"].startswith("https://1234567-sb1.app.netsuite.com/")
    created = netsuite_events.transaction_event(
        _row("43", "2026-11-20 09:16:00", created="2026-11-20 09:16:00"), "vendor_bill_changed", account_id="1"
    )
    assert created["change_kind"] == "created"


def test_period_state_and_diff():
    rows = [
        {"id": 1, "periodname": "Oct 2026", "closed": "F", "aplocked": "T", "arlocked": "F", "alllocked": "F"},
        {"id": 2, "periodname": "Nov 2026", "closed": "F", "aplocked": "F", "arlocked": "F", "alllocked": "F"},
        {"id": 3, "periodname": "Dec 2026", "closed": "F", "aplocked": "F", "arlocked": "F", "alllocked": "F"},
    ]
    assert netsuite_events.period_snapshot(rows) == {"1": "ap_locked", "2": "open", "3": "open"}
    events = netsuite_events.period_events(rows, {"1": "open", "2": "closed"}, account_id="1234567")
    by_period = {e["period_id"]: e for e in events}
    # A period absent from the last snapshot is new, not a change.
    assert set(by_period) == {"1", "2"}
    assert by_period["1"]["state"] == "ap_locked" and by_period["1"]["previous_state"] == "open"
    assert by_period["2"]["change_kind"] == "reopened"
    assert by_period["1"]["message_id"] == "netsuite:1234567:period:1:open>ap_locked"


def test_snapshot_round_trip_and_unreadable():
    assert netsuite_events.load_snapshot(netsuite_events.dump_snapshot({"1": "open"}).encode()) == {"1": "open"}
    assert netsuite_events.load_snapshot("not json") is None
    assert netsuite_events.load_snapshot(None) is None


def test_filter_reason_through_the_event_filter():
    payload = netsuite_events.transaction_event(
        _row("42", "2026-11-20 09:16:00", amount="-90"), "vendor_bill_changed", account_id="1"
    )
    assert event_filter_reason("vendor_bill_changed", payload, {"subsidiary_id": "3"}) is None
    assert event_filter_reason("vendor_bill_changed", payload, {"subsidiary_id": [5]}) == (
        "subsidiary is not in subsidiary_id"
    )
    assert event_filter_reason("vendor_bill_changed", payload, {"min_amount": 100}) == "amount is below min_amount"
    assert event_filter_reason("vendor_bill_changed", payload, {"min_amount": 50}) is None
    assert event_filter_reason("vendor_bill_changed", payload, {"status": "vendbill:a"}) is None
    assert (
        event_filter_reason("vendor_bill_changed", payload, {"status": ["Paid In Full"]}) == "status is not in status"
    )
    assert event_filter_reason("vendor_bill_changed", payload, {"change_kinds": "created"}) == (
        "change kind is not in change_kinds"
    )
    period = {"provider": "netsuite", "event": "accounting_period_changed", "state": "closed"}
    assert event_filter_reason("accounting_period_changed", period, {"states": ["closed"]}) is None
    assert event_filter_reason("accounting_period_changed", period, {"states": "open"}) == (
        "period state is not in states"
    )


@pytest.mark.asyncio
async def test_first_poll_seeds_the_cursor_and_marks_the_records_at_it_seen(monkeypatch):
    calls = []
    claimed = []

    async def _read(credential, sql, max_rows, action):
        calls.append(sql)
        if "MAX(t.lastmodifieddate)" in sql:
            return [{"cursor": "2026-11-20 09:00:00"}], False, None
        return [_row("7", "2026-11-20 09:00:00"), _row("8", "2026-11-20 09:00:00")], False, None

    async def _claim(redis, assignment_id, member, **_kwargs):
        claimed.append((assignment_id, member))
        return True

    monkeypatch.setattr(netsuite, "read_suiteql", _read)
    monkeypatch.setattr(event_poller, "claim_seen", _claim)
    redis, wf = _Redis(), _wf()
    assert await event_poller._poll_netsuite_changes(CREDENTIAL, "vendor_bill_changed", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-20 09:00:00"
    assert "MAX(t.lastmodifieddate)" in calls[0]
    assert "TO_TIMESTAMP('2026-11-20 09:00:00'" in calls[1]
    # The next poll reads >= the cursor: the records at the seed are already seen, so none runs.
    host = netsuite_events.account_host("1234567_SB1")
    assert claimed == [
        (wf.id, f"netsuite:{host}:7:2026-11-20 09:00:00"),
        (wf.id, f"netsuite:{host}:8:2026-11-20 09:00:00"),
    ]


@pytest.mark.asyncio
async def test_first_poll_keeps_no_cursor_when_the_seed_records_cannot_be_read(monkeypatch):
    async def _read(credential, sql, max_rows, action):
        if "MAX(t.lastmodifieddate)" in sql:
            return [{"cursor": "2026-11-20 09:00:00"}], False, None
        return [], False, "Error: NetSuite 429 concurrency limit"

    monkeypatch.setattr(netsuite, "read_suiteql", _read)
    redis, wf = _Redis(), _wf()
    assert await event_poller._poll_netsuite_changes(CREDENTIAL, "vendor_bill_changed", wf, redis) == []
    assert f"event_cursor:{wf.id}" not in redis.values


@pytest.mark.asyncio
async def test_first_poll_on_an_account_with_no_record_starts_at_the_epoch(monkeypatch):
    async def _read(*_args, **_kwargs):
        return [{"cursor": None}], False, None

    monkeypatch.setattr(netsuite, "read_suiteql", _read)
    redis, wf = _Redis(), _wf()
    assert await event_poller._poll_netsuite_changes(CREDENTIAL, "vendor_bill_changed", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] == netsuite_events.EPOCH


@pytest.mark.asyncio
async def test_poll_dispatches_oldest_first_and_moves_the_cursor(monkeypatch):
    seen = {}

    async def _read(credential, sql, max_rows, action):
        seen.update(sql=sql, max_rows=max_rows)
        return [_row("9", "2026-11-20 09:20:00"), _row("8", "2026-11-20 09:16:00")], False, None

    monkeypatch.setattr(netsuite, "read_suiteql", _read)
    wf = _wf(filter_config={"subsidiary_id": "3"})
    redis = _Redis({f"event_cursor:{wf.id}": b"2026-11-20 09:15:00"})
    events = await event_poller._poll_netsuite_changes(CREDENTIAL, "vendor_bill_changed", wf, redis)
    assert [e["record_id"] for e in events] == ["8", "9"]
    assert seen["max_rows"] == netsuite_events.PAGE + 1
    assert "TO_TIMESTAMP('2026-11-20 09:15:00'" in seen["sql"] and "tl.subsidiary IN (3)" in seen["sql"]
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-20 09:20:00"
    assert all(e["account_id"] == "1234567_SB1" for e in events)


@pytest.mark.asyncio
async def test_a_failed_poll_keeps_the_cursor(monkeypatch):
    async def _read(*_args, **_kwargs):
        return [], False, "Error: NetSuite 429 concurrency limit"

    monkeypatch.setattr(netsuite, "read_suiteql", _read)
    wf = _wf()
    redis = _Redis({f"event_cursor:{wf.id}": "2026-11-20 09:15:00"})
    assert await event_poller._poll_netsuite_changes(CREDENTIAL, "invoice_changed", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-20 09:15:00"


@pytest.mark.asyncio
async def test_period_poll_snapshots_first_then_reports_changes(monkeypatch):
    state = {"closed": "F"}

    async def _read(*_args, **_kwargs):
        row = {"id": 7, "periodname": "Oct 2026", "aplocked": "F", "arlocked": "F", "alllocked": "F", **state}
        return [row], False, None

    monkeypatch.setattr(netsuite, "read_suiteql", _read)
    redis, wf = _Redis(), _wf("accounting_period_changed")
    assert await event_poller._poll_netsuite_changes(CREDENTIAL, "accounting_period_changed", wf, redis) == []
    assert await event_poller._poll_netsuite_changes(CREDENTIAL, "accounting_period_changed", wf, redis) == []
    state["closed"] = "T"
    events = await event_poller._poll_netsuite_changes(CREDENTIAL, "accounting_period_changed", wf, redis)
    assert [(e["period_id"], e["previous_state"], e["state"]) for e in events] == [("7", "open", "closed")]
    # Read again, the same state is no change.
    assert await event_poller._poll_netsuite_changes(CREDENTIAL, "accounting_period_changed", wf, redis) == []


@pytest.mark.asyncio
async def test_read_suiteql_refuses_a_credential_without_account():
    rows, more, err = await netsuite.read_suiteql("", "SELECT 1 FROM dual", 1, "probe")
    assert rows == [] and more is False and err
