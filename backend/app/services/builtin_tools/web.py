# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Web fetch and browser automation tool builders.

Moved verbatim from the original ``app/services/builtin_tools.py`` module
as part of the package split (mechanical refactor - no behavior change).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message, safe_uuid
from app.tools.base import CallableToolExecutor, ToolDefinition, register_fetched_file

from ._shared import format_file_size, logger, persist_tool_artifact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import FileRef
    from app.services.browser_profiles.agent_access import ProfileAccess
    from app.tools.base import FileContext, ToolResult

# ── Web Fetch ────────────────────────────────────────────────────────────────

#: How a fetched response is kept. An HTML page is the source of the
#: text the result carries: a working file (a compact chip, no link added to a
#: channel reply that does not name it). Any other response is a document the
#: user may want: a download card, like an integration's download. Either is a
#: FETCHED file: shown only by a reply that names it.
PAGE_FILE_SOURCE = "agent_working"
DOCUMENT_FILE_SOURCE = "agent_download"

#: How long a fetch is answered from Redis (per conversation or run, URL and
#: ``max_chars``), with the file it kept.
WEB_FETCH_CACHE_TTL_SECONDS = 900

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: Common spellings of a type the platform's MIME table names otherwise.
_MIME_ALIASES = {
    "application/csv": "text/csv",
    "application/javascript": "text/javascript",
    "application/x-javascript": "text/javascript",
    "image/jpg": "image/jpeg",
}


def _base_mime(content_type: str | None) -> str:
    """The type a fetched response is read, stored and named with - from a closed set.

    The header is the fetched server's, and what it declares is stored with
    the file and printed outside the untrusted-content block, so no token of
    it is taken as it is: a type of the platform's own MIME table
    (``file_storage.MIME_TYPES``, plus ``application/xhtml+xml`` and
    ``_MIME_ALIASES``), a structured-syntax suffix type as its syntax
    (``application/rss+xml`` is ``application/xml``, ``application/ld+json``
    ``application/json``), any other ``text/*`` as ``text/plain``, and
    anything else as no type (``""``).
    """
    from app.services.file_storage import MIME_TYPES

    mime = (content_type or "").split(";", 1)[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    if mime in MIME_TYPES.values() or mime == "application/xhtml+xml":
        return mime
    if mime.endswith("+json"):
        return "application/json"
    if mime.endswith("+xml"):
        return "application/xml"
    return "text/plain" if mime.startswith("text/") else ""


def _safe_name(name: str | None) -> str:
    return _SAFE_NAME_RE.sub("-", name or "").strip("-.")


def is_html_page(content_type: str | None) -> bool:
    """The test ``fetch_and_extract`` extracts a response as a page by (``is_html_media_type``).

    The same parsed media type ``_base_mime`` stores the file as: a page is
    never kept as anything but ``text/html``, and a header that merely
    contains ``text/html`` is no page.
    """
    from app.services.web_fetch import is_html_media_type

    return is_html_media_type(content_type)


def fetched_filename(url: str, content_type: str | None) -> str:
    """The name a fetched response is kept under.

    The URL's last path segment (else its host), reduced to safe characters -
    the name reaches the model outside the untrusted-content block - with the
    extension of its type when it carries none of it (``report`` served as a
    PDF is ``report.pdf``; the extension is what the text extraction reads a
    file by).
    """
    from urllib.parse import unquote, urlparse

    from app.services.file_storage import MIME_TYPES

    parsed = urlparse(url)
    segment = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    stem = _safe_name(segment) or _safe_name(parsed.hostname) or "download"
    mime = _base_mime(content_type)
    if "." in stem and MIME_TYPES.get(stem.rsplit(".", 1)[-1].lower()) == mime:
        return stem[:200]
    extension = next((ext for ext, known in MIME_TYPES.items() if known == mime), "")
    return f"{stem[:190]}.{extension}" if extension else stem[:200]


def fetched_document_text(
    body: bytes, filename: str, content_type: str | None, limit: int, code_interpreter: bool
) -> tuple[str | None, int, str]:
    """``(text, total, reason)``: what a web_fetch result shows of a non-HTML response.

    The text is the attachment reader's (``attachment_extract.inline_attachment_text``,
    the rule of a chat upload): a PDF or an Office document extracted, a JSON
    or CSV file verbatim within *limit* and summarised past it, cut at *limit*
    with *total* the whole text's length. A text response is handed to the
    reader as UTF-8, decoded first in the charset it declares
    (``decode_attachment_text``), so a response in another charset reads as
    it did before and is summarised like its UTF-8 twin. ``text`` is ``None``
    for a response that holds none, and *reason* says what it is. Runs on the
    parse executor.
    """
    from app.integrations.file_content import DOCUMENT_EXTENSIONS, TEXT_EXTENSIONS, TEXT_MIME_PREFIXES
    from app.services.attachment_extract import (
        decode_attachment_text,
        inline_attachment_text,
        is_html_attachment,
        is_json_attachment,
    )
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

    mime = _base_mime(content_type)
    lower = filename.lower()
    if lower.endswith(DOCUMENT_EXTENSIONS):
        if len(body) > MAX_UPLOAD_SIZE_BYTES:
            return None, 0, f"a document over the {format_file_size(MAX_UPLOAD_SIZE_BYTES)} file limit"
    else:
        if mime.startswith("image/") and mime != "image/svg+xml":
            return None, 0, "an image"
        if not (
            mime.startswith(TEXT_MIME_PREFIXES)
            or mime.endswith(("+json", "+xml"))
            or mime == "image/svg+xml"
            or lower.endswith(TEXT_EXTENSIONS)
        ):
            return None, 0, "binary content"
        decoded = decode_attachment_text(
            body,
            content_type,
            html=is_html_attachment(filename, content_type),
            utf8_first=is_json_attachment(filename, content_type),
        )
        body, content_type = decoded.encode("utf-8"), mime
    view = inline_attachment_text(body, filename, content_type, code_interpreter, limit)
    if not view.stripped_total:
        return None, 0, "a document with no extractable text"
    return view.text, view.total, ""


def build_web_fetch_tool(
    *,
    db: AsyncSession | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the web page fetching tool (+ ``fetch_image_asset``).

    Allows agents to fetch and read the content of any public web page.
    Uses the shared :mod:`app.services.web_fetch` module which is also
    used by the KB crawl endpoint and website sync, ensuring a single
    extraction pipeline across the platform.

    The persistence context (``db`` / ``user_id`` / ``conversation_id`` /
    ``assignment_run_id``) is what ``fetch_image_asset`` stores the downloaded
    image under; the catalog builds the suite without one and the tool then
    refuses at call time.

    ``web_fetch`` keeps every response it reads as a file of the same scope:
    the result names its ``file_id`` and ``[[FILE:id]]`` marker, the
    agent can hand it to any tool that takes a file, and the user gets a chip.
    An HTML page's text is the page extraction it always was; any other
    response is read through the attachment reader instead of being decoded
    as text. Byte-identical responses reuse their file within a conversation
    (``_persist_generated_file``). A session with neither a conversation nor a
    run (an evaluation) keeps the file in memory for the run: stored, it would
    be a file no message and no run ever shows.
    """
    has_scope = db is not None and bool(conversation_id or assignment_run_id)
    scope_label = "conversation" if conversation_id else "run"
    if conversation_id:
        cache_scope = f"conversation:{conversation_id}"
    elif assignment_run_id:
        cache_scope = f"run:{assignment_run_id}"
    else:
        cache_scope = "none"

    async def _persist_fetched(filename: str, data: bytes, content_type: str, *, source: str) -> FileRef | None:
        # Registers nothing: the handler registers the kept file once, after
        # ``_canonical_kept_type`` has retyped a reused row.
        if not has_scope:
            return None
        return await persist_tool_artifact(
            db=db,
            user_id=user_id or "",
            filename=filename,
            raw_bytes=data,
            content_type=content_type or None,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            source=source,
        )

    async def _load_kept_file(file_id: str) -> FileRef | None:
        """The file a cached fetch kept, while it is still a file of this scope."""
        from sqlalchemy import select

        from app.llm.base import FileRef
        from app.models.file_attachment import FileAttachment

        from . import _shared

        file_uuid = safe_uuid(file_id)
        conv_uuid = safe_uuid(conversation_id)
        run_uuid = safe_uuid(assignment_run_id)
        if file_uuid is None or (conv_uuid is None and run_uuid is None):
            return None
        scoped = (
            FileAttachment.conversation_id == conv_uuid
            if conv_uuid is not None
            else FileAttachment.assignment_run_id == run_uuid
        )
        try:
            async with _shared._own_session() as own_db:
                row = (
                    await own_db.execute(select(FileAttachment).where(FileAttachment.id == file_uuid, scoped))
                ).scalar_one_or_none()
                return FileRef.from_attachment(row) if row is not None else None
        except Exception as exc:
            logger.debug("web_fetch: cached file %s not reloaded: %s", file_id, format_exception_message(exc))
            return None

    async def _canonical_kept_type(ref: FileRef, content_type: str) -> FileRef:
        """*ref*, re-typed as *content_type* when the row it reused holds a type outside the closed set.

        A byte-identical re-fetch hands back the row the conversation already
        holds, typed however it was first stored (the server's
        header as it was). Such a type would ride the file's metadata into
        every later listing, so it is rewritten to the one the fetch read the
        same bytes as. A type of the closed set is the row's and is kept. The
        caller registers what this returns: the ref the turn holds carries a
        closed-set type even when the rewrite fails.
        """
        import dataclasses

        from sqlalchemy import update

        from app.models.file_attachment import FileAttachment

        from . import _shared

        if ref.content_type == content_type or _base_mime(ref.content_type) == ref.content_type:
            return ref
        try:
            async with _shared._own_session() as own_db:
                await own_db.execute(
                    update(FileAttachment).where(FileAttachment.id == safe_uuid(ref.file_id)).values(content_type=content_type)
                )
                await own_db.commit()
        except Exception as exc:
            logger.warning(
                "web_fetch: the type of reused file %s was not rewritten: %s", ref.file_id, format_exception_message(exc)
            )
        return dataclasses.replace(ref, content_type=content_type)

    def _kept_file_line(ref: FileRef, *, page: bool, stored: bool) -> str:
        # Outside the untrusted-content block: the type is printed from the
        # closed set ``_base_mime`` reads a server's header into.
        described = (
            f"{ref.filename} ({_base_mime(ref.content_type) or 'application/octet-stream'}, "
            f"{format_file_size(ref.size)})"
        )
        if not stored:
            why = "it could not be stored" if has_scope else "this session cannot store files"
            return (
                f"Kept for this run only ({why}): {described} - file_id {ref.file_id}. "
                "Pass the file_id to any tool that takes a file."
            )
        read = (
            f' read_file_content(file_id="{ref.file_id}", mode="extract_structured") reads the '
            "page's embedded data and its whole text."
            if page
            else ""
        )
        return (
            f"Kept as a file of this {scope_label}: {described} - file_id {ref.file_id} - download marker "
            f"[[FILE:{ref.file_id}]].{read} Pass the file_id to any tool that takes a file; "
            "put the marker in your reply to hand the file to the user."
        )

    def _notes(
        ref: FileRef | None, *, stored: bool, page: bool, not_kept: str, no_text: str, shown: int, total: int
    ) -> str:
        """The platform's lines after the untrusted-content block, built from facts, never replayed.

        A cached fetch keeps these facts, not the lines, so every answer -
        fresh or cached - is written by this formatter from the file as it is
        now, and the truncation footer names the code interpreter only when
        this turn offers it.
        """
        from app.services.attachment_extract import inline_truncation_footer
        from app.services.code_interpreter import code_interpreter_offered
        from app.tools.base import offered_tools

        notes: list[str] = []
        if ref is not None:
            notes.append(_kept_file_line(ref, page=page, stored=stored))
        elif not_kept:
            notes.append(f"Not kept as a file: {not_kept}; the URL above is its link.")
        if no_text:
            notes.append(
                f"No text to show: the response is {no_text}."
                + (f' read_file_content(file_id="{ref.file_id}") opens it.' if ref is not None else "")
            )
        elif ref is not None and total > shown:
            notes.append(
                inline_truncation_footer(
                    file_id=ref.file_id,
                    filename=ref.filename,
                    content_type=_base_mime(ref.content_type),
                    shown=shown,
                    total=total,
                    code_interpreter=code_interpreter_offered(offered_tools()),
                )
            )
        return "\n".join(notes)

    async def _web_fetch(
        url: str,
        max_chars: int = 80000,
        sanitize: bool = False,
        file_context: FileContext | None = None,
    ) -> str:
        import hashlib
        import json

        from app.core.parse_offload import run_parse
        from app.integrations.file_content import keep_file
        from app.services.code_interpreter import code_interpreter_offered
        from app.services.content_safety import wrap_untrusted
        from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES
        from app.services.web_fetch import fetch_and_extract, sanitize_html
        from app.tools.base import offered_tools

        # Validate URL
        if not url.startswith(("http://", "https://")):
            return "Invalid URL: must start with http:// or https://"

        # Clamp max_chars
        max_chars = max(1_000, min(max_chars, 200_000))

        def _answer(page: str, notes: str) -> str:
            # Quarantine the fetched content so embedded instructions cannot
            # masquerade as prompt text (indirect prompt injection); the lines
            # naming the kept file are the platform's and stay outside it.
            wrapped = wrap_untrusted(page, source=f"web page {url}")
            return f"{wrapped}\n\n{notes}" if notes else wrapped

        # Redis cache (15 min), per file scope: an entry names the file its
        # fetch kept, which only this conversation or run may be handed. The
        # page is stored unwrapped - every call gets a fresh boundary - and the
        # platform's lines are rebuilt from the entry's facts (``_notes``).
        scope = f"{cache_scope}\n{max_chars}\n{url}" + ("\nsanitize" if sanitize else "")
        cache_key = "web_fetch:v4:" + hashlib.sha256(scope.encode()).hexdigest()
        redis = None
        try:
            from app.core.redis import redis_client

            redis = await redis_client()
            cached = await redis.get(cache_key)
            if cached:
                entry = json.loads(cached if isinstance(cached, str) else cached.decode("utf-8"))
                cached_ref = await _load_kept_file(entry["file_id"]) if entry.get("file_id") else None
                if not entry.get("file_id") or cached_ref is not None:
                    if cached_ref is not None:
                        # Through the same retyping as a fresh fetch, to the type
                        # that fetch read the bytes as: the row is re-read, and a
                        # rewrite that failed when the entry was written must not
                        # put its type in the turn's FileContext.
                        cached_ref = await _canonical_kept_type(cached_ref, str(entry["content_type"]))
                        register_fetched_file(file_context, cached_ref)
                    return _answer(
                        entry["page"],
                        _notes(
                            cached_ref,
                            stored=cached_ref is not None,
                            page=bool(entry["is_page"]),
                            not_kept=str(entry["not_kept"]),
                            no_text=str(entry["no_text"]),
                            shown=int(entry["shown"]),
                            total=int(entry["total"]),
                        ),
                    )
        except Exception:
            redis = None

        from app.services.sandbox_workspace import preview

        # A started sandbox service: read through the platform's OpenSandbox
        # proxy with its key, and never cached: the next read follows the next fix.
        is_preview = await preview.allowed(url)
        try:
            if is_preview:
                result = await preview.fetch(url, max_chars=max_chars)
            else:
                result = await fetch_and_extract(url, max_chars=max_chars)
        except Exception as exc:
            # The URL is named in the answer so the recorded tool-error friction keeps the host.
            return f"Failed to fetch URL: {url}: {exc}{_tls_hint(exc)}{_refusal_hint(exc, offered_tools())}"

        page = is_html_page(result.content_type)
        storage_type = _base_mime(result.content_type) or "application/octet-stream"
        filename = fetched_filename(result.url, result.content_type)
        body = result.body or b""
        # ``sanitize``: the kept page (and its ``to_workspace_path`` write) loses what runs or
        # fetches, so it is a test fixture as written; the text read below is the same.
        sanitized = bool(sanitize and page and body)
        if sanitized:
            body = await run_parse(sanitize_html, body)
        ref: FileRef | None = None
        stored = False
        not_kept = ""
        if not body:
            not_kept = "the response has no body"
        elif len(body) > MAX_UPLOAD_SIZE_BYTES:
            not_kept = (
                f"it is {format_file_size(len(body))}, over the "
                f"{format_file_size(MAX_UPLOAD_SIZE_BYTES)} file limit"
            )
        else:

            async def _persist(name: str, data: bytes, content_type: str):
                return await _persist_fetched(
                    name, data, content_type, source=PAGE_FILE_SOURCE if page else DOCUMENT_FILE_SOURCE
                )

            kept = await keep_file(_persist, file_context, filename, body, storage_type)
            stored = bool(getattr(kept, "s3_object_name", None))
            if stored:
                kept = await _canonical_kept_type(kept, storage_type)
                register_fetched_file(file_context, kept)
            # A file held in memory is only a file if a later tool can find it.
            ref = kept if stored or file_context is not None else None

        parts = [f"# {result.title}" if result.title else f"# {result.url}"]
        parts.append(f"URL: {result.url}")
        # Each redirect followed, as the server answered it (status and Location), then the final status;
        # a page reached without a redirect reads as it always did.
        hops = getattr(result, "redirects", None) or []
        for hop in hops:
            parts.append(f"Redirect: {hop.status} {hop.url} -> {hop.location}")
        if hops:
            parts.append(f"Status: {result.status_code}")
        parts.append(f"Content-Type: {result.content_type}")
        if sanitized:
            parts.append("Kept file: sanitized (script, style, svg, img, link, iframe and media elements dropped).")
        elif sanitize:
            parts.append("Kept file: as fetched (sanitize applies to an HTML page only).")
        no_text = ""
        shown = total = 0
        if page:
            if result.truncated:
                parts.append(f"Note: Content was truncated to {max_chars} characters.")
            parts.append("")
            parts.append(result.text)
        else:
            text, total, reason = await run_parse(
                fetched_document_text,
                body,
                filename,
                result.content_type,
                max_chars,
                code_interpreter_offered(offered_tools()),
            )
            if text is None:
                no_text = reason
            else:
                shown = len(text)
                if total > shown and ref is None:
                    parts.append(f"Note: Content was truncated to {shown} of {total} characters.")
                parts.append("")
                parts.append(text)
        output = "\n".join(parts)

        # A file held in memory is this call's alone: the next call keeps its
        # own, so only a result a later call can replay is cached.
        if (stored or ref is None) and not is_preview:
            try:
                if redis is None:
                    from app.core.redis import redis_client

                    redis = await redis_client()
                entry = {
                    "page": output,
                    "file_id": ref.file_id if ref is not None else None,
                    "content_type": storage_type,
                    "is_page": page,
                    "not_kept": not_kept,
                    "no_text": no_text,
                    "shown": shown,
                    "total": total,
                }
                await redis.set(cache_key, json.dumps(entry), ex=WEB_FETCH_CACHE_TTL_SECONDS)
            except Exception:
                message = "Failed to cache tool output for key %s"
                logger.debug(message, cache_key, exc_info=True)

        return _answer(
            output,
            _notes(ref, stored=stored, page=page, not_kept=not_kept, no_text=no_text, shown=shown, total=total),
        )

    executor = CallableToolExecutor()
    executor.register("web_fetch", _web_fetch)

    definition = ToolDefinition(
        name="web_fetch",
        description=(
            "Fetch and read the content of a public web page or document. "
            "Returns the main text content of a page with navigation, ads, and "
            "boilerplate stripped, and the extracted text of a document (PDF, "
            "Office, CSV, JSON...). Use this when you need to read the content "
            "at a specific URL — for example to check a link shared by the "
            "user, read documentation, or gather information from a web page. "
            "Every response is also kept as a file of this conversation: the "
            "result names its file_id, which any tool that takes a file "
            "accepts, and the [[FILE:...]] marker that hands it to the user."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to fetch (must start with http:// or https://).",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum characters to return (default 80000). "
                        "Reduce for faster responses when you only need a summary."
                    ),
                    "default": 80000,
                },
                "sanitize": {
                    "type": "boolean",
                    "description": "HTML page: keep it without script, style, svg, img, link, iframe, media (a test fixture).",
                },
            },
            "required": ["url"],
        },
        source="builtin:web",
        workspace_ingest=True,
    )

    # ``fetch_image_asset`` rides the web_fetch suite: fetching a
    # picture off the web is the same capability as fetching a page, stored
    # as a file instead of read as text.
    from .image_assets import build_fetch_image_asset_tool

    asset_defs, _same_executor = build_fetch_image_asset_tool(
        db=db,
        user_id=user_id,
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
        executor=executor,
    )

    return [definition, *asset_defs], executor


#: The statuses a site answers a script it will not serve (bot protection, a
#: sign-in wall, a rate limit), as opposed to a page that does not exist.
_REFUSAL_STATUSES = frozenset({401, 403, 429, 451})


def _tls_hint(exc: BaseException) -> str:
    """What a certificate verification failure means for the next call.

    The platform always verifies TLS certificates: a host serving a self-signed
    or privately issued certificate fails every retry the same way, so the hint
    says so instead of letting the model retry.
    """
    import ssl

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(current):
            return (
                " The host's TLS certificate is not trusted (self-signed or privately issued): "
                "web_fetch verifies certificates and a retry fails the same way."
            )
        current = current.__cause__ or current.__context__
    return ""


def _refusal_hint(exc: BaseException, tools: Any) -> str:
    """What to do next when the site refused the fetch rather than failed it.

    A publisher that blocks scripted clients answers 401, 403 or 429 to the
    platform's User-Agent; retrying ``web_fetch`` gets the same answer. The
    browser tool renders the page as a person's browser does, so it is named
    when the loop offers it (``offered_tools``), and never otherwise.
    """
    import httpx

    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code not in _REFUSAL_STATUSES:
        return ""
    status = exc.response.status_code
    hint = f" The site refused this client (HTTP {status}): retrying web_fetch gets the same answer."
    if any(
        getattr(tool, "name", None) == "browser" and getattr(tool, "source", None) == "builtin:web"
        for tool in tools or ()
    ):
        return hint + " Open the page with the browser tool (navigate, then get_content)."
    return hint + " Use another source for the same content."


# ── Browser Automation ───────────────────────────────────────────────────────


def download_page_count(body: bytes, content_type: str) -> int | None:
    """The page count of a downloaded PDF (``None`` for another type or a PDF pypdf cannot open)."""
    if content_type != "application/pdf" and not body.startswith(b"%PDF"):
        return None
    try:
        import io

        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(body)).pages)
    except Exception:
        return None


#: The actions that act on the page rather than read it: refused in a read-only profile session.
#: ``hover`` dispatches pointer events a page may act on; ``viewport``, ``console`` and an element
#: ``screenshot`` only read it; ``download`` runs a script only with an ``expression`` (refused there).
BROWSER_INTERACTIVE_ACTIONS = frozenset({"click", "type", "press", "hover", "drag", "evaluate"})

#: A profile's session status as the ``profiles`` action words it (``models.browser_profile.SESSION_STATUSES``).
_PROFILE_SESSION_TEXT = {
    "none": "no saved session",
    "unverified": "session saved, not checked yet",
    "active": "signed in",
    "expired": "session expired (it signs in again by itself when it can)",
    "awaiting_mfa": "waiting for a person's MFA code",
    "signing_in": "signing in",
    "failed": "the last sign-in failed",
}


def build_browser_tool(
    session_key: str,
    *,
    db: AsyncSession | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    profile_access: ProfileAccess | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the browser automation tool.

    Provides agents with a headless Chromium browser they can control
    to navigate web pages, interact with elements, extract content,
    take screenshots, and evaluate JavaScript.

    Uses the shared :mod:`app.services.browser_tool` module for session
    management and Playwright orchestration.

    Args:
        session_key: Unique key for browser session (typically
            ``"{agent_id}:{conversation_id}"``).
        db / user_id / conversation_id / assignment_run_id: persistence
            context. When provided, the ``screenshot`` action saves the PNG
            as a durable ``FileAttachment`` (linked to the conversation /
            assignment run) and returns a Markdown image the agent can
            include verbatim so the screenshot renders inline — exactly like
            ``generate_image``. Without it the action degrades to a textual
            note instead of silently discarding the capture.
        profile_access: the browser profiles the run may open
            (``browser_profiles.agent_access``). With it the tool
            gains the ``profiles`` action and ``navigate``'s ``profile``
            argument; a read-only binding refuses
            :data:`BROWSER_INTERACTIVE_ACTIONS`.
    """
    profiles_on = profile_access is not None

    async def _persist_screenshot(
        png_bytes: bytes, file_context: FileContext | None, *, show_to_user: bool = True
    ) -> str | ToolResult:
        """Persist screenshot bytes and return an inline-image ToolResult.

        Every branch ends with the visual summary: dimensions and the
        saturated colours the PNG actually carries, as TEXT. The vision block
        is the first thing the context budget prunes; this line stays
        in the transcript, so an unexpected hex - a cyan mark where the brand
        blue belongs - is checkable long after the pixels are gone, and
        without them at all when storage is unavailable.

        *show_to_user*: the "include this EXACT Markdown in your
        response" instruction is for a screenshot the human should see. An
        agent capturing a figure it is about to place on a slide or upload
        passes ``False`` and gets the file id and the summary without it -
        otherwise every intermediate capture of a deck rebuild is pasted into
        the reply.
        """
        from app.services.image_summary import (
            BLANK_DOMINANT_SHARE,
            format_visual_summary,
            looks_blank,
            summarize_image,
        )

        size = len(png_bytes)
        summary = await summarize_image(png_bytes)
        summary_line = format_visual_summary(summary)
        if looks_blank(summary):
            # A page that did not paint (still loading, content drawn on entering the view) is not a capture to
            # judge a screen on: said in the line that outlives the pixels.
            summary_line += (
                f" This capture may be blank: one colour covers over {BLANK_DOMINANT_SHARE:.0%} of it and it holds "
                "no saturated colour and almost no text or line. Check the page has painted (wait, scroll, or "
                "capture the viewport) before relying on it."
            )
        # Persist whenever there is a DB session AND something to scope the
        # file by: an owner (``user_id``) OR a conversation / assignment run.
        # Company-managed assignment runs intentionally have no file owner
        # (``_resolve_run_file_owner_id`` returns ``None``) but still provide
        # an ``assignment_run_id`` scope, so gating on ``user_id`` alone would
        # wrongly discard their screenshots.
        if db is None or not (user_id or conversation_id or assignment_run_id):
            return (
                f"Screenshot captured ({size} bytes), but it could not be "
                "saved because no file-storage context is available in this "
                f"run. {summary_line}"
            )
        from app.tools.base import ToolResult as _ToolResult

        try:
            import uuid as _uuid

            file_ref = await persist_tool_artifact(
                db=db,
                user_id=user_id or "",
                filename=f"screenshot_{_uuid.uuid4().hex[:8]}.png",
                raw_bytes=png_bytes,
                content_type="image/png",
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                file_context=file_context,
                # A capture the human is not shown is a working file: a compact
                # chip in the web chat, no link appended to a channel reply.
                source="agent" if show_to_user else "agent_working",
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist browser screenshot: %s", format_exception_message(exc), exc_info=True
            )
            return (
                f"Screenshot captured ({size} bytes) but saving it failed: "
                f"{format_exception_message(exc)} {summary_line}"
            )

        url = f"/api/v1/chat/files/{file_ref.file_id}/download"
        if show_to_user:
            handover = (
                "Also include this EXACT Markdown in your response "
                "(use the relative path verbatim, do NOT prepend any domain "
                f"or hostname) so the image renders for the human: "
                f"![Screenshot]({url})\n\n"
            )
        else:
            handover = (
                f"file_id {file_ref.file_id} - a working capture, not shown to the "
                "user unless you place it yourself (an image block, an upload).\n\n"
            )
        return _ToolResult(
            tool_call_id="",
            content=(
                f"Screenshot captured ({size} bytes) and saved as a file. "
                "The PNG is attached as a native vision block - you can see "
                "the pixels. Read the page yourself (URL bar, labels, errors). "
                "Never ask the user to describe or verify it. "
                + handover
                + f"{summary_line} This line outlives the pixels in your context: "
                "check its hex values against the colours the rendering should "
                "have before you publish the image anywhere."
            ),
            files=[file_ref],
        )

    async def _persist_recording(
        recorded: Any, file_context: FileContext | None, *, show_to_user: bool = True
    ) -> str | ToolResult:
        """Keep a ``record`` action's MP4 as a file of the conversation or run and say what the page painted."""
        painted = (
            f"{recorded.frames} frame(s) painted in {recorded.seconds:g} s ({recorded.painted_fps:.1f} fps painted, "
            f"{recorded.width}x{recorded.height}, {recorded.url})"
        )
        if db is None or not (user_id or conversation_id or assignment_run_id):
            return _failed_action(
                f"Recorded {painted}, but it could not be kept: no file-storage context is available."
            )
        import uuid as _uuid

        from app.tools.base import ToolResult as _ToolResult

        try:
            file_ref = await persist_tool_artifact(
                db=db,
                user_id=user_id or "",
                filename=f"recording_{_uuid.uuid4().hex[:8]}.mp4",
                raw_bytes=recorded.mp4,
                content_type="video/mp4",
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                file_context=file_context,
                source="agent" if show_to_user else "agent_working",
            )
        except Exception as exc:
            logger.warning("Failed to persist a browser recording: %s", format_exception_message(exc), exc_info=True)
            return _failed_action(f"Recorded {painted} but keeping it failed: {format_exception_message(exc)}")
        return _ToolResult(
            tool_call_id="",
            content=(
                f"Recorded {painted}: an MP4 of {format_file_size(len(recorded.mp4))} - file_id {file_ref.file_id} - "
                f"download marker [[FILE:{file_ref.file_id}]]. A low painted rate on a page that animates means the "
                "page itself dropped frames; a still page paints few frames."
                + (f" While it recorded, {lane}." if (lane := getattr(recorded, "lane", "")) else "")
                + (" Put the marker in your reply to hand the video to the user." if show_to_user else "")
            ),
            files=[file_ref],
        )

    async def _download(
        url: str,
        expression: str | None,
        file_context: FileContext | None,
        method: str | None = None,
        body: Any = None,
    ) -> str | ToolResult:
        """The ``download`` action: the response the page fetches, kept as a file of the conversation or run."""
        import json

        from app.services.browser_tool import (
            DOWNLOAD_METHODS,
            EvaluateTimeoutError,
            browser_download,
            session_profile,
        )
        from app.tools.base import ToolResult as _ToolResult

        verb = (method or "GET").strip().upper()
        if verb not in DOWNLOAD_METHODS:
            return _failed_action(
                f"Browser action 'download' refused: method '{method}' is not one of {', '.join(DOWNLOAD_METHODS)}."
            )
        payload: str | None = None
        if body is not None and body != "":
            if verb != "POST":
                return _failed_action(
                    "Browser action 'download' refused: 'body' is sent with method POST only; pass method='POST'."
                )
            if isinstance(body, str):
                try:
                    json.loads(body)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'download' refused: 'body' is not valid JSON ({exc}).")
                payload = body
            else:
                payload = json.dumps(body)
        current = session_profile(session_key)
        if current is not None and current.read_only and (expression or verb != "GET"):
            what = "a download's 'expression' runs a script in the page" if expression else "a POST may change state"
            return _failed_action(
                f"The browser is signed in with the browser profile '{current.name}', which this agent opens "
                f"read-only: {what} and is refused. Download with a GET and no expression."
            )
        try:
            fetched = await browser_download(session_key, url, expression, method=verb, body=payload)
        except (ValueError, EvaluateTimeoutError) as exc:
            return _failed_action(f"Browser action 'download' refused: {exc}")
        content_type = _base_mime(fetched.content_type) or "application/octet-stream"
        filename = fetched_filename(fetched.url, content_type)
        described = f"{filename} ({content_type}, {format_file_size(len(fetched.body))})"
        pages = download_page_count(fetched.body, content_type)
        page_line = f" A PDF of {pages} page(s)." if pages is not None else ""
        if db is None or not (user_id or conversation_id or assignment_run_id):
            return _failed_action(
                f"Downloaded {described} from {fetched.url}, but it could not be kept: no file-storage context is "
                "available in this run."
            )
        try:
            file_ref = await persist_tool_artifact(
                db=db,
                user_id=user_id or "",
                filename=filename,
                raw_bytes=fetched.body,
                content_type=content_type,
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                file_context=file_context,
                source="agent_working",
                fetched=True,
            )
        except Exception as exc:
            logger.warning("Failed to persist a browser download: %s", format_exception_message(exc), exc_info=True)
            return _failed_action(f"Downloaded {described} but keeping it failed: {format_exception_message(exc)}")
        follow = (
            f' read_file_content(file_id="{file_ref.file_id}") gives its text; '
            f'render_source_pages(file_id="{file_ref.file_id}") shows its pages as images.'
            if pages is not None
            else f' read_file_content(file_id="{file_ref.file_id}") opens it.'
        )
        return _ToolResult(
            tool_call_id="",
            content=(
                f"Downloaded {fetched.url} and kept it as a working file: {described} - file_id "
                f"{file_ref.file_id} - download marker [[FILE:{file_ref.file_id}]].{page_line}{follow} Pass the "
                "file_id to any tool that takes a file; put the marker in your reply to hand it to the user."
            ),
            files=[file_ref],
        )

    def _failed_action(content: str) -> ToolResult:
        """A failed browser action, flagged as one.

        None of the dispatcher's failure sentences is error-shaped for
        ``is_error_shaped_result``, so returned as a plain string each would be
        recorded as a successful call.
        """
        from app.tools.base import ToolResult as _ToolResult

        return _ToolResult(tool_call_id="", content=content, is_error=True)

    async def _list_profiles() -> str | ToolResult:
        from app.services.browser_profiles.agent_access import usable_profiles
        from app.services.browser_profiles.profiles import effective_session_status
        from app.services.browser_tool import session_profile

        if profile_access is None:
            return "This agent is bound to no browser profile: it browses without a sign-in."
        if not profile_access.user_id:
            return "Browser profiles open only for a person, and this run acts for nobody: it browses without a sign-in."
        rows = await usable_profiles(profile_access)
        if not rows:
            return _failed_action(
                "None of the browser profiles bound to this agent is one the person it acts for may use."
            )
        lines = []
        for row in rows:
            status = _PROFILE_SESSION_TEXT.get(effective_session_status(row), "no saved session")
            mode = "read-only" if profile_access.is_read_only(row.id) else "full access"
            lines.append(f"- **{row.name}** (`{row.id}`): {row.start_url} - {status}; {mode}")
        hidden = len(profile_access.profile_ids) - len(rows)
        if hidden > 0:
            lines.append(f"({hidden} more bound profile(s) the person this run acts for may not use.)")
        current = session_profile(session_key)
        if current is not None:
            lines.append(f"The browser is signed in with **{current.name}** now.")
        return (
            "Browser profiles you may browse signed in with (navigate with the profile argument set to a name or id):\n"
            + "\n".join(lines)
        )

    async def _open_profile(reference: str) -> Any:
        from app.services.browser_profiles.agent_access import ProfileReferenceError, resolve_profile
        from app.services.browser_profiles.agent_sessions import ProfileSessionError, open_profile_session
        from app.services.browser_tool import install_session, session_profile

        try:
            row = await resolve_profile(profile_access, reference)
            current = session_profile(session_key)
            if current is not None and current.profile_id == str(row.id):
                return current
            session = await open_profile_session(row, access=profile_access)
        except (ProfileReferenceError, ProfileSessionError) as exc:
            return _failed_action(str(exc))
        await install_session(session_key, session)
        return session.profile

    async def _save_session(reference: str | None) -> str | ToolResult:
        from app.services.browser_profiles.agent_access import ProfileReferenceError, resolve_profile
        from app.services.browser_profiles.agent_sessions import ProfileSessionError, save_browser_session
        from app.services.browser_tool import capture_session

        if not reference:
            return _failed_action("'profile' is required for the save_session action: the profile to save into.")
        try:
            row = await resolve_profile(profile_access, reference)
            captured = await capture_session(session_key)
            return await save_browser_session(row, captured, access=profile_access)
        except (ProfileReferenceError, ProfileSessionError) as exc:
            return _failed_action(str(exc))

    async def _browser(
        action: str,
        url: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        key: str | None = None,
        expression: str | None = None,
        full_page: bool = False,
        show_to_user: bool = True,
        profile: str | None = None,
        timeout: float | None = None,
        width: int | None = None,
        height: int | None = None,
        level: str | None = None,
        method: str | None = None,
        body: Any = None,
        dx: float | None = None,
        dy: float | None = None,
        steps: int | None = None,
        reduced_motion: str | None = None,
        color_scheme: str | None = None,
        reset: bool | None = None,
        seconds: float | None = None,
        offline: bool | None = None,
        scale: float | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        from app.services.browser_tool import action_order, take_notice

        # In the order the agent called them: a navigate and an evaluate of one turn never swap (UX run cdecbc8a).
        async with action_order(session_key):
            result = await _act(
                action,
                url,
                selector,
                text,
                key,
                expression,
                full_page,
                show_to_user,
                profile,
                timeout,
                width,
                height,
                level,
                file_context,
                method,
                body,
                dx,
                dy,
                steps,
                reduced_motion,
                color_scheme,
                reset,
                seconds,
                offline,
                scale=scale,
            )
        # A session re-created since the last action says so first: never a blank page taken for the old one.
        notice = take_notice(session_key)
        if notice is None:
            return result
        if isinstance(result, str):
            return f"{notice}\n{result}"
        result.content = f"{notice}\n{result.content}"
        return result

    async def _act(
        action: str,
        url: str | None,
        selector: str | None,
        text: str | None,
        key: str | None,
        expression: str | None,
        full_page: bool,
        show_to_user: bool,
        profile: str | None,
        timeout: float | None,
        width: int | None,
        height: int | None,
        level: str | None,
        file_context: FileContext | None,
        method: str | None = None,
        body: Any = None,
        dx: float | None = None,
        dy: float | None = None,
        steps: int | None = None,
        reduced_motion: str | None = None,
        color_scheme: str | None = None,
        reset: bool | None = None,
        seconds: float | None = None,
        offline: bool | None = None,
        scale: float | None = None,
    ) -> str | ToolResult:
        from app.services.browser_tool import (
            EvaluateTimeoutError,
            browser_click,
            browser_close,
            browser_console,
            browser_drag,
            browser_emulate_media,
            browser_evaluate,
            browser_get_content,
            browser_hover,
            browser_navigate,
            browser_network,
            browser_press,
            browser_record,
            browser_screenshot,
            browser_set_viewport,
            browser_snapshot,
            browser_type,
            session_profile,
        )

        try:
            if action == "profiles":
                return await _list_profiles()
            if action == "save_session" and profiles_on:
                return await _save_session(profile)
            if profile and action != "navigate":
                return _failed_action(
                    "'profile' opens a signed-in session with the navigate action only"
                    + (" (and names the profile save_session saves into)." if profiles_on else ".")
                )
            if action in BROWSER_INTERACTIVE_ACTIONS:
                current = session_profile(session_key)
                if current is not None and current.read_only:
                    return _failed_action(
                        f"The browser is signed in with the browser profile '{current.name}', which this agent "
                        f"opens read-only: '{action}' is refused. Navigate to URLs and read the pages (get_content, "
                        "snapshot, screenshot), or close the session to browse without a sign-in."
                    )
            if action == "navigate":
                if profile:
                    from app.services.browser_profiles.agent_sessions import ProfileSessionInfo

                    opened = await _open_profile(profile)
                    if not isinstance(opened, ProfileSessionInfo):
                        return opened
                    mode = "read-only" if opened.read_only else "full access"
                    result = await browser_navigate(session_key, url or opened.start_url, timeout)
                    return f"Signed in with the browser profile '{opened.name}' ({mode}).\n{result}"
                if not url:
                    return "Error: 'url' is required for the navigate action."
                return await browser_navigate(session_key, url, timeout)

            elif action == "get_content":
                return await browser_get_content(session_key)

            elif action == "snapshot":
                return await browser_snapshot(session_key, selector or "")

            elif action == "click":
                if not selector:
                    return "Error: 'selector' is required for the click action."
                return await browser_click(session_key, selector)

            elif action == "type":
                if not selector or text is None:
                    return (
                        "Error: 'selector' and 'text' are required for the type action."
                    )
                return await browser_type(session_key, selector, text)

            elif action == "press":
                if not key:
                    return "Error: 'key' is required for the press action."
                try:
                    return await browser_press(session_key, key)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'press' refused: {exc}")

            elif action == "hover":
                if not selector:
                    return "Error: 'selector' is required for the hover action."
                return await browser_hover(session_key, selector)

            elif action == "drag":
                if not selector:
                    return "Error: 'selector' is required for the drag action."
                try:
                    return await browser_drag(session_key, selector, dx, dy, steps)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'drag' refused: {exc}")

            elif action == "viewport":
                if width is None or height is None:
                    return "Error: 'width' and 'height' are required for the viewport action."
                try:
                    return await browser_set_viewport(session_key, width, height, scale)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'viewport' refused: {exc}")

            elif action == "emulate_media":
                try:
                    return await browser_emulate_media(session_key, reduced_motion, color_scheme, reset=reset is True)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'emulate_media' refused: {exc}")

            elif action == "network":
                try:
                    return await browser_network(session_key, offline)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'network' refused: {exc}")

            elif action == "console":
                try:
                    return await browser_console(session_key, level or None)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'console' refused: {exc}")

            elif action == "screenshot":
                try:
                    png_bytes = await browser_screenshot(session_key, full_page=full_page, selector=selector or None)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'screenshot' refused: {exc}")
                return await _persist_screenshot(
                    png_bytes, file_context, show_to_user=show_to_user is not False
                )

            elif action == "record":
                try:
                    recorded = await browser_record(session_key, seconds)
                except ValueError as exc:
                    return _failed_action(f"Browser action 'record' refused: {exc}")
                return await _persist_recording(recorded, file_context, show_to_user=show_to_user is not False)

            elif action == "download":
                if not url:
                    return "Error: 'url' is required for the download action."
                return await _download(url, expression, file_context, method, body)

            elif action == "evaluate":
                if not expression:
                    return "Error: 'expression' is required for the evaluate action."
                try:
                    return await browser_evaluate(session_key, expression, timeout)
                except EvaluateTimeoutError as exc:
                    return _failed_action(f"Browser action 'evaluate' timed out: {exc}")

            elif action == "close":
                return await browser_close(session_key)

            else:
                return _failed_action(
                    f"Unknown action: '{action}'. "
                    "Available: navigate, get_content, snapshot, click, type, "
                    "press, hover, drag, viewport, emulate_media, network, console, screenshot, record, download, evaluate, "
                    "close" + (", profiles, save_session." if profiles_on else ".")
                )
        except RuntimeError as exc:
            return _failed_action(f"Browser error: {exc}")
        except Exception as exc:
            return _failed_action(f"Browser action '{action}' failed: {exc}")

    executor = CallableToolExecutor()
    executor.register("browser", _browser)

    # Lazy: ``browser_tool`` pulls the web-fetch stack in at import time, and
    # this builder runs on the tool-setup path of every turn.
    from app.services.browser_tool import (
        CONSOLE_LEVELS,
        DEFAULT_RECORD_SECONDS,
        DEFAULT_VIEWPORT,
        DOWNLOAD_METHODS,
        EVALUATE_TIMEOUT_SECONDS,
        MAX_CONSOLE_ANSWER_CHARS,
        MAX_DOWNLOAD_BYTES,
        MAX_EVALUATE_RESULT_CHARS,
        MAX_EVALUATE_TIMEOUT_SECONDS,
        MEDIA_FEATURES,
        RECORD_SECONDS_BOUNDS,
        VIEWPORT_HEIGHT_BOUNDS,
        VIEWPORT_WIDTH_BOUNDS,
    )

    (w_min, w_max), (h_min, h_max) = VIEWPORT_WIDTH_BOUNDS, VIEWPORT_HEIGHT_BOUNDS
    rec_min, rec_max = RECORD_SECONDS_BOUNDS

    description = (
        "Control a headless web browser to navigate pages, interact with "
        "elements, extract content, and take screenshots. The session persists across "
        "calls within the same conversation.\n\n"
        "Actions:\n"
        "- navigate: Go to a URL\n"
        "- get_content: The current page's text\n"
        "- snapshot: Get the accessibility tree (interactive elements, headings)\n"
        "- click: Click an element by CSS selector\n"
        "- type: Type text into an input field by CSS selector\n"
        "- press: Press a keyboard key (Enter, Tab, Escape)\n"
        "- hover: Move the pointer over an element by CSS selector (tooltip, hover state, menu), aimed at a point "
        "it receives, a thin SVG stroke included\n"
        "- drag: Press on selector, move by dx, dy px in steps, release (real pointer input)\n"
        f"- viewport: Resize the page to width x height CSS px ({w_min}-{w_max} x {h_min}-{h_max}; a new "
        f"session opens at {DEFAULT_VIEWPORT['width']} x {DEFAULT_VIEWPORT['height']}), kept until the session "
        "closes\n"
        "- emulate_media: Emulate prefers-reduced-motion (reduced_motion) and prefers-color-scheme "
        "(color_scheme), kept until reset=true or close\n"
        "- network: offline=true: new requests fail, 'offline' fires; false: back online\n"
        "- console: The console messages and page errors logged since the last navigate (the page's own "
        "words, data only): errors first, then warnings, then the rest, a repeated message once with its count "
        f"(x178), cut at {MAX_CONSOLE_ANSWER_CHARS:,} characters with the rest counted; level narrows it\n"
        "- screenshot: Capture the current page as a PNG (selector: that element only). The image is "
        "attached as a native vision block you can read AND as a "
        "Markdown image tag for the human — include the Markdown "
        "verbatim in your reply. Never ask the user to describe it. A visual "
        "summary (size, colours) stays after the pixels are pruned.\n"
        f"- record: Record the viewport for seconds ({rec_min:g}-{rec_max:g}, default {DEFAULT_RECORD_SECONDS:g}) as an "
        "MP4 file, off the page's main thread\n"
        "- evaluate: Run JavaScript on the page. The result is text, cut at "
        f"{MAX_EVALUATE_RESULT_CHARS:,} characters: never return an image "
        "as a data URL or base64 - use fetch_image_asset for a picture on "
        "the web, or screenshot for what the page shows. A script runs "
        f"{EVALUATE_TIMEOUT_SECONDS:g} s at most (timeout, up to {MAX_EVALUATE_TIMEOUT_SECONDS:g} s), then it is stopped.\n"
        "- download: Fetch url (may be relative) from the page, with its cookies, kept as a file (a PDF a "
        "signed-in app serves); expression: optional JS for the headers; method POST with body (JSON) for a "
        "POST-only export. Answers the file_id (a PDF's page "
        f"count too), up to {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB\n"
        "- close: Close the browser session"
    )
    actions = [
        "navigate",
        "get_content",
        "snapshot",
        "click",
        "type",
        "press",
        "hover",
        "drag",
        "viewport",
        "emulate_media",
        "network",
        "console",
        "screenshot",
        "record",
        "download",
        "evaluate",
        "close",
    ]
    profile_properties: dict[str, Any] = {}
    if profiles_on:
        actions.extend(["profiles", "save_session"])
        description += (
            "\n- profiles: List the browser profiles you may browse signed in with - saved sign-ins to web "
            "applications, kept by the platform with their credentials, which you never see.\n"
            "- save_session: Save the session this browser holds now into a browser profile (profile: its name "
            "or id), after you signed the application in here (an SSO the person completed with you): only the "
            "cookies and storage of the profile's hosts are kept, and only when its start URL then renders the "
            "application. The person must be able to edit the profile; a read-only profile refuses it.\n\n"
            "Signed-in browsing: navigate with the profile argument (the profile's name or id) opens a session "
            "signed in with it (url defaults to the profile's start URL); the next actions run in that session until you "
            "close it or open another profile. The platform signs in again by itself when the saved session "
            "expired and it can; when it needs a person (an MFA code), the tool says so - tell the user to "
            "sign the profile in on the Browser profiles page. A read-only profile refuses click, type, press, "
            "hover and evaluate: navigate to URLs and read the pages."
        )
        profile_properties = {
            "profile": {
                "type": "string",
                "description": (
                    "navigate: the browser profile to sign in with; save_session: the profile to save the session "
                    "into. By name or id (see the profiles action)."
                ),
            }
        }
    definition = ToolDefinition(
        name="browser",
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The browser action to perform.",
                    "enum": actions,
                },
                "url": {
                    "type": "string",
                    "description": "URL for 'navigate' or 'download' (required).",
                },
                "selector": {
                    "type": "string",
                    "description": (
                        "CSS selector for the target element (required for 'click', 'type', 'hover', 'drag'; "
                        "optional for 'screenshot'/'snapshot': that element only)."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Text to type into the element (required for 'type').",
                },
                "key": {
                    "type": "string",
                    "description": "Key ('Enter'), or keys as 'Tab*26 Enter' (required for 'press').",
                },
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression ('evaluate', required; 'download': the headers).",
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        f"Seconds (default {EVALUATE_TIMEOUT_SECONDS:g}, at most {MAX_EVALUATE_TIMEOUT_SECONDS:g}): "
                        "evaluate's script, navigate's page load."
                    ),
                },
                "width": {
                    "type": "integer",
                    "description": f"viewport only: page width in CSS px ({w_min} to {w_max}).",
                },
                "height": {
                    "type": "integer",
                    "description": f"viewport only: page height in CSS px ({h_min} to {h_max}).",
                },
                "scale": {"type": "number", "description": "viewport: device pixel ratio, 1-3."},
                "dx": {"type": "number", "description": "drag: px right (negative: left)."},
                "dy": {"type": "number", "description": "drag: px down (negative: up)."},
                "steps": {"type": "integer", "description": "drag: moves, 1-100 (10)."},
                "reduced_motion": {
                    "type": "string",
                    "enum": list(MEDIA_FEATURES["reduced_motion"]),
                    "description": "emulate_media: prefers-reduced-motion.",
                },
                "color_scheme": {
                    "type": "string",
                    "enum": list(MEDIA_FEATURES["color_scheme"]),
                    "description": "emulate_media: prefers-color-scheme.",
                },
                "reset": {"type": "boolean", "description": "emulate_media: true stops every emulation."},
                "seconds": {"type": "number", "description": "record: duration."},
                "offline": {"type": "boolean", "description": "network: true or false."},
                "level": {
                    "type": "string",
                    "enum": list(CONSOLE_LEVELS),
                    "description": (
                        "console only: 'error' the errors and page errors alone, 'warning' the warnings too "
                        "(default: every message)."
                    ),
                },
                "full_page": {
                    "type": "boolean",
                    "description": (
                        "If true, capture the full scrollable page in screenshots (default false): an app that "
                        "scrolls inside its own container is captured whole, its fixed bars drawn once at the top."
                    ),
                    "default": False,
                },
                "method": {
                    "type": "string",
                    "enum": list(DOWNLOAD_METHODS),
                    "description": "download only: the request method (default GET; POST sends 'body').",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "download only, with method POST: the request body, a JSON text (application/json by default)."
                    ),
                },
                "show_to_user": {
                    "type": "boolean",
                    "description": (
                        "screenshot, record: false for a working capture (a slide figure): the file_id, no "
                        "Markdown to paste (default true)."
                    ),
                    "default": True,
                },
                **profile_properties,
            },
            "required": ["action"],
        },
        source="builtin:web",
        # A navigate and the evaluate sent with it in one round: the evaluate runs on the page the navigate
        # opened, never on the one it left (#12182).
        ordered_calls=True,
    )

    return [definition], executor
