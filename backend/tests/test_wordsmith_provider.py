"""Wordsmith provider: coverage of the documented API, effects, safety."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers import wordsmith
from app.integrations.providers.wordsmith import (
    build_tools,
    redact_secrets,
    resolve_base_url,
    split_token,
    validate_connection,
)
from app.integrations.providers.wordsmith.operations import GROUPS, IDEMPOTENT_STARTS, covered_routes
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import _PERSIST_FILE_PROVIDERS, PROVIDER_TOOLS

MATRIX = Path(__file__).resolve().parents[2] / "dev-docs" / "reference" / "wordsmith-api-coverage.md"


def _resp(status: int, payload: object | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    content = b"" if payload is None else json.dumps(payload).encode()
    return httpx.Response(status, content=content, headers=headers or {}, request=httpx.Request("GET", "https://x"))


@asynccontextmanager
async def _client(*_a: Any, **_k: Any) -> AsyncIterator[MagicMock]:
    yield MagicMock()


TEST_KEY = "sk-ws-api1-test"


def _tools(token: str = TEST_KEY) -> tuple[dict[str, Any], dict[str, Callable[..., Any]]]:
    executor = MagicMock()
    defs = build_tools(token, executor)
    handlers = {call.args[0]: call.args[1] for call in executor.register.call_args_list}
    return {d.name: d for d in defs}, handlers


@pytest.fixture
def http() -> Iterator[AsyncMock]:
    mock = AsyncMock()
    with (
        patch.object(wordsmith, "safe_request", mock),
        patch.object(wordsmith, "egress_client", _client),
    ):
        yield mock


# ── Coverage ────────────────────────────────────────────────────────────


def _matrix_rows() -> list[tuple[str, str]]:
    rows = []
    for line in MATRIX.read_text().splitlines():
        match = re.match(r"\| [^|]+ \| (GET|POST|PUT|PATCH|DELETE) \| `/api/v1([^`]+)` \|", line)
        if match:
            rows.append((match.group(1), match.group(2)))
    return rows


def test_every_documented_operation_reaches_a_tool() -> None:
    routes = covered_routes()
    rows = _matrix_rows()
    assert len(rows) >= 152
    missing = [row for row in rows if row not in routes]
    assert missing == []
    # and the table holds nothing the matrix does not document
    assert set(routes) - set(rows) == set()


def test_operation_names_unique_per_tool_and_tools_unique() -> None:
    tools = [group.tool for group in GROUPS]
    assert len(tools) == len(set(tools))
    for group in GROUPS:
        names = [op.operation for op in group.operations]
        assert len(names) == len(set(names)), group.tool


def test_registry_and_catalog() -> None:
    assert "wordsmith" in PROVIDER_TOOLS
    assert "wordsmith" in _PERSIST_FILE_PROVIDERS
    entry: dict[str, Any] = PROVIDER_CATALOG["wordsmith"]  # type: ignore[assignment]
    assert entry["logo"] == "/logos/wordsmith.svg"
    assert entry["integration_types"] == ["legal"]
    assert entry["auth_type"] == "api_key"
    assert (Path(__file__).resolve().parents[2] / "frontend" / "public" / "logos" / "wordsmith.svg").exists()


# ── Read and write split ────────────────────────────────────────────────


def test_read_groups_only_read_and_write_groups_are_withheld_read_only() -> None:
    defs, _ = _tools()
    for group in GROUPS:
        definition = defs[group.tool]
        if group.access == "read":
            assert definition.read_only, group.tool
            for op in group.operations:
                # a POST in a read group must only search or validate
                assert op.method == "GET" or op.operation.startswith(("search", "validate")), op
        else:
            assert not definition.read_only
            assert integration_tool_access(group.tool) == "write", group.tool
    for name in ("send_wordsmith_agent_question", "upload_wordsmith_file", "wordsmith_api_request"):
        assert not defs[name].read_only
        assert integration_tool_access(name) == "write", name
    for name in (
        "get_wordsmith_me",
        "wait_wordsmith_operation",
        "download_wordsmith_file",
        "get_wordsmith_api_request",
    ):
        assert defs[name].read_only


@pytest.mark.asyncio
async def test_generic_read_tool_refuses_other_methods(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (_resp(200, {"ok": True}), None)
    await handlers["get_wordsmith_api_request"](path="/api/v1/tasks", query={"limit": 2})
    method = http.call_args.args[1]
    assert method == "get"
    assert http.call_args.args[2] == "https://api.wordsmith.ai/api/v1/tasks"


@pytest.mark.asyncio
async def test_generic_request_refuses_foreign_urls(http: AsyncMock) -> None:
    _, handlers = _tools()
    answer = await handlers["wordsmith_api_request"](method="GET", path="https://evil.example/x")
    assert answer.startswith("Error")
    answer = await handlers["wordsmith_api_request"](method="GET", path="/tasks/../../me")
    assert answer.startswith("Error")
    http.assert_not_called()


# ── Grouped tools ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_tool_fills_path_and_query(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (_resp(200, {"data": []}), None)
    answer = await handlers["get_wordsmith_tasks"](
        operation="list_comments", task_id="t1", query={"limit": 5, "mine": True}
    )
    args, kwargs = http.call_args
    assert args[1] == "get"
    assert args[2] == "https://api.wordsmith.ai/api/v1/tasks/t1/comments"
    assert kwargs["params"] == {"limit": 5, "mine": "true"}
    assert kwargs["headers"]["Authorization"] == "Bearer sk-ws-api1-test"
    assert "UNTRUSTED" in answer


@pytest.mark.asyncio
async def test_group_tool_refuses_missing_or_path_like_ids(http: AsyncMock) -> None:
    _, handlers = _tools()
    assert "needs task_id" in await handlers["manage_wordsmith_tasks"](operation="delete_task")
    assert "must be an id" in await handlers["manage_wordsmith_tasks"](operation="delete_task", task_id="../me")
    assert "unknown operation" in await handlers["get_wordsmith_tasks"](operation="delete_task", task_id="t")
    http.assert_not_called()


@pytest.mark.asyncio
async def test_list_documents_name_filter_walks_pages_and_stays_local(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.side_effect = [
        (
            _resp(
                200,
                {
                    "items": [{"id": "d1", "file_name": "NDA Acme.pdf"}, {"id": "d2", "file_name": "Lease.pdf"}],
                    "cursor": "c2",
                },
            ),
            None,
        ),
        (_resp(200, {"items": [{"id": "d3", "file_name": "nda beta.docx"}], "cursor": None}), None),
    ]
    answer = await handlers["get_wordsmith_repositories"](
        operation="list_documents", repository_id="r1", query={"folder_id": "f1", "name_contains": "NDA"}
    )
    assert http.call_count == 2
    first, second = (call.kwargs["params"] for call in http.call_args_list)
    assert first == {"folder_id": "f1", "limit": 100}
    assert second == {"folder_id": "f1", "limit": 100, "cursor": "c2"}
    assert "2 of 3 item(s) kept" in answer
    # Quoted ids: the answer's random UNTRUSTED-<8 hex> boundary can hold a bare "d2".
    assert '"d1"' in answer and '"d3"' in answer and '"d2"' not in answer


@pytest.mark.asyncio
async def test_list_documents_name_filter_hands_back_the_cursor_at_the_bound(http: AsyncMock) -> None:
    _, handlers = _tools()
    page = {"items": [{"id": "x", "file_name": "Other.pdf"}], "cursor": "next"}
    http.side_effect = lambda *a, **k: (_resp(200, page), None)
    answer = await handlers["get_wordsmith_repositories"](
        operation="list_documents", repository_id="r1", query={"name_contains": "nda"}
    )
    assert http.call_count == wordsmith._NAME_FILTER_MAX_PAGES
    assert "pass query.cursor" in answer and '"next"' in answer


@pytest.mark.asyncio
async def test_search_metadata_only_drops_snippets_and_keeps_the_body_clean(http: AsyncMock) -> None:
    _, handlers = _tools()
    hits = [
        {"document_id": "d1", "file_name": "NDA Acme.pdf", "snippet": "secret passage", "score": 0.9},
        {"document_id": "d2", "file_name": "Lease.pdf", "snippet": "other", "score": 0.5},
    ]
    http.return_value = (_resp(200, {"items": hits}), None)
    answer = await handlers["get_wordsmith_repositories"](
        operation="search_repository",
        repository_id="r1",
        body={"query": "envelope 123", "metadata_only": True},
        query={"name_contains": "nda"},
    )
    assert http.call_count == 1
    kwargs = http.call_args.kwargs
    assert kwargs["json"] == {"query": "envelope 123"}
    assert "params" not in kwargs
    assert "secret passage" not in answer and '"snippet"' not in answer
    assert '"d1"' in answer and '"d2"' not in answer
    assert "1 of 2 item(s) kept" in answer


@pytest.mark.asyncio
async def test_repository_options_are_offered_and_without_them_the_call_is_unchanged(http: AsyncMock) -> None:
    defs, handlers = _tools()
    assert "name_contains" in defs["get_wordsmith_repositories"].description
    http.return_value = (_resp(200, {"items": [{"snippet": "kept"}]}), None)
    answer = await handlers["get_wordsmith_repositories"](
        operation="search_repository", repository_id="r1", body={"query": "x"}
    )
    assert "kept" in answer


@pytest.mark.asyncio
async def test_start_carries_idempotency_key_and_is_retried(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (_resp(202, {"operation_id": "op1"}), None)
    answer = await handlers["manage_wordsmith_files"](
        operation="translate_file", file_id="f1", body={"language": "fr"}, query={"idempotency_key": "k-1"}
    )
    kwargs = http.call_args.kwargs
    assert kwargs["headers"]["Idempotency-Key"] == "k-1"
    assert "params" not in kwargs  # the key left query once used
    assert 500 in kwargs["retry_statuses"] and 503 in kwargs["retry_statuses"]
    assert kwargs["json"] == {"language": "fr"}
    assert "Idempotency-Key: k-1" in answer
    assert ("POST", "/files/{file_id}/translate") in IDEMPOTENT_STARTS


@pytest.mark.asyncio
async def test_plain_write_is_sent_once(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (_resp(200, {"id": "c1"}), None)
    await handlers["manage_wordsmith_tasks"](operation="add_comment", task_id="t1", body={"body": "hi"})
    kwargs = http.call_args.kwargs
    assert "retry_statuses" not in kwargs
    assert "Idempotency-Key" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_draft_and_task_bodies_name_their_fields_before_the_call(http: AsyncMock) -> None:
    """#2183: an unknown key or priority is refused locally, with the accepted fields named."""
    _, handlers = _tools()
    files, tasks = handlers["manage_wordsmith_files"], handlers["manage_wordsmith_tasks"]
    answer = await files(operation="create_draft", body={"blueprint_id": "b1", "name": "x", "placeholders": {}})
    assert answer.startswith("Error: create_draft does not accept name, placeholders.")
    assert "values (" in answer and "file_name (" in answer
    answer = await tasks(operation="create_task", body={"title": "t", "additional_context": "c"})
    assert "does not accept additional_context" in answer
    answer = await tasks(operation="create_task", body={"title": "t", "priority": "normal"})
    assert "no_priority, urgent, high, medium, low" in answer
    http.assert_not_called()

    http.return_value = (_resp(202, {"operation_id": "op1"}), None)
    await files(operation="create_draft", body={"blueprint_id": "b1", "values": {"party": "A"}, "file_name": "x"})
    assert http.call_args.kwargs["json"]["values"] == {"party": "A"}


# ── Agents ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_agent_waits_and_keys(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (_resp(200, {"id": "q1", "status": "completed", "answer": "Yes."}), None)
    answer = await handlers["send_wordsmith_agent_question"](
        question="Is this valid?", agent_id="a1", file_ids=["f1"], wait=99, idempotency_key="key-9"
    )
    args, kwargs = http.call_args
    assert args[2] == "https://api.wordsmith.ai/api/v1/agents/a1/questions"
    assert kwargs["json"] == {"question": "Is this valid?", "wait": 20, "attachments": [{"file_id": "f1"}]}
    assert kwargs["headers"]["Idempotency-Key"] == "key-9"
    assert "Yes." in answer


@pytest.mark.asyncio
async def test_filtered_question_is_final(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (
        _resp(
            200,
            {"id": "q2", "session_id": None, "status": "filtered", "answer": "Not for me.", "reason": "Not for me."},
        ),
        None,
    )
    answer = await handlers["send_wordsmith_agent_question"](question="Employment?")
    assert "declined" in answer and "do not retry" in answer


@pytest.mark.asyncio
async def test_in_progress_question_names_the_follow_up(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (_resp(200, {"id": "q3", "status": "in_progress"}), None)
    answer = await handlers["send_wordsmith_agent_question"](question="Long one", wait=0)
    assert "operation=get_question" in answer


@pytest.mark.asyncio
async def test_question_limits(http: AsyncMock) -> None:
    _, handlers = _tools()
    assert "10,000" in await handlers["send_wordsmith_agent_question"](question="x" * 10_001)
    assert "10 attachments" in await handlers["send_wordsmith_agent_question"](question="q", urls=["u"] * 11)
    http.assert_not_called()


@pytest.mark.asyncio
async def test_wait_operation_clamps(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (_resp(200, {"id": "op", "status": "succeeded"}), None)
    await handlers["wait_wordsmith_operation"](operation_id="op", wait=60)
    assert http.call_args.kwargs["params"] == {"wait": 20}


# ── Errors, rate limits, secrets ────────────────────────────────────────


@pytest.mark.asyncio
async def test_scope_refusal_names_the_scope(http: AsyncMock) -> None:
    _, handlers = _tools()
    resp = _resp(
        403,
        {"error_code": "insufficient_scope", "message": "Needs tasks:write"},
        {"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="tasks:write"'},
    )
    http.return_value = (resp, "HTTP 403")
    answer = await handlers["manage_wordsmith_tasks"](operation="create_task", body={"title": "x"})
    assert "error_code insufficient_scope" in answer
    assert "needs scope tasks:write" in answer
    assert answer.startswith("Error")


@pytest.mark.asyncio
async def test_rate_limit_uses_safe_request_retry() -> None:
    """429 is retried by safe_request after Retry-After (its own contract)."""
    calls: list[str] = []

    async def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls.append(url)
        if len(calls) == 1:
            return _resp(429, {"error_code": "rate_limit_exceeded"}, {"Retry-After": "0"})
        return _resp(200, {"user_id": "u"})

    client = MagicMock()
    client.request = fake_request

    @asynccontextmanager
    async def egress(*_a: Any, **_k: Any) -> AsyncIterator[MagicMock]:
        yield client

    with patch.object(wordsmith, "egress_client", egress), patch("app.integrations.http.asyncio.sleep", AsyncMock()):
        ok, message = await validate_connection("sk-ws-api1-x")
    assert ok, message
    assert len(calls) == 2


def test_secrets_are_redacted() -> None:
    data = {"id": "w", "secret": "whsec_abc", "nested": [{"callback_secret": "cs"}], "name": "n"}
    clean = redact_secrets(data)
    assert "whsec_abc" not in json.dumps(clean)
    assert "cs" not in json.dumps(clean["nested"])
    assert clean["name"] == "n"


@pytest.mark.asyncio
async def test_webhook_secret_never_reaches_the_model(http: AsyncMock) -> None:
    _, handlers = _tools()
    http.return_value = (_resp(200, {"id": "we1", "secret": "whsec_live"}), None)
    answer = await handlers["manage_wordsmith_webhook_endpoints"](
        operation="create_webhook_endpoint", body={"url": "https://x", "events": ["task.created"]}
    )
    assert "whsec_live" not in answer


# ── Region and Test Connection ──────────────────────────────────────────


def test_region_resolution() -> None:
    assert resolve_base_url("us") == "https://api.us.wordsmith.ai/api/v1"
    assert resolve_base_url("") == "https://api.wordsmith.ai/api/v1"
    assert resolve_base_url("https://evil.example/api/v1") == "https://api.wordsmith.ai/api/v1"
    assert split_token("https://api.us.wordsmith.ai/api/v1||sk||with||pipes") == (
        "https://api.us.wordsmith.ai/api/v1",
        "sk||with||pipes",
    )
    assert split_token("sk-ws-api1-x") == ("https://api.wordsmith.ai/api/v1", "sk-ws-api1-x")


@pytest.mark.asyncio
async def test_validate_connection_names_user_and_org(http: AsyncMock) -> None:
    http.return_value = (_resp(200, {"user_email": "a@b.c", "customer_name": "Acme"}), None)
    ok, message = await validate_connection("https://api.us.wordsmith.ai/api/v1||sk-ws-api1-x")
    assert ok
    assert "a@b.c" in message and "Acme" in message and "api.us.wordsmith.ai" in message
    assert http.call_args.args[2] == "https://api.us.wordsmith.ai/api/v1/me"


@pytest.mark.asyncio
async def test_validate_connection_bad_key(http: AsyncMock) -> None:
    http.return_value = (_resp(401, {"error_code": "unauthorized", "message": "Invalid API key"}), "HTTP 401")
    ok, message = await validate_connection("bad")
    assert not ok
    assert "unauthorized" in message
