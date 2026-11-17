"""Integration-event filters: an event the trigger's filter drops creates no run.

Evaluated by the platform on every polled or notified event, before the run
and before any gate; each dropped event is counted on the assignment. A
trigger without a filter passes every event, as before filters existed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import event_poller
from app.services.integration_event_filters import (
    FILTER_SIGNALS_KEY,
    automated_reason,
    event_filter_reason,
    trigger_filter,
)


def _mail(sender="ceo@acme.example", subject="Q4 numbers", *, to=(), cc=(), headers=None, resource_type=None):
    msg = {
        "id": f"m-{uuid.uuid4().hex[:6]}",
        "from": {"emailAddress": {"address": sender, "name": "Sender"}},
        "subject": subject,
        "bodyPreview": "",
        "receivedDateTime": "2026-10-29T08:00:00Z",
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
    }
    if headers is not None:
        msg["internetMessageHeaders"] = [{"name": k, "value": v} for k, v in headers.items()]
    if resource_type:
        msg["@odata.type"] = resource_type
    return event_poller.microsoft_mail_payload(msg, signals=True)


def test_no_filter_passes_every_event():
    assert event_filter_reason("new_email", _mail("noreply@x.example"), {}) is None
    assert event_filter_reason("new_email", _mail(), None) is None
    assert trigger_filter({"provider": "microsoft"}) == {}


def test_an_unfiltered_trigger_keeps_the_payload_it_always_had():
    payload = event_poller.microsoft_mail_payload({"id": "m1", "from": {"emailAddress": {"address": "a@b"}}})
    assert FILTER_SIGNALS_KEY not in payload
    assert list(payload) == [
        "from",
        "from_name",
        "to",
        "cc",
        "subject",
        "body",
        "received_at",
        "is_flagged",
        "message_id",
        "conversation_id",
        "internet_message_id",
        "message_type",
        "trigger_type",
        "event",
        "provider",
    ]


def test_the_mail_payload_carries_the_visible_recipients():
    """A sent reply renders ``To:`` / ``Cc:`` from the payload, filter or not (#9222)."""
    msg = {
        "id": "m2",
        "from": {"emailAddress": {"address": "svc@corp.example"}},
        "toRecipients": [{"emailAddress": {"address": "partner@x.example", "name": "Partner"}}],
        "ccRecipients": [{"emailAddress": {"address": "boss@corp.example"}}, {"emailAddress": {}}],
    }
    payload = event_poller.microsoft_mail_payload(msg)
    assert payload["to"] == "partner@x.example"
    assert payload["cc"] == "boss@corp.example"
    bcc_only = event_poller.microsoft_mail_payload(
        {"id": "m3", "bccRecipients": [{"emailAddress": {"address": "hidden@x.example"}}]}
    )
    assert bcc_only["to"] == "" and bcc_only["cc"] == ""


def test_a_gmail_address_header_keeps_the_addresses_alone():
    assert event_poller._header_addresses('"Partner, Inc" <p@x.example>, b@y.example') == "p@x.example, b@y.example"
    assert event_poller._header_addresses("") == ""


@pytest.mark.parametrize(
    ("sender", "senders", "dropped"),
    [
        ("alerts@crowdstrike.com", {"deny": ["crowdstrike.com"]}, True),
        ("alerts@mail.crowdstrike.com", {"deny": ["crowdstrike.com"]}, True),
        ("ceo@acme.example", {"deny": ["crowdstrike.com"]}, False),
        ("ceo@acme.example", {"allow": ["acme.example"]}, False),
        ("someone@other.example", {"allow": ["acme.example", "cfo@partner.example"]}, True),
        ("cfo@partner.example", {"allow": ["cfo@partner.example"]}, False),
        ("cfo@acme.example", {"allow": ["acme.example"], "deny": ["cfo@acme.example"]}, True),
        ("ceo@acme.example", {"allow": ["@acme.example"]}, False),
    ],
)
def test_the_sender_allow_and_deny_lists(sender, senders, dropped):
    reason = event_filter_reason("new_email", _mail(sender), {"from": senders})
    assert (reason is not None) is dropped


def test_from_contains_is_honoured():
    assert event_filter_reason("new_email", _mail("billing@acme.example"), {"from_contains": "events@"})
    assert event_filter_reason("new_email", _mail("events@acme.example"), {"from_contains": "events@"}) is None


def test_to_me_and_cc_me_compare_with_the_mailbox():
    me = {"me@acme.example"}
    direct = _mail(to=["Me@Acme.example"])
    copied = _mail(to=["team@acme.example"], cc=["me@acme.example"])
    assert event_filter_reason("new_email", direct, {"to_me": True}, mailbox=me) is None
    assert event_filter_reason("new_email", copied, {"to_me": True}, mailbox=me) == "the mailbox is not a To recipient"
    assert event_filter_reason("new_email", copied, {"cc_me": True}, mailbox=me) is None
    assert event_filter_reason("new_email", copied, {"to_me": True, "cc_me": True}, mailbox=me) is None
    # The mailbox could not be read: the filter lets the mail through.
    assert event_filter_reason("new_email", copied, {"to_me": True}, mailbox=()) is None


def test_subject_contains_takes_a_string_or_a_list():
    assert event_filter_reason("new_email", _mail(subject="Board pack draft"), {"subject_contains": "board"}) is None
    assert event_filter_reason("new_email", _mail(subject="Lunch"), {"subject_contains": ["board", "audit"]})


@pytest.mark.parametrize(
    "mail",
    [
        _mail("noreply@github.com"),
        _mail("no-reply@service.example"),
        _mail("do_not_reply@bank.example"),
        _mail("MAILER-DAEMON@mx.example"),
        _mail("postmaster@acme.example"),
        _mail("tool@saas.example", headers={"Auto-Submitted": "auto-generated"}),
        _mail("news@shop.example", headers={"Precedence": "bulk"}),
        _mail("list@lists.example", headers={"List-Unsubscribe": "<mailto:x>"}),
        _mail("ops@acme.example", headers={"X-Auto-Response-Suppress": "All"}),
        _mail("ops@acme.example", headers={"Content-Type": 'multipart/report; report-type="delivery-status"'}),
        _mail("alice@acme.example", resource_type="#microsoft.graph.eventMessageResponse"),
    ],
)
def test_exclude_automated_drops_mail_no_person_wrote(mail):
    assert automated_reason(mail) is not None
    assert event_filter_reason("new_email", mail, {"exclude_automated": True}).startswith("automated mail")


def test_exclude_automated_keeps_a_person_s_mail():
    human = _mail("alice@acme.example", headers={"Auto-Submitted": "no", "Content-Type": "text/html"})
    assert event_filter_reason("new_email", human, {"exclude_automated": True}) is None


def test_gmail_headers_feed_the_same_filter():
    payload = {
        "from": "Shop <deals@shop.example>",
        "subject": "Sale",
        FILTER_SIGNALS_KEY: {"to": "me@acme.example", "cc": "", "headers": {"List-Id": "<deals.shop.example>"}},
    }
    assert event_filter_reason("new_email", payload, {"exclude_automated": True})
    assert event_filter_reason("new_email", payload, {"to_me": True}, mailbox={"me@acme.example"}) is None


def test_teams_filters_on_channels_chats_and_authors():
    message = {"team_id": "t1", "channel_id": "c1", "from_name": "Build Bot", FILTER_SIGNALS_KEY: {"from_id": "u-9"}}
    assert event_filter_reason("new_teams_reply", message, {"channels": ["t1:c2"]}) == "channel is not in channels"
    assert event_filter_reason("new_teams_reply", message, {"scope": {"teams_channel": ["t1:c1"]}}) is None
    assert event_filter_reason("new_teams_message", message, {"exclude_authors": ["build bot"]}) == "author is excluded"
    assert event_filter_reason("new_teams_message", message, {"exclude_authors": ["U-9"]}) == "author is excluded"
    chat = {"chat_id": "19:abc@thread.v2", "from_name": "Alice"}
    assert event_filter_reason("new_teams_chat_message", chat, {"chat_ids": ["19:other@thread.v2"]})
    assert event_filter_reason("new_teams_chat_message", chat, {"chat_ids": ["19:abc@thread.v2"]}) is None


def test_calendar_change_kinds_and_flag_statuses():
    assert event_filter_reason("calendar_event_changed", {"change_kind": "updated"}, {"change_kinds": ["cancelled"]})
    assert event_filter_reason("calendar_event_changed", {"change_kind": "cancelled"}, {"change_kinds": ["cancelled"]}) is None
    flagged = {**_mail(), "flag_status": "flagged"}
    assert event_filter_reason("email_flag_changed", flagged, {"flag_status": ["complete"]})
    assert event_filter_reason("email_flag_changed", flagged, {"flag_status": ["flagged"]}) is None


# ---------------------------------------------------------------------------
# The poller: a dropped event is seen, never run, and counted
# ---------------------------------------------------------------------------


class _SeenRedis:
    def __init__(self):
        self.values: dict = {}
        self.seen: set = set()

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *args, **kwargs):
        self.values[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


async def test_a_noreply_mail_creates_no_run_and_is_counted(monkeypatch):
    wf = SimpleNamespace(
        id=uuid.uuid4(),
        trigger_config={"provider": "microsoft", "event": "new_email", "filter": {"exclude_automated": True}},
    )
    events = [_mail("noreply@vendor.example"), _mail("ceo@acme.example")]
    claimed: list[str] = []
    triggered: list[dict] = []
    counted: list[tuple] = []

    async def _claim(_redis, _wf_id, msg_id):
        claimed.append(msg_id)
        return True

    async def _trigger(_wf, _agent, payload, **_kwargs):
        triggered.append(payload)
        return SimpleNamespace(status="started")

    async def _count(assignment_id, count):
        counted.append((assignment_id, count))

    monkeypatch.setattr(event_poller, "claim_seen", _claim)
    monkeypatch.setattr(event_poller, "trigger_or_queue_assignment", _trigger)
    monkeypatch.setattr(event_poller, "record_filtered_events", _count)

    async def _poll(*_args, **_kwargs):
        return events

    monkeypatch.setattr(event_poller, "_poll_provider", _poll)
    dispatched, outcome = await event_poller._dispatch_poll_page(
        None,
        _SeenRedis(),
        None,
        wf=wf,
        agent=SimpleNamespace(id=uuid.uuid4()),
        provider="microsoft",
        event_type="new_email",
        access_token="tok",
        integration_id=uuid.uuid4(),
    )

    assert (dispatched, outcome) == (1, "caught_up")
    assert [p["from"] for p in triggered] == ["ceo@acme.example"]
    assert all(FILTER_SIGNALS_KEY not in p for p in triggered)
    assert len(claimed) == 2, "the dropped mail stays seen, never counted twice"
    assert counted == [(wf.id, 1)]
