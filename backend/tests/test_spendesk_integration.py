"""Spendesk provider: credential, auth, entity, paging, guardrails, masking, files, webhooks, coverage."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.providers import spendesk
from app.integrations.providers.spendesk import _client, _tools
from app.integrations.providers.spendesk._client import (
    ENVIRONMENTS,
    build_spendesk_token,
    parse_spendesk_token,
    token_from_config,
)
from app.integrations.providers.spendesk.webhooks import (
    EVENT_TYPES,
    SIGNATURE_HEADER,
    TRIGGER_EVENTS,
    event_key,
    verify_signature,
)
from app.services.integration_tools import PROVIDER_TOOLS
from app.services.tool_classification import definition_tool_type
from app.tools.base import CallableToolExecutor

TOKEN = build_spendesk_token("sandbox", "cid", "csecret")

#: Recorded answers of the Public API (shapes from its OpenAPI spec, 21 Nov 2026).
WALLET = {"data": {"balance": {"amount": 1250000, "currency": "EUR"}, "pendingTopUps": 0}}
COMPANIES = {
    "data": [{"id": "c-fr", "name": "Filigran SAS"}, {"id": "c-us", "name": "Filigran Inc"}],
    "meta": {"pagination": {"page": 1, "pageSize": 30, "total": 2}},
}
SUPPLIER = {
    "data": {"id": "s1", "name": "ACME", "bankDetails": {"iban": "FR7630006000011234567890189", "bic": "AGRIFRPP"}}
}


def _page(page: int, rows: list[dict], total: int) -> dict:
    return {"data": rows, "meta": {"pagination": {"page": page, "pageSize": 2, "total": total}}}


class _Recorder:
    """An httpx transport: the token endpoint, then API answers by path (a list is served in order)."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/token":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"bearer-{self.token_calls}", "expires_in": 3600})
        self.requests.append(request)
        answer = self.routes.get(request.url.path)
        if isinstance(answer, list) and answer and isinstance(answer[0], (httpx.Response, dict)):
            answer = answer.pop(0)
        if isinstance(answer, httpx.Response):
            return answer
        if answer is None:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=answer)


@pytest.fixture
def spendesk_api(monkeypatch):
    from app.integrations import oauth_token_grants

    oauth_token_grants.clear_token_cache()

    def _install(routes):
        recorder = _Recorder(routes)
        transport = httpx.MockTransport(recorder)

        def _http(*args, **kwargs):
            kwargs.pop("transport", None)
            return httpx.AsyncClient(*args, transport=transport, **kwargs)

        async def _no_dns(url, *args, **kwargs):
            return None

        monkeypatch.setattr(_client, "egress_client", _http)
        monkeypatch.setattr(_tools, "egress_client", _http)
        monkeypatch.setattr(oauth_token_grants, "egress_client", _http)
        monkeypatch.setattr(oauth_token_grants, "validate_outbound_url_async", _no_dns)
        return recorder

    yield _install
    oauth_token_grants.clear_token_cache()


def _tools_for(token: str = TOKEN, **kwargs):
    executor = CallableToolExecutor()
    return spendesk.build_tools(token, executor, **kwargs), executor


async def _call(executor, tool_name, /, **kwargs):
    return await executor.handler(tool_name)(**kwargs)


# --- credential ----------------------------------------------------------------


def test_token_round_trip_and_environment_hosts():
    creds = parse_spendesk_token(TOKEN)
    assert (creds.environment, creds.client_id, creds.client_secret) == ("sandbox", "cid", "csecret")
    assert creds.base_url == "https://public-api.demo.spendesk.com"
    assert creds.token_url == "https://public-api.demo.spendesk.com/v1/auth/token"
    assert set(ENVIRONMENTS) == {"production", "sandbox"}


def test_unknown_environment_falls_back_to_production_never_to_a_typed_host():
    creds = parse_spendesk_token(token_from_config("s", {"environment": "https://evil.example", "client_id": "c"}))
    assert creds.host == "public-api.spendesk.com"


def test_secret_with_separator_round_trips():
    assert parse_spendesk_token(build_spendesk_token("production", "c", "a||b")).client_secret == "a||b"


async def test_a_missing_credential_is_named_at_call_time():
    _, executor = _tools_for(build_spendesk_token("production", "", ""))
    answer = await _call(executor, "get_spendesk_wallet_summary")
    assert "missing its client ID, client secret" in answer


# --- auth and entity -----------------------------------------------------------


async def test_bearer_and_entity_header_on_a_v1_read(spendesk_api):
    api = spendesk_api({"/v1/wallet-summary": WALLET})
    _, executor = _tools_for()
    answer = await _call(executor, "get_spendesk_wallet_summary", company_id="c-fr")
    assert "1250000" in answer
    sent = api.requests[0]
    assert sent.headers["Authorization"] == "Bearer bearer-1"
    assert sent.headers["X-Company-Id"] == "c-fr"
    assert "csecret" not in answer


async def test_a_401_mints_a_new_token_once(spendesk_api):
    api = spendesk_api({"/v1/wallet-summary": [httpx.Response(401, json={"message": "expired"}), WALLET]})
    _, executor = _tools_for()
    assert "1250000" in await _call(executor, "get_spendesk_wallet_summary")
    assert api.token_calls == 2


async def test_company_scoped_v2_paths_take_the_entity(spendesk_api):
    api = spendesk_api({"/v2/companies/c-us/invoices": {"data": [], "meta": {"pagination": {"nextCursor": None}}}})
    _, executor = _tools_for()
    await _call(executor, "list_spendesk_invoices", company_id="c-us")
    assert api.requests[0].url.path == "/v2/companies/c-us/invoices"
    assert "required" in await _call(executor, "list_spendesk_invoices")


async def test_validate_lists_the_entities(spendesk_api):
    spendesk_api({"/v2/companies": COMPANIES})
    ok, message = await spendesk.validate_connection(TOKEN)
    assert ok and "2 entities" in message and "Filigran Inc" in message


# --- paging ---------------------------------------------------------------------


async def test_all_pages_reads_until_the_total(spendesk_api):
    api = spendesk_api({"/v1/transactions": [_page(1, [{"id": "t1"}, {"id": "t2"}], 3), _page(2, [{"id": "t3"}], 3)]})
    _, executor = _tools_for()
    answer = await _call(executor, "list_spendesk_transactions", all_pages=True, pageSize=2, startDate="2026-10-01")
    assert "(3 rows)" in answer and "t3" in answer
    assert [r.url.params["page"] for r in api.requests] == ["1", "2"]
    assert api.requests[0].url.params["startDate"] == "2026-10-01"


async def test_one_page_names_the_page_count(spendesk_api):
    spendesk_api({"/v1/transactions": _page(1, [{"id": "t1"}, {"id": "t2"}], 5)})
    _, executor = _tools_for()
    assert "(page 1 of 3, 5 rows)" in await _call(executor, "list_spendesk_transactions", pageSize=2)


# --- guardrails -----------------------------------------------------------------


def test_read_or_write_follows_the_http_method():
    tools, _ = _tools_for()
    by_name = {t.name: t for t in tools}
    for op in _tools.operations():
        tool = by_name[op["name"]]
        if op["method"] == "GET":
            assert tool.read_only and not tool.always_ask, op["name"]
        if op["method"] in {"PUT", "PATCH", "DELETE"}:
            assert tool.always_ask and not tool.read_only, op["name"]
            assert definition_tool_type(tool) == "write", op["name"]


def test_every_write_is_named_with_a_write_verb_and_asks():
    tools, _ = _tools_for()
    writes = [t for t in tools if not t.read_only]
    # 49 writes of the Public API (21 Nov 2026): every POST, PUT, PATCH and DELETE but the 3 read-scoped POSTs.
    assert len(writes) == sum(1 for op in _tools.operations() if not op["read"]) == 49
    for tool in writes:
        assert tool.always_ask, tool.name
        assert definition_tool_type(tool) == "write", tool.name
        assert "Sensitive write" in tool.description


def test_descriptions_are_plain_and_cut_at_a_sentence_end() -> None:
    """Vendor prose is cut at a sentence end, never mid-word, with no Markdown link, backtick or long dash."""
    tools, _ = _tools_for()
    for op in _tools.operations():
        detail = _tools._detail(op)
        assert len(detail) <= _tools.DETAIL_LIMIT, op["name"]
        if len(_tools._plain(str(op.get("description") or ""))) > _tools.DETAIL_LIMIT:
            assert not detail or detail[-1] in ".!?", (op["name"], detail[-40:])
            assert not re.search(r"(?:^|\s)\d+\.$", detail), (op["name"], detail[-40:])
    for tool in tools:
        text = tool.description
        assert "](" not in text, tool.name
        assert "`" not in text, tool.name
        assert "\u2014" not in text and "\u2013" not in text, tool.name
        assert text.endswith("."), tool.name


def test_detail_drops_a_partial_sentence() -> None:
    op = {"description": "First sentence. " + "word " * 80, "summary": "Do it"}
    assert _tools._detail(op) == "First sentence."
    op = {"description": "See [this guide](/reference/x) \u2014 then `call` it.", "summary": "Do it"}
    assert _tools._detail(op) == "See this guide, then call it."


def test_detail_never_ends_on_a_bare_list_number() -> None:
    """A list number's period ("3.") is not a sentence end: the cut falls on the step before it."""
    op = {"description": "1. Create it. 2. Upload until it returns an id. 3. " + "word " * 80, "summary": "Do it"}
    assert _tools._detail(op) == "1. Create it. 2. Upload until it returns an id."


async def test_writes_carry_an_idempotency_key(spendesk_api):
    api = spendesk_api({"/v1/suppliers/s1": SUPPLIER})
    _, executor = _tools_for()
    await _call(executor, "update_spendesk_supplier", id="s1", name="ACME SAS")
    await _call(executor, "update_spendesk_supplier", id="s1", name="ACME SAS", idempotency_key="k-1")
    assert api.requests[0].headers.get("Idempotency-Key")
    assert api.requests[1].headers["Idempotency-Key"] == "k-1"


def test_bank_details_group_is_named_and_built():
    group = spendesk.bank_details_tools()
    assert {"list_spendesk_suppliers", "get_spendesk_supplier", "update_spendesk_supplier"} <= group
    tools, _ = _tools_for()
    assert group <= {t.name for t in tools}


async def test_bank_details_are_masked(spendesk_api):
    spendesk_api({"/v1/suppliers/s1": SUPPLIER})
    _, executor = _tools_for()
    answer = await _call(executor, "get_spendesk_supplier", id="s1")
    assert "FR7630006000011234567890189" not in answer and "****0189" in answer
    assert "AGRIFRPP" not in answer


def test_redact_drops_secrets():
    assert _tools.redact({"secret": "s", "nested": [{"accessToken": "t"}]}) == {
        "secret": "[redacted]",
        "nested": [{"accessToken": "[redacted]"}],
    }


# --- files ----------------------------------------------------------------------


async def test_receipt_download_is_delivered_without_the_bearer(spendesk_api):
    api = spendesk_api(
        {
            "/v1/payables/p1/attachments": {
                "data": [{"url": "https://public-api.demo.spendesk.com/files/r.pdf", "mimeType": "application/pdf"}]
            },
            "/files/r.pdf": httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}),
        }
    )
    stored: list[tuple] = []

    async def persist(filename, data, content_type):
        stored.append((data, filename, content_type))
        return SimpleNamespace(file_id="f1", filename=filename, download_url="/files/f1", url="/files/f1")

    _, executor = _tools_for(persist_file_fn=persist)
    await _call(executor, "download_spendesk_payable_attachment", payable_id="p1")
    assert stored and stored[0][0] == b"%PDF-1.4"
    assert "Authorization" not in api.requests[-1].headers


async def test_a_payable_without_attachment_is_a_missing_receipt(spendesk_api):
    spendesk_api({"/v1/payables/p2/attachments": {"data": []}})
    _, executor = _tools_for()
    assert "missing receipt" in await _call(executor, "download_spendesk_payable_attachment", payable_id="p2")


# --- webhooks -------------------------------------------------------------------


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_check():
    body = json.dumps({"event": {"entity": "payables", "eventType": "created"}, "data": {"id": "p1"}}).encode()
    assert verify_signature(body, "whsec", _sign(body, "whsec")) == (True, "")
    assert verify_signature(body, "whsec", _sign(body, "other"))[0] is False
    assert verify_signature(body, "", "x")[0] is False
    assert verify_signature(body, "whsec", "")[1] == f"missing {SIGNATURE_HEADER}"


def test_event_keys():
    assert {e: e.replace(":", "_") for e in EVENT_TYPES} == TRIGGER_EVENTS
    assert event_key({"event": {"entity": "settlements", "eventType": "created"}}) == "settlements_created"
    assert event_key({"event": "x"}) == ""


# --- coverage and catalog -------------------------------------------------------


def test_every_operation_becomes_one_tool_with_a_unique_name():
    tools, executor = _tools_for()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)) == len(_tools.operations()) + 2
    for name in names:
        assert executor.handler(name) is not None
        assert len(name) <= 64


def test_catalog_entry_and_dispatcher():
    entry = PROVIDER_CATALOG["spendesk"]
    assert entry["integration_category"] == "business_operations"
    assert {e["event"] for e in entry["available_events"]} == set(TRIGGER_EVENTS.values())
    assert set(entry["extra_fields"]) == {"environment", "client_id", "webhook_secret"}
    assert PROVIDER_TOOLS["spendesk"] is spendesk.build_tools
    assert build_dummy_token("spendesk")
