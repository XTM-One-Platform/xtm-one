"""Customers, suppliers, products and quotes: lists with filters, one record,
and the customer / supplier writes. Supplier bank details (IBAN) are masked
in every answer and never written: no tool here changes bank details."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CURSOR,
    LIMIT,
    PennylaneContext,
    clamp,
    definition,
    filters,
    id_schema,
    line,
    money,
    page_footer,
    ref_label,
    text,
)


def mask_iban(value: Any) -> str:
    """An IBAN reduced to its country and last four characters."""
    raw = "".join(str(value or "").split())
    if len(raw) < 8:
        return "" if not raw else "****"
    return f"{raw[:2]}** **** {raw[-4:]}"


def _address(addr: Any) -> str:
    if not isinstance(addr, dict):
        return ""
    return text(
        ", ".join(str(addr.get(k)) for k in ("address", "postal_code", "city", "country_alpha2") if addr.get(k)), 160
    )


def _customer(c: dict[str, Any]) -> str:
    name = c.get("name") or f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
    return "- " + line(
        [
            ("#", c.get("id")),
            ("name", text(name, 80)),
            ("type", c.get("customer_type")),
            ("reg", c.get("reg_no")),
            ("vat", c.get("vat_number")),
            ("emails", ", ".join(c.get("emails") or [])),
            ("account", ref_label(c.get("ledger_account")) if c.get("ledger_account") else None),
            ("terms", c.get("payment_conditions")),
            ("address", _address(c.get("billing_address"))),
            ("ref", c.get("external_reference")),
        ]
    )


def _supplier(s: dict[str, Any]) -> str:
    return "- " + line(
        [
            ("#", s.get("id")),
            ("name", text(s.get("name"), 80)),
            ("reg", s.get("reg_no")),
            ("vat", s.get("vat_number")),
            ("emails", ", ".join(s.get("emails") or [])),
            ("iban", mask_iban(s.get("iban"))),
            ("payment", s.get("supplier_payment_method")),
            (
                "due",
                f"{s.get('supplier_due_date_delay')} days {s.get('supplier_due_date_rule') or ''}".strip()
                if s.get("supplier_due_date_delay") is not None
                else None,
            ),
            ("account", ref_label(s.get("ledger_account")) if s.get("ledger_account") else None),
            ("address", _address(s.get("postal_address"))),
            ("ref", s.get("external_reference")),
        ]
    )


def _address_arg(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if value in (None, "", {}):
        return None, None
    if not isinstance(value, dict):
        return None, "Error: the address is an object {address, postal_code, city, country_alpha2}."
    missing = [k for k in ("address", "postal_code", "city", "country_alpha2") if not value.get(k)]
    if missing:
        return None, f"Error: the address needs {', '.join(missing)}."
    return {k: str(value[k]) for k in ("address", "postal_code", "city", "country_alpha2")}, None


def register(ctx: PennylaneContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def list_pennylane_customers(
        name_prefix: str = "", email: str = "", limit: int = 25, cursor: str = ""
    ) -> str:
        flt = filters(("name", "start_with", name_prefix.strip()), ("emails", "in", email.strip()))
        async with ctx.client() as client:
            page, err = await ctx.page(client, "customers", params={"filter": flt}, limit=clamp(limit), cursor=cursor)
        if err:
            return err
        if not page.items:
            return "No customer matches."
        return f"{len(page.items)} customers:\n" + "\n".join(_customer(c) for c in page.items) + page_footer(page)

    async def get_pennylane_customer(customer_id: int) -> str:
        async with ctx.client() as client:
            data, err = await ctx.get(client, f"customers/{int(customer_id)}")
        return err or _customer(data)[2:]

    async def list_pennylane_suppliers(
        name_prefix: str = "", email: str = "", limit: int = 25, cursor: str = ""
    ) -> str:
        flt = filters(("name", "start_with", name_prefix.strip()), ("emails", "in", email.strip()))
        async with ctx.client() as client:
            page, err = await ctx.page(client, "suppliers", params={"filter": flt}, limit=clamp(limit), cursor=cursor)
        if err:
            return err
        if not page.items:
            return "No supplier matches."
        return f"{len(page.items)} suppliers:\n" + "\n".join(_supplier(s) for s in page.items) + page_footer(page)

    async def get_pennylane_supplier(supplier_id: int) -> str:
        async with ctx.client() as client:
            data, err = await ctx.get(client, f"suppliers/{int(supplier_id)}")
        return err or _supplier(data)[2:]

    async def list_pennylane_products(label: str = "", reference: str = "", limit: int = 25, cursor: str = "") -> str:
        flt = filters(("label", "eq", label.strip()), ("reference", "eq", reference.strip()))
        async with ctx.client() as client:
            page, err = await ctx.page(client, "products", params={"filter": flt}, limit=clamp(limit), cursor=cursor)
        if err:
            return err
        if not page.items:
            return "No product matches."
        return (
            f"{len(page.items)} products:\n"
            + "\n".join(
                "- "
                + line(
                    [
                        ("#", p.get("id")),
                        ("label", text(p.get("label"), 80)),
                        ("ref", p.get("reference")),
                        ("price before tax", money(p.get("price_before_tax"), p.get("currency"))),
                        ("vat", p.get("vat_rate")),
                        ("unit", p.get("unit")),
                        ("archived", "yes" if p.get("archived_at") else None),
                    ]
                )
                for p in page.items
            )
            + page_footer(page)
        )

    async def list_pennylane_quotes(status: str = "", customer_id: int = 0, limit: int = 25, cursor: str = "") -> str:
        flt = filters(("status", "eq", status.strip()), ("customer_id", "eq", int(customer_id) if customer_id else ""))
        async with ctx.client() as client:
            page, err = await ctx.page(client, "quotes", params={"filter": flt}, limit=clamp(limit), cursor=cursor)
        if err:
            return err
        if not page.items:
            return "No quote matches."
        return (
            f"{len(page.items)} quotes:\n"
            + "\n".join(
                "- "
                + line(
                    [
                        ("#", q.get("id")),
                        ("number", q.get("quote_number")),
                        ("date", q.get("date")),
                        ("valid until", q.get("deadline")),
                        ("customer", ref_label(q.get("customer"))),
                        ("total", money(q.get("currency_amount"), q.get("currency"))),
                        ("status", q.get("status")),
                        ("label", text(q.get("label"), 80)),
                    ]
                )
                for q in page.items
            )
            + page_footer(page)
        )

    async def create_pennylane_customer(
        name: str = "",
        first_name: str = "",
        last_name: str = "",
        billing_address: Any = None,
        emails: Any = None,
        vat_number: str = "",
        reg_no: str = "",
        payment_conditions: str = "",
        external_reference: str = "",
    ) -> str:
        address, err = _address_arg(billing_address)
        if err:
            return err
        if not address:
            return "Error: billing_address is required."
        if name:
            path, body = "company_customers", {"name": name, "billing_address": address}
            for key, value in (("vat_number", vat_number), ("reg_no", reg_no)):
                if value:
                    body[key] = value
        elif first_name and last_name:
            path, body = (
                "individual_customers",
                {"first_name": first_name, "last_name": last_name, "billing_address": address},
            )
        else:
            return "Error: give name (a company) or first_name and last_name (an individual)."
        if emails:
            body["emails"] = (
                emails if isinstance(emails, list) else [e.strip() for e in str(emails).split(",") if e.strip()]
            )
        for key, value in (("payment_conditions", payment_conditions), ("external_reference", external_reference)):
            if value:
                body[key] = value
        async with ctx.client() as client:
            data, err = await ctx.request(client, "post", path, body=body, action="create the customer")
        return err or f"Created customer #{data.get('id')} ({name or f'{first_name} {last_name}'})."

    async def update_pennylane_customer(
        customer_id: int,
        name: str = "",
        billing_address: Any = None,
        emails: Any = None,
        vat_number: str = "",
        payment_conditions: str = "",
        external_reference: str = "",
    ) -> str:
        body: dict[str, Any] = {}
        address, err = _address_arg(billing_address)
        if err:
            return err
        if address:
            body["billing_address"] = address
        if emails:
            body["emails"] = (
                emails if isinstance(emails, list) else [e.strip() for e in str(emails).split(",") if e.strip()]
            )
        for key, value in (
            ("name", name),
            ("vat_number", vat_number),
            ("payment_conditions", payment_conditions),
            ("external_reference", external_reference),
        ):
            if value:
                body[key] = value
        if not body:
            return "Error: give at least one field to change."
        async with ctx.client() as client:
            _, err = await ctx.request(
                client, "put", f"customers/{int(customer_id)}", body=body, action=f"update customer #{customer_id}"
            )
        return err or f"Updated customer #{customer_id} ({', '.join(body)})."

    async def create_pennylane_supplier(
        name: str,
        postal_address: Any = None,
        emails: Any = None,
        vat_number: str = "",
        reg_no: str = "",
        external_reference: str = "",
    ) -> str:
        body: dict[str, Any] = {"name": name}
        address, err = _address_arg(postal_address)
        if err:
            return err
        if address:
            body["postal_address"] = address
        if emails:
            body["emails"] = (
                emails if isinstance(emails, list) else [e.strip() for e in str(emails).split(",") if e.strip()]
            )
        for key, value in (("vat_number", vat_number), ("reg_no", reg_no), ("external_reference", external_reference)):
            if value:
                body[key] = value
        async with ctx.client() as client:
            data, err = await ctx.request(client, "post", "suppliers", body=body, action="create the supplier")
        return err or f"Created supplier #{data.get('id')} ({name}), without bank details."

    async def update_pennylane_supplier(
        supplier_id: int,
        name: str = "",
        postal_address: Any = None,
        emails: Any = None,
        vat_number: str = "",
        external_reference: str = "",
    ) -> str:
        body: dict[str, Any] = {}
        address, err = _address_arg(postal_address)
        if err:
            return err
        if address:
            body["postal_address"] = address
        if emails:
            body["emails"] = (
                emails if isinstance(emails, list) else [e.strip() for e in str(emails).split(",") if e.strip()]
            )
        for key, value in (("name", name), ("vat_number", vat_number), ("external_reference", external_reference)):
            if value:
                body[key] = value
        if not body:
            return "Error: give at least one field to change (bank details are never changed here)."
        async with ctx.client() as client:
            _, err = await ctx.request(
                client, "put", f"suppliers/{int(supplier_id)}", body=body, action=f"update supplier #{supplier_id}"
            )
        return err or f"Updated supplier #{supplier_id} ({', '.join(body)})."

    for fn in (
        list_pennylane_customers,
        get_pennylane_customer,
        list_pennylane_suppliers,
        get_pennylane_supplier,
        list_pennylane_products,
        list_pennylane_quotes,
        create_pennylane_customer,
        update_pennylane_customer,
        create_pennylane_supplier,
        update_pennylane_supplier,
    ):
        executor.register(fn.__name__, fn)

    address = {
        "type": "object",
        "description": "{address, postal_code, city, country_alpha2}",
        "properties": {k: {"type": "string"} for k in ("address", "postal_code", "city", "country_alpha2")},
    }
    emails = {"type": "array", "items": {"type": "string"}}
    paging = {"limit": LIMIT, "cursor": CURSOR}
    search = {
        "name_prefix": {"type": "string", "description": "Name starts with"},
        "email": {"type": "string"},
        **paging,
    }
    return [
        definition(
            "list_pennylane_customers",
            "Customers by name prefix or email: registration, VAT, emails, payment terms, billing address.",
            search,
            priority=0,
            read_only=True,
        ),
        definition(
            "get_pennylane_customer",
            "One customer.",
            {"customer_id": id_schema("customer")},
            ["customer_id"],
            read_only=True,
        ),
        definition(
            "list_pennylane_suppliers",
            "Suppliers by name prefix or email: registration, VAT, emails, payment method and terms (IBAN masked).",
            search,
            priority=0,
            read_only=True,
        ),
        definition(
            "get_pennylane_supplier",
            "One supplier (IBAN masked).",
            {"supplier_id": id_schema("supplier")},
            ["supplier_id"],
            read_only=True,
        ),
        definition(
            "list_pennylane_products",
            "Products of the catalog with price before tax, VAT rate and unit.",
            {"label": {"type": "string"}, "reference": {"type": "string"}, **paging},
            read_only=True,
        ),
        definition(
            "list_pennylane_quotes",
            "Quotes by status (pending, accepted, denied, expired, invoiced) or customer.",
            {"status": {"type": "string"}, "customer_id": id_schema("customer"), **paging},
            read_only=True,
        ),
        definition(
            "create_pennylane_customer",
            "Create a customer: a company (name) or an individual (first_name, last_name), with its billing address.",
            {
                "name": {"type": "string"},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "billing_address": address,
                "emails": emails,
                "vat_number": {"type": "string"},
                "reg_no": {"type": "string"},
                "payment_conditions": {
                    "type": "string",
                    "description": "upon_receipt, 7_days, 15_days, 30_days, 30_days_end_of_month, 45_days, 45_days_end_of_month, 60_days",
                },
                "external_reference": {"type": "string"},
            },
            ["billing_address"],
        ),
        definition(
            "update_pennylane_customer",
            "Update a customer's name, billing address, emails, VAT number, payment terms or reference.",
            {
                "customer_id": id_schema("customer"),
                "name": {"type": "string"},
                "billing_address": address,
                "emails": emails,
                "vat_number": {"type": "string"},
                "payment_conditions": {"type": "string"},
                "external_reference": {"type": "string"},
            },
            ["customer_id"],
        ),
        definition(
            "create_pennylane_supplier",
            "Create a supplier (name, address, emails, VAT, registration). Bank details are never set here.",
            {
                "name": {"type": "string"},
                "postal_address": address,
                "emails": emails,
                "vat_number": {"type": "string"},
                "reg_no": {"type": "string"},
                "external_reference": {"type": "string"},
            },
            ["name"],
        ),
        definition(
            "update_pennylane_supplier",
            "Update a supplier's name, address, emails, VAT number or reference. Bank details are never changed here.",
            {
                "supplier_id": id_schema("supplier"),
                "name": {"type": "string"},
                "postal_address": address,
                "emails": emails,
                "vat_number": {"type": "string"},
                "external_reference": {"type": "string"},
            },
            ["supplier_id"],
        ),
    ]
