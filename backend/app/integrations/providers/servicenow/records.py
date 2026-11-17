"""ServiceNow generic Table API escape hatch + Aggregate + Attachments.

The Table API is uniform across EVERY ServiceNow table, so these tools let the
agent reach any table the first-class ITSM/CMDB/catalog tools do not cover
(sla, sys_email, cmdb_ci_*, custom x_* tables, ...): query, get, count, and a
consolidated create/update/delete. Plus attachment list / upload (from a file
the user attached in chat) / download.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.integrations.file_delivery import deliver_file, uploaded_file_answer
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._common import (
    _SOURCE,
    attachment_url,
    cell,
    count_records,
    create_record,
    disp,
    get_record,
    resolve_sys_id,
    result_of,
    sn_request,
    table_url,
    update_record,
)

if TYPE_CHECKING:
    from app.tools.base import FileContext

logger = logging.getLogger(__name__)

PersistFileFn = Callable[[str, bytes, str], Awaitable[Any]]

# Cap attachment download/upload size (ServiceNow's default max is ~10 MB).
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_WRITE_ACTIONS = frozenset({"create", "update", "delete"})


def _format_generic(rec: dict[str, Any], *, max_fields: int = 14) -> str:
    """Render an arbitrary record as a compact, readable block."""
    sys_id = disp(rec.get("sys_id"))
    headline = (
        disp(rec.get("number")) or disp(rec.get("name")) or sys_id or "(record)"
    )
    lines = [f"  {headline}  (sys_id: {sys_id})"]
    shown = 0
    for key, value in rec.items():
        if key == "sys_id":
            continue
        rendered = cell(value, 160)
        if not rendered:
            continue
        lines.append(f"    {key}: {rendered}")
        shown += 1
        if shown >= max_fields:
            lines.append("    ... (more fields; pass sysparm-style fields to narrow)")
            break
    return "\n".join(lines)


def register(
    base: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    persist_file_fn: PersistFileFn | None = None,
) -> list[ToolDefinition]:
    async def query_servicenow_table(
        table: str,
        query: str = "",
        fields: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Query ANY ServiceNow table with a raw encoded query (the escape hatch).

        Use the first-class tools (incidents/changes/problems/CMDB/...) when they
        fit; use this for any other table (sla, sc_task, sys_email, custom x_*
        tables). ``query`` is a ServiceNow encoded query
        (e.g. ``active=true^priorityIN1,2^ORDERBYDESCsys_created_on``). ``fields``
        is a comma-separated allow-list to keep the payload small (recommended).
        """
        table = (table or "").strip()
        if not table:
            return "Error: a table name is required (e.g. 'incident', 'sc_task')."
        from ._common import list_table

        return await list_table(
            base,
            headers,
            table,
            f"query {table}",
            _format_generic,
            noun=f"{table} record",
            query=query,
            limit=limit,
            offset=offset,
            fields=fields,
        )

    async def get_servicenow_record(
        table: str, record: str, number_field: str = "number"
    ) -> str:
        """Get a single record from ANY table by sys_id or human key.

        ``number_field`` is the column to match when ``record`` is not a sys_id
        (default ``number``; use ``name`` for CMDB/user-like tables).
        """
        table = (table or "").strip()
        if not table:
            return "Error: a table name is required."
        return await get_record(
            base,
            headers,
            table,
            record,
            f"get {table} record",
            lambda rec: _format_generic(rec, max_fields=40),
            number_field=number_field or "number",
        )

    async def count_servicenow_records(table: str, query: str = "") -> str:
        """Count records in ANY table matching an encoded query (Aggregate API).

        Cheap way to answer "how many open P1 incidents?" without paging records.
        """
        table = (table or "").strip()
        if not table:
            return "Error: a table name is required."
        count, err = await count_records(
            base, headers, table, query, f"count {table}"
        )
        if err:
            return err
        if count is None:
            return f"Could not determine the {table} count."
        return f"{count} {table} record(s) match the query."

    async def manage_servicenow_record(
        table: str,
        action: str,
        record: str = "",
        fields: dict[str, Any] | None = None,
        number_field: str = "number",
    ) -> str:
        """Create, update, or delete a record in ANY table (the write escape hatch).

        - action="create": ``fields`` is the column->value map (required).
        - action="update": ``record`` (sys_id or number) + ``fields`` (required).
        - action="delete": ``record`` (sys_id or number). PERMANENT - cannot be undone.

        Reference fields take a sys_id; choice fields take their raw value. Prefer
        the first-class tools (incidents/changes/...) when they fit.
        """
        table = (table or "").strip()
        act = (action or "").strip().lower()
        if not table:
            return "Error: a table name is required."
        if act not in _WRITE_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_WRITE_ACTIONS))}."
        if act == "create":
            if not fields:
                return "Error: 'fields' (column->value map) is required to create."
            return await create_record(
                base, headers, table, f"create {table} record", fields, _format_generic
            )
        if act == "update":
            if not record:
                return "Error: 'record' (sys_id or number) is required to update."
            if not fields:
                return "Error: 'fields' (column->value map) is required to update."
            return await update_record(
                base,
                headers,
                table,
                record,
                f"update {table} record",
                fields,
                _format_generic,
                number_field=number_field or "number",
            )
        # delete (destructive)
        if not record:
            return "Error: 'record' (sys_id or number) is required to delete."
        sys_id, err = await resolve_sys_id(
            base, headers, table, record, number_field=number_field or "number"
        )
        if err:
            return err
        _, err = await sn_request(
            "delete",
            table_url(base, table, sys_id),
            f"delete {table} record",
            headers=headers,
        )
        if err:
            return err
        return f"Deleted {table} record '{record}' (sys_id {sys_id})."

    async def list_servicenow_attachments(table: str, record: str) -> str:
        """List the attachments on a record (by table + sys_id or number)."""
        table = (table or "").strip()
        if not table or not record:
            return "Error: table and record (sys_id or number) are required."
        sys_id, err = await resolve_sys_id(base, headers, table, record)
        if err:
            return err
        resp, err = await sn_request(
            "get",
            attachment_url(base),
            f"list attachments for {table} {record}",
            headers=headers,
            params={
                "sysparm_query": f"table_name={table}^table_sys_id={sys_id}",
                "sysparm_limit": 100,
            },
        )
        if err:
            return err
        rows = result_of(resp)
        rows = rows if isinstance(rows, list) else []
        if not rows:
            return f"No attachments on {table} {record}."
        lines = [f"Attachments on {table} {record} ({len(rows)}):"]
        for a in rows:
            size_kb = round((int(a.get("size_bytes") or 0)) / 1024, 1)
            line = (
                f"  {disp(a.get('file_name')) or '?'} ({size_kb} KB, "
                f"{disp(a.get('content_type')) or '?'}) - id: {disp(a.get('sys_id'))}"
            )
            if link := disp(a.get("download_link")):
                line += f" - download: {link}"
            lines.append(line)
        lines.append("\nTip: download_servicenow_attachment(attachment_id) to fetch one.")
        return "\n".join(lines)

    async def add_servicenow_attachment(
        table: str,
        record: str,
        file_id: str = "",
        file_name: str = "",
        content: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Attach a file to a record (by table + sys_id or number).

        Provide EITHER ``file_id`` - a file the user attached in this conversation
        (forwarded byte-for-byte) - OR ``content`` to create a text attachment.
        ``file_name`` overrides the stored name.
        """
        table = (table or "").strip()
        if not table or not record:
            return "Error: table and record (sys_id or number) are required."
        if file_id and file_context is not None:
            try:
                resolved = await file_context.resolve_upload(file_id)
            except Exception as exc:  # pragma: no cover - defensive
                return f"Error: could not read attached file {file_id}: {exc}"
            if not resolved:
                return (
                    f"Error: file '{file_id}' is not available in this conversation."
                )
            data, resolved_name, mime = resolved
            file_name = file_name or resolved_name
        elif content:
            data = content.encode("utf-8")
            mime = "text/plain"
            file_name = file_name or "note.txt"
        else:
            return (
                "Error: provide file_id (a file attached in this chat) or content "
                "(to create a text attachment)."
            )
        # Named by its id: the stored name may be a third party's text, which
        # reaches the model only in the closing quarantine block.
        subject = f"file '{file_id}'" if file_id and file_context is not None else "the text attachment"
        if len(data) > _MAX_ATTACHMENT_BYTES:
            return (
                f"Error: {subject} exceeds the "
                f"{_MAX_ATTACHMENT_BYTES // 1024 // 1024} MB upload limit."
            )
        sys_id, err = await resolve_sys_id(base, headers, table, record)
        if err:
            return err
        upload_headers = {
            "Authorization": headers["Authorization"],
            "Accept": "application/json",
            "Content-Type": mime or "application/octet-stream",
        }
        resp, err = await sn_request(
            "post",
            attachment_url(base, "file"),
            f"upload attachment to {table} {record}",
            headers=upload_headers,
            params={
                "table_name": table,
                "table_sys_id": sys_id,
                "file_name": file_name,
            },
            content=data,
            timeout=60.0,
        )
        if err:
            return err
        res = result_of(resp)
        att_id = disp(res.get("sys_id")) if isinstance(res, dict) else ""
        link = disp(res.get("download_link")) if isinstance(res, dict) else ""
        return uploaded_file_answer(
            f"Attached {subject} ({round(len(data) / 1024, 1)} KB) to {table} "
            f"{record}" + (f" (attachment sys_id: {att_id})." if att_id else "."),
            target="ServiceNow",
            filename=file_name,
            links=[("Download URL", link)],
        )

    async def download_servicenow_attachment(attachment_id: str) -> str | ToolResult:
        """Download an attachment by its sys_id and return it as a download card.

        Use list_servicenow_attachments to find attachment ids. Include the
        returned [[FILE:...]] marker verbatim in your reply.
        """
        attachment_id = (attachment_id or "").strip()
        if not attachment_id:
            return "Error: attachment_id is required."
        meta_resp, err = await sn_request(
            "get",
            attachment_url(base, attachment_id),
            f"get attachment {attachment_id} metadata",
            headers=headers,
        )
        if err:
            return err
        meta = result_of(meta_resp)
        meta = meta if isinstance(meta, dict) else {}
        file_name = disp(meta.get("file_name")) or f"servicenow-{attachment_id}"
        mime = disp(meta.get("content_type")) or "application/octet-stream"
        try:
            size = int(meta.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        if size and size > _MAX_ATTACHMENT_BYTES:
            return (
                f"Attachment '{file_name}' is {round(size / 1024 / 1024, 1)} MB, "
                f"over the {_MAX_ATTACHMENT_BYTES // 1024 // 1024} MB download limit."
            )
        resp, err = await sn_request(
            "get",
            attachment_url(base, f"{attachment_id}/file"),
            f"download attachment {attachment_id}",
            headers=headers,
            follow_redirects=True,
            timeout=60.0,
        )
        if err:
            return err
        data = resp.content
        if len(data) > _MAX_ATTACHMENT_BYTES:
            return (
                f"Attachment '{file_name}' exceeds the "
                f"{_MAX_ATTACHMENT_BYTES // 1024 // 1024} MB download limit."
            )
        return await deliver_file(
            data=data,
            filename=file_name,
            content_type=mime,
            source="ServiceNow",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", disp(meta.get("download_link")))],
        )

    executor.register("query_servicenow_table", query_servicenow_table)
    executor.register("get_servicenow_record", get_servicenow_record)
    executor.register("count_servicenow_records", count_servicenow_records)
    executor.register("manage_servicenow_record", manage_servicenow_record)
    executor.register("list_servicenow_attachments", list_servicenow_attachments)
    executor.register("add_servicenow_attachment", add_servicenow_attachment)
    executor.register("download_servicenow_attachment", download_servicenow_attachment)

    return [
        ToolDefinition(
            name="query_servicenow_table",
            description=(
                "Query ANY ServiceNow table with a raw encoded query - the escape "
                "hatch for tables the first-class tools don't cover (sla, sc_task, "
                "sys_email, custom x_* tables). Pass 'fields' (comma-separated) to "
                "keep results small. Prefer the dedicated incident/change/problem/"
                "CMDB tools when they fit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name (e.g. 'sc_task')"},
                    "query": {
                        "type": "string",
                        "description": "ServiceNow encoded query (^-joined)",
                    },
                    "fields": {
                        "type": "string",
                        "description": "Comma-separated fields to return (recommended)",
                    },
                    "limit": {"type": "integer", "default": 20},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["table"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_servicenow_record",
            description=(
                "Get one record from ANY table by sys_id or human key. "
                "number_field is the match column when not a sys_id (default "
                "'number'; use 'name' for CMDB/user-like tables)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "record": {"type": "string", "description": "sys_id or human key"},
                    "number_field": {"type": "string", "default": "number"},
                },
                "required": ["table", "record"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="count_servicenow_records",
            description=(
                "Count records in ANY table matching an encoded query (Aggregate "
                "API) - cheap way to answer 'how many ...?' without paging records."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "query": {"type": "string", "description": "Encoded query"},
                },
                "required": ["table"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_servicenow_record",
            description=(
                "Create, update, or delete a record in ANY table (write escape "
                "hatch). create: fields map required. update: record + fields. "
                "delete: record (PERMANENT - cannot be undone). Reference fields "
                "take a sys_id; choice fields take their raw value. Prefer the "
                "first-class tools when they fit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "delete"],
                    },
                    "record": {
                        "type": "string",
                        "description": "sys_id or number (update/delete)",
                    },
                    "fields": {
                        "type": "object",
                        "description": "Column -> value map (create/update)",
                    },
                    "number_field": {"type": "string", "default": "number"},
                },
                "required": ["table", "action"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_servicenow_attachments",
            description="List the attachments on a record (by table + sys_id or number).",
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "record": {"type": "string", "description": "sys_id or number"},
                },
                "required": ["table", "record"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_servicenow_attachment",
            description=(
                "Attach a file to a record. Provide file_id (a file the user "
                "attached in this chat, forwarded byte-for-byte) OR content (to "
                "create a text attachment). Use list_servicenow_attachments to "
                "verify."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "record": {"type": "string", "description": "sys_id or number"},
                    "file_id": {
                        "type": "string",
                        "description": "Attached-file id from this conversation",
                    },
                    "file_name": {"type": "string", "description": "Override filename"},
                    "content": {
                        "type": "string",
                        "description": "Text content (creates a text attachment)",
                    },
                },
                "required": ["table", "record"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_servicenow_attachment",
            workspace_ingest=True,
            description=(
                "Download an attachment by its sys_id and return it as a download "
                "card. Find ids with list_servicenow_attachments. Include the "
                "returned [[FILE:...]] marker verbatim in your reply."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attachment_id": {"type": "string", "description": "Attachment sys_id"}
                },
                "required": ["attachment_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
