"""Tests for the CrowdStrike Next-Gen SIEM provider.

Covers the search time and name validation, the LogScale query-job lifecycle
(start, poll, stop on timeout, cancellation), the exact payloads of the
correlation-rule, detection and lookup-file calls, the connectivity probe, the
connection-test dispatch and the compound-token resolution.

The Falcon token exchange (``request_falcon_token``) and every API call
(``safe_request`` / ``fetch_file``) are monkeypatched: no network is touched.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools
from app.core.security import encrypt_value
from app.integrations.file_delivery import FetchedFile, FetchError
from app.integrations.http import parse_compound_token
from app.integrations.providers import crowdstrike_ngsiem as ng
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.eu-1.crowdstrike.com||client-id||client-secret"
_BASE = "https://api.eu-1.crowdstrike.com"


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` routing by (method, url) and recording each call."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        routed = self._router(method, url)
        if isinstance(routed, str):
            return None, routed
        return _StubResponse(routed), None


async def _token_ok(_base_url, _client_id, _client_secret):
    return "dummy-token", 1799


class _PollFetcher:
    """Fake ``fetch_file`` for the search polls, recording into the same list.

    The router answers a dict (the job, served as JSON bytes), a
    ``FetchError`` (refused), or ``bytes`` (a raw body).
    """

    def __init__(self, router, recorder: _Recorder):
        self._router = router
        self._recorder = recorder

    async def __call__(self, _client, method, url, **kwargs):
        self._recorder.calls.append({"method": method, "url": url, **kwargs})
        routed = self._router(method, url)
        if isinstance(routed, FetchError):
            return routed
        data = routed if isinstance(routed, bytes) else json.dumps(routed).encode()
        return FetchedFile(data=data, content_type="application/json", filename="")


def _build(monkeypatch, router, **kwargs) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(ng, "safe_request", recorder)
    monkeypatch.setattr(ng, "fetch_file", _PollFetcher(router, recorder))
    monkeypatch.setattr(ng, "request_falcon_token", _token_ok)
    executor = CallableToolExecutor()
    build_kwargs = {"token": _TOKEN, "executor": executor, **kwargs}
    ng.build_tools(**build_kwargs)
    return executor, recorder


async def _call(executor: CallableToolExecutor, tool: str, /, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


class _Clock:
    """A monotonic clock ``asyncio.sleep`` advances, so polling takes no time."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        import time as _time

        return _time.time()

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(ng, "time", fake)
    monkeypatch.setattr(ng.asyncio, "sleep", fake.sleep)
    return fake


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("24h", "24h"),
        ("15 MIN", "15m"),
        ("15mins", "15m"),
        ("2 hrs", "2h"),
        ("30secs", "30s"),
        ("500ms", "500ms"),
        ("3 weeks", "3w"),
        ("7d", "7d"),
        ("now", "now"),
        ("1700000000", 1_700_000_000_000),
        ("1700000000123", 1_700_000_000_123),
        ("2026-10-01T08:00:00Z", 1_790_841_600_000),
        ("2026-10-01T08:00:00", 1_790_841_600_000),
        ("2026-10-01T10:00:00+02:00", 1_790_841_600_000),
    ],
)
def test_resolve_time_accepts_relative_absolute_and_epoch(raw, expected):
    assert ng.resolve_time(raw, "start") == (expected, None)


@pytest.mark.parametrize("raw", ["yesterday", "12345", "24 parsecs", "2026-13-40"])
def test_resolve_time_refuses_unreadable_bounds(raw):
    value, error = ng.resolve_time(raw, "start")
    assert value is None
    assert error is not None and error.startswith("Error: start")


def test_resolve_time_empty_means_not_given():
    assert ng.resolve_time("", "end") == (None, None)


@pytest.mark.parametrize("name", ["search-all", "investigate_view", "third-party", "my_view2"])
def test_check_repository_accepts_plain_names(name):
    assert ng.check_repository(name) == (name, None)


def test_check_repository_defaults_to_search_all():
    assert ng.check_repository("  ") == ("search-all", None)


@pytest.mark.parametrize("name", ["../admin", "a/b", "a%2Fb", ".", "..", "a\\b", "-x", "a b"])
def test_check_repository_refuses_path_shaped_names(name):
    value, error = ng.check_repository(name)
    assert value is None
    assert error is not None and error.startswith("Error: repository")


@pytest.mark.parametrize("name", ["../x.csv", "a/b.csv", "a\\b.csv", "x..csv", "", ".hidden.csv"])
def test_check_lookup_filename_refuses_paths(name):
    value, error = ng.check_lookup_filename(name)
    assert value is None
    assert error is not None and error.startswith("Error: filename")


def test_render_event_puts_priority_fields_first_and_formats_timestamp():
    rendered = ng.render_event(
        {
            "zeta": 1,
            "@rawstring": "raw line",
            "ComputerName": "WIN-01",
            "@timestamp": 1_790_841_600_000,
            "#event_simpleName": "ProcessRollup2",
        }
    )
    assert rendered == (
        '{"@timestamp": "2026-10-01T08:00:00+00:00", "#event_simpleName": '
        '"ProcessRollup2", "ComputerName": "WIN-01", "zeta": 1, "@rawstring": "raw line"}'
    )


def test_render_event_is_bounded():
    rendered = ng.render_event({"@rawstring": "x" * 10_000})
    assert len(rendered) == ng._MAX_EVENT_CHARS + 1
    assert rendered.endswith("…")


def test_build_tools_with_dummy_token_lists_every_tool():
    tools = ng.build_tools(token="__dummy__", executor=CallableToolExecutor())
    assert [t.name for t in tools] == [
        "search_crowdstrike_ngsiem",
        "list_crowdstrike_ngsiem_correlation_rules",
        "get_crowdstrike_ngsiem_correlation_rule",
        "set_crowdstrike_ngsiem_correlation_rule_status",
        "list_crowdstrike_ngsiem_detections",
        "get_crowdstrike_ngsiem_detection",
        "list_crowdstrike_ngsiem_saved_queries",
        "get_crowdstrike_ngsiem_saved_query",
        "list_crowdstrike_ngsiem_lookup_files",
        "download_crowdstrike_ngsiem_lookup_file",
        "upload_crowdstrike_ngsiem_lookup_file",
    ]
    assert {t.source for t in tools} == {"integration:crowdstrike_ngsiem"}


def test_the_provider_is_registered_with_the_tool_dispatch():
    assert integration_tools.PROVIDER_TOOLS["crowdstrike_ngsiem"] is ng.build_tools
    assert "crowdstrike_ngsiem" in integration_tools._PERSIST_FILE_PROVIDERS


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_JOBS = f"{_BASE}/humio/api/v1/repositories/search-all/queryjobs"


@pytest.mark.asyncio
async def test_search_starts_polls_and_returns_wrapped_events(monkeypatch, clock):
    polls = iter(
        [
            {"done": False, "events": [], "metaData": {"pollAfter": 2000}},
            {
                "done": True,
                "events": [
                    {"@timestamp": 1_790_841_600_000, "ComputerName": "WIN-01"},
                    {"@timestamp": 1_790_841_601_000, "ComputerName": "WIN-02"},
                ],
                "metaData": {"eventCount": 2},
            },
        ]
    )

    def router(method, url):
        if method == "post":
            return {"id": "P1/job id"}
        return next(polls)

    executor, recorder = _build(monkeypatch, router)
    result = await _call(
        executor,
        "search_crowdstrike_ngsiem",
        query="#event_simpleName=ProcessRollup2",
        start="7d",
        end="1h",
    )

    assert not result.is_error
    start_call = recorder.calls[0]
    assert start_call["method"] == "post"
    assert start_call["url"] == _JOBS
    assert start_call["json"] == {
        "queryString": "#event_simpleName=ProcessRollup2",
        "start": "7d",
        "isLive": False,
        "end": "1h",
    }
    poll_urls = [c["url"] for c in recorder.calls if c["method"] == "get"]
    # The job id is a path segment: its slash and space are escaped.
    assert poll_urls == [f"{_JOBS}/P1%2Fjob%20id", f"{_JOBS}/P1%2Fjob%20id"]
    # pollAfter (2000 ms) is honoured between the two polls.
    assert clock.now == 1002.0
    # A finished job is not stopped.
    assert [c for c in recorder.calls if c["method"] == "delete"] == []
    assert "- complete." in result.content
    assert "matched: 2 | returned: 2" in result.content
    assert "Untrusted external content follows" in result.content
    assert '"ComputerName": "WIN-02"' in result.content


@pytest.mark.asyncio
async def test_search_that_outlives_its_timeout_is_stopped_and_marked_partial(monkeypatch, clock):
    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        if method == "delete":
            return {}
        return {
            "done": False,
            "events": [{"ComputerName": "WIN-01"}],
            "metaData": {"pollAfter": 5000, "workDone": 25, "totalWork": 100},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await _call(
        executor, "search_crowdstrike_ngsiem", query="*", timeout_seconds=12
    )

    assert not result.is_error
    deletes = [c for c in recorder.calls if c["method"] == "delete"]
    assert len(deletes) == 1
    assert deletes[0]["url"] == f"{_JOBS}/job1"
    assert "PARTIAL: it did not finish within 12 s, about 25% of the data searched" in result.content
    assert '"ComputerName": "WIN-01"' in result.content


@pytest.mark.asyncio
async def test_a_cancelled_search_is_an_error(monkeypatch, clock):
    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        return {"cancelled": True, "done": False}

    executor, _recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*")

    assert result.is_error
    assert "cancelled the search" in result.content


@pytest.mark.asyncio
async def test_search_reads_a_job_id_inside_the_falcon_envelope(monkeypatch, clock):
    def router(method, url):
        if method == "post":
            return {"resources": [{"id": "wrapped"}], "errors": []}
        return {"resources": {"done": True, "events": []}}

    executor, recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*")

    assert not result.is_error
    assert recorder.calls[1]["url"] == f"{_JOBS}/wrapped"
    assert "No events matched." in result.content


@pytest.mark.asyncio
async def test_search_shows_at_most_limit_events(monkeypatch, clock):
    events = [{"n": i} for i in range(30)]

    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        return {"done": True, "events": events}

    executor, _recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*", limit=3)

    assert "Showing 3 of 30 returned events:" in result.content
    assert "[27 more returned events not shown" in result.content
    assert '{"n": 3}' not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"query": ""}, "Error: query is required"),
        ({"query": "*", "repository": "../x"}, "Error: repository"),
        ({"query": "*", "start": "soon"}, "Error: start"),
        ({"query": "*", "start": "1h", "end": "2h"}, "Error: start must be earlier than end."),
        ({"query": "x" * 20_001}, "Error: query is over 20000 characters."),
    ],
)
async def test_search_refuses_bad_input_before_any_call(monkeypatch, clock, arguments, message):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await _call(executor, "search_crowdstrike_ngsiem", **arguments)

    assert result.is_error
    assert result.content.startswith(message)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_refused_search_start_is_returned_and_never_polled(monkeypatch, clock):
    def router(method, url):
        return "Error: CrowdStrike Next-Gen SIEM returned HTTP 400: syntax error"

    executor, recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="bad(")

    assert result.is_error
    assert "syntax error" in result.content
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_missing_credentials_never_reach_the_api(monkeypatch, clock):
    recorder = _Recorder(lambda m, u: {})
    monkeypatch.setattr(ng, "safe_request", recorder)

    async def _never(*_args):
        raise AssertionError("the token endpoint must not be called without credentials")

    monkeypatch.setattr(ng, "request_falcon_token", _never)
    executor = CallableToolExecutor()
    ng.build_tools(token=f"{_BASE}||||", executor=executor)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*")

    assert result.is_error
    assert result.content.startswith("Error: Failed to authenticate")
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_metadata_warnings_are_shown_on_a_complete_search(monkeypatch, clock):
    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        return {
            "done": True,
            "events": [{"ComputerName": "WIN-01"}],
            "metaData": {"warnings": ["Result buffer full"]},
            "warnings": ["Result buffer full"],
        }

    executor, _recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*")

    assert "- complete." in result.content
    # Read from metaData, and said once even when both places carry it.
    assert result.content.count("- Result buffer full") == 1


@pytest.mark.asyncio
async def test_metadata_warnings_are_shown_when_nothing_matched(monkeypatch, clock):
    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        return {"done": True, "events": [], "metaData": {"warnings": [{"message": "Slow regex"}]}}

    executor, _recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*")

    assert "No events matched." in result.content
    assert "- Slow regex" in result.content


@pytest.mark.asyncio
async def test_a_refused_poll_stops_the_job_and_returns_the_error(monkeypatch, clock):
    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        if method == "delete":
            return {}
        return FetchError("Error: CrowdStrike Next-Gen SIEM returned HTTP 403", status=403)

    executor, recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*")

    assert result.is_error
    assert "HTTP 403" in result.content
    assert [c["method"] for c in recorder.calls] == ["post", "get", "delete"]


@pytest.mark.asyncio
async def test_a_transient_poll_failure_is_polled_again(monkeypatch, clock):
    answers = iter(
        [
            FetchError("Error: HTTP 503", status=503),
            {"done": True, "events": [{"n": 1}]},
        ]
    )

    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        return next(answers)

    executor, recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*")

    assert not result.is_error
    assert "- complete." in result.content
    assert [c["method"] for c in recorder.calls] == ["post", "get", "get"]


@pytest.mark.asyncio
async def test_a_search_whose_every_poll_failed_says_why(monkeypatch, clock):
    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        if method == "delete":
            return {}
        return FetchError("Error: HTTP 502 from the gateway", status=502)

    executor, recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*", timeout_seconds=10)

    assert result.is_error
    assert result.content == "Error: HTTP 502 from the gateway"
    assert [c["method"] for c in recorder.calls if c["method"] == "delete"] == ["delete"]


@pytest.mark.asyncio
async def test_a_poll_that_outlasts_the_budget_keeps_the_last_snapshot(monkeypatch, clock):
    polls = 0

    async def _fetch(_client, method, url, **kwargs):
        nonlocal polls
        polls += 1
        if polls == 1:
            # The first poll uses all but two seconds of the 10 s budget...
            clock.now += 8.0
            body = {"done": False, "events": [{"n": 1}], "metaData": {"pollAfter": 500}}
            return FetchedFile(data=json.dumps(body).encode(), content_type="application/json", filename="")
        # ...and the second never answers: the deadline must cut it short.
        await asyncio.Event().wait()

    def router(method, url):
        return {"id": "job1"} if method == "post" else {}

    executor, recorder = _build(monkeypatch, router)
    monkeypatch.setattr(ng, "fetch_file", _fetch)
    result = await asyncio.wait_for(
        _call(executor, "search_crowdstrike_ngsiem", query="*", timeout_seconds=10), 5
    )

    assert not result.is_error
    assert polls == 2
    assert "PARTIAL: it did not finish within 10 s" in result.content
    assert '{"n": 1}' in result.content
    assert [c["method"] for c in recorder.calls] == ["post", "delete"]


@pytest.mark.asyncio
async def test_an_oversized_poll_is_refused_and_the_job_stopped(monkeypatch, clock):
    def router(method, url):
        if method == "post":
            return {"id": "job1"}
        if method == "delete":
            return {}
        return FetchError("Error: over the cap", too_large=True)

    executor, recorder = _build(monkeypatch, router)
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*")

    assert result.is_error
    assert result.content.startswith("Error: the search's results are over 10 MB.")
    poll = next(c for c in recorder.calls if c["method"] == "get")
    assert poll["max_bytes"] == 10 * 1024 * 1024
    assert [c["method"] for c in recorder.calls] == ["post", "get", "delete"]


@pytest.mark.asyncio
async def test_the_stop_call_is_bounded_and_accepts_a_gone_job(monkeypatch, clock):
    stop_calls: list[dict[str, Any]] = []

    async def _safe_request(_client, method, url, _provider, action, **kwargs):
        if method == "post":
            return _StubResponse({"id": "job1"}), None
        stop_calls.append(kwargs)
        await asyncio.Event().wait()  # a stop that never answers

    def router(method, url):
        return {"done": False, "events": [], "metaData": {"pollAfter": 5000}}

    executor, _recorder = _build(monkeypatch, router)
    monkeypatch.setattr(ng, "safe_request", _safe_request)
    monkeypatch.setattr(ng, "_STOP_TIMEOUT", 0.2)
    result = await asyncio.wait_for(
        _call(executor, "search_crowdstrike_ngsiem", query="*", timeout_seconds=10), 5
    )

    assert not result.is_error
    assert "PARTIAL" in result.content
    assert len(stop_calls) == 1
    assert stop_calls[0]["expected_statuses"] == {404}
    assert stop_calls[0]["max_total_seconds"] == 0.2


@pytest.mark.asyncio
async def test_a_search_start_that_hangs_is_cut_at_the_deadline(monkeypatch):
    async def _hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    executor, _recorder = _build(monkeypatch, lambda m, u: {})
    monkeypatch.setattr(ng, "safe_request", _hang)
    monkeypatch.setattr(ng, "_MIN_SEARCH_TIMEOUT", 1)
    result = await asyncio.wait_for(
        _call(executor, "search_crowdstrike_ngsiem", query="*", timeout_seconds=1), 5
    )

    assert result.is_error
    assert result.content.startswith("Error: CrowdStrike Next-Gen SIEM did not accept the search within 1 s.")


@pytest.mark.asyncio
async def test_an_iso_start_with_a_relative_end_is_accepted(monkeypatch, clock):
    def router(method, url):
        return {"id": "job1"} if method == "post" else {"done": True, "events": []}

    executor, recorder = _build(monkeypatch, router)
    start = datetime.fromtimestamp(time.time() - 2 * 86_400, tz=UTC).isoformat()
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*", start=start, end="1h")

    assert not result.is_error
    assert recorder.calls[0]["json"]["end"] == "1h"
    assert isinstance(recorder.calls[0]["json"]["start"], int)


@pytest.mark.asyncio
async def test_a_search_starting_now_is_refused(monkeypatch, clock):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await _call(executor, "search_crowdstrike_ngsiem", query="*", start="now")

    assert result.content == "Error: start must be earlier than end."
    assert recorder.calls == []


@pytest.mark.parametrize(
    "data",
    [
        {"meta": {"pagination": {"offset": 0}}},
        {"meta": {"pagination": {"total": "many"}}},
        {"meta": {"pagination": None}},
        {"meta": "x"},
        [],
    ],
)
def test_the_pagination_hint_is_silent_without_a_numeric_total(data):
    assert ng._pagination_hint(data, 0, 5) == ""


# ---------------------------------------------------------------------------
# Correlation rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_correlation_rules_builds_the_filter(monkeypatch):
    def router(method, url):
        return {
            "resources": [
                {
                    "id": "r1",
                    "name": "Impossible travel",
                    "status": "active",
                    "severity": 70,
                    "search": {"filter": "#repo=x", "lookback": "1h", "outcome": "detection"},
                    "operation": {"schedule": {"definition": "@every 1h"}},
                }
            ],
            "meta": {"pagination": {"offset": 0, "limit": 25, "total": 40}},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await _call(
        executor,
        "list_crowdstrike_ngsiem_correlation_rules",
        status="Active",
        name="o'brien",
        filter_query="severity:>50",
    )

    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"] == f"{_BASE}/correlation-rules/combined/rules/v1"
    assert call["params"] == {
        "sort": "last_updated_on|desc",
        "limit": 25,
        "offset": 0,
        "filter": "status:'active'+name:~'o\\'brien'+(severity:>50)",
    }
    assert "Impossible travel" in result.content
    assert "schedule: @every 1h" in result.content
    assert "[40 in all - pass offset=1 for the next page]" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"status": "paused"}, "Error: status must be 'active' or 'inactive'."),
        ({"sort": "name"}, "Error: sort must be one of"),
    ],
)
async def test_list_correlation_rules_refuses_unknown_values(monkeypatch, arguments, message):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await _call(executor, "list_crowdstrike_ngsiem_correlation_rules", **arguments)

    assert result.content.startswith(message)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_set_rule_status_patches_a_list_of_updates(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"resources": [{"id": "r1", "status": "inactive"}]}
    )
    result = await _call(
        executor,
        "set_crowdstrike_ngsiem_correlation_rule_status",
        rule_id="r1",
        status="inactive",
        comment="too noisy",
    )

    assert not result.is_error
    assert result.content == "Correlation rule r1 is now inactive."
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert call["url"] == f"{_BASE}/correlation-rules/entities/rules/v1"
    assert call["json"] == [{"id": "r1", "status": "inactive", "comment": "too noisy"}]
    # A write is never retried on a gateway error: it could apply twice.
    assert "retry_statuses" not in call


@pytest.mark.asyncio
async def test_set_rule_status_refuses_an_unknown_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await _call(
        executor, "set_crowdstrike_ngsiem_correlation_rule_status", rule_id="r1", status="off"
    )

    assert result.is_error
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_set_rule_status_reports_an_envelope_refusal(monkeypatch):
    executor, _recorder = _build(
        monkeypatch, lambda m, u: {"resources": [], "errors": [{"message": "rule locked"}]}
    )
    result = await _call(
        executor, "set_crowdstrike_ngsiem_correlation_rule_status", rule_id="r1", status="active"
    )

    assert result.is_error
    assert "rule locked" in result.content


# ---------------------------------------------------------------------------
# Detections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_detections_filters_on_the_ngsiem_product(monkeypatch):
    def router(method, url):
        if "queries/alerts" in url:
            return {"resources": ["ngsiem:1"]}
        return {
            "resources": [
                {
                    "composite_id": "ngsiem:1",
                    "display_name": "Brute force",
                    "severity_name": "High",
                    "status": "new",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await _call(
        executor, "list_crowdstrike_ngsiem_detections", status="new", severity="Hi'gh"
    )

    assert not result.is_error
    query_call = recorder.calls[0]
    assert query_call["params"] == {
        "filter": "product:'ngsiem'+status:'new'+severity_name:'Hi\\'gh'",
        "limit": 25,
        "sort": "created_timestamp|desc",
    }
    assert recorder.calls[1]["json"] == {"composite_ids": ["ngsiem:1"]}
    assert "Brute force" in result.content


@pytest.mark.asyncio
async def test_list_detections_pages_with_the_offset_its_hint_names(monkeypatch):
    def router(method, url):
        if "queries/alerts" in url:
            return {
                "resources": ["ngsiem:26"],
                "meta": {"pagination": {"offset": 25, "limit": 1, "total": 30}},
            }
        return {"resources": [{"composite_id": "ngsiem:26", "status": "new"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await _call(executor, "list_crowdstrike_ngsiem_detections", limit=1, offset=25)

    assert not result.is_error
    assert recorder.calls[0]["params"] == {
        "filter": "product:'ngsiem'",
        "limit": 1,
        "sort": "created_timestamp|desc",
        "offset": 25,
    }
    assert "[30 in all - pass offset=26 for the next page]" in result.content


# ---------------------------------------------------------------------------
# Saved queries and lookup files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_lookup_files_passes_name_and_domain(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"resources": [{"filename": "bad_domains.csv"}]}
    )
    result = await _call(
        executor,
        "list_crowdstrike_ngsiem_lookup_files",
        name="bad",
        search_domain="third-party",
        limit=500,
    )

    assert not result.is_error
    assert recorder.calls[0]["params"] == {
        "limit": 100,
        "offset": 0,
        "filter": "name:~'bad'",
        "search_domain": "third-party",
    }
    assert "bad_domains.csv" in result.content


@pytest.mark.asyncio
async def test_list_saved_queries_refuses_an_unknown_domain(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await _call(
        executor, "list_crowdstrike_ngsiem_saved_queries", search_domain="parsers-repository"
    )

    assert result.content.startswith("Error: search_domain must be one of")
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_saved_query_returns_its_yaml_template(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"resources": [{"id": "q1", "yaml_template": "name: hunt\nqueryString: '*'"}]},
    )
    result = await _call(executor, "get_crowdstrike_ngsiem_saved_query", query_id="q1")

    assert not result.is_error
    assert recorder.calls[0]["params"] == {"ids": "q1"}
    assert "queryString: '*'" in result.content
    assert "Untrusted external content follows" in result.content


class _FileContext:
    def __init__(self, files: dict[str, tuple[bytes, str, str]]):
        self._files = files

    async def resolve_upload(self, file_id: str):
        return self._files.get(file_id)


@pytest.mark.asyncio
async def test_upload_lookup_file_posts_the_file_of_the_run(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"resources": [], "errors": []})
    executor.file_context = _FileContext(
        {"f1": (b"domain\nevil.example\n", "iocs.csv", "text/csv")}
    )
    result = await _call(
        executor,
        "upload_crowdstrike_ngsiem_lookup_file",
        file_id="f1",
        filename="opencti_iocs.csv",
    )

    assert not result.is_error, result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{_BASE}/humio/api/v1/repositories/search-all/files"
    assert call["files"] == {
        "file": ("opencti_iocs.csv", b"domain\nevil.example\n", "text/csv")
    }
    assert "Uploaded lookup file 'opencti_iocs.csv' (20 bytes)" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_name", "message"),
    [
        ("notes.txt", "Error: a lookup file must be a .csv or a .json file"),
        ("../iocs.csv", "Error: filename must be a plain file name"),
    ],
)
async def test_upload_without_a_filename_checks_the_source_name(monkeypatch, source_name, message):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    executor.file_context = _FileContext({"f1": (b"x", source_name, "text/plain")})
    result = await _call(executor, "upload_crowdstrike_ngsiem_lookup_file", file_id="f1")

    assert result.is_error
    assert result.content.startswith(message)
    assert recorder.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("iocs.txt", "Error: a lookup file must be a .csv or a .json file"),
        ("../iocs.csv", "Error: filename must be a plain file name"),
    ],
)
async def test_upload_lookup_file_refuses_bad_names(monkeypatch, filename, message):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    executor.file_context = _FileContext({"f1": (b"x", "iocs.csv", "text/csv")})
    result = await _call(
        executor, "upload_crowdstrike_ngsiem_lookup_file", file_id="f1", filename=filename
    )

    assert result.is_error
    assert result.content.startswith(message)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_download_lookup_file_stores_it_as_a_file_of_the_run(monkeypatch):
    fetched: list[dict[str, Any]] = []

    async def _fake_fetch(_client, method, url, **kwargs):
        fetched.append({"method": method, "url": url, **kwargs})
        return FetchedFile(data=b"a,b\n1,2\n", content_type="application/octet-stream", filename="")

    stored: list[tuple[str, bytes, str]] = []

    async def _persist(filename, data, content_type):
        stored.append((filename, data, content_type))
        return None

    executor, _recorder = _build(monkeypatch, lambda m, u: {}, persist_file_fn=_persist)
    monkeypatch.setattr(ng, "fetch_file", _fake_fetch)
    result = await _call(
        executor, "download_crowdstrike_ngsiem_lookup_file", filename="asset list.csv"
    )

    assert not result.is_error, result.content
    assert len(fetched) == 1
    assert fetched[0]["url"] == f"{_BASE}/humio/api/v1/repositories/search-all/files/asset%20list.csv"
    assert fetched[0]["headers"]["Authorization"] == "Bearer dummy-token"
    assert fetched[0]["max_bytes"] == 50 * 1024 * 1024
    assert stored == [("asset list.csv", b"a,b\n1,2\n", "text/csv")]


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------


class _FakeEgress:
    def __init__(self, status_code: int, calls: list[dict[str, Any]]):
        self._status_code = status_code
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, **kwargs):
        self._calls.append({"url": url, **kwargs})
        return _StubResponse({}, self._status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "ok", "message"),
    [
        (200, True, "CrowdStrike Next-Gen SIEM is connected and the API client can read it."),
        (403, False, "Authenticated, but the API client cannot read Next-Gen SIEM"),
        (500, False, "Authenticated, but the Next-Gen SIEM API answered HTTP 500."),
    ],
)
async def test_validate_connection_probes_ngsiem_read_access(monkeypatch, status_code, ok, message):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ng, "request_falcon_token", _token_ok)
    monkeypatch.setattr(ng, "egress_client", lambda **_kw: _FakeEgress(status_code, calls))

    result_ok, result_message = await ng.validate_connection(_BASE, "id", "secret")

    assert result_ok is ok
    assert result_message.startswith(message)
    assert len(calls) == 1
    assert calls[0]["url"] == f"{_BASE}/ngsiem-content/queries/savedqueries/v1"
    assert calls[0]["params"] == {"limit": 1}


@pytest.mark.asyncio
async def test_validate_connection_fails_on_a_refused_token(monkeypatch):
    async def _token_refused(*_args):
        return None, 0

    monkeypatch.setattr(ng, "request_falcon_token", _token_refused)
    ok, message = await ng.validate_connection(_BASE, "id", "secret")

    assert ok is False
    assert message.startswith("Authentication failed")


@pytest.mark.asyncio
async def test_validate_connection_needs_both_credentials():
    assert await ng.validate_connection(_BASE, "", "secret") == (
        False,
        "Missing API client ID or secret.",
    )


@pytest.mark.asyncio
async def test_the_connection_test_dispatches_to_the_ngsiem_probe(monkeypatch):
    from app.api.v1 import integrations as integrations_api

    seen: list[tuple[str, str, str]] = []

    async def _fake_validate(base_url, client_id, client_secret):
        seen.append((base_url, client_id, client_secret))
        return True, "probed"

    monkeypatch.setattr(ng, "validate_connection", _fake_validate)
    # Real ciphertexts: the secret is read through the vault (reveal_field).
    integration = SimpleNamespace(
        id=uuid.uuid4(),
        provider="crowdstrike_ngsiem",
        status="configured",
        auth_type="api_key",
        extra_config_enc=encrypt_value(json.dumps({"cloud": "eu-1"})),
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=encrypt_value("csecret"),
    )

    result = await integrations_api._test_integration(integration, None, [], None)

    assert result == {"status": "ok", "message": "probed"}
    assert seen == [(_BASE, "cid", "csecret")]


# ---------------------------------------------------------------------------
# Compound-token resolution
# ---------------------------------------------------------------------------


class _FakeIntegration:
    def __init__(self, *, client_id_enc: str | None, client_secret_enc: str | None):
        self.id = uuid.uuid4()
        self.name = "ngsiem-prod"
        self.provider = "crowdstrike_ngsiem"
        self.auth_type = "api_key"
        self.access_token_enc = None
        self.api_key_enc = None
        self.client_id_enc = client_id_enc
        self.client_secret_enc = client_secret_enc
        self.extra_config_enc = encrypt_value(json.dumps({"cloud": "us-2"}))
        self.refresh_token_enc = None
        self.token_expires_at = None
        self.token_meta = None
        self.installation_id = None
        self.is_company_managed = False
        self.user_id = None
        self.scope_kind = None
        self.scope_id = None
        self.binding_metadata = None


class _FakeDB:
    def __init__(self, integ):
        self._integ = integ

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._integ)

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.fixture
def _resolve(monkeypatch):
    async def _no_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _no_refresh)

    async def _runner(*, client_id: str | None, client_secret: str | None) -> str | None:
        integ = _FakeIntegration(
            client_id_enc=encrypt_value(client_id) if client_id else None,
            client_secret_enc=encrypt_value(client_secret) if client_secret else None,
        )
        return await integration_tools._get_token(
            _FakeDB(integ),  # type: ignore[arg-type]
            str(integ.id),
            provider="crowdstrike_ngsiem",
        )

    return _runner


@pytest.mark.asyncio
async def test_token_resolution_builds_the_falcon_compound(_resolve):
    token = await _resolve(client_id="cid", client_secret="sec||ret")

    assert token == "https://api.us-2.crowdstrike.com||cid||sec||ret"


@pytest.mark.asyncio
async def test_token_resolution_keeps_a_partial_compound(_resolve):
    token = await _resolve(client_id=None, client_secret="secret")

    assert token is not None
    assert parse_compound_token(token, 3) == ("https://api.us-2.crowdstrike.com", "", "secret")
