"""Tests for the LogRhythm SIEM integration provider.

Covers tool introspection, the Bearer header + base-URL helpers, the case
listing (status filter + pagination), case status change (validation + the
changeStatus action), the alarm status update (status normalisation + RBP),
adding a case note, alarm listing (envelope unwrapping), and the async search
flow (submit search-task -> poll search-result -> read items). Every API call
routes through the shared ``safe_request`` helper, which is monkeypatched here
so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.http import EmptyBearerTokenError
from app.integrations.providers import logrhythm as lr
from app.integrations.providers.logrhythm import (
    build_logrhythm_headers,
    build_tools,
    logrhythm_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://logrhythm-pm.example.com:8501||tok-123"


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _NonJsonResponse:
    """A 2xx response whose body is not JSON (e.g. an HTML auth/error page)."""

    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def json(self) -> Any:
        raise ValueError("not json")


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._router(method, url)), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(lr, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=executor
    )
    names = {t.name for t in tools}
    assert "get_logrhythm_status" in names
    assert "list_logrhythm_cases" in names
    assert "create_logrhythm_case" in names
    assert "update_logrhythm_case_status" in names
    assert "add_logrhythm_case_note" in names
    assert "list_logrhythm_alarms" in names
    assert "update_logrhythm_alarm_status" in names
    assert "run_logrhythm_search" in names
    assert "list_logrhythm_hosts" in names
    assert "list_logrhythm_entities" in names
    # Exact inventory - the provider ships a fixed set of 15 tools, so an
    # accidentally dropped (or duplicated) registration must fail the build.
    assert len(tools) == 15


def test_build_tools_empty_without_token():
    executor = CallableToolExecutor()
    # base URL only, no API token -> no tools
    assert (
        build_tools(token="https://logrhythm-pm.example.com:8501||", executor=executor)
        == []
    )


def test_build_tools_empty_without_base_url():
    executor = CallableToolExecutor()
    # token only, no base URL -> no tools
    assert build_tools(token="||tok-123", executor=executor) == []


def test_build_tools_empty_with_whitespace_only_token():
    executor = CallableToolExecutor()
    # a pasted token that is only whitespace must degrade to "no tools",
    # never an illegal "Authorization: Bearer " header at request time
    assert (
        build_tools(
            token="https://logrhythm-pm.example.com:8501||   ", executor=executor
        )
        == []
    )


def test_headers_and_base_url_helpers():
    headers = build_logrhythm_headers("tok-123")
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["Accept"] == "application/json"
    # the Bearer guard strips the token before building the header
    assert build_logrhythm_headers("  tok-123  ")["Authorization"] == "Bearer tok-123"
    # trailing-slash normalisation; surrounding whitespace trimmed; empty stays empty
    assert logrhythm_api_base("") == ""
    assert (
        logrhythm_api_base("https://logrhythm-pm.example.com:8501/")
        == "https://logrhythm-pm.example.com:8501"
    )
    assert (
        logrhythm_api_base("  https://logrhythm-pm.example.com:8501/  ")
        == "https://logrhythm-pm.example.com:8501"
    )


def test_build_logrhythm_headers_rejects_blank_token():
    # a blank/whitespace-only token must fail fast via the shared Bearer guard
    # rather than producing an illegal header that httpx rejects later
    with pytest.raises(EmptyBearerTokenError):
        build_logrhythm_headers("   ")


@pytest.mark.asyncio
async def test_call_surfaces_non_json_body(monkeypatch):
    # a 200 with a non-JSON body (e.g. an HTML gateway/auth page) must surface a
    # clear error, not a misleading empty result rendered as "nothing found"
    async def _fake(_client, _method, _url, _provider, _action, **_kwargs):
        return _NonJsonResponse(b"<html>Access Denied</html>"), None

    monkeypatch.setattr(lr, "safe_request", _fake)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="nj1", name="list_logrhythm_cases", arguments={})
    )
    assert "non-JSON" in result.content


@pytest.mark.asyncio
async def test_call_treats_empty_body_as_success(monkeypatch):
    # an empty body (e.g. 204 No Content) is a legitimate "no JSON" success
    async def _fake(_client, _method, _url, _provider, _action, **_kwargs):
        return _NonJsonResponse(b""), None

    monkeypatch.setattr(lr, "safe_request", _fake)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="nj2", name="list_logrhythm_cases", arguments={})
    )
    assert not result.is_error
    assert "no cases found" in result.content.lower()


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cases_filters_and_formats(monkeypatch):
    def router(method, url):
        return [
            {
                "id": "C1",
                "number": 101,
                "name": "Suspicious login",
                "priority": 2,
                "status": {"name": "Incident", "number": 3},
                "entity": {"name": "Primary Site"},
                "dateCreated": "2026-01-02T03:04:05.000Z",
            }
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_logrhythm_cases",
            arguments={"status": "Incident", "limit": 10},
        )
    )
    assert not result.is_error
    assert "Suspicious login" in result.content
    assert "#101" in result.content or "case 101" in result.content
    # status name resolved to its numeric identifier (Incident = 3)
    assert recorder.calls[0]["params"]["statusNumber"] == 3
    # pagination forwarded via the count/offset headers
    assert recorder.calls[0]["headers"]["count"] == "10"


@pytest.mark.asyncio
async def test_list_cases_tolerates_non_numeric_limit(monkeypatch):
    # tool args are not runtime schema-validated; a non-numeric limit must not
    # raise (it previously crashed in `int(limit or 25)`) - it falls back to
    # the default page size instead
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: [
            {
                "id": "C1",
                "number": 1,
                "name": "X",
                "priority": 3,
                "status": {"name": "Created", "number": 1},
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c7", name="list_logrhythm_cases", arguments={"limit": "oops"}
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["headers"]["count"] == "25"


@pytest.mark.asyncio
async def test_list_cases_coerces_and_validates_priority(monkeypatch):
    # priority may arrive as a numeric string; it is coerced to int for the API
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    ok = await executor.execute_tool(
        ToolCall(id="c9", name="list_logrhythm_cases", arguments={"priority": "2"})
    )
    assert not ok.is_error
    assert recorder.calls[-1]["params"]["priority"] == 2
    # an out-of-range priority is rejected with a clear error and makes no call
    bad = await executor.execute_tool(
        ToolCall(id="c10", name="list_logrhythm_cases", arguments={"priority": 9})
    )
    assert "priority filter must be" in bad.content.lower()
    # "0" (string) is the schema-default sentinel = "no priority filter": it must
    # be ignored (not rejected as out-of-range) and omitted from the query params
    zero = await executor.execute_tool(
        ToolCall(id="c12", name="list_logrhythm_cases", arguments={"priority": "0"})
    )
    assert not zero.is_error
    assert "priority" not in recorder.calls[-1]["params"]


@pytest.mark.asyncio
async def test_create_case_requires_name(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c2", name="create_logrhythm_case", arguments={"name": "  "})
    )
    assert "name is required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_create_case_posts_body(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"id": "C9", "number": 9, "name": "Phishing", "priority": 1,
                      "status": {"name": "Created", "number": 1}},
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="create_logrhythm_case",
            arguments={"name": "Phishing", "priority": 1, "summary": "user reported"},
        )
    )
    assert not result.is_error
    assert "#9" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/lr-case-api/cases")
    assert call["json"]["name"] == "Phishing"
    assert call["json"]["priority"] == 1
    assert call["json"]["summary"] == "user reported"


@pytest.mark.asyncio
async def test_change_case_status_rejects_invalid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="update_logrhythm_case_status",
            arguments={"case_id": "5", "status": "bogus"},
        )
    )
    assert "invalid case status" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_change_case_status_valid(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"number": 5, "status": {"name": "Incident", "number": 3}}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="update_logrhythm_case_status",
            arguments={"case_id": "5", "status": "Incident"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "put"
    assert call["url"].endswith("/lr-case-api/cases/5/actions/changeStatus")
    assert call["json"]["statusNumber"] == 3


@pytest.mark.asyncio
async def test_change_case_status_accepts_int_status(monkeypatch):
    # an int status (3) must not raise AttributeError in _resolve_case_status_number
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"number": 5, "status": {"name": "Incident", "number": 3}},
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="update_logrhythm_case_status",
            arguments={"case_id": "5", "status": 3},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["json"]["statusNumber"] == 3


@pytest.mark.asyncio
async def test_add_case_note(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"text": "ok"})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="add_logrhythm_case_note",
            arguments={"case_id": "7", "note": "  Contained the host.  "},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/lr-case-api/cases/7/evidence/note")
    # the note is stripped before it is persisted as evidence
    assert call["json"]["text"] == "Contained the host."


@pytest.mark.asyncio
async def test_update_case_rejects_blank_name(monkeypatch):
    # a whitespace-only name must be rejected with a clear error (parity with
    # create_logrhythm_case) instead of sending an invalid update payload
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="update_logrhythm_case",
            arguments={"case_id": "7", "name": "   "},
        )
    )
    assert "name cannot be blank" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_case_treats_zero_priority_as_noop(monkeypatch):
    # priority "0" is the sentinel "no change": it must not error or set priority
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"number": 7, "status": {"name": "Created", "number": 1}},
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="update_logrhythm_case",
            arguments={"case_id": "7", "name": "Renamed", "priority": "0"},
        )
    )
    assert not result.is_error
    body = recorder.calls[-1]["json"]
    assert body["name"] == "Renamed"
    assert "priority" not in body


# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alarms_unwraps_envelope(monkeypatch):
    def router(method, url):
        return {
            "alarmsCount": 1,
            "alarmsSearchDetails": [
                {
                    "alarmId": 42,
                    "alarmRuleName": "Brute force detected",
                    "alarmStatus": "Opened",
                    "entityName": "Primary Site",
                    "dateInserted": "2026-01-02T03:04:05",
                    "associatedCases": ["C1"],
                }
            ],
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="a1", name="list_logrhythm_alarms", arguments={"status": "Opened"})
    )
    assert not result.is_error
    assert "Brute force detected" in result.content
    assert "alarm 42" in result.content


@pytest.mark.asyncio
async def test_update_alarm_status_rejects_invalid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a2",
            name="update_logrhythm_alarm_status",
            arguments={"alarm_id": 42, "status": "nope"},
        )
    )
    assert "invalid alarm status" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_alarm_status_patches_status_and_rbp(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a3",
            name="update_logrhythm_alarm_status",
            arguments={"alarm_id": 42, "status": "Closed_Resolved", "rbp": 80},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "patch"
    assert call["url"].endswith("/lr-alarm-api/alarms/42")
    assert call["json"]["alarmStatus"] == "Closed_Resolved"
    assert call["json"]["rBP"] == 80


@pytest.mark.asyncio
async def test_update_alarm_status_coerces_string_rbp(monkeypatch):
    # rbp may arrive as a string from the LLM; `"75" >= 0` would raise
    # TypeError before the int() coercion, so it must be coerced first
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a4",
            name="update_logrhythm_alarm_status",
            arguments={"alarm_id": 42, "status": "Working", "rbp": "75"},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["json"]["rBP"] == 75


@pytest.mark.asyncio
async def test_update_alarm_status_accepts_zero_rbp(monkeypatch):
    # 0 is a valid Risk Based Priority (distinct from the -1 "unchanged" sentinel)
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a5",
            name="update_logrhythm_alarm_status",
            arguments={"alarm_id": 42, "status": "Working", "rbp": 0},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["json"]["rBP"] == 0


@pytest.mark.asyncio
async def test_update_alarm_status_accepts_int_status(monkeypatch):
    # an int status (1 -> Opened) must not raise AttributeError in _resolve_alarm_status
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a7",
            name="update_logrhythm_alarm_status",
            arguments={"alarm_id": 42, "status": 1},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["json"]["alarmStatus"] == "Opened"


@pytest.mark.asyncio
async def test_add_alarm_comment_strips(monkeypatch):
    # the comment is stripped before it is persisted on the alarm
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="a6",
            name="add_logrhythm_alarm_comment",
            arguments={"alarm_id": 42, "comment": "  triaged - benign  "},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/lr-alarm-api/alarms/42/comments")
    assert call["json"]["alarmComment"] == "triaged - benign"


# ---------------------------------------------------------------------------
# Search (submit search-task -> poll search-result -> items)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_search_full_flow(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(lr.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if url.endswith("/lr-search-api/actions/search-task"):
            return {"TaskId": "task-1", "TaskStatus": "Searching"}
        if url.endswith("/lr-search-api/actions/search-result"):
            return {
                "TaskStatus": "Completed",
                "Items": [
                    {
                        "normalDate": "2026-01-02T03:04:05",
                        "commonEventName": "Failed authentication",
                        "logSourceName": "Windows Security",
                        "impactedHostName": "DC-01",
                        "message": "logon failure for admin",
                    }
                ],
            }
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s1",
            name="run_logrhythm_search",
            arguments={"keyword": "logon failure", "number_of_days": 2},
        )
    )
    assert not result.is_error
    assert "Failed authentication" in result.content
    assert "DC-01" in result.content
    # The submit body carries the date window + keyword filter
    start = next(
        c for c in recorder.calls if c["url"].endswith("/lr-search-api/actions/search-task")
    )
    assert start["json"]["dateCriteria"]["lastIntervalValue"] == 2
    assert start["json"]["maxMsgsToQuery"] == 100
    # The poll body references the returned TaskId as the search GUID
    poll = next(
        c for c in recorder.calls if c["url"].endswith("/lr-search-api/actions/search-result")
    )
    assert poll["json"]["data"]["searchGuid"] == "task-1"


@pytest.mark.asyncio
async def test_run_search_reports_failure(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(lr.asyncio, "sleep", _fast_sleep)

    def router(method, url):
        if url.endswith("/search-task"):
            return {"TaskId": "task-2", "TaskStatus": "Searching"}
        if url.endswith("/search-result"):
            return {"TaskStatus": "Failed"}
        return {}

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="s2", name="run_logrhythm_search", arguments={"keyword": "x"})
    )
    assert "ended with status Failed" in result.content
