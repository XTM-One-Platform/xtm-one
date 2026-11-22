# SPDX-License-Identifier: Apache-2.0

"""DealHub tools: one per documented operation, built from the operation table.

A GET is a read (``read_only``); any other method is a write that asks a
person before it runs (``always_ask``), whatever DealHub calls it. Paged reads
take ``offset`` / ``limit`` and ``all_pages``. A document or an Excel export is
stored as a file of the conversation. Secrets in an answer are redacted.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.integrations.file_delivery import deliver_file, file_content_type
from app.integrations.providers.dealhub._client import SOURCE, DealHubClient, seg
from app.integrations.providers.dealhub._operations import OPERATIONS, Operation
from app.tools.base import ToolDefinition, ToolResult

MAX_ANSWER_CHARS = 60_000
MAX_PAGES = 20
PAGE_SIZE = 50
_MAX_FILE_BYTES = 50 * 1024 * 1024
REDACTED = "[redacted]"
#: A key holding one of these words (``api_token``, ``client_secret``...) is a secret.
_SECRET_WORDS = ("password", "token", "apikey", "secret", "authorization")
_FILE_EXTENSIONS = {"PDF": "pdf", "WORD": "docx", "EXCEL": "xlsx"}


def redact(data: Any) -> Any:
    """*data* with every secret-named value replaced."""
    if isinstance(data, dict):
        return {key: (REDACTED if _is_secret_key(key) else redact(value)) for key, value in data.items()}
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data


def _is_secret_key(key: Any) -> bool:
    flat = str(key).replace("_", "").replace("-", "").lower()
    return any(word in flat for word in _SECRET_WORDS)


def render(title: str, data: Any) -> str:
    """A title line, then the redacted data as compact JSON, bounded."""
    if data is None:
        return title
    body = json.dumps(redact(data), ensure_ascii=False, default=str, separators=(",", ":"))
    if len(body) > MAX_ANSWER_CHARS:
        body = (
            body[:MAX_ANSWER_CHARS]
            + f"... [cut at {MAX_ANSWER_CHARS} of {len(body)} characters: narrow the call (feature, limit, sku)]"
        )
    return f"{title}\n{body}"


def _description(op: Operation) -> str:
    text = op.description
    if op.paginated:
        text += f" Paged (offset, limit); all_pages reads up to {MAX_PAGES} pages."
    if op.file:
        text += " The file is stored in the conversation."
    if not op.is_read:
        text += " Changes DealHub: asks a person before it runs."
    return text


def _schema(op: Operation) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in op.params:
        properties[param.name] = {**param.schema, "description": param.description}
        if param.required:
            required.append(param.name)
    if op.body is not None:
        properties["body"] = {
            "type": ["object", "array"],
            "description": "Request body (JSON), as DealHub documents it: " + op.body,
        }
        if op.body_required:
            required.append("body")
    if op.paginated:
        properties["all_pages"] = {"type": "boolean", "description": f"Read every page (up to {MAX_PAGES})."}
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _items(data: Any) -> list[Any] | None:
    """The list a paged answer carries (``quotes``, ``products``...), or ``None``."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return value
    return None


def _path(op: Operation, args: dict[str, Any]) -> tuple[str | None, str | None]:
    path = op.path
    for param in op.params:
        if param.where != "path":
            continue
        value = args.get(param.name)
        if value is None or str(value).strip() == "":
            return None, f"Error: {param.name} is required."
        path = path.replace("{" + param.name + "}", seg(value))
    return path, None


def _make_handler(
    client: DealHubClient, op: Operation, persist_file_fn: Any
) -> Callable[..., Awaitable[str | ToolResult]]:
    query_names = [p.name for p in op.params if p.where == "query"]

    async def handler(**kwargs: Any) -> str | ToolResult:
        path, err = _path(op, kwargs)
        if err or path is None:
            return err or "Error: the request path could not be built."
        params = {name: kwargs.get(name) for name in query_names}
        action = op.description.split(":")[0].split(".")[0].rstrip().lower()
        if op.file:
            return await _download(client, op, path, params, action, persist_file_fn, kwargs)
        if op.paginated and kwargs.get("all_pages"):
            return await _all_pages(client, op, path, params, action)
        resp, err = await client.request(op.method, path, action, params=params, body=kwargs.get("body"))
        if err:
            return err
        title = f"{op.name}: {op.method} {op.path}"
        if resp is None or (resp.data is None and not resp.content):
            return f"{title}: done."
        if resp.data is None:
            return f"{title}: DealHub answered {len(resp.content)} byte(s) of {resp.content_type or 'data'}."
        return render(title, resp.data)

    handler.__name__ = op.name
    return handler


async def _all_pages(client: DealHubClient, op: Operation, path: str, params: dict[str, Any], action: str) -> str:
    limit = int(params.get("limit") or PAGE_SIZE)
    offset = int(params.get("offset") or 0)
    collected: list[Any] = []
    pages = 0
    complete = False
    while pages < MAX_PAGES:
        resp, err = await client.request(op.method, path, action, params={**params, "offset": offset, "limit": limit})
        if err:
            return err if not collected else render(f"{op.name}: {len(collected)} item(s), then {err}", collected)
        pages += 1
        items = _items(resp.data if resp else None) or []
        collected.extend(items)
        if len(items) < limit:
            complete = True
            break
        offset += limit
    note = "" if complete else f" (stopped after {MAX_PAGES} pages: continue with offset={offset})"
    return render(f"{op.name}: {len(collected)} item(s) over {pages} page(s){note}", collected)


async def _download(
    client: DealHubClient,
    op: Operation,
    path: str,
    params: dict[str, Any],
    action: str,
    persist_file_fn: Any,
    args: dict[str, Any],
) -> str | ToolResult:
    resp, err = await client.request("GET", path, action, params=params, accept="*/*")
    if err:
        return err
    if resp is None or not resp.content:
        return f"Error: DealHub answered no file while trying to {action}."
    if len(resp.content) > _MAX_FILE_BYTES:
        return f"Error: the file is larger than {_MAX_FILE_BYTES // (1024 * 1024)} MB."
    if op.file == "excel":
        ext, ident = "xlsx", args.get("version_id")
    else:
        ext, ident = (
            _FILE_EXTENSIONS.get(str(params.get("document_type") or "PDF").upper(), "pdf"),
            args.get("dealhub_quote_id"),
        )
    filename = resp.filename or f"dealhub-{op.file}-{ident}.{ext}"
    return await deliver_file(
        data=resp.content,
        filename=filename,
        content_type=file_content_type(resp.content_type, filename),
        source="DealHub",
        persist_file_fn=persist_file_fn,
        details=[f"{op.description.split(' as ')[0]} ({ident})."],
    )


def build_operation_tools(
    client: DealHubClient, register: Callable[[str, Callable[..., Any]], None], persist_file_fn: Any = None
) -> list[ToolDefinition]:
    definitions: list[ToolDefinition] = []
    for op in OPERATIONS:
        register(op.name, _make_handler(client, op, persist_file_fn))
        definitions.append(
            ToolDefinition(
                name=op.name,
                description=_description(op),
                input_schema=_schema(op),
                source=SOURCE,
                priority=0 if op.name in _ESSENTIAL else (1 if op.is_read else 2),
                read_only=op.is_read,
                always_ask=not op.is_read,
            )
        )
    return definitions


#: The tools a deal desk review reaches first.
_ESSENTIAL = frozenset(
    {
        "list_dealhub_quotes",
        "get_dealhub_quote",
        "download_dealhub_quote_document",
        "get_dealhub_product_catalog",
        "list_dealhub_users",
    }
)
