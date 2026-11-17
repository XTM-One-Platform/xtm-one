"""Google Calendar all-day events and read windows (true ``start.date`` / ``end.date`` events).

The tools take an all-day event's last day included and write Google's
exclusive ``end.date``; ``read_google_calendar`` lists a chosen window, past
included, and marks all-day events.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.integrations.providers import google
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

EVENTS = "/calendar/v3/calendars/primary/events"


class _Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: dict = {"id": "EV1", "summary": "Trip", "htmlLink": "https://calendar.google.com/e"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.answer)


@pytest.fixture
def api(monkeypatch) -> _Fake:
    fake = _Fake()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return fake


async def _call(name: str, **arguments: Any) -> ToolResult:
    executor = CallableToolExecutor()
    google.build_tools("tok", executor)
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


# ── create_google_event ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_all_day_event_is_written_with_dates_and_an_exclusive_end(api):
    result = await _call(
        "create_google_event", summary="Trip", start_time="2026-11-04", end_time="2026-11-07", all_day=True
    )

    assert not result.is_error, result.content
    [request] = api.requests
    assert request.method == "POST" and request.url.path == EVENTS
    body = _body(request)
    # 4, 5, 6 and 7 Nov: Google's end date is the day after the last one.
    assert body["start"] == {"date": "2026-11-04"} and body["end"] == {"date": "2026-11-08"}
    assert "All day: 2026-11-04 to 2026-11-07, both included." in result.content


@pytest.mark.asyncio
async def test_two_bare_dates_make_an_all_day_event_without_the_flag(api):
    await _call("create_google_event", summary="Off", start_time="2026-11-04", end_time="2026-11-04")

    body = _body(api.requests[0])
    assert body["start"] == {"date": "2026-11-04"} and body["end"] == {"date": "2026-11-05"}


@pytest.mark.asyncio
async def test_a_timed_event_is_written_as_before(api):
    await _call(
        "create_google_event",
        summary="Review",
        start_time="2026-11-04T10:00:00",
        end_time="2026-11-04T11:00:00",
        timezone="Europe/Paris",
    )

    body = _body(api.requests[0])
    assert body["start"] == {"dateTime": "2026-11-04T10:00:00", "timeZone": "Europe/Paris"}
    assert body["end"] == {"dateTime": "2026-11-04T11:00:00", "timeZone": "Europe/Paris"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end", "all_day", "message"),
    [
        ("2026-11-04T10:00:00", "2026-11-04T11:00:00", True, "YYYY-MM-DD"),
        ("2026-11-04", "2026-11-04T11:00:00", None, "YYYY-MM-DD"),
        ("2026-11-07", "2026-11-04", True, "before start_time"),
        ("2026-11-04", "2026-11-05", False, "date-times"),
    ],
    ids=["flag-with-times", "mixed-bounds", "end-before-start", "false-with-dates"],
)
async def test_an_inconsistent_all_day_request_is_refused_before_any_write(api, start, end, all_day, message):
    arguments: dict[str, Any] = {"summary": "s", "start_time": start, "end_time": end}
    if all_day is not None:
        arguments["all_day"] = all_day

    result = await _call("create_google_event", **arguments)

    assert result.is_error and message in result.content
    assert api.requests == []


# ── update_google_event ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_update_turns_a_timed_event_all_day_and_clears_its_times(api):
    result = await _call(
        "update_google_event", event_id="EV1", start_time="2026-11-04", end_time="2026-11-06", all_day=True
    )

    assert not result.is_error, result.content
    [request] = api.requests
    assert request.method == "PATCH"
    body = _body(request)
    assert body["start"] == {"date": "2026-11-04", "dateTime": None, "timeZone": None}
    assert body["end"] == {"date": "2026-11-07", "dateTime": None, "timeZone": None}


@pytest.mark.asyncio
async def test_an_update_turns_an_all_day_event_back_into_a_timed_one(api):
    await _call(
        "update_google_event",
        event_id="EV1",
        start_time="2026-11-04T09:00:00",
        end_time="2026-11-04T10:00:00",
        timezone="Europe/Paris",
        all_day=False,
    )

    body = _body(api.requests[0])
    assert body["start"] == {"dateTime": "2026-11-04T09:00:00", "timeZone": "Europe/Paris", "date": None}
    assert body["end"] == {"dateTime": "2026-11-04T10:00:00", "timeZone": "Europe/Paris", "date": None}


@pytest.mark.asyncio
async def test_all_day_on_an_update_needs_both_bounds(api):
    result = await _call("update_google_event", event_id="EV1", start_time="2026-11-04", all_day=True)

    assert result.is_error and "needs both start_time and end_time" in result.content
    assert api.requests == []


@pytest.mark.asyncio
async def test_a_lone_date_on_an_update_is_refused(api):
    result = await _call("update_google_event", event_id="EV1", end_time="2026-11-04")

    assert result.is_error and "pass both start_time and end_time" in result.content
    assert api.requests == []


# ── read_google_calendar / get_google_event ──────────────────────────────


@pytest.mark.asyncio
async def test_read_lists_a_past_window_and_marks_all_day_events(api):
    api.answer = {
        "items": [
            {"id": "A1", "summary": "Trip", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-08"}},
            {"id": "A2", "summary": "Off", "start": {"date": "2026-11-09"}, "end": {"date": "2026-11-10"}},
            {
                "id": "T1",
                "summary": "Review",
                "start": {"dateTime": "2026-11-04T10:00:00+01:00"},
                "end": {"dateTime": "2026-11-04T11:00:00+01:00"},
            },
        ]
    }

    result = await _call("read_google_calendar", start_datetime="2026-11-01", end_datetime="2026-11-10")

    params = api.requests[0].url.params
    assert params["timeMin"] == "2026-11-01T00:00:00.000000Z"
    # An end date includes its whole day.
    assert params["timeMax"] == "2026-11-11T00:00:00.000000Z"
    assert "Event: Trip | 2026-11-04 -> 2026-11-07 (all day)" in result.content
    assert "Event: Off | 2026-11-09 (all day)" in result.content
    assert "Event: Review | 2026-11-04T10:00:00+01:00 \u2014 2026-11-04T11:00:00+01:00" in result.content


@pytest.mark.asyncio
async def test_read_without_bounds_keeps_the_next_days_window(api):
    api.answer = {"items": []}
    before = datetime.now(UTC)

    result = await _call("read_google_calendar", days_ahead=3)

    params = api.requests[0].url.params
    time_min = datetime.strptime(params["timeMin"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    time_max = datetime.strptime(params["timeMax"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert before - timedelta(seconds=5) <= time_min <= datetime.now(UTC)
    assert time_max - time_min == timedelta(days=3)
    assert result.content == "No events found in the next 3 days."


@pytest.mark.asyncio
async def test_read_with_days_back_starts_in_the_past(api):
    api.answer = {"items": []}

    await _call("read_google_calendar", days_back=10, days_ahead=0)

    params = api.requests[0].url.params
    time_min = datetime.strptime(params["timeMin"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    time_max = datetime.strptime(params["timeMax"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert time_max - time_min == timedelta(days=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"start_datetime": "next week"}, "ISO 8601"),
        ({"start_datetime": "2026-11-10", "end_datetime": "2026-11-01"}, "ends before it starts"),
        ({"days_back": -1}, "non-negative integer"),
    ],
    ids=["unreadable", "reversed", "negative"],
)
async def test_read_refuses_a_bad_window_before_any_request(api, arguments, message):
    result = await _call("read_google_calendar", **arguments)

    assert result.is_error and message in result.content
    assert api.requests == []


@pytest.mark.asyncio
async def test_get_event_reports_an_all_day_event_and_its_last_day(api):
    api.answer = {"id": "A1", "summary": "Trip", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-08"}}

    result = await _call("get_google_event", event_id="A1")

    assert "Start: 2026-11-04\n" in result.content
    assert "End: 2026-11-07 (last day, included)\n" in result.content
    assert "All day: Yes\n" in result.content


def test_the_calendar_tools_document_all_day_events_and_windows():
    executor = CallableToolExecutor()
    definitions = {d.name: d for d in google.build_tools("tok", executor)}

    assert "all_day" in definitions["create_google_event"].input_schema["properties"]
    assert "all_day" in definitions["update_google_event"].input_schema["properties"]
    read = definitions["read_google_calendar"].input_schema["properties"]
    assert {"start_datetime", "end_datetime", "days_back", "days_ahead"} <= set(read)
