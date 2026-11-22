"""Airwallex provider: credential, bearer cache, masking, write gate, reports, webhooks.

The Airwallex API is replayed from recorded answers (demo environment shapes,
API reference 2026-08-21) through an httpx MockTransport: no network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Iterator
from typing import Any, cast

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations import http as http_mod
from app.integrations.providers import airwallex
from app.integrations.providers.airwallex import _client, _tools
from app.integrations.providers.airwallex._client import build_airwallex_token, parse_airwallex_token
from app.integrations.providers.airwallex.webhooks import (
    EVENT_TYPES,
    TRIGGER_EVENTS,
    is_delivery,
    trigger_event,
    verify_signature,
)
from app.services.integration_read_only import integration_tool_access, withhold_write_tools
from app.services.integration_tools import PROVIDER_TOOLS
from app.tools.base import CallableToolExecutor, ToolDefinition

TOKEN = build_airwallex_token("demo", "cid", "key-1")
WRITE_TOKEN = TOKEN

BALANCES = [
    {
        "currency": "AUD",
        "available_amount": 125000.5,
        "pending_amount": 0,
        "reserved_amount": 0,
        "total_amount": 125000.5,
    },
    {
        "currency": "SGD",
        "available_amount": 48210.0,
        "pending_amount": 1200.0,
        "reserved_amount": 0,
        "total_amount": 49410.0,
    },
]
GLOBAL_ACCOUNTS = {
    "items": [
        {
            "id": "ga_1",
            "account_name": "Filigran Pty Ltd",
            "account_number": "123456789012",
            "iban": "GB33BUKB20201555555555",
            "country_code": "AU",
            "status": "ACTIVE",
        }
    ],
    "page_after": "cursor-2",
}


class _Recorder:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.logins: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/authentication/login":
            self.logins.append(request)
            return httpx.Response(
                201,
                json={"token": f"bearer-{len(self.logins)}", "expires_at": "2099-01-01T00:00:00+0000"},
            )
        self.requests.append(request)
        answer = self.routes.get(f"{request.method} {request.url.path}", self.routes.get(request.url.path))
        if isinstance(answer, list) and answer and isinstance(answer[0], httpx.Response):
            first: httpx.Response = answer.pop(0)
            return first
        if isinstance(answer, httpx.Response):
            return answer
        if answer is None:
            return httpx.Response(404, json={"code": "not_found", "message": "not found"})
        return httpx.Response(200, json=answer)


@pytest.fixture
def airwallex_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[dict[str, object]], _Recorder]]:
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


def _tools_for(token: str = TOKEN) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()
    return airwallex.build_tools(token, executor), executor


async def _call(executor: CallableToolExecutor, name: str, **kwargs: Any) -> Any:
    handler = executor.handler(name)
    assert handler is not None, name
    return await handler(**kwargs)


# --- credential ----------------------------------------------------------------


def test_token_round_trip_keeps_a_key_with_separators() -> None:
    token = build_airwallex_token("sandbox", " cid ", "k||ey", login_as="acct_9")
    creds = parse_airwallex_token(token)
    assert creds.environment == "demo"
    assert creds.host == "api-demo.airwallex.com"
    assert (creds.client_id, creds.api_key, creds.login_as) == ("cid", "k||ey", "acct_9")


def test_unknown_environment_is_production() -> None:
    creds = parse_airwallex_token(_client.token_from_config("key", {"environment": "moon", "client_id": "c"}))
    assert creds.host == "api.airwallex.com"


async def test_a_missing_part_is_named_at_call_time(airwallex_api: Any) -> None:
    airwallex_api({})
    _, executor = _tools_for(build_airwallex_token("demo", "", "key"))
    out = await _call(executor, "list_airwallex_balances")
    assert "client ID" in out and "Error" in out


# --- bearer --------------------------------------------------------------------


async def test_login_headers_and_the_bearer_is_cached(airwallex_api: Any) -> None:
    rec = airwallex_api({"/api/v1/balances/current": BALANCES})
    _, executor = _tools_for(build_airwallex_token("demo", "cid", "key-1", login_as="acct_au"))
    await _call(executor, "list_airwallex_balances")
    await _call(executor, "list_airwallex_balances")
    assert len(rec.logins) == 1
    login = rec.logins[0]
    assert login.url.host == "api-demo.airwallex.com"
    assert login.headers["x-client-id"] == "cid"
    assert login.headers["x-api-key"] == "key-1"
    assert login.headers["x-login-as"] == "acct_au"
    assert all(r.headers["Authorization"] == "Bearer bearer-1" for r in rec.requests)


async def test_a_401_mints_a_new_bearer_once(airwallex_api: Any) -> None:
    rec = airwallex_api(
        {
            "/api/v1/balances/current": [
                httpx.Response(401, json={"code": "unauthorized"}),
                httpx.Response(200, json=BALANCES),
            ]
        }
    )
    _, executor = _tools_for()
    out = await _call(executor, "list_airwallex_balances")
    assert "SGD" in out
    assert len(rec.logins) == 2
    assert rec.requests[-1].headers["Authorization"] == "Bearer bearer-2"


async def test_a_refused_sign_in_never_echoes_the_key(airwallex_api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "credentials_invalid", "message": "bad"})

    transport = httpx.MockTransport(_refuse)
    monkeypatch.setattr(
        _client,
        "egress_client",
        lambda *a, **k: httpx.AsyncClient(*a, transport=transport, **{x: y for x, y in k.items() if x != "transport"}),
    )
    _, executor = _tools_for(build_airwallex_token("demo", "cid", "very-secret-key"))
    out = await _call(executor, "list_airwallex_balances")
    assert out.startswith("Error: Airwallex sign-in failed")
    assert "very-secret-key" not in out


async def test_on_behalf_of_sets_the_connected_account_header(airwallex_api: Any) -> None:
    rec = airwallex_api({"/api/v1/balances/current": BALANCES})
    _, executor = _tools_for()
    out = await _call(executor, "list_airwallex_balances", on_behalf_of="acct_sg")
    assert rec.requests[0].headers["x-on-behalf-of"] == "acct_sg"
    assert "[account acct_sg]" in out


# --- answers -----------------------------------------------------------------------


async def test_account_and_iban_numbers_are_masked(airwallex_api: Any) -> None:
    airwallex_api({"/api/v1/global_accounts": GLOBAL_ACCOUNTS})
    _, executor = _tools_for()
    out = await _call(executor, "list_airwallex_global_accounts", status="ACTIVE")
    assert "123456789012" not in out and "****9012" in out
    assert "GB33BUKB20201555555555" not in out and "****5555" in out
    assert "page=cursor-2" in out


def test_sanitize_redacts_secrets_anywhere() -> None:
    out = _tools.sanitize({"a": [{"webhook_secret": "s", "card_number": "4111111111111111", "cvv": "123"}]})
    assert out == {"a": [{"webhook_secret": "[redacted]", "card_number": "****1111", "cvv": "[redacted]"}]}


async def test_filters_go_to_the_query_string(airwallex_api: Any) -> None:
    rec = airwallex_api({"/api/v1/financial_transactions": {"items": [], "has_more": False}})
    _, executor = _tools_for()
    out = await _call(
        executor,
        "list_airwallex_financial_transactions",
        currency="AUD",
        status="SETTLED",
        from_created_at="2026-11-01T00:00:00+0000",
        page_size=50,
    )
    query = dict(rec.requests[0].url.params)
    assert query == {
        "currency": "AUD",
        "status": "SETTLED",
        "from_created_at": "2026-11-01T00:00:00+0000",
        "page_size": "50",
    }
    assert "(last page)" in out


async def test_a_missing_required_argument_is_refused_before_a_call(airwallex_api: Any) -> None:
    rec = airwallex_api({})
    _, executor = _tools_for()
    out = await _call(executor, "get_airwallex_fx_rate", buy_currency="EUR")
    assert out == "Error: sell_currency is required."
    assert rec.requests == []


# --- the write gate ------------------------------------------------------------------


WRITES = {
    "create_airwallex_beneficiary",
    "update_airwallex_beneficiary",
    "delete_airwallex_beneficiary",
    "create_airwallex_transfer",
    "cancel_airwallex_transfer",
    "create_airwallex_fx_quote",
    "create_airwallex_conversion",
    "freeze_airwallex_card",
    "unfreeze_airwallex_card",
    "update_airwallex_card",
    "create_airwallex_cardholder",
    "update_airwallex_cardholder",
    "create_airwallex_payment_link",
    "deactivate_airwallex_payment_link",
    "create_airwallex_invoice",
    "finalize_airwallex_invoice",
    "void_airwallex_invoice",
}


def test_every_write_is_built_and_asks_a_person() -> None:
    defs, _ = _tools_for()
    by_name = {d.name: d for d in defs}
    assert set(by_name) >= WRITES
    for name in WRITES:
        assert by_name[name].always_ask, name
        assert integration_tool_access(name) == "write", name
        assert "asks a person" in by_name[name].description, name


def test_every_other_tool_is_a_read() -> None:
    defs, _ = _tools_for()
    reads = {d.name for d in defs} - WRITES
    assert all(integration_tool_access(n) == "read" for n in reads), [
        n for n in reads if integration_tool_access(n) != "read"
    ]
    assert not any(d.always_ask for d in defs if d.name in reads)


def test_beneficiary_tools_are_a_named_group() -> None:
    defs, _ = _tools_for()
    group = {d.name for d in defs if "(beneficiaries)" in d.description}
    assert group == {n for n in WRITES if "beneficiary" in n} | {"validate_airwallex_beneficiary"}
    assert {d.name for d in defs} >= {"list_airwallex_beneficiaries", "get_airwallex_beneficiary"}


def test_a_read_only_binding_withholds_every_write() -> None:
    defs, executor = _tools_for()
    kept = withhold_write_tools(defs, executor, integration_label="Airwallex")
    assert not {d.name for d in kept} & WRITES


async def test_a_transfer_carries_an_idempotency_request_id(airwallex_api: Any) -> None:
    rec = airwallex_api({"POST /api/v1/transfers/create": {"id": "tr_1", "status": "IN_APPROVAL"}})
    _, executor = _tools_for(WRITE_TOKEN)
    out = await _call(
        executor,
        "create_airwallex_transfer",
        beneficiary_id="ben_1",
        transfer_currency="AUD",
        transfer_amount=10,
        reason="professional_business_services",
        reference="INV-1",
    )
    body = json.loads(rec.requests[0].content)
    assert body["request_id"] and f"[request_id {body['request_id']}]" in out
    assert body["beneficiary_id"] == "ben_1"


async def test_freeze_sends_the_inactive_status(airwallex_api: Any) -> None:
    rec = airwallex_api({"POST /api/v1/issuing/cards/card_1/update": {"card_id": "card_1", "card_status": "INACTIVE"}})
    _, executor = _tools_for(WRITE_TOKEN)
    await _call(executor, "freeze_airwallex_card", id="card_1")
    assert json.loads(rec.requests[0].content) == {"card_status": "INACTIVE"}


async def test_a_write_is_never_retried_on_a_5xx(airwallex_api: Any) -> None:
    rec = airwallex_api({"POST /api/v1/fx/conversions/create": httpx.Response(503, json={"code": "unavailable"})})
    _, executor = _tools_for(WRITE_TOKEN)
    out = await _call(executor, "create_airwallex_conversion", buy_currency="AUD", sell_currency="SGD", sell_amount=5)
    assert out.startswith("Error")
    assert len(rec.requests) == 1


# --- reports -------------------------------------------------------------------------


async def test_report_export_polls_then_delivers_the_file(airwallex_api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_tools, "REPORT_POLL_SECONDS", 0)
    report = {
        "id": "rep_1",
        "type": "BALANCE_ACTIVITY_REPORT",
        "file_format": "CSV",
        "from_date": "2026-10-01",
        "to_date": "2026-10-31",
    }
    rec = airwallex_api(
        {
            "POST /api/v1/finance/financial_reports/create": {**report, "status": "PENDING"},
            "GET /api/v1/finance/financial_reports/rep_1": {**report, "status": "COMPLETED"},
            "GET /api/v1/finance/financial_reports/rep_1/content": httpx.Response(
                200, content=b"date,amount\n2026-10-02,10.00\n", headers={"content-type": "text/csv"}
            ),
        }
    )
    stored: list[tuple[str, bytes]] = []

    async def _persist(data: bytes, filename: str, content_type: str, **kwargs: Any) -> None:
        stored.append((filename, data))
        return None

    executor = CallableToolExecutor()
    airwallex.build_tools(TOKEN, executor, persist_file_fn=_persist)
    out = await _call(
        executor,
        "export_airwallex_financial_report",
        type="BALANCE_ACTIVITY_REPORT",
        from_date="2026-10-01",
        to_date="2026-10-31",
    )
    assert json.loads(rec.requests[0].content)["file_format"] == "CSV"
    assert [r.url.path for r in rec.requests][-1] == "/api/v1/finance/financial_reports/rep_1/content"
    assert out is not None


# --- webhooks -------------------------------------------------------------------------


def _signed(body: bytes, secret: str, ts: str) -> str:
    return hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).hexdigest()


def test_signature_over_timestamp_and_body() -> None:
    body = b'{"id":"evt_1","name":"payout.transfer.paid","account_id":"acct_1","data":{"object":{}}}'
    ts = "1763766000000"
    now = int(ts) / 1000 + 10
    assert verify_signature(body, "whsec", _signed(body, "whsec", ts), ts, now=now) == (True, "")
    assert verify_signature(body, "whsec", _signed(body, "other", ts), ts, now=now)[1] == "signature mismatch"
    assert "replay" in verify_signature(body, "whsec", _signed(body, "whsec", ts), ts, now=now + 3600)[1]
    assert "x-timestamp" in verify_signature(body, "whsec", "ab", "", now=now)[1]


def test_trigger_keys_are_flat_and_prefixed() -> None:
    assert trigger_event("payout.transfer.funding.failed") == "airwallex_payout_transfer_funding_failed"
    assert len(set(TRIGGER_EVENTS.values())) == len(EVENT_TYPES)


def test_a_delivery_is_recognised_by_its_timestamp_and_event_shape() -> None:
    payload = {"id": "evt", "name": "deposit.settled", "account_id": "acct", "data": {"object": {}}}
    headers: dict[str, str] = {"x-timestamp": "1"}
    empty: dict[str, str] = {}
    assert is_delivery(headers.get, payload)
    assert not is_delivery(empty.get, payload)
    assert not is_delivery(headers.get, {"name": "x"})


# --- catalog --------------------------------------------------------------------------


def test_catalog_entry_lists_events_fields_and_every_tool() -> None:
    spec = cast("dict[str, Any]", PROVIDER_CATALOG["airwallex"])
    assert spec["fields"] == ["api_key"]
    assert set(spec["extra_fields"]) == {"environment", "client_id", "login_as", "webhook_secret"}
    assert {e["event"] for e in spec["available_events"]} == set(TRIGGER_EVENTS.values())
    builder = cast("Callable[..., list[ToolDefinition]]", PROVIDER_TOOLS["airwallex"])
    defs = builder(build_dummy_token("airwallex"), CallableToolExecutor())
    names = {d.name for d in defs}
    assert names >= WRITES
    assert len(names) == len(defs)
    for d in defs:
        assert len(d.description) <= 300, d.name
