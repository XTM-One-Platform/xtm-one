# SPDX-License-Identifier: Apache-2.0

"""One tool per Aikido public API operation, built from ``operations.json``.

``operations.json`` is the curated operation table of the provider: one entry
per documented operation of the Aikido public REST API (read from each
operation's OpenAPI 3.1 definition on apidocs.aikido.dev), with its tool
name, section, method, path, parameters, request body, response kind and the
destructive flag. Each entry becomes one tool:

* path and query parameters are tool arguments (path ones required);
* a JSON object body is spread into arguments (a field named like a path or
  query parameter keeps the parameter); any other body is one ``body``
  argument;
* a paginated list answers one page and says whether another follows
  (``X-Has-Next-Page``), or reads every page with ``all_pages``;
* a CSV or PDF export is delivered as a file of the run, never inlined.

Reads and writes are told apart by the tool name's leading verb, which the
platform's classifier reads (``services/tool_classification.py``): every
non-GET operation is named with a write verb, so a read-only binding
withholds it. Destructive operations (delete, remove, deactivate, disable,
unlink, token rotation) ask a person on every call.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path
from typing import Any

from app.integrations.file_delivery import deliver_file
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._client import PROVIDER, SOURCE, AikidoClient, seg

#: The longest text a tool answers; the rest is cut with a note.
MAX_ANSWER_CHARS = 24_000
#: Pages read at most by ``all_pages`` on one call.
MAX_PAGES = 20
#: Rows per page asked when ``all_pages`` is set and the caller gave none.
ALL_PAGES_PER_PAGE = 100
#: Keys whose value is never answered, whatever the endpoint returns.
_SECRET_KEYS = frozenset(
    {"secret", "clientsecret", "token", "accesstoken", "refreshtoken", "apitoken", "password", "signingsecret"}
)
REDACTED = "[redacted]"

_OPERATIONS_FILE = Path(__file__).with_name("operations.json")


@cache
def operations() -> tuple[dict[str, Any], ...]:
    """The operation table, read once."""
    with _OPERATIONS_FILE.open(encoding="utf-8") as handle:
        return tuple(json.load(handle)["operations"])


def redact(data: Any) -> Any:
    """*data* with every secret-named value replaced."""
    if isinstance(data, dict):
        return {
            key: (REDACTED if str(key).replace("_", "").lower() in _SECRET_KEYS else redact(value))
            for key, value in data.items()
        }
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
            "or page / per_page]"
        )
    return f"{title}\n{body}"


def _description(op: dict[str, Any]) -> str:
    """The tool description: the operation's own words, then how to use it."""
    summary = str(op["summary"]).rstrip(".")
    detail = str(op.get("description") or "").strip()
    text = summary + "."
    if detail and detail.rstrip(".").lower() != summary.lower():
        text += " " + detail.rstrip(".") + "."
    if op.get("paginated"):
        text += " Paged (page from 0, per_page); all_pages reads up to 20 pages."
    if op["response"] in ("csv_or_json", "pdf"):
        text += " A CSV or PDF answer is delivered as a file."
    if op.get("destructive"):
        text += " Destructive: asks a person before it runs."
    if op.get("scopes"):
        text += f" Scope: {', '.join(op['scopes'])}."
    return text


def _schema(op: dict[str, Any]) -> tuple[dict[str, Any], list[str], set[str]]:
    """``(properties, required, body field names)`` of the tool's input."""
    properties: dict[str, Any] = {}
    required: list[str] = []
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
            properties[name] = field_schema
            body_fields.add(name)
        for name in schema.get("required") or []:
            if name in body_fields:
                required.append(name)
    elif body:
        properties["body"] = {
            **body["schema"],
            "description": (body["schema"].get("description") or "The request body, as the API takes it."),
        }
        required.append("body")
    if op.get("paginated"):
        properties["all_pages"] = {
            "type": "boolean",
            "description": f"Read every page (up to {MAX_PAGES}) and answer them together.",
        }
    return properties, required, body_fields


def _make_handler(
    client: AikidoClient,
    op: dict[str, Any],
    body_fields: set[str],
    persist_file_fn: Any,
) -> Callable[..., Awaitable[str | ToolResult]]:
    path_params = [p["name"] for p in op["params"] if p["in"] == "path"]
    query_params = [p["name"] for p in op["params"] if p["in"] == "query"]
    body = op.get("body")
    action = op["summary"][0].lower() + op["summary"][1:] if op["summary"] else op["name"]

    async def handler(**kwargs: Any) -> str | ToolResult:
        path = op["path"]
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
        wanted_format = query.get("format") or (payload.get("format") if isinstance(payload, dict) else None)
        if op["response"] == "pdf" or (op["response"] == "csv_or_json" and wanted_format == "csv"):
            return await _export(client, op, path, query, payload, action, persist_file_fn)
        if op.get("paginated") and kwargs.get("all_pages"):
            return await _all_pages(client, op, path, query, action)
        resp, err = await client.request(op["method"], path, action, params=query, body=payload)
        if err or resp is None:
            return err or f"Error: {PROVIDER} answered nothing while trying to {action}."
        title = f"{PROVIDER}: {op['summary']}"
        if op.get("paginated"):
            page = query.get("page", 0)
            if resp.has_next_page:
                title += f" (page {page}; next page {int(page or 0) + 1})"
            elif resp.has_next_page is False:
                title += f" (page {page}, last page)"
        if resp.data is None and resp.content:
            return f"{title}\n{resp.content[:MAX_ANSWER_CHARS].decode('utf-8', 'replace')}"
        if resp.data is None and op["method"] != "GET":
            return f"{title}: done."
        return render(title, resp.data)

    return handler


async def _all_pages(client: AikidoClient, op: dict[str, Any], path: str, query: dict[str, Any], action: str) -> str:
    rows: list[Any] = []
    page = int(query.get("page") or 0)
    query = {**query, "per_page": query.get("per_page") or ALL_PAGES_PER_PAGE}
    more = False
    for _ in range(MAX_PAGES):
        resp, err = await client.request("GET", path, f"{action} (page {page})", params={**query, "page": page})
        if err or resp is None:
            if rows:
                return render(f"{PROVIDER}: {op['summary']} ({len(rows)} rows read, then: {err})", rows)
            return err or f"Error: {PROVIDER} answered nothing while trying to {action}."
        batch = resp.data if isinstance(resp.data, list) else [resp.data]
        rows.extend(batch)
        more = bool(resp.has_next_page) if resp.has_next_page is not None else len(batch) >= int(query["per_page"])
        if not more or not batch:
            break
        page += 1
    # After a full last iteration `page` already names the next unread page.
    note = f"; more from page {page}" if more else ""
    return render(f"{PROVIDER}: {op['summary']} ({len(rows)} rows{note})", rows)


async def _export(
    client: AikidoClient,
    op: dict[str, Any],
    path: str,
    query: dict[str, Any],
    payload: Any,
    action: str,
    persist_file_fn: Any,
) -> str | ToolResult:
    accept = "application/pdf" if op["response"] == "pdf" else "text/csv, application/json"
    resp, err = await client.request(op["method"], path, action, params=query, body=payload, accept=accept)
    if err or resp is None:
        return err or f"Error: {PROVIDER} answered nothing while trying to {action}."
    if resp.data is not None and not resp.content_type.startswith(("text/csv", "application/pdf")):
        return render(f"{PROVIDER}: {op['summary']}", resp.data)
    extension = "pdf" if "pdf" in resp.content_type or op["response"] == "pdf" else "csv"
    content_type = "application/pdf" if extension == "pdf" else "text/csv"
    return await deliver_file(
        data=resp.content,
        filename=f"aikido-{op['name'].removeprefix('export_aikido_').replace('_', '-')}.{extension}",
        content_type=content_type,
        source=PROVIDER,
        persist_file_fn=persist_file_fn,
        details=[f"{op['summary']} from the Aikido workspace."],
    )


def build_operation_tools(
    client: AikidoClient,
    executor: CallableToolExecutor,
    *,
    persist_file_fn: Any = None,
) -> list[ToolDefinition]:
    """A tool for every operation of the table, its handler registered."""
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
                always_ask=bool(op.get("destructive")),
            )
        )
    return tools


#: Operations an AppSec agent reaches first: kept whatever the tool budget.
_ESSENTIAL = frozenset(
    {
        "export_aikido_issues",
        "get_aikido_issue_detail",
        "list_aikido_open_issue_groups",
        "get_aikido_issue_group_details",
        "get_aikido_issue_counts",
        "list_aikido_code_repos",
        "get_aikido_code_repo",
        "list_aikido_container_repos",
        "list_aikido_clouds",
        "list_aikido_teams",
        "get_aikido_workspace_info",
    }
)


def _priority(op: dict[str, Any]) -> int:
    if op["name"] in _ESSENTIAL:
        return 0
    if op["method"] != "GET":
        return 2
    return 1
