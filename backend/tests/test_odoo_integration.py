"""The Odoo provider on recorded external API answers (no Odoo server)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import httpx
import pytest

from app.integrations.providers.odoo import BANK_DETAIL_TOOLS, OdooCredential, build_tools, validate_connection
from app.integrations.providers.odoo._render import mask, parse_domain, sanitize
from app.integrations.providers.odoo._reports import aged_balance, bucket_of
from app.tools.base import CallableToolExecutor, ToolDefinition
from tests.integration_file_fakes import RecordingPersist, call, fake_api

TOKEN = OdooCredential.build("https://acme.odoo.com/", "acme", "bot@acme.test", "SECRETKEY123", "1, 2")

ACCOUNTS = [
    {"id": 10, "code": "411000", "name": "Receivable", "account_type": "asset_receivable"},
    {"id": 20, "code": "706000", "name": "Sales", "account_type": "income"},
]


def _json2(model_method: Mapping[str, Any], *, json2: bool = True) -> Callable[[httpx.Request], httpx.Response]:
    """A responder: JSON-2 answers from *model_method*, or 404 (JSON-RPC only)."""

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path.startswith("/json/2/"):
            if not json2:
                return httpx.Response(404, text="not found")
            _, _, _, model, method = path.split("/")
            answer = model_method.get(f"{model}.{method}")
            if callable(answer):
                answer = answer(body)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer)
        if path == "/jsonrpc":
            params = body["params"]
            if params["method"] == "version":
                return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"server_version": "17.0"}})
            if params["method"] == "authenticate":
                return httpx.Response(200, json={"jsonrpc": "2.0", "result": 7})
            args = params["args"]
            model, method = args[3], args[4]
            answer = model_method.get(f"{model}.{method}")
            if callable(answer):
                answer = answer({"args": args[5], **args[6]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": answer})
        return httpx.Response(404)

    return responder


def _tools() -> tuple[CallableToolExecutor, dict[str, ToolDefinition], RecordingPersist]:
    executor = CallableToolExecutor()
    persist = RecordingPersist()
    tools = build_tools(TOKEN, executor, persist_file_fn=persist)
    return executor, {t.name: t for t in tools}, persist


def test_credential_round_trip_and_default_companies() -> None:
    cred = OdooCredential.parse(TOKEN)
    assert (cred.base_url, cred.database, cred.login, cred.api_key) == (
        "https://acme.odoo.com",
        "acme",
        "bot@acme.test",
        "SECRETKEY123",
    )
    assert cred.company_ids == (1, 2)
    assert OdooCredential.parse("||db||login||").missing() == "the Odoo URL"


def test_every_write_asks_and_reads_do_not() -> None:
    _, tools, _ = _tools()
    writes = {n for n, t in tools.items() if not t.read_only}
    assert writes, "the write tools are always built"
    for name in writes:
        tool = tools[name]
        assert tool.always_ask, name
        assert tool.description.startswith("SENSITIVE"), name
        assert name.split("_", 1)[0] in {
            "create",
            "update",
            "post",
            "reset",
            "cancel",
            "register",
            "reconcile",
            "set",
            "delete",
            "call",
        }, name
    for name, tool in tools.items():
        if tool.read_only:
            assert not tool.always_ask, name
    assert {
        "create_odoo_journal_entry",
        "register_odoo_payment",
        "reconcile_odoo_journal_items",
        "set_odoo_lock_dates",
    } <= writes
    assert set(tools) >= BANK_DETAIL_TOOLS
    read_only_call = tools["call_odoo_method"].read_only_call
    assert read_only_call is not None
    assert read_only_call({"method": "search_read"}) is True
    assert read_only_call({"method": "action_post"}) is False


def test_masking_and_secret_redaction() -> None:
    out = sanitize(
        {"acc_number": "FR7630006000011234567890189", "password": "x", "bank_account_id": [3, "US 1234 5678 9999"]}
    )
    assert out["acc_number"] == "****0189"
    assert out["password"] == "[redacted]"
    assert out["bank_account_id"] == [3, "****9999"]
    assert mask("12") == "****"


def test_domain_parsing() -> None:
    assert parse_domain('[["state", "=", "posted"], "|", ["a", "=", 1], ["b", "=", 2]]')[1] is None
    assert str(parse_domain("nope")[1]).startswith("Error")
    assert str(parse_domain('[["state", "="]]')[1]).startswith("Error")


async def test_json2_companies_with_allowed_company_context() -> None:
    executor, _, _ = _tools()
    answers = {
        "res.users.context_get": {"allowed_company_ids": [1, 2], "tz": "America/New_York"},
        "res.company.search_read": [{"id": 1, "name": "Acme SAS"}, {"id": 2, "name": "Acme Inc"}],
    }
    with fake_api(_json2(answers)) as sent:
        result = await call(executor, "list_odoo_companies")
    assert "Acme Inc" in result.content
    first = sent[0]
    assert first.url.path == "/json/2/res.users/context_get"
    assert first.headers["Authorization"] == "bearer SECRETKEY123"
    assert first.headers["X-Odoo-Database"] == "acme"
    assert json.loads(first.content)["context"]["allowed_company_ids"] == [1, 2]
    assert "SECRETKEY123" not in result.content


async def test_jsonrpc_fallback_when_json2_is_absent() -> None:
    executor, _, _ = _tools()
    answers = {"account.account.search_read": ACCOUNTS}
    with fake_api(_json2(answers, json2=False)) as sent:
        result = await call(executor, "list_odoo_accounts", company_ids=[2])
    assert "706000" in result.content
    paths = [r.url.path for r in sent]
    assert paths[0].startswith("/json/2/") and "/jsonrpc" in paths
    rpc = [json.loads(r.content) for r in sent if r.url.path == "/jsonrpc"]
    execute = next(b for b in rpc if b["params"]["method"] == "execute_kw")
    assert execute["params"]["args"][6]["context"]["allowed_company_ids"] == [2]


async def test_odoo_fault_is_one_sentence_without_traceback() -> None:
    executor, _, _ = _tools()
    fault = httpx.Response(
        403,
        json={
            "name": "odoo.exceptions.AccessError",
            "message": "You are not allowed to access 'Journal Entry'.",
            "debug": "Traceback (most recent call last): secret",
        },
    )
    with fake_api(_json2({"account.move.search_read": fault})):
        result = await call(executor, "search_odoo_journal_entries", state="posted")
    assert "access rights" in result.content
    assert "Traceback" not in result.content


async def test_trial_balance_opening_and_period() -> None:
    executor, _, _ = _tools()

    def groups(body: dict[str, Any]) -> list[dict[str, Any]]:
        domain = body["domain"]
        if any(c[0] == "date" and c[1] == "<" for c in domain if isinstance(c, list)):
            return [{"account_id": [10, "Receivable"], "debit": 100.0, "credit": 0.0, "balance": 100.0}]
        return [
            {"account_id": [10, "Receivable"], "debit": 50.0, "credit": 30.0, "balance": 20.0},
            {"account_id": [20, "Sales"], "debit": 0.0, "credit": 50.0, "balance": -50.0},
        ]

    answers = {"account.move.line.read_group": groups, "account.account.search_read": ACCOUNTS}
    with fake_api(_json2(answers)):
        result = await call(executor, "get_odoo_trial_balance", date_from="2026-10-01", date_to="2026-10-31")
    rows = json.loads(result.content.split("\n", 1)[1])
    receivable = next(r for r in rows if r["code"] == "411000")
    assert (receivable["opening_balance"], receivable["closing_balance"]) == (100.0, 120.0)
    assert "period debit 50.00" in result.content


def test_aged_buckets() -> None:
    assert bucket_of("2026-11-01", date(2026, 11, 1)) == "not_due"
    assert bucket_of("2026-08-01", date(2026, 11, 21)) == "91_120"
    lines = [
        {"partner_id": [5, "Vendor"], "date_maturity": "2026-06-01", "amount_residual": -300.0},
        {"partner_id": [5, "Vendor"], "date_maturity": "2026-11-30", "amount_residual": -100.0},
    ]
    rows = aged_balance(lines, "payable", "2026-11-21")
    assert rows[0]["total"] == 400.0 and rows[0]["older"] == 300.0 and rows[0]["not_due"] == 100.0


async def test_export_trial_balance_xlsx_is_a_file() -> None:
    executor, _, persist = _tools()
    answers = {
        "account.move.line.read_group": [{"account_id": [20, "Sales"], "debit": 0.0, "credit": 10.0, "balance": -10.0}],
        "account.account.search_read": ACCOUNTS,
    }
    with fake_api(_json2(answers)):
        await call(executor, "export_odoo_report", report="trial_balance", date_to="2026-10-31")
    assert persist.calls and persist.calls[0][0].endswith(".xlsx")
    assert persist.calls[0][1][:2] == b"PK"


async def test_attachments_come_into_the_conversation() -> None:
    executor, _, persist = _tools()
    pdf = base64.b64encode(b"%PDF-1.4 bill").decode()
    answers = {
        "ir.attachment.search_read": [{"id": 1, "name": "bill.pdf", "mimetype": "application/pdf", "datas": pdf}]
    }
    with fake_api(_json2(answers)):
        await call(executor, "download_odoo_attachments", move_id=42)
    assert persist.calls[0][0] == "bill.pdf" and persist.calls[0][1] == b"%PDF-1.4 bill"


async def test_register_payment_runs_the_wizard() -> None:
    executor, _, _ = _tools()
    answers = {"account.payment.register.create": [9], "account.payment.register.action_create_payments": True}
    with fake_api(_json2(answers)) as sent:
        result = await call(executor, "register_odoo_payment", move_ids=[42], journal_id=3)
    assert "sends no money" in result.content
    create = json.loads(sent[0].content)
    assert create["context"]["active_ids"] == [42]
    assert json.loads(sent[1].content)["ids"] == [9]


async def test_partner_bank_accounts_are_masked() -> None:
    executor, _, _ = _tools()
    answers = {"res.partner.bank.search_read": [{"id": 1, "acc_number": "GB29NWBK60161331926819"}]}
    with fake_api(_json2(answers)):
        result = await call(executor, "list_odoo_partner_bank_accounts")
    assert "****6819" in result.content and "60161331926819" not in result.content


@pytest.mark.parametrize("missing", ["base_url", "api_key"])
async def test_validate_names_the_missing_part(missing: str) -> None:
    parts = {"base_url": "https://a.odoo.com", "database": "a", "login": "u", "api_key": "k"}
    parts[missing] = ""
    ok, message = await validate_connection(OdooCredential.build(**parts))
    assert not ok and message.startswith("Missing")


async def test_validate_connection_names_version_and_companies() -> None:
    answers = {"res.users.context_get": {"tz": "UTC"}, "res.company.search_read": [{"id": 1, "name": "Acme Inc"}]}
    with fake_api(_json2(answers)):
        ok, message = await validate_connection(TOKEN)
    assert ok and "17.0" in message and "Acme Inc (#1)" in message and "SECRETKEY123" not in message
