"""Shared request / GraphQL helpers return their failures error-shaped.

``gh_graphql`` returned ``"GitHub GraphQL error: ..."``, ``"GitHub returned an
unexpected, empty, or non-JSON response ... retry shortly."`` and ``"GitHub
GraphQL response contained no data."`` - none matches ``is_error_shaped_result``
(``looks_like_tool_error`` reads ``startswith("graphql error")``). So both
executors stamped ``is_error=False``: the trace recorded a success, the error
brake did not count it, and for a read tool (Projects v2 is GraphQL-only) the
dedup cache STORED the failure, serving the "retry shortly" retry from the cache
for the rest of the run. The same escape existed in the OpenCRQ tRPC helper,
the OpenCTI upload helper, the Splunk job poller, the GitHub node-id / project
resolvers and Tanium's GraphQL tool. Each now uses the existing ``Error: ``
shape at the source, with the wording otherwise unchanged.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.providers import tanium
from app.integrations.providers.github import _common as gh_common
from app.integrations.providers.github import issues as gh_issues
from app.integrations.providers.github import projects as gh_projects
from app.integrations.providers.github import pulls as gh_pulls
from app.integrations.providers.opencrq import _common as crq
from app.integrations.providers.opencti import _common as octi
from app.integrations.providers.splunk import _common as splunk
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    is_auth_failure_result,
    is_auth_or_permission_error,
    is_error_shaped_result,
    is_rate_limit_error,
)


class _Resp:
    def __init__(self, body: Any = None, *, non_json: bool = False, status: int = 200) -> None:
        self._body = body
        self._non_json = non_json
        self.status_code = status
        self.is_success = 200 <= status < 300
        self.text = "boom"

    def json(self) -> Any:
        if self._non_json:
            raise ValueError("not JSON")
        return self._body


def _safe_request_returning(resp: Any):
    async def _fake(*args, **kwargs):
        return resp, None

    return _fake


def _assert_error_shaped(err: str | None) -> str:
    assert err, "the helper must report the failure"
    assert is_error_shaped_result(err) is True, err
    return err


# ---------------------------------------------------------------------------
# GitHub: gh_graphql
# ---------------------------------------------------------------------------

_NON_JSON = "GitHub returned an unexpected, empty, or non-JSON response while trying to list project items."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resp,wording",
    [
        (_Resp(non_json=True), _NON_JSON),
        (_Resp(None), _NON_JSON),
        (
            _Resp({"errors": [{"message": "Could not resolve to a PullRequest with the number of 401."}]}),
            "GitHub GraphQL error: Could not resolve to a PullRequest with the number of 401.",
        ),
        (_Resp({"data": None}), "GitHub GraphQL response contained no data."),
    ],
    ids=["non_json", "null_body", "graphql_errors", "no_data"],
)
async def test_gh_graphql_failures_are_error_shaped(monkeypatch, resp, wording):
    monkeypatch.setattr(gh_common, "safe_request", _safe_request_returning(resp))
    data, err = await gh_common.gh_graphql(None, {}, "query { x }", action="list project items")
    assert data is None
    err = _assert_error_shaped(err)
    assert err.startswith("Error: ")
    assert wording in err, "the wording is kept after the prefix"


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [401, 429])
async def test_a_graphql_not_found_naming_a_status_like_number_is_neither(monkeypatch, number):
    body = {"errors": [{"message": f"Could not resolve to a PullRequest with the number of {number}."}]}
    monkeypatch.setattr(gh_common, "safe_request", _safe_request_returning(_Resp(body)))
    _, err = await gh_common.gh_graphql(None, {}, "query { x }")
    assert is_auth_or_permission_error(err) is False
    assert is_auth_failure_result(err) is False
    assert is_rate_limit_error(err) is False


@pytest.mark.asyncio
async def test_gh_graphql_success_is_unchanged(monkeypatch):
    monkeypatch.setattr(gh_common, "safe_request", _safe_request_returning(_Resp({"data": {"viewer": {"login": "o"}}})))
    assert await gh_common.gh_graphql(None, {}, "query { viewer { login } }") == ({"viewer": {"login": "o"}}, None)


# ---------------------------------------------------------------------------
# GitHub: id resolvers built on the shared helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_pr_node_id_failure_is_error_shaped(monkeypatch):
    monkeypatch.setattr(gh_common, "safe_request", _safe_request_returning(_Resp({})))
    node_id, err = await gh_pulls._pr_node_id(None, "o", "r", 401, {})
    assert node_id is None
    assert "Could not resolve node id for PR #401." in _assert_error_shaped(err)
    assert is_auth_failure_result(err) is False


@pytest.mark.asyncio
async def test_github_issue_id_failure_is_error_shaped(monkeypatch):
    monkeypatch.setattr(gh_common, "safe_request", _safe_request_returning(_Resp({})))
    issue_id, err = await gh_issues._resolve_issue_id(None, "o", "r", 429, {})
    assert issue_id is None
    assert "Could not resolve internal ID for issue #429." in _assert_error_shaped(err)
    assert is_rate_limit_error(err) is False


@pytest.mark.asyncio
async def test_github_project_not_found_is_error_shaped(monkeypatch):
    async def _not_found(client, headers, query, variables=None, **kwargs):
        return None, "Error: GitHub GraphQL error: Could not resolve to an Organization with the login of 'octo'."

    monkeypatch.setattr(gh_common, "gh_graphql", _not_found)
    project_id, err = await gh_projects._resolve_project_id(None, {}, "", "octo", 7)
    assert project_id is None
    assert "No project #7 found for 'octo'" in _assert_error_shaped(err)


# ---------------------------------------------------------------------------
# OpenCRQ: the tRPC helper and the tenant resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resp,wording",
    [
        (None, "OpenCRQ list scenarios returned no response."),
        (_Resp(non_json=True), "OpenCRQ list scenarios returned a non-JSON response."),
        (_Resp({"error": {"message": "boom"}}), "OpenCRQ list scenarios failed: boom"),
    ],
    ids=["no_response", "non_json", "trpc_error"],
)
async def test_opencrq_trpc_failures_are_error_shaped(monkeypatch, resp, wording):
    monkeypatch.setattr(crq, "safe_request", _safe_request_returning(resp))
    data, err = await crq._trpc("https://crq.example.test", {}, "scenarios.list", "list scenarios", client=object())
    assert data is None
    assert wording in _assert_error_shaped(err)


@pytest.mark.asyncio
async def test_opencrq_trpc_success_is_unchanged(monkeypatch):
    monkeypatch.setattr(crq, "safe_request", _safe_request_returning(_Resp({"result": {"data": [1]}})))
    assert await crq._trpc("https://crq.example.test", {}, "s.list", "list", client=object()) == ([1], None)


@pytest.mark.asyncio
async def test_opencrq_no_tenant_is_error_shaped(monkeypatch):
    async def _no_tenants(*args, **kwargs):
        return [], None

    monkeypatch.setattr(crq, "_trpc", _no_tenants)
    tenant, err = await crq._resolve_tenant("https://crq.example.test", {}, "")
    assert tenant is None
    assert "No OpenCRQ tenants are accessible" in _assert_error_shaped(err)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tenants,wording",
    [
        ([{"name": "Acme", "id": ""}], "OpenCRQ returned a tenant without an id"),
        (
            [{"name": "Acme", "id": "t1"}, {"name": "Globex", "id": "t2"}],
            "Multiple OpenCRQ tenants are accessible - specify tenant_id.",
        ),
    ],
    ids=["tenant_without_id", "several_tenants"],
)
async def test_every_opencrq_tenant_failure_is_error_shaped(monkeypatch, tenants, wording):
    async def _listed(*args, **kwargs):
        return tenants, None

    monkeypatch.setattr(crq, "_trpc", _listed)
    tenant, err = await crq._resolve_tenant("https://crq.example.test", {}, "")
    assert tenant is None
    assert wording in _assert_error_shaped(err)


# ---------------------------------------------------------------------------
# OpenCTI: the upload helper
# ---------------------------------------------------------------------------


class _UploadClient:
    def __init__(self, *, raises: bool) -> None:
        self._raises = raises

    async def post(self, *args, **kwargs):
        if self._raises:
            raise httpx.ConnectError("connection refused")
        return _Resp(status=500)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raises,wording",
    [(True, "OpenCTI upload network error: connection refused"), (False, "OpenCTI upload failed (HTTP 500): boom")],
    ids=["network", "http"],
)
async def test_opencti_upload_failures_are_error_shaped(raises, wording):
    data, err = await octi._graphql_upload(
        _UploadClient(raises=raises),
        "https://octi.example.test",
        {},
        "mutation Upload($file: Upload!) { upload(file: $file) }",
        {"file": None},
        "file",
        "a.txt",
        b"x",
        "text/plain",
    )
    assert data == {}
    assert wording in _assert_error_shaped(err)


# ---------------------------------------------------------------------------
# Splunk: the search-job poller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_splunk_failed_job_is_error_shaped(monkeypatch):
    monkeypatch.setattr(splunk, "SEARCH_POLL_INTERVAL", 0)

    async def _api(method, path, action, **kwargs):
        return {"entry": [{"content": {"dispatchState": "FAILED", "isDone": True}}]}, None

    rows, err = await splunk.wait_for_job(_api, "sid-1", 10)
    assert rows is None
    assert "Splunk search job sid-1 failed (dispatchState FAILED)." in _assert_error_shaped(err)


# ---------------------------------------------------------------------------
# Tanium: the GraphQL tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tanium_graphql_errors_are_error_shaped(monkeypatch):
    async def _fake(*args, **kwargs):
        return _Resp({"errors": [{"message": "Cannot query field"}]}), None

    monkeypatch.setattr(tanium, "safe_request", _fake)
    executor = CallableToolExecutor()
    tanium.build_tools(token="https://acme-api.cloud.tanium.com||api-token-xyz", executor=executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="list_tanium_endpoints", arguments={}))
    assert result.is_error is True
    assert "Tanium GraphQL error:" in result.content


# ---------------------------------------------------------------------------
# Agent loop: the flag, the cache and the cooldowns
# ---------------------------------------------------------------------------


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0
        self.offered_tools: list[list[str]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append([t.name for t in (tools or [])])
        response = self._script[min(self._idx, len(self._script) - 1)]
        self._idx += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _call(call_id: str, name: str, arguments: dict) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
    )


async def _run(first_responses: list[Any], script: list[ChatResponse], monkeypatch) -> tuple[list[int], list[dict], _ScriptedLLM]:
    """``read_project`` calls ``gh_graphql``; its responses are served in order."""
    responses = list(first_responses)
    executed: list[int] = []

    async def _fake_safe_request(*args, **kwargs):
        return responses.pop(0) if responses else _Resp({"data": {"ok": True}}), None

    monkeypatch.setattr(gh_common, "safe_request", _fake_safe_request)

    async def read_project(project: int = 0):
        executed.append(project)
        data, err = await gh_common.gh_graphql(None, {}, "query { x }", action="list project items")
        return err or f"items: {data}"

    async def other(i: int = 0):
        return "ok"

    integration = _SafeIntegrationExecutor()
    integration.register("read_project", read_project)
    integration.register("other", other)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(name="read_project", description="read", source="integration:github"),
            ToolDefinition(name="other", description="other", source="integration:github"),
        ],
        integration,
    )
    events: list[dict] = []

    async def _on_event(event):
        events.append(event)

    llm = _ScriptedLLM(script)
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=len(script) + 2,
        on_event=_on_event,
        progressive_tools=False,
    )
    done = [e for e in events if e.get("status") == "tool_done" and e.get("tool") == "read_project"]
    return executed, done, llm


@pytest.mark.asyncio
async def test_the_retry_after_a_transient_non_json_response_runs_again(_quiet_langfuse, monkeypatch):
    """The message says "retry shortly": the identical retry must reach GitHub,
    not the dedup cache, and the failed call is recorded as a failure."""
    wait = al._RATE_LIMIT_COOLDOWN_ITERS
    script = [
        _call("c0", "read_project", {"project": 1}),
        *[_call(f"o{i}", "other", {"i": i}) for i in range(1, wait + 1)],
        _call("c1", "read_project", {"project": 1}),
        ChatResponse(content="done", model="stub"),
    ]
    executed, done, _ = await _run([_Resp(non_json=True)], script, monkeypatch)
    assert executed == [1, 1], "the identical retry was served from the cache"
    assert done[0]["success"] is False
    assert done[1]["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [401, 429])
async def test_a_graphql_not_found_naming_401_or_429_gets_no_cooldown(_quiet_langfuse, monkeypatch, number):
    body = {"errors": [{"message": f"Could not resolve to a PullRequest with the number of {number}."}]}
    script = [
        _call("c0", "read_project", {"project": number}),
        _call("o1", "other", {"i": 1}),
        ChatResponse(content="done", model="stub"),
    ]
    _, done, llm = await _run([_Resp(body)], script, monkeypatch)
    assert done[0]["success"] is False, "the GraphQL failure must be recorded as one"
    assert "read_project" in llm.offered_tools[1], "no auth / rate-limit cooldown for a PR number"
