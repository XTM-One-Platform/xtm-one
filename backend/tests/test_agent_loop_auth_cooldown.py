"""The agent loop's auth cooldown follows the anchored classifier.

Once a tool call is an error, ``_track_tool_result`` decides whether the tool
is withheld for ``_AUTH_FAILURE_COOLDOWN_ITERS`` iterations. It used the raw
substring classifier over the whole error text, so since native integrations
stamp ``is_error`` on returned error strings a genuine non-auth error
that merely QUOTES "401" or "permission denied" took the tool away for eight
iterations. The loop now asks ``is_auth_failure_result`` - the same rule as the
``is_error`` stamp - and every real auth failure shape keeps its cooldown.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers.opencti._common import (
    _describe_graphql_failure,
    _operation_names,
)
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from app.tools.mcp_client import _build_mcp_error_message

_COOLDOWN = al._AUTH_FAILURE_COOLDOWN_ITERS


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM returning a scripted sequence and recording the offered tools."""

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


@pytest.fixture(autouse=True)
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=f"{name} desc", source="integration:x")


def _call(call_id: str, name: str, arguments: dict) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
    )


class _RaisingExecutor:
    """An executor whose ``execute_tool`` itself raises: the loop wraps the
    exception as ``Error executing tool: ...``."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @property
    def tool_names(self) -> list[str]:
        return ["broken"]

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        raise self._exc


async def _offered_after_one_failure(build: Callable[[], object]) -> list[list[str]]:
    """Fail ``broken`` once at iteration 0, then keep the loop alive with
    ``other``; return the tool names offered on every iteration."""
    failing = build()

    async def _other(**kwargs):
        return "ok"

    healthy = CallableToolExecutor()
    healthy.register("other", _other)
    composite = CompositeToolExecutor()
    composite.add_tools([_tool("broken")], failing)
    composite.add_tools([_tool("other")], healthy)

    llm = _ScriptedLLM(
        [
            _call("c0", "broken", {}),
            *[_call(f"o{i}", "other", {"i": i}) for i in range(1, _COOLDOWN + 2)],
            ChatResponse(content="done", model="stub"),
        ]
    )
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=_COOLDOWN + 5,
        progressive_tools=False,
    )
    return llm.offered_tools


def _returning(executor_cls: type, value: object) -> Callable[[], object]:
    def build():
        executor = executor_cls()

        async def _broken(**kwargs):
            return value

        executor.register("broken", _broken)
        return executor

    return build


def _raising(executor_cls: type, exc: Exception) -> Callable[[], object]:
    def build():
        executor = executor_cls()

        async def _broken(**kwargs):
            raise exc

        executor.register("broken", _broken)
        return executor

    return build


def _http_error(status: int, body: dict, provider: str = "GitHub", action: str = "list repos") -> str:
    response = httpx.Response(
        status,
        request=httpx.Request("GET", "https://api.example.test/things"),
        json=body,
    )
    return format_http_error(response, provider, action)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example.test/things")
    response = httpx.Response(status, request=request, json={"message": "Bad credentials"})
    return httpx.HTTPStatusError(f"Client error '{status}'", request=request, response=response)


_ADD_LABEL = (
    "mutation AddLabel($id: ID!, $input: StixRefRelationshipAddInput!) "
    "{ stixDomainObjectEdit(id: $id) { relationAdd(input: $input) { id } } }"
)
_READ = "query StixCoreObjects { stixCoreObjects { id } }"


def _mcp_error(raw: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        content=_build_mcp_error_message("broken", Exception(raw), None),
        is_error=True,
    )


# Every shape a real auth failure reaches the loop in: it must keep the
# 8-iteration cooldown exactly as before.
_AUTH_FAILURES = {
    "format_http_error_401": _returning(_SafeIntegrationExecutor, _http_error(401, {})),
    "format_http_error_403": _returning(
        _SafeIntegrationExecutor,
        _http_error(403, {"message": "Resource not accessible by integration"}),
    ),
    "format_http_error_422_permission": _returning(
        _SafeIntegrationExecutor,
        _http_error(
            422,
            {
                "message": "Validation Failed",
                "errors": [
                    {
                        "message": "The listed users and repositories cannot be searched "
                        "either because the resources do not exist or you do not have "
                        "permission to view them."
                    }
                ],
            },
            action="search issues",
        ),
    ),
    "opencti_graphql_forbidden": _returning(
        _SafeIntegrationExecutor,
        _describe_graphql_failure(
            [{"message": "You are not allowed to do this.", "extensions": {"code": "FORBIDDEN_ACCESS"}}],
            *_operation_names(_ADD_LABEL),
            _ADD_LABEL,
        ),
    ),
    "opencti_graphql_logged_out": _returning(
        _SafeIntegrationExecutor,
        _describe_graphql_failure(
            [{"message": "You must be logged in to do this."}], *_operation_names(_READ), _READ
        ),
    ),
    "json_bad_credentials": _returning(
        _SafeIntegrationExecutor,
        {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"},
    ),
    "json_error_unauthorized": _returning(_SafeIntegrationExecutor, {"error": "Unauthorized"}),
    "raised_github_api_401": _raising(
        CallableToolExecutor, Exception("GitHub API error: 401 Unauthorized")
    ),
    "raised_in_integration_executor": _raising(
        _SafeIntegrationExecutor, PermissionError("401 Unauthorized")
    ),
    "raised_http_status_401": _raising(_SafeIntegrationExecutor, _http_status_error(401)),
    "mcp_unauthorized": _returning(CallableToolExecutor, _mcp_error("401 Unauthorized")),
    "mcp_client_error_401": _returning(
        CallableToolExecutor,
        _mcp_error(
            "Client error '401 Unauthorized' for url 'https://mcp.example.test/mcp'\n"
            "For more information check: "
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401"
        ),
    ),
    "loop_wrapped_exception": lambda: _RaisingExecutor(Exception("Unauthorized: token revoked")),
}

# Genuine non-auth errors that merely QUOTE an auth word: normal handling.
_NON_AUTH_ERRORS = {
    "format_http_error_404_detail_quotes_401": _returning(
        _SafeIntegrationExecutor,
        _http_error(404, {"message": "No comment 401 on this issue"}, "Jira", "get comment"),
    ),
    "format_http_error_404_detail_quotes_permission_denied": _returning(
        _SafeIntegrationExecutor,
        _http_error(
            404,
            {"message": "Workflow 'permission denied handler' does not exist"},
            "n8n",
            "run workflow",
        ),
    ),
    "json_error_envelope_quotes_401": _returning(
        _SafeIntegrationExecutor, {"error": True, "message": "Issue 401 not found"}
    ),
    "raised_multiline_quotes_unauthorized": _raising(
        CallableToolExecutor,
        ValueError("Cannot build the report\ncolumn 'unauthorized_logins' is missing"),
    ),
    # An id the platform writes into the statement is not a status.
    "github_502_on_pr_401": _returning(
        _SafeIntegrationExecutor, _http_error(502, {}, "GitHub", "enable auto-merge on PR #401")
    ),
    "github_502_on_project_429": _returning(
        _SafeIntegrationExecutor, _http_error(502, {}, "GitHub", "resolve project #429 for octo")
    ),
    # A detail quoted without the " Detail: " marker.
    "hubspot_400_quotes_unauthorized_property": _returning(
        _SafeIntegrationExecutor,
        _http_error(
            400,
            {"message": "Property unauthorized_access_count does not exist"},
            "HubSpot",
            "update contact",
        ),
    ),
    "opencti_graphql_quotes_unauthorized_field": _returning(
        _SafeIntegrationExecutor,
        _describe_graphql_failure(
            [{"message": 'Cannot query field "unauthorized_count" on type "Report".'}],
            *_operation_names(_READ),
            _READ,
        ),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("build", _AUTH_FAILURES.values(), ids=_AUTH_FAILURES.keys())
async def test_real_auth_failures_keep_the_auth_cooldown(build):
    offered = await _offered_after_one_failure(build)
    for iteration in range(1, _COOLDOWN + 1):
        assert "broken" not in offered[iteration], (
            f"iteration {iteration} must not offer the auth-broken tool"
        )
    assert "broken" in offered[_COOLDOWN + 1], "the cooldown must expire"


@pytest.mark.asyncio
async def test_a_secondary_rate_limit_quoted_in_a_403_detail_keeps_the_rate_limit_cooldown():
    """GitHub sends its secondary rate limit ONLY as a 403 detail, so the
    rate-limit keywords stay unanchored: the tool gets the short
    rate-limit cooldown, never the auth one."""
    build = _returning(
        _SafeIntegrationExecutor,
        _http_error(
            403,
            {"message": "You have exceeded a secondary rate limit. Please wait a few minutes."},
            "GitHub",
            "list pull requests",
        ),
    )
    offered = await _offered_after_one_failure(build)
    short = al._RATE_LIMIT_COOLDOWN_ITERS
    for iteration in range(1, short + 1):
        assert "broken" not in offered[iteration]
    assert "broken" in offered[short + 1], "a rate limit must not get the auth cooldown"


@pytest.mark.asyncio
@pytest.mark.parametrize("build", _NON_AUTH_ERRORS.values(), ids=_NON_AUTH_ERRORS.keys())
async def test_a_non_auth_error_quoting_an_auth_word_gets_no_auth_cooldown(build):
    offered = await _offered_after_one_failure(build)
    assert "broken" in offered[1], (
        "a single non-auth failure must leave the tool available on the next iteration"
    )
