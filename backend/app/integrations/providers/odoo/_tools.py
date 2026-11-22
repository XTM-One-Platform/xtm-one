# SPDX-License-Identifier: Apache-2.0

"""The Odoo tools: accounting reads, reports and exports, guarded writes.

Reads are a table of :class:`ListSpec` (one Odoo model, its fields, the
filters an agent may pass) plus the reports of :mod:`._reports`. Every tool
takes ``company_ids`` (sent as ``allowed_company_ids``), so one integration
serves every company the user can see.

Read or write follows the Odoo operation, never the tool name: a tool whose
calls only run ``search_read``, ``read``, ``fields_get``, ``search_count`` or
``read_group`` is ``read_only``; every other tool is a write, built always,
named with a write verb, described SENSITIVE and ``always_ask`` (a person
approves each call). The generic ``call_odoo_method`` is a read for a read
method (``read_only_call``) and a write otherwise.

Bank details (``res.partner.bank``, the bank account of a bank journal) are
the named group :data:`BANK_DETAIL_TOOLS`: account numbers answer masked to
their last four characters, their writes ask a person.
"""

from __future__ import annotations

import base64
import functools
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.integrations.file_delivery import deliver_file
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from . import _reports as rep
from ._client import READ_METHODS, SOURCE, OdooClient, OdooCredential, OdooError
from ._render import (
    clamp,
    id_list,
    m2o,
    offset_of,
    page_note,
    parse_domain,
    parse_values,
    render,
    str_list,
    to_date,
)

SENSITIVE = "SENSITIVE (a person approves each call): "

#: Tools that read or change bank account numbers.
BANK_DETAIL_TOOLS = frozenset(
    {
        "list_odoo_partner_bank_accounts",
        "list_odoo_journal_bank_accounts",
        "create_odoo_partner_bank_account",
        "update_odoo_partner_bank_account",
    }
)

_COMPANIES = {
    "type": "array",
    "items": {"type": "integer"},
    "description": (
        "Odoo company ids to act for (allowed_company_ids); list them with list_odoo_companies. "
        "Omit for the integration's default companies."
    ),
}
_LIMIT = {"type": "integer", "description": "Rows per page (1-500, default 80)"}
_OFFSET = {"type": "integer", "description": "Rows to skip (the next page's offset from the previous answer)"}
_DATE_FROM = {"type": "string", "description": "From this date (YYYY-MM-DD)"}
_DATE_TO = {"type": "string", "description": "Up to this date included (YYYY-MM-DD)"}
_IDS = {"type": "array", "items": {"type": "integer"}}

MOVE_TYPES = {
    "customer_invoice": "out_invoice",
    "vendor_bill": "in_invoice",
    "customer_credit_note": "out_refund",
    "vendor_credit_note": "in_refund",
}


@dataclass(frozen=True)
class Filter:
    """An argument of a list tool and the domain condition it becomes."""

    field: str
    operator: str
    #: date, ids, str, strs, bool, text
    kind: str
    description: str = ""


@dataclass(frozen=True)
class ListSpec:
    name: str
    model: str
    summary: str
    fields: tuple[str, ...]
    filters: dict[str, Filter] = field(default_factory=dict)
    order: str = "id desc"
    base_domain: tuple[Any, ...] = ()
    priority: int = 1


def _ids(field_name: str, what: str) -> Filter:
    return Filter(field_name, "in", "ids", f"Only these {what} ids")


_DATES = {
    "date_from": Filter("date", ">=", "date", "From this date (YYYY-MM-DD)"),
    "date_to": Filter("date", "<=", "date", "Up to this date included (YYYY-MM-DD)"),
}
_MOVE_FIELDS = (
    "name",
    "date",
    "ref",
    "move_type",
    "state",
    "journal_id",
    "partner_id",
    "amount_total",
    "amount_residual",
    "currency_id",
    "payment_state",
    "invoice_date",
    "invoice_date_due",
    "company_id",
)
_LINE_FIELDS = (*rep.GL_FIELDS, "reconciled", "date_maturity", "amount_residual", "analytic_distribution")

LISTS: tuple[ListSpec, ...] = (
    ListSpec(
        "list_odoo_accounts",
        "account.account",
        "The chart of accounts: code, name, type, reconcilable flag and currency.",
        ("code", "name", "account_type", "reconcile", "currency_id", "deprecated", "company_ids", "company_id"),
        {
            "code_prefix": Filter("code", "=like", "text", "Accounts whose code starts with this (e.g. 401)"),
            "account_type": Filter("account_type", "=", "str", "asset_receivable, liability_payable, income..."),
            "search": Filter("name", "ilike", "str", "Name contains"),
        },
        order="code",
        priority=0,
    ),
    ListSpec(
        "list_odoo_journals",
        "account.journal",
        "Journals (sale, purchase, bank, cash, general) with their code, type and currency.",
        ("name", "code", "type", "currency_id", "default_account_id", "company_id", "active"),
        {"type": Filter("type", "=", "str", "sale, purchase, bank, cash or general")},
        order="sequence, code",
        priority=0,
    ),
    ListSpec(
        "search_odoo_journal_entries",
        "account.move",
        "Journal entries (account.move) of every type by date, journal, partner, state or reference, newest first.",
        _MOVE_FIELDS,
        {
            **_DATES,
            "journal_ids": _ids("journal_id", "journal"),
            "partner_ids": _ids("partner_id", "partner"),
            "state": Filter("state", "=", "str", "draft, posted or cancel"),
            "move_type": Filter("move_type", "=", "str", "entry, out_invoice, in_invoice, out_refund, in_refund..."),
            "ref": Filter("ref", "ilike", "str", "Reference contains"),
            "name": Filter("name", "ilike", "str", "Number contains"),
        },
        order="date desc, id desc",
        priority=0,
    ),
    ListSpec(
        "search_odoo_journal_items",
        "account.move.line",
        "Journal items (account.move.line) by date, account, journal, partner, entry state or reconciliation.",
        _LINE_FIELDS,
        {
            **_DATES,
            "account_ids": _ids("account_id", "account"),
            "journal_ids": _ids("journal_id", "journal"),
            "partner_ids": _ids("partner_id", "partner"),
            "move_ids": _ids("move_id", "journal entry"),
            "state": Filter("parent_state", "=", "str", "Entry state: draft, posted or cancel"),
            "reconciled": Filter("reconciled", "=", "bool", "true for fully reconciled items, false for open ones"),
        },
        order="date desc, id desc",
        priority=0,
    ),
    ListSpec(
        "list_odoo_partners",
        "res.partner",
        "Partners (customers, vendors, contacts) by name, VAT, email or rank.",
        (
            "name",
            "display_name",
            "is_company",
            "vat",
            "email",
            "phone",
            "country_id",
            "customer_rank",
            "supplier_rank",
            "property_payment_term_id",
            "property_supplier_payment_term_id",
            "company_id",
            "active",
        ),
        {
            "search": Filter("name", "ilike", "str", "Name contains"),
            "vat": Filter("vat", "ilike", "str", "VAT number contains"),
            "email": Filter("email", "ilike", "str", "Email contains"),
            "customers_only": Filter("customer_rank", ">", "bool", "true: customers only"),
            "vendors_only": Filter("supplier_rank", ">", "bool", "true: vendors only"),
        },
        order="name",
    ),
    ListSpec(
        "list_odoo_payments",
        "account.payment",
        "Payments (inbound and outbound) by date, partner, journal, state or type, with their reconciled invoices.",
        (
            "name",
            "date",
            "partner_id",
            "amount",
            "currency_id",
            "payment_type",
            "partner_type",
            "state",
            "journal_id",
            "is_reconciled",
            "is_matched",
            "reconciled_invoice_ids",
            "reconciled_bill_ids",
            "memo",
            "ref",
            "company_id",
        ),
        {
            **_DATES,
            "partner_ids": _ids("partner_id", "partner"),
            "journal_ids": _ids("journal_id", "journal"),
            "state": Filter("state", "=", "str", "draft, in_process, paid, posted, canceled..."),
            "payment_type": Filter("payment_type", "=", "str", "inbound or outbound"),
        },
        order="date desc, id desc",
    ),
    ListSpec(
        "list_odoo_bank_statements",
        "account.bank.statement",
        "Bank statements with their journal, date, starting and ending balances and completeness.",
        ("name", "date", "journal_id", "balance_start", "balance_end", "balance_end_real", "is_complete", "company_id"),
        {**_DATES, "journal_ids": _ids("journal_id", "bank journal")},
        order="date desc, id desc",
    ),
    ListSpec(
        "list_odoo_bank_statement_lines",
        "account.bank.statement.line",
        "Bank statement lines (transactions) and their reconciliation state.",
        (
            "date",
            "payment_ref",
            "partner_id",
            "amount",
            "currency_id",
            "journal_id",
            "statement_id",
            "is_reconciled",
            "amount_residual",
            "move_id",
            "company_id",
        ),
        {
            **_DATES,
            "journal_ids": _ids("journal_id", "bank journal"),
            "statement_ids": _ids("statement_id", "statement"),
            "reconciled": Filter("is_reconciled", "=", "bool", "false: lines still to reconcile"),
            "search": Filter("payment_ref", "ilike", "str", "Label contains"),
        },
        order="date desc, id desc",
    ),
    ListSpec(
        "list_odoo_taxes",
        "account.tax",
        "Taxes with their rate, computation, scope (sale or purchase) and country.",
        ("name", "amount", "amount_type", "type_tax_use", "price_include", "country_id", "active", "company_id"),
        {"scope": Filter("type_tax_use", "=", "str", "sale, purchase or none")},
        order="sequence, name",
    ),
    ListSpec(
        "list_odoo_fiscal_positions",
        "account.fiscal.position",
        "Fiscal positions (tax and account mappings by country or VAT status).",
        ("name", "auto_apply", "country_id", "vat_required", "active", "company_id"),
        {},
        order="sequence, name",
    ),
    ListSpec(
        "list_odoo_analytic_accounts",
        "account.analytic.account",
        "Analytic accounts with their plan and balance.",
        ("name", "code", "plan_id", "balance", "debit", "credit", "partner_id", "active", "company_id"),
        {"search": Filter("name", "ilike", "str", "Name contains")},
        order="name",
    ),
    ListSpec(
        "search_odoo_analytic_lines",
        "account.analytic.line",
        "Analytic lines by date or analytic account.",
        ("date", "name", "amount", "account_id", "general_account_id", "partner_id", "move_line_id", "company_id"),
        {**_DATES, "analytic_account_ids": _ids("account_id", "analytic account")},
        order="date desc, id desc",
    ),
    ListSpec(
        "list_odoo_assets",
        "account.asset",
        "Fixed assets (Odoo Accounting with assets): value, acquisition date, method, book value and state.",
        (
            "name",
            "acquisition_date",
            "original_value",
            "book_value",
            "value_residual",
            "method",
            "method_number",
            "method_period",
            "state",
            "account_asset_id",
            "company_id",
        ),
        {"state": Filter("state", "=", "str", "draft, open, paused, close")},
        order="acquisition_date desc",
    ),
    ListSpec(
        "list_odoo_currencies",
        "res.currency",
        "Active currencies with their current rate and symbol.",
        ("name", "symbol", "rate", "inverse_rate", "active", "decimal_places"),
        {"name": Filter("name", "=", "str", "ISO code, e.g. USD")},
        order="name",
        base_domain=(["active", "=", True],),
    ),
    ListSpec(
        "list_odoo_currency_rates",
        "res.currency.rate",
        "Currency rate history by currency and date.",
        ("name", "currency_id", "rate", "inverse_company_rate", "company_rate", "company_id"),
        {
            "currency_ids": _ids("currency_id", "currency"),
            "date_from": Filter("name", ">=", "date", "From this date"),
            "date_to": Filter("name", "<=", "date", "Up to this date"),
        },
        order="name desc",
    ),
    ListSpec(
        "list_odoo_products",
        "product.product",
        "Products and services with their reference, price, cost and income and expense accounts.",
        (
            "name",
            "default_code",
            "type",
            "list_price",
            "standard_price",
            "uom_id",
            "categ_id",
            "property_account_income_id",
            "property_account_expense_id",
            "active",
            "company_id",
        ),
        {
            "search": Filter("name", "ilike", "str", "Name contains"),
            "default_code": Filter("default_code", "ilike", "str", "Internal reference contains"),
        },
        order="name",
    ),
    ListSpec(
        "list_odoo_partner_bank_accounts",
        "res.partner.bank",
        "Bank details: partner bank accounts (numbers masked to their last 4 characters).",
        ("acc_number", "partner_id", "bank_id", "acc_holder_name", "currency_id", "allow_out_payment", "company_id"),
        {"partner_ids": _ids("partner_id", "partner")},
        order="id desc",
        priority=2,
    ),
)


class _Session:
    """Opens a client per call, sharing the transport and user id it learned."""

    def __init__(self, token: str) -> None:
        self.credential = OdooCredential.parse(token)
        self.transport = ""
        self.uid: int | None = None

    @asynccontextmanager
    async def open(self) -> AsyncIterator[OdooClient]:
        cli = OdooClient(credential=self.credential, transport=self.transport, uid=self.uid)
        async with cli:
            try:
                yield cli
            finally:
                self.transport, self.uid = cli.transport, cli.uid


def _safe(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except OdooError as exc:
            return str(exc)

    return wrapper


def _domain_of(spec: ListSpec, args: dict[str, Any]) -> tuple[list[Any], str | None]:
    domain: list[Any] = [list(c) for c in spec.base_domain]
    for arg, flt in spec.filters.items():
        value = args.get(arg)
        if value is None or value == "" or value == []:
            continue
        if flt.kind == "date":
            day, err = to_date(value, arg)
            if err:
                return [], err
            domain.append([flt.field, flt.operator, day])
        elif flt.kind == "ids":
            ids = id_list(value)
            if ids:
                domain.append([flt.field, "in", ids])
        elif flt.kind == "bool":
            truth = value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
            if flt.operator == ">":
                if truth:
                    domain.append([flt.field, ">", 0])
            else:
                domain.append([flt.field, "=", truth])
        elif flt.kind == "text":
            domain.append([flt.field, flt.operator, f"{value}%"])
        else:
            domain.append([flt.field, flt.operator, str(value)])
    return domain, None


def _list_schema(spec: ListSpec) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for arg, flt in spec.filters.items():
        if flt.kind == "ids":
            props[arg] = {**_IDS, "description": flt.description}
        elif flt.kind == "bool":
            props[arg] = {"type": "boolean", "description": flt.description}
        else:
            props[arg] = {"type": "string", "description": flt.description}
    props.update({"company_ids": _COMPANIES, "limit": _LIMIT, "offset": _OFFSET})
    return props


def _definition(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    write: bool = False,
    priority: int = 1,
    read_only_call: Callable[[Any], bool] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=(SENSITIVE + description) if write else description,
        input_schema={"type": "object", "properties": properties, "required": list(required)},
        source=SOURCE,
        priority=2 if write and priority < 2 else priority,
        read_only=not write,
        always_ask=write,
        read_only_call=read_only_call,
    )


def build_operation_tools(
    token: str, executor: CallableToolExecutor, *, persist_file_fn: Any = None
) -> list[ToolDefinition]:
    session = _Session(token)
    tools: list[ToolDefinition] = []

    def add(defn: ToolDefinition, handler: Callable[..., Awaitable[Any]]) -> None:
        executor.register(defn.name, _safe(handler))
        tools.append(defn)

    # -- table reads -----------------------------------------------------------------

    def list_handler(spec: ListSpec) -> Callable[..., Awaitable[str]]:
        async def handler(**args: Any) -> str:
            domain, err = _domain_of(spec, args)
            if err:
                return err
            limit, offset = clamp(args.get("limit")), offset_of(args.get("offset"))
            companies = id_list(args.get("company_ids"))
            async with session.open() as cli:
                rows = await rep.safe_search_read(
                    cli,
                    spec.model,
                    domain,
                    list(spec.fields),
                    limit=limit,
                    offset=offset,
                    order=spec.order,
                    company_ids=companies,
                    action=f"list {spec.model} records",
                )
                total = await cli.count(spec.model, domain, company_ids=companies) if len(rows) == limit else None
            if not rows:
                return f"No {spec.model} record matches."
            note = page_note(len(rows), offset, limit, total if total is not None else offset + len(rows))
            return render(f"{len(rows)} {spec.model} records{note}:", rows)

        return handler

    for spec in LISTS:
        add(_definition(spec.name, spec.summary, _list_schema(spec), priority=spec.priority), list_handler(spec))

    # -- companies, entries, invoices ----------------------------------------------------

    async def list_odoo_companies() -> str:
        async with session.open() as cli:
            user = await cli.call("res.users", "context_get", action="read the user's context")
            rows = await rep.safe_search_read(
                cli,
                "res.company",
                [],
                ["name", "currency_id", "country_id", "vat", "parent_id", "fiscalyear_lock_date", "tax_lock_date"],
                order="id",
                action="list the companies",
            )
        current = user.get("allowed_company_ids") if isinstance(user, dict) else None
        return render(
            f"{len(rows)} companies this user can see (pass their ids as company_ids):",
            {"companies": rows, "default_allowed_company_ids": current or list(session.credential.company_ids)},
        )

    add(
        _definition(
            "list_odoo_companies",
            "The companies the Odoo user can see, with currency, country and lock dates: the ids every tool takes "
            "as company_ids.",
            {},
            priority=0,
        ),
        list_odoo_companies,
    )

    async def get_odoo_journal_entry(move_id: int, company_ids: Any = None) -> str:
        companies = id_list(company_ids)
        async with session.open() as cli:
            moves = await rep.safe_search_read(
                cli,
                "account.move",
                [["id", "=", int(move_id)]],
                [*_MOVE_FIELDS, "narration", "invoice_origin", "payment_reference", "partner_bank_id"],
                company_ids=companies,
                action=f"read journal entry #{move_id}",
            )
            if not moves:
                return f"Error: Odoo found no journal entry #{move_id} for these companies."
            lines = await rep.safe_search_read(
                cli,
                "account.move.line",
                [["move_id", "=", int(move_id)]],
                list(_LINE_FIELDS),
                order="sequence, id",
                company_ids=companies,
                action=f"read the lines of #{move_id}",
            )
        return render(f"Journal entry #{move_id} with {len(lines)} lines:", {"entry": moves[0], "lines": lines})

    add(
        _definition(
            "get_odoo_journal_entry",
            "One journal entry, invoice or bill with its lines (accounts, debit, credit, partner, matching).",
            {"move_id": {"type": "integer", "description": "account.move id"}, "company_ids": _COMPANIES},
            ("move_id",),
            priority=0,
        ),
        get_odoo_journal_entry,
    )

    async def search_odoo_invoices(
        kind: str = "",
        date_from: str = "",
        date_to: str = "",
        partner_ids: Any = None,
        state: str = "",
        payment_state: str = "",
        number: str = "",
        company_ids: Any = None,
        limit: int = 0,
        offset: int = 0,
    ) -> str:
        types = [MOVE_TYPES[kind]] if kind in MOVE_TYPES else list(MOVE_TYPES.values())
        domain: list[Any] = [["move_type", "in", types]]
        for value, cond in (
            (date_from, ("invoice_date", ">=")),
            (date_to, ("invoice_date", "<=")),
        ):
            if value:
                day, err = to_date(value, cond[0])
                if err:
                    return err
                domain.append([cond[0], cond[1], day])
        if id_list(partner_ids):
            domain.append(["partner_id", "in", id_list(partner_ids)])
        if state:
            domain.append(["state", "=", state])
        if payment_state:
            domain.append(["payment_state", "=", payment_state])
        if number:
            domain.append(["name", "ilike", number])
        size, skip, companies = clamp(limit), offset_of(offset), id_list(company_ids)
        async with session.open() as cli:
            rows = await rep.safe_search_read(
                cli,
                "account.move",
                domain,
                [*_MOVE_FIELDS, "invoice_origin", "payment_reference"],
                limit=size,
                offset=skip,
                order="invoice_date desc, id desc",
                company_ids=companies,
                action="search invoices and bills",
            )
            total = await cli.count("account.move", domain, company_ids=companies) if len(rows) == size else None
        if not rows:
            return "No invoice or bill matches."
        note = page_note(len(rows), skip, size, total if total is not None else skip + len(rows))
        return render(f"{len(rows)} invoices and bills{note}:", rows)

    add(
        _definition(
            "search_odoo_invoices",
            "Customer invoices, vendor bills and credit notes by kind, invoice date, partner, state, payment state "
            "(not_paid, in_payment, paid, partial, reversed) or number.",
            {
                "kind": {"type": "string", "enum": list(MOVE_TYPES), "description": "Omit for every kind"},
                "date_from": _DATE_FROM,
                "date_to": _DATE_TO,
                "partner_ids": {**_IDS, "description": "Only these partner ids"},
                "state": {"type": "string", "description": "draft, posted or cancel"},
                "payment_state": {"type": "string"},
                "number": {"type": "string", "description": "Number contains"},
                "company_ids": _COMPANIES,
                "limit": _LIMIT,
                "offset": _OFFSET,
            },
            priority=0,
        ),
        search_odoo_invoices,
    )

    async def download_odoo_attachments(move_id: int, company_ids: Any = None) -> Any:
        async with session.open() as cli:
            files = await rep.safe_search_read(
                cli,
                "ir.attachment",
                [["res_model", "=", "account.move"], ["res_id", "=", int(move_id)]],
                ["name", "mimetype", "datas", "file_size"],
                order="id",
                company_ids=id_list(company_ids),
                action=f"read the attachments of #{move_id}",
            )
        files = [f for f in files if f.get("datas")]
        if not files:
            return f"Journal entry #{move_id} has no attachment."
        first = files[0]
        result: ToolResult = await deliver_file(
            data=base64.b64decode(first["datas"]),
            filename=str(first.get("name") or f"odoo-{move_id}"),
            content_type=str(first.get("mimetype") or "application/octet-stream"),
            source="Odoo",
            persist_file_fn=persist_file_fn,
            details=[f"Attachment 1 of {len(files)} of journal entry #{move_id}"],
        )
        for index, extra in enumerate(files[1:], start=2):
            more = await deliver_file(
                data=base64.b64decode(extra["datas"]),
                filename=str(extra.get("name") or f"odoo-{move_id}-{index}"),
                content_type=str(extra.get("mimetype") or "application/octet-stream"),
                source="Odoo",
                persist_file_fn=persist_file_fn,
                details=[f"Attachment {index} of {len(files)} of journal entry #{move_id}"],
            )
            result.content = f"{result.content}\n\n{more.content}"
            result.files = [*(result.files or []), *(more.files or [])]
        return result

    add(
        _definition(
            "download_odoo_attachments",
            "Download the attachments (invoice PDF, scanned bill) of an invoice, bill or entry into the conversation.",
            {"move_id": {"type": "integer", "description": "account.move id"}, "company_ids": _COMPANIES},
            ("move_id",),
        ),
        download_odoo_attachments,
    )

    async def list_odoo_journal_bank_accounts(company_ids: Any = None) -> str:
        async with session.open() as cli:
            rows = await rep.safe_search_read(
                cli,
                "account.journal",
                [["type", "=", "bank"]],
                ["name", "code", "bank_account_id", "bank_id", "currency_id", "company_id"],
                order="sequence, code",
                company_ids=id_list(company_ids),
                action="list the bank journals",
            )
        return render(f"{len(rows)} bank journals (account numbers masked):", rows)

    add(
        _definition(
            "list_odoo_journal_bank_accounts",
            "Bank details: the bank account of each bank journal (numbers masked to their last 4 characters).",
            {"company_ids": _COMPANIES},
            priority=2,
        ),
        list_odoo_journal_bank_accounts,
    )

    async def get_odoo_lock_dates(company_ids: Any = None) -> str:
        companies = id_list(company_ids)
        fields = [
            "name",
            "fiscalyear_last_day",
            "fiscalyear_last_month",
            "period_lock_date",
            "fiscalyear_lock_date",
            "tax_lock_date",
            "sale_lock_date",
            "purchase_lock_date",
            "hard_lock_date",
        ]
        async with session.open() as cli:
            domain = [["id", "in", companies]] if companies else []
            rows = await rep.safe_search_read(
                cli, "res.company", domain, fields, company_ids=companies, action="read the lock dates"
            )
            try:
                years = await rep.safe_search_read(
                    cli,
                    "account.fiscal.year",
                    [],
                    ["name", "date_from", "date_to", "company_id"],
                    order="date_from desc",
                    company_ids=companies,
                    action="list the fiscal years",
                )
            except OdooError:
                years = []
        return render("Fiscal year end, lock dates and fiscal years:", {"companies": rows, "fiscal_years": years})

    add(
        _definition(
            "get_odoo_lock_dates",
            "Fiscal year end, lock dates (period, fiscal year, tax, sale, purchase, hard) and the fiscal years.",
            {"company_ids": _COMPANIES},
        ),
        get_odoo_lock_dates,
    )

    # -- reports ---------------------------------------------------------------------

    async def get_odoo_trial_balance(date_to: str, date_from: str = "", company_ids: Any = None) -> str:
        end, err = to_date(date_to, "date_to")
        start, err2 = to_date(date_from, "date_from")
        if err or err2 or not end:
            return err or err2 or "Error: date_to is required (YYYY-MM-DD)."
        async with session.open() as cli:
            rows = await rep.trial_balance(cli, start, end, id_list(company_ids))
        debit = round(sum(r["debit"] for r in rows), 2)
        credit = round(sum(r["credit"] for r in rows), 2)
        return render(
            f"Trial balance {start or 'start'} to {end}, posted entries, {len(rows)} accounts, "
            f"period debit {debit:,.2f}, credit {credit:,.2f}:",
            rows,
        )

    add(
        _definition(
            "get_odoo_trial_balance",
            "Trial balance from posted journal items: per account the opening balance at date_from, period debit "
            "and credit and the closing balance at date_to (company currency).",
            {"date_from": _DATE_FROM, "date_to": _DATE_TO, "company_ids": _COMPANIES},
            ("date_to",),
            priority=0,
        ),
        get_odoo_trial_balance,
    )

    async def get_odoo_general_ledger(
        date_from: str,
        date_to: str,
        account_ids: Any = None,
        journal_ids: Any = None,
        partner_ids: Any = None,
        company_ids: Any = None,
        limit: int = 0,
        offset: int = 0,
    ) -> str:
        start, err = to_date(date_from, "date_from")
        end, err2 = to_date(date_to, "date_to")
        if err or err2 or not start or not end:
            return err or err2 or "Error: date_from and date_to are required (YYYY-MM-DD)."
        domain = rep.posted([["date", ">=", start], ["date", "<=", end]])
        for value, name in ((account_ids, "account_id"), (journal_ids, "journal_id"), (partner_ids, "partner_id")):
            if id_list(value):
                domain.append([name, "in", id_list(value)])
        size, skip, companies = clamp(limit, maximum=500), offset_of(offset), id_list(company_ids)
        async with session.open() as cli:
            lines = await rep.safe_search_read(
                cli,
                "account.move.line",
                domain,
                rep.GL_FIELDS,
                limit=size,
                offset=skip,
                order="account_id, date, id",
                company_ids=companies,
                action="read the general ledger",
            )
            total = await cli.count("account.move.line", domain, company_ids=companies)
        note = page_note(len(lines), skip, size, total)
        return render(
            f"General ledger {start} to {end}, posted, {len(lines)} lines{note} (export_odoo_report for the whole "
            "ledger as a file):",
            [rep.gl_row(line) for line in lines],
        )

    add(
        _definition(
            "get_odoo_general_ledger",
            "General ledger lines of posted entries for a period, by account, journal or partner, paged.",
            {
                "date_from": _DATE_FROM,
                "date_to": _DATE_TO,
                "account_ids": {**_IDS, "description": "Only these account ids"},
                "journal_ids": {**_IDS, "description": "Only these journal ids"},
                "partner_ids": {**_IDS, "description": "Only these partner ids"},
                "company_ids": _COMPANIES,
                "limit": _LIMIT,
                "offset": _OFFSET,
            },
            ("date_from", "date_to"),
            priority=0,
        ),
        get_odoo_general_ledger,
    )

    async def get_odoo_aged_balance(
        kind: str, as_of: str = "", partner_ids: Any = None, company_ids: Any = None
    ) -> str:
        if kind not in ("receivable", "payable"):
            return "Error: kind must be receivable or payable."
        day, err = to_date(as_of, "as_of")
        if err:
            return err
        day = day or date.today().isoformat()
        async with session.open() as cli:
            lines, cut = await rep.aged_lines(cli, kind, day, id_list(company_ids), id_list(partner_ids))
        rows = rep.aged_balance(lines, kind, day)
        total = round(sum(r["total"] for r in rows), 2)
        note = " (cut: more open items than one answer reads; narrow by partner)" if cut else ""
        return render(f"Aged {kind} at {day}: {len(rows)} partners, open {total:,.2f}{note}:", rows)

    add(
        _definition(
            "get_odoo_aged_balance",
            "Aged receivable or payable at a date: open amounts per partner in buckets of days past due (not due, "
            "1-30, 31-60, 61-90, 91-120, older), company currency.",
            {
                "kind": {"type": "string", "enum": ["receivable", "payable"]},
                "as_of": {"type": "string", "description": "Date (YYYY-MM-DD), default today"},
                "partner_ids": {**_IDS, "description": "Only these partner ids"},
                "company_ids": _COMPANIES,
            },
            ("kind",),
            priority=0,
        ),
        get_odoo_aged_balance,
    )

    async def export_odoo_report(
        report: str,
        date_to: str = "",
        date_from: str = "",
        file_format: str = "xlsx",
        company_ids: Any = None,
    ) -> Any:
        end, err = to_date(date_to, "date_to")
        start, err2 = to_date(date_from, "date_from")
        if err or err2:
            return err or err2
        end = end or date.today().isoformat()
        companies = id_list(company_ids)
        fmt = "csv" if file_format == "csv" else "xlsx"
        async with session.open() as cli:
            if report == "trial_balance":
                rows = await rep.trial_balance(cli, start, end, companies)
                headers = list(rows[0]) if rows else ["code", "name"]
                table = [[r[h] for h in headers] for r in rows]
            elif report in ("aged_receivable", "aged_payable"):
                kind = report.removeprefix("aged_")
                lines, _ = await rep.aged_lines(cli, kind, end, companies, [])
                rows = rep.aged_balance(lines, kind, end)
                headers = list(rows[0]) if rows else ["partner"]
                table = [[r[h] for h in headers] for r in rows]
            elif report in ("general_ledger", "fec"):
                if not start:
                    return "Error: date_from is required for a general ledger or FEC export."
                domain = rep.posted([["date", ">=", start], ["date", "<=", end]])
                lines, cut = await rep.all_lines(cli, domain, rep.GL_FIELDS, companies)
                if cut:
                    return f"Error: more than {rep.MAX_LINES} journal items in the period; export a shorter period."
                if report == "general_ledger":
                    rows = [rep.gl_row(line) for line in lines]
                    headers = list(rows[0]) if rows else ["date"]
                    table = [[r[h] for h in headers] for r in rows]
                else:
                    jids = {j for line in lines if (j := m2o(line.get("journal_id"))[0]) is not None}
                    aids = {a for line in lines if (a := m2o(line.get("account_id"))[0]) is not None}
                    journals = await rep.safe_search_read(
                        cli, "account.journal", [["id", "in", sorted(jids)]], ["code", "name"], company_ids=companies
                    )
                    accounts = await rep.account_codes(cli, aids, companies)
                    headers = rep.FEC_HEADERS
                    table = rep.fec_rows(lines, {int(j["id"]): j for j in journals}, accounts)
                    fmt = "csv" if file_format != "xlsx" else fmt
            else:
                return "Error: report must be trial_balance, general_ledger, aged_receivable, aged_payable or fec."
        suffix = "-".join(str(c) for c in companies) or "default"
        name = f"odoo-{report}-{start + '_' if start else ''}{end}-companies-{suffix}.{fmt}"
        if fmt == "csv":
            data = rep.to_csv(headers, table, delimiter="|" if report == "fec" else ",")
            content_type = "text/csv"
        else:
            data = rep.to_xlsx(headers, table, title=report)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return await deliver_file(
            data=data,
            filename=name,
            content_type=content_type,
            source="Odoo",
            persist_file_fn=persist_file_fn,
            details=[f"{len(table)} rows, posted entries"],
        )

    add(
        _definition(
            "export_odoo_report",
            "Export a trial balance, general ledger, aged receivable, aged payable or FEC-style ledger (posted "
            "entries) as a CSV or XLSX file of the conversation.",
            {
                "report": {
                    "type": "string",
                    "enum": ["trial_balance", "general_ledger", "aged_receivable", "aged_payable", "fec"],
                },
                "date_from": _DATE_FROM,
                "date_to": {"type": "string", "description": "Up to this date (YYYY-MM-DD), default today"},
                "file_format": {"type": "string", "enum": ["xlsx", "csv"], "description": "Default xlsx (FEC: csv)"},
                "company_ids": _COMPANIES,
            },
            ("report",),
        ),
        export_odoo_report,
    )

    # -- generic reads -----------------------------------------------------------------

    async def search_read_odoo_records(
        model: str,
        domain: Any = None,
        fields: Any = None,
        order: str = "",
        company_ids: Any = None,
        limit: int = 0,
        offset: int = 0,
    ) -> str:
        parsed, err = parse_domain(domain)
        if err:
            return err
        size, skip, companies = clamp(limit), offset_of(offset), id_list(company_ids)
        async with session.open() as cli:
            rows = await cli.search_read(
                model,
                parsed,
                str_list(fields),
                limit=size,
                offset=skip,
                order=order or None,
                company_ids=companies,
                action=f"search {model}",
            )
            total = await cli.count(model, parsed, company_ids=companies) if len(rows) == size else None
        note = page_note(len(rows), skip, size, total if total is not None else skip + len(rows))
        return render(f"{len(rows)} {model} records{note}:", rows)

    add(
        _definition(
            "search_read_odoo_records",
            "Generic read of any Odoo model (search_read): an Odoo domain as JSON, the fields to read (name them: "
            "get_odoo_model_fields lists them), order, paging.",
            {
                "model": {"type": "string", "description": "Technical model name, e.g. account.move"},
                "domain": {"type": "string", "description": 'JSON domain, e.g. [["state", "=", "posted"]]'},
                "fields": {"type": "array", "items": {"type": "string"}},
                "order": {"type": "string", "description": "e.g. date desc, id desc"},
                "company_ids": _COMPANIES,
                "limit": _LIMIT,
                "offset": _OFFSET,
            },
            ("model",),
        ),
        search_read_odoo_records,
    )

    async def read_odoo_records(model: str, ids: Any, fields: Any = None, company_ids: Any = None) -> str:
        wanted = id_list(ids)
        if not wanted:
            return "Error: give the record ids to read."
        async with session.open() as cli:
            rows = await cli.call(
                model,
                "read",
                ids=wanted,
                company_ids=id_list(company_ids),
                action=f"read {model} records",
                fields=str_list(fields) or None,
            )
        return render(f"{len(rows or [])} {model} records:", rows or [])

    add(
        _definition(
            "read_odoo_records",
            "Generic read of records of any Odoo model by id.",
            {
                "model": {"type": "string"},
                "ids": {**_IDS, "description": "Record ids"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "company_ids": _COMPANIES,
            },
            ("model", "ids"),
        ),
        read_odoo_records,
    )

    async def get_odoo_model_fields(model: str, company_ids: Any = None) -> str:
        async with session.open() as cli:
            data = await cli.call(
                model,
                "fields_get",
                company_ids=id_list(company_ids),
                action=f"read the fields of {model}",
                attributes=["string", "type", "relation", "required", "readonly", "selection"],
            )
        return render(f"Fields of {model}:", data)

    add(
        _definition(
            "get_odoo_model_fields",
            "The fields of an Odoo model (fields_get): label, type, relation, required, read-only, selection values.",
            {"model": {"type": "string"}, "company_ids": _COMPANIES},
            ("model",),
        ),
        get_odoo_model_fields,
    )

    async def count_odoo_records(model: str, domain: Any = None, company_ids: Any = None) -> str:
        parsed, err = parse_domain(domain)
        if err:
            return err
        async with session.open() as cli:
            total = await cli.count(model, parsed, company_ids=id_list(company_ids))
        return f"{total} {model} records match."

    add(
        _definition(
            "count_odoo_records",
            "Count the records of any Odoo model matching a domain (search_count).",
            {"model": {"type": "string"}, "domain": {"type": "string"}, "company_ids": _COMPANIES},
            ("model",),
        ),
        count_odoo_records,
    )

    # -- writes (always ask) -------------------------------------------------------------

    async def _create(cli: OdooClient, model: str, values: dict[str, Any], companies: list[int], what: str) -> int:
        created = await cli.call(model, "create", company_ids=companies, action=f"create {what}", vals_list=[values])
        if isinstance(created, list):
            created = created[0] if created else None
        if not created:
            raise OdooError(f"Error: Odoo created no {what}.")
        return int(created)

    def writer(model: str, what: str) -> tuple[Callable[..., Awaitable[str]], Callable[..., Awaitable[str]]]:
        async def create(values: Any, company_ids: Any = None) -> str:
            vals, err = parse_values(values)
            if err:
                return err
            async with session.open() as cli:
                ident = await _create(cli, model, vals, id_list(company_ids), what)
            return f"Created {what} #{ident} ({model})."

        async def update(record_id: int, values: Any, company_ids: Any = None) -> str:
            vals, err = parse_values(values)
            if err:
                return err
            async with session.open() as cli:
                await cli.call(
                    model,
                    "write",
                    ids=[int(record_id)],
                    company_ids=id_list(company_ids),
                    action=f"update {what} #{record_id}",
                    vals=vals,
                )
            return f"Updated {what} #{record_id} ({', '.join(vals)})."

        return create, update

    values_schema = {"type": "string", "description": "JSON object of Odoo field values"}
    record_id_schema = {"type": "integer", "description": "Record id"}

    create_entry, update_entry = writer("account.move", "journal entry")
    add(
        _definition(
            "create_odoo_journal_entry",
            "Create a DRAFT journal entry (account.move, move_type entry): values with journal_id, date, ref and "
            'line_ids as Odoo commands, e.g. [[0, 0, {"account_id": 1, "debit": 100}], [0, 0, {"account_id": 2, '
            '"credit": 100}]]. Post it with post_odoo_journal_entry.',
            {"values": values_schema, "company_ids": _COMPANIES},
            ("values",),
            write=True,
        ),
        create_entry,
    )
    add(
        _definition(
            "update_odoo_journal_entry",
            "Update a draft journal entry, invoice or bill (account.move write).",
            {"record_id": record_id_schema, "values": values_schema, "company_ids": _COMPANIES},
            ("record_id", "values"),
            write=True,
        ),
        update_entry,
    )

    def move_action(method: str, done: str) -> Callable[..., Awaitable[str]]:
        async def handler(move_ids: Any, company_ids: Any = None) -> str:
            ids = id_list(move_ids)
            if not ids:
                return "Error: give the journal entry ids."
            async with session.open() as cli:
                await cli.call(
                    "account.move", method, ids=ids, company_ids=id_list(company_ids), action=f"{done} {ids}"
                )
            return f"{done.capitalize()} journal entries {ids}."

        return handler

    moves_schema = {"move_ids": {**_IDS, "description": "account.move ids"}, "company_ids": _COMPANIES}
    for name, method, done, text in (
        ("post_odoo_journal_entry", "action_post", "posted", "Post (validate) draft entries, invoices or bills."),
        (
            "reset_odoo_journal_entry_to_draft",
            "button_draft",
            "reset to draft",
            "Reset posted entries, invoices or bills to draft (refused by Odoo inside a lock date).",
        ),
        (
            "cancel_odoo_journal_entry",
            "button_cancel",
            "cancelled",
            "Cancel draft entries, invoices or bills.",
        ),
    ):
        add(_definition(name, text, moves_schema, ("move_ids",), write=True), move_action(method, done))

    async def create_odoo_invoice(
        kind: str,
        partner_id: int,
        lines: Any,
        invoice_date: str = "",
        ref: str = "",
        company_ids: Any = None,
        journal_id: int = 0,
        currency_id: int = 0,
        invoice_date_due: str = "",
    ) -> str:
        if kind not in MOVE_TYPES:
            return f"Error: kind must be one of {', '.join(MOVE_TYPES)}."
        raw = lines
        if isinstance(lines, str):
            raw, err = parse_values('{"l": ' + lines + "}")
            if err:
                return "Error: lines must be a JSON list of objects."
            raw = raw["l"]
        if not isinstance(raw, list) or not raw:
            return "Error: give at least one line (product_id or name, quantity, price_unit, account_id, tax_ids)."
        vals: dict[str, Any] = {
            "move_type": MOVE_TYPES[kind],
            "partner_id": int(partner_id),
            "invoice_line_ids": [[0, 0, dict(line)] for line in raw if isinstance(line, dict)],
        }
        for key, value in (("invoice_date", invoice_date), ("invoice_date_due", invoice_date_due)):
            if value:
                day, err = to_date(value, key)
                if err:
                    return err
                vals[key] = day
        if ref:
            vals["ref"] = ref
        for key, ref_id in (("journal_id", journal_id), ("currency_id", currency_id)):
            if ref_id:
                vals[key] = int(ref_id)
        async with session.open() as cli:
            ident = await _create(cli, "account.move", vals, id_list(company_ids), kind.replace("_", " "))
        return f"Created draft {kind.replace('_', ' ')} #{ident} with {len(vals['invoice_line_ids'])} lines."

    add(
        _definition(
            "create_odoo_invoice",
            "Create a DRAFT customer invoice, vendor bill or credit note with its lines (each: product_id or name, "
            "quantity, price_unit, account_id, tax_ids as [[6, 0, [ids]]]). Never posted or sent here.",
            {
                "kind": {"type": "string", "enum": list(MOVE_TYPES)},
                "partner_id": {"type": "integer"},
                "lines": {"type": "array", "items": {"type": "object"}},
                "invoice_date": {"type": "string"},
                "invoice_date_due": {"type": "string"},
                "ref": {"type": "string", "description": "Vendor reference or customer reference"},
                "journal_id": {"type": "integer"},
                "currency_id": {"type": "integer"},
                "company_ids": _COMPANIES,
            },
            ("kind", "partner_id", "lines"),
            write=True,
        ),
        create_odoo_invoice,
    )
    add(
        _definition(
            "update_odoo_invoice",
            "Update a draft invoice or bill (account.move write: ref, dates, partner, invoice_line_ids commands).",
            {"record_id": record_id_schema, "values": values_schema, "company_ids": _COMPANIES},
            ("record_id", "values"),
            write=True,
        ),
        update_entry,
    )

    async def register_odoo_payment(
        move_ids: Any,
        journal_id: int = 0,
        amount: float = 0,
        payment_date: str = "",
        memo: str = "",
        company_ids: Any = None,
    ) -> str:
        ids = id_list(move_ids)
        if not ids:
            return "Error: give the posted invoice or bill ids to pay."
        vals: dict[str, Any] = {}
        if journal_id:
            vals["journal_id"] = int(journal_id)
        if amount:
            vals["amount"] = float(amount)
        if payment_date:
            day, err = to_date(payment_date, "payment_date")
            if err:
                return err
            vals["payment_date"] = day
        if memo:
            vals["communication"] = memo
        context = {"active_model": "account.move", "active_ids": ids}
        companies = id_list(company_ids)
        async with session.open() as cli:
            wizard = await cli.call(
                "account.payment.register",
                "create",
                company_ids=companies,
                context=context,
                action="prepare the payment",
                vals_list=[vals],
            )
            wizard_id = wizard[0] if isinstance(wizard, list) else wizard
            await cli.call(
                "account.payment.register",
                "action_create_payments",
                ids=[int(wizard_id)],
                company_ids=companies,
                context=context,
                action="register the payment",
            )
        return f"Registered the payment of {ids} in Odoo's books (this records a payment; it sends no money)."

    add(
        _definition(
            "register_odoo_payment",
            "Register a payment on posted invoices or bills (account.payment.register): journal, amount (default the "
            "amount due), date, memo. Records the payment in the books; moves no money.",
            {
                "move_ids": {**_IDS, "description": "Posted invoice or bill ids"},
                "journal_id": {"type": "integer", "description": "Bank or cash journal"},
                "amount": {"type": "number"},
                "payment_date": {"type": "string"},
                "memo": {"type": "string"},
                "company_ids": _COMPANIES,
            },
            ("move_ids",),
            write=True,
        ),
        register_odoo_payment,
    )

    async def reconcile_odoo_journal_items(line_ids: Any, company_ids: Any = None) -> str:
        ids = id_list(line_ids)
        if len(ids) < 2:
            return "Error: give at least two journal item ids on the same reconcilable account."
        async with session.open() as cli:
            await cli.call(
                "account.move.line", "reconcile", ids=ids, company_ids=id_list(company_ids), action=f"reconcile {ids}"
            )
        return f"Reconciled journal items {ids}."

    add(
        _definition(
            "reconcile_odoo_journal_items",
            "Reconcile (match) journal items of one reconcilable account, e.g. an invoice line and its payment line.",
            {"line_ids": {**_IDS, "description": "account.move.line ids"}, "company_ids": _COMPANIES},
            ("line_ids",),
            write=True,
        ),
        reconcile_odoo_journal_items,
    )

    create_partner, update_partner = writer("res.partner", "partner")
    add(
        _definition(
            "create_odoo_partner",
            "Create a partner (customer or vendor): name, is_company, vat, email, country_id, address fields.",
            {"values": values_schema, "company_ids": _COMPANIES},
            ("values",),
            write=True,
        ),
        create_partner,
    )
    add(
        _definition(
            "update_odoo_partner",
            "Update a partner's fields (res.partner write).",
            {"record_id": record_id_schema, "values": values_schema, "company_ids": _COMPANIES},
            ("record_id", "values"),
            write=True,
        ),
        update_partner,
    )

    create_bank, update_bank = writer("res.partner.bank", "partner bank account")
    add(
        _definition(
            "create_odoo_partner_bank_account",
            "Bank details: create a partner bank account (acc_number, partner_id, bank_id, acc_holder_name).",
            {"values": values_schema, "company_ids": _COMPANIES},
            ("values",),
            write=True,
        ),
        create_bank,
    )
    add(
        _definition(
            "update_odoo_partner_bank_account",
            "Bank details: change a partner bank account (res.partner.bank write).",
            {"record_id": record_id_schema, "values": values_schema, "company_ids": _COMPANIES},
            ("record_id", "values"),
            write=True,
        ),
        update_bank,
    )

    lock_fields = (
        "period_lock_date",
        "fiscalyear_lock_date",
        "tax_lock_date",
        "sale_lock_date",
        "purchase_lock_date",
        "hard_lock_date",
    )

    async def set_odoo_lock_dates(company_id: int, lock_dates: Any) -> str:
        vals, err = parse_values(lock_dates)
        if err:
            return err
        unknown = [k for k in vals if k not in lock_fields]
        if unknown:
            return f"Error: {', '.join(unknown)} is not a lock date; use {', '.join(lock_fields)}."
        for key, value in list(vals.items()):
            if value in (None, False, ""):
                vals[key] = False
                continue
            day, derr = to_date(value, key)
            if derr:
                return derr
            vals[key] = day
        async with session.open() as cli:
            await cli.call(
                "res.company",
                "write",
                ids=[int(company_id)],
                company_ids=[int(company_id)],
                action=f"set the lock dates of company #{company_id}",
                vals=vals,
            )
        return f"Set the lock dates of company #{company_id}: {vals}."

    add(
        _definition(
            "set_odoo_lock_dates",
            "Set or clear the lock dates of a company (period, fiscal year, tax, and on Odoo 18+ sale, purchase, "
            "hard). Odoo refuses a hard lock date moved back.",
            {
                "company_id": {"type": "integer"},
                "lock_dates": {"type": "string", "description": 'JSON, e.g. {"fiscalyear_lock_date": "2026-12-31"}'},
            },
            ("company_id", "lock_dates"),
            write=True,
        ),
        set_odoo_lock_dates,
    )

    async def create_odoo_record(model: str, values: Any, company_ids: Any = None) -> str:
        vals, err = parse_values(values)
        if err:
            return err
        async with session.open() as cli:
            ident = await _create(cli, model, vals, id_list(company_ids), f"{model} record")
        return f"Created {model} #{ident}."

    async def update_odoo_records(model: str, ids: Any, values: Any, company_ids: Any = None) -> str:
        vals, err = parse_values(values)
        wanted = id_list(ids)
        if err or not wanted:
            return err or "Error: give the record ids."
        async with session.open() as cli:
            await cli.call(
                model, "write", ids=wanted, company_ids=id_list(company_ids), action=f"update {model}", vals=vals
            )
        return f"Updated {model} {wanted} ({', '.join(vals)})."

    async def delete_odoo_records(model: str, ids: Any, company_ids: Any = None) -> str:
        wanted = id_list(ids)
        if not wanted:
            return "Error: give the record ids."
        async with session.open() as cli:
            await cli.call(model, "unlink", ids=wanted, company_ids=id_list(company_ids), action=f"delete {model}")
        return f"Deleted {model} {wanted}."

    async def call_odoo_method(
        model: str, method: str, ids: Any = None, arguments: Any = None, company_ids: Any = None
    ) -> str:
        if method.startswith("_"):
            return "Error: Odoo does not expose private methods (starting with _)."
        kwargs: dict[str, Any] = {}
        if arguments not in (None, "", {}):
            kwargs, err = parse_values(arguments)
            if err:
                return "Error: arguments must be a JSON object of named arguments."
        wanted = id_list(ids)
        async with session.open() as cli:
            result = await cli.call(
                model,
                method,
                ids=wanted or None,
                company_ids=id_list(company_ids),
                action=f"call {model}.{method}",
                **kwargs,
            )
        return render(f"{model}.{method} answered:", result)

    model_schema = {"type": "string", "description": "Technical model name"}
    for defn, handler in (
        (
            _definition(
                "create_odoo_record",
                "Generic create on any Odoo model.",
                {"model": model_schema, "values": values_schema, "company_ids": _COMPANIES},
                ("model", "values"),
                write=True,
            ),
            create_odoo_record,
        ),
        (
            _definition(
                "update_odoo_records",
                "Generic write on records of any Odoo model.",
                {"model": model_schema, "ids": _IDS, "values": values_schema, "company_ids": _COMPANIES},
                ("model", "ids", "values"),
                write=True,
            ),
            update_odoo_records,
        ),
        (
            _definition(
                "delete_odoo_records",
                "Generic unlink (delete) of records of any Odoo model. Odoo refuses to delete posted entries.",
                {"model": model_schema, "ids": _IDS, "company_ids": _COMPANIES},
                ("model", "ids"),
                write=True,
            ),
            delete_odoo_records,
        ),
    ):
        add(defn, handler)

    call_defn = _definition(
        "call_odoo_method",
        "Call a public method of an Odoo model (e.g. action_reverse, action_register_payment) with record ids and "
        "named arguments. For plain reads prefer search_read_odoo_records.",
        {
            "model": model_schema,
            "method": {"type": "string"},
            "ids": _IDS,
            "arguments": {"type": "string", "description": "JSON object of named arguments"},
            "company_ids": _COMPANIES,
        },
        ("model", "method"),
        write=True,
        read_only_call=lambda arguments: str(arguments.get("method") or "") in READ_METHODS,
    )
    add(call_defn, call_odoo_method)
    return tools
