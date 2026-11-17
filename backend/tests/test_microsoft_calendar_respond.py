"""Outlook calendar: answering a meeting invitation the user received.

The tool runs for real against a fake Microsoft Graph served through
``httpx.MockTransport`` (documented response shapes, no network): the
requests, their paths and their bodies are the ones production sends.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.api.v1.integration_catalog import build_tools_for_provider
from app.integrations.providers import microsoft as ms
from app.services.integration_read_only import integration_tool_access, withhold_write_tools
from app.services.tool_classification import classify_tool_name
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

TOOL = "respond_microsoft_calendar_event"
LINK = "https://outlook.office365.com/owa/?itemid=EV1&exvsurl=1&path=/calendar/item"
PROPOSAL = {
    "start_datetime": "2026-10-30T11:00:00",
    "end_datetime": "2026-10-30T11:30:00",
    "timezone": "Romance Standard Time",
}


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
    """The read-back retries run without sleeping."""
    monkeypatch.setattr(ms.calendar, "_RESPONSE_READ_BACK_DELAYS", (0, 0))


def _json(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


async def _call(name: str, executor: CallableToolExecutor | None = None, **arguments: Any) -> ToolResult:
    if executor is None:
        executor = CallableToolExecutor()
        ms.build_tools("tok", executor)
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


class Invitation:
    """An invitation in the user's calendar: read, answered (Graph records the response), read again."""

    _STATUS = {"accept": "accepted", "tentativelyAccept": "tentativelyAccepted", "decline": "declined"}

    def __init__(self, graph: FakeGraph, path: str = "/v1.0/me/events/EV1", **fields: Any) -> None:
        self.event = {
            "id": path.rsplit("/", 1)[-1],
            "subject": "Quarterly review",
            "start": {"dateTime": "2026-10-30T10:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-30T10:30:00.0000000", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"name": "Alice Martin", "address": "alice@example.com"}},
            "isOrganizer": False,
            "responseStatus": {"response": "notResponded", "time": "0001-01-01T00:00:00Z"},
            "responseRequested": True,
            "allowNewTimeProposals": True,
            "isCancelled": False,
            "type": "singleInstance",
            "webLink": LINK,
            **fields,
        }
        self.bodies: dict[str, dict] = {}
        graph.on("GET", path, lambda _r: httpx.Response(200, json=self.event))
        for action, status in self._STATUS.items():
            graph.on("POST", f"{path}/{action}", self._answer(action, status))

    def _answer(self, action: str, status: str) -> Callable[[httpx.Request], httpx.Response]:
        def answer(request: httpx.Request) -> httpx.Response:
            self.bodies[action] = json.loads(request.content)
            self.event = {**self.event, "responseStatus": {"response": status, "time": "2026-10-29T18:00:00Z"}}
            return httpx.Response(202)

        return answer


# ── The three responses ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "action", "status", "done"),
    [
        ("accept", "accept", "accepted", "accepted"),
        ("tentatively_accept", "tentativelyAccept", "tentativelyAccepted", "tentatively accepted"),
        ("decline", "decline", "declined", "declined"),
    ],
)
async def test_each_response_posts_its_action_and_reads_the_status_back(graph, response, action, status, done):
    invitation = Invitation(graph)

    result = await _call(TOOL, event_id="EV1", response=response, comment="Confirmed, thank you")

    assert not result.is_error, result.content
    # The action's parameters as Graph's metadata names them (the Graph SDKs send them so).
    assert invitation.bodies == {action: {"SendResponse": True, "Comment": "Confirmed, thank you"}}
    assert [(r.method, r.url.path) for r in graph.requests] == [
        ("GET", "/v1.0/me/events/EV1"),
        ("POST", f"/v1.0/me/events/EV1/{action}"),
        ("GET", "/v1.0/me/events/EV1"),
    ]
    assert result.content.splitlines()[:5] == [
        f"Invitation {done}: 'Quarterly review' (ID: EV1)",
        "Start: 2026-10-30T10:00:00.0000000 (UTC)",
        "End: 2026-10-30T10:30:00.0000000 (UTC)",
        f"Your response (read back from Microsoft Graph): {status} (before: notResponded)",
        "Response sent to the organizer (Alice Martin <alice@example.com>): yes, with your comment",
    ]
    assert f"Link: {LINK}" in result.content


@pytest.mark.asyncio
async def test_send_response_false_records_the_answer_without_notifying_the_organizer(graph):
    invitation = Invitation(graph)

    result = await _call(TOOL, event_id="EV1", response="accept", send_response="false")

    assert not result.is_error, result.content
    assert invitation.bodies == {"accept": {"SendResponse": False}}
    assert "Response sent to the organizer: no (send_response=false)" in result.content


@pytest.mark.asyncio
async def test_a_decline_sends_the_proposed_new_time(graph):
    invitation = Invitation(graph)

    result = await _call(
        TOOL, event_id="EV1", response="decline", comment="Can we move it?", proposed_new_time=PROPOSAL
    )

    assert not result.is_error, result.content
    assert invitation.bodies["decline"] == {
        "SendResponse": True,
        "Comment": "Can we move it?",
        "ProposedNewTime": {
            "start": {"dateTime": "2026-10-30T11:00:00", "timeZone": "Romance Standard Time"},
            "end": {"dateTime": "2026-10-30T11:30:00", "timeZone": "Romance Standard Time"},
        },
    }
    assert "yes, with your comment, proposing the new time" in result.content
    assert "Proposed new time: 2026-10-30T11:00:00 - 2026-10-30T11:30:00 (Romance Standard Time)" in result.content


@pytest.mark.asyncio
async def test_a_tentative_acceptance_takes_the_proposal_as_json_in_utc_by_default(graph):
    invitation = Invitation(graph)
    proposal = json.dumps({"start_datetime": "2026-10-30T11:00:00", "end_datetime": "2026-10-30T11:30:00"})

    result = await _call(TOOL, event_id="EV1", response="tentatively_accept", proposed_new_time=proposal)

    assert not result.is_error, result.content
    assert invitation.bodies["tentativelyAccept"]["ProposedNewTime"] == {
        "start": {"dateTime": "2026-10-30T11:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-30T11:30:00", "timeZone": "UTC"},
    }


# ── Occurrences, series and shared calendars ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "note"),
    [
        ("occurrence", "Only this occurrence was answered; the rest of the series is untouched."),
        ("seriesMaster", "The whole recurring series was answered, every occurrence included."),
    ],
)
async def test_an_occurrence_or_a_series_is_answered_as_such(graph, event_type, note):
    invitation = Invitation(graph, path="/v1.0/me/events/OCC2", type=event_type, seriesMasterId="SERIES1")

    result = await _call(TOOL, event_id="OCC2", response="accept")

    assert not result.is_error, result.content
    assert list(invitation.bodies) == ["accept"]
    assert graph.sent("POST", "/v1.0/me/events/OCC2/accept")
    assert result.content.splitlines()[-1] == note


@pytest.mark.asyncio
async def test_an_invitation_on_a_shared_calendar_is_answered_there(graph):
    invitation = Invitation(graph, path="/v1.0/me/calendars/CAL1/events/EV1")

    result = await _call(TOOL, event_id="EV1", response="accept", calendar_id="CAL1")

    assert not result.is_error, result.content
    assert list(invitation.bodies) == ["accept"]
    assert {r.url.path for r in graph.requests} == {
        "/v1.0/me/calendars/CAL1/events/EV1",
        "/v1.0/me/calendars/CAL1/events/EV1/accept",
    }


# ── Refusals ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_event_the_user_organizes_is_refused_and_nothing_is_sent(graph):
    Invitation(graph, isOrganizer=True, responseStatus={"response": "organizer"})

    result = await _call(TOOL, event_id="EV1", response="accept")

    assert result.is_error
    assert (
        "you organize 'Quarterly review' (ID: EV1), so it holds no invitation to respond to; nothing was changed"
        in result.content
    )
    assert "update_microsoft_calendar_event" in result.content
    assert [r.method for r in graph.requests] == ["GET"]


@pytest.mark.asyncio
async def test_a_refusal_from_graph_carries_its_message(graph):
    Invitation(graph)
    graph.on(
        "POST",
        "/v1.0/me/events/EV1/accept",
        _json(
            {
                "error": {
                    "code": "ErrorInvalidRequest",
                    "message": "Your request can't be completed. You can't respond to this meeting.",
                }
            },
            status=400,
        ),
    )

    result = await _call(TOOL, event_id="EV1", response="accept")

    assert result.is_error
    assert "You can't respond to this meeting." in result.content
    assert [r.method for r in graph.requests] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_a_cancelled_meeting_holds_no_invitation(graph):
    Invitation(graph, isCancelled=True)

    result = await _call(TOOL, event_id="EV1", response="decline")

    assert result.is_error and "the organizer cancelled 'Quarterly review'" in result.content
    assert "delete_microsoft_calendar_event" in result.content
    assert [r.method for r in graph.requests] == ["GET"]


@pytest.mark.asyncio
async def test_a_proposal_the_organizer_does_not_allow_is_not_sent(graph):
    Invitation(graph, allowNewTimeProposals=False)

    result = await _call(TOOL, event_id="EV1", response="decline", proposed_new_time=PROPOSAL)

    assert result.is_error and "does not allow new time proposals" in result.content
    assert [r.method for r in graph.requests] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"response": "maybe"}, "response must be accept, tentatively_accept or decline"),
        ({"response": "accept", "send_response": "sometimes"}, "send_response must be true or false"),
        ({"response": "accept", "proposed_new_time": PROPOSAL}, "goes with tentatively_accept or decline"),
        ({"response": "decline", "send_response": False, "proposed_new_time": PROPOSAL}, "needs send_response=true"),
        (
            {"response": "decline", "proposed_new_time": {**PROPOSAL, "end_datetime": "2026-10-30T10:00:00"}},
            "end_datetime must be after start_datetime",
        ),
        (
            {
                "response": "decline",
                "proposed_new_time": {"start": "2026-10-30T11:00:00", "end": "2026-10-30T11:30:00"},
            },
            "not end, start",
        ),
        ({"response": "decline", "proposed_new_time": "tomorrow at 11"}, "proposed_new_time is an object"),
        ({"response": "accept", "event_id": " "}, "event_id is required"),
    ],
)
async def test_an_unreadable_request_is_refused_before_any_call(graph, arguments, message):
    result = await _call(TOOL, **{"event_id": "EV1", **arguments})

    assert result.is_error and message in result.content
    assert graph.requests == []


# ── The read-back ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_read_back_matches_a_fresh_get(graph):
    Invitation(graph)

    answered = await _call(TOOL, event_id="EV1", response="accept")
    fresh = (await _call("get_microsoft_calendar_event", event_id="EV1")).content.splitlines()

    for label in ("Start:", "End:", "Link:"):
        assert next(line for line in answered.content.splitlines() if line.startswith(label)) in fresh


@pytest.mark.asyncio
async def test_a_response_graph_does_not_report_is_not_confirmed(graph):
    Invitation(graph)
    graph.on("POST", "/v1.0/me/events/EV1/accept", httpx.Response(202))

    result = await _call(TOOL, event_id="EV1", response="accept")

    assert result.is_error
    assert "reads back with your response as 'notResponded', so it is not confirmed" in result.content
    # The invitation read, then the first read-back and one more per wait.
    assert len(graph.sent("GET", "/v1.0/me/events/EV1")) == 2 + len(ms.calendar._RESPONSE_READ_BACK_DELAYS)


def _gone_after(graph: FakeGraph, invitation: Invitation, action: str) -> None:
    """Once *action* is answered, the event is no longer found on the calendar."""

    def answer(request: httpx.Request) -> httpx.Response:
        invitation.bodies[action] = json.loads(request.content)
        graph.on("GET", "/v1.0/me/events/EV1", _json({"error": {"code": "ErrorItemNotFound"}}, status=404))
        return httpx.Response(202)

    graph.on("POST", f"/v1.0/me/events/EV1/{action}", answer)


@pytest.mark.asyncio
async def test_a_declined_meeting_taken_off_the_calendar_is_reported_as_read_before(graph):
    invitation = Invitation(graph)
    _gone_after(graph, invitation, "decline")

    result = await _call(TOOL, event_id="EV1", response="decline")

    assert not result.is_error, result.content
    assert list(invitation.bodies) == ["decline"]
    assert result.content.splitlines()[:3] == [
        "Invitation declined: 'Quarterly review' (ID: EV1)",
        "Start: 2026-10-30T10:00:00.0000000 (UTC)",
        "End: 2026-10-30T10:30:00.0000000 (UTC)",
    ]
    assert "no longer finds the event on this calendar" in result.content


@pytest.mark.asyncio
async def test_an_accepted_event_that_cannot_be_read_back_is_not_confirmed(graph):
    invitation = Invitation(graph)
    _gone_after(graph, invitation, "accept")

    result = await _call(TOOL, event_id="EV1", response="accept")

    assert result.is_error
    assert list(invitation.bodies) == ["accept"]
    assert "could not be read back, so it is not confirmed" in result.content


# ── Its place among the tools ─────────────────────────────────────────────


def test_the_tool_says_it_answers_a_received_invitation():
    tool = {t.name: t for t in ms.build_tools("tok", CallableToolExecutor())}[TOOL]

    assert "RECEIVED" in tool.description and "never creates or edits" in tool.description
    assert "update_microsoft_calendar_event" in tool.description
    for response in ("accept", "tentatively_accept", "decline"):
        assert response in tool.description
    schema = tool.input_schema
    assert schema["required"] == ["event_id", "response"]
    assert schema["properties"]["response"]["enum"] == ["accept", "tentatively_accept", "decline"]
    assert schema["properties"]["send_response"]["default"] is True
    assert schema["properties"]["proposed_new_time"]["required"] == ["start_datetime", "end_datetime"]


def test_the_tool_is_a_write_by_its_name_and_listed_as_one():
    assert classify_tool_name(TOOL) == ("write", "write_token")
    assert integration_tool_access(TOOL) == "write"
    listed = {tool.name: tool.tool_type for tool in build_tools_for_provider("microsoft")}
    assert listed[TOOL] == "write"


@pytest.mark.asyncio
async def test_a_read_only_binding_withholds_and_refuses_it(graph):
    executor = CallableToolExecutor()
    kept = {
        d.name for d in withhold_write_tools(ms.build_tools("tok", executor), executor, integration_label="Microsoft")
    }

    assert TOOL not in kept and "get_microsoft_calendar_event" in kept
    result = await _call(TOOL, executor, event_id="EV1", response="accept")
    assert result.is_error and result.content.startswith("Refused:")
    assert graph.requests == []
