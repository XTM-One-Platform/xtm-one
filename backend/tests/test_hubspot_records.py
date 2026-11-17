"""How the HubSpot tools read activity: times, text, engagements, the
timeline order and the inbound engagement report, without any HTTP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations.providers.hubspot_records import (
    InboundContact,
    TimelineEvent,
    aggregate_inbound,
    assess_inbound,
    cap_text,
    engagement_type,
    epoch_ms,
    format_engagement,
    format_group,
    html_to_text,
    iso_utc,
    merge_timeline,
    normalize_engagement,
    parse_hubspot_time,
    parse_time_bound,
    property_changes,
    submissions_from_history,
    submissions_from_profile,
    touch_channel,
)

T0 = datetime(2026, 10, 28, 13, 44, 10, tzinfo=UTC)


def _email(eid: str, at: datetime, *, direction: str = "EMAIL", **props: str) -> dict:
    return {
        "id": eid,
        "properties": {"hs_timestamp": at.isoformat().replace("+00:00", "Z"), "hs_email_direction": direction, **props},
    }


# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-28T13:44:10.123Z", T0 + timedelta(milliseconds=123)),
        ("2026-10-28T15:44:10+02:00", T0),
        ("2026-10-28T13:44:10", T0),
        ("2026-10-28", datetime(2026, 10, 28, tzinfo=UTC)),
        (epoch_ms(T0), T0),
        (str(epoch_ms(T0)), T0),
        (int(T0.timestamp()), T0),
        ("", None),
        ("not a date", None),
        (None, None),
        (True, None),
    ],
)
def test_every_hubspot_time_shape_is_read_as_utc(raw, expected):
    assert parse_hubspot_time(raw) == expected


def test_a_bare_date_bounds_its_whole_utc_day():
    assert parse_time_bound("2026-10-28") == datetime(2026, 10, 28, tzinfo=UTC)
    assert parse_time_bound("2026-10-28", end=True) == datetime(2026, 10, 28, 23, 59, 59, 999000, tzinfo=UTC)
    assert parse_time_bound("2026-10-28T09:00:00-04:00", end=True) == datetime(2026, 10, 28, 13, 0, tzinfo=UTC)
    assert parse_time_bound("") is None


def test_times_are_shown_in_utc_to_the_second():
    assert iso_utc(datetime(2026, 10, 28, 15, 44, 10, 999, tzinfo=UTC) + timedelta(hours=0)) == "2026-10-28T15:44:10Z"
    assert iso_utc(None) == ""


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def test_html_bodies_are_rendered_and_plain_text_is_kept():
    assert html_to_text("<p>Signed <b>deck</b></p>") == "Signed deck"
    assert html_to_text("<div>Hi,<br>Thanks</div><ul><li>one</li></ul><p>A &amp; B</p>") == "Hi,\nThanks\n- one\n\nA & B"
    assert html_to_text("<html><head><style>p{}</style></head><body><p>Body</p><script>x()</script></body></html>") == "Body"
    assert html_to_text("a < b and c > d, R&amp;D") == "a < b and c > d, R&amp;D"
    assert html_to_text(None) == ""


def test_a_cut_body_says_how_much_was_left_out():
    text = "word " * 100
    cut, more = cap_text(text.strip(), 50)
    assert len(cut) <= 50 and more == len(text.strip()) - len(cut)
    assert cap_text("short", 50) == ("short", 0)


# ---------------------------------------------------------------------------
# Engagements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("email", "emails"), ("EMAILS", "emails"), ("0-48", "calls"), ("meeting", "meetings"), ("contacts", ""), (None, "")])
def test_engagement_type_spellings(raw, expected):
    assert engagement_type(raw) == expected


def test_an_email_is_read_with_sender_subject_direction_and_text_body():
    e = normalize_engagement(
        "emails",
        _email(
            "9001",
            T0,
            hs_email_subject="Re: your request",
            hs_email_text="Hello,\nHappy to help.",
            hs_email_html="<p>ignored</p>",
            hs_email_from_firstname="Ana",
            hs_email_from_lastname="Rep",
            hs_email_from_email="ana@acme.example",
            hs_email_to_email="f5@example.com",
            hs_email_status="SENT",
            hs_email_open_count="2",
            hubspot_owner_id="77",
            hs_attachment_ids="7001;7002",
        ),
    )
    assert (e.kind, e.direction, e.timestamp) == ("email", "outgoing", T0)
    assert e.sender == "Ana Rep <ana@acme.example>"
    assert e.body == "Hello,\nHappy to help."
    assert ("opens", "2") in e.details
    assert e.attachment_ids == ["7001", "7002"]
    text = format_engagement(e, owner=lambda o: f"owner {o}", body_chars=100)
    assert text.startswith("2026-10-28T13:44:10Z | EMAIL outgoing | owner owner 77 | from Ana Rep <ana@acme.example>")
    assert "subject 'Re: your request'" in text and "status SENT" in text and "email id 9001" in text
    assert "\n    Hello," in text


def test_an_email_falls_back_to_its_html_and_a_sequence_email_says_so():
    e = normalize_engagement("emails", _email("1", T0, hs_email_html="<p>Step <b>one</b></p>", hs_sequence_id="555"))
    assert e.body == "Step one"
    assert e.kind == "sequence email"
    assert touch_channel(e) == "sequence email"


def test_a_long_body_is_cut_with_a_pointer_to_the_full_read():
    e = normalize_engagement("notes", {"id": "5", "properties": {"hs_timestamp": "2026-10-28", "hs_note_body": "x " * 400}})
    text = format_engagement(e, owner=str, body_chars=100)
    assert "[body cut:" in text and "get_hubspot_engagement(engagement_type='notes', engagement_id='5')" in text


def test_status_and_outcome_go_through_the_labels():
    call = normalize_engagement(
        "calls",
        {"id": "3", "properties": {"hs_timestamp": "2026-10-28T10:00:00Z", "hs_call_direction": "OUTBOUND", "hs_call_disposition": "f240bbac-87c9-4f6e-bf70-924b57d47db7", "hs_call_duration": "125000"}},
        label=lambda _t, name, raw: "Connected" if name == "hs_call_disposition" else raw,
    )
    assert ("outcome", "Connected") in call.status
    assert call.raw_status["hs_call_disposition"].startswith("f240")
    assert ("duration", "2 min 5 s") in call.details
    assert call.direction == "outgoing"


def test_a_meeting_reaches_the_contact_when_it_is_booked():
    booked, held = T0 + timedelta(hours=1), T0 + timedelta(days=5)
    meeting = normalize_engagement(
        "meetings",
        {"id": "8", "properties": {"hs_timestamp": iso_utc(held), "hs_createdate": iso_utc(booked), "hs_meeting_outcome": "COMPLETED", "hs_meeting_source": "MEETINGS_PUBLIC"}},
    )
    assert meeting.timestamp == held and meeting.touch_time == booked
    assert touch_channel(meeting) == "meeting (booked by the contact)"


@pytest.mark.parametrize(
    ("direction", "status", "channel"),
    [("EMAIL", "SENT", "manual email"), ("INCOMING_EMAIL", "", None), ("FORWARDED_EMAIL", "", None), ("EMAIL", "FAILED", None), ("EMAIL", "SCHEDULED", None)],
)
def test_only_an_email_the_contact_got_is_a_touch(direction, status, channel):
    e = normalize_engagement("emails", _email("1", T0, direction=direction, hs_email_status=status))
    assert touch_channel(e) == channel


def test_notes_and_tasks_are_not_touches():
    assert touch_channel(normalize_engagement("notes", {"id": "1", "properties": {"hs_note_body": "x"}})) is None
    assert touch_channel(normalize_engagement("tasks", {"id": "1", "properties": {"hs_task_subject": "x"}})) is None


# ---------------------------------------------------------------------------
# Timeline order
# ---------------------------------------------------------------------------


def test_the_timeline_is_chronological_with_causes_first_at_the_same_second():
    events = [
        TimelineEvent(T0 + timedelta(hours=2), "notes", "9101", "note"),
        TimelineEvent(T0 + timedelta(hours=2), "emails", "9001", "email"),
        TimelineEvent(T0, "property change", "lifecyclestage-lead", "lifecycle"),
        TimelineEvent(None, "tasks", "1", "undated task"),
        TimelineEvent(T0, "form submission", "c-1", "submission"),
        TimelineEvent(T0 + timedelta(hours=1), "property change", "owner", "owner"),
    ]
    assert [e.text for e in merge_timeline(events)] == ["submission", "lifecycle", "owner", "email", "note", "undated task"]
    assert [e.text for e in merge_timeline(iter(events), newest_first=True)] == [
        "note",
        "email",
        "owner",
        "lifecycle",
        "submission",
        "undated task",
    ]


def test_property_history_is_read_oldest_first():
    history = {
        "lifecyclestage": [
            {"value": "salesqualifiedlead", "timestamp": "2026-10-29T10:00:00Z", "sourceType": "CRM_UI", "updatedByUserId": 1077},
            {"value": "lead", "timestamp": "2026-10-28T13:44:10Z", "sourceType": "FORM"},
        ]
    }
    changes = property_changes(history, ["lifecyclestage", "absent"])
    assert [(c.value, c.source, c.user_id) for c in changes] == [("lead", "FORM", ""), ("salesqualifiedlead", "CRM_UI", "1077")]


# ---------------------------------------------------------------------------
# Form submissions
# ---------------------------------------------------------------------------


def test_the_legacy_profile_lists_every_submission_of_a_contact():
    profile = {
        "properties": {"email": {"value": "F5@Example.com"}},
        "form-submissions": [
            {"form-id": "b", "title": "Book a demo", "timestamp": epoch_ms(T0 + timedelta(days=1)), "page-url": "https://x/demo"},
            {"form-id": "a", "title": "Website - Contact", "timestamp": epoch_ms(T0), "page-url": "https://x/contact", "conversion-id": "c-1"},
        ],
    }
    subs = submissions_from_profile(profile, "501")
    assert [(s.form_name, s.timestamp) for s in subs] == [("Website - Contact", T0), ("Book a demo", T0 + timedelta(days=1))]
    assert subs[0].email == "f5@example.com" and subs[0].conversion_id == "c-1"


def test_the_conversion_history_stands_in_for_the_profile():
    history = {"recent_conversion_event_name": [{"value": "Book a demo: Pricing", "timestamp": iso_utc(T0)}]}
    (sub,) = submissions_from_history(history, "501", "f5@example.com")
    assert (sub.form_name, sub.timestamp, sub.form_id) == ("Book a demo: Pricing", T0, "")


# ---------------------------------------------------------------------------
# Inbound engagement report
# ---------------------------------------------------------------------------


def _contact(cid: str, *, owner: str = "", region: str = "", engagements=(), deals=(), replied_at=None) -> InboundContact:
    return InboundContact(
        contact_id=cid,
        name=f"Contact {cid}",
        email=f"{cid}@example.com",
        company="",
        country="France",
        region=region,
        owner_id=owner,
        owner=f"Owner {owner}" if owner else "",
        form="Website - Contact",
        submitted_at=T0,
        engagements=list(engagements),
        deals_created=list(deals),
        replied_at=replied_at,
    )


def test_a_contact_is_touched_by_its_first_channel_after_the_submission():
    before = normalize_engagement("emails", _email("1", T0 - timedelta(days=3)))
    sequence = normalize_engagement("emails", _email("2", T0 + timedelta(hours=5), hs_sequence_id="9"))
    call = normalize_engagement("calls", {"id": "3", "properties": {"hs_timestamp": iso_utc(T0 + timedelta(hours=30))}})
    reply = normalize_engagement("emails", _email("4", T0 + timedelta(hours=6), direction="INCOMING_EMAIL"))
    outcome = assess_inbound(_contact("1", owner="77", engagements=[before, call, sequence, reply], deals=[T0 + timedelta(days=2)]))
    assert outcome.first_touch[1] == "sequence email"
    assert outcome.first_touch_hours == pytest.approx(5.0)
    assert len(outcome.touches) == 2
    assert outcome.replied and outcome.deal_created and not outcome.meeting_booked


def test_activity_before_the_submission_does_not_count():
    old_meeting = normalize_engagement("meetings", {"id": "1", "properties": {"hs_timestamp": iso_utc(T0 - timedelta(days=9)), "hs_meeting_outcome": "COMPLETED"}})
    outcome = assess_inbound(_contact("1", engagements=[old_meeting], deals=[T0 - timedelta(days=1)], replied_at=T0 - timedelta(days=2)))
    assert outcome.first_touch is None
    assert not (outcome.meeting_booked or outcome.meeting_held or outcome.deal_created or outcome.replied)


def test_the_report_aggregates_per_group():
    touched = normalize_engagement("emails", _email("1", T0 + timedelta(hours=3)))
    held = normalize_engagement("meetings", {"id": "2", "properties": {"hs_timestamp": iso_utc(T0 + timedelta(days=1)), "hs_meeting_outcome": "COMPLETED"}})
    outcomes = [
        assess_inbound(_contact("1", owner="77", region="EMEA", engagements=[touched, held])),
        assess_inbound(_contact("2", region="EMEA")),
        assess_inbound(_contact("3", owner="78", region="NA")),
    ]
    total, groups = aggregate_inbound(outcomes, "region")
    assert (total.submitted, total.assigned, total.touched, total.meeting_held) == (3, 2, 1, 1)
    assert [g.name for g in groups] == ["EMEA", "NA"]
    line = format_group(groups[0])
    assert line.startswith("EMEA | submitted 2 | assigned 1 (50%) | touched 1 (50%) | touched within 24 h 1 (50%)")
    assert "first touch median 3.0 h, mean 3.0 h" in line and "first touch by manual email 1" in line
    _total, by_owner = aggregate_inbound(outcomes, "owner")
    assert "(none)" in [g.name for g in by_owner]
