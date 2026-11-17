"""Google TI files - samples, report PDFs, IoC exports and DTM alert attachments.

Every tool here gives the agent the file's links, stores it as a file of the
run (a download card and a file_id) and, but for a sample, carries it on the
result so the model reads it:

* ``download_gti_file_sample`` - a sample is live malware, so it is fetched
  only as Google TI's password-protected ZIP (``POST /intelligence/zip_files``
  with the ``infected`` password, polled, then its signed download URL) and
  stored exactly as it came (``deliver_live_artefact``).
* ``download_gti_report`` - ``GET /collections/{id}/download_report``
  answers the signed storage URL of a report's PDF, fetched without the key.
* ``export_gti_collection_iocs`` - ``GET /collections/{id}/download/{format}``,
  the IoCs of a report, threat actor, malware family, campaign or
  vulnerability as JSON, CSV or STIX.
* ``list_gti_dtm_alert_attachments`` / ``download_gti_dtm_alert_attachment``
  / ``upload_gti_dtm_alert_attachment`` - the files of a DTM alert's analysis
  (``/dtm/alerts/{id}/attachments``); an upload takes any file_id of the run.

The ``x-apikey`` header goes to Google TI's host alone: a request carrying it
never follows a redirect, and a signed storage URL is fetched without it,
every hop under the outbound URL policy (``social_media.fetch_public_media``).
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from app.integrations.file_delivery import (
    SEALED_ARCHIVE_PASSWORD,
    PersistFileFn,
    deliver_file,
    deliver_live_artefact,
    fetch_file,
    file_content_type,
    format_kb,
    link_lines,
    read_pipe_file,
    untrusted_file_facts,
    uploaded_file_answer,
)
from app.integrations.http import safe_request
from app.integrations.providers.social_media import fetch_public_media
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._common import _PROVIDER, _SOURCE, client, data_of, gui_link, trunc

_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
# The ZIP of a sample is built asynchronously; poll its status this long.
_ZIP_POLL_INTERVAL = 3.0
_ZIP_MAX_POLLS = 10
_ZIP_ERROR_STATUSES = {"timeout", "error-starting", "error-creating"}
# Export format -> (file name suffix, content type).
_EXPORT_FORMATS = {
    "json": ("json", "application/json"),
    "csv": ("csv", "text/csv"),
    "stix": ("stix.json", "application/json"),
}
# File types a DTM alert analysis accepts
# (https://gtidocs.readme.io/reference/post-alert-analysis-attachment).
_DTM_ATTACHMENT_EXTENSIONS = {"doc", "docx", "xls", "xlsx", "pdf", "png", "zip", "csv", "txt"}


def register(
    api: str,
    dtm_api: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    persist_file_fn: PersistFileFn | None = None,
) -> list[ToolDefinition]:
    """Register the Google TI file tools and return their definitions."""
    file_headers = {**headers, "Accept": "*/*"}

    def _too_large(name: str, size: int, url: str) -> str:
        return (
            f"The file is {round(size / 1024 / 1024, 1)} MB, over the "
            f"{_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB download limit.\n"
            + untrusted_file_facts(source=_PROVIDER, filename=name, links=[("Download URL", url)])
        )

    # -- samples -------------------------------------------------------------

    async def download_gti_file_sample(hash: str) -> str | ToolResult:
        """Store a sample as Google TI's password-protected ZIP."""
        value = (hash or "").strip().lower()
        if not value or not value.isalnum():
            return "Error: a file hash (MD5, SHA-1, or SHA-256) is required."
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/intelligence/zip_files",
                _PROVIDER,
                f"create sample ZIP for {trunc(value, 80)}",
                headers=headers,
                json={"data": {"password": SEALED_ARCHIVE_PASSWORD, "hashes": [value]}},
            )
            if err:
                return err
            zip_id = str((data_of(resp.json() or {}) or {}).get("id") or "")
            if not zip_id:
                return "Error: Google TI returned no ZIP id."
            status = ""
            for _ in range(_ZIP_MAX_POLLS):
                resp, err = await safe_request(
                    c,
                    "get",
                    f"{api}/intelligence/zip_files/{quote(zip_id, safe='')}",
                    _PROVIDER,
                    f"check sample ZIP {trunc(zip_id, 60)}",
                    headers=headers,
                )
                if err:
                    return err
                attrs = (data_of(resp.json() or {}) or {}).get("attributes") or {}
                status = str(attrs.get("status") or "")
                if status == "finished":
                    if not attrs.get("files_ok"):
                        return f"Google TI holds no downloadable sample for '{trunc(value, 100)}'."
                    break
                if status in _ZIP_ERROR_STATUSES:
                    return f"Google TI could not build the sample ZIP (status: {status})."
                await asyncio.sleep(_ZIP_POLL_INTERVAL)
            else:
                return (
                    f"The sample ZIP {zip_id} is still {status or 'being built'} - "
                    "call the tool again in a moment."
                )
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/intelligence/zip_files/{quote(zip_id, safe='')}/download_url",
                _PROVIDER,
                f"get sample ZIP {trunc(zip_id, 60)} download URL",
                headers=headers,
            )
            if err:
                return err
            signed_url = str(data_of(resp.json() or {}) or "")
        web_link = ("Web URL", gui_link("file", value))
        fetched = await fetch_public_media(
            signed_url, source=_PROVIDER, max_bytes=_MAX_DOWNLOAD_BYTES, what="sample ZIP"
        )
        if isinstance(fetched, str):
            return "\n".join([fetched, *link_lines([web_link])])
        return await deliver_live_artefact(
            data=fetched.data,
            filename=f"{value}.zip",
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[web_link, ("Download URL (signed, expires after 1 hour)", signed_url)],
        )

    # -- reports and IoC exports ----------------------------------------------

    async def download_gti_report(report_id: str) -> str | ToolResult:
        """Store the PDF of a Google TI report as a file of the run."""
        report_id = (report_id or "").strip()
        if not report_id:
            return "Error: report_id is required (a report--... id from search_gti_threats)."
        web_url = gui_link("collection", report_id)
        async with client(timeout=120) as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/collections/{quote(report_id, safe='')}/download_report",
                _PROVIDER,
                f"get the PDF URL of report {trunc(report_id, 80)}",
                headers=headers,
            )
            if err:
                return f"{err}\nWeb URL: {web_url}"
            signed_url = str(data_of(resp.json() or {}) or "")
        fetched = await fetch_public_media(
            signed_url, source=_PROVIDER, max_bytes=_MAX_DOWNLOAD_BYTES, what="report PDF"
        )
        if isinstance(fetched, str):
            return f"{fetched}\nWeb URL: {web_url}"
        links = [("Web URL", web_url), ("Download URL (signed)", signed_url)]
        return await deliver_file(
            data=fetched.data,
            filename=f"{report_id}.pdf",
            content_type="application/pdf",
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=links,
        )

    async def export_gti_collection_iocs(
        collection_id: str, format: str = "json"
    ) -> str | ToolResult:
        """Store the IoCs of a report / threat / campaign / vulnerability as a JSON, CSV or STIX file."""
        collection_id = (collection_id or "").strip()
        if not collection_id:
            return "Error: collection_id is required (from search_gti_threats)."
        fmt = (format or "json").strip().lower()
        if fmt not in _EXPORT_FORMATS:
            return f"Error: format must be one of {', '.join(_EXPORT_FORMATS)}."
        url = f"{api}/collections/{quote(collection_id, safe='')}/download/{fmt}"
        async with client(timeout=120) as c:
            fetched = await fetch_file(
                c,
                "get",
                url,
                provider=_PROVIDER,
                action=f"export IoCs of {trunc(collection_id, 80)} as {fmt}",
                max_bytes=_MAX_DOWNLOAD_BYTES,
                headers=file_headers,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        suffix, content_type = _EXPORT_FORMATS[fmt]
        name = f"{collection_id}-iocs.{suffix}"
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Web URL", gui_link("collection", collection_id)), ("Download URL", url)],
        )

    # -- DTM alert attachments -------------------------------------------------

    def _attachment_url(alert_id: str, attachment_id: str) -> str:
        return (
            f"{dtm_api}/alerts/{quote(alert_id, safe='')}/attachments/"
            f"{quote(attachment_id, safe='')}/download"
        )

    async def _alert_attachments(alert_id: str) -> tuple[list[dict[str, Any]], str | None]:
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{dtm_api}/alerts/{quote(alert_id, safe='')}/attachments",
                _PROVIDER,
                f"list attachments of DTM alert {trunc(alert_id, 60)}",
                headers={**headers, "Content-Type": "application/json"},
            )
            if err:
                return [], err
        payload = resp.json() or {}
        items = payload.get("attachments") if isinstance(payload, dict) else None
        return [a for a in (items or []) if isinstance(a, dict)], None

    async def list_gti_dtm_alert_attachments(alert_id: str) -> str:
        """List the files attached to a DTM alert's analysis."""
        alert_id = (alert_id or "").strip()
        if not alert_id:
            return "Error: alert_id is required (from list_gti_dtm_alerts)."
        attachments, err = await _alert_attachments(alert_id)
        if err:
            return err
        if not attachments:
            return f"DTM alert {trunc(alert_id, 60)} has no attached files."
        lines = [f"Files attached to DTM alert {trunc(alert_id, 60)} ({len(attachments)}):"]
        for a in attachments:
            size = a.get("filesize_bytes")
            lines.append(
                f"  {trunc(a.get('name'), 120)} (id: {trunc(a.get('id'), 60)}, "
                f"{trunc(a.get('content_type'), 60) or '?'}"
                + (f", {format_kb(size)}" if isinstance(size, int) else "")
                + f")\n    Download URL: {_attachment_url(alert_id, str(a.get('id') or ''))}"
            )
        lines.append("download_gti_dtm_alert_attachment(alert_id, attachment_id) stores one as a file.")
        return "\n".join(lines)

    async def download_gti_dtm_alert_attachment(
        alert_id: str, attachment_id: str
    ) -> str | ToolResult:
        """Store a file attached to a DTM alert's analysis as a file of the run."""
        alert_id = (alert_id or "").strip()
        attachment_id = (attachment_id or "").strip()
        if not alert_id or not attachment_id:
            return "Error: alert_id and attachment_id are required (from list_gti_dtm_alert_attachments)."
        attachments, err = await _alert_attachments(alert_id)
        if err:
            return err
        meta = next((a for a in attachments if str(a.get("id")) == attachment_id), {})
        url = _attachment_url(alert_id, attachment_id)
        name = str(meta.get("name") or f"dtm-attachment-{attachment_id}")
        size = meta.get("filesize_bytes")
        if isinstance(size, int) and size > _MAX_DOWNLOAD_BYTES:
            return _too_large(name, size, url)
        async with client(timeout=120) as c:
            fetched = await fetch_file(
                c,
                "get",
                url,
                provider=_PROVIDER,
                action=f"download attachment {trunc(attachment_id, 60)} of DTM alert {trunc(alert_id, 60)}",
                max_bytes=_MAX_DOWNLOAD_BYTES,
                headers=file_headers,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=file_content_type(meta.get("content_type") or fetched.content_type, name),
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", url)],
        )

    async def upload_gti_dtm_alert_attachment(
        alert_id: str, file_id: str, file_context: FileContext | None = None
    ) -> str | ToolResult:
        """Attach a file of the run to a DTM alert's analysis."""
        alert_id = (alert_id or "").strip()
        if not alert_id:
            return "Error: alert_id is required (from list_gti_dtm_alerts)."
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_DOWNLOAD_BYTES, target=_PROVIDER
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, filename, content_type = piped
        file_id = file_id.strip()
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in _DTM_ATTACHMENT_EXTENSIONS:
            return ToolResult(
                tool_call_id="",
                content=uploaded_file_answer(
                    f"Error: DTM accepts {', '.join(sorted(_DTM_ATTACHMENT_EXTENSIONS))} "
                    f"files; the name of file {file_id} has none of these extensions.",
                    target=_PROVIDER,
                    filename=filename,
                ),
                is_error=True,
            )
        async with client(timeout=120) as c:
            _, err = await safe_request(
                c,
                "post",
                f"{dtm_api}/alerts/{quote(alert_id, safe='')}/attachments",
                _PROVIDER,
                f"attach file {file_id} to DTM alert {trunc(alert_id, 60)}",
                headers=headers,
                files={"file": (filename, data, content_type)},
            )
            if err:
                return err
        return uploaded_file_answer(
            f"Attached file {file_id} ({format_kb(len(data))}) to DTM alert "
            f"{trunc(alert_id, 60)}. list_gti_dtm_alert_attachments shows its id and link.",
            target=_PROVIDER,
            filename=filename,
        )

    executor.register("download_gti_file_sample", download_gti_file_sample)
    executor.register("download_gti_report", download_gti_report)
    executor.register("export_gti_collection_iocs", export_gti_collection_iocs)
    executor.register("list_gti_dtm_alert_attachments", list_gti_dtm_alert_attachments)
    executor.register("download_gti_dtm_alert_attachment", download_gti_dtm_alert_attachment)
    executor.register("upload_gti_dtm_alert_attachment", upload_gti_dtm_alert_attachment)

    return [
        ToolDefinition(
            name="download_gti_file_sample",
            workspace_ingest=True,
            description=(
                "Store a Google TI sample (by MD5, SHA-1 or SHA-256) as a file of "
                "the conversation. A sample is live malware: it is fetched only "
                "as Google TI's ZIP encrypted with the password 'infected' (a "
                "ZIP built on demand, counted as a download in your quota), "
                "stored exactly as it came (download card + file_id to forward, "
                "e.g. to a sandbox) and never read inline."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hash": {"type": "string", "description": "MD5, SHA-1 or SHA-256 of the file"},
                },
                "required": ["hash"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_gti_report",
            workspace_ingest=True,
            description=(
                "Store the PDF of a Google TI report (a report--... id from "
                "search_gti_threats / get_gti_threat) as a file of the "
                "conversation (download card + file_id to forward, read inline) "
                "with its web link. Enterprise / Enterprise Plus license."
            ),
            input_schema={
                "type": "object",
                "properties": {"report_id": {"type": "string"}},
                "required": ["report_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="export_gti_collection_iocs",
            description=(
                "Store the IoCs of a Google TI report, threat actor, malware "
                "family, campaign or vulnerability as a JSON, CSV or STIX file of "
                "the conversation (download card + file_id to forward, e.g. to a "
                "TIP import, read inline)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "string",
                        "description": "Collection id (report--..., threat-actor--..., malware--..., campaign--..., vulnerability--...)",
                    },
                    "format": {"type": "string", "enum": list(_EXPORT_FORMATS), "default": "json"},
                },
                "required": ["collection_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_gti_dtm_alert_attachments",
            description=(
                "List the files attached to a Digital Threat Monitoring alert's "
                "analysis with their id, type, size and download URL."
            ),
            input_schema={
                "type": "object",
                "properties": {"alert_id": {"type": "string"}},
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_gti_dtm_alert_attachment",
            workspace_ingest=True,
            description=(
                "Store a file attached to a DTM alert's analysis as a file of "
                "the conversation (download card + file_id to forward, read "
                "inline)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "attachment_id": {"type": "string"},
                },
                "required": ["alert_id", "attachment_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="upload_gti_dtm_alert_attachment",
            description=(
                "Attach a file of the conversation (attached, generated or "
                "downloaded - by file_id) to a DTM alert's analysis. DTM accepts "
                "doc, docx, xls, xlsx, pdf, png, zip, csv and txt files."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "file_id": {
                        "type": "string",
                        "description": "A file attached, generated or downloaded in this conversation",
                    },
                },
                "required": ["alert_id", "file_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
