"""Tests for the TheHive integration provider.

Covers tool introspection, the Bearer ``Authorization`` header + base-URL
helpers, the Query API flows (listCase/listAlert operations + structured
filter composition + pagination), case creation/close with friendly enum
inputs, alert promotion/merge, bulk observable creation, task lifecycle,
Cortex analyzer runs (with cortexId auto-resolution), and the consolidated
manage tool (action + required-argument guards). Every API call routes
through the shared ``safe_request`` helper, which is monkeypatched here so
no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import thehive as thehive_mod
from app.integrations.providers.thehive import (
    build_thehive_headers,
    build_tools,
    thehive_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://thehive.example.com||key-123"


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _NonJsonResponse:
    """A 2xx response whose body is not JSON (login page, proxy error...)."""

    def json(self) -> Any:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._router(method, url)
        if payload is _NonJsonResponse:
            return _NonJsonResponse(), None
        return _StubResponse(payload), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(thehive_mod, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "search_thehive_cases" in names
    assert "get_thehive_case" in names
    assert "create_thehive_case" in names
    assert "close_thehive_case" in names
    assert "search_thehive_alerts" in names
    assert "promote_thehive_alert" in names
    assert "merge_thehive_alert_into_case" in names
    assert "add_thehive_observable" in names
    assert "create_thehive_task" in names
    assert "add_thehive_task_log" in names
    assert "add_thehive_comment" in names
    assert "run_thehive_analyzer" in names
    assert "manage_thehive_item" in names
    assert len(tools) >= 29


def test_tool_priorities_read_write_destructive():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    by_name = {t.name: t for t in tools}
    # reads sort first, writes next, the destructive-capable manage tool last
    assert by_name["search_thehive_cases"].priority == 0
    assert by_name["get_thehive_alert"].priority == 0
    assert by_name["list_thehive_observables"].priority == 0
    assert by_name["create_thehive_case"].priority == 1
    assert by_name["promote_thehive_alert"].priority == 1
    assert by_name["manage_thehive_item"].priority == 2


def test_build_tools_empty_without_base_url():
    executor = CallableToolExecutor()
    assert build_tools(token="||key-123", executor=executor) == []


def test_build_tools_empty_without_key():
    executor = CallableToolExecutor()
    assert build_tools(token="https://thehive.example.com||", executor=executor) == []
    # A whitespace-only key must be treated as absent as well.
    assert (
        build_tools(token="https://thehive.example.com||   ", executor=executor) == []
    )


def test_headers_use_bearer_prefix():
    headers = build_thehive_headers("abc")
    assert headers["Authorization"] == "Bearer abc"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    # surrounding whitespace is stripped from the key
    assert build_thehive_headers("  abc  ")["Authorization"] == "Bearer abc"


def test_build_headers_omits_authorization_when_blank():
    for blank in ("", "   ", None):
        headers = build_thehive_headers(blank)
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"


def test_api_base_normalisation():
    assert (
        thehive_api_base("https://thehive.example.com/")
        == "https://thehive.example.com"
    )
    assert (
        thehive_api_base("  https://thehive.example.com  ")
        == "https://thehive.example.com"
    )
    assert thehive_api_base("") == ""


@pytest.mark.asyncio
async def test_get_user_info_guards_misconfigured_credentials():
    # A misconfigured integration produces a ``||key`` (no base_url) or
    # ``base||`` (no key) compound token - get_user_info must surface a clear
    # configuration error instead of firing a relative-URL request.
    integration = thehive_mod.TheHiveIntegration()
    info = await integration.get_user_info("||key-123")
    assert "base URL is not configured" in info["error"]
    info = await integration.get_user_info("https://thehive.example.com||   ")
    assert "API key is not configured" in info["error"]


# ---------------------------------------------------------------------------
# Case search (Query API)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_cases_builds_query_operations(monkeypatch):
    def router(method, url):
        return [
            {
                "_id": "~40964152",
                "number": 42,
                "title": "Phishing wave",
                "status": "InProgress",
                "stage": "InProgress",
                "severity": 3,
                "assignee": "analyst@corp.org",
                "tags": ["phishing"],
                "_createdAt": 1750000000000,
                "_createdBy": "analyst@corp.org",
            }
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_thehive_cases",
            arguments={
                "keyword": "phishing",
                "stage": "InProgress",
                "severity": "high",
                "tags": "phishing,vip",
                "flagged_only": True,
                "limit": 10,
            },
        )
    )
    assert not result.is_error
    assert "Case #42" in result.content
    assert "Phishing wave" in result.content
    assert "High" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v1/query")
    body = call["json"]
    ops = body["query"]
    assert ops[0] == {"_name": "listCase"}
    # the filter op ANDs every structured clause together
    filter_op = ops[1]
    assert filter_op["_name"] == "filter"
    clauses = filter_op["_and"]
    assert {
        "_or": [
            {"_like": {"_field": "title", "_value": "phishing"}},
            {"_like": {"_field": "description", "_value": "phishing"}},
        ]
    } in clauses
    assert {"_eq": {"_field": "stage", "_value": "InProgress"}} in clauses
    assert {"_eq": {"_field": "severity", "_value": 3}} in clauses
    assert {"_eq": {"_field": "tags", "_value": "phishing"}} in clauses
    assert {"_eq": {"_field": "tags", "_value": "vip"}} in clauses
    assert {"_eq": {"_field": "flag", "_value": True}} in clauses
    # sort + page operations close the pipeline
    assert ops[2] == {"_name": "sort", "_fields": [{"_createdAt": "desc"}]}
    assert ops[3] == {"_name": "page", "from": 0, "to": 10}
    # heavy fields are excluded from list results
    assert body["excludeFields"] == ["description", "summary"]


@pytest.mark.asyncio
async def test_search_cases_rejects_invalid_severity(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="search_thehive_cases",
            arguments={"severity": "apocalyptic"},
        )
    )
    assert "severity must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_cases_rejects_bad_date(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="search_thehive_cases",
            arguments={"created_after": "last week"},
        )
    )
    assert "YYYY-MM-DD" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_cases_date_bounds_are_inclusive(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="c3b",
            name="search_thehive_cases",
            arguments={
                "created_after": "2026-07-01",
                "created_before": "2026-07-01",
            },
        )
    )
    assert not result.is_error
    clauses = recorder.calls[0]["json"]["query"][1]["_and"]
    gte = next(c for c in clauses if "_gte" in c)["_gte"]["_value"]
    lte = next(c for c in clauses if "_lte" in c)["_lte"]["_value"]
    # created_after is midnight; created_before covers the whole day
    # (last millisecond), so a single-day range matches that day's cases.
    assert lte - gte == 86_400_000 - 1


@pytest.mark.asyncio
async def test_get_case_appends_deep_link_and_strips_hash(monkeypatch):
    def router(method, url):
        if url.endswith("/api/v1/query"):
            return []  # no comments
        return {
            "_id": "~40964152",
            "number": 42,
            "title": "Phishing wave",
            "description": "Multiple users reported...",
            "status": "New",
            "stage": "New",
            "severity": 2,
            "tags": [],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c4", name="get_thehive_case", arguments={"case_id": "#42"})
    )
    assert not result.is_error
    assert "Multiple users reported" in result.content
    assert "https://thehive.example.com/cases/~40964152/details" in result.content
    # the '#' prefix is stripped from the case reference
    assert recorder.calls[0]["url"].endswith("/api/v1/case/42")


# ---------------------------------------------------------------------------
# Case writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_case_maps_friendly_enums(monkeypatch):
    def router(method, url):
        return {"_id": "~123", "number": 99, "title": "New incident"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="create_thehive_case",
            arguments={
                "title": "New incident",
                "description": "details",
                "severity": "critical",
                "tlp": "amber",
                "tags": "malware, ransomware",
                "case_template": "ransomware-response",
            },
        )
    )
    assert not result.is_error
    assert "#99" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v1/case")
    payload = call["json"]
    assert payload["severity"] == 4
    assert payload["tlp"] == 2
    assert payload["tags"] == ["malware", "ransomware"]
    assert payload["caseTemplate"] == "ransomware-response"


@pytest.mark.asyncio
async def test_close_case_requires_summary(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="close_thehive_case",
            arguments={"case_id": "42", "summary": "   "},
        )
    )
    assert "summary is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_close_case_patches_status_and_impact(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="close_thehive_case",
            arguments={
                "case_id": "42",
                "summary": "Confirmed phishing, credentials reset.",
                "status": "TruePositive",
                "impact_status": "WithImpact",
            },
        )
    )
    assert not result.is_error
    assert "closed as TruePositive" in result.content
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert call["url"].endswith("/api/v1/case/42")
    assert call["json"] == {
        "status": "TruePositive",
        "summary": "Confirmed phishing, credentials reset.",
        "impactStatus": "WithImpact",
    }


@pytest.mark.asyncio
async def test_close_case_rejects_invalid_impact(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="close_thehive_case",
            arguments={
                "case_id": "42",
                "summary": "done",
                "impact_status": "Catastrophic",
            },
        )
    )
    assert "impact_status must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_merge_cases_requires_two_ids(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c9", name="merge_thehive_cases", arguments={"case_ids": "42"})
    )
    assert "at least two case ids" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_merge_cases_joins_ids_in_path(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"_id": "~9", "number": 100}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="merge_thehive_cases",
            arguments={"case_ids": "#42, 57"},
        )
    )
    assert not result.is_error
    assert recorder.calls[0]["url"].endswith("/api/v1/case/_merge/42,57")


# ---------------------------------------------------------------------------
# Alerts - triage flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_alerts_builds_listalert_query(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="a1",
            name="search_thehive_alerts",
            arguments={"status": "New", "source": "splunk", "limit": 5},
        )
    )
    assert not result.is_error
    body = recorder.calls[0]["json"]
    ops = body["query"]
    assert ops[0] == {"_name": "listAlert"}
    clauses = ops[1]["_and"]
    assert {"_eq": {"_field": "status", "_value": "New"}} in clauses
    assert {"_eq": {"_field": "source", "_value": "splunk"}} in clauses
    assert ops[-1] == {"_name": "page", "from": 0, "to": 5}


@pytest.mark.asyncio
async def test_promote_alert_posts_case_template(monkeypatch):
    def router(method, url):
        return {"_id": "~777", "number": 101, "title": "Promoted case"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="a2",
            name="promote_thehive_alert",
            arguments={"alert_id": "~555", "case_template": "phishing-response"},
        )
    )
    assert not result.is_error
    assert "case #101" in result.content
    assert "https://thehive.example.com/cases/~777/details" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v1/alert/~555/case")
    assert call["json"] == {"caseTemplate": "phishing-response"}


@pytest.mark.asyncio
async def test_merge_alert_into_case_builds_path(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"number": 42})
    result = await executor.execute_tool(
        ToolCall(
            id="a3",
            name="merge_thehive_alert_into_case",
            arguments={"alert_id": "~555", "case_id": "#42"},
        )
    )
    assert not result.is_error
    assert recorder.calls[0]["url"].endswith("/api/v1/alert/~555/merge/42")


@pytest.mark.asyncio
async def test_create_alert_posts_identity_triple(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"_id": "~1", "title": "Suspicious login"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="a4",
            name="create_thehive_alert",
            arguments={
                "alert_type": "siem",
                "source": "splunk",
                "source_ref": "evt-123",
                "title": "Suspicious login",
                "description": "details",
                "severity": "high",
            },
        )
    )
    assert not result.is_error
    payload = recorder.calls[0]["json"]
    assert payload["type"] == "siem"
    assert payload["source"] == "splunk"
    assert payload["sourceRef"] == "evt-123"
    assert payload["severity"] == 3


# ---------------------------------------------------------------------------
# Observables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_observables_requires_exactly_one_target(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(id="o1", name="list_thehive_observables", arguments={})
    )
    assert "exactly one of case_id or alert_id" in result.content
    result = await executor.execute_tool(
        ToolCall(
            id="o2",
            name="list_thehive_observables",
            arguments={"case_id": "42", "alert_id": "~5"},
        )
    )
    assert "exactly one of case_id or alert_id" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_observables_chains_getcase_query(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="o3",
            name="list_thehive_observables",
            arguments={"case_id": "#42", "data_type": "ip", "ioc_only": True},
        )
    )
    assert not result.is_error
    ops = recorder.calls[0]["json"]["query"]
    assert ops[0] == {"_name": "getCase", "idOrName": "42"}
    assert ops[1] == {"_name": "observables"}
    clauses = ops[2]["_and"]
    assert {"_eq": {"_field": "dataType", "_value": "ip"}} in clauses
    assert {"_eq": {"_field": "ioc", "_value": True}} in clauses


@pytest.mark.asyncio
async def test_add_observable_splits_bulk_values(monkeypatch):
    def router(method, url):
        return [{"_id": "~o1"}, {"_id": "~o2"}]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="o4",
            name="add_thehive_observable",
            arguments={
                "case_id": "42",
                "data": "203.0.113.7, 198.51.100.9",
                "data_type": "ip",
                "ioc": True,
            },
        )
    )
    assert not result.is_error
    assert "Added 2 ip observables" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/api/v1/case/42/observable")
    assert call["json"]["data"] == ["203.0.113.7", "198.51.100.9"]
    assert call["json"]["ioc"] is True


@pytest.mark.asyncio
async def test_update_observable_validates_flags(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="o5",
            name="update_thehive_observable",
            arguments={"observable_id": "~o1", "ioc": "yes"},
        )
    )
    assert "ioc must be 'true' or 'false'" in result.content
    assert recorder.calls == []
    result = await executor.execute_tool(
        ToolCall(
            id="o6",
            name="update_thehive_observable",
            arguments={"observable_id": "~o1", "ioc": "true", "sighted": "false"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert call["json"] == {"ioc": True, "sighted": False}


# ---------------------------------------------------------------------------
# Tasks + logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_org_wide_uses_listtask(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="t1",
            name="list_thehive_tasks",
            arguments={"status": "Waiting"},
        )
    )
    assert not result.is_error
    ops = recorder.calls[0]["json"]["query"]
    assert ops[0] == {"_name": "listTask"}
    assert ops[1] == {
        "_name": "filter",
        "_eq": {"_field": "status", "_value": "Waiting"},
    }


@pytest.mark.asyncio
async def test_create_task_posts_to_case(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"_id": "~t1", "title": "Contain host"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="t2",
            name="create_thehive_task",
            arguments={
                "case_id": "#42",
                "title": "Contain host",
                "group": "containment",
                "due_date": "2026-07-15",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/api/v1/case/42/task")
    assert call["json"]["group"] == "containment"
    assert isinstance(call["json"]["dueDate"], int)


@pytest.mark.asyncio
async def test_update_task_rejects_invalid_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="t3",
            name="update_thehive_task",
            arguments={"task_id": "~t1", "status": "Done"},
        )
    )
    assert "status must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_task_completed_reports_completion(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="t4",
            name="update_thehive_task",
            arguments={"task_id": "~t1", "status": "Completed"},
        )
    )
    assert not result.is_error
    assert "Task completed" in result.content
    assert recorder.calls[0]["json"] == {"status": "Completed"}


@pytest.mark.asyncio
async def test_add_task_log_posts_message(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"_id": "~log1"})
    result = await executor.execute_tool(
        ToolCall(
            id="t5",
            name="add_thehive_task_log",
            arguments={"task_id": "~t1", "message": "Blocked the C2 IP."},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/api/v1/task/~t1/log")
    assert call["json"] == {"message": "Blocked the C2 IP."}


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_comment_requires_exactly_one_target(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="m1", name="add_thehive_comment", arguments={"message": "hi"})
    )
    assert "exactly one of case_id or alert_id" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_add_comment_to_alert(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"_id": "~c1"})
    result = await executor.execute_tool(
        ToolCall(
            id="m2",
            name="add_thehive_comment",
            arguments={"alert_id": "~555", "message": "Confirmed malicious."},
        )
    )
    assert not result.is_error
    assert recorder.calls[0]["url"].endswith("/api/v1/alert/~555/comment")


# ---------------------------------------------------------------------------
# Cortex analyzers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_analyzer_auto_resolves_cortex_id(monkeypatch):
    def router(method, url):
        if "/connector/cortex/analyzer/" in url:
            return {"id": "vt", "cortexIds": ["CORTEX-1"]}
        return {"_id": "~job1", "analyzerName": "VirusTotal", "status": "Waiting"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="z1",
            name="run_thehive_analyzer",
            arguments={"analyzer_id": "vt", "observable_id": "~o1"},
        )
    )
    assert not result.is_error
    assert "job id: ~job1" in result.content
    # first call resolves the analyzer, second creates the job
    assert recorder.calls[0]["url"].endswith("/connector/cortex/analyzer/vt")
    job_call = recorder.calls[1]
    assert job_call["url"].endswith("/connector/cortex/job")
    assert job_call["json"] == {
        "analyzerId": "vt",
        "cortexId": "CORTEX-1",
        "artifactId": "~o1",
    }


@pytest.mark.asyncio
async def test_run_analyzer_with_explicit_cortex_skips_resolution(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"_id": "~job2", "status": "Waiting"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="z2",
            name="run_thehive_analyzer",
            arguments={
                "analyzer_id": "vt",
                "observable_id": "~o1",
                "cortex_id": "CORTEX-2",
            },
        )
    )
    assert not result.is_error
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["json"]["cortexId"] == "CORTEX-2"


@pytest.mark.asyncio
async def test_list_analyzers_by_type_uses_type_path(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(id="z3", name="list_thehive_analyzers", arguments={"data_type": "ip"})
    )
    assert "No Cortex analyzers available" in result.content
    assert recorder.calls[0]["url"].endswith("/connector/cortex/analyzer/type/ip")


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_statuses_targets_alert_query(monkeypatch):
    def router(method, url):
        return [{"value": "FalsePositive", "stage": "Closed"}]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="r1", name="list_thehive_statuses", arguments={"target": "alert"})
    )
    assert not result.is_error
    assert "FalsePositive" in result.content
    ops = recorder.calls[0]["json"]["query"]
    assert ops[0] == {"_name": "listAlertStatus"}


@pytest.mark.asyncio
async def test_list_statuses_rejects_invalid_target(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(id="r2", name="list_thehive_statuses", arguments={"target": "task"})
    )
    assert "target must be" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Robustness - non-JSON responses + path-segment injection guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_json_response_returns_clear_error(monkeypatch):
    # A 2xx body that is not JSON (base_url pointing at a login page or a
    # misconfigured proxy) must surface an actionable error, not a crash.
    executor, _recorder = _build(monkeypatch, lambda m, u: _NonJsonResponse)
    for tool, args in (
        ("get_thehive_case", {"case_id": "42"}),
        ("search_thehive_cases", {}),
        ("create_thehive_case", {"title": "t", "description": "d"}),
        ("list_thehive_analyzers", {}),
    ):
        result = await executor.execute_tool(
            ToolCall(id=f"nj-{tool}", name=tool, arguments=args)
        )
        assert "not valid JSON" in result.content, tool


@pytest.mark.asyncio
async def test_unexpected_json_shape_returns_error(monkeypatch):
    # Valid JSON that is not an object (e.g. a list) on an object-returning
    # endpoint must surface an error, not a misleading empty success message.
    executor, _recorder = _build(monkeypatch, lambda m, u: [{"weird": "list"}])
    result = await executor.execute_tool(
        ToolCall(
            id="shape1",
            name="create_thehive_case",
            arguments={"title": "t", "description": "d"},
        )
    )
    assert "unexpected response shape" in result.content


@pytest.mark.asyncio
async def test_padded_case_ref_is_normalized(monkeypatch):
    # " #42 " must normalize to "42" - strip() must run before lstrip('#'),
    # otherwise the '#' survives and gets percent-encoded into the path.
    executor, recorder = _build(monkeypatch, lambda m, u: [{"_id": "~o1"}])
    await executor.execute_tool(
        ToolCall(
            id="pad1",
            name="add_thehive_observable",
            arguments={"case_id": " #42 ", "data": "1.2.3.4", "data_type": "ip"},
        )
    )
    assert recorder.calls[0]["url"].endswith("/api/v1/case/42/observable")
    await executor.execute_tool(
        ToolCall(
            id="pad2",
            name="add_thehive_comment",
            arguments={"case_id": " #42 ", "message": "note"},
        )
    )
    assert recorder.calls[1]["url"].endswith("/api/v1/case/42/comment")


@pytest.mark.asyncio
async def test_query_rejects_non_array_response(monkeypatch):
    # /api/v1/query always answers with a JSON array - an object (e.g. from a
    # rewriting proxy) must surface an error, not read as "no results".
    executor, _recorder = _build(monkeypatch, lambda m, u: {"detail": "proxied"})
    result = await executor.execute_tool(
        ToolCall(id="arr1", name="search_thehive_cases", arguments={})
    )
    assert "unexpected response shape" in result.content
    assert "JSON array" in result.content


@pytest.mark.asyncio
async def test_numeric_ids_are_coerced_to_strings(monkeypatch):
    # Tool arguments arrive without runtime type coercion, so an LLM may
    # pass case_id=42 (int) - id handling must not crash on non-strings.
    def router(method, url):
        if url.endswith("/api/v1/query"):
            return []
        return {"_id": "~1", "number": 42, "title": "t"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="int1", name="get_thehive_case", arguments={"case_id": 42})
    )
    assert not result.is_error
    assert recorder.calls[0]["url"].endswith("/api/v1/case/42")
    result = await executor.execute_tool(
        ToolCall(
            id="int2",
            name="manage_thehive_item",
            arguments={"action": "delete_task", "item_id": 7},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"].endswith("/api/v1/task/7")


@pytest.mark.asyncio
async def test_validate_connection_rejects_non_json_body(monkeypatch):
    # A 2xx HTML login/proxy page must not mark the integration as connected.
    monkeypatch.setattr(
        thehive_mod, "safe_request", _Recorder(lambda m, u: _NonJsonResponse)
    )
    integration = thehive_mod.TheHiveIntegration()
    assert await integration.validate_connection(_TOKEN) is False
    monkeypatch.setattr(
        thehive_mod, "safe_request", _Recorder(lambda m, u: {"login": "svc@corp.org"})
    )
    assert await integration.validate_connection(_TOKEN) is True


@pytest.mark.asyncio
async def test_path_segment_injection_is_encoded(monkeypatch):
    # A crafted id containing '/' must not escape its URL path position.
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="inj1",
            name="manage_thehive_item",
            arguments={"action": "delete_case", "item_id": "../alert/9"},
        )
    )
    assert not result.is_error
    assert recorder.calls[0]["url"].endswith("/api/v1/case/..%2Falert%2F9")
    await executor.execute_tool(
        ToolCall(
            id="inj2",
            name="get_thehive_alert",
            arguments={"alert_id": "~5/../case/1"},
        )
    )
    assert recorder.calls[1]["url"].endswith("/api/v1/alert/~5%2F..%2Fcase%2F1")
    # Legitimate ids (~ids, case numbers) pass through unchanged. Note:
    # get_thehive_alert made a follow-up /query call, so index 3 is next.
    await executor.execute_tool(
        ToolCall(id="inj3", name="get_thehive_case", arguments={"case_id": "#42"})
    )
    assert recorder.calls[3]["url"].endswith("/api/v1/case/42")


# ---------------------------------------------------------------------------
# Consolidated manage tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="g1",
            name="manage_thehive_item",
            arguments={"action": "nuke", "item_id": "~1"},
        )
    )
    assert "action must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_update_comment_requires_message(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="g2",
            name="manage_thehive_item",
            arguments={"action": "update_comment", "item_id": "~c1"},
        )
    )
    assert "message is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_follow_alert_posts(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="g3",
            name="manage_thehive_item",
            arguments={"action": "follow_alert", "item_id": "~555"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v1/alert/~555/follow")


@pytest.mark.asyncio
async def test_manage_delete_case_strips_hash_and_reports_permanent(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="g4",
            name="manage_thehive_item",
            arguments={"action": "delete_case", "item_id": "#42"},
        )
    )
    assert not result.is_error
    assert "permanent" in result.content
    call = recorder.calls[0]
    assert call["method"] == "delete"
    assert call["url"].endswith("/api/v1/case/42")
