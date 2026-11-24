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

"""Code interpreter, image generation, and file generation tool builders.

Moved verbatim from the original ``app/services/builtin_tools.py`` module
as part of the package split (mechanical refactor - no behavior change).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from base64 import b64decode, b64encode
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message, safe_uuid
from app.services import docx_render, pdf_render
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult, register_produced_file

from ._shared import (
    EMBED_FILE_REF_RE as _EMBED_FILE_REF_RE,
)
from ._shared import (
    _own_session,
    _persist_generated_file,
    logger,
    persist_tool_artifact,
)
from ._shared import (
    format_file_size as _format_size,
)
from ._shared import (
    load_scoped_file_ref as _load_scoped_file_ref,
)
from ._shared import (
    resolve_file_ref as _resolve_file_ref,
)
from ._shared import (
    unresolved_file_message as _unresolved_file_message,
)
from .image_assets import ALLOWED_IMAGE_TYPES, sniff_raster_type

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import FileRef
    from app.tools.base import FileContext


# Image handles an agent embeds in generate_file Markdown/HTML are matched by
# the shared ``EMBED_FILE_REF_RE``: the platform ``[[FILE:<id>]]`` marker or a
# ``file:<uuid>`` URL inside ``![alt](...)`` / ``<img src=...>``. The PDF/DOCX
# renderers block every non-``data:`` URL (SSRF guard) and never see the
# platform store, so these handles render as a broken image.
# ``_inline_embedded_file_refs`` resolves each referenced IMAGE via the
# conversation FileContext and rewrites the handle to a
# ``data:<mime>;base64,...`` URI so the real bytes embed.


async def _inline_embedded_file_refs(
    content: str, file_context: FileContext | None
) -> str:
    """Rewrite ``[[FILE:id]]`` / ``file:<uuid>`` IMAGE refs to inline data: URIs.

    Lets an agent embed a rendered diagram it produced earlier (e.g. via
    ``download_figma_image`` / ``download_notion_file`` / the code interpreter)
    into a generated PDF/DOCX by referencing it as ``![alt](file:<id>)`` or
    ``[[FILE:id]]``. Only image files are inlined (Markdown/HTML image embedding
    is image-only); non-image and unresolvable handles are left untouched.
    """
    if file_context is None or not content:
        return content
    ids = {
        (m.group("marker") or m.group("uuid"))
        for m in _EMBED_FILE_REF_RE.finditer(content)
    }
    if not ids:
        return content

    data_uris: dict[str, str] = {}
    for fid in ids:
        try:
            resolved = await file_context.resolve_upload(fid, outbound=False)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "generate_file: could not resolve embedded file %s: %s", fid, format_exception_message(exc)
            )
            continue
        if not resolved:
            continue
        data, _name, mime = resolved
        mime = (mime or "").split(";", 1)[0].strip().lower()
        if not mime.startswith("image/"):
            # Only images embed in Markdown/HTML <img>; skip others so we don't
            # bloat the document with a non-rendering data: blob.
            continue
        data_uris[fid] = f"data:{mime};base64,{b64encode(data).decode('ascii')}"

    if not data_uris:
        return content

    def _replace(match: re.Match) -> str:
        fid = match.group("marker") or match.group("uuid")
        return data_uris.get(fid, match.group(0))

    return _EMBED_FILE_REF_RE.sub(_replace, content)


# ── Reading back and revising conversation files ─────────────────────
#
# A file the agent produced hangs off an ASSISTANT message, and no provider
# inlines assistant attachments — so one turn later its content is simply not
# in the context. Until these tools existed the model had no way to see the
# document it had just written: asked to remove one sentence from a 33 KB
# HTML briefing it went looking for the bytes in the code-interpreter sandbox,
# then in its own traces, burnt the whole iteration budget and never answered.
#
# ``read_file_content`` is the read primitive (windowed, like
# ``get_knowledge_document``); ``edit_file_content`` is the write primitive:
# exact find/replace applied SERVER-SIDE to the stored text, so the model never
# re-emits the document to change a line, and the result is a new revision
# (a new ``FileAttachment``) - the file the user already downloaded is never
# rewritten under them.

#: Character window served by ``read_file_content`` when the caller gives none.
READ_WINDOW_DEFAULT_CHARS = 50_000
#: Hard cap on one ``read_file_content`` window.
READ_WINDOW_MAX_CHARS = 150_000
#: ``read_file_content`` modes: the verbatim source, or the rendering
#: of an HTML / JSON / JSON Lines / CSV file (``attachment_extract``).
READ_MODES: tuple[str, ...] = ("source", "extract_structured")
#: Upper bound on the number of operations in one ``edit_file_content`` call.
EDIT_MAX_OPERATIONS = 50
#: Ceiling on the notes ``annotate_file`` stores for one file: a 1-3
#: line account of what the file shows, the size of a template asset's caption.
FILE_NOTES_MAX_CHARS = 600
#: Control characters ``str.split`` keeps (it splits on the whitespace ones):
#: dropped from the notes before they are stored.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0e-\x1b\x7f-\x9f]")
#: Snippet length echoed back in an edit refusal (bounded: the ``find`` text is
#: model-controlled and may be huge).
_EDIT_SNIPPET_CHARS = 120

#: MIME types whose bytes ARE the source text (decoded as UTF-8), on top of
#: every ``text/*`` type.
_TEXT_LIKE_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/x-ndjson",
        "application/xml",
        "application/xhtml+xml",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/javascript",
        "application/x-javascript",
        "application/typescript",
        "application/sql",
        "application/x-sh",
        "application/csv",
        "application/x-httpd-php",
        "image/svg+xml",
    }
)
#: Extensions read as source text when the MIME type is generic
#: (``application/octet-stream`` is what an unknown extension guesses to).
_TEXT_LIKE_EXTENSIONS = frozenset(
    {
        ".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".md", ".markdown",
        ".txt", ".text", ".rst", ".xml", ".html", ".htm", ".xhtml", ".svg",
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".conf", ".env", ".log", ".sh", ".bash",
        ".ps1", ".bat", ".sql", ".css", ".scss", ".less", ".yar", ".yara",
        ".rules", ".sigma", ".stix", ".tex", ".rtf", ".srt", ".vtt", ".ics",
        ".eml", ".mbox", ".gitignore", ".dockerfile",
    }
)
#: What ``generate_file(source_file_id=...)`` renders: the Markdown,
#: HTML or plain text source of a document. Any other type is refused - a CSV
#: or a JSON file rendered as a document would be one paragraph of raw text.
_RENDERABLE_SOURCE_MIME_TYPES = frozenset(
    {"text/markdown", "text/x-markdown", "text/html", "application/xhtml+xml", "text/plain"}
)
_RENDERABLE_SOURCE_EXTENSIONS = (".md", ".markdown", ".html", ".htm", ".xhtml", ".txt", ".text")
#: Types that say nothing about the bytes: the extension decides.
_GENERIC_MIME_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})


def is_renderable_source_file(filename: str | None, content_type: str | None) -> bool:
    """True when a file is Markdown, HTML or plain text ``generate_file`` can render from."""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in _RENDERABLE_SOURCE_MIME_TYPES:
        return True
    if mime not in _GENERIC_MIME_TYPES:
        return False
    return (filename or "").strip().lower().endswith(_RENDERABLE_SOURCE_EXTENSIONS)


def _read_windows_per_file() -> int:
    """Text windows ``read_file_content`` serves per file, per view and per turn.

    ``AGENT_LOOP_READ_WINDOWS_PER_FILE``; ``0`` disables the cap. Read
    at call time so an operator change is honoured without a rebuild of the tool.
    """
    from app.core.config import settings

    return max(0, int(getattr(settings, "agent_loop_read_windows_per_file", 4) or 0))


def is_text_like_file(filename: str | None, content_type: str | None) -> bool:
    """True when a file's bytes are its source text (editable in place).

    Rendered documents (PDF, DOCX, XLSX) and images are NOT text-like: their
    text can be *extracted* for reading, but there is no source to edit -
    ``generate_file`` renders those from Markdown/HTML that is not stored.
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("text/") or mime in _TEXT_LIKE_MIME_TYPES:
        return True
    name = (filename or "").strip().lower()
    return any(name.endswith(ext) for ext in _TEXT_LIKE_EXTENSIONS)


def _produced_same_name(file_context: object | None, file_ref: FileRef) -> FileRef | None:
    """The file *file_ref* replaces: the latest other one this turn wrote under its name and type."""
    lookup = getattr(file_context, "produced_same_name", None)
    return lookup(file_ref) if callable(lookup) else None


def _same_name_note(file_ref: FileRef, earlier: FileRef) -> str:
    """What ``generate_file`` tells the agent when a file of this turn already has the name.

    A byte-identical rewrite reuses the earlier row (``match_filename``), so
    this only fires for different content - two download cards the user
    cannot tell apart. ``edit_file_content`` only works in place on a
    text-like file, so it is only suggested for one.
    """
    advice = (
        "; prefer edit_file_content for corrections."
        if is_text_like_file(file_ref.filename, file_ref.content_type)
        else "."
    )
    return (
        f"NOTE: {file_ref.filename} was already generated in this turn "
        f"(file_id {earlier.file_id}). This new file supersedes it{advice}"
    )


#: The UTF-8 byte order mark. Dropped by :func:`decode_file_text` and written
#: back by ``edit_file_content`` on a file that opened with it - the two must
#: agree, or an edit silently changes the first three bytes of the file.
UTF8_BOM = b"\xef\xbb\xbf"


def decode_file_text(raw: bytes) -> str | None:
    """Decode *raw* as UTF-8 text; ``None`` when it is not.

    Strict on purpose: the text this returns is what ``edit_file_content``
    re-encodes as UTF-8, so a lossy decode here would be a lossy WRITE there.
    Decoding a Windows-1252 CSV with ``errors="replace"`` turned every
    non-ASCII byte outside the edited region (``caf\\xe9`` -> ``caf\\ufffd``)
    into U+FFFD in the new revision, silently - and the find still matched,
    because the model copies the already-replaced text it was shown. A file
    that is not UTF-8 is therefore refused (``None``): it stays READABLE
    through the extraction path and is never edited in place.

    A NUL byte is the binary tripwire (UTF-8 text never carries one). It
    also classifies UTF-16 text as binary, deliberately: its ASCII range is
    interleaved with NUL, so the file reads through extraction and an
    in-place edit - which could not preserve its encoding either - is not
    offered. A UTF-8 BOM is dropped so the model never has to reproduce an
    invisible character in a ``find``; the edit path puts it back on the
    revision, so the marker is never lost by an edit that did not name it.
    """
    if b"\x00" in raw:
        return None
    if raw.startswith(UTF8_BOM):
        raw = raw[len(UTF8_BOM) :]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


class FileEditError(ValueError):
    """An ``edit_file_content`` operation could not be applied as written."""


def _snippet(text: str) -> str:
    text = text.replace("\n", "\\n")
    if len(text) > _EDIT_SNIPPET_CHARS:
        return text[:_EDIT_SNIPPET_CHARS] + "..."
    return text


def apply_text_edits(text: str, edits: object) -> tuple[str, list[str]]:
    """Apply an ordered list of exact find/replace operations to *text*.

    Each operation is ``{"find": str, "replace": str, "replace_all": bool}``.
    The contract is the one a careful editor would want - no partial writes
    and no silent guesses:

    * ``find`` must be a non-empty string that occurs in the text (as edited
      by the previous operations); an absent snippet refuses the WHOLE call.
    * A snippet that occurs more than once is ambiguous and refused unless
      ``replace_all`` is true (then every occurrence changes).
    * ``replace`` may be empty (a deletion).

    Returns ``(new_text, account)`` where *account* has one human-readable
    line per operation.  Raises :class:`FileEditError` with a message the
    model can act on.
    """
    if not isinstance(edits, list) or not edits:
        raise FileEditError(
            "edits must be a non-empty list of {find, replace} operations."
        )
    if len(edits) > EDIT_MAX_OPERATIONS:
        raise FileEditError(
            f"Too many operations ({len(edits)}); at most {EDIT_MAX_OPERATIONS} "
            "per call. Split the change into several calls."
        )
    account: list[str] = []
    current = text
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            raise FileEditError(
                f"edit {index}: each operation must be an object with "
                "'find' and 'replace' string fields."
            )
        find = edit.get("find")
        replace = edit.get("replace", "")
        replace_all = bool(edit.get("replace_all", False))
        if not isinstance(find, str) or not find:
            raise FileEditError(
                f"edit {index}: 'find' must be a non-empty string (the exact "
                "text to replace)."
            )
        if replace is None:
            replace = ""
        if not isinstance(replace, str):
            raise FileEditError(
                f"edit {index}: 'replace' must be a string (use \"\" to delete "
                "the found text)."
            )
        occurrences = current.count(find)
        if occurrences == 0:
            raise FileEditError(
                f"edit {index}: the text to find was not found: "
                f"'{_snippet(find)}'. Nothing was changed. Read the file with "
                "read_file_content and copy the snippet exactly - whitespace, "
                "punctuation and markup included."
            )
        if occurrences > 1 and not replace_all:
            raise FileEditError(
                f"edit {index}: '{_snippet(find)}' occurs {occurrences} times, "
                "which is ambiguous. Nothing was changed. Include surrounding "
                "text so the snippet is unique, or set replace_all=true to "
                "change every occurrence."
            )
        current = (
            current.replace(find, replace)
            if replace_all
            else current.replace(find, replace, 1)
        )
        changed = occurrences if replace_all else 1
        delta = (len(replace) - len(find)) * changed
        verb = "deleted" if replace == "" else "replaced"
        account.append(
            f"edit {index}: {verb} {changed} occurrence"
            f"{'s' if changed != 1 else ''} of '{_snippet(find)}'"
            f" ({delta:+d} chars)"
        )
    return current, account


# ── Code Interpreter ─────────────────────────────────────────────────────────

#: Mount-line entries shown per result (the rest are in attachments/index.json).
_MOUNT_LINE_MAX_ENTRIES = 12

#: Code that draws a DOCUMENT into a PDF inside the sandbox: the
#: matplotlib PDF backend and the PDF-writing libraries. The sandbox has no
#: document renderer, so such a PDF reads as plain text; a run that writes a
#: ``.pdf`` with one of these gets :data:`SANDBOX_PDF_WARNING` appended to its
#: result. A chart saved as ``chart.pdf`` with ``savefig`` matches none of them.
SANDBOX_PDF_WRITER_PATTERNS: tuple[str, ...] = (
    r"matplotlib\.backends\.backend_pdf",
    r"\bPdfPages\b",
    r"\breportlab\b",
    r"\bfpdf\b",
    r"\bweasyprint\b",
    r"\bpypdf\b.*\.write\(",
    r"\bPdfWriter\b",
)
_SANDBOX_PDF_WRITER_RE = re.compile(
    "|".join(f"(?:{pattern})" for pattern in SANDBOX_PDF_WRITER_PATTERNS), re.IGNORECASE
)
SANDBOX_PDF_WARNING = (
    "WARNING: this PDF was drawn inside the sandbox and will read as plain text (no headings, no "
    "tables, no styling). Sandbox PDF output is for chart figures only. For a document, write the "
    "Markdown/HTML to a file and call generate_file(source_file_id=...) or generate_file(content=...) "
    'with content_type="application/pdf".'
)


def sandbox_drew_pdf_document(code: str, output_files: object) -> bool:
    """Whether a sandbox run wrote a ``.pdf`` with code that draws a document."""
    if not isinstance(code, str) or not isinstance(output_files, list):
        return False
    wrote_pdf = any(
        isinstance(item, dict)
        and (
            str(item.get("filename") or "").strip().lower().endswith(".pdf")
            or str(item.get("content_type") or "").split(";", 1)[0].strip().lower() == "application/pdf"
        )
        for item in output_files
    )
    return wrote_pdf and _SANDBOX_PDF_WRITER_RE.search(code) is not None


async def _read_mount_bytes(file_context: FileContext, ref: FileRef) -> bytes | None:
    """*ref*'s bytes through *file_context*, ``None`` when they could not be loaded (logged)."""
    try:
        return await file_context.read_bytes(ref.file_id)
    except Exception:
        logger.warning("Could not load %s (%s) for the sandbox mount", ref.file_id, ref.filename, exc_info=True)
        return None


class SandboxInputCache:
    """The turn's mount downloads: each file read ONCE for every ``code_interpreter`` call.

    Every call of a turn mounts the turn's files, and a parallel batch
    dispatches its calls together: read per call, a batch of N downloaded each
    file N times and held N copies of it at once. The reads go through
    *file_context* - the mount grants nothing the context did not - and are
    shared by every call handed this cache:

    * single-flight: concurrent calls await ONE load per file, shielded, so a
      cancelled call never cancels it for the others;
    * a failure or a storage miss is not kept - the next call reads again;
    * bounded: at most :data:`~app.services.code_interpreter.MAX_INPUT_TOTAL_BYTES`
      kept (:attr:`held_bytes`); a file past it is read per call, as before;
    * keyed on the ref's storage identity - file id, object key, size - so a
      file whose bytes changed is a miss, never stale bytes: a revision
      (``edit_file_content``) is a new id, a template asset replaced in place a
      new object key. A file carried in memory (``FileRef.from_bytes``) costs
      no download and is read from its ref every time.
    """

    def __init__(self, file_context: FileContext) -> None:
        self.file_context = file_context
        self.held_bytes = 0
        self._kept: dict[tuple[str, str, int], bytes] = {}
        self._loading: dict[tuple[str, str, int], asyncio.Task[bytes | None]] = {}

    async def read(self, ref: FileRef) -> bytes | None:
        """*ref*'s bytes - kept, in flight or loaded now - or ``None`` when they could not be loaded."""
        if ref.has_inline_data:
            return await _read_mount_bytes(self.file_context, ref)
        key = (ref.file_id.removeprefix("file-"), ref.s3_object_name, int(ref.size or 0))
        kept = self._kept.get(key)
        if kept is not None:
            return kept
        load = self._loading.get(key)
        if load is None:
            load = asyncio.create_task(self._load(ref, key), name=f"sandbox-mount:{key[0]}")
            self._loading[key] = load
        return await asyncio.shield(load)

    async def _load(self, ref: FileRef, key: tuple[str, str, int]) -> bytes | None:
        from app.services.code_interpreter import MAX_INPUT_TOTAL_BYTES

        try:
            data = await _read_mount_bytes(self.file_context, ref)
            if data is not None and self.held_bytes + len(data) <= MAX_INPUT_TOTAL_BYTES:
                self._kept[key] = data
                self.held_bytes += len(data)
            return data
        finally:
            self._loading.pop(key, None)


async def collect_sandbox_inputs(
    file_context: FileContext | None,
    *,
    cache: SandboxInputCache | None = None,
) -> tuple[list[tuple[str, bytes]], dict[str, tuple[str, str]], list[str]]:
    """The conversation files to mount in the sandbox.

    Returns ``(inputs, planned, skipped)``: the ``(mount name, bytes)`` pairs -
    every file of *file_context* under :func:`safe_mount_name` plus an
    ``index.json`` mapping file ids to paths - the ``(attachments/name (size)
    line, original filename)`` per mount name, and the original names of the
    files left out (over :data:`MAX_INPUT_FILES` / :data:`MAX_INPUT_TOTAL_BYTES`,
    or whose bytes could not be loaded). Only the files the turn's
    ``FileContext`` already carries are candidates, read through its own
    ``read_bytes`` - the mount grants no access the context did not - and
    through *cache*, the turn's shared reads, when it was filled
    through this same context. A load failure is logged and skipped: the mount
    is a convenience for the code, never the reason it does not run. What the
    model is TOLD is decided after the run, from the names the sandbox reports
    it wrote (:func:`_mount_report`).
    """
    from app.services.code_interpreter import (
        MAX_INPUT_FILES,
        MAX_INPUT_TOTAL_BYTES,
        safe_mount_name,
    )

    inputs: list[tuple[str, bytes]] = []
    planned: dict[str, tuple[str, str]] = {}
    skipped: list[str] = []
    if not _has_sandbox_inputs(file_context):
        return inputs, planned, skipped
    shared = cache if cache is not None and cache.file_context is file_context else None
    taken: set[str] = {"index.json"}
    index: list[dict] = []
    total = 0
    # The run's own inputs first (the input document detached from the
    # prompt, ``attachments/input.json``): under their fixed name, outside the
    # file-count cap - the model was told that path before its first call.
    for name, data in (getattr(file_context, "sandbox_inputs", None) or {}).items():
        if name.casefold() in taken or total + len(data) > MAX_INPUT_TOTAL_BYTES:
            skipped.append(name)
            continue
        taken.add(name.casefold())
        total += len(data)
        inputs.append((name, data))
        planned[name] = (f"attachments/{name} ({_format_size(len(data))})", name)
        index.append(
            {
                "file_id": None,
                "filename": name,
                "path": f"attachments/{name}",
                "content_type": "application/json",
                "size": len(data),
            }
        )
    run_inputs = len(inputs)
    for ref in file_context.list_files():
        if len(inputs) - run_inputs >= MAX_INPUT_FILES:
            skipped.append(ref.filename)
            continue
        size = int(getattr(ref, "size", 0) or 0)
        if total + size > MAX_INPUT_TOTAL_BYTES:
            skipped.append(ref.filename)
            continue
        data = await (shared.read(ref) if shared is not None else _read_mount_bytes(file_context, ref))
        if data is None or total + len(data) > MAX_INPUT_TOTAL_BYTES:
            skipped.append(ref.filename)
            continue
        name = safe_mount_name(ref.filename, ref.file_id)
        if name.casefold() in taken:
            # Only two refs of ONE id can meet here (``file-<id>`` and
            # ``<id>``, :func:`mount_tag`): the file is mounted once. Never a
            # rename - the path every footer and refusal names is this one.
            skipped.append(ref.filename)
            continue
        taken.add(name.casefold())
        total += len(data)
        inputs.append((name, data))
        planned[name] = (f"attachments/{name} ({_format_size(len(data))})", ref.filename or name)
        index.append(
            {
                "file_id": ref.file_id,
                "filename": (ref.filename or "")[:255],
                "path": f"attachments/{name}",
                "content_type": ref.content_type,
                "size": len(data),
            }
        )
    if inputs:
        import json as _json

        inputs.append(("index.json", _json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8")))
    return inputs, planned, skipped


def _has_sandbox_inputs(file_context: FileContext | None) -> bool:
    """Whether *file_context* has anything to mount: a conversation file or a run input."""
    if file_context is None:
        return False
    return not file_context.is_empty or bool(getattr(file_context, "sandbox_inputs", None))


def _mount_report(
    planned: dict[str, tuple[str, str]], skipped: list[str], result: dict
) -> tuple[list[str], list[str]]:
    """What to announce: the planned lines the sandbox confirmed, the rest as not mounted.

    ``result["mounted"]`` lists the mount names the sandbox actually wrote; a
    planned file missing from it (a write that failed) joins the skipped
    list - under its original filename, like the files never planned -
    instead of being announced as an available path. A result WITHOUT the
    key confirmed nothing: an early return (the sandbox unreachable, the
    quota spent, the sandbox not created) must never read as a mount.
    """
    written = result.get("mounted") if isinstance(result, dict) else None
    confirmed = set(written or [])
    mounted = [line for name, (line, _filename) in planned.items() if name in confirmed]
    failed = [filename for name, (_line, filename) in planned.items() if name not in confirmed]
    return mounted, skipped + failed


def _format_mount_line(mounted: list[str], skipped: list[str]) -> str:
    """One bounded line telling the model what its code could open."""
    if not mounted and not skipped:
        return ""
    shown = mounted[:_MOUNT_LINE_MAX_ENTRIES]
    line = "[Conversation files mounted for this run: " + (", ".join(shown) or "none")
    if len(mounted) > len(shown):
        line += f", +{len(mounted) - len(shown)} more"
    if mounted:
        line += "; exact paths in attachments/index.json"
    if skipped:
        line += "; not mounted (too many, too large or failed to load): " + ", ".join(
            (name or "")[:80] for name in skipped[:6]
        )
        if len(skipped) > 6:
            line += f", +{len(skipped) - 6} more"
    return line + "]"


#: Said when the code fails on ``attachments/input.json`` in a run that holds
#: no input document: the path exists only for a detached ``$input``.
NO_INPUT_DOCUMENT_HINT = (
    "[No input document is mounted in this run: attachments/input.json exists only when "
    "the run's message carried a JSON document detached as $input (agent config "
    'output_mode "patch" or input_reference true, or a document too large for the context). '
    "Read the document from the message instead.]"
)


def _opens_absent_input_document(error_text: str, file_context: FileContext | None) -> bool:
    """The code failed on ``attachments/input.json`` and the run mounts no input document."""
    from app.tools.session_input import INPUT_MOUNT_NAME

    if f"attachments/{INPUT_MOUNT_NAME}" not in error_text:
        return False
    if not re.search(r"FileNotFoundError|No such file or directory", error_text):
        return False
    return not (getattr(file_context, "sandbox_inputs", None) or {}).get(INPUT_MOUNT_NAME)


def _unmounted_paths_hint(file_context: FileContext | None, error_text: str = "") -> str:
    """For a run that failed to open a file: what was not there.

    The input document's path (``attachments/input.json``) opened in a run that
    mounts none is answered as such; otherwise, for a run that did not name
    ``attachments``, where the conversation files would have been (metadata
    only, no download).
    """
    from app.services.code_interpreter import mount_path_hint

    if _opens_absent_input_document(error_text, file_context):
        return NO_INPUT_DOCUMENT_HINT
    if file_context is None or file_context.is_empty:
        return ""
    refs = file_context.list_files()
    paths = [mount_path_hint(ref.filename, ref.file_id) for ref in refs[:_MOUNT_LINE_MAX_ENTRIES]]
    more = f", +{len(refs) - len(paths)} more" if len(refs) > len(paths) else ""
    return (
        "[The conversation files are mounted when the code opens them under attachments/: "
        + ", ".join(paths)
        + more
        + "]"
    )


def build_code_interpreter_tool(
    *,
    db: AsyncSession | None = None,
    user_id: str | None = None,
    quota_user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    image_sink: list[str] | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the sandboxed Python code-interpreter tool.

    Args:
        db / user_id / conversation_id / assignment_run_id: persistence
            context. When provided, files the code writes to disk (CSV, XLSX,
            saved images, etc.) are saved as durable ``FileAttachment`` rows
            (linked to the conversation / assignment run) and surfaced via
            ``[[FILE:id]]`` markers — so sandbox files persist as conversation
            files instead of being deleted with the temp dir.
        quota_user_id: user to charge the combined agentic+sandbox quota
            against, when it differs from *user_id* — e.g. a company-managed
            assignment run has no personal file owner (``user_id=None``, by
            design) but still has a charged/owning user whose quota should
            gate and record this dispatch. Defaults to *user_id* when not
            given.
        image_sink: Optional mutable list; when provided, base-64 chart images
            produced by matplotlib are appended here so the caller can inject
            them into the response.

    Returns:
        ``(definitions, executor)`` ready for ``CompositeToolExecutor.add_tools``.
    """

    # The turn's mount downloads: each file read once for every call
    # this tool serves - a parallel batch included - through the FileContext
    # it was read from. Held by the tool, which every entry point builds per
    # turn (a HITL resume is a new execution, a delegated task a new run), and
    # not by the FileContext, which a caller may keep past its turn: the kept
    # bytes go with the turn's tools.
    turn_reads: SandboxInputCache | None = None

    def _turn_reads(file_context: FileContext) -> SandboxInputCache:
        nonlocal turn_reads
        if turn_reads is None or turn_reads.file_context is not file_context:
            turn_reads = SandboxInputCache(file_context)
        return turn_reads

    # The conversation's (or the run's) sandbox workspace: opened on
    # first use, so a turn that never touches it creates nothing.
    from app.services.builtin_tools.sandbox_workspace import SandboxToolExecutor, build_sandbox_workspace_tools
    from app.services.sandbox_workspace.session import (
        WorkspaceSession,
        WorkspaceUnavailableError,
        workspace_enabled,
    )
    from app.services.sandbox_workspace.store import WorkspaceScope

    scope = (
        WorkspaceScope.build(user_id=user_id, conversation_id=conversation_id, assignment_run_id=assignment_run_id)
        if workspace_enabled()
        else None
    )
    workspace = WorkspaceSession(scope) if scope is not None else None

    async def _execute_code(
        code: str,
        session: bool = False,
        reset: bool = False,
        packages: list[str] | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        from app.services.code_interpreter import code_reads_attachments, execute_python, format_text_result

        install_line = ""
        if packages:
            install_line = await _install_packages(packages)

        # Best-effort attribution for per-user sandbox usage recording
        # (spec 002-sandbox-quota-usage-tracking) — a malformed user_id must
        # not break code execution itself, just skip usage attribution.
        _usage_user_id = safe_uuid(user_id)
        _quota_user_id = safe_uuid(quota_user_id)

        # The conversation files ride into the sandbox under ``attachments/`` -
        # only when the code names that directory as a path, and
        # downloaded by the sandbox itself once the run has passed its gates
        # (reachable, quota open, created): a run that never opens them, or
        # one the sandbox refuses, costs no transfer.
        plan: dict[str, Any] = {}

        async def _load_inputs() -> list[tuple[str, bytes]]:
            cache = _turn_reads(file_context) if file_context is not None else None
            inputs, planned, skipped = await collect_sandbox_inputs(file_context, cache=cache)
            plan.update(inputs=inputs, planned=planned, skipped=skipped)
            return inputs

        # ``input_files`` only when there may be something to mount: the plain
        # call keeps every caller and test double of ``execute_python`` that
        # predates the mount working unchanged.
        wants_mount = code_reads_attachments(code) and _has_sandbox_inputs(file_context)
        extra: dict[str, Any] = {"input_files": _load_inputs} if wants_mount else {}
        # Only the keyword arguments in use: every caller and test double of
        # ``execute_python`` that predates the workspace keeps working. The
        # workspace is in use when the code names it, asks for the kernel, or
        # may import what the workspace installed.
        from app.services.sandbox_workspace.run import code_names_workspace

        if workspace is not None and (session is True or code_names_workspace(code) or workspace.may_hold_packages):
            extra["workspace"] = workspace
        if session is True:
            extra.update(session=True, reset=reset is True)
        mounted = workspace if "workspace" in extra and workspace is not None and workspace.available else None
        # Read-your-writes within a tool round (#11714): a call that uses the workspace is counted as a
        # write from its start (it writes back what its code changed), so a reader of the round waits
        # for it; and it mounts only once the writing tools of the round committed, so it runs on the
        # file an edit next to it wrote. Two code_interpreter calls of one round do not wait for each other.
        with mounted.writing(run=True) if mounted is not None else contextlib.nullcontext():
            if mounted is not None:
                await mounted.settled(tools_only=True)
            result = await execute_python(
                code, timeout=300, user_id=_usage_user_id, quota_user_id=_quota_user_id, db=db, **extra
            )
        if image_sink is not None:
            image_sink.extend(result.get("images", []))
        text = format_text_result(result)
        if install_line:
            text = f"{install_line}\n{text}"
        planned = plan.get("planned") or {}
        skipped = plan.get("skipped") or []
        if planned or skipped:
            mounted, not_mounted = _mount_report(planned, skipped, result)
            mount_line = _format_mount_line(mounted, not_mounted)
            if mount_line:
                text = f"{mount_line}\n\n{text}"
            if "input.json" not in planned and _opens_absent_input_document(text, file_context):
                text = f"{text}\n\n{NO_INPUT_DOCUMENT_HINT}"
        elif re.search(r"FileNotFoundError|No such file or directory", text):
            hint = _unmounted_paths_hint(file_context, text)
            if hint:
                text = f"{text}\n\n{hint}"

        if result.get("platform_failure"):
            # No container, so no run: an error result, which the agent loop
            # records as a tool_error friction of the run.
            return ToolResult(tool_call_id="", content=text, is_error=True)
        output_files = result.get("output_files") or []
        # The run and its file stand: the warning is what the model reads next
        # to the marker, so a document drawn here is not handed over as one.
        if sandbox_drew_pdf_document(code, output_files):
            text = f"{text}\n\n{SANDBOX_PDF_WARNING}"
        # Persist whenever there is a DB session AND something to scope the
        # file by: an owner (``user_id``) OR a conversation / assignment run.
        # Company-managed assignment runs intentionally have no file owner
        # (``_resolve_run_file_owner_id`` returns ``None``) but still provide
        # an ``assignment_run_id`` scope, so gating on ``user_id`` alone would
        # wrongly discard the files such runs write to disk.
        if not output_files or db is None or not (
            user_id or conversation_id or assignment_run_id
        ):
            return text

        produced: list = []
        markers: list[str] = []
        for item in output_files:
            if not isinstance(item, dict):
                continue
            data_b64 = item.get("data_base64")
            filename = item.get("filename")
            if (
                not isinstance(data_b64, str)
                or not isinstance(filename, str)
                or not filename.strip()
            ):
                continue
            try:
                raw = b64decode(data_b64, validate=True)
            except Exception:
                logger.warning(
                    "Skipping code-interpreter output file %r: invalid base64",
                    filename,
                )
                continue
            try:
                file_ref = await persist_tool_artifact(
                    db=db,
                    user_id=user_id or "",
                    filename=filename.strip(),
                    raw_bytes=raw,
                    content_type=item.get("content_type"),
                    conversation_id=conversation_id,
                    assignment_run_id=assignment_run_id,
                    file_context=file_context,
                    source="agent_generated",
                )
            except Exception:
                logger.warning(
                    "Persisting code-interpreter output file %r failed",
                    filename,
                    exc_info=True,
                )
                continue
            produced.append(file_ref)
            markers.append(f"[[FILE:{file_ref.file_id}]]")

        if not produced:
            return text
        note = (
            f"\n\nThe code wrote {len(produced)} file(s), now saved as "
            "downloadable files. Include the following marker(s) verbatim in "
            "your response so the user can download them: " + " ".join(markers)
        )
        return ToolResult(tool_call_id="", content=text + note, files=produced)

    async def _install_packages(packages: object) -> str:
        from app.core.config import settings
        from app.services.sandbox_workspace.packages import install

        if workspace is None or not workspace.available:
            reason = workspace.unavailable_reason if workspace is not None else "this session has no workspace"
            return f"[Packages: not installed: {reason}.]"
        requested = [packages] if isinstance(packages, str) else [str(p) for p in packages or [] if str(p).strip()]
        pure_only = (getattr(settings, "custom_tool_sandbox", "auto") or "auto") == "opensandbox"
        try:
            report = await install(workspace, requested, pure_only=pure_only)
        except WorkspaceUnavailableError as exc:
            return f"[Packages: not installed: {exc}.]"
        return report.format()

    executor = SandboxToolExecutor(workspace)
    executor.register("code_interpreter", _execute_code)

    properties: dict[str, Any] = {
        "code": {
            "type": "string",
            "description": (
                "Python code to execute. Without session=true each call starts with a clean "
                "interpreter (no state carried over from previous calls), "
                "so define everything you need here. Use print() for text "
                "output and matplotlib (plt.show()) for charts."
            ),
        },
    }
    if workspace is not None:
        properties["session"] = {
            "type": "boolean",
            "description": (
                "Run in this conversation's long-lived kernel: variables and imports persist from one "
                "session=true call to the next (until it idles out). Default false: a fresh process."
            ),
        }
        properties["reset"] = {"type": "boolean", "description": "With session=true: start a new kernel first."}
        from app.services.sandbox_workspace.packages import allowed_packages

        if allowed_packages():
            properties["packages"] = {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Packages to install into workspace/.venv before the code runs (name or name==version, "
                    "from the platform's allow-list; the sandbox has no network). They persist; the result "
                    "lists what was installed."
                ),
            }

    definition = ToolDefinition(
        name="code_interpreter",
        description=code_interpreter_description(workspace),
        input_schema={"type": "object", "properties": properties, "required": ["code"]},
        source="builtin:code_interpreter",
        describe_for_surface=(lambda _surface: code_interpreter_description(workspace)) if workspace is not None else None,
    )

    definitions = [definition]
    if workspace is not None:
        definitions += build_sandbox_workspace_tools(
            workspace,
            executor,
            db=db,
            user_id=user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
        )
    return definitions, executor


_CODE_INTERPRETER_BASE = (
    "Execute Python in a sandboxed environment for data analysis, "
    "calculations, charts, and data processing. Supports pandas, numpy, "
    "matplotlib, and the standard library. Use print() for text output; "
    "matplotlib figures (call plt.show()) are captured and shown to the "
    "user automatically. Files you write to disk (e.g. "
    "df.to_csv('report.csv'), wb.save('data.xlsx')) are saved as "
    "downloadable files — include any returned [[FILE:id]] markers "
    "verbatim in your response. "
    "The conversation's files are readable as disposable copies under "
    "attachments/ (attachments/index.json maps file ids to paths): compute "
    "over a whole large file here instead of reading it window by window. "
)
_CODE_INTERPRETER_STATELESS = (
    "State is NOT persisted between calls: every execution runs in a "
    "fresh, isolated process, so variables, imports, the sandbox "
    "working directory, and any in-memory data from one call are gone "
    "on the next. Put everything a task needs into a single call, and "
    "surface results you will need later via print() (the output is "
    "added to the conversation) rather than relying on a variable "
    "persisting to a follow-up call. "
)
_CODE_INTERPRETER_WORKSPACE = (
    "Variables and imports are NOT persisted between calls by default: each execution runs in a fresh, "
    "isolated process (put what one step needs into a single call), unless you pass session=true to run "
    "in this conversation's kernel, where they persist. Files under workspace/ (the WORKSPACE_DIR "
    "directory) DO persist across calls, turns and a resumed run: keep sources, data and results there "
    "and work in small steps. to_workspace_path (on file and fetch tools), sandbox_put_file and "
    "materialize_repository put content there without it passing through your context; "
    "sandbox_get_file hands a workspace file to the user. "
)
_CODE_INTERPRETER_TAIL = (
    "matplotlib PDF output is for chart figures only; documents (reports, "
    "inventories, minutes) are rendered by generate_file from Markdown or "
    "HTML, never drawn in the sandbox."
)


def code_interpreter_description(workspace: object | None) -> str:
    """The ``code_interpreter`` description: the workspace sentence and its manifest when there is one."""
    if workspace is None or not getattr(workspace, "available", False):
        return _CODE_INTERPRETER_BASE + _CODE_INTERPRETER_STATELESS + _CODE_INTERPRETER_TAIL
    manifest = getattr(workspace, "described", None)
    if not manifest:
        holds = ""
    elif manifest.startswith("The workspace"):
        holds = f"{manifest} "
    else:
        holds = f"The workspace now holds: {manifest} "
    return _CODE_INTERPRETER_BASE + _CODE_INTERPRETER_WORKSPACE + holds + _CODE_INTERPRETER_TAIL


# ── Image Generation ─────────────────────────────────────────────────────────


class ImageInputError(ValueError):
    """Reference pictures or a mask the image model cannot take; ``str()`` names the model and the fix."""


def _reference_limit(provider: object, model: str | None) -> int:
    limit = getattr(provider, "image_reference_limit", None)
    return int(limit(model)) if callable(limit) else 0


async def _image_inputs(
    provider: object, model: str | None, user_id: str, file_ids: list, mask_file_id: str | None
) -> tuple[list[tuple[bytes, str]], tuple[bytes, str] | None]:
    """The reference pictures and the mask of an image to draw, checked against what *model* takes.

    Each is a picture the person can read (an SVG drawn by the platform's
    bounded rasteriser); a model that takes none, or fewer, is refused by name.
    """
    from app.core.config import settings
    from app.models.file_attachment import FileAttachment
    from app.models.user import User
    from app.services.builtin_tools._shared import _file_handle_key
    from app.services.file_access import can_read_file
    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.svg_raster import rasterize_svg_async

    ids = [_file_handle_key(str(f)).strip() for f in file_ids if str(f or "").strip()]
    if not ids and not mask_file_id:
        return [], None
    if mask_file_id and not ids:
        raise ImageInputError("A mask edits the first reference picture: give reference_file_ids with it.")
    limit = _reference_limit(provider, model)
    if limit <= 0:
        raise ImageInputError(f"The image model {model} draws from no reference picture: describe the image instead.")
    if len(ids) > limit:
        raise ImageInputError(f"The image model {model} draws from at most {limit} reference pictures.")
    takes_mask = getattr(provider, "image_takes_mask", None)
    if mask_file_id and not (callable(takes_mask) and takes_mask(model)):
        raise ImageInputError(f"The image model {model} takes no edit mask: drop mask_file_id.")

    async def read(file_id: str, what: str) -> tuple[bytes, str]:
        async with _own_session() as db:
            user = await db.get(User, safe_uuid(user_id)) if safe_uuid(user_id) else None
            row = await db.get(FileAttachment, safe_uuid(file_id)) if safe_uuid(file_id) else None
            readable = (
                user is not None
                and row is not None
                and (row.content_type or "").startswith("image/")
                and await can_read_file(row, user, db)
            )
        if not readable:
            raise ImageInputError(f"No picture `{file_id}` you can read for {what}.")
        try:
            data = await file_storage.download(row.s3_object_name)
        except FileNotFoundInStorageError:
            raise ImageInputError(f"The picture `{row.filename}` is gone from storage.") from None
        if (row.content_type or "").startswith("image/svg"):
            png = await rasterize_svg_async(data, box=2048, strict=settings.svg_strict_rendering)
            if not png:
                raise ImageInputError(f"The SVG `{row.filename}` could not be drawn.")
            return png, "image/png"
        return data, row.content_type

    pictures = [await read(fid, f"reference picture {index + 1}") for index, fid in enumerate(ids)]
    mask = await read(_file_handle_key(str(mask_file_id)).strip(), "the mask") if mask_file_id else None
    return pictures, mask


async def build_image_generation_tool(
    db: AsyncSession,
    user_id: str,
    conversation_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """Build the AI image-generation tool.

    Returns *None* when no image provider is configured.
    """
    try:
        from app.llm.router import get_image_provider

        img_provider, img_model = await get_image_provider(db)
    except (ValueError, NotImplementedError) as exc:
        logger.debug("Image generation tool unavailable: %s", format_exception_message(exc))
        return None

    from app.models.chat_image import ChatImage

    async def _generate_image(
        prompt: str,
        size: str = "1024x1024",
        reference_file_ids: object = None,
        mask_file_id: str | None = None,
        **_kw: object,
    ):
        from app.llm.base import FileRef
        from app.llm.images import draw_retrying, failure_message
        from app.tools.base import ToolResult
        from app.tools.tool_events import current_tool_deadline

        references = reference_file_ids if isinstance(reference_file_ids, list) else (
            [reference_file_ids] if reference_file_ids else []
        )
        try:
            pictures, mask = await _image_inputs(img_provider, img_model, user_id, references, mask_file_id)
        except ImageInputError as exc:
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)

        async def draw() -> bytes:
            if pictures:
                return await img_provider.edit_image(prompt, pictures, mask=mask, model=img_model, size=size)
            return await img_provider.generate_image(prompt, model=img_model, size=size)

        try:
            image_bytes = await draw_retrying(draw, deadline=current_tool_deadline())
        except Exception as e:
            logger.warning("Image generation failed: %s", format_exception_message(e))
            # The provider's answer to a right call: counted per call by the failure brake, never per tool.
            return ToolResult(tool_call_id="", content=failure_message(e, img_model), is_error=True, outcome_failure=True)
        try:
            try:
                parsed_uid = uuid.UUID(user_id)
            except (ValueError, AttributeError):
                parsed_uid = None

            file_ref = None
            file_att = None
            # Gemini answers JPEG: label the bytes as what they are; PNG for anything unrecognised.
            content_type = sniff_raster_type(image_bytes) or "image/png"
            # Store in S3 for persistent access
            try:
                from app.models.file_attachment import FileAttachment
                from app.services.file_storage import file_storage

                conv_id = uuid.UUID(conversation_id) if conversation_id else None
                s3_prefix = (
                    f"generated/{conversation_id}" if conversation_id else "generated"
                )
                filename = f"image_{uuid.uuid4().hex[:8]}{ALLOWED_IMAGE_TYPES[content_type]}"
                s3_object_name = await file_storage.upload(
                    data=image_bytes,
                    filename=filename,
                    content_type=content_type,
                    prefix=s3_prefix,
                )
                file_att = FileAttachment(
                    id=uuid.uuid4(),
                    user_id=parsed_uid,
                    conversation_id=conv_id,
                    filename=filename,
                    content_type=content_type,
                    size=len(image_bytes),
                    s3_object_name=s3_object_name,
                    source="agent",
                )
                file_ref = FileRef.from_attachment(file_att)
                file_url = f"/api/v1/chat/files/{file_att.id}/download"
            except Exception as s3_err:
                logger.warning(
                    "S3 storage for generated image failed (falling back to DB): %s",
                    format_exception_message(s3_err),
                )
                file_url = None

            # Also keep DB storage for backward compatibility
            chat_img = ChatImage(
                id=uuid.uuid4(),
                user_id=parsed_uid,
                conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
                prompt=prompt,
                model=img_model,
                image_data=image_bytes,
                content_type=content_type,
            )
            # Persist on a dedicated short-lived session, NOT the captured
            # request session: this closure runs mid agentic loop (after the
            # caller released its pooled connection), and pending writes on
            # the request session would re-check-out a connection and pin it
            # across every remaining LLM call of the loop.
            async with _own_session() as img_db:
                if file_att is not None:
                    img_db.add(file_att)
                img_db.add(chat_img)
                await img_db.commit()

            # Prefer S3 URL, fall back to DB-served URL
            img_url = file_url or f"/api/v1/chat/images/{chat_img.id}"
            named = f" (file_id {file_att.id})" if file_att is not None else ""
            content = (
                f"Image generated successfully{named}. Include this EXACT markdown in "
                f"your response (use the relative path verbatim, do NOT prepend "
                f"any domain or hostname): ![{prompt}]({img_url})"
            )
            return ToolResult(
                tool_call_id="",
                content=content,
                files=[file_ref] if file_ref else None,
            )
        except Exception as e:
            logger.error("Generated image not stored: %s", format_exception_message(e), exc_info=True)
            return ToolResult(
                tool_call_id="",
                content=f"The image was drawn but could not be stored: {format_exception_message(e)}",
                is_error=True,
            )

    executor = CallableToolExecutor()
    executor.register("generate_image", _generate_image)

    if load := getattr(img_provider, "load_image_capabilities", None):
        await load(img_model)
    references = _reference_limit(img_provider, img_model)
    properties: dict = {
        "prompt": {
            "type": "string",
            "description": "Detailed text description of the image to generate",
        },
        "size": {
            "type": "string",
            "description": "Image size (default: 1024x1024)",
            "default": "1024x1024",
        },
    }
    drawn_from = ""
    if references:
        # Only offered when the configured image model takes them.
        properties["reference_file_ids"] = {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": references,
            "description": (
                f"1-{references} pictures (files) the image is drawn from: the exact logo, product or "
                "character to use as it is, or a still to edit."
            ),
        }
        drawn_from = f" With reference_file_ids (1-{references} pictures), it draws from them: the exact logo to use"
        if img_provider.image_takes_mask(img_model):
            properties["mask_file_id"] = {
                "type": "string",
                "description": "A PNG the size of the first reference picture: it is edited only where the mask is transparent.",
            }
            drawn_from += ", a still to edit where a mask (mask_file_id) is transparent"
        drawn_from += "."
    definition = ToolDefinition(
        name="generate_image",
        description=(
            "Generate an image from a text description using AI. Use this when "
            "the user asks you to create, draw, or generate an image, "
            "illustration, or visual. Returns a Markdown image reference to "
            f"include verbatim in your response so the image renders.{drawn_from}"
        ),
        input_schema={"type": "object", "properties": properties, "required": ["prompt"]},
        source="builtin:files",
    )

    return [definition], executor


# ── Agent File Generation ─────────────────────────────────────────────────────

# HTML / Markdown → PDF rendering now lives in the shared ``pdf_render``
# module so the OpenCTI ``upload_opencti_file`` tool (and any future channel
# exporter) produces byte-identical PDFs to the ``generate_file`` tool below.
# ``_render_text_to_pdf`` keeps its leading-underscore name because
# ``_generate_file`` calls it and the test-suite monkeypatches
# ``builtin_tools._render_text_to_pdf``. The other rendering helpers
# (``looks_like_html`` / ``render_markdown_to_html`` / ``wrap_content_as_html``)
# are referenced via the ``pdf_render`` module directly.
PDF_RENDER_FORMAT = pdf_render.PDF_RENDER_FORMAT
PDF_RENDER_MARGIN = pdf_render.PDF_RENDER_MARGIN
_render_text_to_pdf = pdf_render.render_text_to_pdf

# Markdown / HTML → DOCX rendering lives in the shared ``docx_render``
# module (python-docx). Same module-level rebinding convention as
# ``_render_text_to_pdf`` so tests can monkeypatch
# ``builtin_tools._render_text_to_docx``.
DOCX_CONTENT_TYPE = docx_render.DOCX_CONTENT_TYPE
_render_text_to_docx = docx_render.render_text_to_docx


def build_generate_file_tool(
    db: AsyncSession,
    user_id: str,
    conversation_id: str | None = None,
    *,
    assignment_run_id: str | None = None,
    agent_id: str | None = None,
    agent_company_managed: bool = False,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the generate_file tool for agent-produced downloadable artifacts.

    *agent_id* (with *agent_company_managed*) is the acting agent: given, a
    file id the turn does not hold is also looked up among the attachments of
    the work items that agent may read (``work_item_files.resolve_readable_attachment``).
    """
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type

    #: The refusal of an id prefix that starts several work item attachments,
    #: by the id as spelled, answered by :func:`_unresolved` instead of not found.
    ambiguous: dict[str, str] = {}

    async def _resolve(file_context: FileContext | None, file_id: object) -> FileRef | None:
        """The file *file_id* names: the turn's context, this run's own rows, then
        the attachments of the work items the agent may read.

        A file the context does not hold is registered in it once found, so
        it stays reachable for the rest of the run under the same ledgers as
        every other file.
        """
        ref = _resolve_file_ref(file_context, file_id)
        if ref is not None or file_context is None or not isinstance(file_id, str):
            return ref
        ref = await _load_scoped_file_ref(
            file_id, conversation_id=conversation_id, assignment_run_id=assignment_run_id
        )
        if ref is None and agent_id:
            from app.services.work_item_files import resolve_readable_attachment

            ref, refusal = await resolve_readable_attachment(
                file_id,
                user_id=user_id,
                agent_id=agent_id,
                agent_company_managed=agent_company_managed,
            )
            if refusal:
                ambiguous[file_id] = refusal
        if ref is not None:
            file_context.add(ref)
        return ref

    def _deleted(ref: FileRef) -> ToolResult:
        """The answer for a file whose bytes are gone (a tombstone): a clear sentence, never an exception."""
        return ToolResult(
            tool_call_id="",
            content=(
                f"Error: {ref.deleted_notice} Its content cannot be read any more (file_id={ref.file_id}, "
                f"{ref.filename}); say so, and make it again if it is still needed."
            ),
            is_error=True,
        )

    def _unresolved(file_context: FileContext | None, file_id: object) -> ToolResult:
        refusal = ambiguous.pop(file_id, None) if isinstance(file_id, str) else None
        return ToolResult(
            tool_call_id="",
            content=refusal or _unresolved_file_message(file_context, file_id),
            is_error=True,
        )

    async def _load_source_text(
        source_file_id: str, file_context: FileContext | None
    ) -> str | ToolResult:
        """The text of the file ``generate_file`` renders instead of ``content``, or the refusal."""
        ref = await _resolve(file_context, source_file_id)
        if ref is None or file_context is None:
            return _unresolved(file_context, source_file_id)
        if ref.is_deleted:
            return _deleted(ref)
        if not is_renderable_source_file(ref.filename, ref.content_type):
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: '{ref.filename}' ({ref.content_type}) is not a Markdown, HTML or plain "
                    "text file, so generate_file cannot render it. source_file_id takes the .md / "
                    ".html / .txt source of a document; to render a document you only have as "
                    "PDF / DOCX, write its Markdown to a file first."
                ),
                is_error=True,
            )
        try:
            raw = await file_context.read_bytes(ref.file_id)
        except Exception as exc:
            logger.warning(
                "generate_file could not load source %s (%s): %s",
                ref.file_id,
                ref.filename,
                format_exception_message(exc),
                exc_info=True,
            )
            raw = None
        if raw is None:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the content of '{ref.filename}' could not be loaded from storage. "
                    "The file may have been removed."
                ),
                is_error=True,
            )
        if b"\x00" in raw:
            return ToolResult(
                tool_call_id="",
                content=f"Error: '{ref.filename}' holds binary data, not text; generate_file cannot render it.",
                is_error=True,
            )
        if raw.startswith(UTF8_BOM):
            raw = raw[len(UTF8_BOM) :]
        return raw.decode("utf-8", errors="replace")

    async def _generate_file(
        filename: str,
        content: str | None = None,
        content_type: str | None = None,
        encoding: str | None = None,
        file_tag: str = "working_file",
        source_file_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> ToolResult:
        from app.llm.base import FileRef

        if not filename or not filename.strip():
            return ToolResult(
                tool_call_id="",
                content="filename is required.",
                is_error=True,
            )
        if source_file_id is not None and not isinstance(source_file_id, str):
            return ToolResult(
                tool_call_id="",
                content="source_file_id must be a string (a file_id, a [[FILE:id]] marker or an exact filename).",
                is_error=True,
            )
        has_source = bool(source_file_id and source_file_id.strip())
        # A model often sends an empty string for an optional field it does not use.
        if has_source and content == "":
            content = None
        if has_source == (content is not None):
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: pass exactly one of content (the text to write) or source_file_id "
                    "(an existing Markdown / HTML / text file of this conversation to render)."
                    if has_source
                    else "Error: content or source_file_id is required - pass exactly one of them."
                ),
                is_error=True,
            )
        if has_source:
            if encoding == "base64":
                return ToolResult(
                    tool_call_id="",
                    content=(
                        "Error: encoding='base64' cannot be combined with source_file_id: the source "
                        "is a text file of this conversation, rendered as it is stored."
                    ),
                    is_error=True,
                )
            loaded = await _load_source_text(source_file_id, file_context)
            if isinstance(loaded, ToolResult):
                return loaded
            content = loaded
        if not isinstance(content, str):
            return ToolResult(
                tool_call_id="",
                content="content must be a string.",
                is_error=True,
            )

        # Resolve the effective content type BEFORE we touch ``content``
        # so the PDF-rendering branch below can decide what to do with
        # plain / Markdown / HTML input.  Auto-detection from the
        # filename extension covers the typical "report.pdf" hint
        # without forcing the agent to pass ``content_type`` explicitly.
        #
        # Normalise to a lowercase BASE MIME type — drop any ``; charset=…``
        # parameters and casing. Without this, an LLM-supplied
        # ``Application/PDF`` or ``application/pdf; charset=utf-8`` would fail
        # the ``== "application/pdf"`` check below, silently bypass the
        # Playwright renderer, and persist UTF-8-encoded Markdown/HTML bytes
        # under a ``.pdf`` filename. Normalising here also keeps the stored
        # ``content_type`` consistent for the downstream
        # ``startswith("image/")`` inline-vs-download and chip-label logic.
        eff_content_type = (
            (content_type or guess_content_type(filename))
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        # The document's size for ROI (#10544), read before an embedded image
        # turns into a data: URI; unknown for bytes handed in already rendered.
        source_chars = None if encoding == "base64" else len(content)

        # PDF rendering path — when the requested output is a PDF and the
        # caller didn't already encode binary PDF bytes (``encoding =
        # 'base64'``), treat ``content`` as text / Markdown / HTML and
        # render it to PDF on the backend via Playwright.  This is the
        # piece that closes the "agent only generated HTML, not PDF"
        # gap: LLMs cannot reliably emit valid PDF binary inline, so
        # we let them author the *content* in their natural format
        # (Markdown / HTML) and convert here.
        if eff_content_type == "application/pdf" and encoding != "base64":
            try:
                content = await _inline_embedded_file_refs(content, file_context)
                raw_bytes = await _render_text_to_pdf(
                    content,
                    title=filename.rsplit(".", 1)[0],
                )
            except Exception as exc:
                logger.warning(
                    "PDF rendering failed for %s: %s",
                    filename,
                    format_exception_message(exc),
                    exc_info=True,
                )
                return ToolResult(
                    tool_call_id="",
                    content=(
                        "PDF rendering failed. The headless browser used for "
                        "PDF generation could not render the supplied content. "
                        "Either supply pre-rendered PDF bytes via "
                        "encoding='base64', or fall back to text/html / "
                        "text/markdown by changing the filename extension."
                    ),
                    is_error=True,
                )
        # DOCX rendering path — same contract as PDF: LLMs cannot emit
        # valid OOXML ZIP binary inline, so treat ``content`` as text /
        # Markdown / HTML and render a real Word document server-side via
        # python-docx. Without this branch the content was UTF-8
        # byte-encoded verbatim and saved under a ``.docx`` name — a file
        # Word refuses to open.
        elif eff_content_type == DOCX_CONTENT_TYPE and encoding != "base64":
            try:
                content = await _inline_embedded_file_refs(content, file_context)
                raw_bytes = await _render_text_to_docx(
                    content,
                    title=filename.rsplit(".", 1)[0],
                )
            except Exception as exc:
                logger.warning(
                    "DOCX rendering failed for %s: %s",
                    filename,
                    format_exception_message(exc),
                    exc_info=True,
                )
                return ToolResult(
                    tool_call_id="",
                    content=(
                        "DOCX rendering failed. The Word document renderer "
                        "could not process the supplied content. Either "
                        "supply pre-built DOCX bytes via encoding='base64', "
                        "or fall back to text/markdown / application/pdf by "
                        "changing the filename extension."
                    ),
                    is_error=True,
                )
        else:
            try:
                if encoding == "base64":
                    raw_bytes = b64decode(content, validate=True)
                elif encoding in (None, "", "utf-8", "text"):
                    raw_bytes = content.encode("utf-8")
                else:
                    return ToolResult(
                        tool_call_id="",
                        content="Unsupported encoding. Use 'base64' or omit encoding for UTF-8 text.",
                        is_error=True,
                    )
            except Exception:
                return ToolResult(
                    tool_call_id="",
                    content="Invalid base64 content.",
                    is_error=True,
                )

        if len(raw_bytes) > MAX_UPLOAD_SIZE_BYTES:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"File too large ({len(raw_bytes):,} bytes). "
                    f"Maximum is {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB."
                ),
                is_error=True,
            )

        effective_file_tag = (file_tag or "working_file").strip().lower()
        if effective_file_tag not in {"working_file", "download_file"}:
            return ToolResult(
                tool_call_id="",
                content="Invalid file_tag. Use 'working_file' or 'download_file'.",
                is_error=True,
            )

        # ``FileAttachment.source`` is a provenance category elsewhere in
        # the codebase ("chat", "knowledge_base", …) and is exposed
        # verbatim by ``GET /chat/files/{id}/info``.  Storing the bare
        # display tags ("working_file" / "download_file") would leak UI
        # labels into a provenance field and break any downstream
        # consumer that switches on ``source``.  Encode the display
        # intent into a provenance-style value (``agent_working`` /
        # ``agent_download``) so the field stays semantically a
        # category — both values communicate "came from
        # ``generate_file``" with finer granularity on intent.
        # ``attachment_meta_from_file_attachment`` maps these back to
        # the original ``file_tag`` shape for the API / frontend.
        provenance_source = (
            "agent_download" if effective_file_tag == "download_file" else "agent_working"
        )
        file_att = await _persist_generated_file(
            db=db,
            user_id=user_id,
            filename=filename.strip(),
            raw_bytes=raw_bytes,
            content_type=eff_content_type,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            source=provenance_source,
            match_filename=True,
        )
        file_ref = FileRef.from_attachment(file_att)
        if assignment_run_id is None:
            # ROI of chat work (#10544, S2c): a PDF or Word document written in a
            # conversation is a deliverable, sized by its source text. A run's
            # file is automation, counted by its assignment.
            from app.services.roi_chat_records import record_document_file

            await record_document_file(
                file_id=file_ref.file_id,
                conversation_id=conversation_id,
                user_id=user_id,
                filename=file_ref.filename,
                content_type=eff_content_type,
                source_chars=source_chars,
            )
        earlier = _produced_same_name(file_context, file_ref)
        register_produced_file(file_context, file_ref)
        content = f"[[FILE:{file_ref.file_id}]]"
        if earlier is not None:
            content += "\n\n" + _same_name_note(file_ref, earlier)
        return ToolResult(
            tool_call_id="",
            content=content,
            files=[file_ref],
        )

    # ── "Look once, remember" ─────────────────────────────────────
    # The turn's vision ledger is ``FileContext.vision_views``: file id -> the
    # 1-based loop step at which a COMPLETED provider request first carried
    # the file's pixels. The LOOP writes it (``_consume_transcript``), for
    # every image-bearing result whatever tool produced it - a read, a
    # browser screenshot, a rendered figure, a generated image, a cache
    # replay - and this tool only READS it: a file the model has seen is
    # answered with the notes it saved (``annotate_file``) or an explicit
    # refusal naming that tool, never the pixels again. Nothing is recorded
    # here at tool time: a result appended and then pruned before any request
    # carried it was never seen, and the next read must carry the pixels
    # again (the recovery path of every dropped-files note). The per-turn cap
    # (``AGENT_LOOP_VISION_VIEWS_PER_TURN``) is the loop's too, applied where
    # results are appended, so a screenshot and a read spend the same budget.
    # The 25 Sep 2026 run re-attached three images ~40 times; each attachment
    # lived one step before the retention pass pruned it and the stub invited
    # the next call. Bounding the ATTACHMENTS is what ends that: the model
    # reads its notes instead.

    def _ledger(file_context) -> dict[str, int]:
        shared = getattr(file_context, "vision_views", None)
        return shared if isinstance(shared, dict) else {}

    def _notes_line(ref) -> str:
        notes = " ".join(str(getattr(ref, "notes", "") or "").split())
        return f" Your notes: {notes}" if notes else ""

    def _show_image(ref, header: str, file_context=None) -> ToolResult:
        """Attach *ref* as a vision block - or answer with the notes / a refusal."""
        seen_step = _ledger(file_context).get(ref.file_id)
        if seen_step is not None:
            notes_line = _notes_line(ref)
            if notes_line:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        header
                        + f") - already viewed at step {seen_step} this turn; the "
                        "pixels are not re-sent." + notes_line
                    ),
                )
            return ToolResult(
                tool_call_id="",
                content=(
                    header
                    + f") - Error: already viewed at step {seen_step} this turn and "
                    "no notes were saved, so the pixels are not re-sent. Describe "
                    "it from what you saw. Next time, right after viewing an image, "
                    "save 1-3 lines of what it shows with "
                    "annotate_file(file_id=..., notes=...) - they are returned "
                    "instead of the pixels from then on."
                ),
                is_error=True,
            )
        notes_line = _notes_line(ref)
        # ONE sentence claims the attachment and asks for the notes: the
        # loop's claim strip (``_strip_attachment_claims``) removes a whole
        # sentence, so when the pixels are dropped "look at it now" goes with
        # "is attached" instead of surviving next to a note that says the
        # pixels are not re-sent - the two-statements shape.
        if notes_line:
            body = ") - the image is attached to this result." + notes_line
        else:
            body = (
                ") - the image is attached to this result; look at it now and save 1-3 lines "
                "of what it shows with annotate_file(file_id=..., notes=...), because the "
                "pixels are pruned from your context after a few steps and are not re-sent "
                "this turn, so your notes are what you will work from."
            )
        return ToolResult(tool_call_id="", content=header + body, files=[ref])

    async def _read_file_content(
        file_id: str,
        offset: int = 0,
        max_chars: int | None = None,
        mode: str | None = None,
        file_context: FileContext | None = None,
    ) -> ToolResult:
        from app.services.attachment_extract import attachment_kind, extract_attachment_text
        from app.services.code_interpreter import code_interpreter_offered, mount_path_hint
        from app.tools.base import offered_tools

        ref = await _resolve(file_context, file_id)
        if ref is None:
            return _unresolved(file_context, file_id)
        if ref.is_deleted:
            return _deleted(ref)
        if file_context is None:  # pragma: no cover - _resolve_file_ref guards it
            return ToolResult(
                tool_call_id="", content="Error: no files in context.", is_error=True
            )
        from app.tools.base import workspace_ingest_requested

        if workspace_ingest_requested():
            # Bound for the sandbox workspace: the file itself, whole.
            return ToolResult(tool_call_id="", content=f"File: {ref.filename}", files=[ref])
        # Every way forward below names the code interpreter only when the
        # running loop offers it - the surface the inline fallback
        # renders the same file with, so an offset means one text on both.
        code_interpreter = code_interpreter_offered(offered_tools())
        header = f"File: {ref.filename} ({ref.content_type}, {_format_size(ref.size)}"
        editable = is_text_like_file(ref.filename, ref.content_type)
        # A raster image is shown, not read: the ref rides on the tool result
        # and the provider turns it into a native vision block. An SVG is an
        # image to a vision model AND source text to an editor - it takes the
        # text path, or edit_file_content would accept a file whose markup
        # the model can never read. Never counted against the window cap:
        # this is the recovery path every dropped-image note names.
        if ref.is_image and not editable:
            return _show_image(ref, header, file_context)
        try:
            start = max(0, int(offset or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            window = int(max_chars) if max_chars else READ_WINDOW_DEFAULT_CHARS
        except (TypeError, ValueError):
            window = READ_WINDOW_DEFAULT_CHARS
        window = max(1, min(window, READ_WINDOW_MAX_CHARS))

        # ``mode="extract_structured"`` reads an HTML / JSON / JSON Lines /
        # CSV file the way a reader needs it: the SAME rendering a
        # capped inline attachment was cut from (``extract_attachment_text``,
        # same decoding, same text), so an offset the inline footer named is
        # the offset served here. The default ``source`` mode is the verbatim text
        # (what ``edit_file_content`` needs to copy a snippet from). A file
        # with no rendering reads the same in both modes and is counted as
        # its source (the EFFECTIVE view keys the window cap).
        requested_mode = str(mode or "source").strip().lower() or "source"
        if requested_mode not in READ_MODES:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: unknown mode {requested_mode!r}. Use "
                    f"{' or '.join(repr(m) for m in READ_MODES)}."
                ),
                is_error=True,
            )
        kind = attachment_kind(ref.filename, ref.content_type)
        structured = requested_mode == "extract_structured" and kind is not None
        view = "extract_structured" if structured else "source"
        renderable = kind is not None

        # The per-file window cap, taken at ADMISSION - before the
        # bytes load, in one synchronous step, so the calls of a parallel
        # batch cannot all see it unmet. Past it the answer is an explicit
        # error naming what was read and the ways forward, never another
        # window (which the retention pass would clear next and the model
        # would ask for again). A read that then serves nothing gives its
        # slot back.
        cap = _read_windows_per_file()
        served = file_context.admit_read_window(ref.file_id, view, cap)
        if served is None:
            compute = (
                "; use code_interpreter to process the whole file (every row, counts, sums), "
                f"where it is mounted at {mount_path_hint(ref.filename, ref.file_id)}."
                if code_interpreter
                else "."
            )
            other = (
                ' For the document as rendered text with its data summarised, read it with mode="extract_structured".'
                if renderable and not structured
                else ""
            )
            spent = file_context.read_window_count(ref.file_id, view)
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: window budget for this file exhausted this turn, {spent} of {cap} served "
                    f"('{ref.filename}', {_format_size(ref.size)}). The file is still attached "
                    f"(file_id={ref.file_id}); only this turn's text windows of it are spent. Answer "
                    f"from what you have and say what you could not read{compute}" + other
                ),
                is_error=True,
            )

        def _no_window(result: ToolResult) -> ToolResult:
            """A read that served no window hands its admission slot back."""
            file_context.release_read_window(ref.file_id, view)
            return result

        # ``missing`` tells a storage miss (nothing came back) apart from a
        # file that IS there but holds no text: an empty ``.md`` the agent
        # just created is an editable file whose complete content is "", not
        # a binary with nothing to extract, and a removed file is neither.
        missing = False
        try:
            if structured:
                # From the RAW bytes, whatever their encoding or name: a
                # Windows-1252 export or an ``application/vnd.api+json``
                # file under ``.bin`` is rendered in the mode asked for,
                # off the event loop, from the memoised rendering when the
                # inline path already produced it.
                raw = await file_context.read_bytes(ref.file_id)
                if raw is None:
                    missing = True
                    text = None
                else:
                    from app.core.parse_offload import run_parse

                    text = await run_parse(
                        extract_attachment_text, raw, ref.filename, ref.content_type, code_interpreter
                    )
            elif editable:
                raw = await file_context.read_bytes(ref.file_id)
                if raw is None:
                    missing = True
                    text = None
                else:
                    text = decode_file_text(raw)
                    if text is None and ref.is_image:
                        # A raster behind an ``.svg`` name: show it instead.
                        return _no_window(_show_image(ref, header, file_context))
                    if text is None:
                        # A text-like name over binary bytes: fall back to
                        # extraction.
                        editable = False
                        text = await file_context.read_text(ref.file_id)
            else:
                text = await file_context.read_text(ref.file_id)
        except Exception as exc:
            logger.warning(
                "read_file_content failed for %s (%s): %s",
                ref.file_id,
                ref.filename,
                format_exception_message(exc),
                exc_info=True,
            )
            missing = True
            text = None
        except BaseException:
            # A cancelled read (a tool timeout, a stop) served nothing: its
            # admission slot goes back, or the file would read as spent for
            # the rest of the turn under a message saying it was read.
            file_context.release_read_window(ref.file_id, view)
            raise
        if missing:
            return _no_window(
                ToolResult(
                    tool_call_id="",
                    content=(
                        f"Error: the content of '{ref.filename}' could not be loaded "
                        "from storage. The file may have been removed."
                    ),
                    is_error=True,
                )
            )
        if text is None or (not editable and not structured and not text.strip()):
            # Nothing to read (a video, a sound): its notes, when it has any, say what it holds.
            notes_line = _notes_line(ref)
            return _no_window(
                ToolResult(
                    tool_call_id="",
                    content=(
                        header + ") - this file has no readable text content "
                        "(binary format with nothing to extract)." + notes_line
                    ),
                    is_error=not notes_line,
                )
            )

        total = len(text)
        if structured:
            kind_note = (
                ", rendered for reading - styles, code and SVG geometry left out, embedded data "
                'summarised; the verbatim source is mode="source", one JSON value is query_file_json)'
            )
        elif editable and renderable:
            kind_note = (
                ', editable source text; mode="extract_structured" gives the rendered document with its '
                "data summarised - one window usually suffices)"
            )
        elif editable:
            kind_note = ", editable source text)"
        else:
            kind_note = (
                ", rendered document - text EXTRACTED for reading; not "
                "editable with edit_file_content, regenerate with generate_file)"
            )
        lines = [header + kind_note]
        if total and start >= total:
            lines.append(
                f"Content: offset {start} is past the end of this file "
                f"({total} characters). Use an offset below {total}."
            )
            return _no_window(ToolResult(tool_call_id="", content="\n".join(lines)))
        # An empty editable file is served as its complete (empty) content,
        # whatever offset was asked for.
        start = min(start, total)
        end = min(start + window, total)
        if start == 0 and end == total:
            lines.append(f"Content (complete, {total} characters):")
        else:
            lines.append(
                f"Content (characters {start}-{end} of {total}, verbatim slice):"
            )
        lines.append(text[start:end])
        if end < total:
            lines.append("")
            left = (cap - served - 1) if cap else None
            again = f'read_file_content(file_id="{ref.file_id}"' + (
                ', mode="extract_structured"' if structured else ""
            ) + f", offset={end})"
            if left is not None and left <= 0:
                compute = (
                    "; to compute over the whole file use code_interpreter "
                    f"(mounted at {mount_path_hint(ref.filename, ref.file_id)})"
                    if code_interpreter
                    else ""
                )
                lines.append(
                    f"[TRUNCATED - {total - end:,} characters remain and this was the last window "
                    "this turn allows for this file. Answer from what you have and say what you "
                    f"could not read{compute}.]"
                )
            else:
                more = f" ({left} more window(s) this turn)" if left is not None else ""
                lines.append(
                    f"[TRUNCATED - {total - end:,} characters remain. A specific later part: "
                    f"{again}{more}. Do not page through the whole file to answer a question "
                    "about it.]"
                )
        return ToolResult(tool_call_id="", content="\n".join(lines))

    async def _query_file_json(
        file_id: str,
        path: str | None = None,
        block: int | None = None,
        file_context: FileContext | None = None,
    ) -> ToolResult:
        """Read one part of the JSON an attachment carries.

        The JSON ``<script>`` elements and JavaScript data literals of an
        HTML file (numbered as its ``## Embedded data`` section numbers
        them), a JSON file itself, or the records of a JSON Lines file,
        evaluated with a JSONPath-style *path* - so a 500 KB payload is read
        by the value the question needs instead of by paging through its
        source. The whole computation - parsing, the query, the formatting,
        against one output budget - runs off the event loop. What it could
        not read names the code interpreter only when the running loop
        offers it.
        """
        from app.core.parse_offload import run_parse
        from app.services.attachment_extract import run_json_query
        from app.services.code_interpreter import code_interpreter_offered
        from app.tools.base import offered_tools

        ref = await _resolve(file_context, file_id)
        if ref is None:
            return _unresolved(file_context, file_id)
        if ref.is_deleted:
            return _deleted(ref)
        if file_context is None:  # pragma: no cover - _resolve_file_ref guards it
            return ToolResult(
                tool_call_id="", content="Error: no files in context.", is_error=True
            )
        if ref.is_image and not is_text_like_file(ref.filename, ref.content_type):
            return ToolResult(
                tool_call_id="",
                content=f"Error: '{ref.filename}' is an image; it carries no JSON to query.",
                is_error=True,
            )
        try:
            raw = await file_context.read_bytes(ref.file_id)
        except Exception as exc:
            logger.warning(
                "query_file_json failed to load %s (%s): %s",
                ref.file_id,
                ref.filename,
                format_exception_message(exc),
                exc_info=True,
            )
            raw = None
        if raw is None:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the content of '{ref.filename}' could not be loaded "
                    "from storage. The file may have been removed."
                ),
                is_error=True,
            )
        text, is_error = await run_parse(
            run_json_query, raw, ref.filename, ref.content_type, path, block, code_interpreter_offered(offered_tools())
        )
        if is_error:
            return ToolResult(tool_call_id="", content=text, is_error=True)
        header = f"File: {ref.filename} ({ref.content_type}, {_format_size(ref.size)})"
        return ToolResult(tool_call_id="", content=f"{header}\n\n{text}")

    async def _edit_file_content(
        file_id: str,
        edits: list | None = None,
        filename: str | None = None,
        file_context: FileContext | None = None,
    ) -> ToolResult:
        from app.llm.base import FileRef

        ref = await _resolve(file_context, file_id)
        if ref is None:
            return _unresolved(file_context, file_id)
        if ref.is_deleted:
            return _deleted(ref)
        if file_context is None:  # pragma: no cover - _resolve_file_ref guards it
            return ToolResult(
                tool_call_id="", content="Error: no files in context.", is_error=True
            )
        if not is_text_like_file(ref.filename, ref.content_type):
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: '{ref.filename}' ({ref.content_type}) is not an "
                    "editable text file - it is a rendered document or binary and "
                    "its source text is not stored. To change it, call "
                    "generate_file with the complete new content (for a PDF/DOCX, "
                    "the full Markdown/HTML), or edit its .md/.html source and "
                    "render that with generate_file(source_file_id=...)."
                ),
                is_error=True,
            )
        try:
            raw = await file_context.read_bytes(ref.file_id)
        except Exception as exc:
            logger.warning(
                "edit_file_content failed to load %s (%s): %s",
                ref.file_id,
                ref.filename,
                format_exception_message(exc),
                exc_info=True,
            )
            raw = None
        if raw is None:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the content of '{ref.filename}' could not be loaded "
                    "from storage (the file may have been removed). Nothing was "
                    "changed."
                ),
                is_error=True,
            )
        text = decode_file_text(raw)
        if text is None:
            # Strict UTF-8 or nothing: re-encoding a lossy decode would write
            # U+FFFD over every non-ASCII byte the edit did not touch. Name the
            # cause so the model does not retry the same call blindly.
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: '{ref.filename}' is not UTF-8 text (it may use a "
                    "legacy encoding such as Windows-1252, or be UTF-16 or "
                    "binary), so it cannot be edited in place without "
                    "corrupting it. Nothing was changed. Read it with "
                    "read_file_content and, if it must change, write a new "
                    "file with generate_file."
                ),
                is_error=True,
            )
        try:
            new_text, account = apply_text_edits(text, edits)
        except FileEditError as exc:
            return ToolResult(tool_call_id="", content=f"Error: {exc}", is_error=True)
        if new_text == text:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"No change: the edits leave '{ref.filename}' identical "
                    "(find and replace are the same text). No new revision was saved."
                ),
            )
        # The edit changes the snippets it named and nothing else: a UTF-8 BOM
        # the original opened with (``decode_file_text`` dropped it so the
        # model never has to reproduce an invisible character in a ``find``)
        # is written back, or the revision would silently lose the marker
        # Excel and friends key their CSV encoding detection on. The invariant
        # is "opens with exactly the BOM the original opened with": a
        # ``replace`` the model copied out of another BOM-prefixed source can
        # put U+FEFF back at offset 0, and a second marker would render as a
        # literal glyph in the first cell.
        raw_bytes = new_text.encode("utf-8")
        if raw.startswith(UTF8_BOM) and not raw_bytes.startswith(UTF8_BOM):
            raw_bytes = UTF8_BOM + raw_bytes
        if len(raw_bytes) > MAX_UPLOAD_SIZE_BYTES:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the edited file would be {len(raw_bytes):,} bytes; "
                    f"maximum is {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB. "
                    "Nothing was changed."
                ),
                is_error=True,
            )
        new_name = (filename or "").strip() or ref.filename
        # A revision is agent-produced by construction, whatever produced the
        # original (an upload, an MCP download, a runner capture): its
        # provenance is the ``generate_file`` pair, and the only distinction
        # that survives is the display one the chat layer keys on - a scratch
        # ``agent_working`` file stays scratch, everything else is offered as
        # a download card (``api/v1/chat.py`` maps every other source to
        # ``download_file`` anyway).
        source = "agent_working" if ref.is_working_file else "agent_download"
        file_att = await _persist_generated_file(
            db=db,
            user_id=user_id,
            filename=new_name,
            raw_bytes=raw_bytes,
            content_type=ref.content_type,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            source=source,
            match_filename=True,
        )
        new_ref = FileRef.from_attachment(file_att)
        register_produced_file(file_context, new_ref)
        summary = "\n".join(f"- {line}" for line in account)
        return ToolResult(
            tool_call_id="",
            content=(
                f"Edited {ref.filename} ({len(account)} edit"
                f"{'s' if len(account) != 1 else ''} applied, "
                f"{_format_size(len(raw_bytes))}):\n{summary}\n"
                f"New revision: [[FILE:{new_ref.file_id}]] - include this marker "
                f"verbatim in your reply. It supersedes the previous revision "
                f"(file_id={ref.file_id}), which is kept unchanged."
            ),
            files=[new_ref],
        )

    async def _annotate_file(
        file_id: str,
        notes: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult:
        """Save the model's 1-3 line account of a file.

        Stored on ``FileAttachment.description`` - the same column a template
        asset's caption lives in, with the same meaning: what the file shows,
        for a reader who cannot see it - and mirrored on the ``FileRef`` in
        this turn's context, so the ``Available Files`` block, a dropped-files
        note and a ``read_file_content`` answered without pixels all quote it.
        """
        ref = await _resolve(file_context, file_id)
        if ref is None:
            return _unresolved(file_context, file_id)
        if ref.is_deleted:
            return _deleted(ref)
        # One line of plain text: whitespace (line breaks included) collapses
        # to single spaces, and the C0 / C1 control characters ``split`` does
        # not treat as whitespace (NUL, ESC, ...) are dropped - a NUL fails
        # the PostgreSQL write below and an escape sequence has no place in
        # a caption a person reads in the Templates UI.
        text = " ".join(_CONTROL_CHARS_RE.sub("", str(notes or "")).split())
        if not text:
            return ToolResult(
                tool_call_id="",
                content="Error: notes must say what the file shows (1-3 lines).",
                is_error=True,
            )
        if len(text) > FILE_NOTES_MAX_CHARS:
            text = text[: FILE_NOTES_MAX_CHARS - 3].rstrip() + "..."
        ref.notes = text
        stored = False
        row_id = safe_uuid(ref.file_id)
        if row_id is not None and ref.s3_object_name:
            from sqlalchemy import update

            from app.models.file_attachment import FileAttachment

            try:
                async with _own_session() as own_db:
                    # Never a template asset's caption: that column is a
                    # shared, human-authored description on a container's
                    # row, and a conversation's context must not be able to
                    # rewrite it whatever handle it holds. The fence matching
                    # no row is a real outcome (a template asset in this
                    # context, a row already gone): the answer reads the row
                    # count, never the fact that the statement ran.
                    result = await own_db.execute(
                        update(FileAttachment)
                        .where(
                            FileAttachment.id == row_id,
                            FileAttachment.template_container_id.is_(None),
                        )
                        .values(description=text)
                    )
                    await own_db.commit()
                stored = (getattr(result, "rowcount", 0) or 0) > 0
            except Exception as exc:
                logger.warning(
                    "annotate_file: could not persist notes for %s: %s",
                    ref.file_id,
                    format_exception_message(exc),
                )
        where = (
            "stored with the file"
            if stored
            else "kept for this turn only (the stored file's own description was not changed)"
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Saved notes for {ref.filename} (file_id {ref.file_id}), {where}: "
                f"{text}\nThey are shown next to the file in Available Files and "
                "returned by read_file_content instead of the pixels."
            ),
        )

    executor = CallableToolExecutor()
    executor.register("generate_file", _generate_file)
    executor.register("read_file_content", _read_file_content)
    executor.register("query_file_json", _query_file_json)
    executor.register("edit_file_content", _edit_file_content)
    executor.register("annotate_file", _annotate_file)
    # ``render_html_to_image`` rides the file_generation suite: a PNG
    # figure rendered from HTML is a generated file, like a PDF from Markdown.
    from .html_render import build_render_html_tool

    render_defs, _same_executor = build_render_html_tool(
        db=db,
        user_id=user_id,
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
        executor=executor,
    )

    def _read_description(code_interpreter: bool) -> str:
        return (
            "Read a file attached to this conversation by file_id (or exact "
            "filename): a file YOU generated earlier (generate_file, code "
            "interpreter output, a download) or one the user uploaded. Generated "
            "files are NOT in your context - always read them with this tool "
            "before describing or changing them; never reconstruct their content "
            "from memory. Returns the source text verbatim (windowed: offset / "
            "max_chars), the extracted text of a PDF/DOCX/XLSX, or shows an image "
            "- ONCE per turn: look at it, save what it shows with annotate_file, "
            "and work from those notes (a second read returns the notes, not the "
            "pixels; the notes also survive the pruning of the image from your "
            "context). For an HTML, JSON or CSV file pass mode=\"extract_structured\" "
            "to get the RENDERED text (styles, code and SVG geometry left out, "
            "embedded data summarised) instead of paging through the source; one "
            "JSON value is read with query_file_json. A few windows per file are "
            "served each turn"
            + (": to compute over a whole file use code_interpreter." if code_interpreter else ".")
        )

    def _read_description_on(surface: tuple[ToolDefinition, ...]) -> str:
        from app.services.code_interpreter import code_interpreter_offered

        return _read_description(code_interpreter_offered(surface))

    read_definition = ToolDefinition(
        name="read_file_content",
        # The loop sends the one its surface calls for; a catalog
        # lists the tool as it reads with the code interpreter alongside.
        description=_read_description(True),
        describe_for_surface=_read_description_on,
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": (
                        "The file id from the Files section or a [[FILE:id]] "
                        "marker (exact filename also accepted when unique)."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Character offset to start from (default 0). Use the "
                        "value given by a truncated read to continue."
                    ),
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": READ_WINDOW_MAX_CHARS,
                    "description": (
                        f"Window size in characters (default "
                        f"{READ_WINDOW_DEFAULT_CHARS}, max {READ_WINDOW_MAX_CHARS})."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": list(READ_MODES),
                    "description": (
                        "'source' (default): the verbatim text, what "
                        "edit_file_content needs. 'extract_structured': for an "
                        "HTML, JSON, JSON Lines or CSV file, the rendered document "
                        "with its data summarised (for HTML the data blocks its "
                        "scripts embed) - read a plan, a report or an export in "
                        "one call. Same as 'source' for any other file."
                    ),
                },
            },
            "required": ["file_id"],
        },
        source="builtin:files",
        workspace_ingest=True,
    )

    query_json_definition = ToolDefinition(
        name="query_file_json",
        description=(
            "Read one part of the JSON an attached file carries: the data "
            "blocks of an HTML file (its JSON <script> elements and JavaScript "
            "data literals - a self-contained plan, dashboard or export), a JSON "
            "file itself, or the records of a JSON Lines file. path is a "
            "JSONPath-style selector - $.plan.regions[*].name, $..title, "
            "$.items[0:5], ['key with spaces'] - and the matching values come "
            "back as JSON. Without a path the structure of each block is "
            "summarised. Use it instead of paging through the source."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": (
                        "The file id from the Files section or a [[FILE:id]] "
                        "marker (exact filename also accepted when unique)."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "JSONPath-style selector: $.key, ['key'], [0], [-1], "
                        "[1:3], [*], .*, ..key, ..* (no filters or unions). "
                        "Omit it to get the structure of the block(s)."
                    ),
                },
                "block": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Which data block of an HTML file to query (1-based, as "
                        "numbered under 'Embedded data'). Default: every block."
                    ),
                },
            },
            "required": ["file_id"],
        },
        source="builtin:files",
    )

    edit_definition = ToolDefinition(
        name="edit_file_content",
        description=(
            "Change an existing text file (HTML, Markdown, CSV, JSON, YAML, code, "
            "...) attached to this conversation with exact find/replace edits "
            "applied server-side - the way to remove a sentence, fix a value or "
            "rename something in a file you already generated. Do NOT regenerate "
            "the whole document for a small change. Read the file first "
            "(read_file_content) and copy each 'find' snippet exactly; every "
            "snippet must exist and be unique (or set replace_all). Nothing is "
            "written unless every edit applies. Saves a NEW revision and returns "
            "its [[FILE:id]] marker - include it verbatim in your reply. Rendered "
            "PDF/DOCX files cannot be edited this way (use generate_file)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The id (or exact filename) of the file to edit.",
                },
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": EDIT_MAX_OPERATIONS,
                    "description": (
                        "Ordered find/replace operations, applied sequentially."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "find": {
                                "type": "string",
                                "description": (
                                    "Exact text to replace (verbatim, including "
                                    "whitespace and markup)."
                                ),
                            },
                            "replace": {
                                "type": "string",
                                "description": (
                                    "Replacement text; empty string deletes the "
                                    "found text."
                                ),
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": (
                                    "Replace every occurrence (default false: the "
                                    "snippet must be unique)."
                                ),
                                "default": False,
                            },
                        },
                        "required": ["find", "replace"],
                    },
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Optional new filename for the revision (defaults to the "
                        "original name)."
                    ),
                },
            },
            "required": ["file_id", "edits"],
        },
        source="builtin:files",
    )

    annotate_definition = ToolDefinition(
        name="annotate_file",
        description=(
            "Save 1-3 lines describing what a file attached to this conversation "
            "shows - call it right after read_file_content shows you an image "
            "(colours, layout, text, what is wrong). The notes are stored with the "
            "file, listed next to it in Available Files, quoted when the image is "
            "pruned from your context, and returned by read_file_content instead "
            "of the pixels for the rest of the turn - so you never need to look "
            "at the same image twice."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The file id (or exact filename) the notes are about.",
                },
                "notes": {
                    "type": "string",
                    "maxLength": FILE_NOTES_MAX_CHARS,
                    "description": (
                        "What the file shows, in 1-3 lines - concrete: the elements, "
                        "their colours (hex when you know it), their placement, and "
                        "anything wrong with it."
                    ),
                },
            },
            "required": ["file_id", "notes"],
        },
        source="builtin:files",
    )

    definition = ToolDefinition(
        name="generate_file",
        description=(
            "Generate a NEW downloadable file artifact from provided content and "
            "return a [[FILE:id]] marker. Use it for large outputs (CSV, JSON, "
            "YAML, reports, IOC lists, detection rules, STIX bundles) instead of "
            "dumping raw content inline. To change a text file that already "
            "exists, use edit_file_content; to render an existing .md/.html "
            "file to PDF or DOCX, pass source_file_id.\n"
            "- PDF (.pdf) / DOCX (.docx) from text: supply the content as "
            "Markdown, HTML, or plain text and leave encoding unset - the "
            "backend renders the binary document. Do NOT author the binary "
            "yourself or base64-encode the text. A document already in a file "
            "(written in the sandbox, patched with edit_file_content, "
            "downloaded) is rendered with source_file_id instead of content, "
            "without re-sending its text.\n"
            "- Embed an IMAGE you already have as a file (e.g. a diagram from "
            "download_figma_image / download_notion_file / the code interpreter) "
            "into a PDF/DOCX: reference it in Markdown as ![alt](file:id) or "
            "[[FILE:id]] (or <img src=\"file:id\">) - the backend inlines the "
            "real image bytes when rendering. External http(s) image URLs are "
            "blocked and will NOT render; use the file handle instead.\n"
            "- Binary you already have (zip, image, or pre-rendered PDF/DOCX "
            "bytes): supply base64 and set encoding='base64'.\n"
            "- file_tag: 'download_file' for files the user should download "
            "(download card); 'working_file' (default) for scratch artifacts "
            "(compact chip).\n"
            "Include the returned [[FILE:...]] token verbatim in your response "
            "so the UI renders the file at the right position."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": (
                        "Output filename including extension "
                        "(for example report.pdf, export.csv, bundle.json, rules.yar)."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "File content as text. For PDF or DOCX output, supply Markdown, "
                        "HTML, or plain text (the backend renders the binary document). "
                        "For binary files (zip, image, pre-rendered PDF/DOCX, etc.), "
                        "supply base64 and set encoding='base64'. Omit it when "
                        "source_file_id is given."
                    ),
                },
                "source_file_id": {
                    "type": "string",
                    "description": (
                        "Render an existing text file of this conversation (its file_id, "
                        "[[FILE:id]] marker, or exact filename) instead of passing content. "
                        "The file must be Markdown, HTML or plain text. Use it to render a "
                        "document authored in the sandbox or patched with edit_file_content "
                        "to PDF or DOCX without re-sending its text."
                    ),
                },
                "content_type": {
                    "type": "string",
                    "description": (
                        "Optional MIME type. Auto-detected from the filename extension when omitted. "
                        "Use 'application/pdf' to force PDF rendering, or "
                        "'application/vnd.openxmlformats-officedocument.wordprocessingml.document' "
                        "to force DOCX rendering of HTML/Markdown content."
                    ),
                },
                "encoding": {
                    "type": "string",
                    "enum": ["base64"],
                    "description": (
                        "Optional: set to 'base64' ONLY when content is base64-encoded binary data "
                        "(zip, image, pre-rendered PDF/DOCX). Omit for text / Markdown / HTML "
                        "(including PDF and DOCX rendering — the backend handles the conversion)."
                    ),
                },
                "file_tag": {
                    "type": "string",
                    "enum": ["working_file", "download_file"],
                    "description": (
                        "Display tag: 'download_file' renders as a prominent download card "
                        "(use for files the user is meant to download); "
                        "'working_file' (default) renders as a compact working chip "
                        "(use for internal/scratch artifacts)."
                    ),
                    "default": "working_file",
                },
            },
            "required": ["filename"],
        },
        source="builtin:files",
    )

    return [
        definition,
        read_definition,
        query_json_definition,
        edit_definition,
        annotate_definition,
        *render_defs,
    ], executor
