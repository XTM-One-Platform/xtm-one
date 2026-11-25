"""The dedup key of event-triggered runs (``services/event_run_dedup.py``).

One mail reaching an agent through two mailboxes, a Graph notification and
the poll behind it, or a delegated pick-up must be one run: the key names the
mail (its Message-ID) or the calendar modification (iCalUId and instant)
independently of the mailbox and of the delivery path. Pure functions, no
database: the cross-assignment skip itself is
``test_event_run_dedup_postgres.py`` and the engine wiring
``test_assignment_event_dedup.py``.
"""

from __future__ import annotations

import pytest

from app.services import event_poller
from app.services import event_run_dedup as dedup

MAIL = {"type": "integration_event", "provider": "microsoft", "event": "new_email", "message_id": "AAMkA"}
CALENDAR = {"type": "integration_event", "provider": "microsoft", "event": "calendar_event_changed"}


def _mail(message_id: str, **extra) -> dict:
    return {
        "internet_message_id": message_id,
        "conversation_id": "AAQkC",
        "received_at": "2026-10-30T17:25:24Z",
        "from": "Person@Example.org",
        **extra,
    }


# -- Message-ID normalisation -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<ABC.123@mail.example>", "abc.123@mail.example"),
        ("ABC.123@mail.example", "abc.123@mail.example"),
        ("  <ABC@x>  ", "abc@x"),
        ("<no-at-sign>", ""),
        ("<has space@x>", ""),
        ("", ""),
        (None, ""),
        ({"nested": 1}, ""),
    ],
)
def test_message_ids_lose_their_brackets_and_case(raw, expected):
    assert dedup.normalize_message_id(raw) == expected


# -- The mail key: the same in every mailbox ----------------------------------


def test_one_mail_in_two_mailboxes_has_one_key():
    """The Graph id differs per mailbox; the Message-ID does not."""
    service_account = event_poller.microsoft_mail_payload(
        {"id": "AAMk-service", "internetMessageId": "<ABC@mail.example>", "conversationId": "C1"}
    )
    person = event_poller.microsoft_mail_payload(
        {"id": "AAMk-person", "internetMessageId": "<ABC@mail.example>", "conversationId": "C2"}
    )
    assert service_account["message_id"] != person["message_id"]
    assert dedup.mail_dedup_key(service_account) == dedup.mail_dedup_key(person) == "mail:abc@mail.example"


def test_two_mails_of_one_thread_have_two_keys():
    assert dedup.mail_dedup_key(_mail("<one@x>")) != dedup.mail_dedup_key(_mail("<two@x>"))


def test_without_a_message_id_the_thread_instant_and_sender_stand_in():
    payload = _mail("")
    assert dedup.mail_dedup_key(payload) == "mail:AAQkC@2026-10-30T17:25:24Z:person@example.org"
    # Nothing to name the mail by: not deduplicated rather than wrongly merged.
    assert dedup.mail_dedup_key({"from": "a@b"}) is None
    assert dedup.mail_dedup_key(None) is None


def test_a_long_key_is_bounded_by_its_digest():
    key = dedup.mail_dedup_key(_mail("<" + "a" * 300 + "@x>"))
    assert key.startswith("sha256:") and len(key) == len("sha256:") + 64


# -- The calendar key: one per modification -----------------------------------


def test_one_modification_seen_by_two_calendars_has_one_key():
    organizer = event_poller.calendar_event_payload(
        {"id": "AAMk-org", "iCalUId": "040000008200E00074C5B7101A82E008", "lastModifiedDateTime": "2026-10-30T17:27:00Z"}
    )
    attendee = event_poller.calendar_event_payload(
        {"id": "AAMk-att", "iCalUId": "040000008200E00074C5B7101A82E008", "lastModifiedDateTime": "2026-10-30T17:27:00Z"}
    )
    assert organizer["message_id"] != attendee["message_id"]
    assert dedup.calendar_dedup_key(organizer) == dedup.calendar_dedup_key(attendee)
    assert dedup.calendar_dedup_key(organizer).startswith("calendar:040000008200E00074C5B7101A82E008@")


def _calendar(modified: str, **changes) -> dict:
    event = {
        "id": "AAMk-org",
        "iCalUId": "U",
        "lastModifiedDateTime": modified,
        "subject": "[Workshop] Community PRs",
        "start": {"dateTime": "2026-11-26T09:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-11-26T10:00:00", "timeZone": "UTC"},
        "attendees": [{"emailAddress": {"address": "A@x.io"}}, {"emailAddress": {"address": "b@x.io"}}],
    }
    event.update(changes)
    return event_poller.calendar_event_payload(event)


def test_saves_that_change_nothing_an_agent_reads_share_one_key():
    """#2216: five saves of one event within minutes, identical time, cancellation
    and attendees, raised five runs; they now carry one dedup key."""
    keys = {
        dedup.calendar_dedup_key(_calendar(t))
        for t in ("2026-11-25T08:47:00Z", "2026-11-25T08:48:00Z", "2026-11-25T08:50:00Z")
    }
    assert len(keys) == 1
    # Attendee order and case are not a change.
    reordered = _calendar(
        "2026-11-25T08:53:00Z",
        attendees=[{"emailAddress": {"address": "b@x.io"}}, {"emailAddress": {"address": "a@x.io"}}],
    )
    assert dedup.calendar_dedup_key(reordered) in keys


def test_a_new_time_attendee_or_cancellation_is_its_own_key():
    base = dedup.calendar_dedup_key(_calendar("T1"))
    moved = _calendar("T2", start={"dateTime": "2026-11-26T11:00:00", "timeZone": "UTC"})
    invited = _calendar("T3", attendees=[{"emailAddress": {"address": "c@x.io"}}])
    cancelled = _calendar("T4", isCancelled=True)
    keys = {base, *(dedup.calendar_dedup_key(p) for p in (moved, invited, cancelled))}
    assert len(keys) == 4


def test_the_calendar_poll_selects_the_attendees():
    assert "attendees" in event_poller._CALENDAR_SELECT.split(",")


def test_each_modification_of_one_event_is_its_own_key():
    first = dedup.calendar_dedup_key({"ical_uid": "U", "last_modified_at": "T1"})
    second = dedup.calendar_dedup_key({"ical_uid": "U", "last_modified_at": "T2"})
    assert first != second


def test_without_an_ical_uid_the_event_id_stands_in_and_without_either_nothing():
    assert dedup.calendar_dedup_key({"event_id": "E", "last_modified_at": "T"}) == "calendar:E@T"
    assert dedup.calendar_dedup_key({"event_id": "E"}) is None
    assert dedup.calendar_dedup_key({"last_modified_at": "T"}) is None


# -- Which runs get a key -----------------------------------------------------


def test_only_microsoft_mail_and_calendar_deliveries_are_keyed():
    assert dedup.event_dedup_key(MAIL, _mail("<a@b>")) == "mail:a@b"
    assert dedup.event_dedup_key(CALENDAR, {"ical_uid": "U", "last_modified_at": "T"}) == "calendar:U@T"
    # A Teams or Slack message keeps its per-assignment seen set alone.
    teams = {"type": "integration_event", "provider": "microsoft", "event": "new_teams_message", "message_id": "m"}
    assert dedup.event_dedup_key(teams, {"message_id": "m"}) is None
    slack = {"type": "integration_event", "provider": "slack", "event": "new_message", "message_id": "m"}
    assert dedup.event_dedup_key(slack, {"message_id": "m"}) is None
    # Not an integration event at all.
    assert dedup.event_dedup_key({"type": "webhook"}, _mail("<a@b>")) is None
    assert dedup.event_dedup_key({"type": "schedule"}, _mail("<a@b>")) is None
    assert dedup.event_dedup_key(None, _mail("<a@b>")) is None


def test_the_provider_and_event_are_matched_case_insensitively():
    assert dedup.event_dedup_key({**MAIL, "provider": "Microsoft", "event": "NEW_EMAIL"}, _mail("<a@b>")) == "mail:a@b"


# -- A delegated pick-up of the same mail -------------------------------------


def test_the_message_ids_a_task_text_names():
    text = (
        "Reply to the mail <ABC.123@mail.example> (Message-ID) from person@example.org; "
        "it answers <ABC.123@mail.example> and quotes <older@mail.example>."
    )
    assert dedup.message_ids_in_text(text) == ["abc.123@mail.example", "older@mail.example"]
    # A bare address is not a Message-ID; nothing without angle brackets is.
    assert dedup.message_ids_in_text("write to person@example.org") == []
    assert dedup.message_ids_in_text(None) == []


def test_a_delegated_run_for_one_mail_shares_the_mails_key():
    payload = {"trigger_type": "delegation", "task": "Draft the reply to <ABC@mail.example>, thread X"}
    assert dedup.run_dedup_key(None, payload) == dedup.mail_dedup_key(_mail("<ABC@mail.example>"))


def test_an_agents_own_delegation_is_not_keyed_but_another_agents_is():
    agent = "0f0e0d0c-0b0a-4908-8706-050403020100"
    own = {"trigger_type": "delegation", "task": "Reply to <ABC@mail.example>", "delegated_by_agent_id": agent}
    assert dedup.run_dedup_key(None, own, agent_id=agent) is None
    other = {**own, "delegated_by_agent_id": "another-agent"}
    assert dedup.run_dedup_key(None, other, agent_id=agent) == "mail:abc@mail.example"
    # Without the agent to compare with, the key stands.
    assert dedup.run_dedup_key(None, own) == "mail:abc@mail.example"


def test_a_task_naming_several_mails_is_about_none_of_them():
    payload = {"trigger_type": "delegation", "task": "Compare <a@x> with <b@x>"}
    assert dedup.run_dedup_key(None, payload) is None


def test_a_work_item_payload_names_the_mail_ahead_of_the_task_text():
    payload = {
        "trigger_type": "work_item",
        "task": "see <other@x>",
        "work_item": {"payload": {"message_id": "<ABC@mail.example>"}},
    }
    assert dedup.run_dedup_key({"type": "work_item"}, payload) == "mail:abc@mail.example"
    flat = {"trigger_type": "delegation", "task": "", "internet_message_id": "<Q@Z>"}
    assert dedup.run_dedup_key(None, flat) == "mail:q@z"


def test_a_manual_scheduled_or_webhook_run_is_never_keyed():
    assert dedup.run_dedup_key({"type": "schedule"}, {"task": "<a@b>"}) is None
    assert dedup.run_dedup_key({"type": "manual"}, {"trigger_type": "manual", "task": "<a@b>"}) is None
    assert dedup.run_dedup_key({"type": "webhook"}, {"message_id": "<a@b>"}) is None
    assert dedup.run_dedup_key(None, None) is None


# -- The window and the context -----------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, 600),
        ({}, 600),
        ({"dedup_window_seconds": 30}, 30),
        ({"dedup_window_seconds": "30"}, 30),
        ({"dedup_window_seconds": 0}, 0),
        ({"dedup_window_seconds": -5}, 0),
        ({"dedup_window_seconds": None}, 600),
        ({"dedup_window_seconds": True}, 600),
        ({"dedup_window_seconds": "soon"}, 600),
        ({"dedup_window_seconds": 10**9}, dedup.MAX_DEDUP_WINDOW_SECONDS),
    ],
)
def test_the_assignment_window_overrides_the_default_within_bounds(config, expected):
    assert dedup.dedup_window_seconds(config, 600) == expected


def test_the_default_window_is_bounded_too():
    assert dedup.dedup_window_seconds({}, -1) == 0
    assert dedup.dedup_window_seconds({}, 10**9) == dedup.MAX_DEDUP_WINDOW_SECONDS


def test_the_context_carries_the_key_once():
    assert dedup.with_dedup_key(None, "mail:a@b") == {"dedup_key": "mail:a@b"}
    assert dedup.with_dedup_key({"type": "integration_event"}, "mail:a@b") == {
        "type": "integration_event",
        "dedup_key": "mail:a@b",
    }
    kept = {"dedup_key": "mail:first"}
    assert dedup.with_dedup_key(kept, "mail:second") is kept
    untouched = {"type": "schedule"}
    assert dedup.with_dedup_key(untouched, None) is untouched


def test_the_skip_summary_names_the_original_run_the_key_and_the_window():
    summary = dedup.duplicate_skip_summary("0740405d", "mail:a@b", 600)
    assert summary.startswith(dedup.DUPLICATE_SKIP_PREFIX)
    assert "0740405d" in summary and "mail:a@b" in summary and "600 s" in summary


# -- configured dispatch dedup key (#10622) ---------------------------------

HEAD_KEY = "{{payload.repository.full_name}}@{{ payload.workflow_run.head_sha }}"
WEBHOOK = {"type": "webhook", "provider": "github"}
POLLED = {"type": "integration_event", "provider": "github", "event": "workflow_run"}


def _run(sha: str = "ABC") -> dict:
    return {"repository": {"full_name": "Org/Repo"}, "workflow_run": {"head_sha": sha}}


def test_a_configured_key_renders_over_the_payload_scoped_to_its_assignment():
    config = {"dedup": {"key": HEAD_KEY}}
    key = dedup.run_dedup_key(WEBHOOK, _run(), trigger_config=config, assignment_id="a1")
    assert key == "cfg:assignment:a1:org/repo@abc"
    assert dedup.run_dedup_key(POLLED, _run(), trigger_config=config, assignment_id="a1") == key
    # Another assignment without a group has its own key space.
    assert dedup.run_dedup_key(WEBHOOK, _run(), trigger_config=config, assignment_id="a2") != key


def test_a_group_shares_the_key_space_across_assignments():
    config = {"dedup": {"key": HEAD_KEY, "group": "red"}}
    first = dedup.run_dedup_key(WEBHOOK, _run(), trigger_config=config, assignment_id="a1")
    second = dedup.run_dedup_key(POLLED, _run(), trigger_config=config, assignment_id="a2")
    assert first == second == "cfg:group:red:org/repo@abc"
    assert dedup.run_dedup_key(WEBHOOK, _run("def"), trigger_config=config, assignment_id="a1") != first


def test_a_configured_key_keys_nothing_outside_an_event_delivery_or_on_a_missing_field():
    config = {"dedup": {"key": HEAD_KEY}}
    assert dedup.run_dedup_key({"type": "schedule"}, _run(), trigger_config=config, assignment_id="a") is None
    assert dedup.run_dedup_key(WEBHOOK, {"repository": {"full_name": "x/y"}}, trigger_config=config) is None
    assert dedup.run_dedup_key(WEBHOOK, {**_run(), "workflow_run": {"head_sha": {}}}, trigger_config=config) is None
    # Without dedup a webhook delivery is not keyed, as before.
    assert dedup.run_dedup_key(WEBHOOK, _run(), trigger_config={}, assignment_id="a") is None


def test_the_configured_window_applies_to_the_configured_key_only():
    config = {"dedup": {"key": HEAD_KEY, "window_seconds": 90}, "dedup_window_seconds": 30}
    assert dedup.dedup_window_seconds(config, 600, "cfg:assignment:a:x") == 90
    assert dedup.dedup_window_seconds(config, 600, "mail:a@b") == 30
    default = dedup.DEFAULT_CONFIGURED_WINDOW_SECONDS
    assert dedup.dedup_window_seconds({"dedup": {"key": HEAD_KEY}}, 600, "cfg:x") == default


def test_the_drop_reason_names_the_kind_of_key_never_a_value():
    assert dedup.drop_reason("cfg:group:g:org/repo@abc") == dedup.CONFIGURED_DROP_REASON
    assert dedup.drop_reason("mail:a@b") == dedup.DUPLICATE_DROP_REASON
    assert "org/repo" not in dedup.drop_reason("cfg:group:g:org/repo@abc")


@pytest.mark.parametrize(
    "raw",
    [
        "repo@sha",
        {"key": ""},
        {"key": "no placeholder"},
        {"key": "{{payload.a}}-{{agent.id}}"},
        {"key": "{{payload.a}}", "window_seconds": 0},
        {"key": "{{payload.a}}", "window_seconds": 3601},
        {"key": "{{payload.a}}", "window_seconds": True},
        {"key": "{{payload.a}}", "group": "bad group"},
        {"key": "{{payload.a}}", "ttl": 5},
        {"key": "{{payload.a}}" * 40},
    ],
)
def test_an_unusable_dedup_is_refused_at_save_and_ignored_at_dispatch(raw):
    config = {"dedup": raw}
    assert dedup.dedup_config_error(config)
    assert dedup.configured_dedup(config) is None
    assert dedup.run_dedup_key(WEBHOOK, {"a": "x"}, trigger_config=config, assignment_id="a") is None


def test_a_usable_dedup_passes_the_trigger_filter_check():
    from app.services.integration_event_filters import trigger_filter_error

    good = {"dedup": {"key": HEAD_KEY, "window_seconds": 120, "group": "green.v1"}}
    assert dedup.dedup_config_error(good) is None
    assert dedup.dedup_config_error({}) is None
    assert trigger_filter_error("integration_event", good) is None
    assert trigger_filter_error("integration_event", {"dedup": {"key": "x"}})
