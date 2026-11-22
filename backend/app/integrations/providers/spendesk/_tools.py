# SPDX-License-Identifier: Apache-2.0

"""One tool per Spendesk Public API operation, built from ``operations.json``.

``operations.json`` is the operation table of the provider: one entry per
documented operation of the Spendesk Public API (read from its OpenAPI spec,
``developer.spendesk.com/openapi/spendesk-public-api.json``; the auth, OAuth
and MCP endpoints are left to the client), with its tool name, section,
method, path, parameters, request body, response kind, scopes, entity mode
and the ``read`` flag. Each entry becomes one tool:

* path and query parameters are tool arguments (path ones required);
* ``company_id`` (the entity) is an argument of every tool but the companies
  list: sent as ``X-Company-Id`` on v1 and in the path on v2; required by an
  organisation-level credential, ignored by a company-level one;
* a JSON object body is spread into arguments, any other body is one
  ``body`` argument;
* a page-numbered list answers one page and its total, or reads every page
  with ``all_pages``; a cursor list answers its next cursor;
* a CSV journal is delivered as a file of the run, never inlined.

Read or write follows the HTTP method: a GET, and a POST whose every scope is
``:read`` (a search, a snapshot), is a read and declared ``read_only``; every
other operation is a write, named with a write verb, marked sensitive and
asks a person on every call (finance guardrail as call-time approval, never a
missing tool). Bank details (IBAN, BIC, account and routing numbers) are
masked in every answer to their last four characters.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path
from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import deliver_file, fetch_file, file_content_type
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._client import PROVIDER, SOURCE, SpendeskClient, seg

#: The longest text a tool answers; the rest is cut with a note.
MAX_ANSWER_CHARS = 24_000
#: Pages read at most by ``all_pages`` on one call.
MAX_PAGES = 20
#: Largest file a download tool fetches.
MAX_FILE_BYTES = 25 * 1024 * 1024
#: The group of the tools that read or write bank details.
BANK_DETAILS_GROUP = "bank_details"
#: Keys whose value is never answered, whatever the endpoint returns.
_SECRET_KEYS = frozenset(
    {"secret", "clientsecret", "token", "accesstoken", "refreshtoken", "apikey", "password", "signingsecret"}
)
#: Keys whose value is a bank detail: answered masked to its last four characters.
_BANK_KEYS = frozenset(
    {
        "iban",
        "bic",
        "swift",
        "swiftcode",
        "accountnumber",
        "bankaccountnumber",
        "routingnumber",
        "sortcode",
        "bankcode",
        "branchcode",
        "accountholdername",
    }
)
REDACTED = "[redacted]"
_COMPANY_ARG = "company_id"
_COMPANY_SCHEMA = {
    "type": "string",
    "description": (
        "The Spendesk entity (company id) to act on, from list_spendesk_companies. Required with an "
        "organisation-level credential; a company-level credential acts on its own company."
    ),
}

_OPERATIONS_FILE = Path(__file__).with_name("operations.json")


@cache
def operations() -> tuple[dict[str, Any], ...]:
    """The operation table, read once."""
    with _OPERATIONS_FILE.open(encoding="utf-8") as handle:
        return tuple(json.load(handle)["operations"])


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def mask(value: Any) -> Any:
    """A bank detail kept to its last four characters."""
    if not isinstance(value, str) or not value:
        return value
    text = value.replace(" ", "")
    return "****" + text[-4:] if len(text) > 4 else "****"


def redact(data: Any) -> Any:
    """*data* with every secret dropped and every bank detail masked."""
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for key, value in data.items():
            norm = _norm(key)
            if norm in _SECRET_KEYS:
                out[key] = REDACTED
            elif norm in _BANK_KEYS and not isinstance(value, (dict, list)):
                out[key] = mask(value)
            else:
                out[key] = redact(value)
        return out
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data


def render(title: str, data: Any) -> str:
    """A tool answer: a title line, then the redacted data as compact JSON, bounded."""
    if data is None:
        return title
    body = json.dumps(redact(data), ensure_ascii=False, default=str, separators=(",", ":"))
    if len(body) > MAX_ANSWER_CHARS:
        body = (
            body[:MAX_ANSWER_CHARS]
            + f"... [cut at {MAX_ANSWER_CHARS} of {len(body)} characters: narrow the call with its filters "
            "or page / pageSize]"
        )
    return f"{title}\n{body}"


def is_write(op: dict[str, Any]) -> bool:
    return not op.get("read")


DETAIL_LIMIT = 300
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_DASH = re.compile(r"\s*[\u2014\u2013]\s*")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
# A bare list number ("3.") ends with a period but is not a sentence end.
_LIST_NUMBER = re.compile(r"(?:^|\s)\d+\.$")


def _plain(text: str) -> str:
    """Vendor prose as plain text: Markdown links reduced to their text, no backticks, no long dashes."""
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = text.replace("`", "").replace("**", "")
    text = _DASH.sub(", ", text)
    return re.sub(r"\s+", " ", text).strip()


def _detail(op: dict[str, Any]) -> str:
    """The vendor detail, plain, cut at the last sentence end within DETAIL_LIMIT (a partial sentence is dropped)."""
    detail = _plain(str(op.get("description") or ""))
    if len(detail) <= DETAIL_LIMIT:
        return detail
    ends = [
        m.end() for m in _SENTENCE_END.finditer(detail, 0, DETAIL_LIMIT) if not _LIST_NUMBER.search(detail[: m.end()])
    ]
    return detail[: ends[-1]] if ends else ""


def _description(op: dict[str, Any]) -> str:
    """The tool description: the operation's own words, then how to use it."""
    summary = _plain(str(op["summary"])).rstrip(".")
    detail = _detail(op)
    text = summary + "."
    if detail and detail.rstrip(".").lower() != summary.lower():
        text += " " + detail.rstrip(".") + "."
    if _page_param(op):
        text += " Paged (page, pageSize); all_pages reads up to 20 pages."
    elif _cursor_param(op):
        text += " Cursor paged: pass the next cursor the answer names."
    if op["response"] == "csv":
        text += " A CSV answer is delivered as a file."
    if op.get("group") == BANK_DETAILS_GROUP:
        text += " Bank details group: IBAN, BIC and account numbers are masked."
    if is_write(op):
        text += " Sensitive write: asks a person before it runs."
    if op.get("scopes"):
        text += f" Scope: {', '.join(op['scopes'])}."
    return text


def _page_param(op: dict[str, Any]) -> bool:
    return any(p["name"] == "page" for p in op["params"])


def _cursor_param(op: dict[str, Any]) -> bool:
    names = {p["name"] for p in op["params"]}
    body = op.get("body") or {}
    fields = set((body.get("schema") or {}).get("properties") or {}) if body.get("mode") == "fields" else set()
    return "cursor" in names or "cursor" in fields


def _schema(op: dict[str, Any]) -> tuple[dict[str, Any], list[str], set[str]]:
    """``(properties, required, body field names)`` of the tool's input."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    if op["entity"] != "none":
        properties[_COMPANY_ARG] = dict(_COMPANY_SCHEMA)
        if op["entity"] == "path":
            required.append(_COMPANY_ARG)
    for param in op["params"]:
        properties[param["name"]] = param["schema"]
        if param["required"]:
            required.append(param["name"])
    body_fields: set[str] = set()
    body = op.get("body")
    if body and body["mode"] == "fields":
        schema = body["schema"]
        for name, field_schema in (schema.get("properties") or {}).items():
            if name in properties:
                continue
            properties[name] = field_schema or {"type": "string"}
            body_fields.add(name)
        for name in schema.get("required") or []:
            if name in body_fields:
                required.append(name)
    elif body:
        properties["body"] = {
            **(body["schema"] or {"type": "object"}),
            "description": ((body["schema"] or {}).get("description") or "The request body, as the API takes it."),
        }
        required.append("body")
    if _page_param(op):
        properties["all_pages"] = {
            "type": "boolean",
            "description": f"Read every page (up to {MAX_PAGES}) and answer them together.",
        }
    if is_write(op):
        properties["idempotency_key"] = {
            "type": "string",
            "description": "Optional Idempotency-Key; one is generated when omitted, so a retry never applies twice.",
        }
    return properties, required, body_fields


def _rows(data: Any) -> list[Any]:
    if isinstance(data, dict):
        for key in ("data", "payables"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    return data if isinstance(data, list) else [data]


def _paging_note(data: Any) -> str:
    """``(page 2 of 7, 190 rows)`` or ``(next cursor X)``, from the answer's meta."""
    if not isinstance(data, dict):
        return ""
    cursor = data.get("nextCursor")
    meta_value = data.get("meta")
    meta: dict[str, Any] = meta_value if isinstance(meta_value, dict) else {}
    pagination_value = meta.get("pagination")
    pagination: dict[str, Any] = pagination_value if isinstance(pagination_value, dict) else {}
    cursor = cursor or pagination.get("nextCursor")
    if cursor:
        return f" (next cursor {cursor})"
    page, size, total = pagination.get("page"), pagination.get("pageSize"), pagination.get("total")
    if isinstance(page, int) and isinstance(size, int) and isinstance(total, int) and size > 0:
        pages = max(1, -(-total // size))
        return f" (page {page} of {pages}, {total} rows)"
    return ""


def _make_handler(
    client: SpendeskClient,
    op: dict[str, Any],
    body_fields: set[str],
    persist_file_fn: Any,
) -> Callable[..., Awaitable[str | ToolResult]]:
    path_params = [p["name"] for p in op["params"] if p["in"] == "path"]
    query_params = [p["name"] for p in op["params"] if p["in"] == "query"]
    body = op.get("body")
    action = op["summary"][0].lower() + op["summary"][1:] if op["summary"] else op["name"]

    async def handler(**kwargs: Any) -> str | ToolResult:
        company = str(kwargs.get(_COMPANY_ARG) or "").strip() or None
        path = op["path"]
        if op["entity"] == "path":
            if not company:
                return f"Error: {_COMPANY_ARG} is required (list_spendesk_companies names the entities)."
            path = path.replace("{companyId}", seg(company))
        for name in path_params:
            value = kwargs.get(name)
            if value is None or str(value).strip() == "":
                return f"Error: {name} is required."
            path = path.replace("{" + name + "}", seg(value))
        query = {name: kwargs[name] for name in query_params if name in kwargs}
        payload: Any = None
        if body and body["mode"] == "fields":
            payload = {name: kwargs[name] for name in body_fields if name in kwargs}
        elif body:
            payload = kwargs.get("body")
        header_company = company if op["entity"] == "header" else None
        if op["response"] == "csv":
            return await _export(client, op, path, query, header_company, action, persist_file_fn)
        if _page_param(op) and kwargs.get("all_pages"):
            return await _all_pages(client, op, path, query, header_company, action)
        resp, err = await client.request(
            op["method"],
            path,
            action,
            params=query,
            body=payload,
            company_id=header_company,
            idempotency_key=kwargs.get("idempotency_key") if is_write(op) else None,
        )
        if err or resp is None:
            return err or f"Error: {PROVIDER} answered nothing while trying to {action}."
        title = f"{PROVIDER}: {op['summary']}{_paging_note(resp.data)}"
        if resp.data is None and resp.content:
            return f"{title}\n{resp.content[:MAX_ANSWER_CHARS].decode('utf-8', 'replace')}"
        if resp.data is None and op["method"] != "GET":
            return f"{title}: done."
        return render(title, resp.data)

    return handler


async def _all_pages(
    client: SpendeskClient,
    op: dict[str, Any],
    path: str,
    query: dict[str, Any],
    company: str | None,
    action: str,
) -> str:
    rows: list[Any] = []
    page = int(query.get("page") or 1)
    size = int(query.get("pageSize") or 30)
    more = False
    for _ in range(MAX_PAGES):
        resp, err = await client.request(
            "GET", path, f"{action} (page {page})", params={**query, "page": page, "pageSize": size}, company_id=company
        )
        if err or resp is None:
            if rows:
                return render(f"{PROVIDER}: {op['summary']} ({len(rows)} rows read, then: {err})", rows)
            return err or f"Error: {PROVIDER} answered nothing while trying to {action}."
        batch = _rows(resp.data)
        rows.extend(batch)
        meta = (resp.data or {}).get("meta") if isinstance(resp.data, dict) else None
        total = ((meta or {}).get("pagination") or {}).get("total") if isinstance(meta, dict) else None
        more = len(rows) < total if isinstance(total, int) else len(batch) >= size
        if not more or not batch:
            break
        page += 1
    note = f"; more from page {page}" if more else ""
    return render(f"{PROVIDER}: {op['summary']} ({len(rows)} rows{note})", rows)


async def _export(
    client: SpendeskClient,
    op: dict[str, Any],
    path: str,
    query: dict[str, Any],
    company: str | None,
    action: str,
    persist_file_fn: Any,
) -> str | ToolResult:
    resp, err = await client.request(
        op["method"], path, action, params=query, company_id=company, accept="text/csv, application/json"
    )
    if err or resp is None:
        return err or f"Error: {PROVIDER} answered nothing while trying to {action}."
    if resp.data is not None:
        # Still being built, or an error body: answered as data.
        return render(f"{PROVIDER}: {op['summary']}", resp.data)
    return await deliver_file(
        data=resp.content,
        filename=f"spendesk-{op['name'].split('_spendesk_', 1)[-1].replace('_', '-')}.csv",
        content_type="text/csv",
        source=PROVIDER,
        persist_file_fn=persist_file_fn,
        details=[f"{op['summary']} from Spendesk."],
    )


def _download_tools(
    client: SpendeskClient, executor: CallableToolExecutor, persist_file_fn: Any
) -> list[ToolDefinition]:
    """Receipts, invoices and purchase order documents, downloaded into the conversation.

    Spendesk answers a short-lived signed URL; the file is fetched without
    the bearer (``fetch_file`` drops headers across origins) and delivered.
    """

    async def _deliver(url: str, name: str, what: str) -> str | ToolResult:
        async with egress_client(timeout=60) as storage:
            fetched = await fetch_file(
                storage,
                "get",
                url,
                provider=PROVIDER,
                action=f"download {what}",
                max_bytes=MAX_FILE_BYTES,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return fetched
        filename = fetched.filename or name
        return await deliver_file(
            data=fetched.data,
            filename=filename,
            content_type=file_content_type(fetched.content_type, filename),
            source=PROVIDER,
            persist_file_fn=persist_file_fn,
            details=[f"{what.capitalize()} from Spendesk; the source link is signed and short-lived."],
        )

    async def download_spendesk_payable_attachment(
        payable_id: str, company_id: str = "", index: int = 0
    ) -> str | ToolResult:
        resp, err = await client.request(
            "GET",
            f"/v1/payables/{seg(payable_id)}/attachments",
            "list the payable's attachments",
            company_id=company_id or None,
        )
        if err or resp is None:
            return err or f"Error: {PROVIDER} answered nothing."
        files = _rows(resp.data)
        if not files or not isinstance(files[0], dict):
            return f"Error: payable {payable_id} has no receipt or invoice attached (a missing receipt)."
        if index < 0 or index >= len(files):
            return f"Error: index {index} is out of range: payable {payable_id} has {len(files)} attachment(s)."
        url = str(files[index].get("url") or "")
        if not url:
            return f"Error: {PROVIDER} returned no download URL for that attachment."
        return await _deliver(url, f"spendesk-payable-{payable_id}-{index}", f"the receipt of payable {payable_id}")

    async def download_spendesk_purchase_order_document(
        purchase_order_id: str, company_id: str = ""
    ) -> str | ToolResult:
        resp, err = await client.request(
            "GET",
            f"/v1/purchase-orders/{seg(purchase_order_id)}/document",
            "read the purchase order document",
            company_id=company_id or None,
        )
        if err or resp is None:
            return err or f"Error: {PROVIDER} answered nothing."
        doc = (resp.data or {}).get("data") if isinstance(resp.data, dict) else None
        url = str((doc or {}).get("url") or "")
        if not url:
            return f"Error: {PROVIDER} returned no document for purchase order {purchase_order_id}."
        return await _deliver(url, f"spendesk-po-{purchase_order_id}.pdf", f"purchase order {purchase_order_id}")

    entity = {_COMPANY_ARG: dict(_COMPANY_SCHEMA)}
    defs: list[tuple[str, Callable[..., Awaitable[str | ToolResult]], str, dict[str, Any], list[str]]] = [
        (
            "download_spendesk_payable_attachment",
            download_spendesk_payable_attachment,
            "Download a receipt or invoice attached to a payable (expense, card payment, invoice) into the "
            "conversation. A payable with no attachment is a missing receipt. Scope: payable:read.",
            {
                "payable_id": {"type": "string", "description": "The payable id."},
                "index": {"type": "integer", "description": "Which attachment, from 0 (default the first)."},
                **entity,
            },
            ["payable_id"],
        ),
        (
            "download_spendesk_purchase_order_document",
            download_spendesk_purchase_order_document,
            "Download a purchase order's PDF document into the conversation. Scope: experimental:purchase-order:read.",
            {"purchase_order_id": {"type": "string", "description": "The purchase order id."}, **entity},
            ["purchase_order_id"],
        ),
    ]
    tools = []
    for name, fn, description, props, required in defs:
        executor.register(name, fn)
        tools.append(
            ToolDefinition(
                name=name,
                description=description,
                input_schema={
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
                source=SOURCE,
                priority=1,
                read_only=True,
            )
        )
    return tools


def build_operation_tools(
    client: SpendeskClient,
    executor: CallableToolExecutor,
    *,
    persist_file_fn: Any = None,
) -> list[ToolDefinition]:
    """A tool for every operation of the table and the download tools, their handlers registered."""
    tools: list[ToolDefinition] = []
    for op in operations():
        properties, required, body_fields = _schema(op)
        schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            schema["required"] = required
        executor.register(op["name"], _make_handler(client, op, body_fields, persist_file_fn))
        tools.append(
            ToolDefinition(
                name=op["name"],
                description=_description(op),
                input_schema=schema,
                source=SOURCE,
                priority=_priority(op),
                always_ask=is_write(op),
                read_only=not is_write(op),
            )
        )
    tools.extend(_download_tools(client, executor, persist_file_fn))
    return tools


def bank_details_tools() -> frozenset[str]:
    """The tools of the ``bank_details`` group."""
    return frozenset(op["name"] for op in operations() if op.get("group") == BANK_DETAILS_GROUP)


#: Operations a finance agent reaches first: kept whatever the tool budget.
_ESSENTIAL = frozenset(
    {
        "list_spendesk_companies",
        "get_spendesk_wallet_summary",
        "list_spendesk_transactions",
        "search_spendesk_payables",
        "get_spendesk_payable",
        "list_spendesk_payable_attachments",
        "download_spendesk_payable_attachment",
        "list_spendesk_requests",
        "list_spendesk_settlements",
        "list_spendesk_cards",
        "list_spendesk_users",
        "list_spendesk_suppliers",
    }
)


def _priority(op: dict[str, Any]) -> int:
    if op["name"] in _ESSENTIAL:
        return 0
    if is_write(op):
        return 2
    return 1
