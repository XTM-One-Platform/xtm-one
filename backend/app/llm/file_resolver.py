# SPDX-License-Identifier: Apache-2.0

"""File resolution layer for LLM providers.

Converts :class:`FileRef` objects into provider-consumable formats:
either native (raw bytes + MIME) when the provider supports the type,
or fallback extracted text when it does not.

Each provider declares which MIME types it can handle natively via
:data:`PROVIDER_NATIVE_MIMES`. The resolver loads bytes from S3 only
when the provider can use them — otherwise it extracts text server-side.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field

from app.llm.base import BaseLLMProvider, ChatMessage, FileRef
from app.services.content_safety import wrap_untrusted

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider capability declarations
# ---------------------------------------------------------------------------
# Each provider key maps to the set of MIME prefixes/types it can handle
# natively (sent as structured content blocks, not as text injection).
# A prefix like "image/" matches any image type.

PROVIDER_NATIVE_MIMES: dict[str, set[str]] = {
    "anthropic": {
        "image/",
        "application/pdf",
    },
    "gemini": {
        "image/",
        "application/pdf",
    },
    "openai": {
        "image/",
        "application/pdf",
    },
    "bedrock": {
        "image/",
        "application/pdf",
        "text/csv",
        "text/plain",
        "text/markdown",
        "text/html",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
    # OpenAI-compatible backends (OpenRouter, Ollama, Mistral, Azure, LiteLLM,
    # custom) — we only know images are safe; docs support is unknown.
    "openai_compat": {
        "image/",
    },
}

# Maximum PDF page count each provider accepts in a native document block.
# A PDF over the limit is a poison pill: it lives in the message history, so
# every subsequent request in the conversation fails with the same 400
# (e.g. Anthropic: "A maximum of 600 PDF pages may be provided").  Such PDFs
# are degraded to server-side text extraction instead.
#
# The cap is per REQUEST, not per file: the same 400 fires when several
# smaller PDFs across the conversation add up to it, which is why
# :func:`resolve_payload_files` spends it as one budget over every document
# block of the payload, newest first.
PROVIDER_PDF_PAGE_LIMITS: dict[str, int] = {
    "anthropic": 600,
    "gemini": 1000,
    # OpenAI documents 100 pages per request (its newer docs cap only bytes).
    "openai": 100,
    # Bedrock mostly fronts Anthropic models — use the same conservative cap.
    "bedrock": 600,
}

# Raw bytes of native PDFs one request may carry, spent like the page cap.
# Anthropic (32 MB) and Gemini inline data (20 MB) cap the base64 request,
# 4/3 of the raw bytes; OpenAI documents 32 MB of file content per request.
PROVIDER_PDF_BYTE_LIMITS: dict[str, int] = {
    "anthropic": 24 * 1024 * 1024,
    "gemini": 15 * 1024 * 1024,
    "openai": 32 * 1024 * 1024,
}

# What a natively-sent PDF page costs. Anthropic bills each page as its
# extracted text PLUS a rendered image of the page - "1,500 to 3,000 tokens
# per page" in its own documentation - and Gemini / Bedrock (fronting Claude)
# land in the same range. The byte-based proxy the overflow check used before
# (``_BYTES_PER_NATIVE_TOKEN``) read a 592 KB PDF as ~2,400 tokens; Anthropic
# charged the same file at hundreds of thousands, which is how a conversation
# reached "prompt is too long: 1443058 tokens > 1000000 maximum" without the
# local guard ever firing.
PDF_TOKENS_PER_PAGE = 2_000

# The share of the model's context window the ATTACHMENTS of one request may
# take together - native documents at their page cost, text fallbacks at their
# character cost. The rest has to hold the system prompt, the tool schemas,
# the conversation and the answer. Past it, attachments of OLDER turns are
# degraded (native -> extracted text -> a one-line stub), newest first kept
# whole, so a long conversation stays answerable instead of failing every
# turn on its own history.
ATTACHMENT_WINDOW_SHARE = 0.5

_MAX_FALLBACK_CHARS = 80_000

#: The extensions a label shows as the file's kind; any other is ``FILE``.
#: A fixed list, not ``mimetypes``: its table grows with the image's
#: ``/etc/mime.types`` once loaded, and the label - hence the prompt cache -
#: must read the same in every process.
_LABEL_KINDS = frozenset(
    {
        "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "markdown", "html", "htm",
        "xls", "xlsx", "ods", "csv", "tsv", "json", "jsonl", "xml", "yaml", "yml", "toml",
        "ppt", "pptx", "odp", "eml", "msg", "ics", "log", "sql", "py", "js", "ts", "sh",
        "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "tif", "tiff",
        "zip", "gz", "tgz", "tar", "7z", "rar", "pcap", "pcapng", "stix", "mp3", "mp4", "wav",
        "webm", "mov", "m4v", "mkv", "avi", "m4a", "ogg", "flac", "aac",
    }
)

def file_label(file_ref: FileRef, detail: str = "") -> str:
    """``[File <file_id><detail>]`` and the file's name in an untrusted block.

    A stored name is often a third party's text (a downloaded document's
    title, an email attachment's name), and the model reads this label on
    every request that carries the file: the label names the file by its id,
    and the name follows in a :func:`wrap_untrusted` block. The block is
    ``stable`` (its boundary derives from its content), so the same file
    gives the same text on every turn and the provider's prompt cache still
    hits. *detail* is the platform's own text (kind, size, a reason).
    """
    label = f"[File {file_ref.file_id}{detail}]"
    block = file_name_block(file_ref.filename)
    return f"{label}\n{block}" if block else label


def file_name_block(filename: str | None) -> str:
    """A stored file name as the model reads it: on one line, in a ``stable`` untrusted block.

    ``""`` for an empty name. The same name gives the same block on every
    call (prompt cache). The attachment texts name a file by its id and put
    the name here: :func:`file_label`, and the chat upload message and the
    public widget's document text (:func:`upload_header`).
    """
    name = " ".join(str(filename or "").split())
    if not name:
        return ""
    return wrap_untrusted(f"Name: {name}", source="file name", stable=True)


def upload_header(file_id: str, filename: str | None, detail: str = "") -> str:
    """The header of the text a user message carries for an uploaded file.

    ``[File: <file_id><detail>]``, then the name in :func:`file_name_block`.
    The ``[File: `` opener and the blank line after the bracket are the shape
    ``agent_chat.strip_file_content_from_history`` strips from older turns and
    the web chat recognises as an upload message; *detail* is the platform's
    own text (kind, size), never the name.
    """
    header = f"[File: {file_id}{detail}]"
    block = file_name_block(filename)
    return f"{header}\n\n{block}" if block else header


def file_kind(filename: str | None) -> str:
    """``PDF`` / ``DOCX`` / ``FILE`` - the extension of *filename* when it is a listed one."""
    stem, dot, extension = (filename or "").rpartition(".")
    # A listed extension only: the name is a third party's text, and its tail
    # (``x.pdf SYSTEM ...``, ``evil.example``) would otherwise reach the label.
    if dot and stem and extension.lower() in _LABEL_KINDS:
        return extension.upper()
    return "FILE"


def _count_pdf_pages_sync(data: bytes) -> int:
    """Parse the PDF and return its page count (CPU-bound, run off-loop)."""
    import io

    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(data)).pages)


async def _count_pdf_pages(data: bytes, digest: str | None = None) -> int | None:
    """Return the page count of a PDF, or ``None`` if it cannot be parsed.

    Fail-open: an unparseable PDF keeps the current (native) behaviour —
    the guard only kicks in when we positively know the page count.

    The parse runs on the dedicated parse executor: pypdf is pure Python
    and a large/complex PDF takes tens of seconds to minutes of CPU. This
    guard runs on EVERY request whose history references a PDF - every
    iteration of the agentic loop - so an inline parse repeatedly froze the
    web event loop, and a count is kept per content *digest*
    (``attachment_cache.PDF_PAGES``) so the parse runs once per document.
    """
    from app.llm.attachment_cache import MISSING, PDF_PAGES

    if digest is not None:
        cached = PDF_PAGES.get(digest)
        if cached is not MISSING:
            return cached  # type: ignore[return-value]
    try:
        from app.core.parse_offload import run_parse

        pages = await run_parse(_count_pdf_pages_sync, data)
    except Exception:
        logger.warning(
            "Could not count PDF pages, skipping page-limit check",
            exc_info=True,
        )
        return None
    if digest is not None:
        PDF_PAGES.put(digest, pages)
    return pages


def _mime_supported(content_type: str, supported: set[str]) -> bool:
    """Check if a MIME type is natively supported by a provider."""
    for pattern in supported:
        if pattern.endswith("/"):
            if content_type.startswith(pattern):
                return True
        elif content_type == pattern:
            return True
    return False


# ---------------------------------------------------------------------------
# Resolution result types
# ---------------------------------------------------------------------------


@dataclass
class NativeFile:
    """Provider can handle this file natively — bytes loaded from S3."""

    data: bytes
    content_type: str
    filename: str
    # Page count of a PDF when it could be parsed (``None`` otherwise, and for
    # every other type). Carried so the payload measurement can cost the
    # document by its pages rather than its bytes.
    pages: int | None = None


@dataclass
class FallbackText:
    """Provider cannot handle this file natively — text extracted."""

    text: str
    filename: str


ResolvedFile = NativeFile | FallbackText


@dataclass
class PreparedVisionImage:
    """Raster image bytes ready to send as a native vision block."""

    data: bytes
    mime: str
    filename: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def prepare_tool_result_attachments(
    files: list[FileRef] | None,
    provider_key: str,
    *,
    source: str,
) -> tuple[list[PreparedVisionImage], list[str]]:
    """Split tool-result files into high-detail vision images + leftover text.

    Images are optimized with ``detail='high'`` (PNG preserved, higher
    long-edge cap) so desktop/browser screenshots stay readable. Every
    non-image file (integrity sidecars, a downloaded PDF or document) is
    returned as its extracted text, within the fallback cap and footer, for
    the caller to append to the tool-result body - a PDF the provider takes
    natively on a user message included.

    Previously every provider either ignored ``ChatMessage.files`` on
    ``role=tool`` or replaced them with a ``[File: name (N bytes)]`` stub,
    so the model never saw runner screenshots at all.
    """
    from app.llm.base import (
        IMAGE_DETAIL_HIGH,
        IMAGE_UNAVAILABLE_TEXT,
        prepare_image_bytes_for_llm_async,
    )

    images: list[PreparedVisionImage] = []
    extra_texts: list[str] = []
    if not files:
        return images, extra_texts

    for idx, file_ref in enumerate(files):
        resolved = await resolve_file(file_ref, provider_key)
        if (
            isinstance(resolved, NativeFile)
            and resolved.content_type.startswith("image/")
        ):
            prepared = await prepare_image_bytes_for_llm_async(
                resolved.data,
                resolved.content_type,
                source=f"{source}:{idx}[{resolved.filename}]",
                detail=IMAGE_DETAIL_HIGH,
            )
            if prepared is None:
                extra_texts.append(IMAGE_UNAVAILABLE_TEXT)
                continue
            data, mime = prepared
            images.append(
                PreparedVisionImage(
                    data=data, mime=mime, filename=resolved.filename
                )
            )
        elif isinstance(resolved, NativeFile):
            # A tool result carries text and images only: a document the
            # provider reads natively elsewhere (a PDF on Anthropic / Gemini / OpenAI,
            # an Office or text file on Bedrock) is read here as its text.
            extra_texts.append(
                (await _extract_fallback(file_ref, data=resolved.data)).text
            )
        else:
            extra_texts.append(resolved.text)
    return images, extra_texts


async def _load_bytes(file_ref: FileRef) -> bytes | None:
    """The file's bytes — inline when the ref carries them, else from S3.

    A download is kept (``attachment_cache.STORED_BYTES``, bounded): the
    transcript re-sends the same attachments on every request of a run, and
    an object key names immutable bytes.

    ``None`` when the download failed; callers fall back to text extraction,
    which reports the failure in its own words. A failure is never kept.
    """
    from app.llm.attachment_cache import MISSING, STORED_BYTES, stored_object_key

    try:
        if file_ref.has_inline_data:
            return file_ref._data
        if getattr(file_ref, "is_deleted", False):
            return None
        key = stored_object_key(file_ref.s3_object_name, file_ref.size)
        if key is not None:
            cached = STORED_BYTES.get(key)
            if cached is not MISSING:
                return cached  # type: ignore[return-value]
        from app.services.file_storage import file_storage

        data = await file_storage.download(file_ref.s3_object_name)
        if key is not None:
            STORED_BYTES.put(key, data)
        return data
    except Exception:
        logger.warning(
            "Failed to download %s from S3 for native resolution, "
            "falling back to text extraction",
            file_ref.s3_object_name,
        )
        return None


#: Downloads :func:`resolve_payload_files` runs at once for a request's
#: attachments that are not kept yet.
_PREFETCH_CONCURRENCY = 4


async def _load_all_bytes(
    refs: list[tuple[tuple[int, int], FileRef]],
) -> dict[tuple[int, int], bytes | None]:
    """:func:`_load_bytes` for every ref, a few downloads at a time.

    The first request of a conversation downloads every attachment its
    history carries; one after the other, that was one storage round-trip per
    file before the first token.
    """
    semaphore = asyncio.Semaphore(_PREFETCH_CONCURRENCY)

    async def _one(file_ref: FileRef) -> bytes | None:
        async with semaphore:
            return await _load_bytes(file_ref)

    loaded = await asyncio.gather(*(_one(file_ref) for _key, file_ref in refs))
    return {key: data for (key, _file_ref), data in zip(refs, loaded, strict=True)}


def _prefetch_refs(
    messages: list[ChatMessage],
) -> list[tuple[tuple[int, int], FileRef]]:
    """The stored attachments :func:`resolve_payload_files` downloads up front.

    Newest first, as the plan walks them, and no more than the byte memo
    keeps: a prefetched file is held until the plan reaches it, while a file
    the plan turns into text or a stub was released as soon as it was
    decided. A file the memo would not keep (over its per-file cap, past its
    total, or of unknown size) is downloaded when the plan reaches it, so a
    history of large files is never in memory all at once.
    """
    from app.llm.attachment_cache import STORED_BYTES_MAX_ITEM, STORED_BYTES_MAX_TOTAL

    picked: list[tuple[tuple[int, int], FileRef]] = []
    room = STORED_BYTES_MAX_TOTAL
    for m_idx in range(len(messages) - 1, -1, -1):
        msg = messages[m_idx]
        if msg.role != "user" or not msg.files:
            continue
        for f_idx, file_ref in enumerate(msg.files):
            size = int(file_ref.size or 0)
            if file_ref.has_inline_data or not 0 < size <= min(STORED_BYTES_MAX_ITEM, room):
                continue
            room -= size
            picked.append(((m_idx, f_idx), file_ref))
    return picked


def _pdf_over_page_limit(
    file_ref: FileRef,
    pages: int | None,
    provider_key: str,
    size: int = 0,
) -> bool:
    """The per-file page and byte caps: a PDF over one goes to text on its own.

    Fail-open on an unknown page count, like :func:`_count_pdf_pages`.
    """
    byte_limit = PROVIDER_PDF_BYTE_LIMITS.get(provider_key)
    if byte_limit is not None and size > byte_limit:
        logger.warning(
            "PDF %s is %d bytes, over the %s native limit of %d — degrading to text extraction",
            file_ref.filename,
            size,
            provider_key,
            byte_limit,
        )
        return True
    page_limit = PROVIDER_PDF_PAGE_LIMITS.get(provider_key)
    if pages is None or page_limit is None or pages <= page_limit:
        return False
    logger.warning(
        "PDF %s has %d pages, over the %s native limit of %d "
        "— degrading to text extraction",
        file_ref.filename,
        pages,
        provider_key,
        page_limit,
    )
    return True


async def _resolve_native_pdf(
    file_ref: FileRef,
    data: bytes,
    provider_key: str,
) -> NativeFile | FallbackText:
    """A PDF the provider takes natively - unless it alone is over the cap.

    Counts the pages (the cost the payload measurement needs) and applies the
    per-file page limit. The per-REQUEST budget is the caller's job.
    """
    pages = await _count_pdf_pages(data, await _content_digest(data, file_ref))
    if _pdf_over_page_limit(file_ref, pages, provider_key, len(data)):
        return await _extract_fallback(file_ref, data=data)
    return NativeFile(
        data=data,
        content_type=file_ref.content_type,
        filename=file_ref.filename,
        pages=pages,
    )


async def resolve_file(
    file_ref: FileRef,
    provider_key: str,
) -> ResolvedFile:
    """Resolve a single FileRef for a given provider.

    If the provider supports the file's MIME type natively, loads the raw
    bytes from S3 and returns :class:`NativeFile`.  Otherwise, extracts
    text content and returns :class:`FallbackText`.

    One file at a time, with no view of the rest of the request: the
    per-request budget (aggregate PDF pages, share of the context window,
    duplicate bytes) is :func:`resolve_payload_files`, which every
    provider's message builder runs over the whole conversation. This stays
    the entry point for a single attachment - a tool result's screenshot, a
    file a builder meets outside the plan.
    """
    supported = PROVIDER_NATIVE_MIMES.get(provider_key, set())

    if _mime_supported(file_ref.content_type, supported):
        data = await _load_bytes(file_ref)
        if data is not None:
            if file_ref.content_type == "application/pdf":
                return await _resolve_native_pdf(file_ref, data, provider_key)
            return NativeFile(
                data=data,
                content_type=file_ref.content_type,
                filename=file_ref.filename,
            )

    return await _extract_fallback(file_ref)


# ---------------------------------------------------------------------------
# Per-request attachment budget
# ---------------------------------------------------------------------------
# Chat attachments live in the conversation history and are re-sent on every
# turn (``get_conversation_history`` rehydrates them so a file stays readable
# after the turn it was uploaded on). Resolved one at a time, nothing bounds
# their sum: a 127-message conversation carrying the same PDF three times
# reached Anthropic's 1M-token and 600-page request caps, and from then on
# EVERY turn failed, because the payload being refused was the history itself.
# The plan below resolves the attachments of one request together, spending
# one budget over them from the newest message back.


def _duplicate_stub(file_ref: FileRef) -> FallbackText:
    """Same bytes as a file attached to a LATER message: sent once, there."""
    return FallbackText(
        text=file_label(
            file_ref,
            " — identical to the copy attached to a later message in this "
            "conversation; its content is not repeated here",
        ),
        filename=file_ref.filename,
    )


def _file_kind(file_ref: FileRef) -> str:
    """``PDF`` / ``DOCX`` / ``FILE`` - the extension, as the resolver's own
    placeholder spells it."""
    return file_kind(file_ref.filename)


#: Extensions of media files whose bytes are never text, whatever MIME type the file was stored under.
_MEDIA_EXTENSIONS = frozenset(
    {"mp4", "m4v", "mov", "webm", "mkv", "avi", "mpeg", "mpg", "mp3", "m4a", "wav", "ogg", "oga", "flac", "aac", "opus"}
)


def _is_media(file_ref: FileRef) -> bool:
    """Whether *file_ref* is a video or an audio file: its bytes decoded as text are noise."""
    content_type = (file_ref.content_type or "").lower()
    if content_type.startswith(("video/", "audio/")):
        return True
    stem, dot, extension = (file_ref.filename or "").rpartition(".")
    return bool(dot and stem and extension.lower() in _MEDIA_EXTENSIONS)


def _media_stub(file_ref: FileRef) -> FallbackText:
    """A video or audio file, named by its id and size: its bytes are never inlined as text."""
    return FallbackText(
        text=file_label(
            file_ref,
            f" ({_file_kind(file_ref)}, {file_ref.size:,} bytes) — a video or audio file, attached by its id; "
            "its bytes are not shown as text",
        ),
        filename=file_ref.filename,
    )


def _omitted_stub(file_ref: FileRef, pages: int | None) -> FallbackText:
    """An older attachment the request has no room left for."""
    what = f"PDF, {pages} pages" if pages else _file_kind(file_ref)
    return FallbackText(
        text=file_label(
            file_ref,
            f" ({what}, {file_ref.size:,} bytes) — attached earlier in this "
            "conversation; omitted from this request to stay within the model's "
            "limits. Ask the user to attach it again if its content is needed now",
        ),
        filename=file_ref.filename,
    )


def _estimate_text_tokens(text: str) -> int:
    return max(len(text) // BaseLLMProvider._CHARS_PER_TOKEN, 1)


async def _content_digest(data: bytes, file_ref: FileRef | None = None) -> str:
    """sha256 of the file bytes, computed off the event loop.

    An upload can be 40 MB (``MAX_UPLOAD_SIZE_BYTES``) and the plan hashes
    every attachment of the history on every request; hashlib releases the
    GIL, so a thread costs nothing and a large file never stalls the loop. A
    stored file's digest is kept by its object key
    (``attachment_cache.CONTENT_DIGESTS``), so it is computed once.
    """
    from app.llm.attachment_cache import CONTENT_DIGESTS, MISSING, stored_object_key

    key = None
    if file_ref is not None and not file_ref.has_inline_data:
        key = stored_object_key(file_ref.s3_object_name, file_ref.size)
    if key is not None:
        cached = CONTENT_DIGESTS.get(key)
        if cached is not MISSING:
            return cached  # type: ignore[return-value]
    digest = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
    if key is not None:
        CONTENT_DIGESTS.put(key, digest)
    return digest


def _current_turn_index(messages: list[ChatMessage]) -> int | None:
    """Index of the request's current user turn - the LAST user-role message.

    Tool results and assistant turns may follow it in the agentic loop; an
    earlier user message is history, whatever it carries.
    """
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].role == "user":
            return idx
    return None


@dataclass
class PayloadFilePlan:
    """The attachments of one request, resolved together under one budget.

    Keyed by ``(message_index, file_index)`` into the ``messages`` list the
    plan was built from, so a builder walking that list in order reads the
    decision for each file instead of resolving it alone. The totals are
    what the overflow check needs to cost native PDFs by their pages: the
    payload measurement only sees base64 blobs by then.
    """

    resolved: dict[tuple[int, int], ResolvedFile] = field(default_factory=dict)
    # Native PDFs kept in this request: their page total, their raw byte
    # total (so a byte-based measurement can take them back out) and their
    # page-based token estimate (what goes in instead).
    native_pdf_pages: int = 0
    native_pdf_bytes: int = 0
    native_pdf_tokens: int = 0
    # Files degraded for the request's sake - text instead of native, a stub
    # instead of text - and files stubbed because the same bytes ride a later
    # message. Filenames, for the one INFO line the builder logs.
    degraded: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    deduplicated: list[str] = field(default_factory=list)

    def get(self, message_index: int, file_index: int) -> ResolvedFile | None:
        return self.resolved.get((message_index, file_index))


def attachment_token_budget(context_window: int | None) -> int | None:
    """Tokens the attachments of one request may take together, or ``None``."""
    if not context_window or context_window <= 0:
        return None
    return int(context_window * ATTACHMENT_WINDOW_SHARE)


async def resolve_payload_files(
    messages: list[ChatMessage],
    provider_key: str,
    *,
    context_window: int | None = None,
) -> PayloadFilePlan:
    """Resolve every user-message attachment of a request under one budget.

    Walks the messages from the NEWEST back, so the files the user just sent
    are the last to lose anything, and decides each file in this order:

    1. **Same bytes as a file already kept from a later message** - a stub
       naming that copy. Re-attaching a document must not send it twice.
    2. **Image** - native, always (bounded by the image optimizer and counted
       by the overflow check per image).
    3. **PDF over the provider's per-file page cap** - text extraction.
    4. **PDF that fits the request's remaining page cap AND token budget** -
       native, costed at :data:`PDF_TOKENS_PER_PAGE` per page.
    5. **Otherwise** - including every file the provider does not take
       natively (OpenAI takes no PDF at all) - text extraction if its text
       fits the remaining token budget, else a one-line stub saying the file
       was omitted and can be re-attached.

    The page cap is the provider's request-wide PDF limit
    (:data:`PROVIDER_PDF_PAGE_LIMITS`); the token budget is
    :func:`attachment_token_budget` of *context_window*, or unbounded when the
    window is unknown. Every text fallback and every stub is costed by its
    characters, so they all draw on the same budget - a conversation whose
    every old PDF became 80k characters of text would otherwise overflow just
    the same, only in text.

    The files of the CURRENT user turn - the last user-role message, whatever
    follows it in the agentic loop - are never stubbed: they are what the
    user just sent, and a turn that cannot see its own attachment is not a
    turn worth saving. They still degrade from native to text under the
    budget; whether that text fits the window at all is the overflow check's
    call, with the real numbers. An attachment on an EARLIER user message is
    history and may be stubbed, even when no later message carries a file:
    protecting it forever would let one oversized fallback fail every later
    turn of the conversation.

    Never raises for a single file: a download or extraction failure becomes
    the resolver's usual placeholder text, exactly as :func:`resolve_file`.
    """
    plan = PayloadFilePlan()
    supported = PROVIDER_NATIVE_MIMES.get(provider_key, set())
    page_limit = PROVIDER_PDF_PAGE_LIMITS.get(provider_key)
    byte_limit = PROVIDER_PDF_BYTE_LIMITS.get(provider_key)
    token_budget = attachment_token_budget(context_window)
    current_turn = _current_turn_index(messages)

    pages_used = 0
    tokens_used = 0
    seen_digests: set[str] = set()

    def _fits(pages: int, tokens: int, size: int = 0) -> bool:
        if page_limit is not None and pages_used + pages > page_limit:
            return False
        if byte_limit is not None and plan.native_pdf_bytes + size > byte_limit:
            return False
        return token_budget is None or tokens_used + tokens <= token_budget

    def _stub(key: tuple[int, int], stub: FallbackText, register: list[str]) -> None:
        # A stub is always sent, so it is always charged: enough of them
        # would otherwise admit text the budget has no room for.
        nonlocal tokens_used
        plan.resolved[key] = stub
        register.append(stub.filename)
        tokens_used += _estimate_text_tokens(stub.text)

    loaded = await _load_all_bytes(_prefetch_refs(messages))

    for m_idx in range(len(messages) - 1, -1, -1):
        msg = messages[m_idx]
        if msg.role != "user" or not msg.files:
            continue
        protected = m_idx == current_turn
        for f_idx, file_ref in enumerate(msg.files):
            key = (m_idx, f_idx)
            pages: int | None = None
            fallback: FallbackText
            data = loaded.pop(key) if key in loaded else await _load_bytes(file_ref)
            if data is None:
                # Text extraction re-downloads and reports its own failure;
                # whatever it returns is budgeted below like any fallback.
                fallback = await _extract_fallback(file_ref)
            else:
                digest = await _content_digest(data, file_ref)
                if digest in seen_digests:
                    _stub(key, _duplicate_stub(file_ref), plan.deduplicated)
                    continue
                seen_digests.add(digest)

                if not _mime_supported(file_ref.content_type, supported):
                    # Not native for this provider: text, budgeted below.
                    fallback = await _extract_fallback(file_ref, data=data)
                elif file_ref.content_type.startswith("image/"):
                    plan.resolved[key] = NativeFile(
                        data=data,
                        content_type=file_ref.content_type,
                        filename=file_ref.filename,
                    )
                    continue
                elif file_ref.content_type == "application/pdf":
                    pages = await _count_pdf_pages(data, digest)
                    if _pdf_over_page_limit(file_ref, pages, provider_key, len(data)):
                        # Over the per-file cap on its own: text.
                        fallback = await _extract_fallback(file_ref, data=data)
                    else:
                        est_tokens = (
                            pages * PDF_TOKENS_PER_PAGE
                            if pages is not None
                            else max(
                                len(data) // BaseLLMProvider._BYTES_PER_NATIVE_TOKEN, 1
                            )
                        )
                        if _fits(pages or 0, est_tokens, len(data)):
                            plan.resolved[key] = NativeFile(
                                data=data,
                                content_type=file_ref.content_type,
                                filename=file_ref.filename,
                                pages=pages,
                            )
                            pages_used += pages or 0
                            tokens_used += est_tokens
                            plan.native_pdf_pages += pages or 0
                            plan.native_pdf_bytes += len(data)
                            plan.native_pdf_tokens += est_tokens
                            continue
                        plan.degraded.append(file_ref.filename)
                        fallback = await _extract_fallback(file_ref, data=data)
                else:
                    # Another native document type (Bedrock: csv, docx,
                    # xlsx...): no page count, costed by bytes as the
                    # overflow check does.
                    est_tokens = max(
                        len(data) // BaseLLMProvider._BYTES_PER_NATIVE_TOKEN, 1
                    )
                    if _fits(0, est_tokens):
                        plan.resolved[key] = NativeFile(
                            data=data,
                            content_type=file_ref.content_type,
                            filename=file_ref.filename,
                        )
                        tokens_used += est_tokens
                        continue
                    plan.degraded.append(file_ref.filename)
                    fallback = await _extract_fallback(file_ref, data=data)

            text_tokens = _estimate_text_tokens(fallback.text)
            if protected or _fits(0, text_tokens):
                plan.resolved[key] = fallback
                tokens_used += text_tokens
                continue
            _stub(key, _omitted_stub(file_ref, pages), plan.omitted)

    if plan.degraded or plan.omitted or plan.deduplicated:
        logger.info(
            "Attachment budget for %s (page cap %s, token budget %s): kept %d "
            "native PDF page(s) (~%d tokens); degraded to text: %s; omitted: %s; "
            "duplicates stubbed: %s",
            provider_key,
            page_limit if page_limit is not None else "none",
            token_budget if token_budget is not None else "unbounded",
            plan.native_pdf_pages,
            plan.native_pdf_tokens,
            ", ".join(plan.degraded) or "-",
            ", ".join(plan.omitted) or "-",
            ", ".join(plan.deduplicated) or "-",
        )
    return plan


async def _extract_fallback(
    file_ref: FileRef,
    data: bytes | None = None,
) -> FallbackText:
    """Extract text from a file as fallback for providers that don't
    support the MIME type natively.

    ``data`` lets callers that already hold the file bytes (e.g. the PDF
    page-limit guard in :func:`resolve_file`) skip a second S3 download.

    A video or audio file is its label stub (kind, size): its bytes are never
    decoded, downloaded or inlined.

    An attachment whose plain extraction fits the cap below is inlined as
    that extraction, whole. An attachment whose plain extraction exceeds the
    cap is inlined as its rendering instead: an HTML page as its RENDERED
    text (its embedded data summarised FIRST, so the cap never cuts it), a
    large JSON / JSON Lines attachment as a structured summary and a large
    CSV with a digest of the whole file ahead of its text - the raw source of
    such a file is what filled the whole fallback budget with
    ``<path d=...>`` markup and sent the model paging through it with
    ``read_file_content``. Every other type is the plain
    document extraction. A cut ends
    with :func:`~app.services.attachment_extract.inline_truncation_footer`,
    whose offset is an offset into the same text ``read_file_content``
    serves. Both name the code interpreter only when the running loop
    offers it (``offered_tools()``) - the surface the tools read too,
    so the two renderings stay one text; outside a loop nothing is named.
    """
    from app.services.attachment_extract import (
        inline_attachment_text_async,
        inline_truncation_footer,
    )
    from app.services.code_interpreter import code_interpreter_offered
    from app.tools.base import offered_tools

    if getattr(file_ref, "is_deleted", False):
        # A tombstone: its bytes are gone; the model reads the notice, never an error.
        return FallbackText(text=f"{file_label(file_ref)}\n\n[{file_ref.deleted_notice}]", filename=file_ref.filename)
    if _is_media(file_ref):
        # An MP4 decoded as UTF-8 is its ftyp/moov/mdat bytes: 80,000 characters
        # of noise in every request that carries it (a browser recording).
        return _media_stub(file_ref)
    code_interpreter = code_interpreter_offered(offered_tools())
    try:
        if data is None:
            if file_ref.has_inline_data:
                data = file_ref._data
            else:
                from app.services.file_storage import file_storage
                data = await file_storage.download(file_ref.s3_object_name)
        # Off-loop: this extraction re-runs on every turn of a conversation
        # whose history carries the attachment - memoised per
        # content hash, so a large page is rendered once, not every turn.
        inline = await inline_attachment_text_async(
            data, file_ref.filename, file_ref.content_type, code_interpreter, _MAX_FALLBACK_CHARS
        )
        text = inline.text
        if inline.stripped_total >= 10:
            if inline.total > _MAX_FALLBACK_CHARS:
                text = (
                    text
                    + "\n\n"
                    + inline_truncation_footer(
                        file_id=file_ref.file_id,
                        filename=file_ref.filename,
                        content_type=file_ref.content_type,
                        shown=_MAX_FALLBACK_CHARS,
                        total=inline.total,
                        code_interpreter=code_interpreter,
                    )
                )
            return FallbackText(
                text=f"{file_label(file_ref)}\n\n{text}",
                filename=file_ref.filename,
            )
    except Exception:
        logger.warning(
            "Text extraction failed for %s (%s)",
            file_ref.filename,
            file_ref.s3_object_name,
        )

    return FallbackText(
        text=file_label(file_ref, f" ({_file_kind(file_ref)}, {file_ref.size:,} bytes)"),
        filename=file_ref.filename,
    )
