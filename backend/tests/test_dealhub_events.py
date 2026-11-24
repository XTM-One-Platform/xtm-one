"""DealHub quote events: webhook payloads, bearer check, filter, poll diff, catalog."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import dealhub_events
from app.services.event_poller import POLL_INTERVALS
from app.services.integration_event_filters import event_filter_reason

# Recorded from DealHub's "Quote Webhook Events Reference" (quotePendingApproval).
PENDING_APPROVAL = {
    "event": "quotePendingApproval",
    "status": "ApprovalProcess",
    "newly_created_quote": False,
    "external_opportunity_id": "786865656565",
    "external_quote_id": "",
    "external_customer_id": "65545425413",
    "dealhub_opportunity_id": "2323232565685498",
    "dealhub_quote_id": "6563232565685498",
    "primary_quote": True,
    "executed_by": "john.smith@mydomain.com",
    "impersonated_by": "",
    "execution_date": "2021-10-22 16:39:08",
    "note_for_approval": "some note",
    "quote_summary": {
        "currency": "USD",
        "total_list_price": 4600.00,
        "total_net_price": 3000.00,
        "total_discount": 34.78,
        "sales_discount": 34.78,
    },
    "approval_details": {
        "status": "approved",
        "reviewed_by": "john.smith1@mydomain.com",
        "impersonated_by": "sysAdmin@mydomain.com",
        "review_date": "2021-10-22 16:39:08",
        "reviewer_comment": "",
    },
    "additional_data": [{"q1": "some text"}, {"q2": "red;green;blue"}],
}


def _quote(quote_id: str, status: str, **info) -> dict:
    return {
        "dealhub_quote_id": quote_id,
        "status": status,
        "info": {"dealhub_quote_name": f"Q {quote_id}", "primary_quote": True, **info},
        "summary": {"currency": "EUR", "total_discount": 12.5, "total_net_price": 1000},
    }


def test_webhook_event_names_map_to_trigger_events():
    assert dealhub_events.event_of("quotePendingApproval") == "quote_pending_approval"
    assert dealhub_events.event_of("quoteWon") == "quote_won"
    assert dealhub_events.event_of("somethingElse") is None


def test_webhook_payload_carries_quote_totals_and_approval():
    payload = dealhub_events.webhook_payload(PENDING_APPROVAL, "quote_pending_approval")
    assert payload is not None
    assert payload["provider"] == "dealhub"
    assert payload["quote_id"] == "6563232565685498"
    assert payload["quote_status"] == "ApprovalProcess"
    assert payload["owner"] == "john.smith@mydomain.com"
    assert payload["total_discount"] == pytest.approx(34.78)
    assert payload["currency"] == "USD"
    assert payload["approval_status"] == "approved"
    assert payload["playbook_answers"] == {"q1": "some text", "q2": "red;green;blue"}
    assert payload["message_id"] == "dealhub:6563232565685498:quote_pending_approval:2021-10-22 16:39:08"


def test_webhook_payload_without_quote_id_is_none():
    assert dealhub_events.webhook_payload({"event": "quoteWon"}, "quote_won") is None


@pytest.mark.parametrize(
    ("header", "ok"),
    [
        ("Bearer s3cret", True),
        ("bearer s3cret", True),
        ("Bearer other", False),
        ("Basic s3cret", False),
        ("Bearer s3crét", False),
        ("Bearer s\u00e9cret", False),
        ("", False),
        (None, False),
    ],
)
def test_bearer_check(header, ok):
    assert dealhub_events.verify_bearer(header, ["s3cret"]) is ok


def test_bearer_check_refuses_a_non_ascii_token_without_raising():
    assert dealhub_events.verify_bearer("Bearer sécretÿ", ["s3cret"]) is False


def test_bearer_check_refuses_without_tokens():
    assert dealhub_events.verify_bearer("Bearer s3cret", []) is False


def test_webhook_tokens_rotation():
    assert dealhub_events.webhook_tokens({"webhook_token": " old , new \n"}) == ["old", "new"]
    assert dealhub_events.webhook_tokens({"webhook_token": 12}) == []
    assert dealhub_events.webhook_tokens({}) == []


def test_replay_window():
    now = datetime(2021, 10, 25, tzinfo=UTC)
    assert dealhub_events.delivery_too_old(PENDING_APPROVAL, now) is False
    assert dealhub_events.delivery_too_old(PENDING_APPROVAL, datetime(2022, 1, 1, tzinfo=UTC)) is True
    assert dealhub_events.delivery_too_old({"execution_date": ""}, now) is False


@pytest.mark.parametrize(
    ("filter_config", "dropped"),
    [
        ({}, False),
        ({"status": ["approvalprocess"]}, False),
        ({"status": "Won"}, True),
        ({"discount_min": 30}, False),
        ({"discount_min": 40}, True),
        ({"discount_max": 20}, True),
        ({"owner": ["mydomain.com"]}, False),
        ({"owner": ["@mydomain.com"]}, False),
        ({"owner": "john.smith@mydomain.com"}, False),
        ({"owner": ["other.com"]}, True),
        ({"primary_only": True}, False),
        ({"external_opportunity_id": ["786865656565"]}, False),
        ({"external_opportunity_id": ["1"]}, True),
    ],
)
def test_filter_keys(filter_config, dropped):
    payload = dealhub_events.webhook_payload(PENDING_APPROVAL, "quote_pending_approval")
    reason = event_filter_reason("quote_pending_approval", payload, filter_config)
    assert (reason is not None) is dropped


def test_filter_primary_only_drops_a_secondary_quote():
    payload = dealhub_events.webhook_payload({**PENDING_APPROVAL, "primary_quote": False}, "quote_pending_approval")
    assert event_filter_reason("quote_pending_approval", payload, {"primary_only": "true"}) is not None


def test_first_poll_records_and_runs_nothing():
    events, statuses = dealhub_events.poll_events([_quote("a", "ApprovalProcess")], "quote_pending_approval", None)
    assert events == []
    assert statuses == {"a": "ApprovalProcess"}


def test_poll_runs_a_status_change_to_the_event_status_once():
    known = {"a": "Draft", "b": "ApprovalProcess"}
    quotes = [
        _quote("a", "ApprovalProcess", submitted_by="ann@x.io", submission_date="2026-11-22 10:00:00"),
        _quote("b", "ApprovalProcess"),
        _quote("c", "Draft"),
    ]
    events, statuses = dealhub_events.poll_events(quotes, "quote_pending_approval", known)
    assert [e["quote_id"] for e in events] == ["a"]
    event = events[0]
    assert event["previous_status"] == "Draft"
    assert event["owner"] == "ann@x.io"
    assert event["delivery"] == "poll"
    assert event["message_id"] == "dealhub:a:quote_pending_approval:poll:2026-11-22 10:00:00"
    assert statuses == {"a": "ApprovalProcess", "b": "ApprovalProcess", "c": "Draft"}
    again, _ = dealhub_events.poll_events(quotes, "quote_pending_approval", statuses)
    assert again == []


def test_poll_skips_a_change_a_webhook_delivered():
    events, statuses = dealhub_events.poll_events(
        [_quote("a", "Won")], "quote_won", {"a": "Published"}, delivered={"a": "Won"}
    )
    assert events == []
    assert statuses == {"a": "Won"}


def test_poll_keeps_quotes_it_did_not_read():
    _events, statuses = dealhub_events.poll_events([], "quote_won", {"a": "Draft"})
    assert statuses == {"a": "Draft"}


def test_webhook_only_events_are_not_polled():
    assert dealhub_events.polled("quote_won") is True
    assert dealhub_events.polled("quote_primary") is False
    events, _ = dealhub_events.poll_events([_quote("a", "Published")], "quote_unpublished", {"a": "Draft"})
    assert events == []


def test_status_map_round_trip():
    dumped = dealhub_events.dump_statuses({"a": "Won"})
    assert dealhub_events.load_statuses(dumped) == {"a": "Won"}
    assert dealhub_events.load_statuses(dumped.encode()) == {"a": "Won"}
    assert dealhub_events.load_statuses(None) is None
    assert dealhub_events.load_statuses("not json") is None


def test_catalog_lists_every_event_with_filter_keys():
    events = PROVIDER_CATALOG["dealhub"]["available_events"]
    assert {e["event"] for e in events} == dealhub_events.EVENTS
    for event in events:
        assert set(event["filter_keys"]) == {
            "status",
            "discount_min",
            "discount_max",
            "owner",
            "primary_only",
            "external_opportunity_id",
        }
        assert event["mechanism"] == ("hybrid" if dealhub_events.polled(event["event"]) else "webhook")
    assert "webhook_token" in PROVIDER_CATALOG["dealhub"]["extra_fields"]


def test_every_event_has_a_poll_interval():
    for event in dealhub_events.EVENTS:
        assert POLL_INTERVALS[event] == dealhub_events.POLL_INTERVAL_SECONDS


class _Resp:
    def __init__(self, data):
        self.data = data


@pytest.mark.asyncio
async def test_list_quotes_pages_until_no_more(monkeypatch):
    calls = []

    async def fake_request(self, method, path, action, *, params=None, body=None, accept="application/json"):
        calls.append(params)
        offset = params["offset"]
        more = offset == 0
        return _Resp(
            {"info": {"more_results_matching_the_request": more}, "quotes": [{"dealhub_quote_id": str(offset)}]}
        ), None

    from app.integrations.providers.dealhub import _client

    monkeypatch.setattr(_client.DealHubClient, "request", fake_request)
    quotes, err = await dealhub_events.list_quotes("tok")
    assert err is None
    assert [q["dealhub_quote_id"] for q in quotes] == ["0", "50"]
    assert calls[0]["feature"] == ["info", "summary"]
    assert calls[1]["offset"] == 50


@pytest.mark.asyncio
async def test_list_quotes_answers_the_error(monkeypatch):
    async def fake_request(self, *args, **kwargs):
        return None, "Error: dealhub refused the API token (HTTP 401)"

    from app.integrations.providers.dealhub import _client

    monkeypatch.setattr(_client.DealHubClient, "request", fake_request)
    quotes, err = await dealhub_events.list_quotes("tok")
    assert quotes == []
    assert err and "401" in err
