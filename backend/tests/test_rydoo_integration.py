"""Rydoo provider: credential, bearer cache, the operation table, masking, the write gate, files, webhooks.

The Rydoo API is replayed from recorded answers (shapes of the public API
reference, 21 Nov 2026) through an httpx MockTransport: no network.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, QueryParams

from app.api.v1 import webhooks as webhook_routes
from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.core.utils import redact_secret_text
from app.integrations import http as http_mod
from app.integrations.providers import rydoo
from app.integrations.providers.rydoo import _client, _tools
from app.integrations.providers.rydoo._client import build_rydoo_token, parse_rydoo_token
from app.integrations.providers.rydoo.webhooks import (
    EVENT_TYPES,
    TRIGGER_EVENTS,
    is_delivery,
    token_matches,
    trigger_event,
)
from app.services.integration_read_only import integration_tool_access, withhold_write_tools
from app.services.integration_tools import PROVIDER_TOOLS
from app.tools.base import CallableToolExecutor, ToolDefinition

TOKEN = build_rydoo_token("cid", "secret-1")

BRANCHES = {
    "items": [
        {"id": "b-fr", "name": "Filigran SAS", "currency": "EUR", "isActive": True},
        {"id": "b-us", "name": "Filigran Inc", "currency": "USD", "isActive": True},
    ],
    "totalCount": 2,
}
EXPORTED = {
    "items": [
        {
            "id": "e-1",
            "status": "Approved",
            "user": {"userId": "u-1", "firstName": "Kate", "iban": "BE123145678901"},
            "meansOfPayment": {"paymentMethod": "CreditCard", "cardNumber": "5032", "accountNumber": "0532013000"},
            "amount": {"value": 120.5, "currency": "EUR"},
        }
    ],
    "nextCursor": "e-1",
}


class _Recorder:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.logins: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.rydoo.com":
            self.logins.append(request)
            return httpx.Response(
                200, json={"access_token": f"bearer-{len(self.logins)}", "expires_in": 3600, "token_type": "Bearer"}
            )
        self.requests.append(request)
        answer = self.routes.get(f"{request.method} {request.url.path}", self.routes.get(request.url.path))
        if isinstance(answer, list) and answer and isinstance(answer[0], httpx.Response):
            first: httpx.Response = answer.pop(0)
            return first
        if isinstance(answer, httpx.Response):
            return answer
        if answer is None:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=answer)


@pytest.fixture
def rydoo_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[dict[str, object]], _Recorder]]:
    http_mod._APP_TOKEN_CACHE.clear()

    def _install(routes: dict[str, object]) -> _Recorder:
        recorder = _Recorder(routes)
        transport = httpx.MockTransport(recorder)

        def _http(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return httpx.AsyncClient(*args, transport=transport, **kwargs)

        monkeypatch.setattr(_client, "egress_client", _http)
        return recorder

    yield _install
    http_mod._APP_TOKEN_CACHE.clear()


def _tools_for(token: str = TOKEN, **kwargs: Any) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()
    return rydoo.build_tools(token, executor, **kwargs), executor


async def _call(executor: CallableToolExecutor, name: str, **kwargs: Any) -> Any:
    handler = executor.handler(name)
    assert handler is not None, name
    return await handler(**kwargs)


# --- credential ----------------------------------------------------------------


def test_token_round_trip_keeps_a_secret_with_separators() -> None:
    creds = parse_rydoo_token(build_rydoo_token(" cid ", "s||ecret"))
    assert (creds.client_id, creds.client_secret) == ("cid", "s||ecret")


def test_token_from_config_reads_the_client_id_from_extra_config() -> None:
    creds = parse_rydoo_token(_client.token_from_config("sec", {"client_id": "abc"}))
    assert (creds.client_id, creds.client_secret) == ("abc", "sec")


async def test_a_missing_part_is_named_at_call_time(rydoo_api: Any) -> None:
    rydoo_api({})
    _, executor = _tools_for(build_rydoo_token("", "sec"))
    out = await _call(executor, "list_rydoo_branches")
    assert "client ID" in out and "Error" in out


# --- bearer --------------------------------------------------------------------


async def test_client_credentials_grant_and_the_bearer_is_cached(rydoo_api: Any) -> None:
    rec = rydoo_api({"/v2/branches": BRANCHES})
    _, executor = _tools_for()
    await _call(executor, "list_rydoo_branches")
    await _call(executor, "list_rydoo_branches")
    assert len(rec.logins) == 1
    form = parse_qs(rec.logins[0].content.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["scope"] == ["api_xpd2"]
    assert form["client_id"] == ["cid"]
    assert all(r.headers["Authorization"] == "Bearer bearer-1" for r in rec.requests)
    assert all(r.url.host == "api.rydoo.com" for r in rec.requests)


async def test_a_401_mints_a_new_bearer_once(rydoo_api: Any) -> None:
    rec = rydoo_api(
        {"/v2/branches": [httpx.Response(401, json={"message": "expired"}), httpx.Response(200, json=BRANCHES)]}
    )
    _, executor = _tools_for()
    out = await _call(executor, "list_rydoo_branches")
    assert "Filigran Inc" in out
    assert len(rec.logins) == 2
    assert rec.requests[-1].headers["Authorization"] == "Bearer bearer-2"


async def test_a_refused_sign_in_never_echoes_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    http_mod._APP_TOKEN_CACHE.clear()
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "invalid_client"}))
    monkeypatch.setattr(
        _client,
        "egress_client",
        lambda *a, **k: httpx.AsyncClient(*a, transport=transport, **{x: y for x, y in k.items() if x != "transport"}),
    )
    _, executor = _tools_for(build_rydoo_token("cid", "very-secret-value"))
    out = await _call(executor, "list_rydoo_branches")
    assert out.startswith("Error: Rydoo sign-in failed")
    assert "very-secret-value" not in out


async def test_validate_connection_lists_the_branches(rydoo_api: Any) -> None:
    rydoo_api({"/v2/branches": BRANCHES})
    ok, message = await rydoo.validate_connection(TOKEN)
    assert ok
    assert "2 active branch(es)" in message and "Filigran SAS" in message
    assert "secret-1" not in message


# --- the operation table -----------------------------------------------------------


def test_every_operation_is_a_tool_with_a_unique_name() -> None:
    defs, _ = _tools_for()
    names = [d.name for d in defs]
    assert len(names) == len(set(names)) == len(rydoo.operations()) == 117
    assert all(len(d.description) <= 300 for d in defs)


def test_read_or_write_follows_the_http_method() -> None:
    defs, _ = _tools_for()
    by_name = {d.name: d for d in defs}
    for op in rydoo.operations():
        tool = by_name[op.name]
        expected = "read" if op.method == "GET" else "write"
        assert integration_tool_access(op.name) == expected, op.name
        assert tool.always_ask is (expected == "write"), op.name


def test_a_read_only_binding_withholds_every_write() -> None:
    defs, executor = _tools_for()
    kept = withhold_write_tools(defs, executor, integration_label="Rydoo")
    writes = {op.name for op in rydoo.operations() if op.method != "GET"}
    assert writes and not {d.name for d in kept} & writes


def test_bank_details_is_a_named_group_of_the_payment_methods() -> None:
    group = {op.name for op in rydoo.operations() if op.group == rydoo.BANK_DETAILS_GROUP}
    assert group == {
        "list_rydoo_payment_methods",
        "count_rydoo_payment_methods",
        "create_rydoo_payment_method",
        "update_rydoo_payment_method",
    }


def test_path_parameters_are_required_snake_case_arguments() -> None:
    defs, _ = _tools_for()
    schemas = {d.name: d.input_schema for d in defs}
    patch = schemas["update_rydoo_user_role_branches"]
    assert set(patch["required"]) == {"user_id", "role"} and "body" in patch["properties"]
    put = schemas["set_rydoo_user_role_permissions"]
    assert set(put["required"]) == {"user_id", "role", "body"}


# --- answers -----------------------------------------------------------------------


async def test_branch_filter_and_masking_on_exported_expenses(rydoo_api: Any) -> None:
    rec = rydoo_api({"/v3/expenses/exported": EXPORTED})
    _, executor = _tools_for()
    out = await _call(
        executor,
        "list_rydoo_exported_expenses",
        branchId="b-fr",
        exportedAtStartDate="2026-10-01",
        exportedAtEndDate="2026-10-31",
    )
    query = parse_qs(rec.requests[0].url.query.decode())
    assert query["branchId"] == ["b-fr"] and query["exportedAtStartDate"] == ["2026-10-01"]
    assert "BE123145678901" not in out and "****8901" in out
    assert "0532013000" not in out and "****3000" in out
    assert "startingAfter=e-1" in out


async def test_a_path_parameter_is_encoded(rydoo_api: Any) -> None:
    rec = rydoo_api({"GET /v3/expenses/a%2Fb": {"id": "a/b"}})
    _, executor = _tools_for()
    await _call(executor, "get_rydoo_expense", id="a/b")
    assert rec.requests[0].url.raw_path.startswith(b"/v3/expenses/a%2Fb")


async def test_a_write_sends_its_body_and_is_not_retried(rydoo_api: Any) -> None:
    rec = rydoo_api({"POST /v2/expenses/reject": httpx.Response(503, json={"message": "busy"})})
    _, executor = _tools_for()
    out = await _call(executor, "reject_rydoo_expenses", body={"expenseIds": ["e-1"], "reason": "duplicate"})
    assert "Error" in out
    assert len(rec.requests) == 1
    assert rec.requests[0].headers["Content-Type"] == "application/json"
    assert b'"reason":"duplicate"' in rec.requests[0].content


async def test_a_write_without_its_body_is_refused_before_the_call(rydoo_api: Any) -> None:
    rec = rydoo_api({})
    _, executor = _tools_for()
    out = await _call(executor, "mark_rydoo_expenses_reimbursed")
    assert out.startswith("Error: body")
    assert not rec.requests


async def test_the_receipt_is_delivered_as_a_file_of_the_run(rydoo_api: Any) -> None:
    rydoo_api(
        {
            "GET /v3/receipts/by-expense/e-1": httpx.Response(
                200, content=b"%PDF-1.7 receipt", headers={"content-type": "application/pdf"}
            )
        }
    )
    stored: list[tuple[str, bytes, str]] = []

    async def _persist(filename: str, data: bytes, content_type: str) -> None:
        stored.append((filename, data, content_type))

    _, executor = _tools_for(persist_file_fn=_persist)
    await _call(executor, "download_rydoo_receipt", id="e-1")
    assert stored == [("rydoo-receipt-e-1.pdf", b"%PDF-1.7 receipt", "application/pdf")]


async def test_an_upload_without_a_file_context_is_refused(rydoo_api: Any) -> None:
    rec = rydoo_api({})
    _, executor = _tools_for()
    out = await _call(executor, "upload_rydoo_receipt", file_id="f-1")
    assert getattr(out, "is_error", False)
    assert not rec.requests


# --- webhooks ------------------------------------------------------------------------


def test_trigger_events_are_prefixed_snake_case() -> None:
    assert trigger_event("ExpenseApproved") == "rydoo_expense_approved"
    assert set(TRIGGER_EVENTS) == set(EVENT_TYPES)
    assert len(set(TRIGGER_EVENTS.values())) == 7


def test_a_delivery_is_recognised_by_its_envelope() -> None:
    payload = {
        "eventTimestamp": "2026-11-21T10:00:00Z",
        "expenseId": "e-1",
        "userId": "u-1",
        "companyId": "c-1",
        "eventType": "ExpenseSubmitted",
        "correlationId": "x",
    }
    assert is_delivery(payload)
    assert not is_delivery({**payload, "eventType": "Unknown"})
    assert not is_delivery({"eventType": "ExpenseSubmitted"})
    assert not is_delivery(["not", "a", "dict"])


_DELIVERY = json.dumps(
    {"eventType": "ExpenseApproved", "expenseId": "e-1", "userId": "u-1", "companyId": "c-1"}
).encode()


class _Session:
    """``async_session()`` answering every query with the Rydoo integration row."""

    def __init__(self, row: object) -> None:
        self._row = row

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: self._row)


class _ReachedError(Exception):
    """The delivery passed the token gate and reached the dedup cache."""


class _Delivery(SimpleNamespace):
    async def body(self) -> bytes:
        return cast(bytes, self._body)


def _serve_rydoo(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any] | None) -> None:
    row = SimpleNamespace(id=uuid.uuid4(), provider="rydoo", extra_config_enc="enc" if config is not None else None)
    monkeypatch.setattr(webhook_routes, "async_session", lambda: _Session(row))
    monkeypatch.setattr(webhook_routes, "decrypt_value", lambda _enc: json.dumps(config or {}))

    async def _claim(*_args: object, **_kwargs: object) -> None:
        raise _ReachedError

    monkeypatch.setattr(webhook_routes, "_claim_webhook_dedup_slot", _claim)


def _rydoo_delivery(query: str = "", headers: dict[str, str] | None = None) -> _Delivery:
    return _Delivery(
        headers=Headers(headers or {}),
        query_params=QueryParams(query),
        _body=_DELIVERY,
        method="POST",
        url=f"https://xtm.example/api/hooks/integrations/x{'?' + query if query else ''}",
    )


@pytest.mark.parametrize(
    ("config", "query", "headers"),
    [
        (None, "", None),
        ({"client_id": "cid"}, "", None),
        ({"client_id": "cid"}, "token=anything", None),
        ({"webhook_token": "t0ken-long-enough"}, "", None),
        ({"webhook_token": "t0ken-long-enough"}, "token=wrong", None),
        ({"webhook_token": "t0ken-long-enough"}, "", {"X-Webhook-Token": "wrong"}),
        ({"webhook_token": False}, "token=False", None),
    ],
)
async def test_a_delivery_without_the_integration_token_is_refused_before_the_dedup_cache(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any] | None,
    query: str,
    headers: dict[str, str] | None,
) -> None:
    _serve_rydoo(monkeypatch, config)
    with pytest.raises(HTTPException) as exc_info:
        await webhook_routes.receive_integration_webhook(str(uuid.uuid4()), cast(Any, _rydoo_delivery(query, headers)))
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize(
    ("query", "headers"),
    [("token=t0ken-long-enough", None), ("", {"X-Webhook-Token": "t0ken-long-enough"})],
)
async def test_a_delivery_with_the_integration_token_passes_the_gate(
    monkeypatch: pytest.MonkeyPatch, query: str, headers: dict[str, str] | None
) -> None:
    _serve_rydoo(monkeypatch, {"client_id": "cid", "webhook_token": "t0ken-long-enough"})
    with pytest.raises(_ReachedError):
        await webhook_routes.receive_integration_webhook(str(uuid.uuid4()), cast(Any, _rydoo_delivery(query, headers)))


def test_token_matches_never_accepts_an_empty_side() -> None:
    assert token_matches("abc", "abc")
    assert not token_matches("", "abc")
    assert not token_matches("abc", "")
    assert not token_matches("abd", "abc")


def test_the_token_in_the_url_is_redacted_from_the_run_context() -> None:
    url = "https://xtm.example/api/hooks/integrations/x?token=t0ken-long-enough"
    assert "t0ken-long-enough" not in redact_secret_text(url)


# --- catalog -------------------------------------------------------------------------


def test_catalog_entry_events_and_dispatcher() -> None:
    entry = cast(dict[str, Any], PROVIDER_CATALOG["rydoo"])
    assert set(entry["extra_fields"]) == {"client_id", "webhook_token"}
    available = cast(list[dict[str, Any]], entry["available_events"])
    assert {e["event"] for e in available} == set(TRIGGER_EVENTS.values())
    assert all("eventType" in e["filter_keys"] for e in available)
    assert PROVIDER_TOOLS["rydoo"] is rydoo.build_tools
    assert parse_rydoo_token(build_dummy_token("rydoo")).missing() == []


def test_sanitize_redacts_secrets() -> None:
    assert _tools.sanitize({"access_token": "t", "nested": [{"clientSecret": "s"}]}) == {
        "access_token": "[redacted]",
        "nested": [{"clientSecret": "[redacted]"}],
    }
