"""Tests for error-shaped tool results being recorded as failures.

Provider handlers routinely CATCH an upstream failure and RETURN the
human-readable error as a NORMAL string.  Before this change
``CallableToolExecutor.execute_tool`` wrapped that string in a ``ToolResult``
with the default ``is_error=False``, so every OpenCTI GraphQL failure was
recorded as a **success**: the Langfuse span said ``is_error=False``, the
``tool_done`` UI event said ``success=True``, and the loop's per-tool
``tool_error_counts`` never incremented — which also meant the
consecutive-error brake never tripped.

``app.tools.base.is_error_shaped_result`` (previously private to
``agent_loop`` and consulted only by the dedup cache) is now applied at the
executor choke point, so the fix covers every native provider rather than one
handler at a time.

The OpenCTI half of the issue is covered too: a GraphQL error names the
operation that failed (``add_opencti_label`` makes three GraphQL calls) and,
for permission failures, the capability the token's role is missing; and a
loop-over-ids tool that partially failed no longer reports a plain success.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.opencti import _common as opencti_common
from app.integrations.providers.opencti._common import (
    _describe_graphql_failure,
    _graphql,
    _operation_names,
    _required_capability,
    format_bulk_outcome,
)
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
)

# The three failures from the reported traces (90bcc5ce…, 024f20bd…,
# e3d8a383…), each of which was displayed as a successful tool call.
_TRACE_GRAPHQL_ERRORS = (
    "GraphQL error in 'AddLabel': You are not allowed to do this.",
    'GraphQL error in \'EntityLogs\': Cannot query field "references" on '
    'type "ContextData".',
    "GraphQL error in 'StixCoreObjects': You must be logged in to do this.",
    "Error: OpenCTI server error (500) while trying to execute GraphQL "
    "operation 'ReadEntity'.",
)


# ─────────────────────────────────────────────────────────────────────────
# Unit: the classifier
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("content", _TRACE_GRAPHQL_ERRORS)
def test_trace_failures_are_error_shaped(content):
    assert is_error_shaped_result(content) is True


@pytest.mark.parametrize(
    "content,expected",
    [
        ("GraphQL error in 'AddLabel': nope", True),
        ("GraphQL upload error in 'UploadFile': nope", True),
        ("Partial failure: 2 of 3 object(s) added to container c1, 1 failed.", True),
        # Benign successes must NOT be reclassified as failures — a false
        # positive here mislabels a real result on the trace and feeds the
        # consecutive-error brake.
        ("Added 3 object(s) to container c1: a, b, c", False),
        ("GraphQL schema for Report has 42 fields", False),
        ("Error handling best practices for OpenCTI connectors", False),
        ("No objects were added.", False),
        ("", False),
    ],
)
def test_error_shaped_classifier(content, expected):
    assert is_error_shaped_result(content) is expected


@pytest.mark.parametrize(
    "content,expected",
    [
        # A boolean ``error`` is a status flag, wherever it sits in the object
        # — ``json.dumps`` preserves insertion order, so the same tool emits
        # both of these shapes and a prefix test would only catch the first.
        ('{"error": true, "message": "Unknown test \'nope\'"}', True),
        ('{"operation": "growth_rate", "error": true, "message": "undefined"}', True),
        # A STRING ``error`` is a data field describing the thing returned, not
        # the call's own status: ``get_trace_payload`` reports a historical
        # offload failure this way while itself succeeding.
        (
            '{"trace_id": "t1", "unavailable": true, '
            '"error": "object storage unavailable", "content": "excerpt"}',
            False,
        ),
        ('{"error": false, "score": 1.0}', False),
        ('{"error": null, "score": 1.0}', False),
        ('{"errors": [], "score": 1.0}', False),
        ('{"error_count": 0, "score": 1.0}', False),
        # Not an object, or not JSON at all.
        ('[{"error": true}]', False),
        ("{not json", False),
    ],
)
def test_json_error_envelope_classifier(content, expected):
    assert is_error_shaped_result(content) is expected


@pytest.mark.parametrize(
    "content",
    [
        # The reported regression: a keyless ``get_vulnintel_status`` SUCCESS
        # whose closing tip mentions the NVD rate limit.
        "NVD CVE API 2.0: OK\nCISA KEV catalog: OK\nEPSS: OK\n"
        "Tip: an NVD API key raises the NVD rate limit 10x.",
        # The same class: a successful result that merely talks about limits.
        "Found 3 articles about API rate limit design.",
        "Report complete. 429 findings triaged, none blocking.",
        # A support ticket ABOUT a rate limit, and a Notion query
        # listing it - data quoting the failure statement below their first line.
        "Subject: GitHub API Rate Limit [FS2-2433]\nStatus: Waiting for customer\n"
        'Content: Invoke-WebRequest : {"message":"API rate limit exceeded for 194.206.23.33."}',
        "\u2022 Untitled\n  ID: 3fe8fce1\n  Preview: the inject fails with 429 Too Many Requests "
        "from api.github.com",
    ],
)
def test_benign_rate_limit_mentions_are_not_errors(content):
    """Rate-limit phrasing is only error-shaped when it IS the message.

    ``is_rate_limit_error`` searches the whole string, which is right where it
    classifies a short error but wrong for a full tool result: mislabelling a
    success is the costly direction (trace, per-tool metrics, error brake).
    """
    assert is_error_shaped_result(content) is False


@pytest.mark.parametrize(
    "content",
    [
        "Error: GitHub rate limit exceeded while trying to list repos.",
        "429 Too Many Requests",
        "Rate limit exceeded. Wait a moment and try again.",
        # The statement embedded in the first line, data below it.
        "GitHub API error: 429 Too Many Requests\nRetry-After: 60",
        # A raised error the MCP client wrapped: the upstream message is the
        # first paragraph after the wrapper.
        "Tool 'search' failed with error:\nUpstream rate limit exceeded\n\nHint: wait before retrying.",
    ],
)
def test_rate_limit_errors_are_still_caught(content):
    assert is_error_shaped_result(content) is True


@pytest.mark.parametrize(
    "content",
    [
        # Successful results that merely MENTION an auth failure.
        "Created issue #401: Fix login redirect",
        "PROJ-401 Update the onboarding checklist (status: Done)",
        "Indicators matched: 401",
        "Report: APT29 gained unauthorized access to the domain controller "
        "via a phishing lure.",
        "CVE-2026-1234 allows remote attackers to bypass access control "
        "(Access Denied page is not enforced).",
        "Found 3 issues:\n- #12 Permission denied when saving\n- #13 Dark mode\n"
        "- #14 Crash on start",
        "Access Denied malware family: loader analysis\nThe sample drops ...",
        "Unauthorized access attempts were blocked by the WAF on 12 hosts.\n"
        "Top sources: 203.0.113.7, 198.51.100.4",
        '[{"title": "Unauthorized sign-in attempt", "severity": "high"}]',
        # Review follow-ups: a result that COUNTS errors, and titles that
        # merely start with a failure phrase.
        "Found 3 401 errors in the scan",
        "Processed 401 errors successfully",
        "Bad credentials management guide",
        "Provider: insufficient scopes documentation",
        # A success that ECHOES a title carrying a failure statement: YouTrack's
        # create result is "Issue created: <id> — <summary>".
        "Issue created: SEC-12 \u2014 Access denied error (403) on the admin page",
        "Issue created: SEC-12 \u2014 Login failed (401) for SSO users",
        # A structured success: the executor JSON-dumps a returned dict, and a
        # message that merely names an issue or an alert is data.
        '{"status": "ok", "message": "Comment added to PROJ-401"}',
        '{"detail": "Alert \'Unauthorized sign-in\' acknowledged"}',
        # A record whose first FIELD holds the phrase: a labelled lead is a
        # message only when the result is that one short line.
        "Title: Access denied - troubleshooting guide\nSpace: IT\nUpdated: 2026-09-01",
    ],
)
def test_benign_auth_mentions_are_not_errors(content):
    """An auth word inside DATA is not the call failing: stamping it
    records a success as an error and puts the tool on an auth cooldown."""
    assert is_auth_failure_result(content) is False
    assert is_error_shaped_result(content) is False


@pytest.mark.parametrize(
    "content",
    [
        "401 Unauthorized",
        "Unauthorized",
        "Unauthorized: token rejected by the upstream API",
        "Bad credentials",
        "Requires authentication",
        "Access denied.",
        "Permission denied (publickey).",
        "GitHub API error: 401",
        "Request rejected (401)",
        "Tool execution failed: GitHub API error: 401 Unauthorized",
        "Validation Failed (422): you do not have permission to view them",
        "Notion: insufficient permissions to read this page",
        "Error: HubSpot access denied while trying to list campaigns - "
        "insufficient scopes. Required scopes: content.",
        '{"message": "Bad credentials", '
        '"documentation_url": "https://docs.github.com/rest"}',
        '{"detail": "Invalid token."}',
        '{"error": "Unauthorized", "statusCode": 401}',
        # An em / en dash ends the phrase like a hyphen does (the providers'
        # own auth messages use one).
        "Authentication failed \u2014 token may be expired",
        "Access denied \u2013 check the token scopes",
    ],
)
def test_auth_failures_are_still_caught(content):
    assert is_auth_failure_result(content) is True
    assert is_error_shaped_result(content) is True


_JIRA_404_QUOTING_401 = (
    "Error: Jira resource not found while trying to get comment. "
    "Detail: No comment 401 on this issue"
)


@pytest.mark.parametrize(
    "content,expected",
    [
        # A raised error the platform wrapped: the first paragraph is the
        # upstream message, the paragraphs below are hints.
        (
            "Tool 'search' failed with error: 401 Unauthorized\n\n"
            "Detected an authentication/permission failure.",
            True,
        ),
        (
            "Tool 'search' failed with error: invalid filter expression\n\n"
            "Parameter reference: status (integer) - HTTP status, e.g. 401",
            False,
        ),
        ("Error executing tool 'get_issue': 401 Unauthorized", True),
        ("Error executing tool: Unauthorized: token revoked", True),
        (
            "Error executing tool 'x': Client error '401 Unauthorized' for url "
            "'https://api.example.test/x'",
            True,
        ),
        # A short raised message that only NAMES an auth word is data.
        ("Error executing tool 'x': Cannot build report: column unauthorized_count is missing", False),
        # A provider GraphQL error: the quoted server messages decide.
        ('Error: GitHub GraphQL error: Cannot query field "unauthorized_count" on type "Repository".', False),
        ("Error: Tanium GraphQL error: [{'message': 'Cannot query field unauthorized_count'}]", False),
        ("Error: GitHub GraphQL error: Could not resolve to a Repository; Bad credentials", True),
        ("Error: Tanium GraphQL error: [{'message': 'Unauthorized', 'path': ['sensors']}]", True),
        # format_http_error: the statement decides, not the quoted detail.
        (_JIRA_404_QUOTING_401, False),
        (
            "Error: GitHub access denied while trying to list repos - insufficient "
            "permissions or scopes. Detail: Resource not accessible by integration",
            True,
        ),
    ],
)
def test_auth_failure_result_reads_the_error_statement(content, expected):
    assert is_auth_failure_result(content) is expected


def _formatted(status: int, body: dict, provider: str, action: str) -> str:
    import httpx

    from app.integrations.http import format_http_error

    response = httpx.Response(
        status, request=httpx.Request("POST", "https://api.example.test/x"), json=body
    )
    return format_http_error(response, provider, action)


_OPENCTI_READ = "query Reports { reports { edges { node { id } } } }"
_OPENCTI_ADD_LABEL = (
    "mutation AddLabel($id: ID!, $input: StixRefRelationshipAddInput!) "
    "{ stixDomainObjectEdit(id: $id) { relationAdd(input: $input) { id } } }"
)


@pytest.mark.parametrize(
    "content",
    [
        # format_http_error quotes the provider's detail WITHOUT the
        # " Detail: " marker for 400, 422 and every other status.
        _formatted(
            400,
            {"message": "Property unauthorized_access_count does not exist", "category": "VALIDATION_ERROR"},
            "HubSpot",
            "update contact",
        ),
        _formatted(422, {"message": "Field 'access denied reason' is invalid"}, "GitHub", "create issue"),
        _formatted(409, {"message": "Workflow 'permission denied handler' is locked"}, "n8n", "run workflow"),
        # An OpenCTI GraphQL error quotes the server's messages.
        _describe_graphql_failure(
            [{"message": 'Cannot query field "unauthorized_count" on type "Report".'}],
            *_operation_names(_OPENCTI_READ),
            _OPENCTI_READ,
        ),
    ],
    ids=["400_bad_request", "422_validation", "409_fallback", "opencti_graphql"],
)
def test_a_quoted_detail_is_not_the_error_statement(content):
    assert is_auth_failure_result(content) is False
    assert is_error_shaped_result(content) is True


@pytest.mark.parametrize(
    "content",
    [
        # GitHub's 422 permission failure in disguise stays an auth failure.
        _formatted(
            422,
            {
                "message": "Validation Failed",
                "errors": [
                    {
                        "message": "The listed users and repositories cannot be searched either "
                        "because the resources do not exist or you do not have permission to view them."
                    }
                ],
            },
            "GitHub",
            "search issues",
        ),
        # A detail that itself STATES the failure still counts.
        _formatted(400, {"message": "Invalid token."}, "Acme", "list things"),
        # OpenCTI forbidden (capability hint) and logged-out (re-authorize hint).
        _describe_graphql_failure(
            [{"message": "You are not allowed to do this.", "extensions": {"code": "FORBIDDEN_ACCESS"}}],
            *_operation_names(_OPENCTI_ADD_LABEL),
            _OPENCTI_ADD_LABEL,
        ),
        _describe_graphql_failure(
            [{"message": "You must be logged in to do this."}],
            *_operation_names(_OPENCTI_READ),
            _OPENCTI_READ,
        ),
    ],
    ids=["github_422_permission", "400_states_invalid_token", "opencti_forbidden", "opencti_logged_out"],
)
def test_a_real_auth_failure_behind_a_quoted_detail_still_counts(content):
    assert is_auth_failure_result(content) is True


def test_a_quoted_auth_word_does_not_change_the_is_error_stamp():
    """Only the AUTH verdict reads the statement: the 404 is still an error."""
    assert is_error_shaped_result(_JIRA_404_QUOTING_401) is True


def test_rate_limit_phrasing_is_never_an_auth_failure_result():
    assert is_auth_failure_result(
        "Permission Denied: You have exceeded a secondary rate limit."
    ) is False


def test_head_scan_requires_a_top_level_error_member():
    """The oversized-body fallback must not match a NESTED ``error: true`` —
    a large result whose first field describes its DATA is still a success."""
    pad = '"x"' + ": " + '"' + "y" * (64 * 1024) + '"'
    nested = '{"summary": {"error": true, "n": 1}, ' + pad + "}"
    assert len(nested) > 64 * 1024
    assert is_error_shaped_result(nested) is False
    # Same size, but the flag IS the top-level status.
    top = '{"error": true, ' + pad + "}"
    assert is_error_shaped_result(top) is True
    # Top-level, but preceded by complete scalar members.
    later = '{"operation": "growth_rate", "n": 2, "error": true, ' + pad + "}"
    assert is_error_shaped_result(later) is True


def test_partial_failure_wording_stays_in_sync_with_the_classifier():
    """``format_bulk_outcome``'s lead-ins are load-bearing for the classifier.

    A future wording tweak would silently un-flag partial failures, so pin the
    coupling rather than trusting the two files to be edited together.
    """
    from app.tools.base import _ERROR_PREFIXES

    partial = format_bulk_outcome("Added", "add", "to c1", ["a"], ["b: nope"])
    total = format_bulk_outcome("Added", "add", "to c1", [], ["b: nope"])
    for produced in (partial, total):
        assert produced.lower().startswith(_ERROR_PREFIXES), (
            f"{produced!r} no longer starts with a recognised error prefix — "
            "update _ERROR_PREFIXES or format_bulk_outcome together"
        )


def test_oversized_bodies_fall_back_to_a_head_scan():
    """A body too large to parse on every tool call is still classified from
    its top-level members, which sit in the first few hundred characters."""
    big = '{"error": true, "pad": "' + "x" * (64 * 1024) + '"}'
    assert is_error_shaped_result(big) is True
    # A nested marker far past the head must NOT match a successful payload.
    ok = '{"rows": [' + '{"n": 1},' * 9000 + '{"error": true}]}'
    assert len(ok) > 64 * 1024
    assert is_error_shaped_result(ok) is False


# ─────────────────────────────────────────────────────────────────────────
# Executor: is_error is stamped from the returned string
# ─────────────────────────────────────────────────────────────────────────


# Both executors: ``_SafeIntegrationExecutor`` (every native integration)
# overrides ``execute_tool`` and never carried the stamp.
_EXECUTORS = pytest.mark.parametrize(
    "executor_cls",
    [CallableToolExecutor, _SafeIntegrationExecutor],
    ids=["callable", "safe_integration"],
)


@pytest.mark.asyncio
@_EXECUTORS
@pytest.mark.parametrize("content", _TRACE_GRAPHQL_ERRORS)
async def test_executor_flags_caught_and_returned_errors(executor_cls, content):
    """A handler that returns (rather than raises) an error is a failure."""
    executor = executor_cls()

    async def _handler():
        return content

    executor.register("t", _handler)
    result = await executor.execute_tool(ToolCall(id="c1", name="t", arguments={}))
    assert result.is_error is True
    assert result.content == content


@pytest.mark.asyncio
@_EXECUTORS
async def test_executor_leaves_successful_results_unflagged(executor_cls):
    executor = executor_cls()

    async def _handler():
        return "Added 3 object(s) to container c1: a, b, c"

    executor.register("t", _handler)
    result = await executor.execute_tool(ToolCall(id="c1", name="t", arguments={}))
    assert result.is_error is False


@pytest.mark.asyncio
async def test_integration_executor_flags_a_returned_http_error():
    """``format_http_error`` is what ``safe_request`` callers return."""
    import httpx

    from app.integrations.http import format_http_error

    response = httpx.Response(
        403,
        request=httpx.Request("GET", "https://api.example.test/things"),
        json={"message": "Resource not accessible by integration"},
    )
    content = format_http_error(response, "list_things", "list things")
    executor = _SafeIntegrationExecutor()

    async def _handler():
        return content

    executor.register("list_things", _handler)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_things", arguments={})
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_integration_executor_leaves_a_success_mentioning_401_unflagged():
    executor = _SafeIntegrationExecutor()

    async def _handler():
        return "Created issue #401: Fix the unauthorized redirect on login"

    executor.register("create_github_issue", _handler)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="create_github_issue", arguments={})
    )
    assert result.is_error is False


@pytest.mark.asyncio
async def test_integration_executor_leaves_an_echoed_failure_title_unflagged():
    """YouTrack's create result echoes the summary: a ticket ABOUT a login
    failure is a successful write, not an auth failure of the call."""
    executor = _SafeIntegrationExecutor()

    async def _handler():
        return "Issue created: SEC-12 \u2014 Login failed (401) for SSO users"

    executor.register("create_youtrack_issue", _handler)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="create_youtrack_issue", arguments={})
    )
    assert result.is_error is False


@pytest.mark.asyncio
@_EXECUTORS
async def test_executor_leaves_a_structured_success_mentioning_401_unflagged(
    executor_cls,
):
    """A returned dict is JSON-dumped by the executor; a success message that
    names ``PROJ-401`` is data, not a JSON auth-error body."""
    executor = executor_cls()

    async def _handler():
        return {"status": "ok", "message": "Comment added to PROJ-401"}

    executor.register("t", _handler)
    result = await executor.execute_tool(ToolCall(id="c1", name="t", arguments={}))
    assert result.is_error is False


@pytest.mark.asyncio
@_EXECUTORS
async def test_executor_respects_an_explicit_tool_result(executor_cls):
    """A handler returning a ``ToolResult`` keeps its own flag — the
    classifier only fills in for plain string/JSON returns."""
    from app.tools.base import ToolResult

    executor = executor_cls()

    async def _handler():
        return ToolResult(tool_call_id="", content="Error: masked", is_error=False)

    executor.register("t", _handler)
    result = await executor.execute_tool(ToolCall(id="c1", name="t", arguments={}))
    assert result.is_error is False


# ─────────────────────────────────────────────────────────────────────────
# Agent loop: span, event and error counters agree with the executor
# ─────────────────────────────────────────────────────────────────────────


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM returning a pre-scripted sequence of responses."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        response = self._script[min(self._idx, len(self._script) - 1)]
        self._idx += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _tool_call_response(*calls: ToolCall) -> ChatResponse:
    return ChatResponse(
        content="", model="stub", stop_reason="tool_calls", tool_calls=list(calls)
    )


@pytest.fixture
def _recorded_spans(monkeypatch):
    """Neutralise Langfuse plumbing, recording each tool span's error flag."""
    spans: list[bool] = []

    def _end_tool_span(_ctx, output=None, is_error=False, **kwargs):
        spans.append(bool(is_error))

    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", _end_tool_span)
    return spans


@pytest.mark.asyncio
async def test_graphql_error_reaches_span_event_and_error_counts(_recorded_spans):
    """The end-to-end guarantee: a handler returning ``"GraphQL error: …"``
    is an error on the trace span, on the ``tool_done`` event, and in the
    loop's ``tool_error_counts`` (observable through the repeated-failure
    nudge that counter drives)."""
    error = "GraphQL error in 'AddLabel': You are not allowed to do this."

    executor = CallableToolExecutor()

    async def _handler(**kwargs):
        return error

    executor.register("add_opencti_label", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="add_opencti_label",
                description="add a label",
                source="integration:opencti",
            )
        ],
        executor,
    )

    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="add_opencti_label", arguments={"a": 1})
            ),
            _tool_call_response(
                ToolCall(id="c2", name="add_opencti_label", arguments={"a": 2})
            ),
            ChatResponse(content="Could not label it.", model="stub"),
        ]
    )
    events: list[dict] = []

    async def _on_event(event):
        events.append(event)

    messages: list[ChatMessage] = [ChatMessage(role="user", content="label it")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        on_event=_on_event,
    )

    # 1. Trace span.
    assert _recorded_spans == [True, True]

    # 2. ``tool_done`` UI event.
    done = [e for e in events if e.get("status") == "tool_done"]
    assert len(done) == 2
    assert all(e["success"] is False for e in done)

    # 3. ``tool_error_counts`` — two consecutive failures reach
    #    ``_MAX_TOOL_FAILURES``, which injects the repeated-failure nudge.
    nudges = [
        m
        for m in messages
        if getattr(m, "role", None) == "system"
        and "add_opencti_label" in (getattr(m, "content", "") or "")
        and "failed" in (getattr(m, "content", "") or "")
    ]
    assert nudges, "tool_error_counts did not reach the repeated-failure nudge"


# ─────────────────────────────────────────────────────────────────────────
# OpenCTI: the error says WHICH call failed and WHICH capability is missing
# ─────────────────────────────────────────────────────────────────────────


_FIND_LABEL = """
query FindLabel($filters: FilterGroup) {
    labels(filters: $filters, first: 1) { edges { node { id value } } }
}
"""
_CREATE_LABEL = """
mutation CreateLabel($input: LabelAddInput!) { labelAdd(input: $input) { id value } }
"""
_ADD_LABEL = """
mutation AddLabel($id: ID!, $input: StixRefRelationshipAddInput!) {
    stixDomainObjectEdit(id: $id) { relationAdd(input: $input) { id } }
}
"""
_ANONYMOUS_TRIGGER = (
    "mutation($input: TriggerLiveAddInput!) { triggerLiveAdd(input: $input) { id } }"
)


@pytest.mark.parametrize(
    "query,operation,root_field",
    [
        (_FIND_LABEL, "FindLabel", "labels"),
        (_CREATE_LABEL, "CreateLabel", "labelAdd"),
        (_ADD_LABEL, "AddLabel", "stixDomainObjectEdit"),
        # Anonymous mutations (notifications.py) fall back to the root field.
        (_ANONYMOUS_TRIGGER, "triggerLiveAdd", "triggerLiveAdd"),
    ],
)
def test_operation_names(query, operation, root_field):
    assert _operation_names(query) == (operation, root_field)


@pytest.mark.parametrize(
    "root_field,query,capability",
    [
        ("labelAdd", _CREATE_LABEL, "SETTINGS_SETLABELS"),
        ("stixDomainObjectEdit", _ADD_LABEL, "KNOWLEDGE_KNUPDATE"),
        ("userEdit", "mutation UserPatch { userEdit { id } }", "SETTINGS_SETACCESSES"),
        (
            "stixCoreObjectEdit",
            "mutation DeleteEntity($id: ID!) { stixCoreObjectEdit(id: $id) { delete } }",
            "KNOWLEDGE_KNUPDATE_KNDELETE",
        ),
        ("stixCoreObjects", "query StixCoreObjects { stixCoreObjects { id } }", "KNOWLEDGE"),
        # Unmapped families report no capability rather than guessing one.
        ("triggerLiveAdd", _ANONYMOUS_TRIGGER, None),
        # ``logs`` serves both the security-activity trail and plain entity
        # history, so it is deliberately unmapped.
        ("logs", "query EntityLogs { logs { edges } }", None),
    ],
)
def test_required_capability(root_field, query, capability):
    assert _required_capability(root_field, query) == capability


def test_permission_error_names_the_step_and_the_capability():
    """``add_opencti_label`` makes three GraphQL calls; the analyst must be
    able to tell which one was refused and what the token is missing."""
    errors = [
        {
            "message": "You are not allowed to do this.",
            "extensions": {"code": "FORBIDDEN_ACCESS"},
        }
    ]
    message = _describe_graphql_failure(
        errors, *_operation_names(_CREATE_LABEL), _CREATE_LABEL
    )
    assert message.startswith("GraphQL error in 'CreateLabel':")
    assert "FORBIDDEN_ACCESS" in message
    assert "SETTINGS_SETLABELS" in message
    assert "list_opencti_capabilities" in message
    # Recorded as a failure, and as a permanent one (auth cooldown).
    assert is_error_shaped_result(message) is True
    assert is_auth_or_permission_error(message) is True


def test_rejected_token_is_distinguished_from_a_missing_capability():
    """"You must be logged in" needs re-authorization, not a new capability —
    the two remediations must never be collapsed."""
    message = _describe_graphql_failure(
        [{"message": "You must be logged in to do this."}],
        "StixCoreObjects",
        "stixCoreObjects",
        "query StixCoreObjects { stixCoreObjects { id } }",
    )
    assert "re-authorize" in message
    assert "capability" not in message
    assert is_auth_or_permission_error(message) is True


def test_non_permission_error_gets_no_remediation_hint():
    """A schema error is not a permission problem and must not be dressed up
    as one — it would trip the loop's long auth cooldown."""
    message = _describe_graphql_failure(
        [{"message": 'Cannot query field "references" on type "ContextData".'}],
        "EntityLogs",
        "logs",
        "query EntityLogs { logs { edges } }",
    )
    assert message == (
        "GraphQL error in 'EntityLogs': Cannot query field \"references\" on "
        'type "ContextData".'
    )
    assert is_error_shaped_result(message) is True
    assert is_auth_or_permission_error(message) is False


def test_extension_data_is_preserved():
    """``extensions.data`` is the only server-provided clue about WHAT was
    refused, so it must survive into the message."""
    message = _describe_graphql_failure(
        [
            {
                "message": "You are not allowed to do this.",
                "extensions": {
                    "code": "FORBIDDEN_ACCESS",
                    "data": {"capability": "SETTINGS_SETACCESSES"},
                },
            }
        ],
        "UserPatch",
        "userEdit",
        "mutation UserPatch { userEdit { id } }",
    )
    assert '"capability": "SETTINGS_SETACCESSES"' in message


@pytest.mark.asyncio
async def test_graphql_names_the_operation_in_the_http_action(monkeypatch):
    """The HTTP-level action passed to ``safe_request`` names the operation
    too, so a 500 is attributable to a step as well (the reported
    ``create_opencti_entity`` follow-up reads)."""
    seen: dict[str, str] = {}

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        seen["action"] = action
        return None, f"Error: {provider} server error (500) while trying to {action}."

    monkeypatch.setattr(opencti_common, "safe_request", _fake_safe_request)
    data, err = await _graphql(None, "https://octi.test", {}, _ADD_LABEL, {})
    assert data == {}
    assert seen["action"] == "execute GraphQL operation 'AddLabel'"
    assert "AddLabel" in err
    assert is_error_shaped_result(err) is True


@pytest.mark.asyncio
async def test_graphql_formats_graphql_level_errors(monkeypatch):
    class _Resp:
        def json(self):
            return {
                "errors": [
                    {
                        "message": "You are not allowed to do this.",
                        "extensions": {"code": "FORBIDDEN_ACCESS"},
                    }
                ]
            }

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        return _Resp(), None

    monkeypatch.setattr(opencti_common, "safe_request", _fake_safe_request)
    data, err = await _graphql(None, "https://octi.test", {}, _ADD_LABEL, {})
    assert data == {}
    assert err.startswith("GraphQL error in 'AddLabel':")
    assert "KNOWLEDGE_KNUPDATE" in err


# ─────────────────────────────────────────────────────────────────────────
# OpenCTI: partial failure in a loop-over-ids tool
# ─────────────────────────────────────────────────────────────────────────


def test_partial_failure_is_reported_as_an_error():
    out = format_bulk_outcome(
        "Added",
        "add",
        "to container c1",
        ["a", "b"],
        ["c: GraphQL error in 'AddToContainer': You are not allowed to do this."],
    )
    assert out.startswith("Partial failure: 2 of 3 object(s) added to container c1")
    assert is_error_shaped_result(out) is True
    # The ids that DID succeed stay fully visible — the point is honesty
    # about the failure, not hiding the successes.
    assert "Added (2): a, b" in out
    assert "AddToContainer" in out


def test_total_failure_is_reported_as_an_error():
    out = format_bulk_outcome("Removed", "remove", "from container c1", [], ["a: nope"])
    assert out.startswith("Failed to remove 1 object(s) from container c1.")
    assert is_error_shaped_result(out) is True


def test_full_success_keeps_its_plain_shape():
    out = format_bulk_outcome("Added", "add", "to container c1", ["a", "b", "c"], [])
    assert out == "Added 3 object(s) to container c1: a, b, c"
    assert is_error_shaped_result(out) is False


def test_empty_input_is_not_an_error():
    out = format_bulk_outcome("Added", "add", "to container c1", [], [])
    assert out == "No objects were added."
    assert is_error_shaped_result(out) is False
