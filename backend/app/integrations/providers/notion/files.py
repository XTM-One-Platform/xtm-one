"""Notion file-upload tool (Direct Upload API)."""

from __future__ import annotations

import asyncio
import mimetypes
from typing import Any
from urllib.parse import unquote, urlparse

from app.core.color import format_hex
from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.file_delivery import untrusted_file_facts, uploaded_file_answer
from app.services.image_summary import (
    MAX_SUMMARY_PIXELS,
    ImageSummary,
    conflicting_gate_colors,
    format_visual_summary,
    looks_like_raster_image,
    parse_color_list,
    run_color_check,
    summarize_image,
)
from app.tools.base import FileContext, ToolDefinition, ToolResult

from ._common import (
    _BASE_URL,
    _NOTION_ALIGNMENT_NOTE,
    _NOTION_RICH_TEXT_LIMIT,
    _NOTION_VERSION,
    _PROVIDER,
    AutoRefreshClient,
    NotionContext,
    _alignment_argument_refusal,
    logger,
)

# ── File uploads (Direct Upload API) ────────────────────────────────────
# Notion's File Upload API attaches real, Notion-hosted files (downloadable
# in-app) to pages, blocks, and database "files" properties. The flow is:
#   1. POST /v1/file_uploads            -> create a File Upload object (get id)
#   2. POST /v1/file_uploads/{id}/send  -> send bytes as multipart/form-data
#      (POST .../complete for multi_part) - OR mode="external_url" to import a
#      publicly hosted file without sending the bytes ourselves.
#   3. Reference the id as {"type": "file_upload", "file_upload": {"id": ...}}
#      in a file/pdf/image/video/audio block or a "files" page property.
# Single-part uploads cap at 20 MB; larger files MUST use multi_part.
_NOTION_SINGLE_PART_LIMIT = 20 * 1024 * 1024  # 20 MB - Notion's single_part ceiling
# Each multi_part chunk must be 5-20 MB (except the final part). 10 MB keeps us
# safely inside that window and well under the 10,000-part cap.
_NOTION_MULTI_PART_CHUNK = 10 * 1024 * 1024
# Overall guard so a single tool call can't read an unbounded blob into memory.
_NOTION_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
# How long to wait for an external_url import to finish (status polling).
_NOTION_IMPORT_POLL_ATTEMPTS = 10
_NOTION_IMPORT_POLL_DELAY = 1.0  # seconds between status polls

# Block rendering choice for a page-body attachment. "auto" infers from the
# content type (pdf/image/video/audio render inline); "file" forces the
# COMPACT download row (filename + Download button) - what you usually want for
# a document like a PDF when an inline reader is too heavy.
_NOTION_FILE_BLOCK_TYPES = ("auto", "file", "pdf", "image", "video", "audio")
# Where on the page the uploaded file lands (the database files property is
# selected separately via ``property_name``).
_NOTION_ATTACH_TARGETS = ("block", "page_icon", "page_cover")


def _notion_file_block_type(content_type: str) -> str:
    """Map a MIME type to the Notion block type that best renders it.

    Notion has dedicated ``pdf``/``image``/``video``/``audio`` blocks and a
    generic ``file`` block. Picking the specific type gives an inline preview
    (e.g. an embedded PDF viewer) instead of a bare download chip.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct == "application/pdf":
        return "pdf"
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    if ct.startswith("audio/"):
        return "audio"
    return "file"


def _filename_from_url(url: str) -> str:
    """Best-effort filename from a URL path (for external_url imports)."""
    try:
        path = urlparse(url).path
    except ValueError:
        return ""
    return unquote(path.rsplit("/", 1)[-1]).strip()


# Hosts Notion serves uploaded / hosted files from (signed S3 buckets + Notion
# CDNs). ``download_notion_file`` restricts its server-side fetch to these so a
# crafted ``file_url`` can't turn the tool into an SSRF vector against internal
# services.
_NOTION_FILE_HOST_SUFFIXES = (
    "amazonaws.com",
    "notion.so",
    "notion-static.com",
    "notion.site",
)


def _is_notion_file_host(url: str) -> bool:
    """True if *url* points at a Notion-hosted/AWS file host (download allow-list)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _NOTION_FILE_HOST_SUFFIXES
    )


def _caption_rich_text(text: str) -> list[dict]:
    """Plain (non-markdown) rich-text array for a file block caption."""
    text = (text or "").strip()
    if not text:
        return []
    return [{"type": "text", "text": {"content": text[:_NOTION_RICH_TEXT_LIMIT]}}]


def register(ctx: NotionContext, executor, *, persist_file_fn=None) -> list[ToolDefinition]:
    _client = ctx.client
    _throttled_request = ctx.request
    headers = ctx.headers

    # ── File uploads (Direct Upload API) ────────────────────────────────

    async def _create_file_upload(
        client: AutoRefreshClient,
        *,
        mode: str,
        filename: str = "",
        content_type: str = "",
        number_of_parts: int = 0,
        external_url: str = "",
    ) -> tuple[dict | None, str | None]:
        """POST /v1/file_uploads — create a File Upload object."""
        payload: dict[str, Any] = {"mode": mode}
        if filename:
            payload["filename"] = filename
        if content_type:
            payload["content_type"] = content_type
        if mode == "multi_part":
            payload["number_of_parts"] = number_of_parts
        if mode == "external_url":
            payload["external_url"] = external_url
        resp, err = await _throttled_request(
            client,
            "post",
            f"{_BASE_URL}/file_uploads",
            _PROVIDER,
            "create file upload",
            json=payload,
        )
        if err:
            return None, err
        return resp.json(), None

    async def _send_file_upload_part(
        client: AutoRefreshClient,
        upload: dict,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        part_number: int | None = None,
    ) -> tuple[dict | None, str | None]:
        """POST .../send — transmit (a part of) the file as multipart/form-data.

        The send endpoint is the one Notion API that expects
        ``multipart/form-data`` instead of JSON, so the default JSON
        Content-Type header is dropped and httpx is left to set the multipart
        boundary itself.
        """
        upload_id = upload.get("id")
        send_url = upload.get("upload_url") or (
            f"{_BASE_URL}/file_uploads/{upload_id}/send"
        )
        send_headers = {
            "Authorization": headers["Authorization"],
            "Notion-Version": _NOTION_VERSION,
        }
        files = {
            "file": (
                filename or "upload",
                data,
                content_type or "application/octet-stream",
            )
        }
        form: dict[str, str] = {}
        if part_number is not None:
            form["part_number"] = str(part_number)
        resp, err = await _throttled_request(
            client,
            "post",
            send_url,
            _PROVIDER,
            "send file upload",
            headers=send_headers,
            files=files,
            data=form,
            timeout=120.0,
        )
        if err:
            return None, err
        return resp.json(), None

    async def _complete_file_upload(
        client: AutoRefreshClient,
        upload: dict,
    ) -> tuple[dict | None, str | None]:
        """POST .../complete — finalize a multi_part upload."""
        upload_id = upload.get("id")
        complete_url = upload.get("complete_url") or (
            f"{_BASE_URL}/file_uploads/{upload_id}/complete"
        )
        resp, err = await _throttled_request(
            client,
            "post",
            complete_url,
            _PROVIDER,
            "complete file upload",
            json={},
        )
        if err:
            return None, err
        return resp.json(), None

    async def _retrieve_file_upload(
        client: AutoRefreshClient,
        upload_id: str,
    ) -> tuple[dict | None, str | None]:
        """GET /v1/file_uploads/{id} — poll a (pending) upload's status."""
        resp, err = await _throttled_request(
            client,
            "get",
            f"{_BASE_URL}/file_uploads/{upload_id}",
            _PROVIDER,
            "retrieve file upload",
        )
        if err:
            return None, err
        return resp.json(), None

    def _verify_uploaded(
        result: dict | None, upload: dict
    ) -> tuple[str | None, str | None]:
        """Confirm a send/complete result reached ``uploaded`` status."""
        upload_id = (result or {}).get("id") or upload.get("id")
        if not upload_id:
            return None, "Error: Notion did not return a file upload id."
        status = (result or {}).get("status")
        if status and status != "uploaded":
            return None, (
                f"Error: Notion file upload status is '{status}', "
                "expected 'uploaded'."
            )
        return upload_id, None

    async def _await_import(
        client: AutoRefreshClient,
        upload: dict,
    ) -> tuple[str | None, str | None]:
        """Poll an external_url import until it is uploaded, failed, or times out."""
        upload_id = upload.get("id")
        if not upload_id:
            return None, "Error: Notion did not return a file upload id."
        status = upload.get("status")
        for _ in range(_NOTION_IMPORT_POLL_ATTEMPTS):
            if status == "uploaded":
                return upload_id, None
            if status == "failed":
                result = upload.get("file_import_result") or {}
                msg = (result.get("error") or {}).get("message") or ""
                detail = f": {msg}" if msg else ""
                return None, f"Error: Notion failed to import the file{detail}."
            await asyncio.sleep(_NOTION_IMPORT_POLL_DELAY)
            upload, err = await _retrieve_file_upload(client, upload_id)
            if err:
                return None, err
            status = (upload or {}).get("status")
        return None, (
            "Error: Notion is still importing the external file after "
            f"{_NOTION_IMPORT_POLL_ATTEMPTS} checks; try again shortly."
        )

    async def _upload_file_to_notion(
        client: AutoRefreshClient,
        *,
        data: bytes | None,
        filename: str,
        content_type: str,
        external_url: str = "",
    ) -> tuple[str | None, str | None]:
        """Run the full create -> send -> (complete) flow.

        Returns ``(file_upload_id, None)`` on success or ``(None, error)``.
        ``external_url`` takes precedence (Notion imports the file itself);
        otherwise *data* is uploaded directly — single_part when it fits in
        20 MB, else multi_part in 10 MB chunks.
        """
        if external_url:
            upload, err = await _create_file_upload(
                client,
                mode="external_url",
                external_url=external_url,
                filename=filename,
                content_type=content_type,
            )
            if err:
                return None, err
            return await _await_import(client, upload)

        if data is None:
            return None, "Error: no file bytes to upload."

        if len(data) <= _NOTION_SINGLE_PART_LIMIT:
            upload, err = await _create_file_upload(
                client,
                mode="single_part",
                filename=filename,
                content_type=content_type,
            )
            if err:
                return None, err
            sent, err = await _send_file_upload_part(
                client,
                upload,
                data=data,
                filename=filename,
                content_type=content_type,
            )
            if err:
                return None, err
            return _verify_uploaded(sent, upload)

        # The full payload already lives in memory as ``data``; slice each part
        # lazily inside the loop (part count via ceil division) instead of
        # pre-building a list of every chunk. Materializing all slices at once
        # would add a second full-size copy and roughly double peak RSS for a
        # large (up to 100 MB) file.
        num_parts = (
            len(data) + _NOTION_MULTI_PART_CHUNK - 1
        ) // _NOTION_MULTI_PART_CHUNK
        upload, err = await _create_file_upload(
            client,
            mode="multi_part",
            filename=filename,
            content_type=content_type,
            number_of_parts=num_parts,
        )
        if err:
            return None, err
        for idx in range(1, num_parts + 1):
            start = (idx - 1) * _NOTION_MULTI_PART_CHUNK
            chunk = data[start : start + _NOTION_MULTI_PART_CHUNK]
            _, err = await _send_file_upload_part(
                client,
                upload,
                data=chunk,
                filename=filename,
                content_type=content_type,
                part_number=idx,
            )
            if err:
                return None, (
                    f"Uploaded {idx - 1}/{num_parts} parts to Notion, "
                    f"then failed: {err}"
                )
        completed, err = await _complete_file_upload(client, upload)
        if err:
            return None, err
        return _verify_uploaded(completed, upload)

    async def _attach_file_upload(
        client: AutoRefreshClient,
        *,
        page_id: str,
        upload_id: str,
        content_type: str,
        filename: str,
        caption: str,
        property_name: str,
        block_type: str = "auto",
        attach_as: str = "block",
        after_block_id: str = "",
    ) -> tuple[str | None, str | None]:
        """Attach an uploaded file to a page (block / icon / cover) or property.

        Returns ``(error, new_block_id)``: ``error`` is ``None`` on success;
        ``new_block_id`` is the created block's ID (page-body block mode only,
        so the caller can chain ordering) or ``None``. Attach target
        precedence: ``page_icon`` / ``page_cover`` > ``property_name`` >
        page-body block.
        """
        file_upload_ref = {"type": "file_upload", "file_upload": {"id": upload_id}}
        if attach_as == "page_icon":
            _, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "set page icon",
                json={"icon": dict(file_upload_ref)},
            )
            return err, None
        if attach_as == "page_cover":
            _, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "set page cover",
                json={"cover": dict(file_upload_ref)},
            )
            return err, None
        if property_name:
            entry = dict(file_upload_ref)
            if filename:
                entry["name"] = filename
            _, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "set file property",
                json={"properties": {property_name: {"files": [entry]}}},
            )
            return err, None
        # Block mode. ``auto`` infers the block type from the content type
        # (pdf/image/video/audio -> inline preview); an explicit ``block_type``
        # wins, so block_type="file" forces the COMPACT download row even for a
        # PDF (Notion renders a pdf/image/etc. block as a big inline reader).
        resolved_type = (
            _notion_file_block_type(content_type) if block_type == "auto" else block_type
        )
        inner = dict(file_upload_ref)
        caption_rt = _caption_rich_text(caption)
        if caption_rt:
            inner["caption"] = caption_rt
        # Only the generic ``file`` block accepts a display ``name``; the
        # pdf/image/video/audio blocks reject it.
        if resolved_type == "file" and filename:
            inner["name"] = filename
        body: dict[str, Any] = {
            "children": [{"type": resolved_type, resolved_type: inner}]
        }
        # ``after`` (a sibling block ID) inserts the block right AFTER that
        # block instead of at the end of the page - the only way to place a
        # file under a specific heading (Notion has no move-block API). Without
        # it every upload lands at the bottom of the page.
        if after_block_id:
            body["after"] = after_block_id
        resp, err = await _throttled_request(
            client,
            "patch",
            f"{_BASE_URL}/blocks/{page_id}/children",
            _PROVIDER,
            "attach file block",
            json=body,
        )
        if err:
            return err, None
        new_block_id = None
        try:
            results = resp.json().get("results", [])
            if results:
                new_block_id = results[0].get("id")
        except (ValueError, AttributeError):  # pragma: no cover - defensive
            new_block_id = None
        return None, new_block_id

    def _upload_error(msg: str) -> ToolResult:
        """Flag an upload failure so it is recorded as a tool error, not success."""
        return ToolResult(tool_call_id="", content=msg, is_error=True)

    async def upload_notion_file(
        page_id: str = "",
        file_id: str = "",
        external_url: str = "",
        property_name: str = "",
        caption: str = "",
        filename: str = "",
        block_type: str = "auto",
        attach_as: str = "block",
        after_block_id: str = "",
        alignment: str = "",
        expect_colors: object = None,
        forbid_colors: object = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Upload a file to Notion and attach it to a page or database row.

        ``alignment`` is deliberately NOT in the tool schema: Notion exposes no
        alignment (left / center / right) for image or media blocks through
        its API - it is a UI-only setting. The parameter exists so a
        model that guesses it is REFUSED with the explicit message
        (``_alignment_argument_refusal``) instead of having the argument
        silently dropped by the executor's unknown-kwargs filter (which
        uploaded the file and let the agent report an alignment it never
        applied). Every image block this tool attaches ends with
        ``_NOTION_ALIGNMENT_NOTE`` for the same reason.

        A raster image uploaded by ``file_id`` is SUMMARISED server-side:
        the result carries its dimensions and the saturated colours
        it actually contains with their share, as text that outlives the
        pixels in the agent's context. ``expect_colors`` / ``forbid_colors``
        (hex lists) turn that summary into a gate run BEFORE the upload - a
        forbidden colour present, or an expected one absent, is a refusal
        naming the colour and its share, so a wrong brand colour cannot reach
        the page through this tool. The gate is refused, never skipped, when
        it cannot run: with ``external_url`` (Notion imports that file itself,
        no bytes pass through here), with a file that is not a raster image
        this platform can decode, with an expected and a forbidden colour too
        close to tell apart (their match regions overlap), and - for
        ``forbid_colors`` - with an animated image, one too large or too
        colourful to count every pixel (sampled) or one whose exact palette
        had to be cut, where absence is not provable.

        This is the way to put a REAL, downloadable file (PDF, image, or any
        binary) into Notion — e.g. attaching a report PDF the agent generated.

        Provide exactly ONE source:
          - ``file_id``: a file attached to OR generated in this conversation
            (forwarded to Notion byte-for-byte — never re-authored). This is
            how you attach a PDF/file the agent just produced.
          - ``external_url``: a publicly reachable HTTPS URL; Notion imports the
            file itself (nothing is downloaded through this server).

        Target (``page_id`` is required — a page ID, or a container block such
        as a toggle/column):
          - Default (``attach_as="block"``): the file is appended to the page
            body as a downloadable block, with an optional ``caption``.
            ``block_type`` controls the rendering:
              * ``"auto"`` (default) — infer from the content type: PDFs,
                images, video and audio render as an INLINE preview/reader.
              * ``"file"`` — a COMPACT download row (filename + Download
                button), no inline reader. Use this for a PDF/document when an
                inline reader is too heavy — this is usually the "just a normal
                file" behavior people want.
              * ``"pdf"``/``"image"``/``"video"``/``"audio"`` — force that
                specific inline block type.
          - ``property_name``: instead attach the file to that ``files``-type
            property on a database row (this REPLACES the property's current
            files). Use the page-body block mode to accumulate several files.
          - ``attach_as="page_icon"`` / ``"page_cover"``: set the page's icon
            or cover image to the uploaded file (use an image).

        PLACEMENT: by default the file block is appended at the END of the page.
        To place it directly UNDER a specific block (e.g. a period heading),
        pass ``after_block_id`` = that block's ID (find it with
        ``list_notion_block_children``). Notion has no move-block API, so the
        only way to position a file is at insert time. The tool returns the new
        block's ID; to stack a second file right after the first, pass that
        returned ID as the next call's ``after_block_id``.

        ``filename`` overrides the stored/display name. Files up to 20 MB upload
        in one request; larger files (up to 100 MB) are sent in parts
        automatically. Requires the integration's "Insert content" capability
        and that the page is shared with the integration.
        """
        page_id = (page_id or "").strip()
        file_id = (file_id or "").strip()
        external_url = (external_url or "").strip()
        property_name = (property_name or "").strip()
        block_type = (block_type or "auto").strip().lower()
        attach_as = (attach_as or "block").strip().lower()
        after_block_id = (after_block_id or "").strip()

        # Refused BEFORE the upload so nothing lands on the page under a false
        # promise; the agent resends the same call without the argument and
        # tells the user.
        alignment_refusal = _alignment_argument_refusal(
            alignment, "the uploaded file", nothing_done="nothing has been uploaded"
        )
        if alignment_refusal:
            return _upload_error(alignment_refusal)
        if not page_id:
            return _upload_error(
                "Error: page_id is required (a Notion page ID, or a container "
                "block ID such as a toggle or column)."
            )
        if file_id and external_url:
            return _upload_error(
                "Error: provide EITHER file_id (a file from this conversation) "
                "OR external_url (a public link) — not both."
            )
        if not file_id and not external_url:
            return _upload_error(
                "Error: provide file_id (a file attached or generated in this "
                "conversation) or external_url (a publicly reachable HTTPS link "
                "for Notion to import)."
            )
        if block_type not in _NOTION_FILE_BLOCK_TYPES:
            return _upload_error(
                f"Error: block_type must be one of "
                f"{', '.join(_NOTION_FILE_BLOCK_TYPES)} (got '{block_type}')."
            )
        if attach_as not in _NOTION_ATTACH_TARGETS:
            return _upload_error(
                f"Error: attach_as must be one of "
                f"{', '.join(_NOTION_ATTACH_TARGETS)} (got '{attach_as}')."
            )
        if property_name and attach_as != "block":
            return _upload_error(
                "Error: property_name attaches the file to a database property; "
                "it cannot be combined with attach_as="
                f"'{attach_as}'. Drop one of them."
            )
        if after_block_id and (attach_as != "block" or property_name):
            return _upload_error(
                "Error: after_block_id only positions a page-body file block; "
                "it cannot be combined with property_name or "
                "attach_as=page_icon/page_cover."
            )

        # The colour gate is parsed up front so a malformed list is
        # refused before any bytes are read, and a check that cannot run is
        # refused rather than skipped: silently uploading past a check the
        # agent asked for is the failure this argument exists to prevent.
        expected, err = parse_color_list(expect_colors, what="expect_colors")
        if err:
            return _upload_error(err)
        forbidden, err = parse_color_list(forbid_colors, what="forbid_colors")
        if err:
            return _upload_error(err)
        conflict = conflicting_gate_colors(expected, forbidden)
        if conflict:
            return _upload_error(f"{conflict} Nothing has been uploaded.")
        color_check = bool(expected or forbidden)
        if color_check and external_url:
            return _upload_error(
                "Error: expect_colors / forbid_colors check the file's pixels, "
                "which only file_id provides - Notion imports an external_url "
                "itself and nothing passes through this server. Nothing has been "
                "uploaded. Upload the image by file_id (download it first if it "
                "only exists at that URL), or drop the colour arguments."
            )

        data: bytes | None = None
        content_type = ""
        is_raster = False
        summary: ImageSummary | None = None
        # The answers name the file by its file_id: its stored name is a third
        # party's text, shown only in the closing quarantine block.
        label = "the external_url file" if external_url else f"file {file_id}"
        if external_url:
            if not external_url.lower().startswith("https://"):
                return _upload_error(
                    "Error: external_url must be a publicly reachable HTTPS URL."
                )
            # Fall back to a safe display name so we never send an empty
            # filename to Notion or surface a confusing "Uploaded ''" message
            # (e.g. when the URL ends in "/" with no explicit filename given).
            filename = (
                filename or _filename_from_url(external_url) or "upload"
            ).strip()
            content_type = mimetypes.guess_type(filename)[0] or ""
        else:
            if file_context is None:
                return _upload_error(
                    f"Error: file '{file_id}' is not available — no files are "
                    "attached to this conversation."
                )
            try:
                resolved = await file_context.resolve_upload(file_id)
            except Exception as exc:  # pragma: no cover - defensive
                return _upload_error(
                    f"Error: could not read attached file {file_id}: {exc}"
                )
            if not resolved:
                return _upload_error(
                    f"Error: file '{file_id}' is not available in this "
                    "conversation. Use a file_id from the Available Files list."
                )
            data, resolved_name, content_type = resolved
            filename = (filename or resolved_name or "upload").strip()
            if len(data) > _NOTION_MAX_UPLOAD_BYTES:
                return _upload_error(
                    f"Error: {label} is "
                    f"{round(len(data) / 1024 / 1024, 1)} MB, over the "
                    f"{_NOTION_MAX_UPLOAD_BYTES // 1024 // 1024} MB upload limit."
                )
            # The summary is computed for every raster image, gate or not: it
            # is what the agent (and whoever reads the trace) checks the
            # published picture against once the pixels are out of context.
            is_raster = looks_like_raster_image(data, content_type)
            if is_raster:
                summary = await summarize_image(data)
            if color_check:
                if not is_raster:
                    return _upload_error(
                        f"Error: the colour check could not run - {label} "
                        "is not a raster image "
                        "this platform can decode (PNG, JPEG, WebP, GIF, BMP, "
                        "TIFF). Nothing has been uploaded. Render it to PNG first, "
                        "or drop expect_colors / forbid_colors for a non-image file."
                    )
                if summary is None:
                    # A raster by its bytes that still yielded nothing: corrupt
                    # or truncated, over the pixel cap, or the analysis itself
                    # failed. Named as such - "not an image" would send the
                    # agent to re-render a file whose format was never the problem.
                    return _upload_error(
                        f"Error: the colour check could not run - {label} looks "
                        "like a raster image but could not be analysed (corrupt or "
                        "truncated bytes, or more than "
                        f"{MAX_SUMMARY_PIXELS // 1_000_000} megapixels). Nothing has "
                        "been uploaded. Re-render it as a PNG of a reasonable size, or "
                        "drop expect_colors / forbid_colors."
                    )
                problems = await run_color_check(summary, expect=expected, forbid=forbidden)
                if problems:
                    return _upload_error(
                        f"Error: refused to upload {label} - "
                        + "; ".join(problems)
                        + ". Nothing has been uploaded. Fix the rendering, LOOK at "
                        "the new PNG (read_file_content) and compare it with the "
                        "user's last screenshot, then call again. "
                        + format_visual_summary(summary)
                    )

        # Once the bytes were read, every outcome - success, a failed send, a
        # file uploaded but not attached - carries the summary: the agent's
        # next move (retry, tell the user) is about that image either way.
        visual_tail = f" {format_visual_summary(summary)}" if is_raster else ""
        async with _client() as client:
            upload_id, err = await _upload_file_to_notion(
                client,
                data=data,
                filename=filename,
                content_type=content_type,
                external_url=external_url,
            )
            if err:
                return _upload_error(f"{err}{visual_tail}")
            err, new_block_id = await _attach_file_upload(
                client,
                page_id=page_id,
                upload_id=upload_id,
                content_type=content_type,
                filename=filename,
                caption=caption,
                property_name=property_name,
                block_type=block_type,
                attach_as=attach_as,
                after_block_id=after_block_id,
            )
            if err:
                return _upload_error(
                    uploaded_file_answer(
                        f"Uploaded {label} to Notion (file id {upload_id}) but "
                        f"could not attach it: {err}{visual_tail}",
                        target=_PROVIDER,
                        filename=filename,
                        content_type=content_type,
                    )
                )

        alignment_note = ""
        if attach_as == "page_icon":
            target = "icon"
        elif attach_as == "page_cover":
            target = "cover"
        elif property_name:
            target = f"property '{property_name}'"
        else:
            rendered = (
                _notion_file_block_type(content_type)
                if block_type == "auto"
                else block_type
            )
            placement = f" after block {after_block_id}" if after_block_id else ""
            target = f"page body (as a {rendered} block{placement})"
            # An image block is the one media block whose alignment the user
            # notices (a wide diagram lands centered, caption centered) and
            # the one they will ask the agent to fix - say up front that the
            # API cannot. Compact ``file`` rows and full-width
            # pdf/video/audio readers have no alignment to speak of.
            if rendered == "image":
                alignment_note = f" {_NOTION_ALIGNMENT_NOTE}"
        size_note = f" ({round(len(data) / 1024, 1)} KB)" if data is not None else ""
        chain_hint = (
            f" New block id: {new_block_id} "
            "(pass it as after_block_id to place the next file right after this one)."
            if new_block_id
            else ""
        )
        visual_note = ""
        if is_raster:
            # ``None`` reads "unavailable": an image Notion accepted but this
            # platform could not decode is still something the agent should
            # know it has not been able to check. The alignment note
            # keeps closing the answer's own text, so the summary sits before it.
            visual_note = visual_tail
            if color_check and summary is not None:
                checks = []
                if expected:
                    checks.append(
                        "expected " + ", ".join(format_hex(rgb) for rgb in expected) + " present"
                    )
                if forbidden:
                    checks.append(
                        "forbidden " + ", ".join(format_hex(rgb) for rgb in forbidden) + " absent"
                    )
                visual_note += " Colour check passed: " + "; ".join(checks) + "."
        return uploaded_file_answer(
            f"Uploaded {label}{size_note} to Notion and attached it to the "
            f"{target} of {page_id}.{chain_hint}{visual_note}{alignment_note}",
            target=_PROVIDER,
            filename=filename,
            content_type=content_type,
        )

    async def download_notion_file(
        block_id: str = "",
        file_url: str = "",
        filename: str = "",
    ) -> str | ToolResult:
        """Download a Notion-hosted file's RAW BYTES and register it as a reusable file_id.

        This is the bridge for "the file/evidence lives in Notion". Notion only
        hands out a short-lived **signed download URL** (there is no "give me the
        bytes" API), and that URL is too long / expiry-bound for other tools to
        fetch themselves. This tool fetches the bytes **server-side**
        (byte-for-byte, no truncation, no URL-length cap) and stores them as a
        downloadable file, returning a ``file_id`` you can forward to any tool
        that accepts a file — e.g. attach as Drata evidence via
        ``source_file_id``.

        Provide EITHER:
          - ``block_id``: a Notion file/pdf/image/video/audio block (PREFERRED —
            a fresh signed URL is resolved automatically). Find it with
            ``get_notion_page`` / ``list_notion_block_children``.
          - ``file_url``: a Notion file URL you already hold (signed S3 / Notion
            CDN). Only Notion-hosted / AWS file hosts are allowed.

        ``filename`` overrides the saved name. Returns a ``[[FILE:id]]`` download
        card plus the file_id to forward.
        """
        from app.llm.base import FileRef

        block_id = (block_id or "").strip()
        file_url = (file_url or "").strip()
        filename = (filename or "").strip()
        if block_id and file_url:
            return _upload_error(
                "Error: provide EITHER block_id OR file_url, not both."
            )
        if not block_id and not file_url:
            return _upload_error(
                "Error: provide block_id (a Notion file block) or file_url (a "
                "Notion file URL) to download."
            )

        url = file_url
        derived_name = filename
        web_url = ""
        if block_id:
            async with _client() as client:
                resp, err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/blocks/{block_id}",
                    _PROVIDER,
                    "retrieve file block",
                )
            if err:
                return _upload_error(err)
            block = resp.json()
            btype = block.get("type", "")
            block_data = block.get(btype) if isinstance(block.get(btype), dict) else {}
            if btype not in ("file", "pdf", "image", "video", "audio"):
                return _upload_error(
                    f"Error: block {block_id} is a '{btype}' block, not a "
                    "downloadable file/pdf/image/video/audio block."
                )
            fileobj = block_data.get("file") or block_data.get("external") or {}
            url = (fileobj.get("url") or "").strip()
            if not url:
                return _upload_error(
                    f"Error: block {block_id} has no downloadable file URL."
                )
            if not derived_name:
                derived_name = (block_data.get("name") or "").strip()
            parent_page = str((block.get("parent") or {}).get("page_id") or "").replace("-", "")
            if parent_page:
                web_url = (
                    f"https://www.notion.so/{parent_page}#{str(block.get('id') or block_id).replace('-', '')}"
                )

        if not url.lower().startswith("https://"):
            return _upload_error(
                "Error: the file URL must be an HTTPS Notion-hosted file URL."
            )
        if not _is_notion_file_host(url):
            host = urlparse(url).hostname or "?"
            return _upload_error(
                f"Error: refusing to download from '{host}' — only Notion-hosted "
                "files (Notion / AWS storage) can be fetched byte-for-byte. For an "
                "external link, use the URL directly."
            )

        # Fetch with a plain client: the signed URL carries its own auth in the
        # query string, so the Notion bearer token must NOT be sent to S3.
        async with egress_client(follow_redirects=True, timeout=120.0) as dl:
            resp, err = await _throttled_request(
                dl, "get", url, _PROVIDER, "download file bytes"
            )
        if err:
            return _upload_error(err)
        content = resp.content
        if not content:
            return _upload_error("Error: the downloaded Notion file is empty.")
        if len(content) > _NOTION_MAX_UPLOAD_BYTES:
            return _upload_error(
                f"Error: file is {round(len(content) / 1024 / 1024, 1)} MB, over "
                f"the {_NOTION_MAX_UPLOAD_BYTES // 1024 // 1024} MB download limit."
            )

        derived_name = (
            derived_name or _filename_from_url(url) or "notion-download"
        ).strip()
        mime = (
            resp.headers.get("content-type", "").split(";", 1)[0].strip()
            or mimetypes.guess_type(derived_name)[0]
            or "application/octet-stream"
        )

        file_ref = None
        if persist_file_fn is not None:
            try:
                file_ref = await persist_file_fn(derived_name, content, mime)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Notion file persistence failed: %s", format_exception_message(exc))
                file_ref = None
        if file_ref is None:
            # No persistence scope (e.g. evaluation) - fall back to an inline
            # FileRef so the bytes are still forwardable within this run.
            file_ref = FileRef.from_bytes(
                data=content, filename=derived_name, content_type=mime
            )
        facts = untrusted_file_facts(
            source="Notion",
            filename=derived_name,
            content_type=mime,
            links=[
                ("Web URL", web_url),
                ("Download URL (signed by Notion, expires after about an hour)", url),
            ],
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Downloaded a {round(len(content) / 1024, 1)} KB file from Notion "
                f"as a reusable file. [[FILE:{file_ref.file_id}]]\n"
                "Forward it to another tool "
                f"with file_id={file_ref.file_id} (e.g. attach it as evidence via "
                "source_file_id), and include the [[FILE:...]] marker verbatim so "
                f"the user gets a download card.\n{facts}"
            ),
            files=[file_ref],
        )

    executor.register("upload_notion_file", upload_notion_file)
    executor.register("download_notion_file", download_notion_file)

    return [
        ToolDefinition(
            name="upload_notion_file",
            description=(
                "Upload a REAL, downloadable file (PDF, image, or any binary) to "
                "Notion and attach it to a page or database row — the way to put "
                "an actual file into Notion (e.g. attach a generated report PDF "
                "so it is downloadable in Notion, not just referenced as text). "
                "Provide EITHER file_id (a file attached to or generated in this "
                "conversation — forwarded byte-for-byte) OR external_url (a public "
                "HTTPS link Notion imports itself). By default the file is "
                "appended to the page body as a block. IMPORTANT: set "
                "block_type='file' for a COMPACT download row (filename + Download "
                "button) — use this for a 'normal file'/document like a PDF when "
                "you do NOT want a big inline PDF reader; block_type='auto' "
                "renders PDFs/images/video/audio inline. Set property_name to "
                "attach to a database row's files property, or "
                "attach_as='page_icon'/'page_cover' to set the page icon/cover. "
                "By default the block is appended at the END of the page; pass "
                "after_block_id (e.g. a heading's block ID) to place it directly "
                "UNDER that block instead. The returned new block ID can be fed "
                "back as after_block_id to stack files in order. "
                "Handles files up to 100 MB (multi-part automatically). "
                "Alignment (left/center/right) is a UI-only setting in Notion "
                "and cannot be set through the API: an uploaded image block is "
                "centered by default and the user aligns it in Notion (hover the "
                "image > alignment icon) - do not pass an alignment. "
                "RENDERED IMAGES (a diagram, a screenshot): LOOK at the PNG with "
                "read_file_content in this run and compare it with the user's "
                "last screenshot before uploading - a DOM/geometry probe is not a "
                "visual check, and never upload an image you could not view. "
                "The result carries a visual summary (dimensions, saturated "
                "colours with their share); pass the brand hex values as "
                "expect_colors / forbid_colors so a wrong colour is refused "
                "server-side (file_id uploads of raster images only)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": (
                            "Target Notion page ID (or a container block ID such "
                            "as a toggle/column) to attach the file to."
                        ),
                    },
                    "file_id": {
                        "type": "string",
                        "description": (
                            "ID of a file attached to or generated in this "
                            "conversation (from the Available Files list). "
                            "Forwarded to Notion byte-for-byte. Mutually "
                            "exclusive with external_url."
                        ),
                    },
                    "external_url": {
                        "type": "string",
                        "description": (
                            "Publicly reachable HTTPS URL for Notion to import "
                            "the file from. Mutually exclusive with file_id."
                        ),
                    },
                    "block_type": {
                        "type": "string",
                        "enum": list(_NOTION_FILE_BLOCK_TYPES),
                        "description": (
                            "Page-body rendering (attach_as='block'). 'file' = "
                            "compact download row (best for a plain document/PDF "
                            "with no inline reader); 'auto' (default) renders "
                            "pdf/image/video/audio inline; or force "
                            "'pdf'/'image'/'video'/'audio'."
                        ),
                    },
                    "attach_as": {
                        "type": "string",
                        "enum": list(_NOTION_ATTACH_TARGETS),
                        "description": (
                            "Where the file lands on the page: 'block' (default, "
                            "page body), 'page_icon', or 'page_cover'. For a "
                            "database 'files' property use property_name instead."
                        ),
                    },
                    "after_block_id": {
                        "type": "string",
                        "description": (
                            "Optional (page-body block mode). ID of the block to "
                            "insert the file block right AFTER - e.g. a heading, "
                            "so the file lands UNDER it instead of at the bottom "
                            "of the page (find it with list_notion_block_children). "
                            "The tool returns the new block's ID; pass it here on "
                            "the next call to stack files in order."
                        ),
                    },
                    "property_name": {
                        "type": "string",
                        "description": (
                            "Optional. Name of a 'files'-type property on a "
                            "database row to attach the file to (replaces that "
                            "property's files). Omit to append the file as a "
                            "block to the page body."
                        ),
                    },
                    "caption": {
                        "type": "string",
                        "description": ("Optional caption shown under the file block (page-body mode only)."),
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Optional override for the file's display name (include an extension, e.g. report.pdf)."
                        ),
                    },
                    "expect_colors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional (file_id raster images only). Hex colours "
                            "that MUST be present in the image, e.g. the brand "
                            'colours of a logo: ["#0015A8", "#7B2CBF"]. The upload '
                            "is refused, naming the colour, when one is absent. "
                            "Saturated colours only: a grey, white or near-black "
                            "entry is refused (the dominant colour is reported in "
                            "the visual summary instead)."
                        ),
                    },
                    "forbid_colors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional (file_id raster images only). Hex colours "
                            "that must NOT appear, e.g. a wrong logo colour: "
                            '["#0FBCFF"]. The upload is refused, naming the '
                            "colour and its share of the image, when one is present. "
                            "Saturated colours only (a grey, white or near-black "
                            "entry is refused); a still image, not an animation."
                        ),
                    },
                },
                "required": ["page_id"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="download_notion_file",
            # ``filename`` only names the saved copy: a rename is the same download.
            cosmetic_arguments=("filename",),
            workspace_ingest=True,
            description=(
                "Download a Notion-hosted file's RAW BYTES and register it as a "
                "reusable file_id (with a download card). Use this when a file "
                "lives in Notion (e.g. a PDF attached to a page) and you need to "
                "forward it to ANOTHER tool: Notion only exposes a short-lived "
                "signed URL (not the bytes), and that URL is too long / "
                "expiry-bound for other tools to fetch. This fetches the bytes "
                "server-side - byte-for-byte, no truncation, no URL-length cap - "
                "and returns a file_id you can pass to any tool that accepts a "
                "file (e.g. attach as Drata evidence via source_file_id). Provide "
                "block_id (a Notion file/pdf/image/video/audio block - preferred) "
                "OR file_url (a Notion file URL you already hold)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {
                        "type": "string",
                        "description": (
                            "Notion file/pdf/image/video/audio block ID to "
                            "download (preferred - a fresh signed URL is resolved "
                            "automatically). Find it with get_notion_page / "
                            "list_notion_block_children. Mutually exclusive with "
                            "file_url."
                        ),
                    },
                    "file_url": {
                        "type": "string",
                        "description": (
                            "A Notion-hosted file URL you already hold (signed S3 "
                            "/ Notion CDN). Only Notion / AWS file hosts are "
                            "allowed. Mutually exclusive with block_id."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional override for the saved file name.",
                    },
                },
                "required": [],
            },
            source="integration:notion",
            priority=1,
        ),
    ]
