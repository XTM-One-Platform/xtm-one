"""Accounting: company, fiscal years, journals, chart of accounts, ledger
entries and lines, trial balance, exports (FEC, general ledger, analytical
general ledger) and ledger entry writes."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CURSOR,
    DATE_FROM,
    DATE_TO,
    LIMIT,
    PennylaneContext,
    as_dict,
    categories_label,
    clamp,
    definition,
    filters,
    id_list,
    id_schema,
    ids_schema,
    line,
    money,
    page_footer,
    ref_label,
    text,
    to_date,
)

#: Export kinds: path and the label shown.
EXPORTS = {
    "fec": ("exports/fecs", "FEC (fichier des écritures comptables)"),
    "general_ledger": ("exports/general_ledgers", "general ledger"),
    "analytical_general_ledger": ("exports/analytical_general_ledgers", "analytical general ledger"),
}


def _ledger_line(item: dict[str, Any], accounts: dict[int, str], journals: dict[int, str]) -> str:
    account = as_dict(item.get("ledger_account"))
    account_id = account.get("id")
    account_label = accounts.get(int(account_id)) if account_id is not None else None
    journal = as_dict(item.get("journal"))
    journal_id = journal.get("id")
    journal_label = journals.get(int(journal_id)) if journal_id is not None else None
    return "- " + line(
        [
            ("line", f"#{item.get('id')}"),
            ("date", item.get("date")),
            ("account", account_label or account.get("number") or ref_label(account)),
            ("journal", journal_label or ref_label(journal) if journal else None),
            (
                "debit",
                money(item.get("debit"), item.get("currency") or "EUR")
                if item.get("debit") not in (None, "0.0", "0", "0.00")
                else None,
            ),
            (
                "credit",
                money(item.get("credit"), item.get("currency") or "EUR")
                if item.get("credit") not in (None, "0.0", "0", "0.00")
                else None,
            ),
            ("label", text(item.get("label"), 120)),
            (
                "entry",
                f"#{(item.get('ledger_entry') or {}).get('id')}"
                if isinstance(item.get("ledger_entry"), dict)
                else None,
            ),
            ("lettered", item.get("lettering_reference") or item.get("letter")),
            ("analytics", categories_label(item.get("categories"))),
        ]
    )


def register(ctx: PennylaneContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def get_pennylane_company() -> str:
        async with ctx.client() as client:
            me, err = await ctx.get(client, "me", action="read the token's company")
            if err:
                return err
            features, _ = await ctx.get(client, "company/features")
        company = me.get("company") or {}
        user = me.get("user") or {}
        scopes = sorted(str(s) for s in me.get("scopes") or [])
        lines = [
            f"Company: {company.get('name')} (#{company.get('id')})",
            line([("registration", company.get("reg_no")), ("accounting", company.get("accounting_logic"))]),
            f"Token user: {user.get('first_name', '')} {user.get('last_name', '')} <{user.get('email', '')}>".strip(),
            f"Scopes ({len(scopes)}): {', '.join(scopes) or 'none listed'}",
        ]
        if isinstance(features, dict) and features:
            on = sorted(k for k, v in features.items() if v is True)
            if on:
                lines.append(f"Features on: {', '.join(on)}")
        return "\n".join(x for x in lines if x)

    async def list_pennylane_fiscal_years() -> str:
        async with ctx.client() as client:
            items, err, _ = await ctx.all_items(client, "fiscal_years", cap=200)
        if err:
            return err
        if not items:
            return "No fiscal year."
        return "Fiscal years:\n" + "\n".join(
            "- " + line([("from", i.get("start")), ("to", i.get("finish")), ("status", i.get("status"))]) for i in items
        )

    async def list_pennylane_journals(journal_type: str = "") -> str:
        async with ctx.client() as client:
            items, err, _ = await ctx.all_items(
                client, "journals", params={"filter": filters(("type", "eq", journal_type))}, cap=500
            )
        if err:
            return err
        if not items:
            return "No journal."
        return f"{len(items)} journals:\n" + "\n".join(
            "- "
            + line(
                [
                    ("id", f"#{i.get('id')}"),
                    ("code", i.get("code")),
                    ("label", text(i.get("label"), 80)),
                    ("type", i.get("type")),
                ]
            )
            for i in items
        )

    async def list_pennylane_ledger_accounts(
        number_prefix: str = "", enabled_only: bool = True, limit: int = 100, cursor: str = ""
    ) -> str:
        flt = filters(("number", "start_with", number_prefix), ("enabled", "eq", "true" if enabled_only else ""))
        async with ctx.client() as client:
            page, err = await ctx.page(
                client, "ledger_accounts", params={"filter": flt}, limit=clamp(limit, 100), cursor=cursor
            )
        if err:
            return err
        if not page.items:
            return "No ledger account matches."
        return (
            f"{len(page.items)} ledger accounts:\n"
            + "\n".join(
                "- "
                + line(
                    [
                        ("id", f"#{i.get('id')}"),
                        ("number", i.get("number")),
                        ("label", text(i.get("label"), 80)),
                        ("vat", i.get("vat_rate")),
                        ("enabled", None if i.get("enabled", True) else "no"),
                    ]
                )
                for i in page.items
            )
            + page_footer(page)
        )

    async def list_pennylane_ledger_entries(
        date_from: str = "", date_to: str = "", journal_ids: Any = None, limit: int = 25, cursor: str = ""
    ) -> str:
        start, err = to_date(date_from, "date_from")
        end, err2 = to_date(date_to, "date_to")
        if err or err2:
            return err or err2 or ""
        journals = id_list(journal_ids)
        flt = filters(("date", "gteq", start), ("date", "lteq", end), ("journal_id", "in", journals))
        async with ctx.client() as client:
            page, err = await ctx.page(
                client, "ledger_entries", params={"filter": flt, "sort": "-date"}, limit=clamp(limit), cursor=cursor
            )
            if err:
                return err
            journal_names = await ctx.names(client, "journals")
        if not page.items:
            return "No ledger entry matches."
        rows = []
        for i in page.items:
            journal = as_dict(i.get("journal"))
            jid = journal.get("id")
            rows.append(
                "- "
                + line(
                    [
                        ("entry", f"#{i.get('id')}"),
                        ("date", i.get("date")),
                        ("journal", journal_names.get(int(jid)) if jid is not None else None),
                        ("piece", i.get("piece_number")),
                        ("label", text(i.get("label"), 120)),
                        ("currency", i.get("currency")),
                    ]
                )
            )
        return f"{len(page.items)} ledger entries:\n" + "\n".join(rows) + page_footer(page)

    async def get_pennylane_ledger_entry(entry_id: int) -> str:
        async with ctx.client() as client:
            entry, err = await ctx.get(client, f"ledger_entries/{int(entry_id)}")
            if err:
                return err
            lines_page, err = await ctx.page(client, f"ledger_entries/{int(entry_id)}/ledger_entry_lines", limit=100)
            if err:
                return err
            accounts = await ctx.names(client, "ledger_accounts")
            journals = await ctx.names(client, "journals")
        head = line(
            [
                ("Entry", f"#{entry.get('id')}"),
                ("date", entry.get("date")),
                ("label", text(entry.get("label"), 160)),
                ("piece", entry.get("piece_number")),
                ("currency", entry.get("currency")),
            ]
        )
        body = "\n".join(_ledger_line(i, accounts, journals) for i in lines_page.items)
        return f"{head}\nLines:\n{body or '(none)'}"

    async def search_pennylane_ledger_entry_lines(
        date_from: str = "",
        date_to: str = "",
        journal_ids: Any = None,
        ledger_account_ids: Any = None,
        account_number_prefix: str = "",
        category_ids: Any = None,
        limit: int = 50,
        cursor: str = "",
    ) -> str:
        start, err = to_date(date_from, "date_from")
        end, err2 = to_date(date_to, "date_to")
        if err or err2:
            return err or err2 or ""
        accounts_filter = id_list(ledger_account_ids)
        async with ctx.client() as client:
            accounts = await ctx.names(client, "ledger_accounts")
            journals = await ctx.names(client, "journals")
            if account_number_prefix:
                prefix = account_number_prefix.strip()
                matched = [aid for aid, label in accounts.items() if label.split(" ", 1)[0].startswith(prefix)]
                if not matched:
                    return f"No ledger account number starts with {prefix}."
                accounts_filter = sorted(set(accounts_filter) | set(matched)) if accounts_filter else matched
            flt = filters(
                ("date", "gteq", start),
                ("date", "lteq", end),
                ("journal_id", "in", id_list(journal_ids)),
                ("ledger_account_id", "in", accounts_filter),
            )
            page, err = await ctx.page(
                client, "ledger_entry_lines", params={"filter": flt, "sort": "-id"}, limit=clamp(limit), cursor=cursor
            )
        if err:
            return err
        items = page.items
        wanted = set(id_list(category_ids))
        if wanted:
            items = [
                i
                for i in items
                if any(isinstance(c, dict) and c.get("id") in wanted for c in i.get("categories") or [])
            ]
        if not items:
            return "No ledger entry line matches." + page_footer(page)
        debit = sum(float(i.get("debit") or 0) for i in items)
        credit = sum(float(i.get("credit") or 0) for i in items)
        note = " (analytical filter applied to this page)" if wanted else ""
        return (
            f"{len(items)} ledger entry lines{note}; page totals debit {debit:,.2f}, credit {credit:,.2f}:\n"
            + "\n".join(_ledger_line(i, accounts, journals) for i in items)
            + page_footer(page)
        )

    async def get_pennylane_trial_balance(
        period_start: str, period_end: str, auxiliary: bool = False, account_number_prefix: str = ""
    ) -> str:
        start, err = to_date(period_start, "period_start")
        end, err2 = to_date(period_end, "period_end")
        if err or err2 or not start or not end:
            return err or err2 or "Error: period_start and period_end are required."
        params = {"period_start": start, "period_end": end, "is_auxiliary": "true" if auxiliary else ""}
        async with ctx.client() as client:
            items, err, capped = await ctx.all_items(client, "trial_balance", params=params, cap=5000)
        if err:
            return err
        prefix = account_number_prefix.strip()
        rows = [i for i in items or [] if not prefix or str(i.get("number", "")).startswith(prefix)]
        if not rows:
            return f"No balance between {start} and {end}."
        debits = sum(float(i.get("debits") or 0) for i in rows)
        credits = sum(float(i.get("credits") or 0) for i in rows)
        body = "\n".join(
            "- "
            + line(
                [
                    ("account", f"{i.get('number')} {text(i.get('label') or i.get('formatted_number'), 60)}".strip()),
                    ("debits", money(i.get("debits"))),
                    ("credits", money(i.get("credits"))),
                    ("balance", money(float(i.get("debits") or 0) - float(i.get("credits") or 0))),
                ]
            )
            for i in rows
        )
        more = "\n[More than 5,000 accounts: narrow with account_number_prefix]" if capped else ""
        return (
            f"Trial balance {start} to {end} ({len(rows)} accounts): debits {debits:,.2f} EUR, "
            f"credits {credits:,.2f} EUR, net {debits - credits:,.2f} EUR\n{body}{more}"
        )

    async def create_pennylane_export(kind: str, period_start: str, period_end: str, mode: str = "") -> str:
        if kind not in EXPORTS:
            return f"Error: kind must be one of {', '.join(EXPORTS)}."
        start, err = to_date(period_start, "period_start")
        end, err2 = to_date(period_end, "period_end")
        if err or err2 or not start or not end:
            return err or err2 or "Error: period_start and period_end are required."
        path, label = EXPORTS[kind]
        body: dict[str, Any] = {"period_start": start, "period_end": end}
        if kind == "analytical_general_ledger" and mode in ("in_line", "in_column"):
            body["mode"] = mode
        async with ctx.client() as client:
            data, err = await ctx.request(client, "post", path, body=body, action=f"start the {label} export")
        if err:
            return err
        return (
            f"Started the {label} export #{data.get('id')} for {start} to {end} (status {data.get('status')}). "
            f"Read it with get_pennylane_export(kind='{kind}', export_id={data.get('id')}) once ready."
        )

    async def get_pennylane_export(kind: str, export_id: int) -> str:
        if kind not in EXPORTS:
            return f"Error: kind must be one of {', '.join(EXPORTS)}."
        path, label = EXPORTS[kind]
        async with ctx.client() as client:
            data, err = await ctx.get(client, f"{path}/{int(export_id)}")
        if err:
            return err
        status = data.get("status")
        if status != "ready":
            return f"The {label} export #{export_id} is {status}; read it again in a minute."
        return f"The {label} export #{export_id} is ready.\nDownload URL (short-lived): {data.get('file_url')}"

    async def create_pennylane_ledger_entry(
        date: str,
        label: str,
        journal_id: int,
        lines: list[dict[str, Any]],
        piece_number: str = "",
        currency: str = "",
        file_attachment_id: int = 0,
    ) -> str:
        day, err = to_date(date, "date")
        if err or not day:
            return err or "Error: date is required."
        clean, problem = _entry_lines(lines)
        if problem:
            return problem
        body: dict[str, Any] = {"date": day, "label": label, "journal_id": int(journal_id), "ledger_entry_lines": clean}
        if piece_number:
            body["piece_number"] = piece_number
        if currency:
            body["currency"] = currency
        if file_attachment_id:
            body["file_attachment_id"] = int(file_attachment_id)
        async with ctx.client() as client:
            data, err = await ctx.request(client, "post", "ledger_entries", body=body, action="create the ledger entry")
        if err:
            return err
        return f"Created ledger entry #{data.get('id')} on {day} in journal #{journal_id} ({len(clean)} lines)."

    async def update_pennylane_ledger_entry(
        entry_id: int, date: str = "", label: str = "", piece_number: str = "", lines: Any = None
    ) -> str:
        body: dict[str, Any] = {}
        if date:
            day, err = to_date(date, "date")
            if err:
                return err
            body["date"] = day
        if label:
            body["label"] = label
        if piece_number:
            body["piece_number"] = piece_number
        if lines:
            clean, problem = _entry_lines(lines)
            if problem:
                return problem
            body["ledger_entry_lines"] = clean
        if not body:
            return "Error: give at least one field to change."
        async with ctx.client() as client:
            _, err = await ctx.request(
                client, "put", f"ledger_entries/{int(entry_id)}", body=body, action=f"update ledger entry #{entry_id}"
            )
        return err or f"Updated ledger entry #{entry_id} ({', '.join(body)})."

    for fn in (
        get_pennylane_company,
        list_pennylane_fiscal_years,
        list_pennylane_journals,
        list_pennylane_ledger_accounts,
        list_pennylane_ledger_entries,
        get_pennylane_ledger_entry,
        search_pennylane_ledger_entry_lines,
        get_pennylane_trial_balance,
        create_pennylane_export,
        get_pennylane_export,
        create_pennylane_ledger_entry,
        update_pennylane_ledger_entry,
    ):
        executor.register(fn.__name__, fn)

    period = {
        "period_start": {"type": "string", "description": "First day (YYYY-MM-DD)"},
        "period_end": {"type": "string", "description": "Last day included (YYYY-MM-DD)"},
    }
    entry_lines = {
        "type": "array",
        "description": "Lines, debits equal to credits: {ledger_account_id, debit, credit, label}; amounts as decimal strings",
        "items": {
            "type": "object",
            "properties": {
                "ledger_account_id": {"type": "integer"},
                "debit": {"type": "string"},
                "credit": {"type": "string"},
                "label": {"type": "string"},
            },
            "required": ["ledger_account_id"],
        },
    }
    export_kind = {
        "type": "string",
        "enum": list(EXPORTS),
        "description": "fec, general_ledger or analytical_general_ledger",
    }
    return [
        definition(
            "get_pennylane_company",
            "The company the Pennylane token reaches: name, registration number, accounting mode, the token's user and scopes, the features on.",
            {},
            priority=0,
            read_only=True,
        ),
        definition(
            "list_pennylane_fiscal_years",
            "Fiscal years of the company with their status (open, closed).",
            {},
            read_only=True,
        ),
        definition(
            "list_pennylane_journals",
            "Accounting journals (code, label, type: bank, sale, purchase, misc...).",
            {"journal_type": {"type": "string", "description": "Only this journal type"}},
            read_only=True,
        ),
        definition(
            "list_pennylane_ledger_accounts",
            "Chart of accounts: ledger account number, label and VAT rate. Filter by number prefix (6 = expenses, 401 = suppliers, 512 = bank).",
            {
                "number_prefix": {"type": "string", "description": "Only accounts whose number starts with this"},
                "enabled_only": {"type": "boolean", "default": True},
                "limit": {**LIMIT, "default": 100},
                "cursor": CURSOR,
            },
            read_only=True,
        ),
        definition(
            "list_pennylane_ledger_entries",
            "Ledger entries (journal entries) by date range and journal, newest first.",
            {
                "date_from": DATE_FROM,
                "date_to": DATE_TO,
                "journal_ids": ids_schema("journal"),
                "limit": LIMIT,
                "cursor": CURSOR,
            },
            priority=0,
            read_only=True,
        ),
        definition(
            "get_pennylane_ledger_entry",
            "One ledger entry with every line: account, debit, credit, label, lettering, analytical categories.",
            {"entry_id": id_schema("ledger entry")},
            ["entry_id"],
            read_only=True,
        ),
        definition(
            "search_pennylane_ledger_entry_lines",
            "Ledger entry lines (the general ledger) by date range, journal, account ids or account number prefix, and analytical category; page totals of debit and credit.",
            {
                "date_from": DATE_FROM,
                "date_to": DATE_TO,
                "journal_ids": ids_schema("journal"),
                "ledger_account_ids": ids_schema("ledger account"),
                "account_number_prefix": {
                    "type": "string",
                    "description": "Only accounts whose number starts with this (e.g. 6226)",
                },
                "category_ids": ids_schema("analytical category"),
                "limit": {**LIMIT, "default": 50},
                "cursor": CURSOR,
            },
            priority=0,
            read_only=True,
        ),
        definition(
            "get_pennylane_trial_balance",
            "Trial balance over a period: debits, credits and balance per account, with totals. auxiliary=true gives the customer and supplier sub-accounts.",
            {
                **period,
                "auxiliary": {"type": "boolean", "default": False},
                "account_number_prefix": {
                    "type": "string",
                    "description": "Only accounts whose number starts with this",
                },
            },
            ["period_start", "period_end"],
            priority=0,
            read_only=True,
        ),
        definition(
            "create_pennylane_export",
            "Start an accounting export over a period: FEC, general ledger, or analytical general ledger (mode in_line or in_column). Read it with get_pennylane_export.",
            {**period, "kind": export_kind, "mode": {"type": "string", "enum": ["in_line", "in_column"]}},
            ["kind", "period_start", "period_end"],
            priority=2,
        ),
        definition(
            "get_pennylane_export",
            "Status of an export started with create_pennylane_export and, once ready, its download URL.",
            {"kind": export_kind, "export_id": id_schema("export")},
            ["kind", "export_id"],
            priority=2,
            read_only=True,
        ),
        definition(
            "create_pennylane_ledger_entry",
            "Create a ledger entry (misc. journal entry) with balanced lines.",
            {
                "date": {"type": "string", "description": "Entry date (YYYY-MM-DD)"},
                "label": {"type": "string"},
                "journal_id": id_schema("journal"),
                "lines": entry_lines,
                "piece_number": {"type": "string"},
                "currency": {"type": "string", "description": "ISO currency, EUR by default"},
                "file_attachment_id": {"type": "integer", "description": "From upload_pennylane_file_attachment"},
            },
            ["date", "label", "journal_id", "lines"],
        ),
        definition(
            "update_pennylane_ledger_entry",
            "Update a ledger entry: date, label, piece number or its lines (replaced whole).",
            {
                "entry_id": id_schema("ledger entry"),
                "date": {"type": "string"},
                "label": {"type": "string"},
                "piece_number": {"type": "string"},
                "lines": entry_lines,
            },
            ["entry_id"],
            priority=2,
        ),
    ]


def _entry_lines(lines: Any) -> tuple[list[dict[str, Any]], str | None]:
    """The lines Pennylane takes, or why they are refused (unbalanced, no account)."""
    if not isinstance(lines, list) or len(lines) < 2:
        return [], "Error: a ledger entry needs at least two lines."
    clean: list[dict[str, Any]] = []
    debit = credit = 0.0
    for number, raw in enumerate(lines, 1):
        if not isinstance(raw, dict) or not str(raw.get("ledger_account_id") or "").isdigit():
            return [], f"Error: line {number} has no ledger_account_id."
        try:
            d = float(raw.get("debit") or 0)
            c = float(raw.get("credit") or 0)
        except (TypeError, ValueError):
            return [], f"Error: line {number} has a debit or credit that is not a number."
        debit += d
        credit += c
        item = {"ledger_account_id": int(raw["ledger_account_id"]), "debit": f"{d:.2f}", "credit": f"{c:.2f}"}
        if raw.get("label"):
            item["label"] = str(raw["label"])
        clean.append(item)
    if round(debit - credit, 2) != 0:
        return [], f"Error: the lines are not balanced (debits {debit:.2f}, credits {credit:.2f})."
    return clean, None
