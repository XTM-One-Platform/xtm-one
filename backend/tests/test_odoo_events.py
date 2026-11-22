"""Odoo polled events for integration_event assignments (#1016, slice 2)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers.odoo import events as odoo_events
from app.integrations.providers.odoo._client import OdooCredential
from app.services import event_poller
from app.services.integration_event_filters import event_filter_reason

TOKEN = "https://acme.example.com||acme||bot@example.com||k3y"


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


def _wf(event: str = "journal_entry_posted", filter_config: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(), trigger_config={"provider": "odoo", "event": event, "filter": filter_config or {}}
    )


def _move(move_id: int, stamp: str, **extra) -> dict:
    return {
        "id": move_id,
        "name": f"MISC/2026/{move_id:04d}",
        "ref": "",
        "date": "2026-11-20",
        "state": "posted",
        "move_type": "entry",
        "journal_id": [3, "MISC Miscellaneous Operations"],
        "company_id": [2, "Example INC"],
        "partner_id": False,
        "amount_total": 120.5,
        "amount_residual": 0,
        "currency_id": [2, "USD"],
        "create_date": stamp,
        "write_date": stamp,
        **extra,
    }


def test_catalog_declares_the_three_events_as_polling_with_filter_keys():
    events = {e["event"]: e for e in PROVIDER_CATALOG["odoo"]["available_events"]}
    assert set(events) == set(odoo_events.EVENTS)
    for event in events.values():
        assert event["mechanism"] == "polling"
        assert set(event["filter_keys"]) == set(odoo_events.FILTER_KEYS)
    for event in odoo_events.EVENTS:
        assert event_poller.POLL_INTERVALS[event] == odoo_events.POLL_INTERVAL_SECONDS == 60


def test_poll_domain_narrows_ids_in_the_domain_and_leaves_names_to_the_payload():
    domain = odoo_events.poll_domain(
        "vendor_bill_created", "2026-11-20 10:00:00", {"company": [2], "journal": ["BILL"], "state": "posted"}
    )
    assert ["create_date", ">=", "2026-11-20 10:00:00"] in domain
    assert ["move_type", "in", ["in_invoice", "in_refund"]] in domain
    assert ["company_id", "in", [2]] in domain
    assert not any(term[0] == "journal_id" for term in domain)
    assert ["state", "in", ["posted"]] in domain
    posted = odoo_events.poll_domain("journal_entry_posted", "2026-11-20 10:00:00", {})
    assert posted == [["write_date", ">=", "2026-11-20 10:00:00"], ["state", "=", "posted"]]


def test_record_event_keys_a_posted_entry_change_and_a_creation_once():
    cred = OdooCredential.parse(TOKEN)
    payload = odoo_events.record_event(_move(7, "2026-11-20 10:05:00"), "journal_entry_posted", credential=cred)
    assert payload["message_id"] == "odoo:acme:account.move:7@2026-11-20 10:05:00"
    assert payload["company"] == "Example INC" and payload["journal_id"] == 3 and payload["state"] == "posted"
    assert payload["entity"]["amount"] == 120.5 and payload["entity"]["currency"] == "USD"
    assert "k3y" not in str(payload)
    bill = odoo_events.record_event(
        _move(8, "2026-11-20 10:06:00", move_type="in_invoice"), "vendor_bill_created", credential=cred
    )
    assert bill["message_id"] == "odoo:acme:account.move:8"
    assert bill["entity"]["move_type"] == "in_invoice"


@pytest.mark.parametrize(
    ("flt", "reason"),
    [
        ({}, None),
        ({"company": "example inc"}, None),
        ({"company": [2]}, None),
        ({"company": ["Other LLC"]}, "company is not in company"),
        ({"journal": "misc"}, None),
        ({"journal": [3]}, None),
        ({"journal": "BANK"}, "journal is not in journal"),
        ({"state": ["Posted"]}, None),
        ({"state": "draft"}, "state is not in state"),
    ],
)
def test_filter_reason_matches_ids_names_codes_and_states(flt, reason):
    payload = odoo_events.record_event(
        _move(7, "2026-11-20 10:05:00"), "journal_entry_posted", credential=OdooCredential.parse(TOKEN)
    )
    assert odoo_events.filter_reason(payload, flt) == reason
    assert event_filter_reason("journal_entry_posted", payload, flt) == reason


@pytest.mark.asyncio
async def test_first_poll_starts_from_now():
    redis, wf = _Redis(), _wf()
    assert await event_poller._poll_odoo_records(TOKEN, "journal_entry_posted", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"]


@pytest.mark.asyncio
async def test_poll_dispatches_oldest_first_and_moves_the_cursor(monkeypatch):
    wf = _wf(filter_config={"company": [2]})
    redis = _Redis({f"event_cursor:{wf.id}": b"2026-11-20 10:00:00"})
    seen = {}

    async def _list(token, event_type, since, filter_config, *, limit):
        seen.update(since=since, filter=filter_config, limit=limit)
        return (
            [_move(2, "2026-11-20 10:04:00"), _move(1, "2026-11-20 10:01:00")][::-1],
            OdooCredential.parse(token),
            None,
        )

    monkeypatch.setattr(odoo_events, "list_changed_records", _list)
    events = await event_poller._poll_odoo_records(TOKEN, "journal_entry_posted", wf, redis)
    assert seen == {"since": "2026-11-20 10:00:00", "filter": {"company": [2]}, "limit": odoo_events.PAGE + 1}
    assert [e["entity"]["id"] for e in events] == [1, 2]
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-20 10:04:00"


@pytest.mark.asyncio
async def test_a_failed_poll_keeps_the_cursor(monkeypatch):
    wf = _wf("payment_registered")
    redis = _Redis({f"event_cursor:{wf.id}": "2026-11-20 10:00:00"})

    async def _list(token, *_, **__):
        return [], OdooCredential.parse(token), "Odoo refused the login or API key"

    monkeypatch.setattr(odoo_events, "list_changed_records", _list)
    assert await event_poller._poll_odoo_records(TOKEN, "payment_registered", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-20 10:00:00"


@pytest.mark.asyncio
async def test_list_changed_records_reads_every_visible_company_ordered_by_stamp(monkeypatch):
    calls = []

    class _Client:
        def __init__(self, credential):
            self.credential = credential

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def search_read(self, model, domain, fields, **kwargs):
            calls.append((model, domain, kwargs))
            if model == "res.company":
                return [{"id": 1}, {"id": 2}]
            return [_move(5, "2026-11-20 10:02:00"), {"id": 6, "write_date": False}]

    monkeypatch.setattr(odoo_events, "OdooClient", _Client)
    records, cred, err = await odoo_events.list_changed_records(
        TOKEN, "journal_entry_posted", "2026-11-20 10:00:00", {}
    )
    assert err is None and cred.database == "acme"
    assert [r["id"] for r in records] == [5]
    model, _, kwargs = calls[-1]
    assert model == "account.move"
    assert kwargs["company_ids"] == [1, 2] and kwargs["order"] == "write_date asc, id asc"


@pytest.mark.asyncio
async def test_list_changed_records_names_a_missing_credential_part():
    records, _, err = await odoo_events.list_changed_records("k3y", "payment_registered", "2026-11-20 10:00:00", {})
    assert records == [] and err == "the Odoo integration lacks the Odoo URL"


@pytest.mark.asyncio
async def test_poll_provider_routes_odoo_events(monkeypatch):
    called = {}

    async def _poll(credential, event_type, wf, redis):
        called["event"] = event_type
        return []

    monkeypatch.setattr(event_poller, "_poll_odoo_records", _poll)
    await event_poller._poll_provider("odoo", "vendor_bill_created", TOKEN, _wf(), _Redis(), "k")
    assert called == {"event": "vendor_bill_created"}
