"""VirusTotal submission + analysis tools.

* ``scan_vt_url`` - submit a URL for analysis (``POST /urls``).
* ``scan_vt_file`` - upload a chat-attached file for analysis
  (``POST /files``; files over 32 MB transparently go through
  ``GET /files/upload_url``).
* ``rescan_vt_object`` - re-analyse an object already known to VirusTotal
  (``POST /{type}/{id}/analyse``).
* ``get_vt_analysis`` - poll a submitted analysis until it completes
  (``GET /analyses/{id}``).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from app.integrations.file_delivery import uploaded_file_answer
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    ENTITY_PATHS,
    attributes_of,
    client,
    data_of,
    format_analysis_stats,
    trunc,
    ts,
    url_identifier,
)

# Direct uploads above this size must go through the special upload URL
# (the /files endpoint rejects payloads over 32 MB).
_DIRECT_UPLOAD_MAX = 32 * 1024 * 1024
# Absolute cap accepted by the upload-URL flow.
_UPLOAD_MAX = 650 * 1024 * 1024


def _analysis_id_of(payload: Any) -> str:
    obj = data_of(payload)
    if isinstance(obj, dict):
        return str(obj.get("id") or "")
    return ""


def _is_vt_upload_url(url: str) -> bool:
    """Accept only HTTPS URLs on virustotal.com (or a subdomain).

    The one-time large-file upload URL from ``/files/upload_url`` is used
    with the ``x-apikey`` header attached - if a tampered/misconfigured
    response pointed elsewhere, posting there would exfiltrate the API key.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (
        host == "virustotal.com" or host.endswith(".virustotal.com")
    )


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register submission + analysis tools and return their definitions."""

    async def scan_vt_url(url: str) -> str:
        """Submit a URL to VirusTotal for analysis; returns the analysis id to poll."""
        url_value = (url or "").strip()
        if not url_value:
            return "Error: a URL is required."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/urls",
                _PROVIDER,
                f"submit URL {trunc(url_value, 120)}",
                headers=headers,
                data={"url": url_value},
            )
            if err:
                return err
            payload = resp.json() or {}
        analysis_id = _analysis_id_of(payload)
        if not analysis_id:
            return "URL submitted, but VirusTotal returned no analysis id."
        return (
            f"URL submitted for analysis.\nAnalysis id: {trunc(analysis_id, 120)}\n"
            "Poll get_vt_analysis with this id until status is 'completed', "
            "then fetch the verdict with get_vt_url_report."
        )

    async def scan_vt_file(
        file_id: str,
        password: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload a file attached in this conversation to VirusTotal for analysis."""
        file_id = (file_id or "").strip()
        if not file_id:
            return "Error: file_id (a file attached in this conversation) is required."
        if file_context is None:
            return "Error: no files are available in this conversation."
        try:
            resolved = await file_context.resolve_upload(file_id)
        except Exception as exc:  # pragma: no cover - defensive
            return f"Error: could not read attached file {trunc(file_id, 120)}: {trunc(exc, 200)}"
        if not resolved:
            return f"Error: file '{trunc(file_id, 120)}' is not available in this conversation."
        data, filename, mime = resolved
        # The attachment filename is user-controlled: the multipart
        # Content-Disposition filename takes a cut form, and the answers name
        # the file by its id - the name reaches the model only in the closing
        # quarantine block.
        display_name = trunc(filename, 120) or "file"
        subject = f"file '{trunc(file_id, 120)}'"
        if len(data) > _UPLOAD_MAX:
            return (
                f"Error: {subject} is {len(data)} bytes - VirusTotal "
                f"accepts uploads up to {_UPLOAD_MAX // 1024 // 1024} MB."
            )

        upload_endpoint = f"{api}/files"
        async with client(timeout=300) as c:
            if len(data) > _DIRECT_UPLOAD_MAX:
                # Large files: fetch a one-time upload URL first.
                resp, err = await safe_request(
                    c,
                    "get",
                    f"{api}/files/upload_url",
                    _PROVIDER,
                    "get large-file upload URL",
                    headers=headers,
                )
                if err:
                    return err
                upload_endpoint = str(data_of(resp.json() or {}) or "")
                # The x-apikey header rides along to this URL - only ever
                # post it back to VirusTotal itself, over TLS.
                if not _is_vt_upload_url(upload_endpoint):
                    return "Error: VirusTotal returned no usable upload URL."
            form: dict[str, Any] = {}
            if password.strip():
                form["password"] = password.strip()
            resp, err = await safe_request(
                c,
                "post",
                upload_endpoint,
                _PROVIDER,
                f"upload {subject}",
                headers=headers,
                files={"file": (display_name, data, mime or "application/octet-stream")},
                data=form or None,
            )
            if err:
                return err
            payload = resp.json() or {}
        analysis_id = _analysis_id_of(payload)
        size_kb = round(len(data) / 1024, 1)
        if not analysis_id:
            return uploaded_file_answer(
                f"Uploaded {subject} ({size_kb} KB), but VirusTotal returned no analysis id.",
                target=_PROVIDER,
                filename=filename,
            )
        return uploaded_file_answer(
            f"Uploaded {subject} ({size_kb} KB) for analysis.\n"
            f"Analysis id: {trunc(analysis_id, 120)}\n"
            "Poll get_vt_analysis with this id until status is 'completed', "
            "then fetch the verdict with get_vt_file_report (use the file's hash).",
            target=_PROVIDER,
            filename=filename,
        )

    async def rescan_vt_object(entity_type: str, entity_id: str) -> str:
        """Request a fresh analysis of a file, URL, domain, or IP already known to VirusTotal."""
        entity_type = (entity_type or "").strip().lower()
        path = ENTITY_PATHS.get(entity_type)
        if not path:
            return f"Error: entity_type must be one of {', '.join(sorted(ENTITY_PATHS))}."
        object_id = url_identifier(entity_id) if entity_type == "url" else (entity_id or "").strip()
        if not object_id:
            return "Error: entity_id is required."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/{path}/{quote(object_id, safe='')}/analyse",
                _PROVIDER,
                f"rescan {entity_type} {trunc(entity_id, 80)}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        analysis_id = _analysis_id_of(payload)
        if not analysis_id:
            return "Rescan requested, but VirusTotal returned no analysis id."
        return (
            f"Rescan of {entity_type} '{trunc(entity_id, 100)}' requested.\n"
            f"Analysis id: {trunc(analysis_id, 120)}\n"
            "Poll get_vt_analysis with this id until status is 'completed'."
        )

    async def get_vt_analysis(analysis_id: str) -> str:
        """Check the status and verdict stats of a submitted analysis."""
        analysis_id = (analysis_id or "").strip()
        if not analysis_id:
            return "Error: analysis_id is required (returned by scan/rescan tools)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/analyses/{quote(analysis_id, safe='')}",
                _PROVIDER,
                f"get analysis {trunc(analysis_id, 60)}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        attrs = attributes_of(obj if isinstance(obj, dict) else {})
        status = trunc(attrs.get("status"), 40) or "unknown"
        lines = [f"Analysis {trunc(analysis_id, 80)}: status {status}"]
        date = ts(attrs.get("date"))
        if date:
            lines.append(f"Submitted: {date} UTC")
        if status == "completed":
            lines.append(format_analysis_stats(attrs, key="stats"))
        else:
            lines.append("Not finished yet - poll again in a few seconds.")
        meta = payload.get("meta") if isinstance(payload, dict) else None
        if isinstance(meta, dict):
            file_info = meta.get("file_info")
            if isinstance(file_info, dict) and file_info.get("sha256"):
                lines.append(f"File SHA-256: {trunc(file_info['sha256'], 80)}")
            url_info = meta.get("url_info")
            if isinstance(url_info, dict) and url_info.get("url"):
                lines.append(f"URL: {trunc(url_info['url'], 200)}")
        return "\n".join(lines)

    executor.register("scan_vt_url", scan_vt_url)
    executor.register("scan_vt_file", scan_vt_file)
    executor.register("rescan_vt_object", rescan_vt_object)
    executor.register("get_vt_analysis", get_vt_analysis)

    return [
        ToolDefinition(
            name="scan_vt_url",
            description=(
                "Submit a URL to VirusTotal for analysis by 70+ URL scanners. "
                "Returns an analysis id - poll get_vt_analysis until "
                "completed, then read the verdict with get_vt_url_report. "
                "Only needed when get_vt_url_report says the URL was never "
                "analysed (or you need a fresh verdict - see rescan_vt_object)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to analyse"},
                },
                "required": ["url"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="scan_vt_file",
            description=(
                "Upload a file the user attached in this conversation to "
                "VirusTotal for analysis by 70+ antivirus engines and "
                "sandboxes. Pass the file_id from the available-files list; "
                "the original bytes are forwarded as-is (files over 32 MB "
                "automatically use the large-file upload flow, up to 650 MB). "
                "NOTE: uploaded files are shared with the VirusTotal "
                "community - never upload confidential documents. Returns an "
                "analysis id to poll with get_vt_analysis."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "id of a file attached in this conversation",
                    },
                    "password": {
                        "type": "string",
                        "description": "Password for protected ZIP archives (optional)",
                    },
                },
                "required": ["file_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="rescan_vt_object",
            description=(
                "Request a FRESH analysis of a file, URL, domain, or IP that "
                "VirusTotal already knows, without re-uploading anything. Use "
                "when the existing report is stale. Returns an analysis id to "
                "poll with get_vt_analysis."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": sorted(ENTITY_PATHS),
                        "description": "Type of the object to re-analyse",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Hash for files, raw URL for urls, domain name, or IP address",
                    },
                },
                "required": ["entity_type", "entity_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_vt_analysis",
            description=(
                "Check a submitted VirusTotal analysis by id (from "
                "scan_vt_url, scan_vt_file, or rescan_vt_object). Returns the "
                "status (queued / in-progress / completed) and, when "
                "completed, the detection stats plus the file hash or URL for "
                "the full report lookup."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "analysis_id": {
                        "type": "string",
                        "description": "Analysis id returned by a scan/rescan tool",
                    },
                },
                "required": ["analysis_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
