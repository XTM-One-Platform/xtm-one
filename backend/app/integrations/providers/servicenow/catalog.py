"""ServiceNow Service Catalog (the ``/api/sn_sc/servicecatalog`` API + request tables).

Browse catalog items, discover the variables an item needs, order an item
(order_now), and track the resulting requests (sc_request) and requested items
(sc_req_item).
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _SOURCE,
    build_encoded_query,
    clamp,
    disp,
    format_entity_list,
    is_sys_id,
    list_table,
    record_url,
    result_of,
    sc_url,
    sn_request,
    truncate,
)


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_servicenow_catalog_items(
        text: str = "", limit: int = 20, offset: int = 0
    ) -> str:
        """Browse/search orderable Service Catalog items; returns their sys_ids.

        ``text`` searches item names/descriptions. Use the returned item sys_id
        with get_servicenow_catalog_item_variables and order_servicenow_catalog_item.
        """
        params: dict[str, Any] = {
            "sysparm_limit": clamp(limit),
            "sysparm_offset": max(0, int(offset or 0)),
        }
        if text and text.strip():
            params["sysparm_text"] = text.strip()
        resp, err = await sn_request(
            "get",
            sc_url(base, "items"),
            "list catalog items",
            headers=headers,
            params=params,
        )
        if err:
            return err
        items = result_of(resp)
        items = items if isinstance(items, list) else []

        def _fmt(it: dict[str, Any]) -> str:
            return (
                f"  {disp(it.get('name')) or '?'}\n"
                f"    {truncate(disp(it.get('short_description')), 120) or 'no description'}\n"
                f"    Category: {disp((it.get('category') or {}).get('title') if isinstance(it.get('category'), dict) else it.get('category')) or 'n/a'} | "
                f"sys_id: {disp(it.get('sys_id'))}"
            )

        return format_entity_list(items, _fmt, noun="catalog item")

    async def get_servicenow_catalog_item_variables(item_sys_id: str) -> str:
        """List the variables (form fields) a catalog item needs to be ordered.

        Returns each variable's name (the key to use in order_servicenow_catalog_item's
        ``variables``), label, type, and whether it is mandatory.
        """
        item_sys_id = (item_sys_id or "").strip()
        if not item_sys_id:
            return "Error: item_sys_id is required (use list_servicenow_catalog_items)."
        resp, err = await sn_request(
            "get",
            sc_url(base, f"items/{item_sys_id}/variables"),
            f"get catalog item {item_sys_id} variables",
            headers=headers,
        )
        if err:
            return err
        variables = result_of(resp)
        variables = variables if isinstance(variables, list) else []

        def _fmt(v: dict[str, Any]) -> str:
            mandatory = disp(v.get("mandatory")) in ("true", "True", "1")
            return (
                f"  {disp(v.get('name')) or '?'} ({disp(v.get('label'))})"
                f" - type {disp(v.get('type'))}"
                + (" [MANDATORY]" if mandatory else "")
            )

        return format_entity_list(variables, _fmt, noun="variable")

    async def order_servicenow_catalog_item(
        item_sys_id: str,
        quantity: int = 1,
        variables: dict[str, Any] | None = None,
        requested_for: str = "",
    ) -> str:
        """Order a Service Catalog item (order_now) and return the request number.

        ``variables`` maps each variable name (from
        get_servicenow_catalog_item_variables) to its value. ``requested_for`` is
        an optional user sys_id to order on behalf of (defaults to the
        integration user). This submits a real request - it is not a dry run.
        """
        item_sys_id = (item_sys_id or "").strip()
        if not item_sys_id:
            return "Error: item_sys_id is required (use list_servicenow_catalog_items)."
        body: dict[str, Any] = {
            "sysparm_quantity": str(max(1, int(quantity or 1))),
            "variables": variables or {},
        }
        if requested_for:
            body["sysparm_requested_for"] = requested_for
        resp, err = await sn_request(
            "post",
            sc_url(base, f"items/{item_sys_id}/order_now"),
            f"order catalog item {item_sys_id}",
            headers=headers,
            json=body,
        )
        if err:
            return err
        res = result_of(resp)
        if not isinstance(res, dict):
            return "Catalog item ordered."
        number = disp(res.get("request_number")) or disp(res.get("number"))
        req_id = disp(res.get("request_id")) or disp(res.get("sys_id"))
        link = record_url(base, "sc_request", req_id) if req_id else ""
        out = f"Ordered. Request {number or '(created)'}"
        if req_id:
            out += f" (sys_id: {req_id})"
        if link:
            out += f"\n  Link: {link}"
        return out

    async def list_servicenow_requests(
        query: str = "",
        active: bool | None = True,
        text: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List catalog requests (sc_request / REQ records), newest-first."""
        encoded = build_encoded_query(
            {"active": None if active is None else ("true" if active else "false")},
            raw_query=query,
            text=text,
            order_by="sys_updated_on",
        )

        def _fmt(rec: dict[str, Any]) -> str:
            return (
                f"  {disp(rec.get('number'))}: {truncate(disp(rec.get('short_description')), 90)}\n"
                f"    Stage: {disp(rec.get('request_state')) or disp(rec.get('stage')) or '?'} | "
                f"Approval: {disp(rec.get('approval')) or '?'} | "
                f"Requested for: {disp(rec.get('requested_for')) or '?'} | "
                f"sys_id: {disp(rec.get('sys_id'))}"
            )

        return await list_table(
            base,
            headers,
            "sc_request",
            "list requests",
            _fmt,
            noun="request",
            query=encoded,
            limit=limit,
            offset=offset,
            fields="number,short_description,request_state,stage,approval,requested_for,sys_updated_on,sys_id",
        )

    async def list_servicenow_request_items(
        request: str = "",
        query: str = "",
        active: bool | None = True,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List requested items (sc_req_item / RITM records).

        Filter to one parent request by its number (REQ...) or sys_id via
        ``request``, or pass a raw ``query``.
        """
        filters: dict[str, Any] = {
            "active": None if active is None else ("true" if active else "false")
        }
        if request and request.strip():
            req = request.strip()
            # RITMs link to their parent via the ``request`` reference; accept a
            # sys_id (match the reference directly) or a REQ number (dot-walk to
            # request.number).
            filters["request" if is_sys_id(req) else "request.number"] = req
        encoded = build_encoded_query(filters, raw_query=query, order_by="sys_updated_on")

        def _fmt(rec: dict[str, Any]) -> str:
            return (
                f"  {disp(rec.get('number'))}: {truncate(disp(rec.get('short_description')), 90)}\n"
                f"    State: {disp(rec.get('state')) or '?'} | "
                f"Stage: {disp(rec.get('stage')) or '?'} | "
                f"Item: {disp(rec.get('cat_item')) or '?'} | "
                f"sys_id: {disp(rec.get('sys_id'))}"
            )

        return await list_table(
            base,
            headers,
            "sc_req_item",
            "list request items",
            _fmt,
            noun="requested item",
            query=encoded,
            limit=limit,
            offset=offset,
            fields="number,short_description,state,stage,cat_item,request,sys_updated_on,sys_id",
        )

    executor.register("list_servicenow_catalog_items", list_servicenow_catalog_items)
    executor.register(
        "get_servicenow_catalog_item_variables", get_servicenow_catalog_item_variables
    )
    executor.register("order_servicenow_catalog_item", order_servicenow_catalog_item)
    executor.register("list_servicenow_requests", list_servicenow_requests)
    executor.register("list_servicenow_request_items", list_servicenow_request_items)

    _limit = {"type": "integer", "default": 20}
    _offset = {"type": "integer", "default": 0}
    return [
        ToolDefinition(
            name="list_servicenow_catalog_items",
            description=(
                "Browse/search orderable ServiceNow Service Catalog items and return "
                "their sys_ids. Pair with get_servicenow_catalog_item_variables + "
                "order_servicenow_catalog_item."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Search item name/description"},
                    "limit": _limit,
                    "offset": _offset,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_servicenow_catalog_item_variables",
            description=(
                "List the variables (form fields) a catalog item needs to be "
                "ordered - their names, labels, types, and mandatory flags. The "
                "variable names are the keys for order_servicenow_catalog_item."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_sys_id": {"type": "string", "description": "Catalog item sys_id"}
                },
                "required": ["item_sys_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="order_servicenow_catalog_item",
            description=(
                "Order a Service Catalog item (order_now) and return the created "
                "request number. variables maps variable names to values; "
                "requested_for is an optional user sys_id to order on behalf of. "
                "This submits a REAL request."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_sys_id": {"type": "string", "description": "Catalog item sys_id"},
                    "quantity": {"type": "integer", "default": 1},
                    "variables": {
                        "type": "object",
                        "description": "Map of variable name -> value",
                    },
                    "requested_for": {
                        "type": "string",
                        "description": "User sys_id to order for (optional)",
                    },
                },
                "required": ["item_sys_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_servicenow_requests",
            description=(
                "List Service Catalog requests (sc_request / REQ records), "
                "newest-first. active defaults to true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Raw encoded query"},
                    "active": {"type": "boolean", "default": True},
                    "text": {"type": "string", "description": "Match short description"},
                    "limit": _limit,
                    "offset": _offset,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_servicenow_request_items",
            description=(
                "List requested items (sc_req_item / RITM records). Filter to one "
                "parent request by number (REQ...) or sys_id via 'request', or use "
                "a raw query. active defaults to true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "description": "Parent request number (REQ...) or sys_id",
                    },
                    "query": {"type": "string", "description": "Raw encoded query"},
                    "active": {"type": "boolean", "default": True},
                    "limit": _limit,
                    "offset": _offset,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
