# SPDX-License-Identifier: Apache-2.0

"""Accounting reports computed from the Odoo journal items.

Trial balance, general ledger, aged receivable and payable and an FEC-style
export are built from ``account.move.line`` itself (posted entries only), so
they answer the same on every Odoo version and edition: Odoo's own report
engine (``account.report``) differs between versions and the Enterprise
reports are not part of the external API contract. ``read_group`` sums by
account when the server offers it; otherwise the lines are paged and summed
here.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import date
from typing import Any

from ._client import OdooClient, OdooError
from ._render import m2o, money

#: Journal items read per page when a report pages, and the most it reads.
PAGE = 2000
MAX_LINES = 100_000

#: The aged-balance buckets, in days past due.
BUCKETS = (("not_due", None, 0), ("1_30", 1, 30), ("31_60", 31, 60), ("61_90", 61, 90), ("91_120", 91, 120))
OLDER = "older"

GL_FIELDS = [
    "date",
    "move_id",
    "move_name",
    "journal_id",
    "account_id",
    "partner_id",
    "name",
    "ref",
    "debit",
    "credit",
    "balance",
    "matching_number",
    "amount_currency",
    "currency_id",
    "company_id",
]


def posted(domain: Iterable[Any] = ()) -> list[Any]:
    return [["parent_state", "=", "posted"], *domain]


async def safe_search_read(
    cli: OdooClient,
    model: str,
    domain: list[Any],
    fields: list[str],
    *,
    limit: int = 0,
    offset: int = 0,
    order: str | None = None,
    company_ids: Iterable[int] = (),
    action: str = "",
) -> list[dict[str, Any]]:
    """``search_read``, retried once with only the fields this version knows."""
    companies = list(company_ids)
    try:
        return await cli.search_read(
            model, domain, fields, limit=limit, offset=offset, order=order, company_ids=companies, action=action
        )
    except OdooError as exc:
        if "field" not in str(exc).lower():
            raise
        known = await cli.call(
            model, "fields_get", company_ids=companies, action=f"read the fields of {model}", attributes=["type"]
        )
        kept = [f for f in fields if isinstance(known, dict) and f in known]
        if kept == fields:
            raise
        return await cli.search_read(
            model, domain, kept, limit=limit, offset=offset, order=order, company_ids=companies, action=action
        )


async def all_lines(
    cli: OdooClient, domain: list[Any], fields: list[str], company_ids: Iterable[int], *, order: str = "date, id"
) -> tuple[list[dict[str, Any]], bool]:
    """Every journal item of *domain* up to :data:`MAX_LINES`; ``(rows, cut)``."""
    rows: list[dict[str, Any]] = []
    offset = 0
    companies = list(company_ids)
    while len(rows) < MAX_LINES:
        page = await safe_search_read(
            cli,
            "account.move.line",
            domain,
            fields,
            limit=PAGE,
            offset=offset,
            order=order,
            company_ids=companies,
            action="read journal items",
        )
        rows.extend(page)
        if len(page) < PAGE:
            return rows, False
        offset += PAGE
    return rows[:MAX_LINES], True


async def sums_by_account(cli: OdooClient, domain: list[Any], company_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Debit, credit and balance per account for *domain*."""
    companies = list(company_ids)
    out: dict[int, dict[str, Any]] = {}
    try:
        groups = await cli.call(
            "account.move.line",
            "read_group",
            company_ids=companies,
            action="sum journal items by account",
            domain=domain,
            fields=["debit:sum", "credit:sum", "balance:sum"],
            groupby=["account_id"],
            lazy=False,
        )
        for group in groups or []:
            ident, name = m2o(group.get("account_id"))
            if ident is None:
                continue
            out[ident] = {
                "name": name,
                "debit": money(group.get("debit")),
                "credit": money(group.get("credit")),
                "balance": money(group.get("balance")),
            }
        return out
    except OdooError:
        pass
    rows, _ = await all_lines(cli, domain, ["account_id", "debit", "credit", "balance"], companies, order="id")
    for row in rows:
        ident, name = m2o(row.get("account_id"))
        if ident is None:
            continue
        acc = out.setdefault(ident, {"name": name, "debit": 0.0, "credit": 0.0, "balance": 0.0})
        for key in ("debit", "credit", "balance"):
            acc[key] = round(acc[key] + money(row.get(key)), 2)
    return out


async def account_codes(cli: OdooClient, ids: Iterable[int], company_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = await safe_search_read(
        cli,
        "account.account",
        [["id", "in", wanted]],
        ["code", "name", "account_type"],
        company_ids=company_ids,
        action="read the accounts",
    )
    return {int(r["id"]): r for r in rows}


async def trial_balance(
    cli: OdooClient, date_from: str | None, date_to: str, company_ids: Iterable[int]
) -> list[dict[str, Any]]:
    """One row per account: opening balance, period debit and credit, closing balance."""
    companies = list(company_ids)
    period: list[Any] = [["date", "<=", date_to]]
    if date_from:
        period.append(["date", ">=", date_from])
    moves = await sums_by_account(cli, posted(period), companies)
    opening = await sums_by_account(cli, posted([["date", "<", date_from]]), companies) if date_from else {}
    codes = await account_codes(cli, set(moves) | set(opening), companies)
    rows = []
    for ident in set(moves) | set(opening):
        acc = codes.get(ident, {})
        start = opening.get(ident, {}).get("balance", 0.0)
        mov = moves.get(ident, {"debit": 0.0, "credit": 0.0, "balance": 0.0})
        rows.append(
            {
                "account_id": ident,
                "code": acc.get("code") or "",
                "name": acc.get("name") or (moves.get(ident) or opening.get(ident) or {}).get("name", ""),
                "account_type": acc.get("account_type") or "",
                "opening_balance": round(start, 2),
                "debit": mov["debit"],
                "credit": mov["credit"],
                "closing_balance": round(start + mov["balance"], 2),
            }
        )
    rows.sort(key=lambda r: (str(r["code"]), r["account_id"]))
    return rows


async def aged_lines(
    cli: OdooClient, kind: str, as_of: str, company_ids: Iterable[int], partner_ids: list[int]
) -> tuple[list[dict[str, Any]], bool]:
    """The open receivable or payable journal items at *as_of*."""
    account_type = "asset_receivable" if kind == "receivable" else "liability_payable"
    base = posted([["date", "<=", as_of], ["reconciled", "=", False]])
    if partner_ids:
        base.append(["partner_id", "in", partner_ids])
    fields = ["partner_id", "date", "date_maturity", "amount_residual", "move_id", "move_name", "account_id"]
    try:
        return await all_lines(cli, [*base, ["account_id.account_type", "=", account_type]], fields, company_ids)
    except OdooError:
        # Odoo 15 and earlier: the type is account_id.internal_type.
        return await all_lines(cli, [*base, ["account_id.internal_type", "=", kind]], fields, company_ids)


def bucket_of(due: str | None, as_of: date) -> str:
    try:
        days = (as_of - date.fromisoformat(str(due)[:10])).days if due else 0
    except ValueError:
        days = 0
    for name, low, high in BUCKETS:
        if (low is None and days <= high) or (low is not None and low <= days <= high):
            return name
    return OLDER


def aged_balance(lines: list[dict[str, Any]], kind: str, as_of: str) -> list[dict[str, Any]]:
    """One row per partner with the open amount per bucket (payables shown positive)."""
    day = date.fromisoformat(as_of)
    sign = 1 if kind == "receivable" else -1
    names = [b[0] for b in BUCKETS] + [OLDER]
    partners: dict[Any, dict[str, Any]] = {}
    for line in lines:
        ident, name = m2o(line.get("partner_id"))
        row = partners.setdefault(
            ident, {"partner_id": ident, "partner": name or "(no partner)", **dict.fromkeys(names, 0.0), "total": 0.0}
        )
        amount = sign * money(line.get("amount_residual"))
        bucket = bucket_of(line.get("date_maturity") or line.get("date"), day)
        row[bucket] = round(row[bucket] + amount, 2)
        row["total"] = round(row["total"] + amount, 2)
    return sorted(partners.values(), key=lambda r: -abs(r["total"]))


def gl_row(line: dict[str, Any]) -> dict[str, Any]:
    _, account = m2o(line.get("account_id"))
    _, journal = m2o(line.get("journal_id"))
    _, partner = m2o(line.get("partner_id"))
    move_id, move = m2o(line.get("move_id"))
    _, currency = m2o(line.get("currency_id"))
    return {
        "date": line.get("date") or "",
        "entry": line.get("move_name") or move,
        "entry_id": move_id,
        "journal": journal,
        "account": account,
        "partner": partner,
        "label": line.get("name") or "",
        "ref": line.get("ref") or "",
        "debit": money(line.get("debit")),
        "credit": money(line.get("credit")),
        "balance": money(line.get("balance")),
        "matching": line.get("matching_number") or "",
        "amount_currency": money(line.get("amount_currency")),
        "currency": currency,
    }


FEC_HEADERS = [
    "JournalCode",
    "JournalLib",
    "EcritureNum",
    "EcritureDate",
    "CompteNum",
    "CompteLib",
    "CompAuxNum",
    "CompAuxLib",
    "PieceRef",
    "PieceDate",
    "EcritureLib",
    "Debit",
    "Credit",
    "EcritureLet",
    "DateLet",
    "ValidDate",
    "Montantdevise",
    "Idevise",
]


def fec_rows(
    lines: list[dict[str, Any]], journals: dict[int, dict[str, Any]], accounts: dict[int, dict[str, Any]]
) -> list[list[Any]]:
    """FEC-style rows (the French Fichier des Écritures Comptables columns)."""
    out = []
    for line in lines:
        jid, jname = m2o(line.get("journal_id"))
        aid, aname = m2o(line.get("account_id"))
        pid, pname = m2o(line.get("partner_id"))
        _, move = m2o(line.get("move_id"))
        _, currency = m2o(line.get("currency_id"))
        day = str(line.get("date") or "").replace("-", "")
        out.append(
            [
                (journals.get(jid or 0) or {}).get("code", ""),
                (journals.get(jid or 0) or {}).get("name", jname),
                line.get("move_name") or move,
                day,
                (accounts.get(aid or 0) or {}).get("code", ""),
                (accounts.get(aid or 0) or {}).get("name", aname),
                pid or "",
                pname,
                line.get("ref") or line.get("move_name") or move,
                day,
                line.get("name") or "",
                f"{money(line.get('debit')):.2f}".replace(".", ","),
                f"{money(line.get('credit')):.2f}".replace(".", ","),
                line.get("matching_number") or "",
                "",
                day,
                f"{money(line.get('amount_currency')):.2f}".replace(".", ","),
                currency,
            ]
        )
    return out


def to_csv(headers: list[str], rows: list[list[Any]], *, delimiter: str = ",") -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def to_xlsx(headers: list[str], rows: list[list[Any]], *, title: str = "Odoo") -> bytes:
    from openpyxl import Workbook  # type: ignore[import-untyped]

    book = Workbook()
    sheet = book.active
    sheet.title = title[:31]
    sheet.append(headers)
    for row in rows:
        sheet.append([v if isinstance(v, (int, float, str)) or v is None else str(v) for v in row])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
