"""Jira issue attachments - list, download, add (text or a file of the run), delete.

Every answer that meets an attachment carries its id and its ``content`` URL.
The download tool stores the attachment as a file of the run (via the
injected ``persist_file_fn``): a ``[[FILE:id]]`` download card, a file_id to
forward, the file on the result so the model reads it. It falls back to
an in-memory file reference when no persistence callback is available.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.integrations.file_delivery import deliver_file, read_pipe_file, uploaded_file_answer
from app.services.content_safety import single_line
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._common import _SOURCE, api3, format_attachment, jira_request

logger = logging.getLogger(__name__)

# Persist callback signature: (filename, raw_bytes, content_type) -> FileRef|None
PersistFileFn = Callable[[str, bytes, str], Awaitable[Any]]

# Cap download size so a huge attachment cannot exhaust memory / context.
_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024


def register(
    base: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    persist_file_fn: PersistFileFn | None = None,
) -> list[ToolDefinition]:
    api = api3(base)

    async def list_jira_attachments(issue_key: str) -> str:
        """List the attachments on an issue (filename, size, type, id, author, content URL)."""
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}",
            f"list attachments for {issue_key}",
            headers=headers,
            params={"fields": "attachment"},
        )
        if err:
            return err
        attachments = (resp.json().get("fields") or {}).get("attachment") or []
        if not attachments:
            return f"No attachments on {issue_key}."
        lines = [f"Attachments on {issue_key} ({len(attachments)}):"]
        lines.extend(f"  {format_attachment(a)}" for a in attachments)
        lines.append("\nTip: download_jira_attachment(attachment_id) to fetch one.")
        return "\n".join(lines)

    async def download_jira_attachment(attachment_id: str) -> str | ToolResult:
        """Download an attachment by id and return it as a downloadable file."""
        # Resolve filename + mime from the attachment metadata first.
        meta_resp, meta_err = await jira_request(
            "get",
            f"{api}/attachment/{attachment_id}",
            f"get attachment {attachment_id} metadata",
            headers=headers,
        )
        if meta_err:
            return meta_err
        meta = meta_resp.json()
        filename = meta.get("filename") or f"jira-attachment-{attachment_id}"
        mime_type = meta.get("mimeType") or "application/octet-stream"
        size = meta.get("size") or 0
        if size and size > _MAX_DOWNLOAD_BYTES:
            return (
                f"Attachment '{filename}' is {round(size / 1024 / 1024, 1)} MB, which "
                f"exceeds the {_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB download limit. "
                f"Download URL: {meta.get('content') or 'N/A'}"
            )
        resp, err = await jira_request(
            "get",
            f"{api}/attachment/content/{attachment_id}",
            f"download attachment {attachment_id}",
            headers=headers,
            follow_redirects=True,
            timeout=60.0,
        )
        if err:
            return err
        data = resp.content
        if len(data) > _MAX_DOWNLOAD_BYTES:
            return (
                f"Attachment '{filename}' exceeds the "
                f"{_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB download limit."
            )
        return await deliver_file(
            data=data,
            filename=filename,
            content_type=mime_type,
            source="Jira",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", meta.get("content"))],
        )

    async def _attach(
        issue_key: str, filename: str, data: bytes, mime_type: str, *, what: str
    ) -> str:
        """Attach *data* to *issue_key*; *what* names it in the answer (``file <id>``, never its name)."""
        # Adding attachments requires the X-Atlassian-Token: no-check header.
        upload_headers = {**headers, "X-Atlassian-Token": "no-check"}
        resp, err = await jira_request(
            "post",
            f"{api}/issue/{issue_key}/attachments",
            f"add attachment to {issue_key}",
            headers=upload_headers,
            files={"file": (filename, data, mime_type)},
        )
        if err:
            return err
        created = resp.json()
        first = created[0] if isinstance(created, list) and created and isinstance(created[0], dict) else {}
        # The stored name and Jira's echo of it go in the block.
        return uploaded_file_answer(
            f"Attached {what} to {issue_key}" + (f" (id: {first.get('id', '?')})." if first else "."),
            target="Jira",
            filename=filename,
            details=[f"Jira name: {single_line(str(first['filename']))}"] if first.get("filename") else [],
            links=[("Download URL", first.get("content"))],
        )

    async def add_jira_attachment_text(
        issue_key: str, filename: str, content: str
    ) -> str:
        """Attach a text/markdown file (created from ``content``) to an issue.

        Useful for attaching generated notes, logs, or reports.
        """
        return await _attach(issue_key, filename, content.encode("utf-8"), "text/plain", what="the text file")

    async def upload_jira_attachment(
        issue_key: str,
        file_id: str,
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Attach a file of the run (attached, generated or downloaded) to an issue, byte-for-byte."""
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_DOWNLOAD_BYTES, target="Jira"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, mime_type = piped
        return await _attach(
            issue_key, (filename or "").strip() or piped_name, data, mime_type, what=f"file {file_id.strip()}"
        )

    async def delete_jira_attachment(attachment_id: str) -> str:
        """Delete an attachment by id. Cannot be undone."""
        _, err = await jira_request(
            "delete",
            f"{api}/attachment/{attachment_id}",
            f"delete attachment {attachment_id}",
            headers=headers,
        )
        if err:
            return err
        return f"Attachment {attachment_id} deleted."

    executor.register("list_jira_attachments", list_jira_attachments)
    executor.register("download_jira_attachment", download_jira_attachment)
    executor.register("add_jira_attachment_text", add_jira_attachment_text)
    executor.register("upload_jira_attachment", upload_jira_attachment)
    executor.register("delete_jira_attachment", delete_jira_attachment)

    return [
        ToolDefinition(
            name="list_jira_attachments",
            description="List the attachments on an issue with filename, size, type, author, attachment id and download URL.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_jira_attachment",
            workspace_ingest=True,
            description=(
                "Download an issue attachment by id into the conversation: a download "
                "card, its download URL and a file_id to forward to any tool that takes "
                "a file; its readable content comes with the result. Use "
                "list_jira_attachments to find ids. Include the returned [[FILE:...]] "
                "marker verbatim so the user gets a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attachment_id": {"type": "string", "description": "Attachment id from list_jira_attachments"},
                },
                "required": ["attachment_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_jira_attachment_text",
            description="Attach a text/markdown file (built from the provided content) to an issue - e.g. a generated note, log or report.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "filename": {"type": "string", "description": "Attachment filename (e.g. 'notes.md')"},
                    "content": {"type": "string", "description": "Text content of the file"},
                },
                "required": ["issue_key", "filename", "content"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="upload_jira_attachment",
            description=(
                "Attach a file of this conversation to an issue byte-for-byte: any file "
                "attached by the user, generated, or downloaded by another tool (a PDF, "
                "an image, a spreadsheet...), by its file_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                    "filename": {"type": "string", "description": "Optional attachment name (default: the file's own name)"},
                },
                "required": ["issue_key", "file_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_jira_attachment",
            description="Delete an issue attachment by id permanently. Cannot be undone.",
            input_schema={
                "type": "object",
                "properties": {
                    "attachment_id": {"type": "string", "description": "Attachment id"},
                },
                "required": ["attachment_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
