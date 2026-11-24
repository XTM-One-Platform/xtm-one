"""DealHub provider: operation table, read/write by HTTP method, auth, host guard,
paging, file delivery and redaction, against recorded DealHub answers."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers import dealhub
from app.integrations.providers.dealhub import _client, _tools
from app.integrations.providers.dealhub._client import (
    DEFAULT_HOST,
    build_dealhub_token,
    normalize_host,
    parse_dealhub_token,
)
from app.integrations.providers.dealhub._operations import OPERATIONS
from app.services.integration_tools import PROVIDER_TOOLS
from app.services.tool_classification import definition_tool_type
from app.tools.base import CallableToolExecutor, ToolDefinition

# Recorded answers, shaped as the DealHub reference documents them.
QUOTE: dict[str, Any] = {
    "dealhub_quote_id": "Q-1001",
    "status": "Pending Approval",
    "info": {"dealhub_quote_name": "Acme renewal", "currency": "EUR", "playbook": "Enterprise"},
    "summary": {"currency": "EUR", "total_list_price": 120000, "total_net_price": 96000, "total_discount": 20},
    "line_items": [{"sku": "XTM-ENT", "name": "XTM One Enterprise", "net_price": 96000, "user_discount": 20}],
    "approvals": [{"reviewer": "finance", "step": 1, "status": "Pending", "reason": "Discount over 15%"}],
}
USERS: dict[str, Any] = {
    "users": [{"login": "rep", "email": "rep@example.com", "api_token": "s3cr3t", "is_active": True}]
}
_TOKEN = "api.dealhub.io||tok"


class _Resp:
    def __init__(
        self,
        status: int,
        body: Any = None,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        if content is None:
            content = b"" if body is None else json.dumps(body).encode()
            headers = {"content-type": "application/json", **(headers or {})}
        self.content = content
        self.headers = headers or {}
        self._body = body

    def json(self) -> Any:
        return self._body


Route = _Resp | Callable[[str], _Resp]


class _Log(list[dict[str, Any]]):
    routes: dict[tuple[str, str], Route]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Log:
    """Record every request; answer from ``calls.routes`` keyed by (method, path)."""
    log = _Log()
    log.routes = {}

    async def _fake(
        client: Any,
        method: str,
        url: str,
        provider: str,
        action: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> tuple[_Resp | None, str | None]:
        path = url.split("dealhub.io", 1)[1].split("?", 1)[0]
        log.append({"method": method.upper(), "url": url, "path": path, "headers": headers, **kwargs})
        route = log.routes.get((method.upper(), path), _Resp(200, {}))
        answer = route(url) if callable(route) else route
        if answer.status_code >= 400:
            expected = kwargs.get("expected_statuses")
            return (answer if expected else None), f"Error: DealHub answered HTTP {answer.status_code}."
        return answer, None

    monkeypatch.setattr(_client, "safe_request", _fake)
    return log


def _tools_by_name(token: str = _TOKEN, **kwargs: Any) -> tuple[dict[str, ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()
    defs = dealhub.build_tools(token, executor, **kwargs)
    return {d.name: d for d in defs}, executor


async def _call(executor: CallableToolExecutor, name: str, **args: Any) -> Any:
    handler = executor.handler(name)
    assert handler is not None, name
    return await handler(**args)


# ── Catalog and table ──────────────────────────────────────────────────


def test_catalog_entry_and_builder_are_registered() -> None:
    entry = PROVIDER_CATALOG["dealhub"]
    assert isinstance(entry, dict)
    assert entry["fields"] == ["api_key"]
    assert "instance_host" in entry["extra_fields"]
    assert PROVIDER_TOOLS["dealhub"] is dealhub.build_tools


def test_every_operation_is_one_tool_with_a_unique_name() -> None:
    names = [op.name for op in OPERATIONS]
    assert len(names) == len(set(names))
    defs, _ = _tools_by_name()
    assert set(defs) == set(names)
    assert len(OPERATIONS) >= 50


def test_read_or_write_follows_the_http_method_never_the_name() -> None:
    defs, _ = _tools_by_name()
    for op in OPERATIONS:
        d = defs[op.name]
        if op.method == "GET":
            assert d.read_only and not d.always_ask, op.name
        else:
            assert d.always_ask and not d.read_only, op.name
    # A POST DealHub documents as a lookup still asks a person.
    assert defs["price_dealhub_products"].always_ask


def test_the_catalog_classification_follows_the_method() -> None:
    defs, _ = _tools_by_name()
    for op in OPERATIONS:
        expected = "read" if op.method == "GET" else "write"
        assert definition_tool_type(defs[op.name]) == expected, op.name


def test_every_path_slot_is_a_required_path_parameter() -> None:
    for op in OPERATIONS:
        slots = {part[1:-1] for part in op.path.split("/") if part.startswith("{")}
        declared = {p.name for p in op.params if p.where == "path"}
        assert slots == declared, op.name
        assert all(p.required for p in op.params if p.where == "path"), op.name


# ── Credential and host ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("", DEFAULT_HOST),
        ("https://service-eu1.dealhub.io/", "service-eu1.dealhub.io"),
        ("API.DealHub.io", "api.dealhub.io"),
        ("evil.example.com", None),
        ("dealhub.io.evil.com", None),
        ("http://api.dealhub.io", None),
        ("api.dealhub.io:8443", None),
    ],
)
def test_the_host_stays_under_dealhub_io(typed: str, expected: str | None) -> None:
    assert normalize_host(typed) == expected


def test_token_round_trip_and_bare_token() -> None:
    creds = parse_dealhub_token(build_dealhub_token("service-eu1.dealhub.io", " tok||with||bars "))
    assert creds.host == "service-eu1.dealhub.io"
    assert creds.token == "tok||with||bars"
    assert parse_dealhub_token("bare").host == DEFAULT_HOST


async def test_a_foreign_host_never_receives_the_token(calls: _Log) -> None:
    _, executor = _tools_by_name("evil.example.com||tok")
    answer = await _call(executor, "list_dealhub_users")
    assert answer.startswith("Error:") and "dealhub.io" in answer
    assert calls == []


async def test_missing_token_is_named_without_a_call(calls: _Log) -> None:
    _, executor = _tools_by_name("api.dealhub.io||")
    answer = await _call(executor, "list_dealhub_users")
    assert "no API token" in answer
    assert calls == []


async def test_validate_connection_reads_active_users_and_never_echoes_the_token(calls: _Log) -> None:
    calls.routes[("GET", "/api/v1/users")] = _Resp(200, USERS)
    ok, message = await dealhub.validate_connection("api.dealhub.io||topsecret")
    assert ok and "1 active user" in message
    assert "topsecret" not in message
    assert calls[0]["headers"]["Authorization"] == "Bearer topsecret"
    assert "status=active" in calls[0]["url"]


async def test_validate_connection_refused_token(calls: _Log) -> None:
    calls.routes[("GET", "/api/v1/users")] = _Resp(401, {"error": "unauthorized"})
    ok, message = await dealhub.validate_connection("api.dealhub.io||badkey")
    assert not ok and "refused the API token" in message
    assert "badkey" not in message


# ── Calls ──────────────────────────────────────────────────────────────


async def test_get_quote_with_lines_and_approvals(calls: _Log) -> None:
    calls.routes[("GET", "/api/v2/quote/Q-1001")] = _Resp(200, QUOTE)
    _, executor = _tools_by_name()
    answer = await _call(executor, "get_dealhub_quote", quote_id="Q-1001", feature=["line_items", "approvals"])
    assert "Pending Approval" in answer and "Discount over 15%" in answer
    assert "feature=line_items" in calls[0]["url"] and "feature=approvals" in calls[0]["url"]


async def test_path_values_are_encoded(calls: _Log) -> None:
    _, executor = _tools_by_name()
    await _call(executor, "get_dealhub_user_by_login", login="a/b c")
    assert calls[0]["path"] == "/api/v1/user/login/a%2Fb%20c"


async def test_a_missing_path_value_is_refused(calls: _Log) -> None:
    _, executor = _tools_by_name()
    assert (await _call(executor, "get_dealhub_user", id=" ")).startswith("Error: id is required")
    assert calls == []


async def test_all_pages_reads_until_a_short_page(calls: _Log) -> None:
    pages = {0: [QUOTE] * 2, 2: [QUOTE]}

    def answer(url: str) -> _Resp:
        offset = int(url.split("offset=")[1].split("&")[0])
        return _Resp(200, {"quotes": pages.get(offset, [])})

    calls.routes[("GET", "/api/v2/quotes")] = answer
    _, executor = _tools_by_name()
    result = await _call(executor, "list_dealhub_quotes", limit=2, all_pages=True)
    assert result.startswith("list_dealhub_quotes: 3 item(s) over 2 page(s)")
    assert len(calls) == 2


async def test_a_write_sends_its_json_body(calls: _Log) -> None:
    _, executor = _tools_by_name()
    body = {"version_id": "V1"}
    await _call(executor, "activate_dealhub_version", body=body)
    assert calls[0]["method"] == "POST"
    assert json.loads(calls[0]["content"]) == body
    assert calls[0]["retry_statuses"] is None


async def test_secrets_in_answers_are_redacted(calls: _Log) -> None:
    calls.routes[("GET", "/api/v1/users")] = _Resp(200, USERS)
    _, executor = _tools_by_name()
    answer = await _call(executor, "list_dealhub_users")
    assert "s3cr3t" not in answer and "[redacted]" in answer


async def test_quote_document_is_delivered_as_a_file(calls: _Log, monkeypatch: pytest.MonkeyPatch) -> None:
    calls.routes[("GET", "/api/v1/quote/Q-1001/document")] = _Resp(
        200,
        content=b"%PDF-1.7 fake",
        headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="Q-1001.pdf"'},
    )
    delivered: dict[str, Any] = {}

    async def _deliver(**kwargs: Any) -> SimpleNamespace:
        delivered.update(kwargs)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(_tools, "deliver_file", _deliver)
    _, executor = _tools_by_name()
    await _call(executor, "download_dealhub_quote_document", dealhub_quote_id="Q-1001", document_type="PDF")
    assert delivered["filename"] == "Q-1001.pdf"
    assert delivered["data"].startswith(b"%PDF")
    assert delivered["source"] == "DealHub"
