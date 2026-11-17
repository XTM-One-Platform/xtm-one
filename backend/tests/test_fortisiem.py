"""Tests for the Fortinet FortiSIEM integration provider.

Covers tool introspection, the ``org/user`` HTTP Basic login construction, the
epoch-milliseconds timestamp rendering, integer code -> label mappings, the
event-query XML builder (user input must be XML-escaped), the XML results
parser, and the watchlist HTTP-200-but-Failed envelope handling. API calls
route through the shared ``safe_request`` helper, monkeypatched here so no
network is touched.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers import fortisiem as f
from app.integrations.providers.fortisiem import (
    _cell,
    _parse_event_results,
    _ts,
    build_event_query_xml,
    build_fortisiem_headers,
    build_tools,
    fortisiem_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://fortisiem.example.com||super||apiuser||s3cret"


class _StubResponse:
    def __init__(self, payload: Any, text: str = ""):
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that records calls and returns a fixed payload."""

    def __init__(self, payload: Any, text: str = ""):
        self._payload = payload
        self._text = text
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._payload, self._text), None


class _SeqRecorder:
    """Fake ``safe_request`` returning a different response per call."""

    def __init__(self, responses: list[_StubResponse]):
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)], None


def _build(
    monkeypatch, payload: Any, text: str = ""
) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(payload, text)
    monkeypatch.setattr(f, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_16_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    names = {t.name for t in tools}
    assert len(tools) == 16
    assert "list_fortisiem_incidents" in names
    assert "run_fortisiem_event_query" in names
    assert "add_fortisiem_watchlist_entries" in names
    assert "manage_fortisiem_watchlist" in names


def test_build_tools_empty_without_base_url():
    executor = CallableToolExecutor()
    assert build_tools(token="", executor=executor) == []
    assert build_tools(token="||super||user||pass", executor=executor) == []


def test_build_tools_with_introspection_dummy():
    # The catalog introspects with the 2-part URL dummy; tools must still build.
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=executor
    )
    assert len(tools) == 16


# ---------------------------------------------------------------------------
# Auth + URL helpers
# ---------------------------------------------------------------------------


def test_fortisiem_api_base():
    assert (
        fortisiem_api_base("https://fsm.example.com/")
        == "https://fsm.example.com/phoenix/rest"
    )


def test_basic_login_includes_organization():
    headers = build_fortisiem_headers("super", "apiuser", "s3cret")
    decoded = base64.b64decode(
        headers["Authorization"].removeprefix("Basic ")
    ).decode()
    assert decoded == "super/apiuser:s3cret"


def test_basic_login_without_organization():
    headers = build_fortisiem_headers("", " apiuser ", " s3cret\n")
    decoded = base64.b64decode(
        headers["Authorization"].removeprefix("Basic ")
    ).decode()
    # No org -> bare username; credentials are stripped.
    assert decoded == "apiuser:s3cret"


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def test_ts_renders_epoch_millis_as_utc():
    # 2023-11-14T22:13:20Z
    assert _ts(1700000000000) == "2023-11-14T22:13:20Z"
    assert _ts(0) == ""
    assert _ts(None) == ""
    assert _ts("garbage") == "garbage"


def test_cell_neutralizes_pipes_and_whitespace():
    # Raw log messages (CEF, FortiGate key-value) routinely contain literal
    # pipes that would break the pipe-delimited results table.
    assert _cell("CEF:0|Fortinet|FortiGate") == "CEF:0¦Fortinet¦FortiGate"
    assert _cell("a\r\nb\tc") == "a  b c"
    assert _cell(None) == "null"


# ---------------------------------------------------------------------------
# Event query XML (user input must be escaped)
# ---------------------------------------------------------------------------


def test_event_query_xml_escapes_constraint():
    xml_body = build_event_query_xml(
        'eventSeverity >= 9 AND destName = "<evil>"',
        "phRecvTime,eventType",
        1700000000,
        1700003600,
    )
    assert "&lt;evil&gt;" in xml_body
    assert "<evil>" not in xml_body
    assert "<Low>1700000000</Low>" in xml_body
    assert "<High>1700003600</High>" in xml_body
    assert "<AttrList>phRecvTime,eventType</AttrList>" in xml_body


def test_parse_event_results_renders_table():
    content = (
        '<queryResult totalCount="2"><events>'
        '<event><attributes>'
        '<attribute name="eventType">PH_LOGON_FAILURE</attribute>'
        '<attribute name="srcIpAddr">10.0.0.5</attribute>'
        "</attributes></event>"
        '<event><attributes>'
        '<attribute name="eventType">PH_LOGON_SUCCESS</attribute>'
        '<attribute name="srcIpAddr">10.0.0.6</attribute>'
        "</attributes></event>"
        "</events></queryResult>"
    )
    out = _parse_event_results(content, "eventType,srcIpAddr", 50)
    assert "totalCount 2" in out
    assert "PH_LOGON_FAILURE | 10.0.0.5" in out


def test_parse_event_results_handles_garbage():
    assert "unparseable" in _parse_event_results("not xml", "a,b", 10)


# ---------------------------------------------------------------------------
# Incidents — time window + filter mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_incidents_builds_filtered_payload(monkeypatch):
    executor, recorder = _build(monkeypatch, {"total": 0, "data": []})
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_fortisiem_incidents",
            arguments={"hours": 4, "status": "active", "category": "security"},
        )
    )
    call = recorder.calls[0]
    assert call["url"].endswith("/phoenix/rest/pub/incident")
    payload = call["json"]
    assert payload["filters"]["status"] == [0]
    assert payload["filters"]["phIncidentCategory"] == [4]
    assert payload["timeTo"] - payload["timeFrom"] == 4 * 3_600_000
    assert payload["orderBy"] == "incidentLastSeen"


@pytest.mark.asyncio
async def test_list_incidents_coerces_string_pagination_args(monkeypatch):
    # Tool args are not schema-coerced at execution time; strings must not
    # crash and garbage must degrade to the defaults.
    executor, recorder = _build(monkeypatch, {"total": 0, "data": []})
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_fortisiem_incidents",
            arguments={"start": "10", "limit": "5", "min_severity": "bogus"},
        )
    )
    payload = recorder.calls[0]["json"]
    assert payload["start"] == 10
    assert payload["size"] == 5


@pytest.mark.asyncio
async def test_list_incidents_min_severity_filters_client_side(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        {
            "total": 120,
            "data": [
                {"incidentId": 1, "eventSeverity": 9, "incidentTitle": "high"},
                {"incidentId": 2, "eventSeverity": 3, "incidentTitle": "low"},
                {"incidentId": 3, "eventSeverity": None, "incidentTitle": "none"},
            ],
        },
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_fortisiem_incidents",
            arguments={"min_severity": 5},
        )
    )
    content = str(result.content)
    assert "incident #1" in content
    assert "incident #2" not in content
    assert "incident #3" not in content
    # The paging hint reflects the SERVER-side total and says so.
    assert "server-side filters" in content
    assert "120 incidents match" in content


@pytest.mark.asyncio
async def test_list_incidents_rejects_unknown_status(monkeypatch):
    executor, recorder = _build(monkeypatch, {"total": 0, "data": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_fortisiem_incidents",
            arguments={"status": "closed"},
        )
    )
    assert "status must be one of" in str(result.content)
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Watchlists — the HTTP-200-but-Failed envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_failed_envelope_is_surfaced(monkeypatch):
    executor, _ = _build(
        monkeypatch, {"response": "Reason for failure", "status": "Failed"}
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_fortisiem_watchlist", arguments={"watchlist_id": 7})
    )
    assert "Reason for failure" in str(result.content)


@pytest.mark.asyncio
async def test_add_watchlist_entries_posts_entry_objects(monkeypatch):
    executor, recorder = _build(
        monkeypatch, {"response": "Successfully added", "status": "Success"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="add_fortisiem_watchlist_entries",
            arguments={"watchlist_id": 42, "values": ["10.0.0.5", "evil.test"]},
        )
    )
    call = recorder.calls[0]
    assert call["url"].endswith("/watchlist/addTo")
    assert call["params"] == {"watchlistId": 42}
    entries = call["json"]
    assert [e["entryValue"] for e in entries] == ["10.0.0.5", "evil.test"]
    assert all(e["dataCreationType"] == "USER" for e in entries)
    assert "2 entries" in str(result.content)


@pytest.mark.asyncio
async def test_get_incident_rejects_non_numeric_id(monkeypatch):
    executor, recorder = _build(monkeypatch, {})
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_fortisiem_incident",
            arguments={"incident_id": "not-a-number"},
        )
    )
    assert "must be a numeric FortiSIEM id" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_set_entry_state_coerces_string_false(monkeypatch):
    # bool("false") is True - the tool must parse string booleans so a
    # disable request is not silently turned into an enable.
    executor, recorder = _build(
        monkeypatch, {"response": {"id": 5, "state": "Disabled"}, "status": "Success"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="manage_fortisiem_watchlist",
            arguments={"action": "set_entry_state", "entry_id": 5, "state": "false"},
        )
    )
    assert recorder.calls[0]["params"] == {"state": "false"}
    assert "disabled" in str(result.content)


@pytest.mark.asyncio
async def test_manage_watchlist_rejects_unknown_action(monkeypatch):
    executor, recorder = _build(monkeypatch, {})
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="manage_fortisiem_watchlist",
            arguments={"action": "explode"},
        )
    )
    assert "action must be one of" in str(result.content)
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Event query flow (submit -> poll -> results) + query id validation
# ---------------------------------------------------------------------------

_RESULTS_XML = (
    '<queryResult totalCount="1"><events><event><attributes>'
    '<attribute name="eventType">PH_LOGON_FAILURE</attribute>'
    "</attributes></event></events></queryResult>"
)


@pytest.mark.asyncio
async def test_run_event_query_submits_polls_and_fetches(monkeypatch):
    # Real FortiSIEM query ids contain a comma (``<id>,<timestamp>``).
    recorder = _SeqRecorder(
        [
            _StubResponse(None, "10412,1753450073522"),  # submit -> queryId
            _StubResponse(None, "100"),  # progress -> complete
            _StubResponse(None, _RESULTS_XML),  # results
        ]
    )
    monkeypatch.setattr(f, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="run_fortisiem_event_query",
            arguments={"query": "eventSeverity >= 9", "limit": 10},
        )
    )
    urls = [c["url"] for c in recorder.calls]
    assert urls[0].endswith("/phoenix/rest/query/eventQuery")
    assert urls[1].endswith("/query/progress/10412,1753450073522")
    assert urls[2].endswith("/query/events/10412,1753450073522/0/10")
    assert recorder.calls[0]["headers"]["Content-Type"] == "text/xml"
    assert "PH_LOGON_FAILURE" in str(result.content)


@pytest.mark.asyncio
async def test_run_event_query_rejects_unsafe_server_query_id(monkeypatch):
    executor, recorder = _build(monkeypatch, None, text="../../etc/passwd")
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="run_fortisiem_event_query",
            arguments={"query": "eventSeverity >= 9"},
        )
    )
    assert "unexpected event query id" in str(result.content)
    assert len(recorder.calls) == 1  # submit only — never polled


@pytest.mark.asyncio
async def test_query_results_accepts_comma_query_id(monkeypatch):
    recorder = _SeqRecorder(
        [_StubResponse(None, "100"), _StubResponse(None, _RESULTS_XML)]
    )
    monkeypatch.setattr(f, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_fortisiem_event_query_results",
            arguments={"query_id": "10412,1753450073522"},
        )
    )
    assert recorder.calls[0]["url"].endswith("/query/progress/10412,1753450073522")
    assert "PH_LOGON_FAILURE" in str(result.content)


@pytest.mark.asyncio
async def test_query_results_rejects_malformed_query_id(monkeypatch):
    executor, recorder = _build(monkeypatch, {})
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="get_fortisiem_event_query_results",
            arguments={"query_id": "../../etc/passwd"},
        )
    )
    assert "query_id must be" in str(result.content)
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Connectivity status tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_watchlist_count(monkeypatch):
    executor, recorder = _build(
        monkeypatch, {"response": {"entry_count": 6}, "status": "Success"}
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_fortisiem_status", arguments={})
    )
    assert recorder.calls[0]["url"].endswith("/phoenix/rest/watchlist/cnt")
    content = str(result.content)
    assert "Connected to Fortinet FortiSIEM" in content
    assert "6" in content
