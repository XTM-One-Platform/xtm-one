# SPDX-License-Identifier: Apache-2.0

"""The HubSpot line-item tools: read a deal's line items, create one on a deal,
update or delete one.

A deal's ACV and TCV (``hs_acv``, ``hs_tcv``) are calculated by HubSpot from
its line items and cannot be written on the deal: correcting them means
correcting the line items. ``hubspot.build_tools`` builds one
:class:`HubSpotLineItems` per toolbox around its rate-limited request function
and adds each tool's scopes to its description.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx

from app.core.egress import egress_client
from app.tools.base import ToolDefinition

_BASE_URL = "https://api.hubapi.com"

#: The rate-limited request of ``hubspot.build_tools`` (``safe_request``'s contract).
Request = Callable[..., Awaitable[tuple[httpx.Response | None, str | None]]]

#: HubSpot-defined association type of a line item to its deal.
LINE_ITEM_TO_DEAL_TYPE_ID = 20
#: Line items per batch read (HubSpot's maximum), associations per page, pages read.
_BATCH = 100
_ASSOCIATION_PAGE = 500
_MAX_ASSOCIATION_PAGES = 4

#: What a line item is shown with: (property, label), after its id and name.
LINE_ITEM_FIELDS: tuple[tuple[str, str], ...] = (
    ("hs_product_id", "product"),
    ("hs_sku", "SKU"),
    ("price", "price"),
    ("quantity", "quantity"),
    ("discount", "discount"),
    ("hs_discount_percentage", "discount %"),
    ("amount", "amount"),
    ("hs_recurring_billing_period", "term"),
    ("recurringbillingfrequency", "billing frequency"),
    ("hs_recurring_billing_start_date", "billing start"),
    ("hs_acv", "ACV"),
    ("hs_tcv", "TCV"),
)
#: What the deal is shown with, above its line items.
_DEAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("amount", "amount"),
    ("hs_acv", "ACV"),
    ("hs_tcv", "TCV"),
    ("hs_arr", "ARR"),
    ("deal_currency_code", "currency"),
)
#: The line-item values summed under the list, to compare with the deal.
_SUMMED = (("amount", "amount"), ("hs_acv", "ACV"), ("hs_tcv", "TCV"))


def _body(resp: httpx.Response | None) -> dict[str, Any]:
    if resp is None:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _json_object(text: str, argument: str) -> tuple[dict[str, Any], str | None]:
    """The JSON object *text* holds (empty for no text), or an error naming *argument*."""
    if not str(text or "").strip():
        return {}, None
    try:
        value = json.loads(text)
    except ValueError:
        return {}, f"Error: {argument} is not valid JSON."
    if not isinstance(value, dict):
        return {}, f"Error: {argument} must be a JSON object of property names to values."
    return value, None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _shown_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_line_item(item: Mapping[str, Any], extra: Sequence[str] = ()) -> str:
    """One line item on one line: id, name, then every field it has a value for."""
    props = item.get("properties") or {}
    parts = [f"{item.get('id', '?')} {props.get('name') or '(no name)'}"]
    for name, label in (*LINE_ITEM_FIELDS, *((e, e) for e in extra)):
        value = props.get(name)
        if value not in (None, ""):
            parts.append(f"{label} {value}")
    return "  - " + " | ".join(parts)


def line_items_sum(items: Sequence[Mapping[str, Any]]) -> str:
    """The sum of the line items' amount, ACV and TCV, the values HubSpot rolls up to the deal."""
    sums = []
    for name, label in _SUMMED:
        values = [_number((i.get("properties") or {}).get(name)) for i in items]
        known = [v for v in values if v is not None]
        if known:
            sums.append(f"{label} {_shown_number(sum(known))}")
    return "Sum of the line items: " + (", ".join(sums) if sums else "no amount, ACV or TCV set")


class HubSpotLineItems:
    """The line-item tools of one toolbox."""

    def __init__(self, request: Request, *, headers: Mapping[str, str]) -> None:
        self._request = request
        self._headers = dict(headers)

    async def _call(
        self, client: httpx.AsyncClient, method: str, path: str, action: str, **kwargs: Any
    ) -> tuple[httpx.Response | None, str | None]:
        return await self._request(
            client, method, f"{_BASE_URL}{path}", "HubSpot", action, headers=self._headers, **kwargs
        )

    async def _line_item_ids(self, client: httpx.AsyncClient, deal_id: str) -> tuple[list[str], str | None]:
        ids: list[str] = []
        after = ""
        for _ in range(_MAX_ASSOCIATION_PAGES):
            params = {"limit": str(_ASSOCIATION_PAGE)}
            if after:
                params["after"] = after
            resp, err = await self._call(
                client,
                "get",
                f"/crm/v4/objects/deals/{deal_id}/associations/line_items",
                f"list the line items of deal {deal_id}",
                params=params,
            )
            if err:
                return [], err
            data = _body(resp)
            ids.extend(str(r["toObjectId"]) for r in data.get("results", []) if r.get("toObjectId") is not None)
            after = str(((data.get("paging") or {}).get("next") or {}).get("after") or "")
            if not after:
                break
        return list(dict.fromkeys(ids)), None

    async def list_hubspot_line_items(self, deal_id: str, properties: str = "") -> str:
        """A deal's amount, ACV and TCV, then each of its line items and their sum."""
        deal_id = str(deal_id or "").strip()
        if not deal_id:
            return "Error: pass deal_id."
        extra = [p.strip() for p in str(properties or "").split(",") if p.strip()]
        names = list(dict.fromkeys(["name", *(n for n, _ in LINE_ITEM_FIELDS), *extra]))
        async with egress_client(timeout=30) as client:
            resp, err = await self._call(
                client,
                "get",
                f"/crm/v3/objects/deals/{deal_id}",
                f"get deal {deal_id}",
                params={"properties": ",".join(n for n, _ in _DEAL_FIELDS)},
            )
            if err:
                return err
            deal = _body(resp).get("properties") or {}
            ids, err = await self._line_item_ids(client, deal_id)
            if err:
                return err
            by_id: dict[str, Mapping[str, Any]] = {}
            for start in range(0, len(ids), _BATCH):
                resp, err = await self._call(
                    client,
                    "post",
                    "/crm/v3/objects/line_items/batch/read",
                    "read line items",
                    json={"properties": names, "inputs": [{"id": i} for i in ids[start : start + _BATCH]]},
                )
                if err:
                    return err
                for item in _body(resp).get("results", []):
                    if isinstance(item, dict) and item.get("id") is not None:
                        by_id[str(item["id"])] = item
        shown = [f"{label} {deal[name]}" for name, label in _DEAL_FIELDS if deal.get(name) not in (None, "")]
        lines = [f"Deal {deal_id}: " + (", ".join(shown) if shown else "no amount, ACV or TCV set")]
        items = [by_id[i] for i in ids if i in by_id]
        if not items:
            lines.append("No line items.")
            return "\n".join(lines)
        lines.append(f"{len(items)} line item(s):")
        lines.extend(format_line_item(item, extra) for item in items)
        lines.append(line_items_sum(items))
        if len(items) < len(ids):
            lines.append(f"({len(ids) - len(items)} associated line item(s) could not be read.)")
        return "\n".join(lines)

    async def create_hubspot_line_item(
        self,
        deal_id: str,
        product_id: str = "",
        name: str = "",
        price: str = "",
        quantity: str = "1",
        properties_json: str = "",
    ) -> str:
        """Create a line item (from a product, or a name and a price) on a deal."""
        deal_id = str(deal_id or "").strip()
        if not deal_id:
            return "Error: pass deal_id."
        props, err = _json_object(properties_json, "properties_json")
        if err:
            return err
        for key, value in (("hs_product_id", product_id), ("name", name), ("price", price), ("quantity", quantity)):
            text = str(value if value is not None else "").strip()
            if text:
                props.setdefault(key, text)
        if not props.get("hs_product_id") and not (props.get("name") and str(props.get("price", "")).strip()):
            return "Error: pass product_id, or name and price (a line item needs a product or its own name and price)."
        body = {
            "properties": props,
            "associations": [
                {
                    "to": {"id": deal_id},
                    "types": [
                        {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": LINE_ITEM_TO_DEAL_TYPE_ID}
                    ],
                }
            ],
        }
        async with egress_client(timeout=30) as client:
            resp, err = await self._call(
                client, "post", "/crm/v3/objects/line_items", f"create a line item on deal {deal_id}", json=body
            )
        if err:
            return err
        created = _body(resp)
        return (
            f"Created line item {created.get('id', '?')} on deal {deal_id}:\n{format_line_item(created)}\n"
            "HubSpot recalculates the deal's ACV and TCV from its line items: list_hubspot_line_items reads them."
        )

    async def update_hubspot_line_item(self, line_item_id: str, properties_json: str) -> str:
        """Update a line item's properties."""
        line_item_id = str(line_item_id or "").strip()
        if not line_item_id:
            return "Error: pass line_item_id."
        props, err = _json_object(properties_json, "properties_json")
        if err:
            return err
        if not props:
            return "Error: pass properties_json with at least one property to change."
        async with egress_client(timeout=30) as client:
            resp, err = await self._call(
                client,
                "patch",
                f"/crm/v3/objects/line_items/{line_item_id}",
                f"update line item {line_item_id}",
                json={"properties": props},
            )
        if err:
            return err
        updated = _body(resp)
        updated.setdefault("id", line_item_id)
        return (
            f"Line item {line_item_id} updated:\n{format_line_item(updated)}\n"
            "HubSpot recalculates the deal's ACV and TCV from its line items: list_hubspot_line_items reads them."
        )

    async def delete_hubspot_line_item(self, line_item_id: str) -> str:
        """Archive (delete) a line item; it leaves its deal."""
        line_item_id = str(line_item_id or "").strip()
        if not line_item_id:
            return "Error: pass line_item_id."
        async with egress_client(timeout=30) as client:
            _, err = await self._call(
                client, "delete", f"/crm/v3/objects/line_items/{line_item_id}", f"delete line item {line_item_id}"
            )
        if err:
            return err
        return f"Line item {line_item_id} deleted (archived); its deal's ACV and TCV are recalculated without it."

    def handlers(self) -> dict[str, Callable[..., Awaitable[Any]]]:
        return {
            "list_hubspot_line_items": self.list_hubspot_line_items,
            "create_hubspot_line_item": self.create_hubspot_line_item,
            "update_hubspot_line_item": self.update_hubspot_line_item,
            "delete_hubspot_line_item": self.delete_hubspot_line_item,
        }


_TERM_HELP = (
    "hs_recurring_billing_period is the term as an ISO-8601 period (P12M for twelve months), "
    "recurringbillingfrequency the billing frequency (monthly, quarterly, annually...), "
    "hs_discount_percentage or discount the discount"
)


def line_item_definitions(source: str) -> list[ToolDefinition]:
    """The line-item tools' definitions (``hubspot.build_tools`` adds their scopes)."""
    return [
        ToolDefinition(
            name="list_hubspot_line_items",
            description=(
                "A HubSpot deal's line items: the deal's amount, ACV, TCV and currency, then each line item (id, name, "
                "product, price, quantity, discount, amount, term, billing frequency and start, ACV, TCV) and their sum. "
                "A deal's ACV and TCV are calculated from its line items: read them here before and after a change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string", "description": "HubSpot deal ID"},
                    "properties": {
                        "type": "string",
                        "description": "Extra line-item properties to show, comma-separated",
                        "default": "",
                    },
                },
                "required": ["deal_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="create_hubspot_line_item",
            description=(
                "Create a line item on a HubSpot deal, from a product (product_id) or with its own name and price. "
                f"properties_json sets any other line-item property: {_TERM_HELP}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string", "description": "Deal the line item is added to"},
                    "product_id": {"type": "string", "description": "Product library ID", "default": ""},
                    "name": {"type": "string", "description": "Line item name (without a product)", "default": ""},
                    "price": {"type": "string", "description": "Unit price", "default": ""},
                    "quantity": {"type": "string", "description": "Quantity", "default": "1"},
                    "properties_json": {
                        "type": "string",
                        "description": 'JSON object of other properties, e.g. {"hs_recurring_billing_period": "P12M"}',
                        "default": "",
                    },
                },
                "required": ["deal_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="update_hubspot_line_item",
            description=f"Update properties of a HubSpot line item (price, quantity, name, term...): {_TERM_HELP}.",
            input_schema={
                "type": "object",
                "properties": {
                    "line_item_id": {"type": "string", "description": "Line item ID (list_hubspot_line_items)"},
                    "properties_json": {
                        "type": "string",
                        "description": 'JSON object of properties to change, e.g. {"price": "1000", "hs_recurring_billing_period": "P12M"}',
                    },
                },
                "required": ["line_item_id", "properties_json"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="delete_hubspot_line_item",
            description="Delete (archive) a HubSpot line item; it leaves its deal, whose ACV and TCV are recalculated.",
            input_schema={
                "type": "object",
                "properties": {"line_item_id": {"type": "string", "description": "Line item ID"}},
                "required": ["line_item_id"],
            },
            source=source,
            priority=1,
        ),
    ]
