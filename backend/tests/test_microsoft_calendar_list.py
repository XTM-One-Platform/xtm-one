"""Regression tests for the Microsoft calendar-list tool.

The tool was reshaped to accept ISO 8601 strings (not `datetime` objects)
and to support both past and future events. The tests below lock in:

- ISO parsing edge cases (`Z` suffix, explicit offset, naive → UTC).
- Default windowing (start only, end only, neither provided).
- Reverse-range validation (``end <= start`` rejected).
- The URL-encoding fix (date params travel via the ``params`` dict, never
  embedded as ``+00:00`` in the URL path).
- The corrected JSON Schema (``string`` type, not ``datetime``).
- Stale "upcoming" description replaced with the past+future wording.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._common import _parse_iso_datetime
from app.tools.base import CallableToolExecutor, ToolCall

# `asyncio_mode = "auto"` is set in pyproject.toml so the `@pytest.mark.asyncio`
# marker is technically redundant — we keep it explicit on the runtime
# tests below to match the convention used in the recently-converted
# suites (`test_langfuse_env_seed.py`, `test_langfuse_tracing.py`, …) and
# so a grep on `@pytest.mark.asyncio` always finds every async test.

# ── _parse_iso_datetime ──────────────────────────────────────────────────


def test_parse_iso_datetime_accepts_z_suffix():
    parsed = _parse_iso_datetime("2026-05-06T22:33:00Z")
    assert parsed == datetime(2026, 5, 6, 22, 33, tzinfo=UTC)
    assert parsed.tzinfo == UTC


def test_parse_iso_datetime_accepts_explicit_offset_and_normalises_to_utc():
    # 20:33 in +02:00 is 18:33 UTC.
    parsed = _parse_iso_datetime("2026-05-06T20:33:00+02:00")
    assert parsed == datetime(2026, 5, 6, 18, 33, tzinfo=UTC)
    assert parsed.tzinfo == UTC


def test_parse_iso_datetime_treats_naive_input_as_utc():
    parsed = _parse_iso_datetime("2026-05-06T22:33:00")
    assert parsed == datetime(2026, 5, 6, 22, 33, tzinfo=UTC)


def test_parse_iso_datetime_rejects_empty_string():
    with pytest.raises(ValueError, match="non-empty"):
        _parse_iso_datetime("")
    with pytest.raises(ValueError, match="non-empty"):
        _parse_iso_datetime("   ")


def test_parse_iso_datetime_rejects_garbage():
    with pytest.raises(ValueError, match="Invalid ISO 8601"):
        _parse_iso_datetime("not-a-date")
    with pytest.raises(ValueError, match="Invalid ISO 8601"):
        _parse_iso_datetime("2026/05/06 22:33")


def test_parse_iso_datetime_rejects_non_string_inputs():
    """Tool arguments are LLM-supplied JSON — a number, list, dict, or
    ``None`` would otherwise crash inside ``.strip()`` with an
    ``AttributeError`` and reach the executor as an opaque exception
    instead of a clean tool-readable ``ValueError``."""
    for bad in (None, 12345, 12.5, ["2026-01-01"], {"value": "2026-01-01"}):
        with pytest.raises(ValueError, match="must be an ISO 8601 string"):
            _parse_iso_datetime(bad)


# ── Tool definition shape ────────────────────────────────────────────────


def _calendar_tool_definition(tools):
    for t in tools:
        if t.name == "list_microsoft_calendar_events":
            return t
    raise AssertionError("list_microsoft_calendar_events not registered")


def test_tool_schema_uses_string_type_not_datetime():
    """Critical fix: JSON Schema must use a recognised type."""
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    definition = _calendar_tool_definition(tools)

    props = definition.input_schema["properties"]
    assert props["start_datetime"]["type"] == "string"
    assert props["end_datetime"]["type"] == "string"
    # "datetime" is not a valid JSON Schema type; ensure we did not regress.
    for name in ("start_datetime", "end_datetime"):
        assert props[name]["type"] != "datetime"


def test_tool_schema_documents_iso_8601_format():
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    definition = _calendar_tool_definition(tools)

    props = definition.input_schema["properties"]
    for name in ("start_datetime", "end_datetime"):
        desc = props[name]["description"]
        assert "ISO 8601" in desc


def test_tool_schema_keeps_days_as_int_with_default():
    """AreDee-Bangs requested `days` stay as `int` with a default value."""
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    definition = _calendar_tool_definition(tools)

    days_schema = definition.input_schema["properties"]["days"]
    assert days_schema["type"] == "integer"
    assert days_schema["default"] == 7


def test_tool_description_mentions_past_and_future():
    """Reviewer pointed out the description still said "upcoming" only."""
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    definition = _calendar_tool_definition(tools)

    desc = definition.description.lower()
    assert "past" in desc and "future" in desc


def test_tool_definition_makes_all_inputs_optional():
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    definition = _calendar_tool_definition(tools)

    assert definition.input_schema["required"] == []


# ── Tool runtime behaviour (mocked HTTP) ─────────────────────────────────


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RequestRecorder:
    """Stand-in for `safe_request` that captures the call shape."""

    def __init__(self, payload: dict[str, Any] | None = None):
        self.payload = payload or {"value": []}
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, provider, op, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "provider": provider, "op": op, **kwargs}
        )
        return _StubResponse(self.payload), None


async def _run_tool(monkeypatch, *, payload=None, **arguments):
    """Invoke the tool with the live executor, mocking `safe_request`.

    Async helper so each call site can ``await`` it from an ``async def``
    test under ``pytest-asyncio``'s auto mode — replaces the previous
    per-test ``asyncio.run()`` wrapping.
    """
    recorder = _RequestRecorder(payload=payload)
    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", recorder)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="list_microsoft_calendar_events",
            arguments=arguments,
        )
    )
    return recorder, result


@pytest.mark.asyncio
async def test_runtime_uses_params_dict_and_avoids_plus_encoding_bug(monkeypatch):
    """Critical fix: never embed `+00:00` in the URL — use params + ``Z``."""
    recorder, result = await _run_tool(
        monkeypatch,
        start_datetime="2026-05-06T22:33:00Z",
        end_datetime="2026-05-13T22:33:00Z",
    )
    assert not result.is_error
    assert len(recorder.calls) == 1
    call = recorder.calls[0]

    # URL must NOT carry the date inline (would break with `+00:00`).
    assert "startDateTime=" not in call["url"]
    assert "endDateTime=" not in call["url"]
    assert "+00:00" not in call["url"]

    # All date params travel via the ``params`` dict.
    params = call["params"]
    assert params["startDateTime"] == "2026-05-06T22:33:00Z"
    assert params["endDateTime"] == "2026-05-13T22:33:00Z"
    # And the values keep the safe ``Z`` suffix, never the unencoded plus.
    assert "+" not in params["startDateTime"]
    assert "+" not in params["endDateTime"]


@pytest.mark.asyncio
async def test_runtime_default_window_uses_now_plus_seven_days(monkeypatch):
    """No inputs → now → now + 7 days (preserves historical behaviour)."""
    before = datetime.now(UTC)
    recorder, result = await _run_tool(monkeypatch)
    after = datetime.now(UTC)

    assert not result.is_error
    params = recorder.calls[0]["params"]
    start = datetime.strptime(params["startDateTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    end = datetime.strptime(params["endDateTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert before.replace(microsecond=0) <= start <= after.replace(microsecond=0) + timedelta(seconds=1)
    # End must be ~7 days after start (allowing a tolerance for second rounding).
    assert timedelta(days=7) - timedelta(seconds=2) <= end - start <= timedelta(days=7) + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_runtime_start_only_defaults_end_to_start_plus_days(monkeypatch):
    recorder, _ = await _run_tool(
        monkeypatch,
        start_datetime="2026-01-01T00:00:00Z",
    )
    params = recorder.calls[0]["params"]
    assert params["startDateTime"] == "2026-01-01T00:00:00Z"
    assert params["endDateTime"] == "2026-01-08T00:00:00Z"


@pytest.mark.asyncio
async def test_runtime_start_only_with_custom_days(monkeypatch):
    recorder, _ = await _run_tool(
        monkeypatch,
        start_datetime="2026-01-01T00:00:00Z",
        days=30,
    )
    params = recorder.calls[0]["params"]
    assert params["startDateTime"] == "2026-01-01T00:00:00Z"
    assert params["endDateTime"] == "2026-01-31T00:00:00Z"


@pytest.mark.asyncio
async def test_runtime_end_only_defaults_start_to_end_minus_days(monkeypatch):
    """End-only is the "last N days" lookback shape."""
    recorder, _ = await _run_tool(
        monkeypatch,
        end_datetime="2026-01-08T00:00:00Z",
        days=7,
    )
    params = recorder.calls[0]["params"]
    assert params["startDateTime"] == "2026-01-01T00:00:00Z"
    assert params["endDateTime"] == "2026-01-08T00:00:00Z"


@pytest.mark.asyncio
async def test_runtime_both_provided_ignores_days(monkeypatch):
    recorder, _ = await _run_tool(
        monkeypatch,
        start_datetime="2026-01-01T00:00:00Z",
        end_datetime="2026-12-31T00:00:00Z",
        days=999,
    )
    params = recorder.calls[0]["params"]
    assert params["startDateTime"] == "2026-01-01T00:00:00Z"
    assert params["endDateTime"] == "2026-12-31T00:00:00Z"


@pytest.mark.asyncio
async def test_runtime_rejects_reverse_range(monkeypatch):
    """end_datetime before start_datetime is an obvious user error."""
    recorder, result = await _run_tool(
        monkeypatch,
        start_datetime="2026-12-31T00:00:00Z",
        end_datetime="2026-01-01T00:00:00Z",
    )
    assert "end_datetime must be after start_datetime" in result.content
    # Tool must short-circuit before hitting Graph.
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_runtime_rejects_equal_range(monkeypatch):
    recorder, result = await _run_tool(
        monkeypatch,
        start_datetime="2026-01-01T00:00:00Z",
        end_datetime="2026-01-01T00:00:00Z",
    )
    assert "end_datetime must be after start_datetime" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_runtime_rejects_malformed_iso(monkeypatch):
    recorder, result = await _run_tool(
        monkeypatch,
        start_datetime="not-a-date",
    )
    assert "Invalid ISO 8601" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_runtime_rejects_non_positive_days(monkeypatch):
    for bad in (0, -1):
        recorder, result = await _run_tool(monkeypatch, days=bad)
        assert "positive integer" in result.content
        assert recorder.calls == []


@pytest.mark.asyncio
async def test_runtime_rejects_bool_days(monkeypatch):
    """`bool` is a subclass of `int`; the validator must reject it
    explicitly so ``days=True`` isn't silently treated as a 1-day window."""
    for bad in (True, False):
        recorder, result = await _run_tool(monkeypatch, days=bad)
        assert "positive integer" in result.content
        assert recorder.calls == []


@pytest.mark.asyncio
async def test_runtime_rejects_non_string_iso(monkeypatch):
    """A non-string ``start_datetime`` (e.g. number, dict, ``None``) must
    fall through ``_parse_iso_datetime``'s explicit type guard and return
    a tool-readable error rather than bubbling up as an ``AttributeError``
    from ``.strip()``."""
    for bad in (12345, ["2026-01-01"], {"value": "2026-01-01"}):
        recorder, result = await _run_tool(monkeypatch, start_datetime=bad)
        assert "must be an ISO 8601 string" in result.content
        assert recorder.calls == []


@pytest.mark.asyncio
async def test_runtime_handles_events_response(monkeypatch):
    payload = {
        "value": [
            {
                "subject": "Standup",
                "start": {"dateTime": "2026-05-06T09:00:00Z"},
                "end": {"dateTime": "2026-05-06T09:15:00Z"},
                "location": {"displayName": "Teams"},
            }
        ]
    }
    _, result = await _run_tool(
        monkeypatch,
        payload=payload,
        start_datetime="2026-05-06T00:00:00Z",
        end_datetime="2026-05-07T00:00:00Z",
    )
    assert not result.is_error
    assert "Standup" in result.content
    assert "Teams" in result.content


@pytest.mark.asyncio
async def test_runtime_empty_response_mentions_range(monkeypatch):
    _, result = await _run_tool(
        monkeypatch,
        payload={"value": []},
        start_datetime="2026-05-06T00:00:00Z",
        end_datetime="2026-05-07T00:00:00Z",
    )
    assert "No calendar events" in result.content
    # No longer says "upcoming" (which was the stale wording).
    assert "upcoming" not in result.content.lower()


# ── Shared calendars + free/busy interpretation ──────────────────


class _RouteRecorder:
    """`safe_request` stand-in returning a payload chosen by URL substring.

    Routes are matched in order, so put the most specific substring first.
    """

    def __init__(
        self,
        routes: list[tuple[str, dict[str, Any]]],
        default: dict[str, Any] | None = None,
    ):
        self.routes = routes
        self.default = default or {"value": []}
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, provider, op, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "provider": provider, "op": op, **kwargs}
        )
        for sub, payload in self.routes:
            if sub in url:
                return _StubResponse(payload), None
        return _StubResponse(self.default), None


async def _run_named_tool(monkeypatch, name, recorder, **arguments):
    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="call-1", name=name, arguments=arguments)
    )
    return recorder, result


def test_new_calendar_tools_are_registered():
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    names = {t.name for t in tools}
    assert "list_microsoft_calendars" in names
    assert "get_microsoft_free_busy" in names


def test_events_schema_exposes_calendar_id():
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    definition = _calendar_tool_definition(tools)
    assert "calendar_id" in definition.input_schema["properties"]
    # calendar_id stays optional so the default-calendar behaviour is preserved.
    assert "calendar_id" not in definition.input_schema["required"]


@pytest.mark.asyncio
async def test_all_day_busy_event_is_marked_busy(monkeypatch):
    """A full-day blocker (all-day event, showAs=busy) must read as busy and
    show a single inclusive date - Graph's all-day end is exclusive."""
    payload = {
        "value": [
            {
                "subject": "Full-day blocker",
                "start": {"dateTime": "2026-06-11T00:00:00.0000000"},
                "end": {"dateTime": "2026-06-12T00:00:00.0000000"},
                "isAllDay": True,
                "showAs": "busy",
            }
        ]
    }
    recorder = _RouteRecorder([("calendarView", payload)])
    _, result = await _run_named_tool(
        monkeypatch,
        "list_microsoft_calendar_events",
        recorder,
        start_datetime="2026-06-10T00:00:00Z",
        end_datetime="2026-06-13T00:00:00Z",
    )
    assert not result.is_error
    assert "[Busy]" in result.content
    assert "(all day)" in result.content
    assert "2026-06-11" in result.content
    # The exclusive end date must NOT leak into the inclusive display.
    assert "2026-06-12" not in result.content


@pytest.mark.asyncio
async def test_timed_event_surfaces_show_as(monkeypatch):
    payload = {
        "value": [
            {
                "subject": "Focus time",
                "start": {"dateTime": "2026-06-11T09:00:00Z"},
                "end": {"dateTime": "2026-06-11T10:00:00Z"},
                "showAs": "tentative",
            }
        ]
    }
    recorder = _RouteRecorder([("calendarView", payload)])
    _, result = await _run_named_tool(
        monkeypatch,
        "list_microsoft_calendar_events",
        recorder,
        start_datetime="2026-06-11T00:00:00Z",
        end_datetime="2026-06-12T00:00:00Z",
    )
    assert "[Tentative]" in result.content


@pytest.mark.asyncio
async def test_calendar_id_targets_shared_calendar(monkeypatch):
    """A calendar_id routes the request to /me/calendars/{id}/... and the id
    is percent-encoded so special chars never break the URL path."""
    recorder = _RouteRecorder([])
    _, result = await _run_named_tool(
        monkeypatch,
        "list_microsoft_calendar_events",
        recorder,
        calendar_id="ABC/123==",
        start_datetime="2026-06-10T00:00:00Z",
        end_datetime="2026-06-13T00:00:00Z",
    )
    assert not result.is_error
    url = recorder.calls[0]["url"]
    assert "/me/calendars/" in url
    assert url.endswith("/calendarView")
    assert "ABC%2F123%3D%3D" in url


@pytest.mark.asyncio
async def test_list_microsoft_calendars_reports_shared_calendar(monkeypatch):
    payload = {
        "value": [
            {
                "id": "cal-1",
                "name": "Calendar",
                "isDefaultCalendar": True,
                "canEdit": True,
                "owner": {"address": "me@example.com"},
            },
            {
                "id": "cal-2",
                "name": "Julien (shared)",
                "isDefaultCalendar": False,
                "canEdit": False,
                "owner": {"address": "julien@filigran.io"},
            },
        ]
    }
    recorder = _RouteRecorder([("/me/calendars", payload)])
    _, result = await _run_named_tool(
        monkeypatch, "list_microsoft_calendars", recorder
    )
    assert not result.is_error
    assert recorder.calls[0]["url"].endswith("/me/calendars")
    assert "Julien (shared)" in result.content
    assert "read-only" in result.content
    assert "julien@filigran.io" in result.content
    assert "cal-2" in result.content


@pytest.mark.asyncio
async def test_free_busy_posts_get_schedule_for_attendees(monkeypatch):
    payload = {
        "value": [
            {
                "scheduleId": "julien@filigran.io",
                "scheduleItems": [
                    {
                        "status": "busy",
                        "start": {"dateTime": "2026-06-12T09:00:00.0000000"},
                        "end": {"dateTime": "2026-06-12T10:00:00.0000000"},
                        "subject": "1:1",
                    }
                ],
                "availabilityView": "0220",
            }
        ]
    }
    recorder = _RouteRecorder([("getSchedule", payload)])
    _, result = await _run_named_tool(
        monkeypatch,
        "get_microsoft_free_busy",
        recorder,
        attendees="julien@filigran.io",
        start_datetime="2026-06-12T00:00:00Z",
        end_datetime="2026-06-13T00:00:00Z",
    )
    assert not result.is_error
    # Explicit attendees -> no /me lookup, single getSchedule POST.
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert "getSchedule" in call["url"]
    assert call["json"]["schedules"] == ["julien@filigran.io"]
    assert "Busy" in result.content
    assert "1:1" in result.content


@pytest.mark.asyncio
async def test_free_busy_defaults_to_self_via_me_lookup(monkeypatch):
    """With no attendees the tool resolves the signed-in user's address first,
    then queries getSchedule for that address."""
    recorder = _RouteRecorder(
        [
            (
                "getSchedule",
                {
                    "value": [
                        {
                            "scheduleId": "me@example.com",
                            "scheduleItems": [],
                            "availabilityView": "0000",
                        }
                    ]
                },
            ),
            ("/me", {"mail": "me@example.com"}),
        ]
    )
    _, result = await _run_named_tool(
        monkeypatch, "get_microsoft_free_busy", recorder
    )
    assert not result.is_error
    assert any(c["url"].endswith("/me") for c in recorder.calls)
    sched_call = next(c for c in recorder.calls if "getSchedule" in c["url"])
    assert sched_call["json"]["schedules"] == ["me@example.com"]
    assert "Free for the whole window" in result.content


@pytest.mark.asyncio
async def test_calendar_events_follow_pagination(monkeypatch):
    """A multi-page calendarView (``@odata.nextLink``) is fully walked so events
    on a later page aren't silently dropped, and the follow-up request targets
    the nextLink URL verbatim."""
    from app.integrations.providers import microsoft as ms

    pages = [
        {
            "value": [
                {
                    "subject": "Morning standup",
                    "start": {"dateTime": "2026-06-11T09:00:00Z"},
                    "end": {"dateTime": "2026-06-11T09:15:00Z"},
                    "showAs": "busy",
                }
            ],
            "@odata.nextLink": (
                "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=p2"
            ),
        },
        {
            "value": [
                {
                    "subject": "Afternoon review",
                    "start": {"dateTime": "2026-06-11T15:00:00Z"},
                    "end": {"dateTime": "2026-06-11T16:00:00Z"},
                    "showAs": "busy",
                }
            ]
        },
    ]
    calls: list[str] = []

    async def _recorder(client, method, url, provider, op, **kwargs):
        calls.append(url)
        payload = pages[min(len(calls) - 1, len(pages) - 1)]
        return _StubResponse(payload), None

    monkeypatch.setattr(ms._common, "safe_request", _recorder)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="list_microsoft_calendar_events",
            arguments={
                "start_datetime": "2026-06-11T00:00:00Z",
                "end_datetime": "2026-06-12T00:00:00Z",
            },
        )
    )
    assert not result.is_error
    # Both pages were fetched; the second request used the nextLink URL.
    assert len(calls) == 2
    assert "$skiptoken=p2" in calls[1]
    assert "Morning standup" in result.content
    assert "Afternoon review" in result.content


@pytest.mark.asyncio
async def test_calendar_events_render_event_id(monkeypatch):
    """The listing must select and surface each event's id so it can be fed
    back into get/update/delete_microsoft_calendar_event."""
    payload = {
        "value": [
            {
                "id": "AAMkAGI2=",
                "subject": "Sync",
                "start": {"dateTime": "2026-06-11T09:00:00Z"},
                "end": {"dateTime": "2026-06-11T09:30:00Z"},
                "showAs": "busy",
            }
        ]
    }
    recorder = _RouteRecorder([("calendarView", payload)])
    _, result = await _run_named_tool(
        monkeypatch,
        "list_microsoft_calendar_events",
        recorder,
        start_datetime="2026-06-11T00:00:00Z",
        end_datetime="2026-06-12T00:00:00Z",
    )
    assert not result.is_error
    assert "id: AAMkAGI2=" in result.content
    # The listing must explicitly request the id field from Graph.
    assert "id" in recorder.calls[0]["params"]["$select"].split(",")


@pytest.mark.asyncio
async def test_list_microsoft_calendars_follows_pagination(monkeypatch):
    """list_microsoft_calendars must walk @odata.nextLink so a shared calendar
    on a later page is not silently omitted."""
    from app.integrations.providers import microsoft as ms

    pages = [
        {
            "value": [
                {"id": "cal-1", "name": "Calendar", "canEdit": True},
            ],
            "@odata.nextLink": (
                "https://graph.microsoft.com/v1.0/me/calendars?$skiptoken=p2"
            ),
        },
        {
            "value": [
                {"id": "cal-2", "name": "Julien (shared)", "canEdit": False},
            ]
        },
    ]
    calls: list[str] = []

    async def _recorder(client, method, url, provider, op, **kwargs):
        calls.append(url)
        payload = pages[min(len(calls) - 1, len(pages) - 1)]
        return _StubResponse(payload), None

    monkeypatch.setattr(ms._common, "safe_request", _recorder)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="call-1", name="list_microsoft_calendars", arguments={})
    )
    assert not result.is_error
    assert len(calls) == 2
    assert "$skiptoken=p2" in calls[1]
    assert "Calendar" in result.content
    assert "Julien (shared)" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_interval", [1, 7, 0, 1441])
async def test_free_busy_rejects_invalid_interval(monkeypatch, bad_interval):
    """`interval_minutes` must be a multiple of 5 in [5, 1440]; an invalid value
    must return a tool-readable error and never reach Graph (which would 400)."""
    recorder = _RouteRecorder([])
    _, result = await _run_named_tool(
        monkeypatch,
        "get_microsoft_free_busy",
        recorder,
        attendees="julien@filigran.io",
        start_datetime="2026-06-12T00:00:00Z",
        end_datetime="2026-06-13T00:00:00Z",
        interval_minutes=bad_interval,
    )
    assert "interval_minutes" in result.content
    # The doomed Graph call is never made.
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_event_encodes_calendar_and_event_ids(monkeypatch):
    """`get_microsoft_calendar_event` routes to /me/calendars/{id}/events/{id}
    with BOTH the calendar id and the event id percent-encoded."""
    recorder = _RouteRecorder([])
    _, result = await _run_named_tool(
        monkeypatch,
        "get_microsoft_calendar_event",
        recorder,
        event_id="AAMk/abc==",
        calendar_id="ABC/123==",
    )
    assert not result.is_error
    assert (
        "/me/calendars/ABC%2F123%3D%3D/events/AAMk%2Fabc%3D%3D"
        in recorder.calls[0]["url"]
    )


@pytest.mark.asyncio
async def test_update_event_encodes_event_id_on_default_calendar(monkeypatch):
    """With no `calendar_id`, the event tools keep the historical
    /me/events/{id} path - but the event id is still percent-encoded."""
    recorder = _RouteRecorder([])
    _, result = await _run_named_tool(
        monkeypatch,
        "update_microsoft_calendar_event",
        recorder,
        event_id="AAMk/abc==",
        subject="New title",
    )
    assert not result.is_error
    assert recorder.calls[0]["method"] == "patch"
    assert recorder.calls[0]["url"].endswith("/me/events/AAMk%2Fabc%3D%3D")


@pytest.mark.asyncio
async def test_delete_event_routes_to_shared_calendar(monkeypatch):
    recorder = _RouteRecorder([])
    _, result = await _run_named_tool(
        monkeypatch,
        "delete_microsoft_calendar_event",
        recorder,
        event_id="AAMk/abc==",
        calendar_id="ABC/123==",
    )
    assert not result.is_error
    # The event is read first (organizer? attendees?), then deleted - both on
    # the shared calendar's path.
    assert [c["method"] for c in recorder.calls] == ["get", "delete"]
    for call in recorder.calls:
        assert "/me/calendars/ABC%2F123%3D%3D/events/AAMk%2Fabc%3D%3D" in call["url"]


@pytest.mark.asyncio
async def test_create_event_calendar_id_routing(monkeypatch):
    """`create_microsoft_calendar_event` posts to the calendar-scoped path when
    `calendar_id` is given (encoded), and to /me/events when it is omitted."""
    recorder = _RouteRecorder([])
    _, scoped = await _run_named_tool(
        monkeypatch,
        "create_microsoft_calendar_event",
        recorder,
        subject="Sync",
        start_datetime="2026-06-12T09:00:00Z",
        end_datetime="2026-06-12T10:00:00Z",
        calendar_id="ABC/123==",
    )
    assert not scoped.is_error
    assert recorder.calls[0]["method"] == "post"
    assert recorder.calls[0]["url"].endswith("/me/calendars/ABC%2F123%3D%3D/events")

    default_recorder = _RouteRecorder([])
    _, default = await _run_named_tool(
        monkeypatch,
        "create_microsoft_calendar_event",
        default_recorder,
        subject="Sync",
        start_datetime="2026-06-12T09:00:00Z",
        end_datetime="2026-06-12T10:00:00Z",
    )
    assert not default.is_error
    assert default_recorder.calls[0]["url"].endswith("/me/events")
