"""Pennylane provider: what each tool sends, and what it makes of the answer.

The fake API is routed by path under ``/api/external/v2`` and answers with
records shaped like the documented Company API v2 answers. It records every
request, so a test pins the query (``filter`` JSON, ``cursor``), the headers
(Bearer, ``X-Company-Id``) and the body as well as the rendering.
"""

from __future__ import annotations

import contextlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.api.v1 import integration_testing
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers import pennylane
from app.integrations.providers.pennylane import _common, catalog
from app.services.integration_tools import PROVIDER_TOOLS, _SafeIntegrationExecutor
from app.services.tool_classification import definition_tool_type
from tests.integration_file_fakes import RecordingPersist, call, fake_api, file_context_with

PREFIX = "/api/external/v2/"


class _Unpaced:
    @contextlib.asynccontextmanager
    async def slot(self, *args, **kwargs):
        yield


@pytest.fixture(autouse=True)
def _unpaced(monkeypatch):
    """The per-token limiter is process-wide; its lock must not outlive a test's event loop."""
    monkeypatch.setattr(_common, "get_shared_rate_limiter", lambda *a, **k: _Unpaced())


def _executor(token: str = "pl-test-token", **kwargs) -> tuple[_SafeIntegrationExecutor, list]:  # noqa: S107
    executor = _SafeIntegrationExecutor()
    tools = pennylane.build_tools(token, executor, **kwargs)
    return executor, tools


class FakePennylane:
    """Answers each path (``GET supplier_invoices``) from *routes*."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.url.path.startswith(PREFIX):
            route = self.routes.get(f"{request.method} {request.url}")
        else:
            route = self.routes.get(f"{request.method} {request.url.path[len(PREFIX) :]}")
        if route is None:
            return httpx.Response(404, json={"error": f"no route for {request.method} {request.url.path}"})
        answer = route(request) if callable(route) else route
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def query(self, path: str) -> list[dict]:
        return [
            {k: v[0] for k, v in parse_qs(r.url.query.decode()).items()}
            for r in self.requests
            if r.url.path == PREFIX + path
        ]

    def bodies(self, method: str, path: str) -> list:
        return [
            json.loads(r.content or b"null")
            for r in self.requests
            if r.method == method and r.url.path == PREFIX + path
        ]


def listing(items, next_cursor=None):
    return {"items": items, "has_more": bool(next_cursor), "next_cursor": next_cursor}


ME = {
    "user": {"id": 1, "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "locale": "fr"},
    "company": {"id": 42, "name": "Example SAS", "reg_no": "123456789", "accounting_logic": "cash"},
    "scopes": ["supplier_invoices:readonly", "ledger_entries:readonly"],
}
SUPPLIER_INVOICE = {
    "id": 501,
    "label": "Cloud hosting October",
    "invoice_number": "F-2026-10",
    "currency": "EUR",
    "amount": "1200.0",
    "currency_amount": "1200.0",
    "currency_amount_before_tax": "1000.0",
    "date": "2026-10-31",
    "deadline": "2026-11-30",
    "remaining_amount_with_tax": "1200.0",
    "paid": False,
    "payment_status": "to_be_paid",
    "accounting_status": "complete",
    "supplier": {"id": 77, "name": "Hosting Co", "url": "x"},
    "categories": [{"id": 9, "label": "R&D", "weight": "1", "category_group": {"id": 3, "label": "Department"}}],
    "public_file_url": "https://files.example.com/signed/501.pdf",
    "filename": "F-2026-10.pdf",
    "ledger_entry": {"id": 9001},
}


# ---------------------------------------------------------------------------
# Catalog, wiring and classification
# ---------------------------------------------------------------------------


def test_catalog_lists_the_provider_with_its_fields():
    entry = PROVIDER_CATALOG["pennylane"]
    assert entry["auth_type"] == "api_key"
    assert entry["fields"] == ["api_key"]
    assert "company_id" in entry["extra_fields"]
    assert entry["logo"] == "/logos/pennylane.svg"
    assert PROVIDER_TOOLS["pennylane"] is pennylane.build_tools


def test_every_tool_is_registered_and_named_for_the_provider():
    executor, tools = _executor()
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    assert all("pennylane" in n for n in names)
    for name in names:
        assert name in executor._handlers


#: Hand-written tools whose calls change nothing in Pennylane (the catalog's reads are its GETs).
READS = {
    "get_pennylane_company",
    "list_pennylane_fiscal_years",
    "list_pennylane_journals",
    "list_pennylane_ledger_accounts",
    "list_pennylane_ledger_entries",
    "get_pennylane_ledger_entry",
    "search_pennylane_ledger_entry_lines",
    "get_pennylane_trial_balance",
    "get_pennylane_export",
    "list_pennylane_customer_invoices",
    "list_pennylane_supplier_invoices",
    "get_pennylane_invoice",
    "download_pennylane_invoice_pdf",
    "list_pennylane_customers",
    "get_pennylane_customer",
    "list_pennylane_suppliers",
    "get_pennylane_supplier",
    "list_pennylane_products",
    "list_pennylane_quotes",
    "list_pennylane_bank_accounts",
    "list_pennylane_transactions",
    "list_pennylane_categories",
    "list_pennylane_changes",
} | {op.name for op in catalog.OPS if op.method == "get"}


def test_reads_are_reads_and_every_other_tool_is_a_write():
    """integration_read_only withholds exactly the tools that change Pennylane."""
    _, tools = _executor()
    for tool in tools:
        expected = "read" if tool.name in READS else "write"
        assert definition_tool_type(tool) == expected, tool.name
        assert tool.read_only is (tool.name in READS), tool.name
    writes = {t.name for t in tools} - READS
    assert "mark_pennylane_customer_invoice_paid" in writes
    assert "create_pennylane_export" in writes


def test_the_whole_api_surface_is_exposed():
    """Every Company API v2 operation has a tool: nothing left out by policy."""
    _, tools = _executor()
    names = {t.name for t in tools}
    for expected in (
        "create_pennylane_bank_account",
        "list_pennylane_bank_establishments",
        "create_pennylane_sepa_mandate",
        "cancel_pennylane_gocardless_mandate",
        "request_pennylane_pro_account_mandate",
        "letter_pennylane_ledger_lines",
        "finalize_pennylane_customer_invoice",
        "set_pennylane_supplier_e_invoice_status",
        "create_pennylane_webhook_subscription",
    ):
        assert expected in names
    assert len(names) >= 140


def test_sensitive_writes_wait_for_a_person():
    _, tools = _executor()
    by_name = {t.name: t for t in tools}
    for name in (
        "mark_pennylane_customer_invoice_paid",
        "set_pennylane_supplier_invoice_payment_status",
        "create_pennylane_bank_account",
        "create_pennylane_sepa_mandate",
        "delete_pennylane_sepa_mandate",
        "cancel_pennylane_gocardless_mandate",
        "send_pennylane_customer_invoice",
        "finalize_pennylane_customer_invoice",
    ):
        assert by_name[name].always_ask, name
    assert not any(t.always_ask for t in tools if t.read_only)


def test_bank_tools_form_one_named_group():
    _, tools = _executor()
    names = {t.name for t in tools}
    bank = set(pennylane.BANK_DETAIL_TOOLS)
    assert bank.issubset(names)
    assert "list_pennylane_bank_accounts" in bank
    assert all("mandate" not in n or n in pennylane.BANK_DETAIL_TOOLS for n in names)


# ---------------------------------------------------------------------------
# Auth, connection test and errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_test_reads_me_and_names_company_and_scopes():
    api = FakePennylane({"GET me": ME})
    with fake_api(api):
        result = await integration_testing.test_provider_connectivity("pennylane", "pl-key", {}, "Pennylane")
    assert result["status"] == "ok"
    assert "Example SAS" in result["message"] and "supplier_invoices:readonly" in result["message"]
    request = api.requests[0]
    assert request.headers["Authorization"] == "Bearer pl-key"
    assert "X-Company-Id" not in request.headers


@pytest.mark.asyncio
async def test_firm_token_sends_the_company_id_header():
    api = FakePennylane({"GET me": ME})
    with fake_api(api):
        result = await integration_testing.test_provider_connectivity(
            "pennylane", "firm-key", {"company_id": "42"}, "Pennylane"
        )
    assert result["status"] == "ok"
    assert api.requests[0].headers["X-Company-Id"] == "42"
    assert api.requests[0].headers["Authorization"] == "Bearer firm-key"


@pytest.mark.asyncio
async def test_expired_token_is_named_and_never_echoed():
    api = FakePennylane({"GET me": httpx.Response(401, json={"error": "Unauthorized"})})
    with fake_api(api):
        ok, message = await pennylane.validate_connection("secret-token-value")
    assert not ok
    assert "rejected the API token" in message
    assert "secret-token-value" not in message


@pytest.mark.asyncio
async def test_missing_scope_names_the_scope_to_grant():
    api = FakePennylane({"GET trial_balance": httpx.Response(403, json={"error": "Forbidden"})})
    executor, _ = _executor()
    with fake_api(api):
        result = await call(executor, "get_pennylane_trial_balance", period_start="2026-01-01", period_end="2026-03-31")
    assert "scope missing" in result.content
    assert "trial_balance:readonly" in result.content


@pytest.mark.asyncio
async def test_rate_limit_is_retried(monkeypatch):
    import app.integrations.http as http_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(http_module.asyncio, "sleep", no_sleep)
    answers = iter([httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json=listing([]))])
    api = FakePennylane({"GET fiscal_years": lambda _r: next(answers)})
    executor, _ = _executor()
    with fake_api(api):
        result = await call(executor, "list_pennylane_fiscal_years")
    assert result.content == "No fiscal year."
    assert len(api.requests) == 2


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supplier_invoices_filter_and_render_amounts_and_labels():
    api = FakePennylane({"GET supplier_invoices": listing([SUPPLIER_INVOICE], next_cursor="c2")})
    executor, _ = _executor()
    with fake_api(api):
        result = await call(
            executor,
            "list_pennylane_supplier_invoices",
            date_from="2026-10-01",
            date_to="2026-10-31",
            supplier_ids=[77],
            payment_status="to_be_paid",
        )
    sent = api.query("supplier_invoices")[0]
    flt = json.loads(sent["filter"])
    assert {"field": "date", "operator": "gteq", "value": "2026-10-01"} in flt
    assert {"field": "supplier_id", "operator": "in", "value": "77"} in flt
    assert {"field": "payment_status", "operator": "eq", "value": "to_be_paid"} in flt
    assert sent["sort"] == "-date"
    assert "Hosting Co (#77)" in result.content
    assert "1,200.00 EUR" in result.content
    assert "Department: R&D" in result.content
    assert "c2" in result.content


@pytest.mark.asyncio
async def test_ledger_lines_by_account_prefix_resolve_labels_and_totals():
    lines = [
        {
            "id": 1,
            "date": "2026-10-02",
            "debit": "100.0",
            "credit": "0.0",
            "label": "Hosting",
            "ledger_account": {"id": 11},
            "journal": {"id": 5},
            "ledger_entry": {"id": 9001},
            "categories": [],
        },
        {
            "id": 2,
            "date": "2026-10-03",
            "debit": "0.0",
            "credit": "40.0",
            "label": "Refund",
            "ledger_account": {"id": 11},
            "journal": {"id": 5},
            "ledger_entry": {"id": 9002},
            "categories": [],
        },
    ]
    api = FakePennylane(
        {
            "GET ledger_accounts": listing(
                [{"id": 11, "number": "6061", "label": "Supplies"}, {"id": 12, "number": "401", "label": "Suppliers"}]
            ),
            "GET journals": listing([{"id": 5, "code": "HA", "label": "Purchases"}]),
            "GET ledger_entry_lines": listing(lines),
        }
    )
    executor, _ = _executor()
    with fake_api(api):
        result = await call(
            executor, "search_pennylane_ledger_entry_lines", account_number_prefix="606", date_from="2026-10-01"
        )
    flt = json.loads(api.query("ledger_entry_lines")[0]["filter"])
    assert {"field": "ledger_account_id", "operator": "in", "value": "11"} in flt
    assert "6061 Supplies" in result.content and "HA Purchases" in result.content
    assert "debit 100.00, credit 40.00" in result.content


@pytest.mark.asyncio
async def test_trial_balance_pages_through_and_sums():
    first = listing([{"number": "401", "label": "Suppliers", "debits": "10.0", "credits": "110.0"}], next_cursor="n")
    second = listing([{"number": "512", "label": "Bank", "debits": "100.0", "credits": "0.0"}])
    api = FakePennylane({"GET trial_balance": lambda r: second if "cursor=n" in str(r.url) else first})
    executor, _ = _executor()
    with fake_api(api):
        result = await call(executor, "get_pennylane_trial_balance", period_start="2026-01-01", period_end="2026-12-31")
    assert api.query("trial_balance")[0]["period_start"] == "2026-01-01"
    assert "2 accounts" in result.content
    assert "debits 110.00 EUR, credits 110.00 EUR, net 0.00 EUR" in result.content


@pytest.mark.asyncio
async def test_invoice_detail_shows_lines_payments_and_matching():
    api = FakePennylane(
        {
            "GET supplier_invoices/501": SUPPLIER_INVOICE,
            "GET supplier_invoices/501/invoice_lines": listing(
                [{"label": "VM", "quantity": "1", "currency_amount": "1200.0", "vat_rate": "FR_200"}]
            ),
            "GET supplier_invoices/501/payments": listing([]),
            "GET supplier_invoices/501/matched_transactions": listing(
                [{"id": 3, "date": "2026-11-02", "amount": "-1200.0", "label": "SEPA Hosting Co"}]
            ),
        }
    )
    executor, _ = _executor()
    with fake_api(api):
        result = await call(executor, "get_pennylane_invoice", kind="supplier", invoice_id=501)
    assert "VM" in result.content and "FR_200" in result.content
    assert "SEPA Hosting Co" in result.content
    assert "Ledger entry: #9001" in result.content


@pytest.mark.asyncio
async def test_invoice_pdf_lands_in_the_conversation_without_the_token():
    api = FakePennylane(
        {
            "GET supplier_invoices/501": SUPPLIER_INVOICE,
            "GET https://files.example.com/signed/501.pdf": httpx.Response(
                200, content=b"%PDF-1.4 x", headers={"Content-Type": "application/pdf"}
            ),
        }
    )
    persist = RecordingPersist()
    executor, _ = _executor(persist_file_fn=persist)
    with fake_api(api):
        await call(executor, "download_pennylane_invoice_pdf", kind="supplier", invoice_id=501)
    assert persist.calls and persist.calls[0][0] == "F-2026-10.pdf"
    storage = next(r for r in api.requests if r.url.host == "files.example.com")
    assert "Authorization" not in storage.headers


@pytest.mark.asyncio
async def test_supplier_iban_is_masked():
    api = FakePennylane(
        {"GET suppliers": listing([{"id": 77, "name": "Hosting Co", "iban": "FR7630006000011234567890189"}])}
    )
    executor, _ = _executor()
    with fake_api(api):
        result = await call(executor, "list_pennylane_suppliers")
    assert "FR7630006000011234567890189" not in result.content
    assert "FR** **** 0189" in result.content


@pytest.mark.asyncio
async def test_unmatched_transactions_only():
    api = FakePennylane(
        {
            "GET transactions": listing(
                [
                    {
                        "id": 1,
                        "date": "2026-11-01",
                        "amount": "-50.0",
                        "currency": "EUR",
                        "label": "Card",
                        "matched_invoices": [],
                    },
                    {
                        "id": 2,
                        "date": "2026-11-02",
                        "amount": "-1200.0",
                        "currency": "EUR",
                        "label": "SEPA",
                        "matched_invoices": [{"id": 501, "type": "supplier"}],
                    },
                ]
            )
        }
    )
    executor, _ = _executor()
    with fake_api(api):
        result = await call(executor, "list_pennylane_transactions", unmatched_only=True)
    assert "Card" in result.content and "SEPA" not in result.content


# The sort fields each list endpoint of the company API v2 accepts (its reference, "Available
# fields"); a field outside the list answers 400, as GET transactions did with -date (#2612).
ALLOWED_SORT_FIELDS = {
    "transactions": {"id"},
    "ledger_entries": {"id", "date"},
    "ledger_entry_lines": {"id", "date"},
    "supplier_invoices": {"id", "date"},
    "customer_invoices": {"id", "date"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "path", "arguments"),
    [
        ("list_pennylane_transactions", "transactions", {"date_from": "2026-11-16", "date_to": "2026-11-22"}),
        ("list_pennylane_transactions", "transactions", {}),
        ("list_pennylane_ledger_entries", "ledger_entries", {}),
        ("search_pennylane_ledger_entry_lines", "ledger_entry_lines", {}),
        ("list_pennylane_supplier_invoices", "supplier_invoices", {}),
        ("list_pennylane_customer_invoices", "customer_invoices", {}),
    ],
)
async def test_every_list_sorts_on_a_field_the_endpoint_allows(tool, path, arguments):
    api = FakePennylane(
        {
            f"GET {path}": listing([]),
            "GET ledger_accounts": listing([]),
            "GET journals": listing([]),
        }
    )
    executor, _ = _executor()
    with fake_api(api):
        await call(executor, tool, **arguments)
    sent = api.query(path)
    assert sent, f"{tool} never called GET {path}"
    sort = sent[0]["sort"]
    assert sort.removeprefix("-") in ALLOWED_SORT_FIELDS[path]
    if path == "transactions":
        assert sort == "-id"
        flt = json.loads(sent[0].get("filter", "[]"))
        if arguments:
            assert {"field": "date", "operator": "gteq", "value": "2026-11-16"} in flt
            assert {"field": "date", "operator": "lteq", "value": "2026-11-22"} in flt


@pytest.mark.asyncio
async def test_changes_since_read_the_changelog():
    api = FakePennylane(
        {
            "GET changelogs/supplier_invoices": listing(
                [{"id": 501, "operation": "insert", "processed_at": "2026-11-21T10:00:00Z"}]
            )
        }
    )
    executor, _ = _executor()
    with fake_api(api):
        result = await call(executor, "list_pennylane_changes", resource="supplier_invoices", since="2026-11-20")
    assert api.query("changelogs/supplier_invoices")[0]["start_date"] == "2026-11-20T00:00:00Z"
    assert "insert" in result.content


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_customer_invoice_is_always_a_draft():
    api = FakePennylane(
        {"POST customer_invoices": httpx.Response(201, json={"id": 700, "currency_amount": "120.0", "currency": "EUR"})}
    )
    executor, _ = _executor()
    with fake_api(api):
        result = await call(
            executor,
            "create_pennylane_customer_invoice_draft",
            customer_id=12,
            date="2026-11-21",
            deadline="2026-12-21",
            lines=[
                {
                    "label": "Licence",
                    "raw_currency_unit_price": "100",
                    "unit": "piece",
                    "vat_rate": "FR_200",
                    "quantity": 1,
                }
            ],
        )
    body = api.bodies("POST", "customer_invoices")[0]
    assert body["draft"] is True and body["customer_id"] == 12
    assert body["invoice_lines"][0]["vat_rate"] == "FR_200"
    assert "#700" in result.content


@pytest.mark.asyncio
async def test_ledger_entry_must_balance():
    executor, _ = _executor()
    with fake_api(FakePennylane({})) as sent:
        result = await call(
            executor,
            "create_pennylane_ledger_entry",
            date="2026-11-21",
            label="Accrual",
            journal_id=5,
            lines=[{"ledger_account_id": 11, "debit": "100.00"}, {"ledger_account_id": 12, "credit": "90.00"}],
        )
    assert "Error" in result.content and "balance" in result.content
    assert not sent


@pytest.mark.asyncio
async def test_categorise_a_transaction_with_an_equal_split():
    api = FakePennylane({"PUT transactions/3/categories": httpx.Response(200, json=[])})
    executor, _ = _executor()
    with fake_api(api):
        result = await call(
            executor, "set_pennylane_categories", object_type="transaction", object_id=3, categories=[9, 10]
        )
    body = api.bodies("PUT", "transactions/3/categories")[0]
    assert [c["id"] for c in body] == [9, 10]
    assert sum(float(c["weight"]) for c in body) == pytest.approx(1.0)
    assert "transaction #3" in result.content


@pytest.mark.asyncio
async def test_match_and_mark_paid():
    api = FakePennylane(
        {
            "POST supplier_invoices/501/matched_transactions": httpx.Response(201, json={}),
            "PUT customer_invoices/700/mark_as_paid": httpx.Response(204),
        }
    )
    executor, _ = _executor()
    with fake_api(api):
        matched = await call(
            executor, "match_pennylane_invoice_transaction", kind="supplier", invoice_id=501, transaction_id=3
        )
        paid = await call(executor, "mark_pennylane_customer_invoice_paid", invoice_id=700)
    assert api.bodies("POST", "supplier_invoices/501/matched_transactions")[0] == {"transaction_id": 3}
    assert "Matched bank transaction #3" in matched.content
    assert "no money moved" in paid.content


@pytest.mark.asyncio
async def test_supplier_payment_status_accepts_only_the_documented_values():
    executor, _ = _executor()
    with fake_api(FakePennylane({})) as sent:
        result = await call(
            executor, "set_pennylane_supplier_invoice_payment_status", invoice_id=501, payment_status="wire_now"
        )
    assert "Error" in result.content and not sent


@pytest.mark.asyncio
async def test_catalog_tools_fill_the_path_and_pass_the_body():
    api = FakePennylane(
        {
            "POST ledger_entry_lines/lettering": httpx.Response(201, json={"id": 1}),
            "GET sepa_mandates": listing(
                [{"id": 4, "iban": "FR7630006000011234567890189", "customer": {"id": 12, "name": "Acme"}}]
            ),
            "GET customer_invoices/7/categories": [{"id": 9, "label": "R&D", "weight": "1"}],
        }
    )
    executor, _ = _executor()
    with fake_api(api):
        lettered = await call(executor, "letter_pennylane_ledger_lines", ledger_entry_lines=[{"id": 1}, {"id": 2}])
        mandates = await call(executor, "list_pennylane_sepa_mandates")
        cats = await call(executor, "get_pennylane_invoice_categories", kind="customer", id=7)
        refused = await call(executor, "get_pennylane_invoice_categories", kind="other", id=7)
    assert api.bodies("POST", "ledger_entry_lines/lettering")[0] == {"ledger_entry_lines": [{"id": 1}, {"id": 2}]}
    assert "Done" in lettered.content
    assert "FR7630006000011234567890189" not in mandates.content and "0189" in mandates.content
    assert "Acme" in mandates.content
    assert "R&D" in cats.content
    assert "kind must be one of" in refused.content


@pytest.mark.asyncio
async def test_file_attachment_upload_is_multipart():
    api = FakePennylane({"POST file_attachments": httpx.Response(201, json={"id": 88, "url": "x"})})
    context, file_id = file_context_with(b"%PDF-1.4 y", "receipt.pdf", "application/pdf")
    executor, _ = _executor()
    with fake_api(api):
        result = await call(executor, "upload_pennylane_file_attachment", file_id=file_id, file_context=context)
    request = next(r for r in api.requests if r.url.path.endswith("file_attachments"))
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    assert "attachment #88" in result.content
