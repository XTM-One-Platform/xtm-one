# SPDX-License-Identifier: Apache-2.0

"""Perk (formerly TravelPerk) integration – trips, bookings, invoices, users, webhooks.

API reference: https://developers.perk.com/reference
Auth: API Key (full admin) — ``Authorization: ApiKey {key}``
Version header: ``Api-Version: 1``
Base URL: ``https://api.travelperk.com``
"""

from __future__ import annotations

import json as _json
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.file_delivery import deliver_file, fetch_file, file_content_type
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

_BASE_URL = "https://api.travelperk.com"
#: Cost Centers API (https://developers.perk.com/reference/list-cost-centers):
#: underscored, unlike the other resources. ``/cost-centers`` answers 404.
_COST_CENTERS_PATH = "/cost_centers"
_TIMEOUT = 30
_PROVIDER = "Perk"
#: Largest invoice PDF downloaded.
_MAX_PDF_BYTES = 25 * 1024 * 1024


def _headers(api_key: str) -> dict[str, str]:
    return {"Api-Version": "1", "Authorization": f"ApiKey {api_key}"}


def _invoice_pdf_url(invoice: dict) -> str:
    """The invoice's ``pdf`` link, or its ``/invoices/{serial}/pdf`` endpoint."""
    link = str(invoice.get("pdf") or "").strip()
    if link:
        return link
    serial = str(invoice.get("serial_number") or invoice.get("id") or "")
    return f"{_BASE_URL}/invoices/{quote(serial, safe='')}/pdf"


# ---------------------------------------------------------------------------
# Perk tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    headers = _headers(token)
    persist_file_fn = _kwargs.get("persist_file_fn")

    # ── Trips ──────────────────────────────────────────────────────────

    async def list_perk_trips(
        limit: int = 20,
        status: str = "",
        traveler_id: str = "",
        modified_gte: str = "",
        modified_lt: str = "",
    ) -> str:
        """List trips ordered by last modified date (descending)."""
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        if status:
            params["status"] = status
        if traveler_id:
            params["traveler_id"] = traveler_id
        if modified_gte:
            params["modified_gte"] = modified_gte
        if modified_lt:
            params["modified_lt"] = modified_lt

        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/trips",
                _PROVIDER,
                "list trips",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            trips = data.get("trips", data.get("results", []))
            if not trips:
                return "No trips found."
            lines: list[str] = []
            for t in trips:
                tid = t.get("id", "?")
                name = t.get("trip_name", t.get("name", "Untitled"))
                st = t.get("status", "?")
                start = t.get("start", "?")
                end = t.get("end", "?")
                booker = t.get("booker_id", "")
                start_loc = t.get("start_location", {})
                end_loc = t.get("end_location", {})
                origin = (
                    start_loc.get("name", start_loc.get("iata_code", ""))
                    if isinstance(start_loc, dict)
                    else ""
                )
                dest = (
                    end_loc.get("name", end_loc.get("iata_code", ""))
                    if isinstance(end_loc, dict)
                    else ""
                )
                route = f" | Route: {origin} → {dest}" if origin or dest else ""
                lines.append(
                    f"ID: {tid} | {name} | Status: {st} | {start} → {end}{route}"
                    + (f" | Booker: {booker}" if booker else "")
                )
            total = data.get("total", len(trips))
            return f"Showing {len(trips)} of {total} trips:\n" + "\n".join(lines)

    async def read_perk_trip(trip_id: str) -> str:
        """Get full details of a specific trip by ID."""
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/trips/{trip_id}",
                _PROVIDER,
                "read trip",
                headers=headers,
            )
            if err:
                return err
            return _json.dumps(resp.json(), indent=2, default=str)

    # ── Bookings ───────────────────────────────────────────────────────

    async def list_perk_bookings(
        limit: int = 20,
        trip_id: str = "",
        status: str = "",
        booking_type: str = "",
        modified_gte: str = "",
        modified_lt: str = "",
    ) -> str:
        """List bookings (flights, hotels, cars, trains) with optional filters."""
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        if trip_id:
            params["trip_id"] = trip_id
        if status:
            params["status"] = status
        if booking_type:
            params["type"] = booking_type
        if modified_gte:
            params["modified_gte"] = modified_gte
        if modified_lt:
            params["modified_lt"] = modified_lt

        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/bookings",
                _PROVIDER,
                "list bookings",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            bookings = data.get("bookings", data.get("results", []))
            if not bookings:
                return "No bookings found."
            lines: list[str] = []
            for b in bookings:
                bid = b.get("id", "?")
                btype = b.get("type", "?")
                st = b.get("status", "?")
                conf = b.get("confirmation_number", b.get("confirmation", "N/A"))
                start = b.get("start", {})
                end = b.get("end", {})
                start_str = (
                    start.get("datetime", start.get("date", "?"))
                    if isinstance(start, dict)
                    else str(start)
                )
                end_str = (
                    end.get("datetime", end.get("date", "?"))
                    if isinstance(end, dict)
                    else str(end)
                )
                loc = b.get("location", {})
                loc_name = (
                    loc.get("name", loc.get("city", ""))
                    if isinstance(loc, dict)
                    else ""
                )
                loc_str = f" | Location: {loc_name}" if loc_name else ""
                lines.append(
                    f"ID: {bid} | {btype} | Status: {st} | Confirmation: {conf} | {start_str} → {end_str}{loc_str}"
                )
            total = data.get("total", len(bookings))
            return f"Showing {len(bookings)} of {total} bookings:\n" + "\n".join(lines)

    # ── Invoices ───────────────────────────────────────────────────────

    async def list_perk_invoices(
        limit: int = 20,
        status: str = "",
        billing_period: str = "",
        issuing_date_gte: str = "",
        issuing_date_lte: str = "",
        due_date_gte: str = "",
        due_date_lte: str = "",
        serial_number_contains: str = "",
    ) -> str:
        """List invoices with optional filters. Only invoices from Jan 2019 onwards."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if billing_period:
            params["billing_period"] = billing_period
        if issuing_date_gte:
            params["issuing_date_gte"] = issuing_date_gte
        if issuing_date_lte:
            params["issuing_date_lte"] = issuing_date_lte
        if due_date_gte:
            params["due_date_gte"] = due_date_gte
        if due_date_lte:
            params["due_date_lte"] = due_date_lte
        if serial_number_contains:
            params["serial_number_contains"] = serial_number_contains

        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/invoices",
                _PROVIDER,
                "list invoices",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            invoices = data.get("invoices", data.get("results", []))
            if not invoices:
                return "No invoices found."
            lines: list[str] = []
            for inv in invoices:
                serial = inv.get("serial_number", inv.get("id", "?"))
                st = inv.get("status", "?")
                total = inv.get("total", inv.get("amount", "?"))
                currency = inv.get("currency", "?")
                issued = inv.get("issuing_date", inv.get("date", "?"))
                due = inv.get("due_date", "")
                profile = inv.get("profile_id", "")
                due_str = f" | Due: {due}" if due else ""
                prof_str = f" | Profile: {profile}" if profile else ""
                lines.append(
                    f"#{serial} | Status: {st} | {total} {currency} | Issued: {issued}{due_str}{prof_str}"
                    f" | PDF: {_invoice_pdf_url(inv)}"
                )
            total_count = data.get("total", len(invoices))
            return f"Showing {len(invoices)} of {total_count} invoices:\n" + "\n".join(
                lines
            )

    async def read_perk_invoice(invoice_id: str) -> str:
        """Retrieve a single invoice by ID with full details."""
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/invoices/{invoice_id}",
                _PROVIDER,
                "read invoice",
                headers=headers,
            )
            if err:
                return err
            invoice = resp.json()
            text = _json.dumps(invoice, indent=2, default=str)
            if isinstance(invoice, dict):
                text += f"\nPDF: {_invoice_pdf_url(invoice)}"
            return text

    async def download_perk_invoice_pdf(serial_number: str) -> str | ToolResult:
        """Download an invoice's PDF and store it as a file of the run."""
        serial = str(serial_number or "").strip().lstrip("#")
        if not serial:
            return "Error: serial_number is required (e.g. INV-01-987654)."
        url = f"{_BASE_URL}/invoices/{quote(serial, safe='')}/pdf"
        async with egress_client(timeout=60) as client:
            fetched = await fetch_file(
                client,
                "get",
                url,
                provider=_PROVIDER,
                action=f"download invoice {serial} PDF",
                max_bytes=_MAX_PDF_BYTES,
                headers=headers,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nPDF: {url}"
        filename = fetched.filename or f"{serial}.pdf"
        return await deliver_file(
            data=fetched.data,
            filename=filename,
            content_type=file_content_type(fetched.content_type, filename),
            source="Perk",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", url)],
            details=[f"Invoice {serial}."],
        )

    async def list_perk_invoice_lines(
        limit: int = 20,
        status: str = "",
        serial_number_contains: str = "",
        issuing_date_gte: str = "",
        issuing_date_lte: str = "",
    ) -> str:
        """List invoice line items with optional filters."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if serial_number_contains:
            params["serial_number_contains"] = serial_number_contains
        if issuing_date_gte:
            params["issuing_date_gte"] = issuing_date_gte
        if issuing_date_lte:
            params["issuing_date_lte"] = issuing_date_lte

        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/invoices/lines",
                _PROVIDER,
                "list invoice lines",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            lines_data = data.get("invoice_lines", data.get("results", []))
            if not lines_data:
                return "No invoice lines found."
            lines: list[str] = []
            for ln in lines_data:
                desc = ln.get("description", ln.get("expense_type", "?"))
                amount = ln.get("total_amount", ln.get("amount", "?"))
                currency = ln.get("currency", "?")
                invoice_serial = ln.get("invoice_serial_number", "?")
                lines.append(
                    f"Invoice #{invoice_serial} | {desc} | {amount} {currency}"
                )
            total_count = data.get("total", len(lines_data))
            return (
                f"Showing {len(lines_data)} of {total_count} invoice lines:\n"
                + "\n".join(lines)
            )

    async def list_perk_invoice_profiles(
        limit: int = 20,
        exclude_personal: bool = False,
    ) -> str:
        """List invoice profiles (billing entities) associated with the account."""
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        if exclude_personal:
            params["exclude_personal"] = "true"

        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/profiles",
                _PROVIDER,
                "list invoice profiles",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            profiles = data.get("profiles", data.get("results", []))
            if not profiles:
                return "No invoice profiles found."
            lines: list[str] = []
            for p in profiles:
                pid = p.get("id", "?")
                name = p.get("name", p.get("company_name", "?"))
                country = p.get("country_name", p.get("country", ""))
                billing = p.get("billing_period", "")
                country_str = f" | Country: {country}" if country else ""
                billing_str = f" | Billing: {billing}" if billing else ""
                lines.append(f"ID: {pid} | {name}{country_str}{billing_str}")
            return "\n".join(lines)

    # ── Suppliers ──────────────────────────────────────────────────────

    async def list_perk_suppliers(
        limit: int = 20,
        booking_id: str = "",
    ) -> str:
        """List suppliers related to account bookings, ordered by ID."""
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        if booking_id:
            params["booking_id"] = booking_id

        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/suppliers",
                _PROVIDER,
                "list suppliers",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            suppliers = data.get("suppliers", data.get("results", []))
            if not suppliers:
                return "No suppliers found."
            lines: list[str] = []
            for s in suppliers:
                sid = s.get("id", "?")
                name = s.get("name", "?")
                stype = s.get("type", "")
                type_str = f" | Type: {stype}" if stype else ""
                lines.append(f"ID: {sid} | {name}{type_str}")
            return "\n".join(lines)

    # ── Users ──────────────────────────────────────────────────────────

    async def list_perk_users(
        limit: int = 50,
        trip_id: str = "",
        user_id: str = "",
    ) -> str:
        """List users (travelers/employees) on the account."""
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        if trip_id:
            params["trip_id"] = trip_id
        if user_id:
            params["user_id"] = user_id

        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users",
                _PROVIDER,
                "list users",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            users = data.get("users", data.get("results", []))
            if not users:
                return "No users found."
            lines: list[str] = []
            for u in users:
                uid = u.get("id", "?")
                first = u.get("first_name", "")
                last = u.get("last_name", "")
                name = f"{first} {last}".strip() or "Unknown"
                email = u.get("email", "")
                phone = u.get("phone", "")
                email_str = f" | Email: {email}" if email else ""
                phone_str = f" | Phone: {phone}" if phone else ""
                lines.append(f"ID: {uid} | {name}{email_str}{phone_str}")
            total = data.get("total", len(users))
            return f"Showing {len(users)} of {total} users:\n" + "\n".join(lines)

    # ── Cost Centers ───────────────────────────────────────────────────

    async def list_perk_cost_centers(limit: int = 50, offset: int = 0) -> str:
        """List the cost centers of the Perk account (``GET /cost_centers``)."""
        try:
            size, start = int(limit or 50), int(offset or 0)
        except (TypeError, ValueError):
            return "Error: limit and offset must be integers."
        params = {"limit": max(1, min(size, 100)), "offset": max(0, start)}
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}{_COST_CENTERS_PATH}",
                _PROVIDER,
                "list cost centers",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            centers = data.get("cost_centers", data.get("results", []))
            if not centers:
                return "No cost centers found."
            lines: list[str] = []
            for c in centers:
                cid = c.get("id", "?")
                name = c.get("name", "?")
                details = [
                    f"{label}: {c[key]}"
                    for key, label in (
                        ("erp_code", "ERP code"),
                        ("code", "Code"),
                        ("count_users", "Users"),
                    )
                    if c.get(key) not in (None, "")
                ]
                if c.get("archived"):
                    details.append("archived")
                lines.append(" | ".join([f"ID: {cid}", str(name), *details]))
            shown = f"Showing {len(centers)} cost center(s) from offset {params['offset']}"
            more = (
                f" - more may follow: list_perk_cost_centers(offset={params['offset'] + len(centers)})"
                if len(centers) >= params["limit"]
                else ""
            )
            return f"{shown}{more}:\n" + "\n".join(lines)

    async def read_perk_cost_center(cost_center_id: str) -> str:
        """Get a cost center by ID: users, approver, delegate, companies (``GET /cost_centers/{id}``)."""
        cid = str(cost_center_id or "").strip()
        if not cid:
            return "Error: cost_center_id is required (from list_perk_cost_centers)."
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}{_COST_CENTERS_PATH}/{quote(cid, safe='')}",
                _PROVIDER,
                "read cost center",
                headers=headers,
            )
            if err:
                return err
            return _json.dumps(resp.json(), indent=2, default=str)

    # ── Webhooks ───────────────────────────────────────────────────────

    async def list_perk_webhooks() -> str:
        """List all webhook subscriptions configured on the Perk account."""
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/webhooks",
                _PROVIDER,
                "list webhooks",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            hooks = data.get("webhooks", data.get("results", []))
            if not hooks:
                return "No webhooks configured."
            lines: list[str] = []
            for h in hooks:
                hid = h.get("id", "?")
                name = h.get("name", "?")
                url = h.get("url", "?")
                events = ", ".join(h.get("events", []))
                lines.append(f"ID: {hid} | {name} | URL: {url} | Events: {events}")
            return "\n".join(lines)

    async def list_perk_webhook_events() -> str:
        """List all available Perk webhook event types you can subscribe to."""
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/webhooks/events",
                _PROVIDER,
                "list webhook events",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            events = data.get("events", data.get("results", []))
            if not events:
                return "No webhook events available."
            if isinstance(events, list) and events and isinstance(events[0], str):
                return "Available events:\n" + "\n".join(f"  - {e}" for e in events)
            lines: list[str] = []
            for e in events:
                if isinstance(e, dict):
                    name = e.get("name", e.get("topic", "?"))
                    desc = e.get("description", "")
                    lines.append(f"  - {name}" + (f": {desc}" if desc else ""))
                else:
                    lines.append(f"  - {e}")
            return "Available events:\n" + "\n".join(lines)

    async def create_perk_webhook(name: str, url: str, secret: str, events: str) -> str:
        """Create a webhook subscription on the Perk account.

        ``events`` is a comma-separated list of event names.
        """
        event_list = [e.strip() for e in events.split(",") if e.strip()]
        if not event_list:
            return "Error: at least one event is required."

        payload = {
            "name": name,
            "url": url,
            "secret": secret,
            "events": event_list,
        }
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/webhooks",
                _PROVIDER,
                "create webhook",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            hook = resp.json()
            return (
                f"Webhook created: {hook.get('name', name)} (ID: {hook.get('id', '?')})"
            )

    async def delete_perk_webhook(webhook_id: str) -> str:
        """Delete a webhook subscription by ID."""
        async with egress_client(timeout=_TIMEOUT) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/webhooks/{webhook_id}",
                _PROVIDER,
                "delete webhook",
                headers=headers,
            )
            if err:
                return err
            return f"Webhook {webhook_id} deleted successfully."

    # ── Custom Fields ──────────────────────────────────────────────────

    async def list_perk_custom_fields(trip_id: str = "") -> str:
        """The custom field values of a trip (``GET /trips/{trip_id}/custom-fields``).

        Perk exposes custom fields per trip only: there is no account-wide list.
        """
        tid = str(trip_id or "").strip()
        if not tid:
            return (
                "Error: Perk exposes custom field values per trip - pass trip_id "
                "(from list_perk_trips)."
            )
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/trips/{quote(tid, safe='')}/custom-fields",
                _PROVIDER,
                "list custom fields",
                headers=headers,
            )
            if err:
                return err
            return _json.dumps(resp.json(), indent=2, default=str)

    # ── Register all tools ─────────────────────────────────────────────

    executor.register("list_perk_trips", list_perk_trips)
    executor.register("read_perk_trip", read_perk_trip)
    executor.register("list_perk_bookings", list_perk_bookings)
    executor.register("list_perk_invoices", list_perk_invoices)
    executor.register("read_perk_invoice", read_perk_invoice)
    executor.register("download_perk_invoice_pdf", download_perk_invoice_pdf)
    executor.register("list_perk_invoice_lines", list_perk_invoice_lines)
    executor.register("list_perk_invoice_profiles", list_perk_invoice_profiles)
    executor.register("list_perk_suppliers", list_perk_suppliers)
    executor.register("list_perk_users", list_perk_users)
    executor.register("list_perk_cost_centers", list_perk_cost_centers)
    executor.register("read_perk_cost_center", read_perk_cost_center)
    executor.register("list_perk_webhooks", list_perk_webhooks)
    executor.register("list_perk_webhook_events", list_perk_webhook_events)
    executor.register("create_perk_webhook", create_perk_webhook)
    executor.register("delete_perk_webhook", delete_perk_webhook)
    executor.register("list_perk_custom_fields", list_perk_custom_fields)

    return [
        # ── Trips ──────────────────────────────────────────────────────
        ToolDefinition(
            name="list_perk_trips",
            priority=0,
            description=(
                "List trips from Perk with traveler names, dates, status, and locations. "
                "Supports filtering by status, traveler_id, and modified date range."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max trips to return (default 20)",
                        "default": 20,
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by trip status (e.g. booked, cancelled)",
                        "default": "",
                    },
                    "traveler_id": {
                        "type": "string",
                        "description": "Filter by traveler user ID",
                        "default": "",
                    },
                    "modified_gte": {
                        "type": "string",
                        "description": "Modified on or after this date (ISO 8601)",
                        "default": "",
                    },
                    "modified_lt": {
                        "type": "string",
                        "description": "Modified before this date (ISO 8601)",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:perk",
        ),
        ToolDefinition(
            name="read_perk_trip",
            description="Get full details of a specific trip by ID, including locations and booker.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {"trip_id": {"type": "string", "description": "Trip ID"}},
                "required": ["trip_id"],
            },
            source="integration:perk",
        ),
        # ── Bookings ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_perk_bookings",
            priority=0,
            description=(
                "List bookings (flights, hotels, cars, trains) from Perk. "
                "Supports filtering by trip_id, status, type, and modified date range."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max bookings to return (default 20)",
                        "default": 20,
                    },
                    "trip_id": {
                        "type": "string",
                        "description": "Filter by trip ID (comma-separated for multiple)",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by booking status",
                        "default": "",
                    },
                    "booking_type": {
                        "type": "string",
                        "description": "Filter by type (flight, hotel, car, train)",
                        "default": "",
                    },
                    "modified_gte": {
                        "type": "string",
                        "description": "Modified on or after this date (ISO 8601)",
                        "default": "",
                    },
                    "modified_lt": {
                        "type": "string",
                        "description": "Modified before this date (ISO 8601)",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:perk",
        ),
        # ── Invoices ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_perk_invoices",
            priority=0,
            description=(
                "List invoices from Perk with amounts, currency, status, dates and PDF link. "
                "Supports filtering by status (paid/unpaid), billing period, issuing/due dates, and serial number."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max invoices to return (default 20)",
                        "default": 20,
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status: paid or unpaid",
                        "default": "",
                    },
                    "billing_period": {
                        "type": "string",
                        "description": "Filter: instant, weekly, biweekly, monthly",
                        "default": "",
                    },
                    "issuing_date_gte": {
                        "type": "string",
                        "description": "Issued on or after (YYYY-MM-DD)",
                        "default": "",
                    },
                    "issuing_date_lte": {
                        "type": "string",
                        "description": "Issued on or before (YYYY-MM-DD)",
                        "default": "",
                    },
                    "due_date_gte": {
                        "type": "string",
                        "description": "Due on or after (YYYY-MM-DD)",
                        "default": "",
                    },
                    "due_date_lte": {
                        "type": "string",
                        "description": "Due on or before (YYYY-MM-DD)",
                        "default": "",
                    },
                    "serial_number_contains": {
                        "type": "string",
                        "description": "Partial match on serial number",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:perk",
        ),
        ToolDefinition(
            name="read_perk_invoice",
            description="Retrieve a single invoice by ID with full line-item details and its PDF link.",
            input_schema={
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "string", "description": "Invoice ID"}
                },
                "required": ["invoice_id"],
            },
            source="integration:perk",
            priority=1,
        ),
        ToolDefinition(
            name="download_perk_invoice_pdf",
            workspace_ingest=True,
            description=(
                "Download an invoice's PDF into the conversation: a download card, its link and "
                "a file_id to forward to any tool that takes a file; the PDF's text comes with "
                "the result. Include the returned [[FILE:...]] marker verbatim so the user gets "
                "a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "serial_number": {
                        "type": "string",
                        "description": "Invoice serial number (e.g. INV-01-987654), from list_perk_invoices",
                    }
                },
                "required": ["serial_number"],
            },
            source="integration:perk",
            priority=1,
        ),
        ToolDefinition(
            name="list_perk_invoice_lines",
            description=(
                "List invoice line items (individual charges) from Perk. "
                "Supports filtering by status, serial number, and issuing date range."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return (default 20)",
                        "default": 20,
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status: paid or unpaid",
                        "default": "",
                    },
                    "serial_number_contains": {
                        "type": "string",
                        "description": "Partial match on invoice serial",
                        "default": "",
                    },
                    "issuing_date_gte": {
                        "type": "string",
                        "description": "Issued on or after (YYYY-MM-DD)",
                        "default": "",
                    },
                    "issuing_date_lte": {
                        "type": "string",
                        "description": "Issued on or before (YYYY-MM-DD)",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:perk",
            priority=1,
        ),
        ToolDefinition(
            name="list_perk_invoice_profiles",
            description="List invoice profiles (billing entities) for the Perk account.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max profiles to return (default 20)",
                        "default": 20,
                    },
                    "exclude_personal": {
                        "type": "boolean",
                        "description": "Exclude personal invoice profiles",
                        "default": False,
                    },
                },
                "required": [],
            },
            source="integration:perk",
            priority=1,
        ),
        # ── Suppliers ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_perk_suppliers",
            description="List suppliers related to your Perk bookings (airlines, hotels, etc.).",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max suppliers to return (default 20)",
                        "default": 20,
                    },
                    "booking_id": {
                        "type": "string",
                        "description": "Filter by booking ID(s), comma-separated",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:perk",
        ),
        # ── Users ──────────────────────────────────────────────────────
        ToolDefinition(
            name="list_perk_users",
            description="List users (travelers/employees) on the Perk account with names, emails, and phone numbers.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max users to return (default 50)",
                        "default": 50,
                    },
                    "trip_id": {
                        "type": "string",
                        "description": "Filter by trip ID (comma-separated)",
                        "default": "",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Find a specific user by ID",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:perk",
            priority=1,
        ),
        # ── Cost Centers ───────────────────────────────────────────────
        ToolDefinition(
            name="list_perk_cost_centers",
            description=(
                "List the cost centers of the Perk account: ID, name, ERP code, "
                "number of users, archived state. Paginated with limit / offset."
            ),
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Cost centers per page (1-100, default 50)",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "How many cost centers to skip (default 0)",
                        "default": 0,
                    },
                },
                "required": [],
            },
            source="integration:perk",
        ),
        ToolDefinition(
            name="read_perk_cost_center",
            description=(
                "Get a cost center by ID: its users, approver, delegate, additional "
                "approvers, companies and ERP code."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cost_center_id": {
                        "type": "string",
                        "description": "Cost center ID",
                    }
                },
                "required": ["cost_center_id"],
            },
            source="integration:perk",
            priority=1,
        ),
        # ── Webhooks ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_perk_webhooks",
            description="List all webhook subscriptions configured on the Perk account.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:perk",
            priority=1,
        ),
        ToolDefinition(
            name="list_perk_webhook_events",
            description="List all available Perk webhook event types you can subscribe to.",
            priority=2,
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:perk",
        ),
        ToolDefinition(
            name="create_perk_webhook",
            description=(
                "Create a webhook subscription on the Perk account. "
                "Provide a name, target URL, shared secret, and comma-separated event names."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name for the webhook subscription",
                    },
                    "url": {
                        "type": "string",
                        "description": "Target URL to receive event POSTs",
                    },
                    "secret": {
                        "type": "string",
                        "description": "Shared secret for payload signing (Tk-Signature header)",
                    },
                    "events": {
                        "type": "string",
                        "description": "Comma-separated list of event names to subscribe to",
                    },
                },
                "required": ["name", "url", "secret", "events"],
            },
            source="integration:perk",
            priority=1,
        ),
        ToolDefinition(
            name="delete_perk_webhook",
            description="Delete a webhook subscription by ID from the Perk account.",
            input_schema={
                "type": "object",
                "properties": {
                    "webhook_id": {
                        "type": "string",
                        "description": "Webhook subscription ID to delete",
                    }
                },
                "required": ["webhook_id"],
            },
            source="integration:perk",
            priority=1,
        ),
        # ── Custom Fields ──────────────────────────────────────────────
        ToolDefinition(
            name="list_perk_custom_fields",
            description=(
                "Get the custom field values of a trip (project codes, events, "
                "client names...). Perk exposes custom fields per trip only."
            ),
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "trip_id": {
                        "type": "string",
                        "description": "Trip ID (from list_perk_trips)",
                    }
                },
                "required": ["trip_id"],
            },
            source="integration:perk",
        ),
    ]
