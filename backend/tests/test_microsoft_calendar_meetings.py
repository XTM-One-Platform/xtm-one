"""Outlook calendar: Teams recording / transcription options and recurring series.

The tools run for real against a fake Microsoft Graph served through
``httpx.MockTransport`` (documented response shapes, no network): the
requests, their bodies, their query strings and their headers are the ones
production sends.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft._recurrence import (
    RecurrenceError,
    _build_patterned_recurrence,
    _describe_recurrence,
)
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_NTg0%40thread.v2/0"
    "?context=%7b%22Tid%22%3a%22tid%22%2c%22Oid%22%3a%22oid%22%7d"
)
# The onlineMeeting id built from JOIN_URL ({organizerOid}_{threadId}), read first.
BUILT = "/v1.0/me/onlineMeetings/oid_19:meeting_NTg0@thread.v2"
WEEKLY = {"pattern": {"type": "weekly", "daysOfWeek": ["wednesday"]}, "range": {"type": "endDate", "endDate": "2027-01-06"}}


class FakeGraph:
    """Routes ``(method, decoded path)`` to canned responses and records every request."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, response: Any) -> None:
        self.routes.insert(0, (method.upper(), path, response))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, path, response in self.routes:
            if request.method == method and request.url.path == path:
                return response(request) if callable(response) else response
        raise AssertionError(f"no fake route for {request.method} {request.url}")

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


@pytest.fixture
def graph(monkeypatch) -> FakeGraph:
    fake = FakeGraph()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return fake


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    """The retries that wait for a just-created meeting run without sleeping."""
    monkeypatch.setattr(ms.calendar, "_ONLINE_MEETING_RETRY_DELAYS", (0, 0, 0))


def _json(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


async def _call(name: str, **arguments: Any) -> ToolResult:
    executor = CallableToolExecutor()
    ms.build_tools("tok", executor)
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def _event(event_id: str = "EV1", **fields: Any) -> dict:
    return {
        "id": event_id,
        "subject": "Weekly sync",
        "isOnlineMeeting": True,
        "onlineMeeting": {"joinUrl": JOIN_URL},
        "isOrganizer": True,
        "organizer": {"emailAddress": {"address": "assistant@example.com"}},
        "type": "singleInstance",
        "start": {"dateTime": "2026-10-07T17:00:00.0000000", "timeZone": "Europe/Paris"},
        "end": {"dateTime": "2026-10-07T18:00:00.0000000", "timeZone": "Europe/Paris"},
        **fields,
    }


class FakeMeeting:
    """The onlineMeeting of the event: lookup by built id or join URL, PATCH, read back.

    With *by_id* the id built from the join URL answers; without, it misses
    (404) and the ``JoinWebUrl`` filter resolves the meeting.
    """

    def __init__(self, graph: FakeGraph, *, keeps_changes: bool = True, by_id: bool = True, **props: Any) -> None:
        self.props = {
            "id": "MTG1",
            "joinWebUrl": JOIN_URL,
            "recordAutomatically": False,
            "allowTranscription": True,
            "allowRecording": True,
            "lobbyBypassSettings": {"scope": "organization", "isDialInBypassEnabled": False},
            "allowedPresenters": "everyone",
            "meetingOptionsWebUrl": "https://teams.microsoft.com/meetingOptions/?organizerId=oid",
            **props,
        }
        self.patches: list[dict] = []
        self.keeps_changes = keeps_changes
        graph.on("GET", "/v1.0/me/onlineMeetings", lambda _r: httpx.Response(200, json={"value": [self.props]}))
        graph.on(
            "GET",
            BUILT,
            (lambda _r: httpx.Response(200, json=self.props)) if by_id else _json({"error": {"code": "NotFound"}}, status=404),
        )
        graph.on("PATCH", "/v1.0/me/onlineMeetings/MTG1", self._patch)
        graph.on("GET", "/v1.0/me/onlineMeetings/MTG1", lambda _r: httpx.Response(200, json=self.props))

    def _patch(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.patches.append(body)
        if self.keeps_changes:
            self.props.update(body)
        return httpx.Response(200, json={**self.props, **body})


# ── Recording and transcription ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_teams_event_created_with_record_automatically_reads_it_back(graph):
    graph.on("POST", "/v1.0/me/events", _json(_event(), status=201))
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    # The id built from the join URL misses: the JoinWebUrl filter finds the organizer's meeting.
    meeting = FakeMeeting(graph, by_id=False)

    result = await _call(
        "create_microsoft_calendar_event",
        subject="Weekly sync",
        start_datetime="2026-10-07T17:00:00",
        end_datetime="2026-10-07T18:00:00",
        timezone="Europe/Paris",
        is_online_meeting=True,
        record_automatically=True,
    )

    assert not result.is_error, result.content
    created = json.loads(graph.sent("POST", "/v1.0/me/events")[0].content)
    assert created["isOnlineMeeting"] is True and created["onlineMeetingProvider"] == "teamsForBusiness"
    assert meeting.patches == [{"recordAutomatically": True}]
    assert "read back from Microsoft Graph" in result.content
    assert "Record and transcribe automatically: on (recordAutomatically=true)" in result.content
    lookup = graph.sent("GET", "/v1.0/me/onlineMeetings")[0]
    # The join URL travels percent-encoded once inside the OData filter.
    assert lookup.url.params["$filter"] == f"JoinWebUrl eq '{JOIN_URL}'"
    assert b"19%253ameeting_NTg0" in lookup.url.query
    # The read-back is a GET after the PATCH, never the PATCH answer alone.
    order = [(r.method, r.url.path) for r in graph.requests]
    assert order.index(("GET", "/v1.0/me/onlineMeetings/MTG1")) > order.index(("PATCH", "/v1.0/me/onlineMeetings/MTG1"))


@pytest.mark.asyncio
async def test_the_meeting_id_built_from_the_join_url_is_read_first(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    meeting = FakeMeeting(graph)

    result = await _call("update_microsoft_calendar_event", event_id="EV1", record_automatically=True)

    assert not result.is_error, result.content
    assert graph.sent("GET", BUILT) and graph.sent("GET", "/v1.0/me/onlineMeetings") == []
    # The PATCH goes to the id Graph answered with.
    assert meeting.patches == [{"recordAutomatically": True}]


@pytest.mark.asyncio
async def test_all_three_options_are_sent_in_one_patch(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    meeting = FakeMeeting(graph, allowTranscription=False, allowRecording=False)

    result = await _call(
        "update_microsoft_calendar_event",
        event_id="EV1",
        record_automatically=True,
        allow_transcription=True,
        allow_recording="true",
    )

    assert not result.is_error, result.content
    assert meeting.patches == [{"recordAutomatically": True, "allowTranscription": True, "allowRecording": True}]
    assert graph.sent("PATCH", "/v1.0/me/events/EV1") == []


@pytest.mark.asyncio
async def test_recording_can_be_turned_off_and_back_on(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    meeting = FakeMeeting(graph, recordAutomatically=True)

    off = await _call("update_microsoft_calendar_event", event_id="EV1", record_automatically=False)
    on = await _call("update_microsoft_calendar_event", event_id="EV1", record_automatically=True)

    assert meeting.patches == [{"recordAutomatically": False}, {"recordAutomatically": True}]
    assert "recordAutomatically=false" in off.content and not off.is_error
    assert "recordAutomatically=true" in on.content and not on.is_error


@pytest.mark.asyncio
async def test_a_change_graph_does_not_keep_is_an_error_not_a_success(graph):
    graph.on("POST", "/v1.0/me/events", _json(_event(), status=201))
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    FakeMeeting(graph, keeps_changes=False)

    result = await _call(
        "create_microsoft_calendar_event",
        subject="Weekly sync",
        start_datetime="2026-10-07T17:00:00",
        end_datetime="2026-10-07T18:00:00",
        is_online_meeting=True,
        record_automatically=True,
    )

    assert result.is_error
    assert "did not keep the Teams meeting options" in result.content
    assert '"recordAutomatically": false' in result.content
    assert "was created (ID: EV1)" in result.content and "NOT changed" in result.content


@pytest.mark.asyncio
async def test_a_missing_scope_names_the_permission_to_grant(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    graph.on("GET", BUILT, _json({"id": "MTG1"}))
    graph.on(
        "PATCH",
        "/v1.0/me/onlineMeetings/MTG1",
        _json({"error": {"code": "Forbidden", "message": "Insufficient privileges"}}, status=403),
    )

    result = await _call("update_microsoft_calendar_event", event_id="EV1", record_automatically=True)

    assert result.is_error
    assert "OnlineMeetings.ReadWrite" in result.content
    assert "re-authorize the Microsoft integration" in result.content


@pytest.mark.asyncio
async def test_a_refused_lookup_names_the_read_permission(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    graph.on("GET", BUILT, _json({"error": {"code": "Forbidden"}}, status=403))
    graph.on("GET", "/v1.0/me/onlineMeetings", _json({"error": {"code": "Forbidden"}}, status=403))

    result = await _call("update_microsoft_calendar_event", event_id="EV1", allow_recording=False)

    assert result.is_error and "OnlineMeetings.Read" in result.content
    assert graph.sent("PATCH", "/v1.0/me/onlineMeetings/MTG1") == []


@pytest.mark.asyncio
async def test_options_need_a_teams_meeting_before_anything_is_written(graph):
    result = await _call(
        "create_microsoft_calendar_event",
        subject="Sync",
        start_datetime="2026-10-07T17:00:00",
        end_datetime="2026-10-07T18:00:00",
        record_automatically=True,
    )

    assert result.is_error and "is_online_meeting=true" in result.content
    assert graph.requests == []


@pytest.mark.asyncio
async def test_an_event_without_a_teams_meeting_has_no_options_to_change(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event(isOnlineMeeting=False, onlineMeeting=None)))

    result = await _call("update_microsoft_calendar_event", event_id="EV1", record_automatically=True)

    assert result.is_error and "has no Teams meeting" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("is_organizer", [False, None], ids=["attendee", "not-reported"])
async def test_only_the_organizer_changes_the_options(graph, is_organizer):
    graph.on(
        "GET",
        "/v1.0/me/events/EV1",
        _json(_event(isOrganizer=is_organizer, organizer={"emailAddress": {"address": "boss@filigran.io"}})),
    )

    result = await _call("update_microsoft_calendar_event", event_id="EV1", record_automatically=True)

    assert result.is_error and "boss@filigran.io" in result.content
    assert graph.sent("GET", "/v1.0/me/onlineMeetings") == [] and graph.sent("GET", BUILT) == []


@pytest.mark.asyncio
async def test_a_non_boolean_option_is_refused(graph):
    result = await _call("update_microsoft_calendar_event", event_id="EV1", record_automatically="yes")

    assert result.is_error and "record_automatically must be true or false" in result.content
    assert graph.requests == []


@pytest.mark.asyncio
async def test_the_join_url_graph_sets_late_is_waited_for(graph):
    reads = iter([_event(onlineMeeting=None), _event()])
    graph.on("POST", "/v1.0/me/events", _json(_event(onlineMeeting=None), status=201))
    graph.on("GET", "/v1.0/me/events/EV1", lambda _r: httpx.Response(200, json=next(reads)))
    meeting = FakeMeeting(graph)

    result = await _call(
        "create_microsoft_calendar_event",
        subject="Sync",
        start_datetime="2026-10-07T17:00:00",
        end_datetime="2026-10-07T18:00:00",
        is_online_meeting=True,
        record_automatically=True,
    )

    assert not result.is_error, result.content
    assert len(graph.sent("GET", "/v1.0/me/events/EV1")) == 2 and meeting.patches


@pytest.mark.asyncio
async def test_options_on_a_shared_calendar_go_through_that_calendar(graph):
    graph.on("POST", "/v1.0/me/calendars/CAL1/events", _json(_event(), status=201))
    graph.on("GET", "/v1.0/me/calendars/CAL1/events/EV1", _json(_event()))
    meeting = FakeMeeting(graph)

    result = await _call(
        "create_microsoft_calendar_event",
        subject="Sync",
        start_datetime="2026-10-07T17:00:00",
        end_datetime="2026-10-07T18:00:00",
        is_online_meeting=True,
        record_automatically=True,
        calendar_id="CAL1",
    )

    assert not result.is_error, result.content
    assert meeting.patches == [{"recordAutomatically": True}]


@pytest.mark.asyncio
async def test_get_event_reports_the_meeting_options(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    FakeMeeting(graph, recordAutomatically=True, allowedPresenters="organizer")

    result = await _call("get_microsoft_calendar_event", event_id="EV1")

    assert not result.is_error
    assert f"Teams join URL: {JOIN_URL}" in result.content
    assert "recordAutomatically=true" in result.content
    assert "allowTranscription=true" in result.content and "allowRecording=true" in result.content
    assert "Who can bypass the lobby: people in my organization" in result.content
    assert "Who can present: only me (the organizer)" in result.content
    select = graph.sent("GET", "/v1.0/me/events/EV1")[0].url.params["$select"].split(",")
    assert {"isOrganizer", "type", "seriesMasterId"} <= set(select)


@pytest.mark.asyncio
async def test_get_event_still_answers_when_the_options_cannot_be_read(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    graph.on("GET", BUILT, _json({"error": {"code": "Forbidden"}}, status=403))
    graph.on("GET", "/v1.0/me/onlineMeetings", _json({"error": {"code": "Forbidden"}}, status=403))

    result = await _call("get_microsoft_calendar_event", event_id="EV1")

    assert not result.is_error
    assert "Subject: Weekly sync" in result.content
    assert "Teams meeting options: could not be read" in result.content and "OnlineMeetings.Read" in result.content


@pytest.mark.asyncio
async def test_an_attendee_reads_the_options_through_the_built_meeting_id(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event(isOrganizer=False)))
    FakeMeeting(graph, recordAutomatically=True)

    result = await _call("get_microsoft_calendar_event", event_id="EV1")

    assert "recordAutomatically=true" in result.content
    # The JoinWebUrl filter resolves organized meetings only: never asked here.
    assert graph.sent("GET", "/v1.0/me/onlineMeetings") == []


@pytest.mark.asyncio
async def test_a_meeting_graph_never_shows_is_an_error_once_the_wait_is_spent(graph):
    graph.on("POST", "/v1.0/me/events", _json(_event(), status=201))
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    graph.on("GET", BUILT, _json({"error": {"code": "NotFound"}}, status=404))
    graph.on("GET", "/v1.0/me/onlineMeetings", _json({"value": []}))

    result = await _call(
        "create_microsoft_calendar_event",
        subject="Sync",
        start_datetime="2026-10-07T17:00:00",
        end_datetime="2026-10-07T18:00:00",
        is_online_meeting=True,
        record_automatically=True,
    )

    assert result.is_error
    assert "returned no Teams meeting" in result.content and "was created (ID: EV1)" in result.content
    # One look, then one per delay of the shared budget (the join URL was there at once).
    assert len(graph.sent("GET", "/v1.0/me/onlineMeetings")) == 1 + len(ms.calendar._ONLINE_MEETING_RETRY_DELAYS)


@pytest.mark.asyncio
async def test_a_read_back_that_fails_does_not_confirm_the_change(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    meeting = FakeMeeting(graph)
    graph.on("GET", "/v1.0/me/onlineMeetings/MTG1", _json({"error": {"code": "ServiceUnavailable"}}, status=503))

    result = await _call("update_microsoft_calendar_event", event_id="EV1", record_automatically=True)

    assert result.is_error and meeting.patches
    assert "could not be read back, so it is not confirmed" in result.content


@pytest.mark.asyncio
async def test_an_event_update_saved_with_its_options_refused_says_both(graph):
    graph.on("PATCH", "/v1.0/me/events/EV1", _json({"id": "EV1", "subject": "Renamed", "type": "singleInstance"}))
    graph.on("GET", "/v1.0/me/events/EV1", _json(_event()))
    graph.on("GET", BUILT, _json({"id": "MTG1"}))
    graph.on("PATCH", "/v1.0/me/onlineMeetings/MTG1", _json({"error": {"code": "Forbidden"}}, status=403))

    result = await _call("update_microsoft_calendar_event", event_id="EV1", subject="Renamed", allow_recording=False)

    assert result.is_error
    assert "OnlineMeetings.ReadWrite" in result.content
    assert "Calendar event updated: 'Renamed'" in result.content and "Its Teams meeting options were NOT changed." in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, 0, "yes"])
async def test_an_option_that_is_not_a_boolean_is_refused(graph, value):
    result = await _call("update_microsoft_calendar_event", event_id="EV1", allow_transcription=value)

    assert result.is_error and "allow_transcription must be true or false" in result.content
    assert graph.requests == []


# ── Recurrence: the argument as a Graph patternedRecurrence ───────────────


def test_a_weekly_recurrence_is_normalised_and_starts_on_the_event():
    built = _build_patterned_recurrence(
        {"pattern": {"type": "Weekly", "daysOfWeek": "Wednesday"}, "range": {"type": "enddate", "endDate": "2027-01-06"}},
        "2026-10-07T17:00:00",
    )

    # Graph requires firstDayOfWeek on a weekly pattern: its default, sunday, is supplied.
    assert built == {
        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"], "firstDayOfWeek": "sunday"},
        "range": {"type": "endDate", "startDate": "2026-10-07", "endDate": "2027-01-06"},
    }
    assert _describe_recurrence(built) == "weekly on Wednesday, every week; from 2026-10-07 until 2027-01-06"


def test_a_given_first_day_of_week_is_kept_and_other_patterns_get_none():
    weekly = _build_patterned_recurrence(
        {"pattern": {"type": "weekly", "daysOfWeek": ["monday"], "firstDayOfWeek": "Monday"}, "range": {"type": "noEnd"}},
        "2026-10-05T09:00:00",
    )
    daily = _build_patterned_recurrence({"pattern": {"type": "daily"}, "range": {"type": "noEnd"}}, "2026-10-05T09:00:00")

    assert weekly and weekly["pattern"]["firstDayOfWeek"] == "monday"
    assert daily and "firstDayOfWeek" not in daily["pattern"]


def test_a_recurrence_given_as_json_text_is_read():
    built = _build_patterned_recurrence(json.dumps(WEEKLY), "2026-10-07T17:00:00")
    assert built and built["pattern"]["daysOfWeek"] == ["wednesday"]


@pytest.mark.parametrize(
    ("recurrence", "reason"),
    [
        ({"pattern": {"type": "fortnightly"}, "range": {"type": "noEnd"}}, "recurrence.pattern.type must be one of"),
        ({"pattern": {"type": "weekly"}, "range": {"type": "noEnd"}}, "needs daysOfWeek"),
        ({"pattern": {"type": "weekly", "daysOfWeek": ["funday"]}, "range": {"type": "noEnd"}}, "daysOfWeek entry"),
        ({"pattern": {"type": "weekly", "daysOfWeek": ["monday"], "dayOfMonth": 3}, "range": {"type": "noEnd"}}, "does not take dayOfMonth"),
        ({"pattern": {"type": "daily", "interval": 0}, "range": {"type": "noEnd"}}, "interval must be a whole number"),
        ({"pattern": {"type": "daily", "interval": True}, "range": {"type": "noEnd"}}, "interval must be a whole number"),
        ({"pattern": {"type": "absoluteMonthly", "dayOfMonth": 32}, "range": {"type": "noEnd"}}, "dayOfMonth must be a whole number between 1 and 31"),
        ({"pattern": {"type": "absoluteYearly", "dayOfMonth": 30, "month": 2}, "range": {"type": "noEnd"}}, "between 1 and 29"),
        ({"pattern": {"type": "relativeMonthly", "daysOfWeek": ["monday"], "index": "fifth"}, "range": {"type": "noEnd"}}, "index must be one of"),
        ({"pattern": {"type": "daily"}, "range": {"type": "endDate"}}, "needs endDate"),
        ({"pattern": {"type": "daily"}, "range": {"type": "endDate", "endDate": "2026-10-01"}}, "is before its startDate"),
        ({"pattern": {"type": "daily"}, "range": {"type": "numbered"}}, "needs numberOfOccurrences"),
        ({"pattern": {"type": "daily"}, "range": {"type": "noEnd", "endDate": "2027-01-01"}}, "does not take endDate"),
        ({"pattern": {"type": "daily"}, "range": {"type": "noEnd", "startDate": "2026-10-08"}}, "must be the date of start_datetime"),
        ({"pattern": {"type": "daily"}, "range": {"type": "noEnd", "startDate": "next week"}}, "must be a date like"),
        ({"pattern": {"type": "daily"}}, "needs both a pattern and a range"),
        ({"pattern": {"type": "daily"}, "range": {"type": "noEnd"}, "every": "week"}, "does not take every"),
        (["weekly"], "must be an object"),
    ],
)
def test_an_invalid_recurrence_is_refused_with_its_reason(recurrence, reason):
    with pytest.raises(RecurrenceError, match=reason):
        _build_patterned_recurrence(recurrence, "2026-10-07T17:00:00")


@pytest.mark.parametrize(
    ("recurrence", "expected"),
    [
        (
            {"pattern": {"type": "daily", "interval": None}, "range": {"type": "noEnd", "endDate": None, "recurrenceTimeZone": None}},
            {"pattern": {"type": "daily", "interval": 1}, "range": {"type": "noEnd", "startDate": "2026-10-07"}},
        ),
        (
            {"pattern": {"type": "weekly", "daysOfWeek": ["monday"], "firstDayOfWeek": None}, "range": {"type": "numbered", "numberOfOccurrences": 4, "startDate": None}},
            {
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday"], "firstDayOfWeek": "sunday"},
                "range": {"type": "numbered", "startDate": "2026-10-07", "numberOfOccurrences": 4},
            },
        ),
        (
            {"pattern": {"type": "relativeMonthly", "daysOfWeek": ["friday"], "index": None, "dayOfMonth": None}, "range": {"type": "noEnd"}},
            {"pattern": {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["friday"]}, "range": {"type": "noEnd", "startDate": "2026-10-07"}},
        ),
    ],
    ids=["daily", "weekly", "relative-monthly"],
)
def test_a_property_sent_as_null_is_not_given(recurrence, expected):
    assert _build_patterned_recurrence(recurrence, "2026-10-07T17:00:00") == expected


def test_no_recurrence_is_a_one_off_event():
    assert _build_patterned_recurrence(None, "2026-10-07T17:00:00") is None
    assert _build_patterned_recurrence({}, "2026-10-07T17:00:00") is None


@pytest.mark.asyncio
async def test_a_weekly_series_is_created_with_one_meeting_for_every_occurrence(graph):
    series = _event(type="seriesMaster", recurrence={**WEEKLY, "range": {**WEEKLY["range"], "startDate": "2026-10-07"}})
    graph.on("POST", "/v1.0/me/events", _json(series, status=201))
    graph.on("GET", "/v1.0/me/events/EV1", _json(series))
    meeting = FakeMeeting(graph)

    result = await _call(
        "create_microsoft_calendar_event",
        subject="Weekly sync",
        start_datetime="2026-10-07T17:00:00",
        end_datetime="2026-10-07T18:00:00",
        timezone="Europe/Paris",
        is_online_meeting=True,
        recurrence=WEEKLY,
        record_automatically=True,
    )

    assert not result.is_error, result.content
    created = json.loads(graph.sent("POST", "/v1.0/me/events")[0].content)
    assert created["recurrence"] == {
        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"], "firstDayOfWeek": "sunday"},
        "range": {"type": "endDate", "startDate": "2026-10-07", "endDate": "2027-01-06"},
    }
    assert meeting.patches == [{"recordAutomatically": True}]
    assert "Recurrence: weekly on Wednesday, every week; from 2026-10-07 until 2027-01-06" in result.content
    assert "list_microsoft_event_occurrences(series_id=EV1)" in result.content
    assert "apply to every occurrence" in result.content


@pytest.mark.asyncio
async def test_an_invalid_recurrence_creates_nothing(graph):
    result = await _call(
        "create_microsoft_calendar_event",
        subject="Weekly sync",
        start_datetime="2026-10-07T17:00:00",
        end_datetime="2026-10-07T18:00:00",
        recurrence={"pattern": {"type": "weekly"}, "range": {"type": "noEnd"}},
    )

    assert result.is_error and "needs daysOfWeek" in result.content
    assert graph.requests == []


# ── Occurrences: list, move one, cancel one ───────────────────────────────


def _series(**fields: Any) -> dict:
    return {
        "id": "SERIES1",
        "subject": "Weekly sync",
        "type": "seriesMaster",
        "originalStartTimeZone": "Europe/Paris",
        "recurrence": {**WEEKLY, "range": {**WEEKLY["range"], "startDate": "2026-10-07"}},
        "cancelledOccurrences": ["OID.SERIES1.2026-10-28", "OID.SERIES1.2026-11-25", "OID.SERIES1.2027-03-31"],
        **fields,
    }


def _occurrence(day: str, occurrence_id: str, kind: str = "occurrence") -> dict:
    return {
        "id": occurrence_id,
        "type": kind,
        "start": {"dateTime": f"{day}T17:00:00.0000000", "timeZone": "Europe/Paris"},
        "end": {"dateTime": f"{day}T18:00:00.0000000", "timeZone": "Europe/Paris"},
    }


@pytest.mark.asyncio
async def test_occurrences_are_listed_in_the_series_time_zone_with_the_cancelled_ones(graph):
    graph.on("GET", "/v1.0/me/events/SERIES1", _json(_series()))
    graph.on(
        "GET",
        "/v1.0/me/events/SERIES1/instances",
        _json(
            {
                "value": [
                    _occurrence("2026-09-30", "BEFORE"),
                    _occurrence("2026-10-14", "OCC2"),
                    _occurrence("2026-10-07", "OCC1"),
                    _occurrence("2026-11-04", "OCC3", "exception"),
                    _occurrence("2026-12-31", "LASTDAY"),
                    _occurrence("2027-01-01", "AFTER"),
                ]
            }
        ),
    )

    result = await _call("list_microsoft_event_occurrences", series_id="SERIES1", start="2026-10-01", end="2026-12-31")

    assert not result.is_error, result.content
    lines = result.content.splitlines()
    assert "Pattern: weekly on Wednesday, every week; from 2026-10-07 until 2027-01-06" in lines
    # Bare dates are whole days of the series' calendar, the end date included.
    assert "Occurrences between 2026-10-01 and 2026-12-31 (4):" in lines
    assert lines.index("- Wed 2026-10-07 17:00-18:00 (Europe/Paris) | id: OCC1") < lines.index(
        "- Wed 2026-10-14 17:00-18:00 (Europe/Paris) | id: OCC2"
    )
    assert "- Wed 2026-11-04 17:00-18:00 (Europe/Paris) [moved or edited] | id: OCC3" in lines
    assert "- Thu 2026-12-31 17:00-18:00 (Europe/Paris) | id: LASTDAY" in lines
    assert "BEFORE" not in result.content and "AFTER" not in result.content
    assert "Cancelled occurrences between 2026-10-01 and 2026-12-31 (2): 2026-10-28, 2026-11-25" in lines
    # The series is found first; cancelledOccurrences is asked of the master's own read only.
    found, master = graph.sent("GET", "/v1.0/me/events/SERIES1")
    assert "cancelledOccurrences" not in found.url.params["$select"].split(",")
    assert "cancelledOccurrences" in master.url.params["$select"].split(",")
    instances = graph.sent("GET", "/v1.0/me/events/SERIES1/instances")[0]
    # A day wider on each side than the dates asked: a local midnight is never missed.
    assert instances.url.params["startDateTime"] == "2026-09-30T00:00:00Z"
    assert instances.url.params["endDateTime"] == "2027-01-02T00:00:00Z"
    assert instances.headers["prefer"] == 'outlook.timezone="Europe/Paris"'
    # A per-request header is laid over the session's: the bearer token stays.
    assert instances.headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_a_one_day_window_and_an_exact_window(graph):
    graph.on("GET", "/v1.0/me/events/SERIES1", _json(_series(cancelledOccurrences=["OID.SERIES1.2026-10-28"])))
    graph.on("GET", "/v1.0/me/events/SERIES1/instances", _json({"value": [_occurrence("2026-10-21", "OCC4")]}))

    one_day = await _call("list_microsoft_event_occurrences", series_id="SERIES1", start="2026-10-21", end="2026-10-21")
    exact = await _call(
        "list_microsoft_event_occurrences",
        series_id="SERIES1",
        start="2026-10-21T00:00:00Z",
        end="2026-10-28T00:00:00Z",
    )

    assert "Occurrences between 2026-10-21 and 2026-10-21 (1):" in one_day.content
    # An exact end instant is excluded, so is a cancelled occurrence on that date.
    assert "between 2026-10-21 and 2026-10-27" in exact.content and "Cancelled" not in exact.content
    params = graph.sent("GET", "/v1.0/me/events/SERIES1/instances")[-1].url.params
    assert (params["startDateTime"], params["endDateTime"]) == ("2026-10-21T00:00:00Z", "2026-10-28T00:00:00Z")


@pytest.mark.asyncio
async def test_occurrence_paging_stops_past_the_cap(graph, monkeypatch):
    monkeypatch.setattr(ms.calendar, "_OCCURRENCES_MAX", 2)
    base = "/v1.0/me/events/SERIES1"
    graph.on("GET", base, _json(_series(cancelledOccurrences=[])))
    pages = {
        None: {"value": [_occurrence("2026-10-07", "OCC1"), _occurrence("2026-10-14", "OCC2")], "@odata.nextLink": f"https://graph.microsoft.com{base}/instances?$skiptoken=p2"},
        "p2": {"value": [_occurrence("2026-10-21", "OCC3")], "@odata.nextLink": f"https://graph.microsoft.com{base}/instances?$skiptoken=p3"},
        "p3": {"value": [_occurrence("2026-10-28", "OCC4")]},
    }
    graph.on("GET", f"{base}/instances", lambda r: httpx.Response(200, json=pages[r.url.params.get("$skiptoken")]))

    result = await _call("list_microsoft_event_occurrences", series_id="SERIES1", start="2026-10-01", end="2026-12-31")

    assert "(the first 2; narrow the window to see the rest)" in result.content
    assert "OCC1" in result.content and "OCC2" in result.content and "OCC3" not in result.content
    assert [r.url.params.get("$skiptoken") for r in graph.sent("GET", f"{base}/instances")] == [None, "p2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", [None, 'Europe/Paris"; x="1', "tzone://Microsoft/Custom"], ids=["none", "unsafe", "custom"])
async def test_a_series_without_a_usable_time_zone_is_listed_in_utc(graph, zone):
    graph.on("GET", "/v1.0/me/events/SERIES1", _json(_series(originalStartTimeZone=zone, cancelledOccurrences=[])))
    graph.on("GET", "/v1.0/me/events/SERIES1/instances", _json({"value": [_occurrence("2026-10-07", "OCC1")]}))

    result = await _call("list_microsoft_event_occurrences", series_id="SERIES1", start="2026-10-01", end="2026-10-31")

    assert "Times and dates are in UTC" in result.content and "OCC1" in result.content
    assert "prefer" not in graph.sent("GET", "/v1.0/me/events/SERIES1/instances")[0].headers


@pytest.mark.asyncio
async def test_cancelled_occurrences_in_an_exact_window_are_matched_by_date(graph):
    graph.on("GET", "/v1.0/me/events/SERIES1", _json(_series(cancelledOccurrences=["OID.SERIES1.2026-10-28"])))
    graph.on("GET", "/v1.0/me/events/SERIES1/instances", _json({"value": []}))

    result = await _call(
        "list_microsoft_event_occurrences",
        series_id="SERIES1",
        start="2026-10-21T00:00:00Z",
        end="2026-10-29T00:00:00Z",
    )

    assert "Cancelled occurrences between 2026-10-21 and 2026-10-28 (1) (matched by date" in result.content


@pytest.mark.asyncio
async def test_an_occurrence_id_lists_its_series(graph):
    graph.on("GET", "/v1.0/me/events/OCC1", _json({"id": "OCC1", "type": "occurrence", "seriesMasterId": "SERIES1"}))
    graph.on("GET", "/v1.0/me/events/SERIES1", _json(_series(cancelledOccurrences=[])))
    graph.on("GET", "/v1.0/me/events/SERIES1/instances", _json({"value": [_occurrence("2026-10-07", "OCC1")]}))

    result = await _call("list_microsoft_event_occurrences", series_id="OCC1", start="2026-10-01", end="2026-10-31")

    assert not result.is_error, result.content
    assert "(series_id: SERIES1)" in result.content and "Cancelled" not in result.content


@pytest.mark.asyncio
async def test_occurrences_follow_pages_on_a_shared_calendar(graph):
    base = "/v1.0/me/calendars/CAL1/events/SERIES1"
    graph.on("GET", base, _json(_series(cancelledOccurrences=[])))
    pages = iter(
        [
            {"value": [_occurrence("2026-10-07", "OCC1")], "@odata.nextLink": f"https://graph.microsoft.com{base}/instances?$skiptoken=p2"},
            {"value": [_occurrence("2026-10-14", "OCC2")]},
        ]
    )
    graph.on("GET", f"{base}/instances", lambda _r: httpx.Response(200, json=next(pages)))

    result = await _call(
        "list_microsoft_event_occurrences", series_id="SERIES1", start="2026-10-01", end="2026-10-31", calendar_id="CAL1"
    )

    assert "(2):" in result.content and "OCC2" in result.content
    assert all(r.headers["prefer"] == 'outlook.timezone="Europe/Paris"' for r in graph.sent("GET", f"{base}/instances"))


@pytest.mark.asyncio
async def test_a_one_off_event_has_no_occurrences(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json({"id": "EV1", "type": "singleInstance"}))

    result = await _call("list_microsoft_event_occurrences", series_id="EV1", start="2026-10-01", end="2026-10-31")

    assert result.is_error and "not part of a recurring series" in result.content


@pytest.mark.asyncio
async def test_an_inverted_window_is_refused(graph):
    result = await _call("list_microsoft_event_occurrences", series_id="SERIES1", start="2026-10-31", end="2026-10-01")

    assert result.is_error and "end must be after start" in result.content
    assert graph.requests == []


@pytest.mark.asyncio
async def test_cancelling_one_occurrence_notifies_the_attendees_with_the_comment(graph):
    graph.on(
        "GET",
        "/v1.0/me/events/OCC3",
        _json({"id": "OCC3", "isOrganizer": True, "type": "occurrence", "seriesMasterId": "SERIES1", "attendees": [{"emailAddress": {"address": "a@filigran.io"}}]}),
    )
    graph.on("POST", "/v1.0/me/events/OCC3/cancel", httpx.Response(202))

    result = await _call("delete_microsoft_calendar_event", event_id="OCC3", comment="No sync during the all-hands")

    assert not result.is_error, result.content
    # The action's parameter is named "Comment" in Graph's metadata (as the Graph SDKs send it).
    assert json.loads(graph.sent("POST", "/v1.0/me/events/OCC3/cancel")[0].content) == {"Comment": "No sync during the all-hands"}
    assert graph.sent("DELETE", "/v1.0/me/events/OCC3") == []
    assert "1 attendee(s) received a cancellation notice with your comment" in result.content
    assert "Only this occurrence was cancelled; the rest of the series is untouched." in result.content


@pytest.mark.asyncio
async def test_cancelling_a_series_cancels_every_occurrence(graph):
    graph.on(
        "GET",
        "/v1.0/me/events/SERIES1",
        _json({"id": "SERIES1", "isOrganizer": True, "type": "seriesMaster", "attendees": [{"emailAddress": {"address": "a@filigran.io"}}]}),
    )
    graph.on("POST", "/v1.0/me/events/SERIES1/cancel", httpx.Response(202))

    result = await _call("delete_microsoft_calendar_event", event_id="SERIES1")

    assert json.loads(graph.sent("POST", "/v1.0/me/events/SERIES1/cancel")[0].content) == {}
    assert "The whole recurring series was cancelled, every occurrence included." in result.content


@pytest.mark.asyncio
async def test_an_event_that_cannot_be_read_is_not_deleted(graph):
    graph.on("GET", "/v1.0/me/events/GONE", _json({"error": {"code": "ErrorItemNotFound"}}, status=404))

    result = await _call("delete_microsoft_calendar_event", event_id="GONE")

    assert result.is_error and "not found" in result.content
    assert [r.method for r in graph.requests] == ["GET"]


@pytest.mark.asyncio
async def test_a_refused_cancellation_is_reported_and_nothing_is_deleted(graph):
    graph.on(
        "GET",
        "/v1.0/me/calendars/CAL1/events/EV1",
        _json({"id": "EV1", "isOrganizer": True, "type": "singleInstance", "attendees": [{"emailAddress": {"address": "a@x"}}]}),
    )
    graph.on("POST", "/v1.0/me/calendars/CAL1/events/EV1/cancel", _json({"error": {"code": "ErrorAccessDenied"}}, status=403))

    result = await _call("delete_microsoft_calendar_event", event_id="EV1", calendar_id="CAL1", comment="Sorry")

    assert result.is_error and "access denied" in result.content
    assert graph.sent("DELETE", "/v1.0/me/calendars/CAL1/events/EV1") == []


@pytest.mark.asyncio
async def test_an_event_without_attendees_is_deleted(graph):
    graph.on("GET", "/v1.0/me/events/EV1", _json({"id": "EV1", "isOrganizer": True, "type": "singleInstance", "attendees": []}))
    graph.on("DELETE", "/v1.0/me/events/EV1", httpx.Response(204))

    result = await _call("delete_microsoft_calendar_event", event_id="EV1", comment="bye")

    assert not result.is_error
    assert "Calendar event EV1 deleted." in result.content and "The comment was not sent" in result.content
    assert graph.sent("POST", "/v1.0/me/events/EV1/cancel") == []


@pytest.mark.asyncio
async def test_an_attendee_deleting_a_meeting_removes_only_their_copy(graph):
    graph.on(
        "GET",
        "/v1.0/me/events/EV1",
        _json({"id": "EV1", "isOrganizer": False, "type": "singleInstance", "attendees": [{"emailAddress": {"address": "a@x"}}]}),
    )
    graph.on("DELETE", "/v1.0/me/events/EV1", httpx.Response(204))

    result = await _call("delete_microsoft_calendar_event", event_id="EV1")

    assert "removed from your calendar only" in result.content
    assert graph.sent("POST", "/v1.0/me/events/EV1/cancel") == []


@pytest.mark.asyncio
async def test_moving_one_occurrence_leaves_the_series(graph):
    graph.on(
        "PATCH",
        "/v1.0/me/events/OCC2",
        _json({"id": "OCC2", "subject": "Weekly sync", "type": "exception", "seriesMasterId": "SERIES1"}),
    )

    result = await _call(
        "update_microsoft_calendar_event",
        event_id="OCC2",
        start_datetime="2026-10-15T17:00:00",
        end_datetime="2026-10-15T18:00:00",
        timezone="Europe/Paris",
    )

    assert not result.is_error
    body = json.loads(graph.sent("PATCH", "/v1.0/me/events/OCC2")[0].content)
    assert body["start"] == {"dateTime": "2026-10-15T17:00:00", "timeZone": "Europe/Paris"}
    assert "Only this occurrence was changed; the rest of the series is untouched." in result.content


@pytest.mark.asyncio
async def test_the_listing_links_an_occurrence_to_its_series(graph):
    graph.on(
        "GET",
        "/v1.0/me/calendarView",
        _json({"value": [{"id": "OCC1", "subject": "Weekly sync", "seriesMasterId": "SERIES1", "start": {"dateTime": "2026-10-07T15:00:00"}, "end": {"dateTime": "2026-10-07T16:00:00"}}]}),
    )

    result = await _call("list_microsoft_calendar_events", start_datetime="2026-10-07T00:00:00Z", end_datetime="2026-10-08T00:00:00Z")

    assert "| id: OCC1 | series_id: SERIES1" in result.content


@pytest.mark.asyncio
async def test_no_online_meeting_is_an_empty_structured_list_a_gate_check_reads_as_empty(graph: FakeGraph) -> None:
    """A gate check tests the structured value: no meeting is ``[]``, never the non-empty text."""
    from app.services.assignment_activation_check import condition_met, structured_result

    graph.on(
        "GET", "/v1.0/me/calendarView", _json({"value": [{"id": "E1", "subject": "Lunch", "isOnlineMeeting": False}]})
    )

    result = await _call("list_microsoft_online_meetings", days=1)

    data, readable = structured_result(result)
    assert readable and data == []
    assert "No online meetings found" in result.content
    assert not condition_met(data, "non_empty")
    assert condition_met(data, "empty")


@pytest.mark.asyncio
async def test_online_meetings_are_listed_as_the_structured_value(graph: FakeGraph) -> None:
    event = {
        "subject": "ExCo",
        "isOnlineMeeting": True,
        "onlineMeeting": {"joinUrl": JOIN_URL},
        "start": {"dateTime": "2026-11-19T09:00:00.0000000"},
        "end": {"dateTime": "2026-11-19T10:00:00.0000000"},
        "organizer": {"emailAddress": {"name": "Organizer"}},
    }
    graph.on("GET", "/v1.0/me/calendarView", _json({"value": [event]}))

    result = await _call("list_microsoft_online_meetings", days=7)

    assert not result.is_error
    assert "Found 1 online meeting(s)" in result.content
    [record] = result.structured
    assert record["subject"] == "ExCo"
    assert record["start"] == "2026-11-19 09:00:00"
    assert record["organizer"] == "Organizer"
    assert record["join_url"] == JOIN_URL
    assert record["meeting_id"]


def test_the_new_parameters_are_optional_and_described():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in ms.build_tools("tok", executor)}
    create = tools["create_microsoft_calendar_event"].input_schema
    update = tools["update_microsoft_calendar_event"].input_schema
    for name in ("recurrence", "record_automatically", "allow_transcription", "allow_recording"):
        assert name in create["properties"] and name not in create["required"]
    for name in ("record_automatically", "allow_transcription", "allow_recording"):
        assert name in update["properties"] and name not in update["required"]
    assert "comment" in tools["delete_microsoft_calendar_event"].input_schema["properties"]
    assert tools["list_microsoft_event_occurrences"].input_schema["required"] == ["series_id", "start", "end"]
    for name in ("create_microsoft_calendar_event", "update_microsoft_calendar_event"):
        assert "record_automatically" in tools[name].description
    assert "recurrence" in tools["create_microsoft_calendar_event"].description
    assert "meeting options" in tools["get_microsoft_calendar_event"].description
    assert "occurrence" in tools["delete_microsoft_calendar_event"].description
