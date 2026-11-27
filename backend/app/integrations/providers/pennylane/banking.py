"""Bank accounts and transactions (with their matching), analytical category
groups and categories, analytical categorisation writes, file attachments and
the changelogs read for incremental reads. No tool initiates a payment or
changes bank details; bank accounts form :data:`BANK_ACCOUNT_TOOLS`."""

from __future__ import annotations

from typing import Any

from app.integrations.file_delivery import read_pipe_file, uploaded_file_answer
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._common import (
    CURSOR,
    DATE_FROM,
    DATE_TO,
    LIMIT,
    PROVIDER,
    PennylaneContext,
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

#: Tools that show bank account data: one group a binding can withhold.
BANK_ACCOUNT_TOOLS = frozenset({"list_pennylane_bank_accounts"})

#: Changelog resources and their endpoint.
CHANGELOGS = {
    "customer_invoices": "changelogs/customer_invoices",
    "supplier_invoices": "changelogs/supplier_invoices",
    "customers": "changelogs/customers",
    "suppliers": "changelogs/suppliers",
    "products": "changelogs/products",
    "transactions": "changelogs/transactions",
    "quotes": "changelogs/quotes",
    "ledger_entries": "changelogs/ledger_entries",
    "ledger_entry_lines": "changelogs/ledger_entry_lines",
}

#: Objects that take an analytical categorisation.
CATEGORISABLE = {
    "transaction": "transactions/{id}/categories",
    "supplier_invoice": "supplier_invoices/{id}/categories",
    "customer_invoice": "customer_invoices/{id}/categories",
    "ledger_entry_line": "ledger_entry_lines/{id}/categories",
}

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _weights(categories: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``[{id, weight}]`` from ``[{id, weight}]`` or a list of ids (equal split)."""
    if not isinstance(categories, list) or not categories:
        return None, "Error: give categories as [{id, weight}] (weights 0-1 summing to 1 per group) or a list of ids."
    if all(isinstance(c, int | str) for c in categories):
        ids = id_list(categories)
        if not ids:
            return None, "Error: no category id given."
        share = round(1 / len(ids), 7)
        out = [{"id": i, "weight": str(share)} for i in ids]
        out[-1]["weight"] = str(round(1 - share * (len(ids) - 1), 7))
        return out, None
    out = []
    for c in categories:
        if not isinstance(c, dict) or c.get("id") in (None, ""):
            return None, "Error: each category is {id, weight}."
        try:
            weight = float(c.get("weight", 1))
        except (TypeError, ValueError):
            return None, f"Error: weight of category {c.get('id')} is not a number."
        if not 0 < weight <= 1:
            return None, f"Error: weight of category {c.get('id')} must be in (0, 1]."
        out.append({"id": int(c["id"]), "weight": str(round(weight, 7))})
    return out, None


def register(ctx: PennylaneContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def list_pennylane_bank_accounts() -> str:
        async with ctx.client() as client:
            items, err, _ = await ctx.all_items(client, "bank_accounts", cap=200)
        if err:
            return err
        if not items:
            return "No bank account."
        return f"{len(items)} bank accounts:\n" + "\n".join(
            "- "
            + line(
                [
                    ("#", a.get("id")),
                    ("name", text(a.get("name"), 80)),
                    ("bank", ref_label(a.get("bank_establishment")) if a.get("bank_establishment") else None),
                    ("balance", money(a.get("balance"), a.get("currency"))),
                    ("journal", ref_label(a.get("journal")) if a.get("journal") else None),
                ]
            )
            for a in items
        )

    async def list_pennylane_transactions(
        date_from: str = "",
        date_to: str = "",
        bank_account_ids: Any = None,
        unmatched_only: bool = False,
        min_amount: float | None = None,
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        start, err = to_date(date_from, "date_from")
        end, err2 = to_date(date_to, "date_to")
        if err or err2:
            return err or err2 or ""
        flt = filters(
            ("date", "gteq", start), ("date", "lteq", end), ("bank_account_id", "in", id_list(bank_account_ids))
        )
        async with ctx.client() as client:
            # GET /transactions sorts on id alone (a date sort answers 400); newest first.
            page, err = await ctx.page(
                client, "transactions", params={"filter": flt, "sort": "-id"}, limit=clamp(limit), cursor=cursor
            )
        if err:
            return err
        items = page.items
        if unmatched_only:
            items = [t for t in items if not t.get("matched_invoices")]
        if min_amount is not None:
            items = [t for t in items if abs(float(t.get("amount") or 0)) >= float(min_amount)]
        if not items:
            return "No transaction matches on this page." + page_footer(page)
        rows = []
        for t in items:
            matched = t.get("matched_invoices") or []
            rows.append(
                "- "
                + line(
                    [
                        ("#", t.get("id")),
                        ("date", t.get("date")),
                        ("amount", money(t.get("currency_amount") or t.get("amount"), t.get("currency"))),
                        ("label", text(t.get("label"), 100)),
                        ("account", ref_label(t.get("bank_account")) if t.get("bank_account") else None),
                        (
                            "counterpart",
                            ref_label(t.get("supplier") or t.get("customer"))
                            if (t.get("supplier") or t.get("customer"))
                            else None,
                        ),
                        (
                            "matched",
                            ", ".join(f"{m.get('type')} #{m.get('id')}" for m in matched if isinstance(m, dict))
                            or "no",
                        ),
                        ("analytics", categories_label(t.get("categories"))),
                    ]
                )
            )
        note = " (filtered on this page)" if unmatched_only or min_amount is not None else ""
        return f"{len(items)} bank transactions{note}:\n" + "\n".join(rows) + page_footer(page)

    async def list_pennylane_categories(category_group_id: int = 0, label_prefix: str = "") -> str:
        flt = filters(
            ("category_group_id", "eq", int(category_group_id) if category_group_id else ""),
            ("label", "start_with", label_prefix.strip()),
        )
        async with ctx.client() as client:
            groups, err, _ = await ctx.all_items(client, "category_groups", cap=200)
            if err:
                return err
            items, err, _ = await ctx.all_items(client, "categories", params={"filter": flt}, cap=2000)
        if err:
            return err
        names = {g.get("id"): g.get("label") for g in groups or []}
        if not items:
            return "No analytical category matches."
        by_group: dict[Any, list[str]] = {}
        for c in items:
            gid = (c.get("category_group") or {}).get("id") if isinstance(c.get("category_group"), dict) else None
            by_group.setdefault(gid, []).append(
                line(
                    [
                        ("#", c.get("id")),
                        ("label", text(c.get("label"), 80)),
                        ("code", c.get("analytical_code")),
                        ("direction", c.get("direction")),
                    ]
                )
            )
        out = []
        for gid, rows in by_group.items():
            out.append(f"{names.get(gid) or 'No group'} (group #{gid}):")
            out += [f"- {r}" for r in rows]
        return "\n".join(out)

    async def set_pennylane_categories(object_type: str, object_id: int, categories: Any) -> str:
        if object_type not in CATEGORISABLE:
            return f"Error: object_type must be one of {', '.join(CATEGORISABLE)}."
        body, err = _weights(categories)
        if err or body is None:
            return err or "Error: no category given."
        async with ctx.client() as client:
            _, err = await ctx.request(
                client,
                "put",
                CATEGORISABLE[object_type].format(id=int(object_id)),
                body=body,
                action=f"categorise {object_type} #{object_id}",
            )
        return err or f"Set the analytical categories of {object_type} #{object_id}: " + ", ".join(
            f"#{c['id']} {float(c['weight']) * 100:.0f}%" for c in body
        )

    async def upload_pennylane_file_attachment(
        file_id: str, file_name: str = "", file_context: FileContext | None = None
    ) -> str | ToolResult:
        piped = await read_pipe_file(file_context, file_id, max_bytes=MAX_ATTACHMENT_BYTES, target=PROVIDER)
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, content_type = piped
        filename = (file_name or "").strip() or piped_name
        async with ctx.client() as client:
            answer, err = await ctx.request(
                client,
                "post",
                "file_attachments",
                files={"file": (filename, data, content_type)},
                data={"filename": filename},
                action="upload the file attachment",
            )
        if err:
            return err
        return uploaded_file_answer(
            f"Uploaded file {file_id.strip()} to Pennylane as attachment #{answer.get('id')} "
            "(pass it as file_attachment_id to create_pennylane_ledger_entry).",
            target=PROVIDER,
            filename=filename,
        )

    async def list_pennylane_changes(resource: str, since: str = "", limit: int = 100, cursor: str = "") -> str:
        if resource not in CHANGELOGS:
            return f"Error: resource must be one of {', '.join(CHANGELOGS)}."
        params: dict[str, Any] = {}
        if since and not cursor:
            day, err = to_date(since, "since")
            if err:
                return err
            params["start_date"] = since if "T" in since else f"{day}T00:00:00Z"
        async with ctx.client() as client:
            page, err = await ctx.page(
                client, CHANGELOGS[resource], params=params, limit=clamp(limit, 100, 1000), cursor=cursor
            )
        if err:
            return err
        if not page.items:
            return f"No change on {resource} since {since or 'the start of the changelog (4 weeks)'}."
        rows = [
            "- "
            + line(
                [
                    ("#", c.get("id")),
                    ("operation", c.get("operation")),
                    ("at", c.get("processed_at") or c.get("updated_at")),
                ]
            )
            for c in page.items
        ]
        return f"{len(page.items)} changes on {resource}:\n" + "\n".join(rows) + page_footer(page)

    for fn in (
        list_pennylane_bank_accounts,
        list_pennylane_transactions,
        list_pennylane_categories,
        set_pennylane_categories,
        upload_pennylane_file_attachment,
        list_pennylane_changes,
    ):
        executor.register(fn.__name__, fn)

    return [
        definition(
            "list_pennylane_bank_accounts",
            "Bank accounts of the company with their bank, balance and journal (no account numbers).",
            {},
            read_only=True,
        ),
        definition(
            "list_pennylane_transactions",
            "Bank transactions by date and bank account, newest first, with the invoices each is matched to; unmatched_only and min_amount narrow the page.",
            {
                "date_from": DATE_FROM,
                "date_to": DATE_TO,
                "bank_account_ids": ids_schema("bank account"),
                "unmatched_only": {"type": "boolean", "default": False},
                "min_amount": {"type": "number", "description": "Absolute amount at least"},
                "limit": LIMIT,
                "cursor": CURSOR,
            },
            priority=0,
            read_only=True,
        ),
        definition(
            "list_pennylane_categories",
            "Analytical category groups and their categories (label, analytical code, direction).",
            {"category_group_id": id_schema("category group"), "label_prefix": {"type": "string"}},
            read_only=True,
        ),
        definition(
            "set_pennylane_categories",
            "Set the analytical categories of a transaction, supplier invoice, customer invoice or ledger entry line (replaces them): [{id, weight}] or a list of ids split equally.",
            {
                "object_type": {"type": "string", "enum": list(CATEGORISABLE)},
                "object_id": {"type": "integer"},
                "categories": {"type": "array", "items": {}, "description": "[{id, weight 0-1}] or [id, ...]"},
            },
            ["object_type", "object_id", "categories"],
        ),
        definition(
            "upload_pennylane_file_attachment",
            "Upload a file of this conversation to Pennylane as a file attachment (for a ledger entry).",
            {"file_id": {"type": "string"}, "file_name": {"type": "string"}},
            ["file_id"],
            priority=2,
        ),
        definition(
            "list_pennylane_changes",
            "What changed since a date on one resource (insert, update, delete with ids), from Pennylane's changelogs (4 weeks kept).",
            {
                "resource": {"type": "string", "enum": list(CHANGELOGS)},
                "since": {"type": "string", "description": "Date or date-time (UTC)"},
                "limit": {"type": "integer", "default": 100},
                "cursor": CURSOR,
            },
            ["resource"],
            priority=2,
            read_only=True,
        ),
    ]
