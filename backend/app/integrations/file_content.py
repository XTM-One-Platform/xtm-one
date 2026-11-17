# SPDX-License-Identifier: Apache-2.0

"""Files an integration tool brings into a conversation: kept, read, forwarded.

Shared by the Microsoft and Google tools that meet an email attachment or a
drive document. Every such answer gives the agent the file as a
reusable file id (a file chip for the user), the text the platform can read
inline - inside the same budgets as the drive readers, with a footer that says
when it was cut and where the rest is - and the links the source exposes.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from email import message_from_bytes
from email.policy import default as _default_policy
from typing import Any

from app.core.utils import format_exception_message
from app.llm.base import FileRef

logger = logging.getLogger(__name__)

#: Persist callback injected by ``integration_tools._make_persist_file_fn``:
#: ``(filename, raw_bytes, content_type) -> FileRef | None``.
PersistFileFn = Callable[[str, bytes, str], Awaitable[Any]]

#: Office / PDF formats the platform's own extractor reads
#: (``services.chunking.extract_text_from_file``), by extension.
DOCUMENT_EXTENSIONS = (".docx", ".pptx", ".pdf", ".xlsx", ".xls")

#: The same formats by MIME type, for a file whose name carries no extension.
DOCUMENT_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
}

#: Extensions read as plain text even when the source reports a generic MIME.
TEXT_EXTENSIONS = (
    ".txt",
    ".csv",
    ".tsv",
    ".md",
    ".markdown",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".yaml",
    ".yml",
    ".ini",
    ".rst",
    ".ics",
    ".vcf",
)
TEXT_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/csv",
    "application/javascript",
)

#: Budgets of the drive readers (``providers/microsoft/_drive.py``): a text file up to
#: 500 KB, shown up to 10,000 characters; a document up to 25 MB, shown up to
#: 50,000 characters - the same budget as a chat upload's message.
MAX_TEXT_BYTES = 500_000
MAX_TEXT_CHARS = 10_000
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_DOCUMENT_CHARS = 50_000


def file_id_list(value: Any) -> list[str]:
    """The platform file ids a tool call names: a list, or one comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    ids: list[str] = []
    for item in items:
        text = str(item).strip() if item is not None else ""
        if text and text not in ids:
            ids.append(text)
    return ids


def cut_inline(text: str, limit: int, rest_hint: str) -> str:
    """*text* up to *limit* characters, with a footer that says so when cut."""
    if len(text) <= limit:
        return text
    return (
        f"{text[:limit]}\n\n[... truncated: the first {limit:,} of {len(text):,} "
        f"characters are shown. {rest_hint}]"
    )


def kilobytes(size: Any) -> str:
    try:
        return f"{round(int(size or 0) / 1024, 1)} KB"
    except (TypeError, ValueError):
        return "unknown size"


def _email_text(data: bytes) -> str:
    """Headers and the text body of an RFC 822 message (an attached email)."""
    message = message_from_bytes(data, policy=_default_policy)
    lines = [
        f"{header}: {message[header]}"
        for header in ("From", "To", "Cc", "Date", "Subject")
        if message[header]
    ]
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, UnicodeError, AttributeError):
            text = ""
        if body.get_content_type() == "text/html":
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    return "\n".join(lines) + ("\n\n" + text.strip() if text.strip() else "")


async def readable_text(
    data: bytes, filename: str, content_type: str
) -> tuple[str | None, int, str]:
    """``(text, limit, reason)`` for the content a tool result can show inline.

    ``text`` is ``None`` when the file is not readable as text, and ``reason``
    then says why ("not text-readable inline", "too large to extract
    inline", ...). ``limit`` is the character budget the text is cut to.
    """
    name = (filename or "").strip()
    lower = name.lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()

    extension = next((ext for ext in DOCUMENT_EXTENSIONS if lower.endswith(ext)), "")
    if not extension and mime in DOCUMENT_MIME_EXTENSIONS:
        extension = DOCUMENT_MIME_EXTENSIONS[mime]
        name = f"{name or 'document'}{extension}"
    if extension:
        if len(data) > MAX_DOCUMENT_BYTES:
            return None, 0, "too large to extract inline"
        from app.services.chunking import extract_text_from_file_async

        text = (await extract_text_from_file_async(data, name)).strip()
        if not text:
            return None, 0, "a document with no extractable text"
        return text, MAX_DOCUMENT_CHARS, ""

    if mime == "message/rfc822" or lower.endswith(".eml"):
        if len(data) > MAX_DOCUMENT_BYTES:
            return None, 0, "too large to read inline"
        try:
            text = _email_text(data).strip()
        except Exception as exc:  # malformed message: say so, never guess
            logger.info("Attached email could not be parsed: %s", format_exception_message(exc))
            text = ""
        if not text:
            return None, 0, "an email with no readable text"
        return text, MAX_DOCUMENT_CHARS, ""

    if mime.startswith(TEXT_MIME_PREFIXES) or lower.endswith(TEXT_EXTENSIONS):
        if len(data) > MAX_TEXT_BYTES:
            return None, 0, "too large to read inline"
        # The platform's one reading decoder: BOM, declared charset, UTF-8,
        # then cp1252 - a UTF-16 or legacy export is read, not mangled.
        from app.services.attachment_extract import decode_attachment_text

        text = decode_attachment_text(
            data,
            content_type,
            html=mime in ("text/html", "application/xhtml+xml") or lower.endswith((".html", ".htm")),
            utf8_first=mime == "application/json" or lower.endswith(".json"),
        )
        return text, MAX_TEXT_CHARS, ""

    if mime.startswith("image/"):
        return None, 0, "an image"
    return None, 0, "not text-readable inline"


async def keep_file(
    persist_file_fn: PersistFileFn | None,
    file_context: Any,
    filename: str,
    data: bytes,
    content_type: str,
) -> FileRef:
    """Store *data* as a conversation file and return its reference.

    The injected ``persist_file_fn`` writes a real ``FileAttachment`` (a
    download chip) and registers it in the run's FileContext. Without one - an
    evaluation or any caller with no user, conversation or run - the file is
    kept in memory and registered in *file_context* so the next tools can
    still read or forward it by id.
    """
    from app.tools.base import workspace_ingest_requested

    file_ref = None
    # Bound for the sandbox workspace (``to_workspace_path``): kept in
    # memory for the write, never stored as a conversation file.
    if persist_file_fn is not None and not workspace_ingest_requested():
        try:
            file_ref = await persist_file_fn(filename, data, content_type)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("File persistence failed for '%s': %s", filename, format_exception_message(exc))
            file_ref = None
    if file_ref is None:
        from app.tools.base import register_fetched_file

        file_ref = FileRef.from_bytes(data=data, filename=filename, content_type=content_type)
        register_fetched_file(file_context, file_ref)
    return file_ref


def kept_file_line(file_ref: FileRef) -> str:
    """How the answer hands the kept file over: the chip marker and the id."""
    return (
        f"Saved in the conversation as a reusable file: [[FILE:{file_ref.file_id}]] - "
        f"forward it to another tool with file_id={file_ref.file_id}; the user gets a "
        "download card only when the answer carries the [[FILE:...]] marker verbatim."
    )


def rest_of_file_hint(file_ref: FileRef) -> str:
    return (
        f"The whole file is in the conversation (file_id={file_ref.file_id}): read the "
        "rest with read_file_content or process it with code_interpreter."
    )
