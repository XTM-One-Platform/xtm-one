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

"""``to_workspace_path``: a tool's content written to the workspace, not returned.

The dispatcher (``CompositeToolExecutor.execute_tool``) runs a
``workspace_ingest`` tool without the argument, collecting every file the call
brings into the turn, then hands the result here. What is written:

- the files the call brought in (a download, a fetched response, the raw
  bytes a renderer registered when :func:`~app.tools.base.workspace_ingest_requested`),
  one at the path, several under it as a directory;
- otherwise the result's text, as UTF-8.

A PDF or Office document among the files also lands as its text layer,
extracted on the server by the platform's own reader
(:func:`~app.services.chunking.extract_text_from_file`, the one the inline
result and the knowledge base use): the raw file at its path and the text at
``<path>.txt``, or the text alone when the target itself ends in ``.txt``. The
sandbox has no PDF library, so without it a fetched report could not be
searched in code.

The model gets the path, size and sha256 back instead of the content, so a
repository file crosses the context zero times instead of twice. A call that
failed is returned as it was, with nothing written; a workspace that refuses
the write (quota) says so and returns nothing either.
"""

from __future__ import annotations

import contextlib
import logging
import posixpath

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import metrics
from app.services.sandbox_workspace.manifest import QuotaExceededError, format_size
from app.services.sandbox_workspace.paths import WorkspacePathError, display, normalize
from app.services.sandbox_workspace.session import WorkspaceSession, WorkspaceUnavailableError
from app.tools.base import FileContext, ToolCall, ToolResult

logger = logging.getLogger(__name__)

#: Files named one by one in the answer; a larger tree (an unzipped archive) is counted past it.
_MAX_LISTED_FILES = 60
#: The suffix of the text layer written next to a document.
TEXT_LAYER_SUFFIX = ".txt"


def _document_name(ref: object, target: str) -> str | None:
    """The name the extractor reads *ref* by when it is a PDF or Office document, else ``None``.

    Its own name, else the target path, else the extension of its declared
    type (a response named after a URL without one).
    """
    from app.integrations.file_content import DOCUMENT_EXTENSIONS, DOCUMENT_MIME_EXTENSIONS

    name = posixpath.basename(str(getattr(ref, "filename", "") or "").replace("\\", "/"))
    for candidate in (name, posixpath.basename(target)):
        if candidate.lower().endswith(DOCUMENT_EXTENSIONS):
            return candidate
    mime = str(getattr(ref, "content_type", "") or "").split(";", 1)[0].strip().lower()
    extension = DOCUMENT_MIME_EXTENSIONS.get(mime)
    return f"{name or 'document'}{extension}" if extension else None


async def _text_layer(data: bytes, name: str) -> str:
    """The text of the document *data*, ``""`` when it holds none or cannot be read."""
    from app.integrations.file_content import MAX_DOCUMENT_BYTES
    from app.services.chunking import extract_text_from_file_async

    if len(data) > MAX_DOCUMENT_BYTES:
        return ""
    try:
        text = await extract_text_from_file_async(data, name)
    except Exception as exc:  # an unreadable document is said, the raw file still written
        logger.info("No text layer for %s: %s", name[:200], format_exception_message(exc))
        return ""
    return text if text.strip() else ""


async def _with_text_layers(payloads: list[tuple[str, bytes, object]]) -> tuple[list[tuple[str, bytes]], list[str]]:
    """*payloads* (path, bytes, ref) with the text layer of each document, and the notes to say.

    A document whose target ends in ``.txt`` is replaced by its text; any
    other gets ``<path>.txt`` beside it unless the call already writes that
    path. A document with no text keeps its raw bytes and is named in a note.
    """
    taken = {path.casefold() for path, _data, _ref in payloads}
    out: list[tuple[str, bytes]] = []
    notes: list[str] = []
    for path, data, ref in payloads:
        name = _document_name(ref, path)
        if name is None:
            out.append((path, data))
            continue
        text = await _text_layer(data, name)
        if not text:
            out.append((path, data))
            notes.append(
                f"No text layer could be extracted from {display(path)} (scanned, encrypted or empty): "
                "only the raw file is written."
            )
            continue
        encoded = text.encode("utf-8")
        if path.lower().endswith(TEXT_LAYER_SUFFIX):
            out.append((path, encoded))
            notes.append(f"{display(path)} holds the text layer of the document {name[:200]}, not its raw bytes.")
            continue
        out.append((path, data))
        text_path = f"{path}{TEXT_LAYER_SUFFIX}"
        if text_path.casefold() not in taken:
            taken.add(text_path.casefold())
            out.append((text_path, encoded))
    return out, notes


async def read_ref_bytes(ref: object, file_context: FileContext | None) -> bytes:
    """The bytes of a file a tool brought in: in memory, through the turn's context, else storage."""
    if getattr(ref, "has_inline_data", False):
        data = ref.inline_data
        if data is not None:
            return data
    if file_context is not None:
        data = await file_context.read_bytes(str(ref.file_id))
        if data is not None:
            return data
    from app.services.file_storage import file_storage

    return await file_storage.download(ref.s3_object_name)


def _file_name(ref: object, taken: set[str]) -> str:
    raw = posixpath.basename(str(getattr(ref, "filename", "") or "").replace("\\", "/")) or "file"
    name = "".join(ch if ch.isprintable() and ch not in '/\\:*?"<>|' else "_" for ch in raw).strip(" .") or "file"
    stem, dot, ext = name.rpartition(".")
    candidate, n = name, 1
    while candidate.casefold() in taken:
        n += 1
        candidate = f"{stem}-{n}.{ext}" if dot and stem else f"{name}-{n}"
    taken.add(candidate.casefold())
    return candidate


def _tree_path(ref: object, taken: set[str]) -> str | None:
    """The path below the target directory a file of a tree asks for (``FileRef.relative_path``).

    ``None`` when it asks for none, or for one the workspace refuses or another
    file of the call already took: the file is then named by :func:`_file_name`.
    """
    raw = getattr(ref, "relative_path", None)
    if not raw:
        return None
    try:
        rel = normalize(raw)
    except WorkspacePathError:
        return None
    if rel.casefold() in taken:
        return None
    taken.add(rel.casefold())
    return rel


def _open_line(rel: str, paths: list[str]) -> str:
    """Where code_interpreter opens what was written: one file, a directory, or the files named."""
    if len(paths) == 1:
        return f"Open it in code_interpreter as {display(paths[0])}."
    if all(p.startswith(f"{rel}/") for p in paths):
        return f"Open them in code_interpreter under {display(rel)}/."
    return "Open them in code_interpreter as " + " and ".join(display(p) for p in paths) + "."


def _file_lines(files: list) -> list[str]:
    lines: list[str] = []
    for ref in files:
        file_id = str(getattr(ref, "file_id", "") or "")
        if getattr(ref, "s3_object_name", None):
            lines.append(f"Also kept as conversation file {file_id} ([[FILE:{file_id}]] hands it to the user).")
    return lines


class WorkspaceIngestor:
    """The run's :class:`~app.tools.base.WorkspaceSink`, over its :class:`WorkspaceSession`."""

    def __init__(self, session: WorkspaceSession) -> None:
        self.session = session

    def writing(self) -> contextlib.AbstractContextManager[None]:
        """A ``to_workspace_path`` call counted as a workspace write while it runs (#11714).

        The dispatcher enters it around the tool and its ingest: a reader of the
        same tool round (``sandbox_read_file``, ``code_interpreter``) waits for
        the file the call brings in instead of answering it is not there.
        """
        return self.session.writing()

    async def _settled(self) -> None:
        """Wait, bounded, for the workspace writes of the caller's tool round (``WorkspaceSession.settled``)."""
        if self.session.available:
            await self.session.settled()

    def check_path(self, path: object) -> str | None:
        """Why *path* cannot be written, before the tool runs; ``None`` when it can."""
        if not self.session.available:
            return None
        try:
            normalize(path)
        except WorkspacePathError as exc:
            return f"Error: to_workspace_path refused: {exc}. Nothing was run."
        return None

    async def write_file(self, path: object, data: bytes, *, tool: str) -> str:
        """Write *data* at *path* for *tool*, beside its answer; the line naming the file.

        Unlike ``to_workspace_path`` the tool's answer is kept: a render's PNG
        lands in the workspace for a measurement in code while its probe
        findings still reach the model (``render_html_to_image``'s
        ``png_workspace_path``). Raises :class:`ValueError` with the sentence
        for the model.
        """
        if not self.session.available:
            raise ValueError(f"{self.session.unavailable_reason}.")
        try:
            # Counted as a workspace write while it commits: a reader of the same tool round waits for it.
            with self.session.writing():
                entry, _plan = await self.session.put_bytes(normalize(path), data, operation="write")
        except (WorkspacePathError, QuotaExceededError, WorkspaceUnavailableError) as exc:
            metrics.operation("write_for_tool", "refused")
            raise ValueError(f"{exc}.") from None
        metrics.operation("write_for_tool", "ok")
        logger.debug("%s wrote %s to the sandbox workspace", tool, entry.path)
        return f"{display(entry.path)} ({format_size(entry.size)}, sha256 {entry.sha256})"

    async def read_file(self, path: object, *, tool: str) -> tuple[str, str, bytes]:
        """The bytes of a workspace file a ``workspace_path`` argument hands to *tool*.

        Read on the server from the stored contents; nothing is persisted as
        a conversation file. A read changes no state, so it writes no audit
        row (#9743): only its ``read_for_tool`` metric counts it.
        Raises :class:`ValueError` with the sentence for the model.
        """
        if not self.session.available:
            raise ValueError(f"{self.session.unavailable_reason}.")
        try:
            rel = normalize(path)
        except WorkspacePathError as exc:
            raise ValueError(f"{exc}.") from None
        await self._settled()
        try:
            entry, data = await self.session.get_bytes(rel)
        except FileNotFoundError:
            raise ValueError(await self.session.not_found(rel)) from None
        except WorkspaceUnavailableError as exc:
            raise ValueError(f"{exc}.") from None
        metrics.operation("read_for_tool", "ok")
        logger.debug(
            "Handed %s (%d bytes) of sandbox workspace %s to %s",
            display(entry.path),
            entry.size,
            self.session.workspace_id,
            tool,
        )
        return rel, rel.rsplit("/", 1)[-1], data

    async def checkout_changes(self, directory: object, paths: list[str] | None):
        """The checkout record at *directory* and what the workspace changed since (``checkout.py``).

        Raises :class:`ValueError` with the sentence for the model when the
        directory holds no record the platform reads.
        """
        from app.services.sandbox_workspace.checkout import (
            RECORD_NAME,
            CheckoutRecord,
            CheckoutRecordError,
            changes_since,
            read_record_bytes,
        )
        from app.services.sandbox_workspace.gitignore import GitIgnore, gitignore_paths

        if not self.session.available:
            raise ValueError(f"{self.session.unavailable_reason}.")
        try:
            base = normalize(directory)
        except WorkspacePathError as exc:
            raise ValueError(f"{exc}.") from None
        # A status, diff or commit of the round that edits the checkout sees the edit (#11714).
        await self._settled()
        try:
            record_entry, data = await read_record_bytes(self.session, base)
        except FileNotFoundError:
            raise ValueError(
                f"{display(base)}/ holds no {RECORD_NAME}: it was not written by materialize_repository (or the "
                "record was removed). Check the repository out again, or name the files to commit."
            ) from None
        except WorkspaceUnavailableError as exc:
            raise ValueError(f"{exc}.") from None
        try:
            record = CheckoutRecord.from_json(data, path=base)
        except CheckoutRecordError as exc:
            raise ValueError(str(exc)) from None
        entries = await self.session.entries(refresh=True)
        # The record's write time: a file outside its scope written after it is named, never counted (#10144).
        since = float(getattr(record_entry, "mtime", 0) or 0) or None
        # The checkout's .gitignore files: an untracked file they exclude (a build's output) is counted, never listed.
        rules: list[tuple[str, bytes]] = []
        for repo_path in gitignore_paths(p for p in map(record.repo_path, map(str, entries)) if p):
            try:
                _entry, rule_bytes = await self.session.get_bytes(record.workspace_path(repo_path))
            except (FileNotFoundError, WorkspaceUnavailableError):
                continue
            rules.append((repo_path, rule_bytes))
        return record, changes_since(record, entries, paths=paths, since=since, ignore=GitIgnore(rules))

    async def prepush_warning(self, record: object, changes: object) -> str:
        """The pre-push guards a ``from_checkout`` change set touches that did not pass on its tree (``prepush.py``)."""
        from app.services.sandbox_workspace import prepush

        if not self.session.available:
            return ""
        return await prepush.commit_warning(self.session, record, changes)  # type: ignore[arg-type]

    async def advance_checkout(
        self,
        record: object,
        *,
        commit: str,
        ref: str,
        written: dict[str, bytes],
        removed: list[str],
        rewrites: dict[str, bytes] | None = None,
    ) -> str:
        """Move the checkout *record* to the commit made from it (``checkout.advance``).

        Rewrites ``<checkout>/.xtm-checkout`` and returns the sentence for
        the commit's result. *rewrites* (repository path -> bytes) are the
        workspace files the commit merged with the branch: they are written
        first, so the workspace holds what was committed. Never raises: the
        commit is made whatever happens to the record, so a failure is said,
        not thrown.
        """
        from app.services.sandbox_workspace.checkout import CheckoutRecord, advance, origin, write_record

        if not isinstance(record, CheckoutRecord):
            return ""
        moved = advance(record, commit=commit, ref=ref, written=written, removed=removed)
        try:
            for repo_path, data in (rewrites or {}).items():
                await self.session.put_bytes(record.workspace_path(repo_path), data, operation="checkout_merge")
            await write_record(self.session, moved, operation="checkout_advance")
        except (QuotaExceededError, WorkspaceUnavailableError, WorkspacePathError) as exc:
            metrics.operation("checkout_advance", "refused")
            return (
                f"The checkout record {display(moved.record_path)} was not moved to the new commit ({exc}): "
                "sandbox_checkout_status still counts these files as changed."
            )
        except Exception as exc:  # the commit is made: never fail it on the record
            logger.warning("checkout record advance failed: %s", format_exception_message(exc))
            metrics.operation("checkout_advance", "failed")
            return (
                f"The checkout record {display(moved.record_path)} was not moved to the new commit "
                f"({format_exception_message(exc)}): sandbox_checkout_status still counts these files as changed."
            )
        metrics.operation("checkout_advance", "ok")
        return (
            f"Checkout record {display(moved.record_path)} moved to {origin(moved)}: "
            "what this commit carried is no longer a change."
        )

    async def note_checkout_pending(
        self, record: object, *, branch: str, parent: str, written: dict[str, bytes], removed: list[str]
    ) -> bool:
        """Write in *record* the commit a ``from_checkout`` call is about to make (``checkout.with_pending``, #12377).

        A call cut after its commit landed then leaves the note, and the next
        ``from_checkout`` adopts the landed commit. Never raises: the commit
        goes on without the note.
        """
        from app.services.sandbox_workspace.checkout import CheckoutRecord, with_pending, write_record

        if not isinstance(record, CheckoutRecord) or not self.session.available:
            return False
        noted = with_pending(record, branch=branch, parent=parent, written=written, removed=removed)
        try:
            await write_record(self.session, noted, operation="checkout_advance")
        except Exception as exc:  # the commit is made without the note
            logger.warning("checkout pending note failed: %s", format_exception_message(exc))
            return False
        return True

    async def adopt_landed_checkout(self, record: object, *, commit: str) -> str:
        """Move *record* to *commit*, the landed commit its pending note describes (``checkout.adopt_pending``).

        Returns the sentence for the result, ``""`` when the record was not moved. Never raises.
        """
        from app.services.sandbox_workspace.checkout import CheckoutRecord, adopt_pending, origin, write_record

        if not isinstance(record, CheckoutRecord) or not self.session.available:
            return ""
        moved = adopt_pending(record, commit=commit)
        try:
            await write_record(self.session, moved, operation="checkout_advance")
        except Exception as exc:  # the record stays: the drift reconcile still runs
            logger.warning("checkout adoption of a landed commit failed: %s", format_exception_message(exc))
            metrics.operation("checkout_adopt_landed", "failed")
            return ""
        metrics.operation("checkout_adopt_landed", "ok")
        return (
            f"Checkout record {display(moved.record_path)} moved to {origin(moved)}: an earlier from_checkout "
            "commit of these files had landed before its answer was lost."
        )

    async def checkout_records_for(self, workspace_paths: list[str]) -> dict[str, object]:
        """Workspace path -> the checkout record of the nearest directory above it holding one.

        A path under no checkout is left out; each directory is read once.
        Raises what the session raises; a missing or unreadable record is no
        record.
        """
        from app.services.sandbox_workspace.checkout import CheckoutRecord, CheckoutRecordError, read_record_bytes

        cache: dict[str, CheckoutRecord | None] = {}
        found: dict[str, object] = {}
        if not self.session.available:
            return found
        for workspace_path in workspace_paths:
            parts = str(workspace_path).split("/")[:-1]
            for depth in range(len(parts), 0, -1):
                root = "/".join(parts[:depth])
                if root not in cache:
                    try:
                        _entry, data = await read_record_bytes(self.session, root)
                        cache[root] = CheckoutRecord.from_json(data, path=root)
                    except (FileNotFoundError, CheckoutRecordError):
                        cache[root] = None
                if cache[root] is not None:
                    found[workspace_path] = cache[root]
                    break
        return found

    async def advance_checkout_files(
        self,
        *,
        repository: str,
        branch: str,
        parent: str,
        commit: str,
        written: dict[str, tuple[str, bytes]],
        committed: list[str] | None = None,
    ) -> str:
        """Move the checkout record a ``files[]`` commit of its own files was made from (``checkout.advance_files``).

        Each written workspace path is matched to the nearest directory above it
        holding a record; a record that cannot move (another repository, a
        record behind the branch, a file committed at another path) is left
        where it is, and the next ``from_checkout`` reconciles. Returns the
        sentences for the commit's result, ``""`` when no checkout file was
        written. *committed* names every path the commit wrote or deleted
        (the written ones when not given). Never raises: the commit is made.
        """
        from app.services.sandbox_workspace.checkout import CheckoutRecord, advance_files, origin, write_record

        if not self.session.available or not written:
            return ""
        paths = list(committed) if committed is not None else [repo_path for repo_path, _data in written.values()]
        records: dict[str, CheckoutRecord] = {}
        grouped: dict[str, dict[str, tuple[str, bytes]]] = {}
        try:
            for workspace_path, record in (await self.checkout_records_for(list(written))).items():
                if not isinstance(record, CheckoutRecord):
                    continue
                records[record.path] = record
                grouped.setdefault(record.path, {})[workspace_path] = written[workspace_path]
            lines: list[str] = []
            for root, files in grouped.items():
                record = records[root]
                moved = advance_files(
                    record,
                    repository=repository,
                    branch=branch,
                    parent=parent,
                    commit=commit,
                    written=files,
                    committed=paths,
                )
                if moved is None:
                    metrics.operation("checkout_advance_files", "kept")
                    lines.append(
                        f"Checkout record {display(record.record_path)} kept at its commit "
                        f"{record.commit[:12] or '(none)'}: this commit was not made on it from its own files alone, "
                        "so the next from_checkout merges the branch's changes."
                    )
                    continue
                await write_record(self.session, moved, operation="checkout_advance")
                metrics.operation("checkout_advance_files", "ok")
                lines.append(
                    f"Checkout record {display(moved.record_path)} moved to {origin(moved)}: "
                    "the checkout files this commit carried are no longer a change."
                )
            return " ".join(lines)
        except Exception as exc:  # the commit is made: never fail it on the record
            logger.warning("checkout record advance after a files commit failed: %s", format_exception_message(exc))
            metrics.operation("checkout_advance_files", "failed")
            return (
                f"A checkout record was not moved to the new commit ({format_exception_message(exc)}): "
                "sandbox_checkout_status may still count the committed files as changed."
            )

    async def ingest(
        self,
        *,
        call: ToolCall,
        result: ToolResult,
        captured: list,
        path: object,
        file_context: FileContext | None,
    ) -> ToolResult:
        if result.is_error:
            metrics.operation("ingest", "tool_error")
            return result
        if not self.session.available:
            return ToolResult(
                tool_call_id=result.tool_call_id,
                content=f"{result.content}\n\n[to_workspace_path ignored: {self.session.unavailable_reason}.]",
                files=result.files,
            )
        rel = normalize(path)
        notes: list[str] = []
        files: list = []
        seen: set[str] = set()
        for ref in [*captured, *(result.files or [])]:
            key = str(getattr(ref, "file_id", "") or id(ref))
            if key not in seen:
                seen.add(key)
                files.append(ref)
        try:
            if files:
                read: list[tuple[str, bytes, object]] = []
                tree = any(getattr(ref, "relative_path", None) for ref in files)
                if len(files) == 1 and not tree:
                    read.append((rel, await read_ref_bytes(files[0], file_context), files[0]))
                else:
                    taken: set[str] = set()
                    for ref in files:
                        name = _tree_path(ref, taken) or _file_name(ref, taken)
                        read.append((f"{rel}/{name}", await read_ref_bytes(ref, file_context), ref))
                # A tree (an unzipped archive) is written as it came: a
                # document of it is the source's file, not one to read.
                if tree:
                    payloads = [(p, d) for p, d, _ref in read]
                else:
                    payloads, notes = await _with_text_layers(read)
                plan = await self.session.put_tree(payloads, operation="ingest")
                written = [(p, len(d)) for p, d in payloads]
                shas = await self.session.entries()
            else:
                data = (result.content or "").encode("utf-8")
                _entry, plan = await self.session.put_bytes(rel, data, operation="ingest")
                written = [(rel, len(data))]
                shas = await self.session.entries()
        except QuotaExceededError as exc:
            metrics.operation("ingest", "quota")
            return ToolResult(
                tool_call_id=result.tool_call_id,
                content=f"Error: {exc} The content was not returned either: free space, or call {call.name} again "
                "without to_workspace_path.",
                is_error=True,
            )
        except (WorkspaceUnavailableError, WorkspacePathError) as exc:
            metrics.operation("ingest", "refused")
            return ToolResult(
                tool_call_id=result.tool_call_id,
                content=f"{result.content}\n\n[to_workspace_path ignored: {exc}.]",
                files=result.files,
            )
        except Exception as exc:
            logger.warning("Writing %s's content to the sandbox workspace failed: %s", call.name, format_exception_message(exc))
            metrics.operation("ingest", "failed")
            return ToolResult(
                tool_call_id=result.tool_call_id,
                content=f"Error: the content could not be written to the sandbox workspace "
                f"({format_exception_message(exc)}); call {call.name} again without to_workspace_path.",
                is_error=True,
            )
        metrics.operation("ingest", "ok")
        lines = [
            f"- {display(p)} ({format_size(size)}, sha256 {shas[p].sha256 if p in shas else '?'})"
            for p, size in written[:_MAX_LISTED_FILES]
        ]
        if len(written) > _MAX_LISTED_FILES:
            lines.append(f"- ... and {len(written) - _MAX_LISTED_FILES} more file(s): sandbox_list_files {display(rel)}")
        content = "\n".join(
            [
                f"Written to the sandbox workspace instead of returned ({call.name}):",
                *lines,
                _open_line(rel, [p for p, _size in written]),
                *notes,
                f"Workspace now {format_size(plan.size_bytes)} of {format_size(self.session.limits.quota_bytes)}.",
                *_file_lines(files),
            ]
        )
        # The files stay off the result: a file on a tool result is read into
        # the model's context as its text (``prepare_tool_result_attachments``),
        # which would echo what was just written to the workspace instead of
        # returned. A stored one stays a file of the run, named above.
        if file_context is not None:
            for ref in files:
                if getattr(ref, "s3_object_name", None):
                    file_context.add(ref)
        return ToolResult(tool_call_id=result.tool_call_id, content=content)
