"""Customer and supplier invoices: lists with filters, one invoice with its
lines, payments and matched transactions, the PDF into the conversation, and
the writes (draft customer invoice, supplier invoice update, matching, paid
status, accounting validation)."""

from __future__ import annotations

from typing import Any

from app.integrations.file_delivery import deliver_file, fetch_file, file_content_type
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

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

#: Largest invoice PDF downloaded.
MAX_PDF_BYTES = 25 * 1024 * 1024

#: Supplier invoice payment statuses Pennylane accepts on a write.
SUPPLIER_PAYMENT_STATUSES = ("paid", "to_be_paid")

KINDS = {"customer": "customer_invoices", "supplier": "supplier_invoices"}


def _party(invoice: dict[str, Any], kind: str) -> str:
    return ref_label(invoice.get("customer" if kind == "customer" else "supplier"))


def _summary(invoice: dict[str, Any], kind: str) -> str:
    cur = invoice.get("currency") or "EUR"
    status = invoice.get("status") if kind == "customer" else invoice.get("payment_status")
    return "- " + line(
        [
            ("#", invoice.get("id")),
            ("number", invoice.get("invoice_number")),
            ("date", invoice.get("date")),
            ("due", invoice.get("deadline")),
            ("customer" if kind == "customer" else "supplier", _party(invoice, kind)),
            ("total", money(invoice.get("currency_amount") or invoice.get("amount"), cur)),
            ("before tax", money(invoice.get("currency_amount_before_tax"), cur)),
            ("left to pay", money(invoice.get("remaining_amount_with_tax"), cur) if not invoice.get("paid") else None),
            ("status", status),
            ("accounting", invoice.get("accounting_status") if kind == "supplier" else None),
            ("draft", "yes" if invoice.get("draft") else None),
            ("label", text(invoice.get("label"), 100)),
            ("analytics", categories_label(invoice.get("categories"))),
        ]
    )


def register(ctx: PennylaneContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def _list(
        kind: str,
        date_from: str,
        date_to: str,
        party_ids: Any,
        status: str,
        invoice_number: str,
        category_ids: Any,
        draft: str,
        limit: int,
        cursor: str,
    ) -> str:
        start, err = to_date(date_from, "date_from")
        end, err2 = to_date(date_to, "date_to")
        if err or err2:
            return err or err2 or ""
        party_field = "customer_id" if kind == "customer" else "supplier_id"
        conditions: list[tuple[str, str, Any]] = [
            ("date", "gteq", start),
            ("date", "lteq", end),
            (party_field, "in", id_list(party_ids)),
            ("invoice_number", "eq", invoice_number.strip()),
            ("category_id", "in", id_list(category_ids)),
        ]
        if kind == "supplier":
            conditions.append(("payment_status", "eq", status.strip()))
        elif draft in ("true", "false"):
            conditions.append(("draft", "eq", draft))
        async with ctx.client() as client:
            page, err = await ctx.page(
                client,
                KINDS[kind],
                params={"filter": filters(*conditions), "sort": "-date"},
                limit=clamp(limit),
                cursor=cursor,
            )
        if err:
            return err
        items = page.items
        if kind == "customer" and status:
            items = [i for i in items if str(i.get("status")) == status.strip()]
        if not items:
            return f"No {kind} invoice matches." + page_footer(page)
        total = sum(float(i.get("amount") or 0) for i in items)
        return (
            f"{len(items)} {kind} invoices, total {total:,.2f} EUR (company currency):\n"
            + "\n".join(_summary(i, kind) for i in items)
            + page_footer(page)
        )

    async def list_pennylane_customer_invoices(
        date_from: str = "",
        date_to: str = "",
        customer_ids: Any = None,
        status: str = "",
        invoice_number: str = "",
        category_ids: Any = None,
        draft: str = "",
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        return await _list(
            "customer", date_from, date_to, customer_ids, status, invoice_number, category_ids, draft, limit, cursor
        )

    async def list_pennylane_supplier_invoices(
        date_from: str = "",
        date_to: str = "",
        supplier_ids: Any = None,
        payment_status: str = "",
        invoice_number: str = "",
        category_ids: Any = None,
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        return await _list(
            "supplier",
            date_from,
            date_to,
            supplier_ids,
            payment_status,
            invoice_number,
            category_ids,
            "",
            limit,
            cursor,
        )

    async def get_pennylane_invoice(kind: str, invoice_id: int) -> str:
        if kind not in KINDS:
            return "Error: kind must be customer or supplier."
        base = f"{KINDS[kind]}/{int(invoice_id)}"
        async with ctx.client() as client:
            invoice, err = await ctx.get(client, base)
            if err:
                return err
            lines, _ = await ctx.page(client, f"{base}/invoice_lines", limit=100)
            payments, _ = await ctx.page(client, f"{base}/payments", limit=100)
            matched, _ = await ctx.page(client, f"{base}/matched_transactions", limit=100)
        cur = invoice.get("currency") or "EUR"
        out = [_summary(invoice, kind)[2:]]
        if invoice.get("ledger_entry"):
            out.append(f"Ledger entry: #{(invoice.get('ledger_entry') or {}).get('id')}")
        if lines and lines.items:
            out.append("Lines:")
            out += [
                "- "
                + line(
                    [
                        ("label", text(i.get("label"), 120)),
                        ("qty", i.get("quantity")),
                        ("unit price", money(i.get("raw_currency_unit_price") or i.get("unit_price"), cur)),
                        ("amount", money(i.get("currency_amount"), cur)),
                        ("vat", i.get("vat_rate")),
                        ("account", ref_label(i.get("ledger_account"))),
                        ("analytics", categories_label(i.get("categories"))),
                    ]
                )
                for i in lines.items
            ]
        if payments and payments.items:
            out.append("Payments:")
            out += [
                "- "
                + line(
                    [
                        ("date", p.get("payment_date") or p.get("date")),
                        ("amount", money(p.get("currency_amount") or p.get("amount"), p.get("currency") or cur)),
                        ("status", p.get("status")),
                        ("label", text(p.get("label"), 80)),
                    ]
                )
                for p in payments.items
            ]
        if matched and matched.items:
            out.append("Matched transactions:")
            out += [
                "- "
                + line(
                    [
                        ("#", t.get("id")),
                        ("date", t.get("date")),
                        ("amount", money(t.get("amount"), t.get("currency") or cur)),
                        ("label", text(t.get("label"), 80)),
                    ]
                )
                for t in matched.items
            ]
        else:
            out.append("Matched transactions: none")
        return "\n".join(out)

    async def download_pennylane_invoice_pdf(kind: str, invoice_id: int) -> str | ToolResult:
        if kind not in KINDS:
            return "Error: kind must be customer or supplier."
        async with ctx.client() as client:
            invoice, err = await ctx.get(client, f"{KINDS[kind]}/{int(invoice_id)}")
        if err:
            return err
        url = str(invoice.get("public_file_url") or "").strip()
        if not url:
            return f"Error: {kind} invoice #{invoice_id} has no PDF in Pennylane."
        name = str(invoice.get("filename") or f"pennylane-{kind}-invoice-{invoice_id}.pdf")
        # public_file_url is a signed link: fetched without the API token.
        async with ctx.client() as storage:
            fetched = await fetch_file(
                storage,
                "get",
                url,
                provider=PROVIDER,
                action=f"download the PDF of {kind} invoice #{invoice_id}",
                max_bytes=MAX_PDF_BYTES,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=file_content_type(fetched.content_type, name),
            source=PROVIDER,
            persist_file_fn=ctx.persist_file_fn,
            links=[("Download URL", url)],
            details=[f"The PDF of {kind} invoice #{invoice_id} ({invoice.get('invoice_number') or 'no number'})."],
        )

    async def create_pennylane_customer_invoice_draft(
        customer_id: int,
        date: str,
        deadline: str,
        lines: list[dict[str, Any]],
        label: str = "",
        currency: str = "",
        subject: str = "",
        free_text: str = "",
        external_reference: str = "",
    ) -> str:
        day, err = to_date(date, "date")
        due, err2 = to_date(deadline, "deadline")
        if err or err2 or not day or not due:
            return err or err2 or "Error: date and deadline are required."
        if not isinstance(lines, list) or not lines:
            return "Error: give at least one line."
        clean = []
        for number, raw in enumerate(lines, 1):
            if not isinstance(raw, dict):
                return f"Error: line {number} is not an object."
            if raw.get("product_id"):
                item = {"product_id": int(raw["product_id"]), "quantity": raw.get("quantity") or 1}
            else:
                missing = [k for k in ("label", "raw_currency_unit_price", "unit", "vat_rate") if not raw.get(k)]
                if missing:
                    return f"Error: line {number} needs {', '.join(missing)} (or a product_id)."
                item = {k: raw[k] for k in ("label", "raw_currency_unit_price", "unit", "vat_rate")}
                item["quantity"] = raw.get("quantity") or 1
            for extra in ("ledger_account_id", "description", "discount"):
                if raw.get(extra) not in (None, ""):
                    item[extra] = raw[extra]
            clean.append(item)
        body: dict[str, Any] = {
            "customer_id": int(customer_id),
            "date": day,
            "deadline": due,
            "draft": True,
            "invoice_lines": clean,
        }
        for key, value in (
            ("label", label),
            ("currency", currency),
            ("pdf_invoice_subject", subject),
            ("pdf_invoice_free_text", free_text),
            ("external_reference", external_reference),
        ):
            if value:
                body[key] = value
        async with ctx.client() as client:
            data, err = await ctx.request(
                client, "post", "customer_invoices", body=body, action="create the draft customer invoice"
            )
        if err:
            return err
        return f"Created draft customer invoice #{data.get('id')} for customer #{customer_id} ({len(clean)} lines, total {money(data.get('currency_amount'), data.get('currency') or 'EUR')}). It stays a draft until finalized in Pennylane."

    async def update_pennylane_supplier_invoice(
        invoice_id: int,
        invoice_number: str = "",
        label: str = "",
        date: str = "",
        deadline: str = "",
        supplier_id: int = 0,
        external_reference: str = "",
    ) -> str:
        body: dict[str, Any] = {}
        for key, value in (
            ("invoice_number", invoice_number),
            ("label", label),
            ("external_reference", external_reference),
        ):
            if value:
                body[key] = value
        for key, value in (("date", date), ("deadline", deadline)):
            if value:
                day, err = to_date(value, key)
                if err:
                    return err
                body[key] = day
        if supplier_id:
            body["supplier_id"] = int(supplier_id)
        if not body:
            return "Error: give at least one field to change."
        async with ctx.client() as client:
            _, err = await ctx.request(
                client,
                "put",
                f"supplier_invoices/{int(invoice_id)}",
                body=body,
                action=f"update supplier invoice #{invoice_id}",
            )
        return err or f"Updated supplier invoice #{invoice_id} ({', '.join(body)})."

    async def match_pennylane_invoice_transaction(kind: str, invoice_id: int, transaction_id: int) -> str:
        if kind not in KINDS:
            return "Error: kind must be customer or supplier."
        async with ctx.client() as client:
            _, err = await ctx.request(
                client,
                "post",
                f"{KINDS[kind]}/{int(invoice_id)}/matched_transactions",
                body={"transaction_id": int(transaction_id)},
                action=f"match transaction #{transaction_id} to {kind} invoice #{invoice_id}",
            )
        return err or f"Matched bank transaction #{transaction_id} to {kind} invoice #{invoice_id}."

    async def unmatch_pennylane_invoice_transaction(kind: str, invoice_id: int, transaction_id: int) -> str:
        if kind not in KINDS:
            return "Error: kind must be customer or supplier."
        async with ctx.client() as client:
            _, err = await ctx.request(
                client,
                "delete",
                f"{KINDS[kind]}/{int(invoice_id)}/matched_transactions/{int(transaction_id)}",
                action=f"unmatch transaction #{transaction_id} from {kind} invoice #{invoice_id}",
            )
        return err or f"Removed the match of transaction #{transaction_id} on {kind} invoice #{invoice_id}."

    async def mark_pennylane_customer_invoice_paid(invoice_id: int) -> str:
        async with ctx.client() as client:
            _, err = await ctx.request(
                client,
                "put",
                f"customer_invoices/{int(invoice_id)}/mark_as_paid",
                action=f"mark customer invoice #{invoice_id} paid",
            )
        return err or f"Marked customer invoice #{invoice_id} as paid in Pennylane (no money moved)."

    async def set_pennylane_supplier_invoice_payment_status(invoice_id: int, payment_status: str) -> str:
        if payment_status not in SUPPLIER_PAYMENT_STATUSES:
            return f"Error: payment_status must be one of {', '.join(SUPPLIER_PAYMENT_STATUSES)}."
        async with ctx.client() as client:
            _, err = await ctx.request(
                client,
                "put",
                f"supplier_invoices/{int(invoice_id)}/payment_status",
                body={"payment_status": payment_status},
                action=f"set the payment status of supplier invoice #{invoice_id}",
            )
        return (
            err
            or f"Set the payment status of supplier invoice #{invoice_id} to {payment_status} (no payment initiated)."
        )

    async def set_pennylane_supplier_invoice_accounting_validated(invoice_id: int) -> str:
        async with ctx.client() as client:
            _, err = await ctx.request(
                client,
                "put",
                f"supplier_invoices/{int(invoice_id)}/validate_accounting",
                action=f"validate the accounting of supplier invoice #{invoice_id}",
            )
        return err or f"Validated the accounting of supplier invoice #{invoice_id}."

    for fn in (
        list_pennylane_customer_invoices,
        list_pennylane_supplier_invoices,
        get_pennylane_invoice,
        download_pennylane_invoice_pdf,
        create_pennylane_customer_invoice_draft,
        update_pennylane_supplier_invoice,
        match_pennylane_invoice_transaction,
        unmatch_pennylane_invoice_transaction,
        mark_pennylane_customer_invoice_paid,
        set_pennylane_supplier_invoice_payment_status,
        set_pennylane_supplier_invoice_accounting_validated,
    ):
        executor.register(fn.__name__, fn)

    kind = {"type": "string", "enum": list(KINDS), "description": "customer or supplier"}
    common = {
        "date_from": DATE_FROM,
        "date_to": DATE_TO,
        "invoice_number": {"type": "string"},
        "category_ids": ids_schema("analytical category"),
        "limit": LIMIT,
        "cursor": CURSOR,
    }
    return [
        definition(
            "list_pennylane_customer_invoices",
            "Customer (sales) invoices by date, customer, status (paid, late, upcoming, draft, partially_paid...), number or analytical category, newest first, with totals and what is left to pay.",
            {
                **common,
                "customer_ids": ids_schema("customer"),
                "status": {"type": "string"},
                "draft": {"type": "string", "enum": ["true", "false"]},
            },
            priority=0,
            read_only=True,
        ),
        definition(
            "list_pennylane_supplier_invoices",
            "Supplier (purchase) invoices by date, supplier, payment status (to_be_paid, fully_paid, paid_offline...), number or analytical category, newest first, with accounting status.",
            {**common, "supplier_ids": ids_schema("supplier"), "payment_status": {"type": "string"}},
            priority=0,
            read_only=True,
        ),
        definition(
            "get_pennylane_invoice",
            "One customer or supplier invoice with its lines, payments, matched bank transactions and ledger entry.",
            {"kind": kind, "invoice_id": id_schema("invoice")},
            ["kind", "invoice_id"],
            priority=0,
            read_only=True,
        ),
        definition(
            "download_pennylane_invoice_pdf",
            "Download the PDF of a customer or supplier invoice into the conversation.",
            {"kind": kind, "invoice_id": id_schema("invoice")},
            ["kind", "invoice_id"],
            read_only=True,
        ),
        definition(
            "create_pennylane_customer_invoice_draft",
            "Create a DRAFT customer invoice (never finalized or sent): lines by product_id and quantity, or label, raw_currency_unit_price, unit, vat_rate (FR_200 = 20%) and quantity.",
            {
                "customer_id": id_schema("customer"),
                "date": {"type": "string"},
                "deadline": {"type": "string", "description": "Payment deadline (YYYY-MM-DD)"},
                "lines": {"type": "array", "items": {"type": "object"}, "description": "Invoice lines"},
                "label": {"type": "string"},
                "currency": {"type": "string"},
                "subject": {"type": "string"},
                "free_text": {"type": "string"},
                "external_reference": {"type": "string"},
            },
            ["customer_id", "date", "deadline", "lines"],
        ),
        definition(
            "update_pennylane_supplier_invoice",
            "Update a supplier invoice's number, label, dates, supplier or external reference.",
            {
                "invoice_id": id_schema("supplier invoice"),
                "invoice_number": {"type": "string"},
                "label": {"type": "string"},
                "date": {"type": "string"},
                "deadline": {"type": "string"},
                "supplier_id": {"type": "integer"},
                "external_reference": {"type": "string"},
            },
            ["invoice_id"],
        ),
        definition(
            "match_pennylane_invoice_transaction",
            "Match a bank transaction to a customer or supplier invoice (reconciliation).",
            {"kind": kind, "invoice_id": id_schema("invoice"), "transaction_id": id_schema("transaction")},
            ["kind", "invoice_id", "transaction_id"],
        ),
        definition(
            "unmatch_pennylane_invoice_transaction",
            "Remove the match between a bank transaction and an invoice.",
            {"kind": kind, "invoice_id": id_schema("invoice"), "transaction_id": id_schema("transaction")},
            ["kind", "invoice_id", "transaction_id"],
            priority=2,
        ),
        definition(
            "mark_pennylane_customer_invoice_paid",
            "Mark a customer invoice as paid in Pennylane's books (moves no money). Waits for a person's approval.",
            {"invoice_id": id_schema("customer invoice")},
            ["invoice_id"],
            priority=2,
            always_ask=True,
        ),
        definition(
            "set_pennylane_supplier_invoice_payment_status",
            "Set a supplier invoice's payment status (paid or to_be_paid); initiates no payment. Waits for a person's approval.",
            {
                "invoice_id": id_schema("supplier invoice"),
                "payment_status": {"type": "string", "enum": list(SUPPLIER_PAYMENT_STATUSES)},
            },
            ["invoice_id", "payment_status"],
            priority=2,
            always_ask=True,
        ),
        definition(
            "set_pennylane_supplier_invoice_accounting_validated",
            "Validate the accounting of a supplier invoice (its entry becomes final).",
            {"invoice_id": id_schema("supplier invoice")},
            ["invoice_id"],
            priority=2,
        ),
    ]
