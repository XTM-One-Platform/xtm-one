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

"""The sandbox workspace's tools, built with ``code_interpreter``.

``sandbox_put_file`` / ``sandbox_get_file`` move a file between the
conversation and the workspace, ``sandbox_list_files`` / ``sandbox_remove_files``
complete the set, ``sandbox_read_file`` / ``sandbox_grep`` / ``sandbox_outline``
read a file without echoing it (a window, a search, its definitions),
``sandbox_edit_file`` changes one in place (exact find/replace, the diff as
read-back), ``sandbox_write_file`` creates a new text file (or replaces one
on request), ``materialize_repository`` checks a repository's tree
out into the workspace through the connected integration,
``sandbox_checkout_status`` / ``sandbox_checkout_diff`` are the ``git status``
and ``git diff`` of such a checkout, ``sandbox_checkout_rebase`` its
``git rebase`` onto a new ref (a three-way merge per file),
``sandbox_diff_files`` the ``diff -u`` of two workspace files or the
``diff3 -m`` of three, and
``sandbox_run_command`` runs a check the checkout declares
(``.xtm-one/checks.toml``) in a container of the check's image, on the
OpenSandbox backend only, and ``sandbox_start_service`` /
``sandbox_service_status`` / ``sandbox_service_logs`` /
``sandbox_stop_service`` start a service it declares there (``[service.*]``)
behind a preview URL, on the same backend. They are named
``sandbox_*`` because the platform's own Workspaces have ``get_workspace``
and ``list_workspaces``, whose token keys a ``workspace_get`` would share.

:class:`SandboxToolExecutor` is the executor the code interpreter's tools are
registered on: added to a run's ``CompositeToolExecutor``, it makes the
workspace the run's ingestion sink (``to_workspace_path``) and reads the
manifest the ``code_interpreter`` description shows before the loop starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import affected as affecting
from app.services.sandbox_workspace import (
    audit,
    codemod,
    commands,
    editing,
    eviction,
    filediff,
    images,
    installed,
    jobs,
    jsonkeys,
    labs,
    metrics,
    moving,
    patching,
    playwright_trace,
    prepush,
    reading,
    replacing,
    services,
)
from app.services.sandbox_workspace import checkout as checkouts
from app.services.sandbox_workspace import landed as landing
from app.services.sandbox_workspace import rebase as rebasing
from app.services.sandbox_workspace.checkout import RECORD_NAME, build_record
from app.services.sandbox_workspace.checks import MAX_ARGS, WHOLE_ARG, ChecksError
from app.services.sandbox_workspace.detect import WORKFLOWS_DIR
from app.services.sandbox_workspace.gitignore import GitIgnore, gitignore_paths
from app.services.sandbox_workspace.ingest import WorkspaceIngestor, read_ref_bytes
from app.services.sandbox_workspace.kube import KubeError
from app.services.sandbox_workspace.manifest import QuotaExceededError, format_diff, format_size
from app.services.sandbox_workspace.paths import WorkspacePathError, display, is_under, normalize
from app.services.sandbox_workspace.repository import ArchiveTooLargeError, extract_tree
from app.services.sandbox_workspace.session import (
    WorkspaceFileUnreadableError,
    WorkspaceSession,
    WorkspaceUnavailableError,
)
from app.tools.base import (
    CallableToolExecutor,
    FileContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
    capturing_ingest,
    offered_tools,
)

logger = logging.getLogger(__name__)

#: The ``sandbox_*`` tools and ``materialize_repository``: built only for a
#: turn or a run that has a workspace.
SANDBOX_TOOL_NAMES = (
    "sandbox_put_file",
    "sandbox_get_file",
    "sandbox_list_files",
    "sandbox_remove_files",
    "sandbox_move_files",
    "sandbox_read_file",
    "sandbox_grep",
    "sandbox_outline",
    "sandbox_read_symbol",
    "sandbox_playwright_trace",
    "sandbox_edit_file",
    "sandbox_replace_in_files",
    "sandbox_codemod",
    "sandbox_apply_patch",
    "sandbox_write_file",
    "sandbox_json_keys",
    "sandbox_convert_images",
    "materialize_repository",
    "sandbox_checkout_status",
    "sandbox_checkout_diff",
    "sandbox_checkout_rebase",
    "sandbox_diff_files",
    "sandbox_run_command",
    "sandbox_start_service",
    "sandbox_service_status",
    "sandbox_service_logs",
    "sandbox_stop_service",
)
#: The tools that write the workspace: each is counted in flight on the session, so a
#: ``sandbox_run_command`` or ``sandbox_start_service`` of the same tool round waits for it.
WORKSPACE_WRITE_TOOLS = frozenset(
    {
        "sandbox_put_file",
        "sandbox_remove_files",
        "sandbox_move_files",
        "sandbox_edit_file",
        "sandbox_replace_in_files",
        "sandbox_apply_patch",
        "sandbox_write_file",
        "sandbox_json_keys",
        "sandbox_convert_images",
        "materialize_repository",
        "sandbox_checkout_rebase",
        "sandbox_diff_files",
    }
)
_LIST_LIMIT_MAX = 500
#: The largest text ``sandbox_write_file`` writes in one call (the size the
#: read tools still open as text).
MAX_WRITE_BYTES = reading.MAX_TEXT_FILE_BYTES


def commands_offered() -> bool:
    """Whether ``sandbox_run_command`` and the service tools are offered: the OpenSandbox backend runs commands, the local subprocess sandbox does not."""
    from app.core.config import settings

    return (getattr(settings, "custom_tool_sandbox", "auto") or "auto") == "opensandbox"


class SandboxToolExecutor(CallableToolExecutor):
    """The code interpreter's executor, aware of the run it is added to."""

    def __init__(self, session: WorkspaceSession | None = None) -> None:
        super().__init__()
        self.session = session
        self.composite: Any = None

    def on_composite(self, composite: Any) -> None:
        self.composite = composite
        if self.session is not None and self.session.available:
            composite.workspace_sink = WorkspaceIngestor(self.session)

    async def prepare_for_run(self) -> None:
        if self.session is not None:
            await self.session.prepare()


def _error(message: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=f"Error: {message}", is_error=True)


def _replace_notes(planned: replacing.Plan, skipped: list[str], binary: int, display: Any) -> str:
    """What a replace left out, one sentence each: unchanged matches, files not searched, binary files."""
    notes: list[str] = []
    if planned.unchanged:
        shown = ", ".join(f"{display(p)} ({n} match(es))" for p, n in planned.unchanged[:10])
        more = f" and {len(planned.unchanged) - 10} more" if len(planned.unchanged) > 10 else ""
        notes.append(f"Matched but left as it was by the replacement: {shown}{more}.")
    if skipped:
        more = f" and {len(skipped) - 10} more" if len(skipped) > 10 else ""
        notes.append(f"Not searched: {'; '.join(skipped[:10])}{more}.")
    if binary:
        notes.append(f"{binary} binary file(s) skipped.")
    return (" " + " ".join(notes)) if notes else ""


def _counted_write(session: WorkspaceSession, handler: Any) -> Any:
    """*handler* counted as a workspace write in flight on *session* while it runs, in the session's write turn.

    Counted first (synchronously, so a command of the same round sees it at
    once), then run alone among the writing tools: a second write of the round
    reads what the first committed instead of overwriting it.
    """

    @functools.wraps(handler)
    async def counted(*args: Any, **kwargs: Any) -> Any:
        with session.writing():
            async with session.write_turn():
                return await handler(*args, **kwargs)

    return counted


def _provider_of(definition: ToolDefinition) -> set[str]:
    names = {str(definition.source or "").split(":", 1)[-1].casefold()}
    if definition.source_label:
        names.add(definition.source_label.casefold())
    return {name for name in names if name}


def _same_commit(a: str, b: str) -> bool:
    """Whether two commit SHAs name the same commit, one possibly abbreviated (an archive names 7 to 40 hex)."""
    a, b = (a or "").strip().casefold(), (b or "").strip().casefold()
    if not a or not b:
        return False
    short, full = sorted((a, b), key=len)
    return len(short) >= 7 and full.startswith(short)


def _default_destination(repository: str) -> str:
    return repository.strip().strip("/").rsplit("/", 1)[-1] or "repository"


_ARCHIVE_COMMIT_RE = re.compile(r"-([0-9a-f]{7,40})$")


def _untracked_line(changes: checkouts.CheckoutChanges, record: checkouts.CheckoutRecord) -> str:
    """The out-of-scope edits of a checkout on a line of their own, or ``""`` (#10144)."""
    note = changes.untracked_note(record.path)
    return "\n" + note.strip() if note else ""


def _archive_commit(root: str) -> str:
    """The commit an archive's top directory names (``<owner>-<repo>-<sha>``), else ``""``."""
    match = _ARCHIVE_COMMIT_RE.search(root or "")
    return match.group(1) if match else ""


async def _resolve_put_ref(
    file_context: FileContext | None,
    file_id: object,
    *,
    conversation_id: str | None,
    run_id: str | None,
    user_id: str | None = None,
) -> tuple[Any, str | None]:
    """The file ``sandbox_put_file`` copies, or the refusal of an ambiguous id prefix.

    The turn's context (the id however the model spelled it: bare, ``file-<id>``, the ``[[FILE:<id>]]``
    marker, a unique 8+ hex prefix), then the conversation's or run's own row by id
    (``load_scoped_file_ref``), then a stored file the acting *user_id* owns in any of the user's
    conversations or runs, by id or prefix (``work_item_files.load_owned_file_ref``, the Files page's
    "Mine" predicate), then a template asset whose container the user can see, by its full id
    (``template_containers.load_visible_asset``, the grant ``get_template`` applies). A row found joins the
    context as a fetched file, so ``code_interpreter`` mounts it under ``attachments/`` from then on. Never
    another user's file.
    """
    from app.services.builtin_tools._shared import _file_handle_key, load_scoped_file_ref, resolve_file_ref

    if not isinstance(file_id, str) or not file_id.strip():
        return None, None
    ref = resolve_file_ref(file_context, file_id)
    if ref is not None:
        return ref, None
    refusal = None
    ref = await load_scoped_file_ref(file_id, conversation_id=conversation_id, assignment_run_id=run_id)
    if ref is None and user_id:
        from app.services.work_item_files import load_owned_file_ref

        ref, refusal = await load_owned_file_ref(file_id, user_id)
    if ref is None and refusal is None and user_id:
        from app.llm.base import FileRef
        from app.services.template_containers import load_visible_asset

        asset = await load_visible_asset(_file_handle_key(file_id).removeprefix("file-"), user_id)
        ref = FileRef.from_attachment(asset) if asset is not None else None
    if ref is not None and file_context is not None:
        from app.tools.base import register_fetched_file

        register_fetched_file(file_context, ref)
    return ref, refusal


async def _file_owner_note(file_id: object, owner: uuid.UUID | None) -> str:
    """Whose file a refused id is, for its owner only: `` (it is a file of conversation X)``, or of an
    assignment run, or `` (it is unregistered: bound to no conversation or run)``.

    Only a row the acting user owns is described; any other id, a stranger's included, answers ``""``, the same
    refusal as an id nobody issued, so the answer never confirms another person's file exists. Best effort.
    """
    from app.core.utils import safe_uuid
    from app.services.builtin_tools._shared import _file_handle_key

    if owner is None or not isinstance(file_id, str):
        return ""
    row_id = safe_uuid(_file_handle_key(file_id).removeprefix("file-"))
    if row_id is None:
        return ""
    try:
        from sqlalchemy import select

        from app.models.file_attachment import FileAttachment
        from app.services.builtin_tools._shared import _own_session

        async with _own_session() as db:
            row = (
                await db.execute(
                    select(FileAttachment.conversation_id, FileAttachment.assignment_run_id).where(
                        FileAttachment.id == row_id, FileAttachment.user_id == owner
                    )
                )
            ).first()
    except Exception as exc:
        logger.debug("Owner of refused file %s not read: %s", row_id, format_exception_message(exc))
        return ""
    if row is None:
        return ""
    if row.conversation_id is not None:
        return f" (it is a file of conversation {row.conversation_id})"
    if row.assignment_run_id is not None:
        return f" (it is a file of assignment run {row.assignment_run_id})"
    return " (it is unregistered: bound to no conversation or run)"


async def _ended_runs(run_ids: set[str]) -> set[str]:
    """The ids of *run_ids* whose assignment run has ended (``TERMINAL_RUN_STATUSES``), or no longer exists.

    A checkout records the run that wrote it (#11500): the clean trees of an ended run are reclaimed at the
    file limit. Best effort: a read that fails answers nothing ended, so nothing is reclaimed on its account.
    """
    from app.core.utils import safe_uuid

    wanted = {str(r): safe_uuid(str(r)) for r in run_ids if r}
    ids = [u for u in wanted.values() if u is not None]
    if not ids:
        return set()
    try:
        from sqlalchemy import select

        from app.models.assignment import AssignmentRun
        from app.services.assignment_engine import TERMINAL_RUN_STATUSES
        from app.services.builtin_tools._shared import _own_session

        async with _own_session() as db:
            rows = (
                await db.execute(select(AssignmentRun.id, AssignmentRun.status).where(AssignmentRun.id.in_(ids)))
            ).all()
    except Exception as exc:
        logger.debug("Run states of checkouts not read: %s", format_exception_message(exc))
        return set()
    status = {str(row.id): row.status for row in rows}
    return {
        text
        for text, parsed in wanted.items()
        if parsed is not None and (str(parsed) not in status or status[str(parsed)] in TERMINAL_RUN_STATUSES)
    }


def build_sandbox_workspace_tools(
    session: WorkspaceSession,
    executor: SandboxToolExecutor,
    *,
    db: Any = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
) -> list[ToolDefinition]:
    """Register the workspace tools on *executor*; the definitions to offer with ``code_interpreter``."""
    from app.core.utils import safe_uuid

    owner = safe_uuid(user_id) if user_id else None

    async def sandbox_put_file(file_id: str, path: str, file_context: FileContext | None = None) -> str | ToolResult:
        ref, refusal = await _resolve_put_ref(
            file_context, file_id, conversation_id=conversation_id, run_id=assignment_run_id, user_id=user_id
        )
        if refusal:
            return ToolResult(tool_call_id="", content=refusal, is_error=True)
        if ref is None:
            whose = await _file_owner_note(file_id, owner)
            return _error(
                f"file {file_id} is not a file of this conversation{whose}: pass the file_id a tool or an upload gave."
            )
        try:
            data = await read_ref_bytes(ref, file_context)
            entry, plan = await session.put_bytes(path, data, operation="put")
        except (WorkspacePathError, QuotaExceededError, WorkspaceUnavailableError) as exc:
            metrics.operation("put", "refused")
            return _error(str(exc))
        except Exception as exc:
            metrics.operation("put", "failed")
            return _error(f"the file could not be written to the workspace ({format_exception_message(exc)}).")
        metrics.operation("put", "ok")
        await audit.record(
            "workspace_put",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Put file {ref.filename!r} into the sandbox workspace as {display(entry.path)}",
            details={"file_id": str(ref.file_id), "path": entry.path, "size": entry.size, "sha256": entry.sha256},
        )
        return (
            f"Wrote {ref.filename or file_id} to {display(entry.path)} ({format_size(entry.size)}, sha256 {entry.sha256}). "
            f"Workspace now {format_size(plan.size_bytes)} of {format_size(session.limits.quota_bytes)}."
        )

    async def sandbox_get_file(path: str, file_context: FileContext | None = None) -> str | ToolResult:
        try:
            # The file as the writes of this tool round left it (#11714).
            await session.settled()
            entry, data = await session.get_bytes(path)
        except FileNotFoundError:
            return _error(await session.not_found(path))
        except (WorkspacePathError, WorkspaceUnavailableError) as exc:
            return _error(str(exc))
        from app.services.builtin_tools._shared import persist_tool_artifact

        name = entry.path.rsplit("/", 1)[-1]
        if db is None or not (user_id or conversation_id or assignment_run_id):
            from app.llm.base import FileRef
            from app.tools.base import register_produced_file

            ref = FileRef.from_bytes(data=data, filename=name, content_type=None)
            register_produced_file(file_context, ref)
        else:
            import mimetypes

            ref = await persist_tool_artifact(
                db=db,
                user_id=user_id or "",
                filename=name,
                raw_bytes=data,
                content_type=mimetypes.guess_type(name)[0],
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                file_context=file_context,
                source="agent_generated",
            )
        metrics.operation("get", "ok")
        # The file is registered on the turn already (above). Only an image
        # rides on the result: a text file there is read back to the model as
        # its whole text by every provider (``prepare_tool_result_attachments``),
        # so a 14 KB HTML mockup handed over cost its 14 KB again in the context.
        import mimetypes

        is_image = str(getattr(ref, "content_type", "") or mimetypes.guess_type(name)[0] or "").startswith("image/")
        return ToolResult(
            tool_call_id="",
            content=(
                f"{display(entry.path)} ({format_size(entry.size)}) is now conversation file {ref.file_id}: "
                f"pass file_id={ref.file_id} to any tool that takes a file, and put [[FILE:{ref.file_id}]] in your "
                "reply to hand it to the user. Its content is not shown here: read it with sandbox_read_file."
            ),
            files=[ref] if is_image else None,
        )

    async def sandbox_list_files(
        path: str = "", limit: int = 100, depth: int | None = None, glob: str = ""
    ) -> str | ToolResult:
        try:
            prefix = normalize(path or "", allow_root=True)
            await session.settled()
            entries = await session.entries(refresh=True)
        except (WorkspacePathError, WorkspaceUnavailableError) as exc:
            return _error(str(exc))
        metrics.operation("list", "ok")
        try:
            shown = max(1, min(int(limit), _LIST_LIMIT_MAX))
        except (TypeError, ValueError):
            shown = 100
        try:
            levels = None if depth is None else int(depth)
        except (TypeError, ValueError):
            return _error("depth is a whole number of directory levels (1 = the directory's direct files).")
        from app.services.sandbox_workspace.manifest import format_manifest

        return format_manifest(
            entries,
            limit=shown,
            quota_bytes=session.limits.quota_bytes,
            prefix=prefix,
            depth=levels,
            glob=str(glob or "").strip(),
        )

    async def sandbox_remove_files(paths: list[str] | str) -> str | ToolResult:
        items = [paths] if isinstance(paths, str) else list(paths or [])
        if not items:
            return _error("give the paths (files or directories) to remove.")
        try:
            gone, plan = await session.remove(items)
        except (WorkspacePathError, WorkspaceUnavailableError, QuotaExceededError) as exc:
            return _error(str(exc))
        metrics.operation("remove", "ok")
        if not gone:
            return "Nothing matched: the workspace is unchanged."
        return f"{format_diff(plan.diff)} Workspace now {format_size(plan.size_bytes)}."

    async def sandbox_move_files(
        moves: list[dict[str, str]] | dict[str, str], overwrite: bool = False, copy: bool = False
    ) -> str | ToolResult:
        copy = bool(copy)
        operation, verb = ("copy", "copied") if copy else ("move", "moved")
        try:
            pairs = moving.parse(moves)
            current = await session.entries(refresh=True)
            planned = moving.plan(pairs, current, overwrite=bool(overwrite), copy=copy)
        except (moving.MoveError, WorkspacePathError, WorkspaceUnavailableError) as exc:
            metrics.operation(operation, "refused")
            return _error(str(exc))
        try:
            # A copy keeps its sources: the new entries share their blobs, nothing is uploaded.
            plan = await session.relink(planned.upserts, [] if copy else planned.removals, operation=operation)
        except FileNotFoundError as exc:
            metrics.operation(operation, "refused")
            return _error(
                f"{display(str(exc))} changed while it was {verb}; list the workspace and try again. Nothing was written."
            )
        except (QuotaExceededError, WorkspaceUnavailableError) as exc:
            metrics.operation(operation, "refused")
            return _error(str(exc))
        except Exception as exc:
            metrics.operation(operation, "failed")
            return _error(f"the {operation} could not be written to the workspace ({format_exception_message(exc)}).")
        metrics.operation(operation, "ok")
        summary = f"{verb.capitalize()} {len(planned.moved)} file(s) in the sandbox workspace"
        details = {"moves": [[old, entry.path] for old, entry in planned.moved[:200]], "replaced": planned.replaced}
        # Literal actions: test_sandbox_workspace_audit_reads reads them from the source.
        if copy:
            await audit.record(
                "workspace_copy", user_id=owner, workspace_id=session.workspace_id, summary=summary, details=details
            )
        else:
            await audit.record(
                "workspace_move", user_id=owner, workspace_id=session.workspace_id, summary=summary, details=details
            )
        return (
            f"{verb.capitalize()} {len(planned.moved)} file(s) in one write, content unchanged"
            + (f", {len(planned.replaced)} replaced" if planned.replaced else "")
            + f". Workspace {format_size(plan.size_bytes)} of {format_size(session.limits.quota_bytes)}.\n"
            + moving.report(planned)
        )

    async def _text_of(path: str, *, settle: bool = True) -> tuple[Any, str] | ToolResult:
        """The entry and decoded text of a workspace file, or the refusal.

        *settle*: wait first for the workspace writes of this tool round (an
        edit next to the read), so the read sees what they commit; a tool of
        :data:`WORKSPACE_WRITE_TOOLS` passes ``False``, it is itself one of
        those writes and would wait for itself.
        """
        try:
            if settle:
                await session.settled()
            entry, data = await session.get_bytes(path)
        except FileNotFoundError:
            return _error(await session.not_found(path))
        except (WorkspacePathError, WorkspaceUnavailableError, WorkspaceFileUnreadableError) as exc:
            return _error(str(exc))
        if not reading.is_text(data):
            return _error(
                f"{display(entry.path)} is binary ({format_size(entry.size)}): it has no text window. "
                "sandbox_get_file hands it over as a file."
            )
        return entry, reading.decode(data)

    async def _readable_text(path: str) -> tuple[Any, str, str] | ToolResult:
        """The entry, decoded text and provenance label of a file the read tools answer: the workspace
        file, else (a path under an installed tree) the file of the live command container, whose
        answer ends with :data:`installed.INSTALLED_READ_LABEL`; the label is ``""`` for a workspace file."""
        got = await _text_of(path)
        if not isinstance(got, ToolResult):
            return got[0], got[1], ""
        installed_text = await _installed_text(path)
        if installed_text is None:
            return got
        if isinstance(installed_text, ToolResult):
            return installed_text
        return installed_text[0], installed_text[1], f"\n{installed.INSTALLED_READ_LABEL}"

    async def sandbox_read_file(
        path: str,
        offset_line: int = 1,
        max_lines: int | None = None,
        max_line_chars: int = reading.DEFAULT_LINE_CHARS,
    ) -> str | ToolResult:
        got = await _readable_text(path)
        if isinstance(got, ToolResult):
            return got
        entry, text, label = got
        metrics.operation("read", "ok")
        # No window asked: the default one is bounded in characters too, so a file of long lines
        # answers its first part and the line to continue from, never 60,000 characters at once.
        asked = max_lines is not None
        return (
            reading.format_window(
                display(entry.path),
                reading.window(text, offset_line, max_lines if asked else reading.DEFAULT_WINDOW_LINES),
                size=entry.size,
                line_chars=reading.clamp_line_chars(max_line_chars),
                max_chars=reading.MAX_RESULT_CHARS if asked else reading.DEFAULT_WINDOW_CHARS,
            )
            + label
        )

    async def _installed_text(path: str) -> tuple[Any, str] | ToolResult | None:
        """A file of an installed tree (node_modules, .venv) the manifest does not record, read from the
        workspace's live command container; ``None`` when *path* is not under such a tree."""
        try:
            rel = normalize(path)
        except WorkspacePathError:
            return None
        if not installed.installed_path(rel):
            return None
        try:
            data = await installed.read_file(session, rel, max_bytes=reading.MAX_GREP_FILE_BYTES, user_id=owner)
        except installed.NoLiveContainerError as exc:
            return await _install_first(rel, exc)
        except installed.InstalledUnavailableError as exc:
            return _error(str(exc))
        except FileNotFoundError:
            return _error(
                f"{display(rel)} is neither in the workspace nor in its live command containers (an installed file "
                f"up to {format_size(reading.MAX_GREP_FILE_BYTES)} is read there): check the path."
            )
        except Exception as exc:
            return _error(
                f"{display(rel)} could not be read from the command container ({format_exception_message(exc)})."
            )
        if not reading.is_text(data):
            return _error(f"{display(rel)} is binary ({format_size(len(data))}): it has no text window.")
        from app.services.sandbox_workspace.manifest import ManifestEntry

        return ManifestEntry(path=rel, size=len(data), sha256="", mtime=0.0), reading.decode(data)

    async def _install_first(rel: str, exc: Exception) -> ToolResult:
        """A read of an installed tree with no live command container: a normal answer (not an error,
        nothing was wrong with the call) naming the exact install call to run, then the read again."""
        call = await _install_call(rel)
        metrics.operation("read", "needs_install")
        lines = [f"{display(rel)} is not readable yet: {exc}"]
        if call:
            lines.append(f"Run first: {call}; then repeat this call.")
        return ToolResult(tool_call_id="", content="\n".join(lines))

    async def _outside_record_note(written: list[str]) -> str:
        """The warning naming the written files a checkout's record does not cover, or ``""`` (best effort).

        Each file is matched to the deepest checkout holding it; a file outside every checkout says nothing.
        """
        try:
            entries = await session.entries(refresh=True)
            roots = eviction.checkout_roots(entries)
            by_root: dict[str, list[str]] = {}
            for rel in written:
                holders = [r for r in roots if rel.startswith(f"{r}/")]
                if holders:
                    by_root.setdefault(max(holders, key=len), []).append(rel)
            notes: list[str] = []
            for root, paths in sorted(by_root.items()):
                _entry, data = await session.get_bytes(f"{root}/{RECORD_NAME}")
                record = checkouts.CheckoutRecord.from_json(data, path=root)
                notes.append(checkouts.outside_record_note(record, paths))
            return "".join(notes)
        except Exception:
            logger.debug("outside-record check of %s not resolved", written, exc_info=True)
            return ""

    async def _install_call(rel: str) -> str:
        """``sandbox_run_command(checkout=..., check=...)`` installing the tree *rel* lies in, or ``""``.

        The checkout is the deepest one holding *rel*; its install check is the one of the
        directory that holds the installed tree, else of the nearest enclosing directory.
        """
        from app.core.config import settings

        try:
            entries = await session.entries(refresh=True)
            roots = [r for r in eviction.checkout_roots(entries) if rel.startswith(f"{r}/")]
            if not roots:
                return ""
            root = max(roots, key=len)
            found = await commands.checkout_checks(session, root, images=set(settings.opensandbox_images))
        except Exception:
            logger.debug("install call for %s not resolved", rel, exc_info=True)
            return ""
        directory = installed.install_directory(rel, root)
        name = commands.install_check_for(found.specs, directory)
        if not name:
            return ""
        return f'sandbox_run_command(checkout="{root}", check="{name}")'

    async def _installed_grep(
        regex: Any,
        prefix: str,
        globs: list[str],
        budget: int,
        context: int,
        line_chars: int,
        fields: Any,
        grouping: reading.Grouping | None = None,
    ) -> reading.GrepResult | ToolResult:
        """The search over an installed tree (*prefix* under node_modules, .venv) run on the files of the
        workspace's live command container, read-only: nothing enters the manifest.

        Each batch is one helper run and one tar round trip, so the batches are large
        (:data:`reading.INSTALLED_READ_FILES`), and the search stops before the call's deadline
        naming what it did not reach: one package of a few thousand small files timed the call out.
        """
        try:
            tree, sandbox = await installed.list_tree(session, prefix, user_id=owner)
        except installed.NoLiveContainerError as exc:
            return await _install_first(prefix, exc)
        except installed.InstalledUnavailableError as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(
                f"{display(prefix)} could not be listed in the command container ({format_exception_message(exc)})."
            )
        result = reading.GrepResult()
        sized = [(rel, size) for rel, size in tree.files if reading.selected(rel, prefix, globs)]
        deadline = time.monotonic() + commands.wait_budget()
        batches = reading.plan_grep(sized, result, batch_files=reading.INSTALLED_READ_FILES)
        for index, batch in enumerate(batches):
            if time.monotonic() >= deadline:
                result.stopped = "the call's time budget ran out"
                result.left[:0] = [rel for rest in batches[index:] for rel in rest]
                break
            try:
                blobs = await installed.read_files(sandbox, batch, max_bytes=reading.MAX_GREP_FILE_BYTES)
            except Exception as exc:
                return _error(
                    f"{display(prefix)} could not be read from the command container ({format_exception_message(exc)})."
                )
            for rel in batch:
                data = blobs.get(rel)
                if data is None:
                    result.unreadable.append(rel)
                    continue
                result.bytes_scanned += len(data)
                if not reading.is_text(data):
                    result.files_skipped += 1
                    continue
                result.files_scanned += 1
                if reading.grep_text(
                    display(rel),
                    reading.decode(data),
                    regex,
                    context=context,
                    budget=budget,
                    out=result,
                    line_chars=line_chars,
                    fields=fields,
                    grouping=grouping,
                ):
                    result.files_matched += 1
        if tree.overflow:
            result.stopped = result.stopped or f"{installed.MAX_TREE_FILES} installed files listed"
        return result

    async def sandbox_grep(
        pattern: str,
        path_prefix: str = "",
        max_matches: int = reading.DEFAULT_MAX_MATCHES,
        context_lines: int = 0,
        include_globs: list[str] | None = None,
        ignore_case: bool = False,
        max_line_chars: int = reading.DEFAULT_LINE_CHARS,
        json_fields: list[str] | None = None,
        compact_json: bool = False,
        max_chars: int = reading.DEFAULT_GREP_CHARS,
        group_by: str | None = None,
        time_field: str | None = None,
    ) -> str | ToolResult:
        try:
            regex = reading.compile_pattern(pattern, ignore_case=bool(ignore_case))
            prefix = normalize(path_prefix or "", allow_root=True)
            # The manifest is read once the writes of this tool round committed: an edit next to the search.
            await session.settled()
            entries = await session.entries(refresh=True)
        except (reading.PatternError, WorkspacePathError, WorkspaceUnavailableError) as exc:
            return _error(str(exc))
        globs = [str(g) for g in (include_globs or []) if str(g).strip()]
        budget, context = reading.clamp_matches(max_matches), reading.clamp_context(context_lines)
        line_chars = reading.clamp_line_chars(max_line_chars)
        fields = reading.clamp_json_fields(json_fields, compact_json)
        grouping = reading.clamp_grouping(group_by, time_field)
        result = reading.GrepResult()

        def shaped(found: reading.GrepResult, where: str) -> str:
            chars = reading.clamp_grep_chars(max_chars)
            if grouping is not None:
                return reading.format_groups(pattern, grouping, found, prefix=where, max_chars=chars)
            return reading.format_grep(pattern, found, prefix=where, max_chars=chars)

        sized = [(rel, entries[rel].size) for rel in sorted(entries) if reading.selected(rel, prefix, globs)]
        if not sized and prefix and installed.installed_path(prefix):
            # An installed tree (an install a check ran) is not in the manifest: searched in its container.
            found = await _installed_grep(regex, prefix, globs, budget, context, line_chars, fields, grouping)
            if isinstance(found, ToolResult):
                return found
            metrics.bytes_moved("read", found.bytes_scanned)
            metrics.operation("grep", "ok")
            text = shaped(found, display(prefix))
            return f"{text}\n{installed.INSTALLED_READ_LABEL}"

        async def read(rel: str) -> bytes | None:
            """One file's bytes through the current manifest; ``None`` when it went or stays unreadable."""
            try:
                _entry, data = await session.read_entry(entries[rel])
            except FileNotFoundError:
                return None
            except WorkspaceFileUnreadableError:
                result.unreadable.append(rel)
                return None
            return data

        # A whole checkout is searched to its end within the call's time budget; what the budget did not
        # reach is named (a stop at a file count missed two readers of a renamed path, #758).
        deadline = time.monotonic() + commands.wait_budget()
        batches = reading.plan_grep(sized, result)
        for index, batch in enumerate(batches):
            if time.monotonic() >= deadline:
                result.stopped = "the call's time budget ran out"
                result.left[:0] = [rel for rest in batches[index:] for rel in rest]
                break
            # The reads of a batch run at once (bounded by plan_grep), the matches are taken in path order.
            blobs = await asyncio.gather(*(read(rel) for rel in batch))
            for rel, data in zip(batch, blobs, strict=True):
                if data is None:
                    continue
                result.bytes_scanned += len(data)
                if not reading.is_text(data):
                    result.files_skipped += 1
                    continue
                result.files_scanned += 1
                if reading.grep_text(
                    display(rel),
                    reading.decode(data),
                    regex,
                    context=context,
                    budget=budget,
                    out=result,
                    line_chars=line_chars,
                    fields=fields,
                    grouping=grouping,
                ):
                    result.files_matched += 1
        metrics.bytes_moved("read", result.bytes_scanned)
        metrics.operation("grep", "ok")
        return shaped(result, display(prefix) if prefix else "")

    async def sandbox_outline(path: str) -> str | ToolResult:
        got = await _readable_text(path)
        if isinstance(got, ToolResult):
            return got
        entry, text, label = got
        if entry.size > reading.MAX_TEXT_FILE_BYTES:
            return _error(
                f"{display(entry.path)} is larger than {format_size(reading.MAX_TEXT_FILE_BYTES)}: read it in windows with sandbox_read_file."
            )
        metrics.operation("outline", "ok")
        return (
            reading.format_outline(
                display(entry.path), reading.outline(entry.path, text), total_lines=len(text.splitlines())
            )
            + label
        )

    async def sandbox_playwright_trace(
        path: str,
        around: str | float | None = None,
        window_ms: int | None = None,
        frame_at: float | None = None,
        max_chars: int | None = None,
    ) -> str | ToolResult:
        """A Playwright trace.zip of the workspace, read in one bounded answer; read-only but for the frame file."""
        try:
            await session.settled()
            entry, data = await session.get_bytes(path)
        except FileNotFoundError:
            return _error(await session.not_found(path))
        except (WorkspacePathError, WorkspaceUnavailableError, WorkspaceFileUnreadableError) as exc:
            return _error(str(exc))
        if entry.size > playwright_trace.MAX_TRACE_BYTES:
            return _error(
                f"{display(entry.path)} is larger than {format_size(playwright_trace.MAX_TRACE_BYTES)}: not read as a trace."
            )
        try:
            answer = playwright_trace.summarize(
                data,
                label=display(entry.path),
                around=around,
                window_ms=window_ms,
                frame_at=frame_at,
                max_chars=max_chars,
            )
        except playwright_trace.TraceError as exc:
            metrics.operation("trace", "refused")
            return _error(f"{display(entry.path)}: {exc}")
        text = answer.text
        if answer.frame_bytes is not None and answer.frame_name:
            target = f"{entry.path.rsplit('/', 1)[0]}/{answer.frame_name}" if "/" in entry.path else answer.frame_name
            try:
                frame, _ = await session.put_bytes(target, answer.frame_bytes, operation="put")
                text += f"\nScreencast frame written to {display(frame.path)} ({format_size(frame.size)}): read_file_content shows it."
            except (WorkspacePathError, QuotaExceededError, WorkspaceUnavailableError) as exc:
                text += f"\nScreencast frame not written: {exc}"
        metrics.bytes_moved("read", entry.size)
        metrics.operation("trace", "ok")
        return text

    async def sandbox_read_symbol(
        path: str, symbol: str, occurrence: int = 0, max_line_chars: int = reading.DEFAULT_LINE_CHARS
    ) -> str | ToolResult:
        """One definition of a file whole, by name: one call, re-readable by name after a prune."""
        name = symbol.strip() if isinstance(symbol, str) else ""
        if not name:
            return _error("symbol names the class, function or method to read (Class.method or a bare name).")
        got = await _readable_text(path)
        if isinstance(got, ToolResult):
            return got
        entry, text, label = got
        if entry.size > reading.MAX_TEXT_FILE_BYTES:
            return _error(
                f"{display(entry.path)} is larger than {format_size(reading.MAX_TEXT_FILE_BYTES)}: read it in windows with sandbox_read_file."
            )
        found = reading.symbols(entry.path, text)
        if found is None:
            return _error(reading.format_outline(display(entry.path), None, total_lines=0))
        matches = reading.find_symbol(found, name)
        if not matches:
            near = [s.name for s in found if name.casefold() in s.name.casefold()][:20]
            hint = f" Names close to it: {', '.join(near)}." if near else " sandbox_outline lists the definitions."
            return _error(f"{display(entry.path)} has no definition named {name!r}.{hint}")
        pick = reading.clamp_occurrence(occurrence)
        if len(matches) > 1 and not pick:
            return reading.format_symbol_choices(display(entry.path), name, matches)
        if pick > len(matches):
            return _error(
                f"{display(entry.path)} has {len(matches)} definition(s) named {name!r}: occurrence {pick} does not exist."
            )
        chosen = matches[(pick or 1) - 1]
        metrics.operation("read", "ok")
        win = reading.window(text, chosen.first, chosen.last - chosen.first + 1)
        return (
            f"{chosen.kind} {chosen.name} (lines {chosen.first}-{chosen.last}):\n"
            + reading.format_window(
                display(entry.path), win, size=entry.size, line_chars=reading.clamp_line_chars(max_line_chars)
            )
            + label
        )

    async def sandbox_edit_file(path: str, edits: list[dict] | None = None) -> str | ToolResult:
        try:
            sources = editing.replace_from_sources(edits)
        except editing.EditError as exc:
            return _error(str(exc))
        raw_edits: object = edits
        if sources and isinstance(edits, list):
            # replace_from: the replacement is a workspace file's text, read here, never re-sent by the model.
            texts: dict[int, str] = {}
            for index, source in sources:
                got_source = await _text_of(source, settle=False)
                if isinstance(got_source, ToolResult):
                    return got_source
                texts[index] = got_source[1]
            raw_edits = editing.with_replace_from(edits, texts)
        try:
            parsed = editing.parse_edits(raw_edits, allow_ranges=True)
        except editing.EditError as exc:
            return _error(str(exc))
        got = await _text_of(path, settle=False)
        if isinstance(got, ToolResult):
            return got
        entry, before = got
        try:
            applied = editing.apply_edits(before, parsed)
        except editing.EditError as exc:
            metrics.operation("edit", "refused")
            return _error(f"{display(entry.path)}: {exc}")
        if applied.text == before:
            return f"{display(entry.path)} is unchanged: every edit replaced text with itself. Nothing was written."
        try:
            new_entry, plan = await session.put_bytes(entry.path, applied.text.encode("utf-8"), operation="edit")
        except (QuotaExceededError, WorkspaceUnavailableError) as exc:
            metrics.operation("edit", "refused")
            return _error(str(exc))
        except Exception as exc:
            metrics.operation("edit", "failed")
            return _error(f"the edited file could not be written to the workspace ({format_exception_message(exc)}).")
        added, removed = editing.summarize(before, applied.text)
        outside_note = await _outside_record_note([new_entry.path])
        metrics.operation("edit", "ok")
        await audit.record(
            "workspace_edit",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Edited {display(new_entry.path)} in the sandbox workspace ({len(parsed)} edit(s), +{added} -{removed})",
            details={
                "path": new_entry.path,
                "edits": len(parsed),
                "replacements": applied.replacements,
                "size": new_entry.size,
                "sha256": new_entry.sha256,
            },
        )
        return (
            f"Edited {display(new_entry.path)}: {len(parsed)} edit(s), {sum(applied.replacements)} replacement(s), "
            f"+{added} -{removed} lines; now {format_size(new_entry.size)}, sha256 {new_entry.sha256}. "
            f"Workspace {format_size(plan.size_bytes)} of {format_size(session.limits.quota_bytes)}.{outside_note}\n\n"
            + editing.unified_diff(
                new_entry.path,
                before,
                applied.text,
                max_chars=editing.EDIT_ECHO_CHARS,
                rest_hint="sandbox_checkout_diff or sandbox_read_file reads it",
            )
        )

    async def sandbox_replace_in_files(
        pattern: str,
        replacement: str = "",
        path_prefix: str = "",
        include_globs: list[str] | None = None,
        ignore_case: bool = False,
        max_replacements: int = replacing.DEFAULT_MAX_REPLACEMENTS,
        dry_run: bool = False,
    ) -> str | ToolResult:
        try:
            searched = reading.compile_pattern(pattern, ignore_case=bool(ignore_case))
            regex = re.compile(searched.pattern, searched.flags | re.MULTILINE)
            template = replacing.check_template(replacement)
            prefix = normalize(path_prefix or "", allow_root=True)
            entries = await session.entries(refresh=True)
        except (reading.PatternError, replacing.ReplaceError, WorkspacePathError, WorkspaceUnavailableError) as exc:
            return _error(str(exc))
        globs = [str(g) for g in (include_globs or []) if str(g).strip()]
        if not prefix and not globs:
            return _error(
                "name the files to change: a path_prefix or include_globs (a replace never runs on the whole "
                "workspace unscoped). Nothing was written."
            )
        chosen = [
            rel
            for rel in sorted(entries)
            if reading.selected(rel, prefix, globs)
            and rel.rsplit("/", 1)[-1] != RECORD_NAME
            and not reading.unread_binary(rel)
        ]
        # The budget of sandbox_grep: a file it searches is a file a replace changes, and every
        # file left out is named with its reason, never folded into a "No match".
        texts: list[tuple[str, str]] = []
        skipped: list[str] = []
        binary = 0
        total = 0
        wanted: list[str] = []
        past_cap: list[str] = []
        for rel in chosen:
            size = entries[rel].size
            if size > reading.MAX_GREP_FILE_BYTES:
                skipped.append(
                    f"{display(rel)} ({format_size(size)}, over the {format_size(reading.MAX_GREP_FILE_BYTES)} of one file)"
                )
                continue
            if total + size > reading.MAX_GREP_BYTES:
                past_cap.append(rel)
                continue
            total += size
            wanted.append(rel)
        if past_cap and not dry_run:
            # A file of the scope the call never reads may hold a match: writing the others would
            # leave the change half applied (#485). Refused, nothing written, every file named.
            metrics.operation("replace", "refused")
            shown = ", ".join(display(rel) for rel in past_cap[:10])
            more = f" and {len(past_cap) - 10} more" if len(past_cap) > 10 else ""
            return _error(
                f"the scope holds {len(past_cap)} file(s) past the {format_size(reading.MAX_GREP_BYTES)} one call "
                f"reads: {shown}{more}. Nothing was written (a replace is all or nothing): narrow path_prefix or "
                "include_globs and run it in parts."
            )
        if past_cap:
            more = f" and {len(past_cap) - 10} more" if len(past_cap) > 10 else ""
            skipped.append(
                f"{len(past_cap)} file(s) past the {format_size(reading.MAX_GREP_BYTES)} one call reads "
                f"({', '.join(display(r) for r in past_cap[:10])}{more})"
            )

        async def read(rel: str) -> bytes | None:
            try:
                _entry, data = await session.read_entry(entries[rel])
            except (FileNotFoundError, WorkspaceFileUnreadableError):
                skipped.append(f"{display(rel)} (unreadable)")
                return None
            return data

        # The reads run in bounded batches (as sandbox_grep reads) and stop at the call's time
        # budget (#460): a whole checkout timed out at the tool deadline with no answer at all.
        deadline = time.monotonic() + commands.wait_budget()
        not_reached: list[str] = []
        for start in range(0, len(wanted), reading.GREP_READ_FILES):
            batch = wanted[start : start + reading.GREP_READ_FILES]
            if time.monotonic() >= deadline:
                not_reached = wanted[start:]
                break
            blobs = await asyncio.gather(*(read(rel) for rel in batch))
            for rel, data in zip(batch, blobs, strict=True):
                if data is None:
                    continue
                if reading.is_text(data):
                    texts.append((rel, reading.decode(data)))
                else:
                    binary += 1
        if not_reached and not dry_run:
            metrics.operation("replace", "refused")
            shown = ", ".join(display(rel) for rel in not_reached[:10])
            more = f" and {len(not_reached) - 10} more" if len(not_reached) > 10 else ""
            return _error(
                f"the call's time budget ran out after reading {len(wanted) - len(not_reached)} of {len(wanted)} "
                f"file(s); not reached: {shown}{more}. Nothing was written (a replace is all or nothing): narrow "
                "path_prefix or include_globs and run it in parts."
            )
        try:
            planned = replacing.plan(texts, regex, template, cap=replacing.clamp_max(max_replacements))
        except replacing.ReplaceError as exc:
            metrics.operation("replace", "refused")
            return _error(str(exc))
        if not_reached:
            more = f" and {len(not_reached) - 10} more" if len(not_reached) > 10 else ""
            skipped.append(
                f"{len(not_reached)} file(s) the time budget did not reach ({', '.join(display(r) for r in not_reached[:10])}{more})"
            )
        scope = display(prefix) if prefix else "the workspace"
        notes = _replace_notes(planned, skipped, binary, display)
        if not planned.changes:
            if planned.unchanged:
                return (
                    f"Nothing to change for {pattern[:80]!r} under {scope}: it matches, but the replacement leaves "
                    f"every file as it was (nothing was written).{notes}"
                )
            read = " read before the time budget ran out" if not_reached else ""
            return f"No match for {pattern[:80]!r} in {planned.files_scanned} text file(s){read} under {scope}: nothing was written.{notes}"
        head = (
            f"{planned.replacements} replacement(s) in {len(planned.changes)} file(s) "
            f"of {planned.files_scanned} scanned under {scope}"
        )
        if dry_run:
            stats, diffs = replacing.report(planned.changes, display)
            return f"Dry run, nothing written: {head}.{notes}\n{stats}\n\n{diffs}"
        # A write answers each distinct line change once, not one hunk per site: the dry run
        # (or sandbox_checkout_diff) is where the whole diff is read.
        stats, _diffs = replacing.report(planned.changes, display)
        changed = replacing.distinct_changes(planned.changes)
        try:
            plan = await session.put_tree(
                ((c.path, c.after.encode("utf-8")) for c in planned.changes), operation="edit"
            )
        except (QuotaExceededError, WorkspaceUnavailableError) as exc:
            metrics.operation("replace", "refused")
            return _error(str(exc))
        except Exception as exc:
            metrics.operation("replace", "failed")
            return _error(f"the changed files could not be written to the workspace ({format_exception_message(exc)}).")
        metrics.operation("replace", "ok")
        await audit.record(
            "workspace_edit",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Replaced {planned.replacements} match(es) in {len(planned.changes)} file(s) of the sandbox workspace",
            details={"paths": [c.path for c in planned.changes], "replacements": planned.replacements},
        )
        outside_note = await _outside_record_note([c.path for c in planned.changes])
        return (
            f"Replaced: {head}, in one write. Workspace {format_size(plan.size_bytes)} of "
            f"{format_size(session.limits.quota_bytes)}.{outside_note}{notes}\n{stats}\n\n{changed}"
        )

    async def sandbox_codemod(
        checkout: str,
        transform: str,
        path_prefix: str = "",
        include_globs: list[str] | None = None,
        check: str = "",
        dry_run: bool = False,
    ) -> str | ToolResult:
        """A transform module run over the selected files by the checkout's codemod check, written all or nothing."""
        from app.core.config import settings

        settle_note = await _settle()
        try:
            root = normalize(checkout or "", allow_root=True)
            module = normalize(transform or "")
            prefix = normalize(path_prefix or "", allow_root=True)
            entries = await session.entries(refresh=True)
        except (WorkspacePathError, WorkspaceUnavailableError) as exc:
            return _error(str(exc))
        if module not in entries:
            return _error(
                f"transform {display(module)} is not in the workspace: write the transform module first (sandbox_write_file)."
            )
        globs = [str(g) for g in (include_globs or []) if str(g).strip()]
        if not prefix and not globs:
            return _error("name the files to transform: a path_prefix or include_globs. Nothing was written.")
        chosen = [
            rel
            for rel in sorted(entries)
            if reading.selected(rel, prefix, globs)
            and rel != module
            and rel.rsplit("/", 1)[-1] != RECORD_NAME
            and not rel.startswith(codemod.STAGING_DIR + "/")
            and not reading.unread_binary(rel)
            and entries[rel].size <= reading.MAX_TEXT_FILE_BYTES
        ]
        if not chosen:
            return (
                f"No file selected under {display(prefix) if prefix else 'the workspace'}: nothing was run or written."
            )
        if len(chosen) > codemod.MAX_SELECTED:
            return _error(
                f"{len(chosen)} files selected, over {codemod.MAX_SELECTED}: narrow path_prefix or include_globs. Nothing was written."
            )
        try:
            specs = await commands.checkout_checks(session, root, images=set(settings.opensandbox_images))
        except ChecksError as exc:
            return _error(str(exc))
        name = str(check or "").strip() or next((n for n in sorted(specs.specs) if n.endswith("codemod")), "")
        if not name:
            return _error(
                f"checkout {display(root)} declares no codemod check (a check whose name ends with codemod, "
                "such as [check.frontend-codemod], in .xtm-one/checks.toml, "
                "running the repository's codemod runner): the tool runs only a check the checkout declares."
            )
        staging = codemod.staging_dir(uuid.uuid4().hex[:12])
        befores: list[tuple[str, str]] = []
        for rel in chosen:
            try:
                _entry, data = await session.read_entry(entries[rel])
            except (FileNotFoundError, WorkspaceFileUnreadableError):
                continue
            if reading.is_text(data):
                befores.append((rel, reading.decode(data)))
        try:
            command = await commands.resolve(
                session,
                checkout=checkout,
                check=name,
                args=["placeholder"],
                allow_argv=False,
                max_timeout=max(1, int(settings.code_interpreter_command_max_seconds)),
                known_images=set(settings.opensandbox_images),
            )
        except ChecksError as exc:
            return _error(str(exc))
        except WorkspaceUnavailableError as exc:
            return _error(str(exc))
        top = codemod.root_from(command.cwd)
        # The placeholder argument stands for the staging directory, known only now (relative to the check's directory).
        command = dataclasses.replace(command, argv=(*command.argv[:-1], f"{top}/{staging}"))
        try:
            await session.put_tree(
                [
                    (
                        f"{staging}/{codemod.REQUEST_FILE}",
                        codemod.request(transform=module, files=[p for p, _ in befores], root=top),
                    )
                ],
                operation="codemod",
            )
        except (QuotaExceededError, WorkspaceUnavailableError) as exc:
            return _error(str(exc))
        outcome = await commands.run_with_fallback(command, workspace=session, user_id=owner, db=db, background=False)

        async def _drop_staging(extra: list[tuple[str, bytes]] | None = None) -> Any:
            now = await session.entries(refresh=True)
            return await session.put_tree(
                extra or [], remove=[p for p in now if p.startswith(staging + "/")], operation="codemod"
            )

        if outcome.refused or outcome.busy or outcome.lost or outcome.starting or outcome.running:
            with contextlib.suppress(Exception):
                await _drop_staging()
            metrics.operation("codemod", "refused")
            return _error(f"the codemod check did not complete: nothing was written.\n{outcome.text}")
        now = await session.entries(refresh=True)
        result_path = f"{staging}/{codemod.RESULT_FILE}"
        try:
            if result_path not in now:
                raise codemod.CodemodError(
                    f"the codemod check wrote no {codemod.RESULT_FILE} (exit code {outcome.exit_code}). Nothing was written.\n{outcome.text}"
                )
            _entry, raw = await session.read_entry(now[result_path])
            results = codemod.read_result(raw)
            afters: dict[str, str] = {}
            out_prefix = f"{staging}/{codemod.OUTPUT_DIR}/"
            for rel, entry in now.items():
                if rel.startswith(out_prefix):
                    _e, data = await session.read_entry(entry)
                    afters[rel.removeprefix(out_prefix)] = reading.decode(data)
            planned = codemod.plan(befores, results, afters)
            if planned.refused:
                raise codemod.CodemodError(codemod.refusal(planned, display))
            if len(planned.changes) > codemod.MAX_FILES:
                raise codemod.CodemodError(
                    f"the transform changes {len(planned.changes)} files, over {codemod.MAX_FILES}: run it in parts. Nothing was written."
                )
        except codemod.CodemodError as exc:
            with contextlib.suppress(Exception):
                await _drop_staging()
            metrics.operation("codemod", "refused")
            return _error(str(exc))
        settle = f"{settle_note}\n\n" if settle_note else ""
        scope = display(prefix) if prefix else "the workspace"
        if not planned.changes:
            with contextlib.suppress(Exception):
                await _drop_staging()
            metrics.operation("codemod", "ok")
            return f"{settle}The transform changed none of the {planned.files_scanned} file(s) under {scope}: nothing was written."
        stats, diffs = codemod.report(planned.changes, display)
        head = f"{len(planned.changes)} file(s) changed of {planned.files_scanned} transformed under {scope}, every one parses"
        if dry_run:
            with contextlib.suppress(Exception):
                await _drop_staging()
            metrics.operation("codemod", "ok")
            return f"{settle}Dry run, nothing written: {head}.\n{stats}\n\n{diffs}"
        try:
            plan = await _drop_staging([(c.path, c.after.encode("utf-8")) for c in planned.changes])
        except (QuotaExceededError, WorkspaceUnavailableError) as exc:
            metrics.operation("codemod", "refused")
            return _error(str(exc))
        metrics.operation("codemod", "ok")
        await audit.record(
            "workspace_edit",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Codemod {display(module)} changed {len(planned.changes)} file(s) of the sandbox workspace",
            details={"paths": [c.path for c in planned.changes], "transform": module, "check": name},
        )
        return (
            f"{settle}Transformed: {head}, in one write. Workspace {format_size(plan.size_bytes)} of "
            f"{format_size(session.limits.quota_bytes)}.\n{stats}\n\n{diffs}"
        )

    async def sandbox_apply_patch(
        patch: str = "",
        checkout: str = "",
        exclude: list[str] | str | None = None,
        dry_run: bool = False,
    ) -> str | ToolResult:
        """A unified diff the workspace holds applied to a checkout's files: fuzz 0, all or nothing, one write."""
        if not str(patch or "").strip():
            return _error(
                "patch names the workspace file holding the unified diff (e.g. the to_workspace_path of get_github_pr_diff)."
            )
        got = await _text_of(patch, settle=False)
        if isinstance(got, ToolResult):
            return got
        patch_entry, diff_text = got
        try:
            root = normalize(checkout or "", allow_root=True)
            files = patching.parse(diff_text)
            entries = await session.entries(refresh=True)
        except (WorkspacePathError, patching.PatchError, WorkspaceUnavailableError) as exc:
            metrics.operation("apply_patch", "refused")
            return _error(f"{exc}. Nothing was written.")
        skip = [exclude] if isinstance(exclude, str) else [str(p) for p in (exclude or [])]
        chosen = [f for f in files if not patching.excluded(f, skip)]
        left_out = [f.path for f in files if f not in chosen]
        if len(chosen) > patching.MAX_FILES:
            return _error(
                f"the patch changes {len(chosen)} files, more than {patching.MAX_FILES}: apply it in parts with exclude. Nothing was written."
            )

        def _at(rel: str) -> str:
            return f"{root}/{rel}" if root else rel

        writes: dict[str, bytes] = {}
        removals: list[str] = []
        failures: list[patching.Failure] = []
        lines: list[str] = []
        for file_patch in chosen:
            try:
                for name in (file_patch.old_path, file_patch.new_path):
                    if name is not None:
                        patching.check_path(name)
                if file_patch.binary:
                    raise patching.PatchError(
                        "a binary patch has no text hunks: exclude it and copy the file in with sandbox_put_file"
                    )
                if file_patch.mode_only:
                    lines.append(f"- {file_patch.path}: mode change only, nothing to write")
                    continue
                source = _at(file_patch.old_path) if file_patch.old_path is not None else None
                before: str | None = None
                if source is not None and source in entries:
                    _e, data = await session.read_entry(entries[source])
                    if not reading.is_text(data):
                        raise patching.PatchError("the checkout's file is binary")
                    before = reading.decode(data)
                if file_patch.kind == "added" and _at(file_patch.path) in entries:
                    raise patching.PatchError("the patch adds the file, which already exists in the checkout")
                if file_patch.kind == "renamed" and _at(file_patch.new_path or "") in entries:
                    raise patching.PatchError(f"the rename target {file_patch.new_path} already exists in the checkout")
                applied = patching.apply_file(before, file_patch)
            except patching.PatchError as exc:
                message = str(exc)
                header = (
                    message.split(" does not apply", 1)[0].removeprefix("hunk ") if message.startswith("hunk ") else ""
                )
                failures.append(patching.Failure(path=file_patch.path, header=header, message=message))
                continue
            except (FileNotFoundError, WorkspaceFileUnreadableError) as exc:
                failures.append(patching.Failure(path=file_patch.path, header="", message=f"unreadable ({exc})"))
                continue
            added, removed = file_patch.stat()
            moved = [o for _h, o in applied.offsets if o]
            note = f", {len(moved)} hunk(s) at an offset ({', '.join(f'{o:+d}' for o in moved[:5])})" if moved else ""
            if file_patch.kind == "deleted":
                removals.append(_at(file_patch.old_path or ""))
            else:
                writes[_at(file_patch.new_path or "")] = applied.text.encode("utf-8")
                if file_patch.kind == "renamed":
                    removals.append(_at(file_patch.old_path or ""))
            shown = (
                file_patch.path if file_patch.kind != "renamed" else f"{file_patch.old_path} -> {file_patch.new_path}"
            )
            lines.append(f"- {shown}: {file_patch.kind}, {len(file_patch.hunks)} hunk(s), +{added} -{removed}{note}")
        scope = display(root) if root else "the workspace root"
        excluded_note = (
            f"\nExcluded ({len(left_out)}): {', '.join(left_out[:20])}" + (" ..." if len(left_out) > 20 else "")
            if left_out
            else ""
        )
        if failures:
            metrics.operation("apply_patch", "refused")
            shown_failures = "\n".join(f"- {f.path}: {f.message}" for f in failures[: patching.MAX_REPORTED_FAILURES])
            more = (
                f"\n... and {len(failures) - patching.MAX_REPORTED_FAILURES} more"
                if len(failures) > patching.MAX_REPORTED_FAILURES
                else ""
            )
            return _error(
                f"{len(failures)} of {len(chosen)} file patch(es) of {display(patch_entry.path)} do not apply to {scope}: "
                f"nothing was written.\n{shown_failures}{more}\nRebase the checkout onto the diff's base, or exclude those files "
                f"and settle them with sandbox_edit_file.{excluded_note}"
            )
        head = f"{len(chosen)} file patch(es) of {display(patch_entry.path)} applied to {scope}"
        if dry_run:
            return f"Dry run, nothing written: {head}.\n" + "\n".join(lines) + excluded_note
        if not writes and not removals:
            return f"{head}: nothing to write.\n" + "\n".join(lines) + excluded_note
        try:
            plan = await session.put_tree(writes.items(), remove=removals, operation="edit")
        except (QuotaExceededError, WorkspaceUnavailableError, WorkspacePathError) as exc:
            metrics.operation("apply_patch", "refused")
            return _error(f"{exc}. Nothing was written.")
        except Exception as exc:
            metrics.operation("apply_patch", "failed")
            return _error(f"the patched files could not be written to the workspace ({format_exception_message(exc)}).")
        metrics.operation("apply_patch", "ok")
        await audit.record(
            "workspace_edit",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Applied a patch to {len(writes) + len(removals)} file(s) of the sandbox workspace",
            details={"patch": patch_entry.path, "written": sorted(writes), "removed": removals, "excluded": left_out},
        )
        return (
            f"Applied: {head}, in one write ({len(writes)} written, {len(removals)} removed). Workspace "
            f"{format_size(plan.size_bytes)} of {format_size(session.limits.quota_bytes)}. sandbox_checkout_diff reads the result.\n"
            + "\n".join(lines)
            + excluded_note
        )

    async def sandbox_write_file(
        path: str, content: str = "", create_dirs: bool = True, overwrite: bool = False
    ) -> str | ToolResult:
        if not isinstance(content, str):
            return _error("content must be text (UTF-8); sandbox_put_file copies a binary file in.")
        data = content.encode("utf-8", errors="surrogatepass") if content else b""
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return _error("content holds a lone surrogate: it is not valid UTF-8 text. Nothing was written.")
        if len(data) > MAX_WRITE_BYTES:
            return _error(
                f"content is {format_size(len(data))}, over the {format_size(MAX_WRITE_BYTES)} one call writes. Nothing was written."
            )
        try:
            rel = normalize(path)
            current = await session.entries(refresh=True)
        except (WorkspacePathError, WorkspaceUnavailableError) as exc:
            metrics.operation("write", "refused")
            return _error(str(exc))
        if any(p.startswith(rel + "/") for p in current):
            metrics.operation("write", "refused")
            return _error(f"{display(rel)} is a directory of the workspace: give a file path. Nothing was written.")
        parents = [rel.rsplit("/", i)[0] for i in range(1, rel.count("/") + 1)]
        blocking = next((p for p in parents if p in current), None)
        if blocking is not None:
            metrics.operation("write", "refused")
            return _error(f"{display(blocking)} is a file, so it cannot hold {display(rel)}. Nothing was written.")
        existing = current.get(rel)
        if existing is not None and not overwrite:
            metrics.operation("write", "refused")
            return _error(
                f"{display(rel)} already exists ({format_size(existing.size)}): change it with sandbox_edit_file, "
                "or pass overwrite=true to replace it whole. Nothing was written."
            )
        if not create_dirs and parents and not any(p.startswith(parents[0] + "/") for p in current):
            metrics.operation("write", "refused")
            return _error(f"{display(parents[0])} does not exist and create_dirs is false. Nothing was written.")
        try:
            # put_bytes uploads the content and commits the manifest before it
            # returns: a sandbox_run_command or code_interpreter call made
            # after this answer finds the file.
            entry, plan = await session.put_bytes(rel, data, operation="write")
        except (QuotaExceededError, WorkspaceUnavailableError) as exc:
            metrics.operation("write", "refused")
            return _error(str(exc))
        except Exception as exc:
            metrics.operation("write", "failed")
            return _error(f"the file could not be written to the workspace ({format_exception_message(exc)}).")
        metrics.operation("write", "ok")
        verb = "Replaced" if existing is not None else "Created"
        await audit.record(
            "workspace_write",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"{verb} {display(entry.path)} in the sandbox workspace ({format_size(entry.size)})",
            details={"path": entry.path, "size": entry.size, "sha256": entry.sha256, "overwrite": existing is not None},
        )
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        outside_note = await _outside_record_note([entry.path])
        return (
            f"{verb} {display(entry.path)}: {lines} line(s), {format_size(entry.size)}, sha256 {entry.sha256}. "
            f"Workspace {format_size(plan.size_bytes)} of {format_size(session.limits.quota_bytes)}.{outside_note}"
        )

    async def sandbox_json_keys(
        paths: list[str] | None = None,
        set: dict | None = None,
        rename: dict | None = None,
        remove: list[str] | None = None,
        get: list[str] | None = None,
        place: dict | None = None,
        french_spacing: str | None = None,
        set_from: str | None = None,
    ) -> str | ToolResult:
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            return _error("paths must name one or more JSON files of the workspace.")
        if set_from:
            # set_from: the set object is a workspace JSON file, read here, never re-sent by the model.
            if set is not None:
                return _error("give either 'set' or 'set_from', not both. Nothing was written.")
            got_set = await _text_of(set_from, settle=False)
            if isinstance(got_set, ToolResult):
                return got_set
            try:
                set = json.loads(got_set[1])
            except ValueError as exc:
                return _error(f"set_from {set_from} is not JSON ({exc}). Nothing was written.")
            if not isinstance(set, dict):
                return _error(f"set_from {set_from} must hold a JSON object like 'set'. Nothing was written.")
        texts: dict[str, str] = {}
        for path in paths:
            got = await _text_of(path, settle=False)
            if isinstance(got, ToolResult):
                return got
            entry, text = got
            texts[entry.path] = text
        try:
            given = {(p if p == "*" else normalize(p)): v for p, v in set.items()} if isinstance(set, dict) else set
            changes = jsonkeys.change_keys(
                texts,
                set_keys=given,
                rename=rename,
                remove=remove,
                get=get,
                place=place,
                french_spacing=french_spacing,
            )
        except (jsonkeys.JsonKeysError, WorkspacePathError) as exc:
            metrics.operation("json_keys", "refused")
            return _error(f"{exc} Nothing was written.")
        written = [c for c in changes if c.modified]
        if written:
            try:
                plan = await session.put_tree(
                    ((c.path, c.text.encode("utf-8")) for c in written), operation="json_keys"
                )
            except (QuotaExceededError, WorkspaceUnavailableError) as exc:
                metrics.operation("json_keys", "refused")
                return _error(str(exc))
            except Exception as exc:
                metrics.operation("json_keys", "failed")
                return _error(f"the files could not be written to the workspace ({format_exception_message(exc)}).")
            await audit.record(
                "workspace_edit",
                user_id=owner,
                workspace_id=session.workspace_id,
                summary=f"Changed the keys of {len(written)} JSON file(s) in the sandbox workspace",
                details={"paths": [c.path for c in written]},
            )
        metrics.operation("json_keys", "ok")
        lines = []
        for change in changes:
            lines.append(display(change.path) + change.line()[len(change.path) :])
            if change.spacing_line():
                lines.append(change.spacing_line())
            lines.extend(jsonkeys.describe_values(change))
        head = (
            f"Wrote {len(written)} of {len(changes)} file(s) in one commit; workspace {format_size(plan.size_bytes)}."
            if written
            else "Nothing was written."
        )
        return head + "\n" + "\n".join(lines)

    async def sandbox_convert_images(
        conversions: list[dict[str, Any]] | dict[str, Any] | None = None, overwrite: bool = False
    ) -> str | ToolResult:
        try:
            planned = images.parse(conversions)
            current = await session.entries(refresh=True)
        except (images.ImageError, WorkspacePathError, WorkspaceUnavailableError) as exc:
            metrics.operation("convert_images", "refused")
            return _error(f"{exc} Nothing was written.")
        for conversion in planned:
            if conversion.source not in current:
                metrics.operation("convert_images", "refused")
                return _error(f"{display(conversion.source)} is not a file of the workspace. Nothing was written.")
            if conversion.target in current and conversion.target != conversion.source and not overwrite:
                metrics.operation("convert_images", "refused")
                return _error(
                    f"{display(conversion.target)} already exists: pass overwrite=true to replace it. Nothing was written."
                )
        results: list[images.Converted] = []
        for conversion in planned:
            try:
                _entry, data = await session.get_bytes(conversion.source)
                # Pillow decodes on a thread: a large capture must not hold the event loop.
                results.append(await asyncio.to_thread(images.convert, conversion, data))
            except FileNotFoundError:
                metrics.operation("convert_images", "refused")
                return _error(f"{display(conversion.source)} changed while it was read. Nothing was written.")
            except images.ImageError as exc:
                metrics.operation("convert_images", "refused")
                return _error(f"{exc} Nothing was written.")
        try:
            plan = await session.put_tree(((r.conversion.target, r.data) for r in results), operation="convert_images")
        except (QuotaExceededError, WorkspaceUnavailableError) as exc:
            metrics.operation("convert_images", "refused")
            return _error(str(exc))
        except Exception as exc:
            metrics.operation("convert_images", "failed")
            return _error(f"the images could not be written to the workspace ({format_exception_message(exc)}).")
        metrics.operation("convert_images", "ok")
        await audit.record(
            "workspace_write",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Converted {len(results)} image(s) in the sandbox workspace",
            details={"conversions": [[r.conversion.source, r.conversion.target] for r in results]},
        )
        return (
            f"Converted {len(results)} image(s) in one write. "
            f"Workspace {format_size(plan.size_bytes)} of {format_size(session.limits.quota_bytes)}.\n"
            + "\n".join(r.line() for r in results)
        )

    async def _archive_tree(
        provider: str,
        repository: str,
        ref: str,
        subs: list[str],
        *,
        operation: str,
        extra_files: list[str] | None = None,
    ) -> tuple[Any, str] | ToolResult:
        """The tree at *ref* through the provider's archive tool, filtered to *subs* (and *extra_files*, kept as they
        are): ``(tree, tool name)``, or the error."""
        composite = executor.composite
        if composite is None:
            return _error("no integration is connected to this run.")
        surface = offered_tools() or tuple(composite.all_tools)
        wanted = str(provider or "").strip().casefold()
        archive_tools = [d for d in surface if d.repository_archive]
        candidates = [d for d in archive_tools if wanted in _provider_of(d)]
        if not candidates:
            known = sorted({name for d in archive_tools for name in _provider_of(d) if ":" not in name})
            return _error(
                f"no {provider!r} repository integration is connected to this agent"
                + (f"; connected: {', '.join(known)}" if known else "; none is")
                + f". {operation} reads repositories through a connected integration only."
            )
        remaining = max(
            0, session.limits.quota_bytes - sum(e.size for e in (await session.entries(refresh=True)).values())
        )
        last_error = ""
        for definition in candidates:
            arguments: dict[str, Any] = {"repo": repository, "ref": ref}
            # An archive tool that narrows to paths (GitHub: a repository whose whole archive is over its
            # in-memory limit) gets the sub_paths and the extra files, so a narrow checkout of a heavy
            # repository is never refused for the media it leaves out.
            if subs and "paths" in ((definition.input_schema or {}).get("properties") or {}):
                arguments["paths"] = [*subs, *(extra_files or [])]
            call = ToolCall(
                id=f"materialize-{uuid.uuid4().hex[:12]}",
                name=definition.name,
                arguments=arguments,
            )
            # This call passed the approval gate (when the run has one): the
            # download it makes is the one that was approved. A gate raised
            # inside a tool could not reach the person anyway.
            bypass = getattr(composite, "bypass_approval_for", None)
            if callable(bypass):
                bypass({call.id})
            with capturing_ingest() as captured:
                result = await composite.execute_tool(call)
            if result.is_error:
                last_error = result.content
                continue
            archives = [*captured, *(result.files or [])]
            if not archives:
                last_error = f"{definition.name} returned no archive"
                continue
            try:
                data = await read_ref_bytes(archives[0], composite.file_context)
                tree = extract_tree(
                    data, sub_paths=subs, max_bytes=remaining or session.limits.quota_bytes, extra_files=extra_files
                )
                left_out = (result.structured or {}).get("left_out") if isinstance(result.structured, dict) else None
                if left_out:
                    tree.skipped.extend(f"{p} (outside the sub_paths of an archive over its limit)" for p in left_out)
            except (ArchiveTooLargeError, WorkspacePathError) as exc:
                metrics.operation(operation, "refused")
                return _error(f"{repository} was not written: {exc}.")
            except Exception as exc:
                last_error = f"the archive could not be read ({format_exception_message(exc)})"
                continue
            return tree, definition.name
        metrics.operation(operation, "failed")
        return _error(f"{repository} could not be checked out: {last_error[:500]}")

    async def materialize_repository(
        provider: str,
        repository: str,
        ref: str = "",
        sub_paths: list[str] | None = None,
        path: str = "",
        add_paths: list[str] | None = None,
        prune_clean_checkouts: bool = False,
    ) -> str | ToolResult:
        try:
            destination = normalize(path or _default_destination(repository))
            subs = [normalize(sub) for sub in sub_paths or [] if str(sub).strip().strip("/")]
            adds = [normalize(sub) for sub in add_paths or [] if str(sub).strip().strip("/")]
        except WorkspacePathError as exc:
            return _error(str(exc))
        if adds:
            if subs or str(ref or "").strip():
                return _error(
                    "add_paths grows the checkout at path at the commit it records: pass neither sub_paths "
                    "nor ref with it (a new tree is a checkout at another path)."
                )
            return await _grow_checkout(repository, destination, adds, prune=bool(prune_clean_checkouts))
        fetched = await _archive_tree(provider, repository, ref, subs, operation="materialize_repository")
        if isinstance(fetched, ToolResult):
            return fetched
        tree, tool_name = fetched
        # A sub_path inside the directory a declared check runs from was
        # widened to it: the record, the replace set and the answer
        # name what was written.
        requested, subs = subs, tree.sub_paths or subs
        if not tree.files:
            return _error(
                f"{repository}@{ref or 'default branch'} has no file"
                + (f" under {', '.join(subs)}" if subs else "")
                + ": nothing was written."
            )
        # The checkout record (checkout.py): what was checked out and the
        # hash of every file, so create_github_commit(from_checkout=...)
        # can tell what the sandbox changed. The archive's top directory
        # names the commit the ref resolved to (GitHub: <owner>-<repo>-<sha>).
        # The files an earlier, wider tree left outside the sub_paths are its
        # out-of-scope baseline: an edit to one is named later (#10144).
        outside = (
            checkouts.outside_baseline(
                destination, subs, (p for p, _ in tree.files), await session.entries(refresh=True)
            )
            if subs
            else {}
        )
        record = build_record(
            outside=outside,
            run_id=assignment_run_id or "",
            provider=provider,
            repository=repository,
            ref=ref,
            commit=_archive_commit(tree.root),
            path=destination,
            sub_paths=subs,
            files=tree.files,
        )
        files_out = [*tree.files, (RECORD_NAME, record.to_json())]
        # A narrow checkout replaces its companions too: the
        # workflows directory whole, each manifest by its path.
        replace_scopes = [*subs, *tree.companions, *([WORKFLOWS_DIR] if subs else [])] or [""]
        evicted: list[eviction.Footprint] = []
        try:
            plan = await session.put_tree(
                files_out, under=destination, replace=replace_scopes, operation="materialize_repository"
            )
        except WorkspaceUnavailableError as exc:
            metrics.operation("materialize_repository", "refused")
            return _error(str(exc))
        except QuotaExceededError as exc:
            # Over the file limit: the clean checkouts of a ref that is gone
            # (a branch deleted at its merge) go in the same change set; a
            # checkout with a local change never does (eviction.py).
            async def _write_without(remove: list[str]) -> Any:
                return await session.put_tree(
                    files_out,
                    under=destination,
                    replace=replace_scopes,
                    remove=remove,
                    operation="materialize_repository",
                )

            retried = await _evict_stale_checkouts(
                destination, _write_without, str(exc), prune=bool(prune_clean_checkouts)
            )
            if isinstance(retried, ToolResult):
                metrics.operation("materialize_repository", "refused")
                return retried
            plan, evicted = retried
        metrics.operation("materialize_repository", "ok")
        size = sum(len(content) for _p, content in tree.files)
        await audit.record(
            "materialize_repository",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Checked {repository}@{ref or 'default branch'} out into the sandbox workspace at {display(destination)}",
            details={
                "provider": provider,
                "repository": repository,
                "ref": ref,
                "sub_paths": subs,
                "requested_sub_paths": requested,
                "path": destination,
                "files": len(tree.files),
                "companions": len(tree.companions),
                "size": size,
                "tool": tool_name,
            },
        )
        skipped = (
            f" Not written: {', '.join(tree.skipped[:5])}"
            + (f", +{len(tree.skipped) - 5} more" if len(tree.skipped) > 5 else "")
            + "."
            if tree.skipped
            else ""
        )
        return (
            f"Checked {repository}@{ref or 'default branch'} out into {display(destination)}/: "
            f"{len(tree.files):,} file(s), {format_size(size)}"
            + (f" (under {', '.join(subs)})" if subs else "")
            + "".join(f", {w.sub_path} widened to {w.directory}/ ({w.words()})" for w in tree.widened)
            + "".join(f", {d} added (a declared check of the checkout reads it: needs)" for d in tree.needed)
            + "".join(f", {d}/ added (a requirements file under them installs from it)" for d in tree.dependencies)
            + "".join(f", {d}/ added (a Maven module under them builds against it)" for d in tree.reactor)
            + "".join(
                f", {d}/ added (the install's prepare or postinstall script builds there)" for d in tree.lifecycle
            )
            + "".join(f", {d}/ added (a package of the checkout links it: workspace:)" for d in tree.workspace_links)
            + "".join(
                f", {d}/ added (the Relay src a relay.config of the checkout names: the compiler reads every fragment)"
                for d in tree.relay_sources
            )
            + "".join(
                f", {d}/ added (the repository's release notes: a note written there is committed with the change)"
                for d in tree.release_notes
            )
            + (
                f", {', '.join(tree.local[:5])}{f' and {len(tree.local) - 5} more' if len(tree.local) > 5 else ''} kept"
                " (a package.json or .yarnrc.yml of the install names it: patch:, file:, portal:, yarnPath)"
                if tree.local
                else ""
            )
            + (
                f", with {len(tree.companions)} manifest, lock and workflow file(s) from outside them so its checks are detected"
                if tree.companions
                else ""
            )
            + f". The tree only: no .git directory and no credential is written. {format_diff(plan.diff)}"
            + (" " + eviction.evicted_line(evicted, pruned=bool(prune_clean_checkouts)) if evicted else "")
            + f" Workspace now {format_size(plan.size_bytes)} of {format_size(session.limits.quota_bytes)}.{skipped}"
            f" Checkout record: {display(destination)}/{RECORD_NAME}"
            + (f" (commit {record.commit[:12]})" if record.commit else "")
            + f'; create_github_commit(from_checkout="{destination}") commits what changed under it.'
        )

    async def _evict_stale_checkouts(
        destination: str,
        write: Callable[[list[str]], Awaitable[Any]],
        refusal: str,
        *,
        prune: bool = False,
    ) -> tuple[Any, list[eviction.Footprint]] | ToolResult:
        """A materialize (new tree or ``add_paths`` grow) refused for its limits, retried once.

        Reads every other checkout root of the workspace, asks the provider
        whether the ref of each clean one still exists, and calls *write* with
        the files of the clean ones whose ref is gone, removed in the same
        change set as the new files. With *prune* (``prune_clean_checkouts``,
        asked explicitly) every clean root goes, whatever its ref. A root with
        a local change or a stray file never goes. The error names each root
        (size, ref, state) and the removal call when nothing may go or the
        retry is still over the limits.
        """
        entries = await session.entries(refresh=True)
        records: dict[str, checkouts.CheckoutRecord] = {}
        for root in eviction.checkout_roots(entries):
            try:
                _entry, data = await session.get_bytes(f"{root}/{RECORD_NAME}")
                records[root] = checkouts.CheckoutRecord.from_json(data, path=root)
            except (FileNotFoundError, ValueError, WorkspaceUnavailableError):
                continue
        current = str(assignment_run_id or "")
        ignores = {root: await _gitignore_of(record, entries) for root, record in records.items()}
        found = eviction.footprints(
            entries, records, exclude=destination, current_run=current, now=time.time(), ignores=ignores
        )
        # The run each checkout carries (#11500): a clean tree of an ended run
        # goes whatever its ref, so earlier runs never hold the limit.
        others = {f.record.run_id for f in found if f.record.run_id and f.run_state != eviction.RUN_LIVE}
        ended = await _ended_runs(others) if others and not prune else set()
        found = [dataclasses.replace(f, run_state=eviction.RUN_ENDED) if f.record.run_id in ended else f for f in found]
        checked: list[eviction.Footprint] = []
        reads = 0
        for footprint in found:
            # With prune the ref does not decide: no provider read; nor for a
            # checkout already reclaimable for its ended run or its age.
            if (
                not prune
                and footprint.clean
                and not footprint.stale
                and footprint.ref_state == eviction.REF_UNKNOWN
                and reads < eviction.MAX_LISTED
            ):
                reads += 1
                footprint = dataclasses.replace(footprint, ref_state=await _ref_state(footprint.record))
            checked.append(footprint)
        chosen = eviction.chosen(checked, prune=prune)
        hint = eviction.refusal_hint(checked, pruned=prune)
        if not chosen:
            return _error(refusal + ("\n" + hint if hint else ""))
        try:
            plan = await write(eviction.eviction_paths(chosen, entries))
        except (QuotaExceededError, WorkspaceUnavailableError, WorkspacePathError) as exc:
            return _error(f"{exc}\n{hint}")
        await audit.record(
            "workspace_remove",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=(
                f"Evicted {len(chosen)} clean checkout(s)"
                + (" (prune_clean_checkouts)" if prune else " of a gone ref, an ended run or past the idle TTL")
                + " to stay under the workspace limits"
            ),
            details={
                "paths": [f.path for f in chosen],
                "refs": [f.record.ref for f in chosen],
                "runs": [f.record.run_id for f in chosen],
                "reasons": ["prune" if prune else f.reason() for f in chosen],
                "prune": prune,
            },
        )
        metrics.operation("checkout_evict", "ok")
        return plan, chosen

    async def _gitignore_of(record: checkouts.CheckoutRecord, entries: Any) -> GitIgnore:
        """The rules of the checkout's ``.gitignore`` files, as ``sandbox_checkout_status`` reads them."""
        rules: list[tuple[str, bytes]] = []
        for repo_path in gitignore_paths(p for p in map(record.repo_path, map(str, entries)) if p):
            try:
                _entry, data = await session.get_bytes(record.workspace_path(repo_path))
            except (FileNotFoundError, WorkspaceUnavailableError):
                continue
            rules.append((repo_path, data))
        return GitIgnore(rules)

    async def _ref_state(record: checkouts.CheckoutRecord) -> str:
        """Whether the recorded ref still exists, through the provider's ``repository_compare`` tool."""
        composite = executor.composite
        if composite is None or not record.ref:
            return eviction.REF_UNKNOWN
        surface = offered_tools() or tuple(composite.all_tools)
        wanted = record.provider.strip().casefold()
        for definition in [d for d in surface if d.repository_compare and wanted in _provider_of(d)]:
            call = ToolCall(
                id=f"checkout-evict-{uuid.uuid4().hex[:12]}",
                name=definition.name,
                arguments={"repo": record.repository, "base": record.ref, "head": record.ref},
            )
            # materialize_repository passed the approval gate (when the run
            # has one): the read it makes is part of the approved call.
            bypass = getattr(composite, "bypass_approval_for", None)
            if callable(bypass):
                bypass({call.id})
            result = await composite.execute_tool(call)
            if result.is_error:
                continue
            structured = result.structured if isinstance(result.structured, dict) else {}
            if structured.get("status") == "ref_not_found":
                return eviction.REF_GONE
            return eviction.REF_PRESENT
        return eviction.REF_UNKNOWN

    async def _grow_checkout(
        repository: str, destination: str, adds: list[str], *, prune: bool = False
    ) -> str | ToolResult:
        """``materialize_repository(add_paths=...)``: the checkout at *destination* grown at its recorded commit.

        Writes the files under *adds* (widened and with their companions as
        sub_paths are) that the workspace does not hold and the record does
        not name, never removes anything, and extends the record
        (``checkout.grown``) so status, diff, rebase and commits cover the
        new files. A file the workspace holds is kept when it is an edit; a
        stale one, left by an earlier checkout and untouched since the record
        was written (``checkout.stale_on_grow``), is written at the record's
        commit, so it is never committed back as a change (#10998).
        """
        try:
            record, _changes = await WorkspaceIngestor(session).checkout_changes(destination, None)
        except ValueError as exc:
            metrics.operation("materialize_repository", "refused")
            return _error(f"add_paths grows an existing checkout: {exc}")
        if record.repository.strip().casefold() != repository.strip().strip("/").casefold():
            metrics.operation("materialize_repository", "refused")
            return _error(
                f"{display(destination)}/ is a checkout of {record.repository}, not {repository}: "
                "add_paths grows a checkout of the same repository."
            )
        if not record.sub_paths:
            return _error(f"{display(destination)}/ is a checkout of the whole tree: every path is already in it.")
        new = [sub for sub in adds if not record.covers(sub)]
        if not new:
            return (
                f"{', '.join(adds)} already in the checkout at {display(destination)}/ "
                f"(under {', '.join(record.sub_paths)}): nothing was fetched or written."
            )
        base = record.commit or record.ref
        fetched = await _archive_tree(record.provider, record.repository, base, new, operation="materialize_repository")
        if isinstance(fetched, ToolResult):
            return fetched
        tree, tool_name = fetched
        if not tree.files:
            return _error(
                f"{record.repository}@{base or 'default branch'} has no file under {', '.join(new)}: nothing was written."
            )
        held = await session.entries(refresh=True)
        record_entry = held.get(record.record_path)
        writes: list[tuple[str, bytes]] = []
        kept: list[str] = []
        stale: list[str] = []
        for repo_path, data in tree.files:
            if repo_path in record.files:
                continue
            entry = held.get(record.workspace_path(repo_path))
            if entry is None:
                writes.append((repo_path, data))
                continue
            if entry.sha256 == hashlib.sha256(data).hexdigest():
                continue
            if checkouts.stale_on_grow(record, repo_path, entry, since=getattr(record_entry, "mtime", None)):
                stale.append(repo_path)
                writes.append((repo_path, data))
            else:
                kept.append(repo_path)
        moved = checkouts.grown(record, sub_paths=tree.sub_paths or new, files=tree.files)
        if assignment_run_id:
            # The run growing it holds it now: never reclaimed as an ended run's tree.
            moved = dataclasses.replace(moved, run_id=str(assignment_run_id))
        grow_files = [*writes, (RECORD_NAME, moved.to_json())]

        async def _write_without(remove: list[str]) -> Any:
            return await session.put_tree(
                grow_files, under=record.path, remove=remove, operation="materialize_repository"
            )

        evicted: list[eviction.Footprint] = []
        try:
            plan = await _write_without([])
        except QuotaExceededError as exc:
            # The same eviction as a new checkout (#9993): the grown checkout
            # itself is never a candidate, a dirty one never goes.
            retried = await _evict_stale_checkouts(
                record.path, _write_without, f"{display(record.path)}/ was not grown: {exc}.", prune=prune
            )
            if isinstance(retried, ToolResult):
                metrics.operation("materialize_repository", "refused")
                return retried
            plan, evicted = retried
        except (WorkspaceUnavailableError, WorkspacePathError) as exc:
            metrics.operation("materialize_repository", "refused")
            return _error(f"{display(record.path)}/ was not grown: {exc}.")
        metrics.operation("materialize_repository", "ok")
        size = sum(len(content) for _p, content in writes)
        await audit.record(
            "materialize_repository",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Grew the checkout of {record.repository} at {display(record.path)} by {', '.join(new)}",
            details={
                "provider": record.provider,
                "repository": record.repository,
                "ref": record.ref,
                "commit": record.commit,
                "add_paths": new,
                "sub_paths": list(moved.sub_paths),
                "path": record.path,
                "files": len(writes),
                "kept": len(kept),
                "stale": len(stale),
                "size": size,
                "tool": tool_name,
            },
        )
        return (
            f"Grew the checkout at {display(record.path)}/ by {', '.join(new)} at {checkouts.origin(record)}: "
            f"{len(writes):,} file(s) written, {format_size(size)}"
            + "".join(f", {w.sub_path} widened to {w.directory}/ ({w.words()})" for w in tree.widened)
            + "".join(f", {d} added (a declared check of the checkout reads it: needs)" for d in tree.needed)
            + (
                f"; {len(stale):,} stale file(s) an earlier checkout left there, untouched since, were "
                "replaced by their version at that commit"
                if stale
                else ""
            )
            + (
                f"; {len(kept):,} file(s) edited in the workspace were kept as they are (each shows as modified): "
                + ", ".join(kept[:10])
                + (f" and {len(kept) - 10:,} more" if len(kept) > 10 else "")
                if kept
                else ""
            )
            + (". Nothing of this checkout was removed." if evicted else ". Nothing was removed.")
            + f" The record now covers {', '.join(moved.sub_paths)}: status, diff, rebase and "
            f'create_github_commit(from_checkout="{record.path}") include the new files. {format_diff(plan.diff)}'
            + (" " + eviction.evicted_line(evicted, pruned=prune) if evicted else "")
            + f" Workspace now {format_size(plan.size_bytes)} of {format_size(session.limits.quota_bytes)}."
        )

    async def sandbox_checkout_status(checkout: str = "", paths: list[str] | str | None = None) -> str | ToolResult:
        wanted = [paths] if isinstance(paths, str) else list(paths or [])
        try:
            record, changes = await WorkspaceIngestor(session).checkout_changes(checkout, wanted)
        except ValueError as exc:
            metrics.operation("checkout_status", "refused")
            return _error(str(exc))
        metrics.operation("checkout_status", "ok")
        status = checkouts.format_status(record, changes, shown_as=display(record.path))
        # The guards the next push would be warned about, said before it: the read that precedes a commit (#434).
        warned = "" if changes.empty else await WorkspaceIngestor(session).prepush_warning(record, changes)
        return f"{status}\n{warned}" if warned else status

    async def _base_bytes(record: checkouts.CheckoutRecord, repo_path: str, *, at: str = "") -> bytes | str:
        """The file as checked out (or at commit *at*), read through the provider's ``repository_file`` tool; a
        sentence when it cannot be."""
        composite = executor.composite
        if composite is None:
            return "no integration is connected to this run"
        surface = offered_tools() or tuple(composite.all_tools)
        wanted = record.provider.strip().casefold()
        candidates = [d for d in surface if d.repository_file and wanted in _provider_of(d)]
        if not candidates:
            return f"no {record.provider!r} integration that reads a repository file is connected"
        last_error = ""
        for definition in candidates:
            call = ToolCall(
                id=f"checkout-diff-{uuid.uuid4().hex[:12]}",
                name=definition.name,
                arguments={"repo": record.repository, "path": repo_path, "branch": at or record.commit or record.ref},
            )
            # sandbox_checkout_diff passed the approval gate (when the run has
            # one): the reads it makes are the ones that were approved.
            bypass = getattr(composite, "bypass_approval_for", None)
            if callable(bypass):
                bypass({call.id})
            with capturing_ingest() as captured:
                result = await composite.execute_tool(call)
            files = [*captured, *(result.files or [])]
            if result.is_error or not files:
                last_error = (result.content or f"{definition.name} returned no file").removeprefix("Error: ")[:200]
                continue
            try:
                return await read_ref_bytes(files[0], composite.file_context)
            except Exception as exc:
                last_error = f"the file could not be read ({format_exception_message(exc)})"
        return last_error or "the file could not be read"

    async def _compare_with(
        record: checkouts.CheckoutRecord, target: str
    ) -> tuple[str, str, str, rebasing.BranchChanges | None]:
        """``(merge base, why not read, status, branch changes)`` of the recorded commit against *target*, through
        the provider's ``repository_compare`` tool.

        ``("", why, "", None)`` when it cannot be read (no recorded commit, no
        compare tool, an error): the rebase then keeps the files as checked
        out as its base. The status is the compare's: ``identical`` when the
        checkout already sits on the ref's commit, ``ahead`` when it holds it.
        The branch changes are the files the recorded commit changes since the
        merge base (``None`` when the provider does not list them, #11853).
        """
        if not record.commit:
            return "", "the checkout record names no commit", "", None
        composite = executor.composite
        if composite is None:
            return "", "no integration is connected to this run", "", None
        surface = offered_tools() or tuple(composite.all_tools)
        wanted = record.provider.strip().casefold()
        candidates = [d for d in surface if d.repository_compare and wanted in _provider_of(d)]
        if not candidates:
            return "", f"no {record.provider!r} integration that compares refs is connected", "", None
        last_error = ""
        for definition in candidates:
            call = ToolCall(
                id=f"checkout-rebase-{uuid.uuid4().hex[:12]}",
                name=definition.name,
                arguments={"repo": record.repository, "base": target, "head": record.commit},
            )
            # sandbox_checkout_rebase passed the approval gate (when the run
            # has one): the read it makes is the one that was approved.
            bypass = getattr(composite, "bypass_approval_for", None)
            if callable(bypass):
                bypass({call.id})
            result = await composite.execute_tool(call)
            structured = result.structured if isinstance(result.structured, dict) else {}
            sha = str(structured.get("merge_base") or "").strip()
            if not result.is_error and sha:
                return sha, "", str(structured.get("status") or ""), rebasing.branch_changes(structured)
            last_error = (result.content or f"{definition.name} named no merge base").removeprefix("Error: ")[:200]
        return "", last_error or "the merge base could not be read", "", None

    async def sandbox_diff_files(
        path: str = "",
        other: str = "",
        base: str = "",
        context_lines: int = filediff.DEFAULT_CONTEXT_LINES,
        write_merge_to: str = "",
    ) -> str | ToolResult:
        """``diff -u`` of two workspace files, or ``diff3 -m`` of three with *base*."""
        if not str(path or "").strip() or not str(other or "").strip():
            return _error("name two workspace files: path (the first, or ours) and other (the second, or theirs).")
        target = str(write_merge_to or "").strip()
        if target and not str(base or "").strip():
            return _error("write_merge_to needs base: a merge takes three files (base, path as ours, other as theirs).")
        texts: list[tuple[str, str]] = []
        for name in (path, other, *([base] if str(base or "").strip() else [])):
            # A write tool itself (write_merge_to): counted in flight, so it does not wait for its own round.
            got = await _text_of(name, settle=False)
            if isinstance(got, ToolResult):
                metrics.operation("diff_files", "refused")
                return got
            entry, text = got
            texts.append((display(entry.path), text))
        context = filediff.clamp_context(context_lines)
        (ours_name, ours), (theirs_name, theirs) = texts[0], texts[1]
        if len(texts) == 2:
            metrics.operation("diff_files", "ok")
            added, removed = filediff.changed_lines(ours, theirs)
            return f"{ours_name} -> {theirs_name}: +{added} -{removed} lines.\n\n" + filediff.two_way(
                ours_name, ours, theirs_name, theirs, context=context
            )
        base_name, base_text = texts[2]
        merged = filediff.three_way(base_text, ours, theirs, ours_label=ours_name, theirs_label=theirs_name)
        head = f"Three-way merge of {ours_name} (ours) and {theirs_name} (theirs) from {base_name} (base): " + (
            f"{merged.conflicts} conflict(s), markers at {filediff.describe_markers(merged.marker_lines)} of the merged text."
            if merged.conflicts
            else f"no overlapping hunk, but the merged text is broken: {merged.defect}. Settle it before committing."
            if merged.defect
            else f"merged cleanly, no conflict; parsed as {merged.validated}, valid with no duplicate key."
            if merged.validated
            else "merged cleanly, no conflict."
        )
        written = ""
        if target:
            try:
                rel = normalize(target)
                new_entry, _plan = await session.put_bytes(rel, merged.text.encode("utf-8"), operation="write")
            except (WorkspacePathError, QuotaExceededError, WorkspaceUnavailableError) as exc:
                metrics.operation("diff_files", "refused")
                return _error(f"{head} The merge was not written: {exc}")
            except Exception as exc:
                metrics.operation("diff_files", "failed")
                return _error(f"{head} The merge could not be written ({format_exception_message(exc)}).")
            await audit.record(
                "workspace_write",
                user_id=owner,
                workspace_id=session.workspace_id,
                summary=f"Wrote the three-way merge {display(new_entry.path)} in the sandbox workspace ({format_size(new_entry.size)})",
                details={
                    "path": new_entry.path,
                    "size": new_entry.size,
                    "sha256": new_entry.sha256,
                    "conflicts": merged.conflicts,
                },
            )
            written = (
                f" Written to {display(new_entry.path)} ({format_size(new_entry.size)}, sha256 {new_entry.sha256})"
                + (": settle the markers with sandbox_edit_file before committing it." if merged.conflicts else ".")
            )
        metrics.operation("diff_files", "ok")
        added, removed = filediff.changed_lines(ours, merged.text)
        stat = f"{head}{written}\nThe merged text against {ours_name}: +{added} -{removed} lines."
        if target:
            # Written: the conflicts to settle, never the whole diff of the other side's clean changes
            # (a lock file with one conflict answered 30,000 characters of bumps).
            if merged.conflicts:
                return f"{stat}\n\n" + filediff.conflict_hunks(merged.text, merged.marker_lines, context=context)
            return f"{stat} Call again without write_merge_to for the whole diff.\n"
        return f"{stat}\n\n" + filediff.two_way(ours_name, ours, "merged", merged.text, context=context)

    async def sandbox_checkout_diff(
        checkout: str = "",
        paths: list[str] | str | None = None,
        context_lines: int = checkouts.DEFAULT_CONTEXT_LINES,
    ) -> str | ToolResult:
        wanted = [paths] if isinstance(paths, str) else list(paths or [])
        try:
            record, changes = await WorkspaceIngestor(session).checkout_changes(checkout, wanted)
        except ValueError as exc:
            metrics.operation("checkout_diff", "refused")
            return _error(str(exc))
        shown_as = display(record.path)
        if changes.empty:
            metrics.operation("checkout_diff", "ok")
            return checkouts.format_status(record, changes, shown_as=shown_as)
        context = checkouts.clamp_diff_context(context_lines)
        marked = checkouts.marked(changes)
        selected, left_out = marked[: checkouts.MAX_DIFF_FILES], [p for p, _ in marked[checkouts.MAX_DIFF_FILES :]]
        gate = asyncio.Semaphore(6)

        async def one(repo_path: str, mark: str) -> tuple[str, str | None]:
            """The diff of one file, or ``("", reason)`` when it was not diffed."""
            after: bytes | None = None
            if mark != "D":
                try:
                    _entry, after = await session.get_bytes(record.workspace_path(repo_path))
                except (FileNotFoundError, WorkspacePathError, WorkspaceUnavailableError) as exc:
                    return "", f"{repo_path} (unreadable in the workspace: {exc})"
            before: bytes | None = None
            if mark != "A":
                if after is not None and not reading.is_text(after):
                    # A binary is named, never shown: its base is not read.
                    return (
                        f"Binary file {repo_path} differs ({record.files[repo_path].size:,} -> {len(after):,} bytes).\n",
                        None,
                    )
                async with gate:
                    got = await _base_bytes(record, repo_path)
                if isinstance(got, str):
                    return "", f"{repo_path} (base not read: {got})"
                if hashlib.sha256(got).hexdigest() != record.files[repo_path].sha256:
                    return "", (
                        f"{repo_path} (the file read at {record.commit[:12] or record.ref or 'the default branch'} "
                        "does not match the checkout record's sha256: the base moved; check the repository out again)"
                    )
                before = got
            return checkouts.file_diff(repo_path, before, after, context=context), None

        results = await asyncio.gather(*(one(p, m) for p, m in selected))
        parts: list[str] = []
        not_diffed: list[str] = []
        used = 0
        for (repo_path, _mark), (text, reason) in zip(selected, results, strict=True):
            if reason is not None:
                not_diffed.append(reason)
            elif used + len(text) > checkouts.MAX_DIFF_RESULT_CHARS:
                left_out.append(repo_path)
            else:
                parts.append(text)
                used += len(text)
        metrics.operation("checkout_diff", "ok")
        head = (
            f"{shown_as}/: checkout of {checkouts.origin(record)}; {changes.count} file(s) changed, "
            f"{len(parts)} diffed against the files as checked out ({context} context line(s))."
            f"{changes.untracked_note(record.path)}{changes.ignored_note()}"
        )
        tail: list[str] = []
        if not_diffed:
            tail.append("Not diffed: " + "; ".join(not_diffed) + ".")
        if left_out:
            tail.append(
                f"Not shown ({len(left_out)} file(s) past the answer's bound): {', '.join(sorted(left_out)[:20])}"
                + (f", +{len(left_out) - 20} more" if len(left_out) > 20 else "")
                + "; ask again with paths."
            )
        return "\n".join([head, "", *parts, *tail]).rstrip() + "\n"

    async def sandbox_checkout_rebase(
        checkout: str = "",
        ref: str = "",
        on_conflict: str = rebasing.OnConflict.MARKERS.value,
        background: bool = False,
        run_id: str = "",
    ) -> str | ToolResult:
        """The ``git rebase`` of a checkout onto *ref*: a three-way merge per file (``rebase.py``).

        *background* runs it as a job of the workspace ledger (``jobs.py``) and answers its run id at once;
        *run_id* polls that job. A call while the same rebase runs answers that job, never a second rebase.
        """
        if str(run_id or "").strip():
            return await _rebase_poll(str(run_id).strip())
        # Counted before the first await, as a wrapped write is, so a command of the same round waits for it.
        with session.writing():
            return await _rebase_start(checkout, ref, on_conflict, background)

    async def _rebase_start(checkout: str, ref: str, on_conflict: str, background: bool) -> str | ToolResult:
        """Start the rebase: now in the write turn, or as a background job; a running job is answered instead."""
        target = str(ref or "").strip()
        if not target:
            return _error("name the ref to rebase onto: a branch, a tag or a commit SHA.")
        try:
            mode = rebasing.OnConflict(str(on_conflict or rebasing.OnConflict.MARKERS.value).strip())
        except ValueError:
            choices = ", ".join(m.value for m in rebasing.OnConflict)
            return _error(f"on_conflict must be one of: {choices}.")
        key = jobs.rebase_key(checkout, target, mode.value)
        try:
            held = jobs.rebase_job(await session.latest_kernel_state(), key=key)
        except WorkspaceUnavailableError:
            held = None
        if held is not None and jobs.rebase_alive(held):
            # A repeat of a call whose rebase still runs (a resumed turn): that job, never a second rebase.
            return await _rebase_poll(str(held["run_id"]))
        if not background:
            # Registered unwrapped (a poll must not hold the write turn its job waits for): the turn is taken here.
            async with session.write_turn():
                return await _rebase_now(checkout, target, mode)
        job_id = f"rebase-{uuid.uuid4().hex[:12]}"
        label = f"{str(checkout or '.').strip().strip('/') or '.'}/ onto {target}"

        async def work() -> tuple[str, bool]:
            # The job takes the session's write turn itself, after the writes of the round that started it.
            with session.writing():
                async with session.write_turn():
                    outcome = await _rebase_now(checkout, target, mode)
            if isinstance(outcome, ToolResult):
                return outcome.content, outcome.is_error
            return outcome, False

        try:
            await jobs.rebase_running(session, key, job_id, label, work)
        except (WorkspaceUnavailableError, QuotaExceededError) as exc:
            return _error(f"the background rebase was not started: {exc}.")
        metrics.operation("checkout_rebase", "started")
        return (
            f"Rebase of {label} started in the background as run_id {job_id}. sandbox_checkout_rebase(run_id="
            f'"{job_id}") waits for its answer; a resumed turn finds the same job, so it is applied once.\n'
        )

    async def _rebase_poll(job_id: str) -> str | ToolResult:
        """The answer of the background rebase *job_id*, or where it stands."""
        try:
            status, text, is_error = await jobs.rebase_answer(session, job_id)
        except WorkspaceUnavailableError as exc:
            return _error(str(exc))
        if status == "ended":
            return _error(text.removeprefix("Error: ")) if is_error else text
        if status == "running":
            return (
                f"Rebase {job_id} ({text}) is still running. Call sandbox_checkout_rebase(run_id=\"{job_id}\") again "
                "to wait for it; do not start another rebase of this checkout.\n"
            )
        if status == "lost":
            return _error(
                f"rebase {job_id} ({text}) stopped with its worker before it answered. Its writes land in one change "
                "set, so it either landed whole or wrote nothing: start it again with the same checkout and ref, and "
                "a landed rebase answers 'already sits on' without writing."
            )
        return _error(f"no background rebase {job_id} in this workspace: pass the run_id a background call answered.")

    async def _rebase_now(checkout: str, target: str, mode: rebasing.OnConflict) -> str | ToolResult:
        """The rebase itself, in the caller's write turn."""
        try:
            record, changes = await WorkspaceIngestor(session).checkout_changes(checkout, None)
        except ValueError as exc:
            metrics.operation("checkout_rebase", "refused")
            return _error(str(exc))
        # A file still holding the markers of an earlier rebase would be merged as text and nest them:
        # the rebase waits until every conflict the workspace changed is settled.
        unsettled: list[str] = []
        for repo_path in (*changes.modified, *changes.added):
            try:
                _entry, data = await session.get_bytes(record.workspace_path(repo_path))
            except (FileNotFoundError, WorkspacePathError, WorkspaceUnavailableError):
                continue
            if rebasing.has_conflict_markers(data):
                unsettled.append(repo_path)
        if unsettled:
            metrics.operation("checkout_rebase", "refused")
            shown = ", ".join(unsettled[: rebasing.MAX_LISTED])
            more = f", +{len(unsettled) - rebasing.MAX_LISTED} more" if len(unsettled) > rebasing.MAX_LISTED else ""
            return _error(
                f"{display(record.path)}/ was not rebased onto {target}: {len(unsettled)} file(s) still hold conflict "
                f"markers ({shown}{more}), and a rebase over them would nest new markers inside the old ones. Settle "
                f"each with sandbox_edit_file (sandbox_grep '^<<<<<<< ' finds them), then rebase again. Nothing was written."
            )
        # Exactly once: a call replayed after its worker was lost (the answer
        # never came back, the rebase had landed) finds the checkout already on
        # the ref's commit, or ahead of it, and writes nothing, before any
        # download of the ref's tree.
        merge_base, why, compared, branch = await _compare_with(record, target)
        if compared in ("identical", "ahead"):
            metrics.operation("checkout_rebase", "ok")
            where = "on" if compared == "identical" else "ahead of"
            return (
                f"{display(record.path)}/ already sits {where} {record.repository}@{target} (commit "
                f"{record.commit[:12]}): nothing to rebase, nothing was written. A rebase this run started and lost "
                "the answer of has landed; sandbox_checkout_status shows the checkout's changes.\n"
            )
        # The files the branch changes outside a narrow checkout (#11853): the
        # trees below are read at the sub_paths only, so without this a branch's
        # backend hunks were never merged, the record moved to the ref, and a
        # later add_paths wrote the ref's versions over them. Each one is read at
        # the recorded commit, brought into the checkout and merged like any file
        # of it, or the rebase is refused with their names, nothing written.
        scope = list(record.sub_paths)
        outside = (
            branch.outside(record)
            if branch is not None and merge_base and record.sub_paths and not _same_commit(merge_base, record.commit)
            else None
        )
        pulled: dict[str, bytes] = {}
        absent: set[str] = set()
        dropped: list[str] = []
        extra: list[str] = []
        removals: list[str] = []
        if outside is not None and outside.paths:
            extra = list(outside.paths)
            lead = (
                f"{display(record.path)}/ was not rebased onto {target}: the branch at {record.commit[:12]} changes "
                f"{len(extra)} file(s) outside the checkout's sub_paths"
            )
            grow = (
                f" Grow the checkout first (materialize_repository path='{record.path}', add_paths=[...] with their "
                "directories: it writes them at the recorded commit), then rebase. Nothing was written."
            )
            if outside.capped or len(outside.changed) > rebasing.MAX_PULLED:
                metrics.operation("checkout_rebase", "refused")
                cap = " (the provider's compare stopped at its file cap)" if outside.capped else ""
                return _error(
                    f"{lead}{cap}, over the {rebasing.MAX_PULLED} a rebase reads: "
                    f"{', '.join(sorted(extra)[: rebasing.MAX_LISTED])}.{grow}"
                )
            held = await session.entries(refresh=True)
            since = getattr(held.get(record.record_path), "mtime", None)
            unread: list[str] = []
            for repo_path in outside.changed:
                got = await _base_bytes(record, repo_path)
                if isinstance(got, str):
                    unread.append(f"{repo_path} ({got})")
                    continue
                entry = held.get(record.workspace_path(repo_path))
                if entry is None:
                    pulled[repo_path] = got
                    absent.add(repo_path)
                elif entry.sha256 == hashlib.sha256(got).hexdigest() or checkouts.stale_on_grow(
                    record, repo_path, entry, since=since
                ):
                    pulled[repo_path] = got
                # Else the workspace holds an edit of it: that edit is the workspace side of the merge.
            if unread:
                metrics.operation("checkout_rebase", "refused")
                return _error(
                    f"{lead}, and {len(unread)} could not be read at that commit: "
                    f"{'; '.join(unread[: rebasing.MAX_LISTED])}.{grow}"
                )
            for repo_path in outside.removed:
                entry = held.get(record.workspace_path(repo_path))
                if entry is not None and checkouts.stale_on_grow(record, repo_path, entry, since=since):
                    dropped.append(repo_path)
            # Recorded as checked out: the paths join the sub_paths, so the plan merges them, the moved
            # record holds the ref's version of each (an add of the branch shows as added) and a later
            # add_paths grow finds them held.
            record = record.carried(
                files=record.files, sub_paths=(*record.sub_paths, *(p for p in extra if not record.covers(p)))
            )
        fetched = await _archive_tree(
            record.provider, record.repository, target, scope, operation="checkout_rebase", extra_files=extra
        )
        if isinstance(fetched, ToolResult):
            return fetched
        tree, tool_name = fetched
        checked_out = record
        if tree.sub_paths and tuple(tree.sub_paths) != tuple(scope):
            # The ref's checks.toml widened the scope: the files newly
            # in it are taken from the ref, and the record keeps the wider
            # sub_paths, so status, diff and commits cover them.
            widened = tuple(tree.sub_paths)
            kept = tuple(p for p in extra if not any(is_under(p, s) for s in widened))
            record = dataclasses.replace(record, sub_paths=(*widened, *kept))
        theirs = dict(tree.files)
        # The base of the three-way merge is the merge base of the recorded
        # commit and the ref, as git rebase uses it: a checkout on a
        # feature branch records the branch's own files, so taking the record
        # as the base read every file the branch added as "deleted by the
        # ref" and removed it. When the merge base is the recorded commit (the
        # checkout is behind the ref) the record is the base, as before.
        base_files: dict[str, bytes] | None = None
        base_note = ""
        theirs_commit = _archive_commit(tree.root)
        if merge_base and not _same_commit(merge_base, record.commit):
            if theirs_commit and _same_commit(merge_base, theirs_commit):
                base_files = theirs
            else:
                at_base = await _archive_tree(
                    record.provider,
                    record.repository,
                    merge_base,
                    scope,
                    operation="checkout_rebase",
                    extra_files=extra,
                )
                if isinstance(at_base, ToolResult):
                    return at_base
                base_files = dict(at_base[0].files)
            base_note = f"Merged from the merge base {merge_base[:12]} of {record.commit[:12]} and {target}."
        elif not merge_base and record.commit:
            base_note = f"Merged from the files as checked out: the merge base was not read ({why})."
        if base_files is not None:
            record_for_plan = dataclasses.replace(
                record, files=rebasing.base_files_of(record, base_files, checked_out=checked_out)
            )
        else:
            record_for_plan = record
        ours: dict[str, str] = {}
        for workspace_path, entry in (await session.entries(refresh=True)).items():
            repo_path = record.repo_path(str(workspace_path))
            if repo_path is None or repo_path == RECORD_NAME:
                continue
            try:
                repo_path = normalize(repo_path)
            except WorkspacePathError:
                continue
            if record.tracks(repo_path) or repo_path in theirs:
                ours[repo_path] = entry.sha256
        for repo_path, data in pulled.items():
            ours[repo_path] = hashlib.sha256(data).hexdigest()
        for repo_path in dropped:
            ours.pop(repo_path, None)
        # A record a from_checkout commit moved (#1029) keeps the checkout-time hash of every file the
        # commit did not write, so the workspace holds them older than the branch commit it names when
        # the branch gained commits the checkout never held (main merged in on the server). The branch
        # side of each such file is read at that commit, else a file only the ref changed was kept over
        # it and a file both changed lost what the branch had gained, reported as a clean merge.
        refreshed: list[str] = []
        if record.base_commit and record.commit and not _same_commit(record.base_commit, record.commit):
            at_branch = await _archive_tree(
                record.provider, record.repository, record.commit, scope, operation="checkout_rebase"
            )
            if isinstance(at_branch, ToolResult):
                return at_branch
            stale, gone = rebasing.stale_branch_sides(
                {p: f for p, f in checked_out.files.items() if p not in pulled},
                ours,
                dict(at_branch[0].files),
                in_scope=checked_out.covers,
            )
            for repo_path, data in stale.items():
                pulled[repo_path] = data
                ours[repo_path] = hashlib.sha256(data).hexdigest()
            for repo_path in gone:
                ours.pop(repo_path, None)
                removals.append(record.workspace_path(repo_path))
            refreshed = sorted([*stale, *gone])
            if refreshed and base_files is None and merge_base and _same_commit(merge_base, record.commit):
                # The branch commit is the merge base itself: its tree, not the older record, is the base.
                base_files = dict(at_branch[0].files)
                record_for_plan = dataclasses.replace(
                    record, files=rebasing.base_files_of(record, base_files, checked_out=checked_out)
                )
        steps = rebasing.plan(record_for_plan, ours, theirs, in_scope=record.covers, checked_out=record.files)
        summary = rebasing.RebaseSummary(pulled=sorted(extra), refreshed=refreshed)
        # The branch's files brought in are written as they are unless a step below replaces them.
        writes: dict[str, bytes] = dict(pulled)
        removals.extend(record.workspace_path(p) for p in dropped)
        gate = asyncio.Semaphore(6)

        async def merged(step: rebasing.FileStep) -> rebasing.FileMerge | tuple[None, int, str]:
            """The merge of a MERGE step, or ``(None, 1, why)`` when its sides could not be read."""
            if step.path in pulled:
                mine = pulled[step.path]
            else:
                try:
                    _entry, mine = await session.get_bytes(record.workspace_path(step.path))
                except (FileNotFoundError, WorkspacePathError, WorkspaceUnavailableError) as exc:
                    return None, 1, f"unreadable in the workspace: {exc}"
            base: bytes | None = None
            if base_files is not None:
                base = base_files.get(step.path)
            elif step.path in record.files:
                async with gate:
                    got = await _base_bytes(record, step.path)
                if isinstance(got, str):
                    return None, 1, f"base not read: {got}; the workspace version is kept"
                if hashlib.sha256(got).hexdigest() != record.files[step.path].sha256:
                    return None, 1, "the base read does not match the checkout record; the workspace version is kept"
                base = got
            return rebasing.merge_file(base, mine, theirs[step.path], on_conflict=mode, path=step.path)

        merges = [s for s in steps if s.step is rebasing.Step.MERGE]
        outcomes = dict(zip((s.path for s in merges), await asyncio.gather(*(merged(s) for s in merges)), strict=True))
        for step in steps:
            if step.step is rebasing.Step.TAKE:
                writes[step.path] = theirs[step.path]
                summary.updated.append(step.path)
            elif step.step is rebasing.Step.REMOVE:
                writes.pop(step.path, None)
                if step.path not in absent:
                    removals.append(record.workspace_path(step.path))
                summary.removed.append(step.path)
            elif step.step is rebasing.Step.KEEP:
                if step.path not in record_for_plan.files and step.path not in theirs:
                    summary.added.append(step.path)
                else:
                    summary.kept.append(step.path)
            elif step.step is rebasing.Step.CONFLICT:
                summary.conflicts.append((step.path, f"{step.reason}: the workspace version is kept"))
            else:
                outcome = outcomes[step.path]
                if isinstance(outcome, tuple):
                    summary.conflicts.append((step.path, outcome[2]))
                    continue
                if outcome.reason:
                    summary.conflicts.append((step.path, outcome.reason))
                    continue
                writes[step.path] = outcome.data
                if outcome.conflicts:
                    why = f"{outcome.conflicts} hunk(s) between conflict markers"
                    summary.conflicts.append((step.path, f"{why}: {outcome.detail}" if outcome.detail else why))
                else:
                    summary.merged.append(step.path)
                    if outcome.validated:
                        summary.validated.append((step.path, outcome.validated))
        moved = rebasing.rebased_record(
            record, ref=target, commit=_archive_commit(tree.root), theirs=theirs, in_scope=record.covers
        )
        # The writes, the removals and the moved record in one change set: the
        # quota is checked after the removals, and a refusal or a failure
        # leaves every file and the old record as they were.
        rebase_writes = [*writes.items(), (RECORD_NAME, moved.to_json())]
        evicted: list[eviction.Footprint] = []
        try:
            await session.put_tree(rebase_writes, under=record.path, remove=removals, operation="checkout_rebase")
        except QuotaExceededError as exc:
            # Over the file limit: as materialize_repository does, the clean
            # checkouts of a gone ref, an ended run or past the idle TTL go in
            # the same change set; a checkout with a local change never does.
            async def _write_without(remove: list[str]) -> Any:
                return await session.put_tree(
                    rebase_writes, under=record.path, remove=[*removals, *remove], operation="checkout_rebase"
                )

            retried = await _evict_stale_checkouts(
                record.path, _write_without, f"{display(record.path)}/ was not rebased: {exc}."
            )
            if isinstance(retried, ToolResult):
                metrics.operation("checkout_rebase", "refused")
                return retried
            _plan, evicted = retried
        except (WorkspaceUnavailableError, WorkspacePathError) as exc:
            metrics.operation("checkout_rebase", "refused")
            return _error(f"{display(record.path)}/ was not rebased: {exc}.")
        metrics.operation("checkout_rebase", "conflict" if summary.conflicts else "ok")
        await audit.record(
            "checkout_rebase",
            user_id=owner,
            workspace_id=session.workspace_id,
            summary=f"Rebased the checkout at {display(record.path)} onto {record.repository}@{target}",
            details={
                "repository": record.repository,
                "from": record.commit or record.ref,
                "ref": target,
                "commit": moved.commit,
                "updated": len(summary.updated),
                "removed": len(summary.removed),
                "added_kept": len(summary.added),
                "merge_base": merge_base,
                "merged": len(summary.merged),
                "conflicts": len(summary.conflicts),
                "pulled": len(summary.pulled),
                "tool": tool_name,
            },
        )
        answer = summary.describe(
            shown_as=display(record.path), before=checkouts.origin(record), after=checkouts.origin(moved)
        )
        if tree.widened:
            answer += "".join(
                f"{w.sub_path} widened to {w.directory}/ ({w.words()} at the ref).\n" for w in tree.widened
            )
        if base_note:
            answer += base_note + "\n"
        if evicted:
            answer += eviction.evicted_line(evicted, pruned=False) + "\n"
        return answer

    async def sandbox_run_command(
        checkout: str,
        check: str = "",
        args: list[str] | None = None,
        argv: list[str] | None = None,
        cwd: str = "",
        run_id: str = "",
        affected: bool = False,
        background: bool = False,
        stop: bool = False,
        service_id: str = "",
        env: dict[str, str] | None = None,
    ) -> str | ToolResult:
        from app.core.config import settings

        if service_id:
            return await _run_in_lab(
                checkout, check, args, argv, cwd, service_id, run_id, affected, background, stop, env
            )
        if stop and not run_id:
            metrics.operation("run_command", "refused")
            return _error("stop=true ends a running command: give its run_id (the one a running answer named).")
        if run_id:
            outcome = await commands.poll_run(
                str(run_id).strip(), workspace=session, user_id=owner, db=db, stop=bool(stop)
            )
            return await _command_answer(outcome, checkout)
        if affected and (argv or not check):
            metrics.operation("run_command", "refused")
            return _error(
                "affected=true selects the test files of a check: name the check (e.g. a unit test check), not argv."
            )
        # Before the checks are read: an edit of this round may be the checks file itself.
        settle_note = await _settle()
        if not check and not argv:
            # The listing the description promises: a normal answer, never an "Error:" (a friction).
            try:
                listing = await commands.list_checks(
                    session, checkout=checkout, known_images=set(settings.opensandbox_images)
                )
            except ChecksError as exc:
                metrics.operation("run_command", "refused")
                return _error(str(exc))
            except WorkspaceUnavailableError as exc:
                return _error(str(exc))
            metrics.operation("run_command", "ok")
            return f"{settle_note}\n\n{listing}" if settle_note else listing
        try:
            command = await commands.resolve(
                session,
                checkout=checkout,
                check=check,
                args=args,
                argv=argv,
                cwd=cwd,
                allow_argv=bool(settings.code_interpreter_command_allow_argv),
                max_timeout=max(1, int(settings.code_interpreter_command_max_seconds)),
                known_images=set(settings.opensandbox_images),
                env=env,
            )
        except ChecksError as exc:
            metrics.operation("run_command", "refused")
            return _error(str(exc))
        except WorkspaceUnavailableError as exc:
            return _error(str(exc))
        selection_text = ""
        # A formatter declared affected_by_default (backend-format) called without args runs on the change, never
        # the whole tree: a checkout has no .git, so the script alone cannot tell what changed.
        # A fixer declared affected_narrows_args (backend-format-fix) named a file runs on the change's lines of
        # it, never the whole file: a file main never formatted would otherwise come back rewritten throughout.
        if affected or command.affected_by_default or command.narrow_args:
            got = await _affected_command(command, checkout)
            if isinstance(got, ToolResult):
                return got
            if isinstance(got, str):
                metrics.operation("run_command", "ok")
                return got
            command, selection_text = got
        # The call's job (sandbox_workspace.jobs): the run it starts is recorded under its arguments, so a
        # resumed turn's repeat of this call reads that run's saved result instead of running the check again.
        call_arguments = {
            "checkout": checkout,
            "check": check,
            "args": args,
            "argv": argv,
            "cwd": cwd,
            "affected": affected,
        }
        with jobs.bound(call_arguments):
            outcome = await commands.run_with_fallback(
                command,
                workspace=session,
                user_id=owner,
                db=db,
                background=bool(background),
            )
        answer = await _command_answer(outcome, checkout, command)
        if selection_text and isinstance(answer, str):
            answer = f"{selection_text}\n\n{answer}"
        if command.env_unset and isinstance(answer, str):
            answer += "\n" + commands.unset_env_note(command.env_unset)
        if settle_note and isinstance(answer, str):
            answer = f"{settle_note}\n\n{answer}"
        return answer

    async def _run_in_lab(
        checkout: str,
        check: str,
        args: list[str] | None,
        argv: list[str] | None,
        cwd: str,
        service_id: str,
        run_id: str,
        affected: bool,
        background: bool,
        stop: bool,
        env: dict[str, str] | None = None,
    ) -> str | ToolResult:
        """A check run inside the running lab *service_id* (its workspace container), where its services answer."""
        from app.core.config import settings

        if run_id or affected or background or stop:
            metrics.operation("run_command", "refused")
            return _error(
                "service_id runs one check in a lab and answers when it ends: run_id, stop, background and "
                "affected do not apply to it."
            )
        if not labs.labs_enabled() or await labs.find(session, str(service_id).strip()) is None:
            metrics.operation("run_command", "refused")
            return _error(
                f"no lab {service_id!r} is running for this workspace: service_id takes the id "
                "sandbox_start_service answered for an agentic lab (sandbox_service_status reads it)."
            )
        if not check and not argv:
            metrics.operation("run_command", "refused")
            return _error("with service_id, check names the check to run in the lab (sandbox_run_command without a check lists them).")
        try:
            command = await commands.resolve(
                session,
                checkout=checkout,
                check=check,
                args=args,
                argv=argv,
                cwd=cwd,
                allow_argv=bool(settings.code_interpreter_command_allow_argv),
                max_timeout=max(1, int(settings.code_interpreter_command_max_seconds)),
                known_images=set(settings.opensandbox_images),
                env=env,
            )
        except ChecksError as exc:
            metrics.operation("run_command", "refused")
            return _error(str(exc))
        except WorkspaceUnavailableError as exc:
            return _error(str(exc))
        try:
            root = normalize(checkout or "", allow_root=True)
        except WorkspacePathError as exc:
            return _error(str(exc))
        outcome = await labs.run_check_in_lab(
            session,
            str(service_id).strip(),
            checkout=root,
            label=command.label,
            argv=command.argv,
            cwd=command.cwd,
            env=command.env,
            timeout=float(command.timeout),
        )
        metrics.operation("run_command", "refused" if outcome.refused else "ok")
        if outcome.refused:
            return _error(outcome.text.removeprefix("Error: "))
        return outcome.text

    async def _settle() -> str:
        """Wait for the workspace writes of this tool round; a line for the answer when it waited."""
        waited, left = await session.settled()
        if left:
            return (
                f"Ran after waiting {waited:.0f} s for {left} workspace write(s) of this round still in flight: "
                "the command may not see them; run it again once they answered."
            )
        if waited >= 0.1:
            return f"Ran after the workspace write(s) of this round committed ({waited:.1f} s)."
        return ""

    async def _affected_command(
        command: commands.ResolvedCommand, checkout: str
    ) -> tuple[commands.ResolvedCommand, str] | str | ToolResult:
        """*command* with the test files the checkout's changes affect appended; a sentence when none is."""
        try:
            record, changes = await WorkspaceIngestor(session).checkout_changes(checkout, [])
        except ValueError as exc:
            metrics.operation("run_command", "refused")
            return _error(f"affected=true reads the checkout record: {exc}")
        # A files or lines check counts the commits made from the checkout too: their files are the branch's own.
        on_branch = command.affected in ("files", "lines")
        unchanged = changes.empty and not (on_branch and record.branch_paths())
        if unchanged and command.narrow_args:
            # Nothing changed: the args name files outside any change, run as given (the call asked for them).
            return command, f"{display(record.path)}/ has no change since its checkout: the args run as given."
        if unchanged:
            return (
                f"{display(record.path)}/ has no change since its checkout or the last commit made from it "
                f"({checkouts.origin(record)}): "
                + (
                    "no changed file, nothing ran (this check runs on the change when called without args"
                    + (f"; args [{WHOLE_ARG!r}] runs the whole suite, bounded" if command.affected == "tests" else "")
                    + "). "
                    + affecting.COMMITTED_LEFT_OUT
                    if command.affected_by_default
                    else "no test is affected, nothing ran. Run the check without affected to run it whole."
                )
                + _untracked_line(changes, record)
            )

        async def read_base(repo_path: str) -> bytes | str:
            return await _base_bytes(record, repo_path)

        if on_branch:
            # A formatter or a linter on the change's own files (and lines): never the repository's drift around them.
            # Read against the commit the checkout was made at, so lines a pushed commit wrote stay the branch's own.
            async def read_baseline(repo_path: str) -> bytes | str:
                return await _base_bytes(record, repo_path, at=record.baseline)

            files = await affecting.changed_files(
                session,
                record,
                changes,
                cwd=command.cwd,
                read_base=read_baseline,
                globs=command.affected_globs,
                lines=command.affected == "lines",
                whole_repository=command.affected_whole_repository,
            )
            described = files.describe(mode=command.affected, baseline=checkouts.baseline_origin(record))
            if command.narrow_args:
                given = command.narrow_args
                narrowed, notes = affecting.narrow_args(given, files)
                head = command.argv[: len(command.argv) - len(given)]
                text = "\n".join(
                    [
                        f"Args narrowed to the changed {command.affected} against {checkouts.baseline_origin(record)} "
                        "(pass --whole to run on them as given):"
                    ]
                    + [f"- {a}" for a in narrowed[:25]]
                    + notes
                )
                if not any(not a.startswith("-") for a in narrowed):
                    return text + "\nNo changed line in the args: nothing ran."
                room = max(0, MAX_ARGS - len(head))
                return dataclasses.replace(command, argv=(*head, *narrowed[:room]), default_args=()), text
            if not files.args:
                return described + "\nNo changed file for this check: nothing ran. " + affecting.COMMITTED_LEFT_OUT
            plain = command.without_defaults()
            room = max(0, MAX_ARGS - len(plain))
            picked = files.args[:room]
            if len(files.args) > len(picked):
                described += (
                    f"\nOnly the first {len(picked)} file(s) run (argv bound {MAX_ARGS}); run the rest with args."
                )
            return dataclasses.replace(command, argv=(*plain, *picked), default_args=()), described + _untracked_line(
                changes, record
            )

        # The selection shares the call with the command: it stops reading in time for the command to answer.
        selection = await affecting.affected_tests(
            session,
            record,
            changes,
            cwd=command.cwd,
            read_base=read_base,
            deadline=commands.selection_deadline(),
            always=command.affected_always,
            scans=command.affected_scan_patterns,
            # A file marked `integration` under backend-unit's -m "not integration" is collected and runs nothing.
            excluded=affecting.excluded_markers(command.without_defaults()),
        )
        if selection.deselected:
            # Name the check that runs a deselected marker (integration -> backend-integration), not "the check".
            from app.core.config import settings

            try:
                declared = await commands.checkout_checks(session, record.path, images=set(settings.opensandbox_images))
            except ChecksError:
                declared = None  # the generic advice stays: the checks file was read once already
            if declared is not None:
                selection.runners = affecting.marker_runners(
                    (
                        (spec.name, spec.argv, "/".join(p for p in (record.path, spec.cwd.strip("/")) if p))
                        for spec in declared.specs.values()
                    ),
                    command.cwd,
                    {m for found in selection.deselected.values() for m in found},
                )
        if not selection.tests:
            whole = (
                f"args [{WHOLE_ARG!r}] (the whole suite, bounded)"
                if command.affected_by_default
                else "the check without affected"
            )
            return (
                selection.describe()
                + f"\nNo test file names what changed: nothing ran. Run {whole}, or name test files in args."
            )
        # One test file pytest cannot collect (a fixture outside a narrow checkout) must not stop the rest.
        # The selected files replace the check's default target (a linter's src/), as args would.
        # A selected file reading a tree the narrow checkout lacks fails on a missing file: it is counted, never run.
        runnable = selection.runnable()
        if not runnable:
            return (
                selection.describe()
                + f"\nEvery selected test file reads a directory {display(record.path)}/ does not hold: nothing ran. "
                "Grow the checkout with the add_paths above, or name test files in args."
            )
        base = command.without_defaults()
        keep_going = commands.continue_on_collection_errors(base)
        room = max(0, MAX_ARGS - len(base) - len(keep_going))
        chosen = runnable[:room]
        text = selection.describe() + _untracked_line(changes, record)
        if len(runnable) > len(chosen):
            text += (
                f"\nOnly the first {len(chosen)} test file(s) run (argv bound {MAX_ARGS}); run the rest with args, "
                "or the check without affected."
            )
        return dataclasses.replace(command, argv=(*base, *keep_going, *chosen), default_args=()), text

    async def _command_answer(
        outcome: commands.CommandOutcome, checkout: str, command: commands.ResolvedCommand | None = None
    ) -> str | ToolResult:
        """The tool's answer to a run or a poll: an error, ``running`` with its run_id, or the result (audited).

        A pre-push guard's whole run is kept for ``create_github_commit(from_checkout=...)`` (``prepush.py``).
        """
        if outcome.refused:
            metrics.operation("run_command", "refused")
            return _error(outcome.text.removeprefix("Error: "))
        if outcome.busy:
            # Another command holds the container: a state the answer names, never an Error (a friction).
            metrics.operation("run_command", "busy")
            return outcome.text
        if outcome.lost:
            # A run whose container went with a restart: a state the platform created, never an Error (a friction).
            metrics.operation("run_command", "lost")
            return outcome.text
        if outcome.starting:
            metrics.operation("run_command", "starting")
            return outcome.text
        if outcome.running:
            metrics.operation("run_command", "running")
            return (
                outcome.text
                + f'\nCall sandbox_run_command again with checkout="{checkout}" and run_id="{outcome.run_id}" '
                "to wait for its end (exit code, output, report, workspace changes), or with stop=true as well to "
                "end it now with the output so far."
            )
        metrics.operation("run_command", "stopped" if outcome.stopped else ("failed" if outcome.timed_out else "ok"))
        if outcome.command is not None:
            await commands.audit_run(
                user_id=owner, workspace_id=session.workspace_id, command=outcome.command, outcome=outcome
            )
        ran = outcome.command or command
        if ran is not None and not outcome.stopped and not outcome.timed_out:
            await prepush.record_run(session, checkout, check=ran.check, argv=ran.argv, exit_code=outcome.exit_code)
        return outcome.text + await _outside_checkout_note(outcome, checkout, ran)

    async def _outside_checkout_note(
        outcome: commands.CommandOutcome, checkout: str, ran: commands.ResolvedCommand | None = None
    ) -> str:
        """Lines naming the manifests an install rewrote and what a run did outside the sub_paths; "" if none."""
        if not (outcome.missing or outcome.written or outcome.uncollected):
            return ""
        try:
            record, _changes = await WorkspaceIngestor(session).checkout_changes(checkout, [])
        except ValueError:
            return ""
        notes: list[str] = []
        tracked = [p for p in map(record.repo_path, outcome.rewritten) if p and record.tracks(p)]
        manifests = commands.rewritten_manifests(ran, tracked)
        if manifests:
            notes.append(
                f"The install rewrote {len(manifests)} tracked manifest(s) of the checkout: "
                + ", ".join(manifests[:5])
                + (", ..." if len(manifests) > 5 else "")
                + ". create_github_commit(from_checkout=...) would commit them as a change of the branch: "
                "restore them, or pass paths naming only your changes."
            )
        if not record.sub_paths:
            return "".join(f"\n{note}" for note in notes)
        kept = ", ".join(record.sub_paths)
        missing = record.outside(outcome.missing)
        if missing:
            notes.append(
                f"Not runnable in this checkout: the run looked for {len(missing)} file(s) outside its sub_paths ({kept}): "
                + ", ".join(missing[:5])
                + (", ..." if len(missing) > 5 else "")
                + ". The tests failing on them are environmental, not a regression: grow the checkout with "
                "materialize_repository add_paths to run them, or leave them to the pull request's CI."
            )
            verdict = commands.missing_file_verdict(outcome, missing)
            if verdict:
                notes.append(verdict)
            if outcome.uncollected:
                notes.append(commands.uncollected_note(outcome))
        stray = record.outside(outcome.written)
        if stray:
            notes.append(
                f"Stray writes: the run wrote {len(stray)} file(s) outside the checkout's sub_paths ({kept}): "
                + ", ".join(stray[:5])
                + (", ..." if len(stray) > 5 else "")
                + ". They are not changes of the checkout (status and from_checkout skip them); remove them with "
                "sandbox_remove_files."
            )
        return "".join(f"\n{note}" for note in notes)

    async def sandbox_start_service(
        checkout: str, service: str = "", background: bool = False, sync: bool = False, restart: str = ""
    ) -> str | ToolResult:
        from app.core.config import settings

        settle_note = await _settle()
        if not service:
            # No service named: the declared services, a normal answer (never an error to probe for).
            try:
                listing = await services.list_declared(session, checkout=checkout)
            except ChecksError as exc:
                return _error(str(exc))
            except WorkspaceUnavailableError as exc:
                return _error(str(exc))
            return f"{settle_note}\n\n{listing}" if settle_note else listing
        try:
            resolved = await services.resolve(
                session,
                checkout=checkout,
                service=service,
                known_images=set(settings.opensandbox_images),
                # A lab runs each service in its own container: a dependency may run on a product image.
                lab_images=set(settings.agentic_labs_images) if labs.labs_enabled() else frozenset(),
            )
        except ChecksError as exc:
            metrics.operation("start_service", "refused")
            return _error(str(exc))
        except WorkspaceUnavailableError as exc:
            return _error(str(exc))
        if restart and not sync:
            return _error("restart goes with sync=true: the edits are copied, then that service is run again.")
        if sync:
            if not labs.labs_enabled():
                return _error(
                    "sync applies to an agentic lab only: stop the service and start it again to copy the edits."
                )
            outcome = await _lab_call(labs.sync_lab(resolved, workspace=session, restart=(restart or "").strip()))
            metrics.operation("sync_service", "refused" if outcome.refused else "ok")
            if outcome.refused:
                return _error(outcome.text.removeprefix("Error: "))
            return f"{settle_note}\n\n{outcome.text}" if settle_note else outcome.text
        if labs.labs_enabled():
            # An agentic coding lab: a pod of its own, no quota checked or counted (labs.py).
            outcome = await labs.start_lab(resolved, workspace=session, background=bool(background))
        else:
            outcome = await services.start_service(resolved, workspace=session, user_id=owner, db=db)
        metrics.operation(
            "start_service",
            "refused" if outcome.refused else ("ok" if outcome.ready else ("starting" if outcome.starting else "failed")),
        )
        if outcome.refused and not outcome.service_id:
            return _error(outcome.text.removeprefix("Error: "))
        await services.audit_service(
            "started", user_id=owner, workspace_id=session.workspace_id, outcome=outcome, name=resolved.key
        )
        return f"{settle_note}\n\n{outcome.text}" if settle_note else outcome.text

    async def _lab_call(call: Any) -> services.ServiceOutcome:
        try:
            return await call
        except KubeError as exc:
            return services.ServiceOutcome(text=f"Error: the lab could not be reached: {exc}", refused=True)

    async def sandbox_service_status(service_id: str, env: list[str] | None = None) -> str | ToolResult:
        if await labs.find(session, service_id) is not None:
            outcome = await _lab_call(labs.lab_status(session, service_id, env=list(env or [])))
        else:
            outcome = await services.service_status(session, service_id, user_id=owner)
            if env and outcome.service_id:
                outcome.text += "\nenv: answered for a lab only."
        return _error(outcome.text.removeprefix("Error: ")) if outcome.refused else outcome.text

    async def sandbox_service_logs(
        service_id: str,
        service: str = "",
        lines: int = services.DEFAULT_LOG_LINES,
        pattern: str = "",
        ignore_case: bool = False,
    ) -> str | ToolResult:
        if await labs.find(session, service_id) is not None:
            outcome = await _lab_call(
                labs.lab_logs(
                    session, service_id, service=service, lines=lines, pattern=pattern, ignore_case=ignore_case
                )
            )
        else:
            outcome = await services.service_logs(
                session,
                service_id,
                service=service,
                lines=lines,
                pattern=pattern,
                ignore_case=ignore_case,
                user_id=owner,
            )
        return _error(outcome.text.removeprefix("Error: ")) if outcome.refused else outcome.text

    async def sandbox_stop_service(service_id: str) -> str | ToolResult:
        if await labs.find(session, service_id) is not None:
            outcome = await _lab_call(labs.stop_lab(session, service_id))
        else:
            outcome = await services.stop_service(session, service_id, user_id=owner)
        if outcome.refused:
            return _error(outcome.text.removeprefix("Error: "))
        await services.audit_service("stopped", user_id=owner, workspace_id=session.workspace_id, outcome=outcome)
        return outcome.text

    for name, handler in (
        ("sandbox_put_file", sandbox_put_file),
        ("sandbox_get_file", sandbox_get_file),
        ("sandbox_list_files", sandbox_list_files),
        ("sandbox_remove_files", sandbox_remove_files),
        ("sandbox_move_files", sandbox_move_files),
        ("sandbox_read_file", sandbox_read_file),
        ("sandbox_grep", sandbox_grep),
        ("sandbox_outline", sandbox_outline),
        ("sandbox_read_symbol", sandbox_read_symbol),
        ("sandbox_playwright_trace", sandbox_playwright_trace),
        ("sandbox_edit_file", sandbox_edit_file),
        ("sandbox_replace_in_files", sandbox_replace_in_files),
        ("sandbox_codemod", sandbox_codemod),
        ("sandbox_apply_patch", sandbox_apply_patch),
        ("sandbox_write_file", sandbox_write_file),
        ("sandbox_json_keys", sandbox_json_keys),
        ("sandbox_convert_images", sandbox_convert_images),
        ("materialize_repository", materialize_repository),
        ("sandbox_checkout_status", sandbox_checkout_status),
        ("sandbox_checkout_diff", sandbox_checkout_diff),
        ("sandbox_checkout_rebase", sandbox_checkout_rebase),
        ("sandbox_diff_files", sandbox_diff_files),
        ("sandbox_run_command", sandbox_run_command),
        ("sandbox_start_service", sandbox_start_service),
        ("sandbox_service_status", sandbox_service_status),
        ("sandbox_service_logs", sandbox_service_logs),
        ("sandbox_stop_service", sandbox_stop_service),
    ):
        # sandbox_checkout_rebase takes the write turn itself: a background job and its poll must not share it.
        wrapped = name in WORKSPACE_WRITE_TOOLS and name != "sandbox_checkout_rebase"
        executor.register(name, _counted_write(session, handler) if wrapped else handler)

    def _run_command_waits_only(arguments: Mapping[str, Any]) -> bool:
        return bool(arguments.get("run_id")) and not arguments.get("stop")

    async def _run_command_landed(arguments: Mapping[str, Any]) -> str | bool | None:
        return await jobs.landed(session, arguments)

    async def _rebase_landed(arguments: Mapping[str, Any]) -> str | bool | None:
        return await jobs.rebase_landed(session, arguments)

    async def _edit_landed(arguments: Mapping[str, Any]) -> str | bool | None:
        return await landing.edit_probe(session, arguments)

    async def _replace_landed(arguments: Mapping[str, Any]) -> str | bool | None:
        return await landing.replace_probe(session, arguments)

    source = "builtin:code_interpreter"
    run_command_definition = ToolDefinition(
        name="sandbox_run_command",
        description=(
            "Run one of a checkout's checks (install, lint, types, unit tests, build: one argv each, no shell) in a "
            "sandbox container of the check's image, with the "
            "workspace mounted and what the command wrote under it saved back. The result gives the exit code, "
            "the duration, a JUnit or SARIF report summarised into findings (path, line, rule, message) when the "
            "check declares one, a bounded head and tail of stdout and stderr, and the run directory "
            "(.xtm/runs/RUN_ID/: whole output, run.json). Any repository works as it is: the checks are detected "
            "from its own CI workflows, manifests and Makefile, or read from .xtm-one/checks.toml when it has one; "
            "call it without check to list them (a normal answer, the declared services with them). The container has the sandbox network policy and no credential. "
            "A command longer than one call (npm ci, pip install, a full test suite) keeps running in its container: "
            "the call answers running with a run_id, and a call with that run_id waits for its end. "
            "background=true answers running at once (first lines and run_id) so the turn goes on while it runs; "
            "run_id with stop=true ends a running command with every process it started and answers the output so far. "
            "Prefer it to any pure-Python approximation of a test or lint run."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "checkout": {
                    "type": "string",
                    "description": "The checkout's directory in the workspace (the repository root), e.g. main.",
                },
                "check": {"type": "string", "description": "The check to run, by name; omit it to list the checks."},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments appended to the check's argv (a test file, a directory, a flag).",
                },
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "A command of your own instead of a declared check; only when the platform allows it.",
                },
                "cwd": {
                    "type": "string",
                    "description": "With argv or script: working directory, relative to the checkout.",
                },
                "run_id": {
                    "type": "string",
                    "description": (
                        "The run_id a previous call answered with status running (a long install or test run): "
                        "waits for that run's end instead of starting a command; check, args and argv are ignored."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "true: start the command and answer running within about a second, with its run_id and "
                        "first lines, instead of waiting for it; follow it later with run_id (wait) or run_id and "
                        "stop=true (end it). One command runs at a time per image container."
                    ),
                },
                "stop": {
                    "type": "boolean",
                    "description": (
                        "With run_id: end that running command now, with every process it started; the answer is "
                        "its result as stopped, with the output so far and what it wrote under the workspace."
                    ),
                },
                "affected": {
                    "type": "boolean",
                    "description": (
                        "true: run the check on the test files the checkout's changes affect, appended to its argv: "
                        "the definitions, string keys and module paths the changed lines touch (against the files as "
                        "checked out), searched word for word in the test files under the check's directory, plus the "
                        "changed test files. The answer names what was selected and why; nothing runs when no test "
                        "names a change. A pytest check runs with --continue-on-collection-errors, so a test file a narrow "
                        "checkout cannot collect is named not runnable and the rest still runs. A check the listing marks "
                        "'affected=true: the changed files' or '... lines' (a formatter) gets the changed files instead, "
                        "or each modified file as PATH:A-B with its changed lines. Use it before a push "
                        "instead of picking test files by hand."
                    ),
                },
                "service_id": {
                    "type": "string",
                    "description": (
                        "An agentic lab sandbox_start_service started from this checkout: the check runs inside "
                        "that lab (its workspace container, the lab's copy of the checkout), where the lab's "
                        "services answer on 127.0.0.1, which a check container cannot reach. Answers when the "
                        "check ends (at most 270 s), with its exit code and output."
                    ),
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Variables for this run over the check's own, e.g. a placeholder for a CI secret.",
                },
            },
            "required": ["checkout"],
        },
        source=source,
        # A call with a run_id and no stop only waits for a command already running: a read a resumed turn may repeat.
        read_only_call=_run_command_waits_only,
        # A check run checks the tree the edit tools change: run, edit, run is never an identical repeat (#175).
        recheck_after_write=True,
        # A repeat of a call whose check ended before the worker restart is answered with its saved result.
        landed_probe=_run_command_landed,
    )
    service_id_property = {"type": "string", "description": "The service_id sandbox_start_service answered."}
    service_definitions = [
        ToolDefinition(
            name="sandbox_start_service",
            description=(
                "Start a service a checkout declares in .xtm-one/checks.toml ([service.NAME]: the product running from "
                "the checkout, e.g. a lab with its backend, frontend and data) in a sandbox container of its own, with "
                "the workspace copied in, the services it depends on started first and each one waited for on its port. "
                "Answers the service_id and a preview_url to open with the browser tool or web_fetch. The container has "
                "no credential and the sandbox network policy; it stops at sandbox_stop_service or at its declared "
                "lifetime after the last call on it. A service already running for the checkout is answered again. A lab "
                "whose readiness outlives the call (or background=true) is answered 'starting' with its service_id: "
                "follow it with sandbox_service_status (each service's state) and sandbox_service_logs; the same start "
                "again waits for that lab, never a second one. Without service it lists the declared services. "
                "sync=true on a running lab copies the checkout files written since its last copy into it; with "
                "restart=NAME that service's process alone is run again, its build caches kept."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "checkout": {
                        "type": "string",
                        "description": "The checkout's directory in the workspace (where .xtm-one/checks.toml is), e.g. main.",
                    },
                    "service": {
                        "type": "string",
                        "description": "The declared service to start, by name; empty to list the declared services.",
                    },
                    "background": {
                        "type": "boolean",
                        "description": (
                            "Lab only: answer 'starting' with the service_id once the pod runs and holds the checkout, "
                            "without waiting for readiness (default false: waits up to about 4 minutes)."
                        ),
                    },
                    "sync": {
                        "type": "boolean",
                        "description": (
                            "Lab only: copy the checkout files written since the lab's last copy into the running lab, "
                            "services left running (default false: start)."
                        ),
                    },
                    "restart": {"type": "string", "description": "With sync: a service of the lab to run again."},
                },
                "required": ["checkout"],
            },
            source=source,
            # A repeat adopts the service held for the checkout and name (ready: answered again; partial: replaced),
            # so a resumed turn runs it at once, even after two cuts: a 75 s lab start is what a rollout interrupts.
            replay_safe=True,
            cut_safe=True,
        ),
        ToolDefinition(
            name="sandbox_service_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description=(
                "Whether a started service still answers on its port, with its preview_url and start time; renews its lifetime. "
                "A stopped or unknown service_id answers 'not running', a normal result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "service_id": service_id_property,
                    "env": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Variable names: per lab container, set, set (empty) or missing; never a value.",
                    },
                },
                "required": ["service_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_service_logs",
            read_only=True,
            description=(
                "The last lines of a started service's output (and of the services it depends on, or one of them), "
                "bounded: read them when it did not become ready or a page shows an error. Pass pattern to keep only "
                "the lines matching a regular expression (e.g. 'seeded|not seeded', 'error|traceback'): the answer "
                "names how many lines matched out of how many were scanned, without the banners and build steps."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "service_id": service_id_property,
                    "service": {"type": "string", "description": "One of its services by name; empty for all of them."},
                    "lines": {
                        "type": "integer",
                        "description": (
                            f"Lines per service (default {services.DEFAULT_LOG_LINES}, at most {services.MAX_LOG_LINES}); "
                            "with pattern, the matching lines kept (the newest)."
                        ),
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Optional regular expression (Python re, searched in each line): only the matching lines "
                            f"of the newest {services.MAX_LOG_SCAN_LINES} are answered. An invalid one is refused with its error."
                        ),
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Match pattern case-insensitively (default false).",
                    },
                },
                "required": ["service_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_stop_service",
            description="Stop a started service and discard its container. Stop every service you started once you are done with it.",
            input_schema={
                "type": "object",
                "properties": {"service_id": service_id_property},
                "required": ["service_id"],
            },
            source=source,
        ),
    ]
    definitions = [
        ToolDefinition(
            name="sandbox_put_file",
            description=(
                "Copy a conversation file (an upload, a download, a generated file, a saved video frame, a template asset: its file_id) into the "
                "code interpreter's sandbox workspace at path (path src/app.py is workspace/src/app.py), where every later "
                "code_interpreter call finds it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "The file_id of a file of this conversation."},
                    "path": {"type": "string", "description": "Destination path in the workspace, e.g. src/app.py."},
                },
                "required": ["file_id", "path"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_get_file",
            description=(
                "Turn a file of the sandbox workspace into a conversation file the user sees as a download card: "
                "the result gives its file_id and its [[FILE:id]] marker. To look at a file, use sandbox_read_file, "
                "sandbox_grep or sandbox_outline instead; to commit or upload a workspace file without a download, "
                "pass workspace_path to the tool instead (create_github_commit files[].workspace_path, or "
                "from_checkout for a whole checkout; render_html_to_image workspace_path for an HTML mockup)."
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path in the workspace."}},
                "required": ["path"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_list_files",
            description="List the sandbox workspace (or a directory of it): paths, sizes, modification times.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "A directory to list; empty for the whole workspace."},
                    "limit": {
                        "type": "integer",
                        "description": f"Files to list (1-{_LIST_LIMIT_MAX}); the rest are counted.",
                    },
                    "depth": {"type": "integer", "description": "Levels below path to list (1: its direct files)."},
                    "glob": {
                        "type": "string",
                        "description": "File name pattern, e.g. *.test.ts or {a,b}.md (with a /: path from path).",
                    },
                },
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_remove_files",
            description="Remove files or whole directories from the sandbox workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files or directories to remove.",
                    }
                },
                "required": ["paths"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_move_files",
            description=(
                "Move or rename files or whole directories of the sandbox workspace, every move in one write, the "
                "content kept as is (nothing re-uploaded). Use it, not code_interpreter, to rename a module or move a "
                "package. Refused with nothing written: a source absent, a destination inside its source, two moves "
                "onto one path, an existing destination unless overwrite (a file only). copy=true copies instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "moves": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                            "required": ["from", "to"],
                        },
                        "description": "Workspace paths, e.g. [{from: src/old.py, to: src/new.py}].",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Replace an existing destination file (default false).",
                    },
                    "copy": {
                        "type": "boolean",
                        "description": "Copy: keep every source where it is (default false: move). Server side, nothing re-uploaded.",
                    },
                },
                "required": ["moves"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_read_file",
            description=(
                "Read a window of a text file of the sandbox workspace with line numbers: max_lines lines from "
                "offset_line, the total line count and the next offset when more remains. Use it instead of printing a "
                "file from code_interpreter; a whole large file never belongs in the context (sandbox_outline for its "
                "definitions, sandbox_read_symbol for one of them whole, sandbox_grep to find the lines). A file of an "
                "installed tree (node_modules, .venv, site-packages) is read from the live command container. A line longer than "
                "max_line_chars is cut with the rest "
                "counted: raise max_line_chars to read a long line (a table row, a minified file) whole."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path in the workspace, e.g. src/app.py."},
                    "offset_line": {"type": "integer", "description": "First line to show (1-based; default 1)."},
                    "max_lines": {
                        "type": "integer",
                        "description": (
                            f"Lines to show (default {reading.DEFAULT_WINDOW_LINES}, cut at "
                            f"{reading.DEFAULT_WINDOW_CHARS:,} characters; given, at most {reading.MAX_WINDOW_LINES} "
                            f"lines and {reading.MAX_RESULT_CHARS:,} characters)."
                        ),
                    },
                    "max_line_chars": {
                        "type": "integer",
                        "description": (
                            f"Characters shown per line (default {reading.DEFAULT_LINE_CHARS}, at most {reading.MAX_LINE_CHARS})."
                        ),
                    },
                },
                "required": ["path"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_grep",
            read_only=True,
            description=(
                "Search the text files of the sandbox workspace with a regular expression (Python re syntax, one line "
                "at a time): path:line: text hits, bounded by max_matches with the rest counted, optional context "
                "lines. Installed packages and checkout records are skipped unless path_prefix names them, images "
                "and archives are skipped unread; a text file up to 32 MB (a job log) is searched. A search past "
                "its budget names the files it did not reach. The matches shown are capped at max_chars characters "
                "(the rest counted); on a log of JSON lines pass json_fields (or compact_json) to see only those keys, "
                "or group_by to count the matched lines per value of a key, per file side by side with their first "
                "and last times (a red job log against a green one in one call)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "The regular expression to search for."},
                    "path_prefix": {
                        "type": "string",
                        "description": "A directory or file of the workspace to search; empty for all of it.",
                    },
                    "max_matches": {
                        "type": "integer",
                        "description": f"Matches to return (default {reading.DEFAULT_MAX_MATCHES}, at most {reading.MAX_MATCHES}).",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": f"Lines of context around each match (0 to {reading.MAX_CONTEXT_LINES}).",
                    },
                    "include_globs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File name or path globs to restrict the search to, e.g. *.py or backend/**/*.md.",
                    },
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive match (default false)."},
                    "max_line_chars": {
                        "type": "integer",
                        "description": (
                            f"Characters shown per matched or context line (default {reading.DEFAULT_LINE_CHARS}, "
                            f"at most {reading.MAX_LINE_CHARS})."
                        ),
                    },
                    "json_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Keys to show of a matched line that holds a JSON object (a structured log line), their "
                            "values joined by ' | ', an absent key written '-', a dotted key (attributes.queue) read "
                            "nested; a line that is not JSON is shown whole."
                        ),
                    },
                    "group_by": {
                        "type": "string",
                        "description": (
                            "A key of the JSON object on each matched line (dotted for a nested key, e.g. "
                            "attributes.queue): instead of the lines, one row per value of it with, for each file "
                            "searched, the count of matched lines and the first and last line and time."
                        ),
                    },
                    "time_field": {
                        "type": "string",
                        "description": (
                            f"With group_by: the key the time of a line is read from (default "
                            f"{reading.DEFAULT_TIME_FIELD}); the runner prefix of the line when absent."
                        ),
                    },
                    "compact_json": {
                        "type": "boolean",
                        "description": "json_fields "
                        + ", ".join(reading.DEFAULT_JSON_FIELDS)
                        + " when json_fields is not given.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            f"Characters the matches shown hold (default {reading.DEFAULT_GREP_CHARS:,}, "
                            f"at most {reading.MAX_RESULT_CHARS:,}); the matches past it are counted."
                        ),
                    },
                },
                "required": ["pattern"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_outline",
            read_only=True,
            description=(
                "The definitions of a source file of the sandbox workspace with their line numbers: classes, "
                "functions and methods (Python through its syntax tree; declaration lines for "
                + ", ".join(e for e in reading.outline_kinds() if e not in ("py", "pyi"))
                + "; headings for Markdown). Read it before a window: it tells where to look."
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path in the workspace."}},
                "required": ["path"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_read_symbol",
            description=(
                "Read one definition of a source file of the sandbox workspace whole, by name: a class, a function, a "
                "method (Class.method) or a nested function, with its decorators and line numbers, in one call (Python "
                "through its syntax tree; other outlined languages run to the next definition at the same depth). The "
                "name is stable: after a context prune, the same call reads it again. Several matches are listed with "
                "their lines (pass the qualified name or occurrence); a definition longer than the result cap names "
                "the offset_line that continues in sandbox_read_file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path in the workspace, e.g. backend/app/main.py."},
                    "symbol": {
                        "type": "string",
                        "description": "The definition's name: Class, Class.method, function, or outer.inner for a nested function.",
                    },
                    "occurrence": {
                        "type": "integer",
                        "description": "Which match to read when several share the name (1-based; default 0: list them).",
                    },
                    "max_line_chars": {
                        "type": "integer",
                        "description": (
                            f"Characters shown per line (default {reading.DEFAULT_LINE_CHARS}, at most {reading.MAX_LINE_CHARS})."
                        ),
                    },
                },
                "required": ["path", "symbol"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_playwright_trace",
            description=(
                "Read a Playwright trace.zip of the workspace (a CI artifact from download_github_workflow_artifact) "
                "in one bounded answer: the failure with its locator and line, then inside a time window the "
                "actions with their call logs, the page URL per snapshot, console errors, the requests (status, method, "
                "path, duration; counts outside it) and the test attachments, JSON ones inlined. The window defaults "
                "to the failing action and window_ms before it. Use it instead of a code_interpreter script."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The trace.zip in the workspace."},
                    "around": {
                        "type": "string",
                        "description": "Centre the window on a call id of the action list, or on seconds from the trace start.",
                    },
                    "window_ms": {
                        "type": "integer",
                        "description": f"Milliseconds before the anchor (default {playwright_trace.DEFAULT_WINDOW_MS:,}).",
                    },
                    "frame_at": {
                        "type": "number",
                        "description": "Seconds from the trace start: the nearest screencast frame is written beside the zip.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": f"Characters of the answer (default {playwright_trace.DEFAULT_MAX_CHARS:,}, at most {playwright_trace.MAX_CHARS:,}).",
                    },
                },
                "required": ["path"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_edit_file",
            description=(
                "Change a text file of the sandbox workspace in place with exact find/replace edits, applied in order: "
                "every find must exist and be unique (or set replace_all); nothing is written unless every edit "
                "applies. Returns the unified diff. Use it, not a code_interpreter script, to change a source file; "
                "copy each find from sandbox_read_file exactly (whitespace included). For a long block, give "
                "start_line/end_line (as sandbox_read_file numbers them) with expect_first/expect_last (text of "
                "those lines, checked; *_blank for a blank line) instead of find; insert_after_line inserts; "
                "move_to_after_line moves that range (replace empty)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path in the workspace of the file to change."},
                    "edits": {
                        "type": "array",
                        "description": f"Ordered edits (1 to {editing.MAX_EDITS}).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {
                                    "type": "string",
                                    "description": "Exact text to replace (verbatim, whitespace included).",
                                },
                                "replace": {
                                    "type": "string",
                                    "description": "Replacement text; empty deletes the found text (or the line range).",
                                },
                                "replace_from": {
                                    "type": "string",
                                    "description": "Instead of replace (give it empty): a workspace file whose text is the replacement.",
                                },
                                "replace_all": {
                                    "type": "boolean",
                                    "description": "Replace every occurrence (default false: the snippet must be unique).",
                                },
                                "occurrence": {
                                    "type": "integer",
                                    "description": "Replace only this occurrence of find (1-based) when it is not unique.",
                                },
                                "start_line": {
                                    "type": "integer",
                                    "description": (
                                        "Instead of find: first line of the range to replace (1-based, in the file "
                                        "as the previous edits of this call left it)."
                                    ),
                                },
                                "end_line": {"type": "integer", "description": "Last line of the range (inclusive)."},
                                "expect_first": {
                                    "type": "string",
                                    "description": "Text the start_line holds (a part of it); a stale number is refused.",
                                },
                                "expect_last": {
                                    "type": "string",
                                    "description": "Text the end_line holds (a part of it), for a range of several lines.",
                                },
                                "expect_first_blank": {
                                    "type": "boolean",
                                    "description": "The anchor line is blank.",
                                },
                                "expect_last_blank": {
                                    "type": "boolean",
                                    "description": "The end_line is blank.",
                                },
                                "insert_after_line": {
                                    "type": "integer",
                                    "description": "Insert replace after this line (0: top); expect_first checks it.",
                                },
                                "move_to_after_line": {
                                    "type": "integer",
                                    "description": "Move start_line-end_line after this line (0: top; before the move).",
                                },
                                "expect_target": {
                                    "type": "string",
                                    "description": "Text the move_to_after_line holds.",
                                },
                                "expect_target_blank": {"type": "boolean", "description": "That line is blank."},
                            },
                            "required": ["replace"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
            source=source,
            # A resumed turn's repeat of an edit already in the file, reworded at the same anchor included,
            # is answered "already applied" instead of inserting it twice (#12097).
            landed_probe=_edit_landed,
            landed_key=landing.edit_key,
        ),
        ToolDefinition(
            name="sandbox_replace_in_files",
            description=(
                "Replace a regular expression (Python re, multiline) across the text files of a path_prefix and/or "
                "include_globs in ONE all-or-nothing write: a rename, a moved import, an option renamed in many "
                "files. replacement is a re template (\\1, named groups) in which \\uXXXX, \\UXXXXXXXX and \\xXX "
                "are written as their character (\\u00a0 for a no-break space). Refused, nothing written, past "
                "max_replacements or when the time budget runs out (a dry_run answers what it read). Searches "
                "every text file up to 32 MB, as sandbox_grep; a file left out is named "
                "with its reason, and matches the replacement leaves unchanged are reported as such. Answers a "
                "diffstat per file and each distinct change; dry_run=true: diffs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "The regular expression."},
                    "replacement": {
                        "type": "string",
                        "description": "The re template (\\uXXXX / \\xXX expanded); empty deletes each match.",
                    },
                    "path_prefix": {"type": "string", "description": "A workspace directory to change under."},
                    "include_globs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File name or path globs, e.g. *.py.",
                    },
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive (default false)."},
                    "max_replacements": {
                        "type": "integer",
                        "description": (
                            f"Cap (default {replacing.DEFAULT_MAX_REPLACEMENTS}, at most {replacing.MAX_REPLACEMENTS:,})."
                        ),
                    },
                    "dry_run": {"type": "boolean", "description": "Report without writing."},
                },
                "required": ["pattern", "replacement"],
            },
            source=source,
            read_only_call=landing.replace_dry_run,
            landed_probe=_replace_landed,
            landed_key=landing.replace_key,
        ),
        ToolDefinition(
            name="sandbox_apply_patch",
            description=(
                "Apply a unified diff the workspace holds (get_github_pr_diff with to_workspace_path, a saved "
                "git diff) to a checkout's files in ONE all-or-nothing write: each hunk's context must match exactly "
                "(fuzz 0; an offset is allowed and reported), added, deleted and renamed files included. A stale hunk "
                "refuses the call with its file, hunk and line, nothing written. Use it, not code_interpreter, to "
                "re-land or port a change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "Workspace path of the diff, e.g. work/pr.diff."},
                    "checkout": {
                        "type": "string",
                        "description": "The checkout directory its paths are relative to, e.g. main.",
                    },
                    "exclude": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repository paths, directories or globs left out of the patch.",
                    },
                    "dry_run": {"type": "boolean", "description": "Check every hunk without writing."},
                },
                "required": ["patch", "checkout"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_write_file",
            description=(
                "Create a text file of the sandbox workspace with the content given (UTF-8): a new source file, test "
                "or document, written whole, its directories created. An existing file is refused unless overwrite is "
                "true (change one with sandbox_edit_file). Returns the path, size and sha256; the file is stored before "
                "the answer, so sandbox_run_command and code_interpreter find it on their next call. Use it, not a "
                "code_interpreter script, to create a file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path in the workspace of the file to create, e.g. src/app.py.",
                    },
                    "content": {
                        "type": "string",
                        "description": f"The whole text of the file (at most {format_size(MAX_WRITE_BYTES)}).",
                    },
                    "create_dirs": {
                        "type": "boolean",
                        "description": "Create the missing directories of path (default true).",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Replace the file when it exists (default false: an existing path is refused).",
                    },
                },
                "required": ["path", "content"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_convert_images",
            description=(
                "Convert, resize or compress images of the sandbox workspace in one write: a PNG capture to WebP for "
                "a README, a screenshot bounded to a width, a JPEG recompressed. The output format is the suffix of "
                "'to' (.png, .jpg, .jpeg, .webp); a resize keeps the aspect ratio and never enlarges. Answers each "
                "file's size before and after. Use it, not a code_interpreter Pillow script, to write an image of a "
                "checkout. Refused with nothing written: a source absent or not an image, an animated source, an "
                "existing target unless overwrite."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "conversions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string", "description": "Source image, a workspace path."},
                                "to": {
                                    "type": "string",
                                    "description": "Target path; its suffix is the format (may equal from).",
                                },
                                "max_width": {"type": "integer", "description": "Bound on the width in px."},
                                "max_height": {"type": "integer", "description": "Bound on the height in px."},
                                "quality": {
                                    "type": "integer",
                                    "description": f"JPEG and lossy WebP quality, 1-100 (default {images.DEFAULT_QUALITY}).",
                                },
                                "lossless": {"type": "boolean", "description": "Lossless WebP."},
                            },
                            "required": ["from", "to"],
                        },
                        "description": f"At most {images.MAX_CONVERSIONS} conversions.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Replace an existing target other than the source (default false).",
                    },
                },
                "required": ["conversions"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_json_keys",
            description=(
                "Set, rename, remove or read top-level keys of i18n catalogs (JSON, one TS/JS object literal, "
                "or gettext .po keyed by msgid) in one call, each file written back in its own layout, all or "
                "nothing. Order: rename, set, remove; get reads after. A rename plus a set of the new key changes "
                "key and value in place. Use it, not code_interpreter or one sandbox_edit_file per file, to add a "
                "key everywhere."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "The catalogs."},
                    "set": {
                        "type": "object",
                        "description": 'Path ("*": every file) to {key: value}; a new key goes last unless placed.',
                    },
                    "set_from": {
                        "type": "string",
                        "description": "Instead of set: a workspace JSON file holding that object, read on the server.",
                    },
                    "place": {
                        "type": "object",
                        "description": '{new key: {"after": key} or {"before": key}}: where a key set adds goes.',
                    },
                    "rename": {"type": "object", "description": "{old key: new key}, in place."},
                    "remove": {"type": "array", "items": {"type": "string"}, "description": "Keys to drop."},
                    "get": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys to read. In a .json file a /a/0/b key is a nested JSON Pointer.",
                    },
                    "french_spacing": {
                        "type": "string",
                        "enum": ["report", "fix"],
                        "description": (
                            "fr catalogs: a plain space before ? ! ; : is reported (default) or fixed "
                            "to the no-break space it takes."
                        ),
                    },
                },
                "required": ["paths"],
            },
            source=source,
        ),
        ToolDefinition(
            name="materialize_repository",
            description=(
                "Check a repository out into the sandbox workspace, on the server, with the connected "
                "integration's credential (the sandbox stays offline): the tree at ref, optionally only "
                "sub_paths, under path (default: the repository name). Re-running it replaces that tree. "
                "No .git directory and no credential is written. A checkout record (.xtm-checkout.json under path: "
                "repository, ref, commit, the hash of every file) is written with it, so "
                "create_github_commit(from_checkout=path) commits exactly what the sandbox changed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "The connected integration that hosts it, by its provider name.",
                    },
                    "repository": {"type": "string", "description": "owner/name."},
                    "ref": {
                        "type": "string",
                        "description": "Branch, tag or commit SHA; empty for the default branch.",
                    },
                    "sub_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Directories or files of the repository to keep; empty for the whole tree. The manifests, "
                            "lock files and workflows its checks are read from are kept with them; a sub_path inside "
                            "the directory a declared check runs from is widened to that directory."
                        ),
                    },
                    "path": {"type": "string", "description": "Workspace directory to write it under."},
                    "add_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Grow the existing checkout at path instead of replacing it: these repository paths are "
                            "written at the commit its record names, nothing is removed, an edit is kept (a stale "
                            "file an earlier checkout left is rewritten), and the record covers them. Not with sub_paths or ref."
                        ),
                    },
                    "prune_clean_checkouts": {
                        "type": "boolean",
                        "description": (
                            "Only when the write would go over the workspace limits: remove every other clean "
                            "checkout (no local change, no stray file) first, whatever its ref. Without it only the "
                            "clean checkouts of a deleted branch go. A checkout with a change is never removed. "
                            "Default false."
                        ),
                    },
                },
                "required": ["provider", "repository"],
            },
            source=source,
            # A repeat replaces the tree at path (add_paths keeps the workspace's edits), so a resumed turn runs the
            # checkout a cut left half-written at once instead of holding it back as an unrecorded write (#10284).
            replay_safe=True,
        ),
        ToolDefinition(
            name="sandbox_checkout_status",
            description=(
                "The git status of a materialize_repository checkout (it has no .git): one M (modified), A (added) "
                "or D (removed) line per file changed since the checkout, with its origin (repository, ref, commit); "
                "'clean' when nothing changed. The same change set create_github_commit(from_checkout=...) commits. "
                "Read it before a commit, and first thing after a resumed run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "checkout": {
                        "type": "string",
                        "description": "The checkout's directory in the workspace, e.g. main.",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repository files or directories to narrow it to (optional).",
                    },
                },
                "required": ["checkout"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_checkout_diff",
            read_only=True,
            description=(
                "The git diff of a materialize_repository checkout: a unified diff of each changed file against the "
                "file as checked out (read through the connected integration at the recorded commit and checked "
                "against the recorded hash), added files from /dev/null, removed ones to /dev/null, binaries named "
                f"not shown; at most {checkouts.MAX_DIFF_FILES} files and {checkouts.MAX_DIFF_RESULT_CHARS:,} "
                "characters per call, the rest named. Read it before a commit instead of diffing by hand."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "checkout": {
                        "type": "string",
                        "description": "The checkout's directory in the workspace, e.g. main.",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repository files or directories to diff (optional; default every changed file).",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": (
                            f"Context lines around each change (default {checkouts.DEFAULT_CONTEXT_LINES}, "
                            f"at most {checkouts.MAX_CONTEXT_LINES})."
                        ),
                    },
                },
                "required": ["checkout"],
            },
            source=source,
        ),
        ToolDefinition(
            name="sandbox_checkout_rebase",
            description=(
                "The git rebase of a materialize_repository checkout: bring it onto a new ref (main moved, a PR "
                "conflicts) without losing its edits. The tree at ref is read on the server with the same "
                "sub_paths; per file, a three-way merge of the file as checked out, the workspace and the ref: a "
                "side that did not change takes the other, text changed on both sides is merged line by line and "
                "overlapping hunks are written between <<<<<<< workspace / ======= / >>>>>>> ref markers; a binary "
                "or a delete against a change keeps the workspace side. The checkout record moves to the ref, so "
                "sandbox_checkout_status and create_github_commit(from_checkout=...) then show only your changes. "
                "The answer lists the files updated, removed, merged and in conflict."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "checkout": {
                        "type": "string",
                        "description": "The checkout's directory in the workspace, e.g. main.",
                    },
                    "ref": {"type": "string", "description": "Branch, tag or commit SHA to rebase onto, e.g. main."},
                    "on_conflict": {
                        "type": "string",
                        "enum": [m.value for m in rebasing.OnConflict],
                        "description": (
                            "A hunk both sides changed: markers (default), or written without markers as the "
                            "ref's lines then the lines the workspace added there (ref-then-workspace: a line added "
                            "next to a line the ref rewrote), or the workspace's then the ref's added lines; ref or "
                            "workspace keeps that side's lines alone."
                        ),
                    },
                    "background": {
                        "type": "boolean",
                        "description": "true for a large checkout: answer a run_id at once while it runs; a resumed turn finds the same job.",
                    },
                    "run_id": {
                        "type": "string",
                        "description": "The run_id a background call answered: wait for that rebase's answer (no ref needed).",
                    },
                },
                "required": ["checkout"],
            },
            source=source,
            # A poll by run_id only waits for a job already started.
            read_only_call=lambda arguments: bool(str(arguments.get("run_id") or "").strip()),
            landed_probe=_rebase_landed,
        ),
        ToolDefinition(
            name="sandbox_diff_files",
            # Without write_merge_to it only compares: a read.
            read_only_call=lambda arguments: not arguments.get("write_merge_to"),
            description=(
                "Diff two text files of the sandbox workspace (diff -u of path -> other), or, with base, merge three "
                "(diff3 -m: path as ours and other as theirs, merged line by line from base, overlapping hunks between "
                "<<<<<<< / ======= / >>>>>>> markers labelled with the file names): the conflict count, the line of "
                "each conflict in the merged text and the merged text's diff against path. write_merge_to stores the "
                "merged file (replacing it) and answers its conflict hunks alone, ready for sandbox_edit_file and "
                "merge_github_branch(resolutions=[{path, workspace_path}]). Workspace files only; use it, not a "
                "code_interpreter difflib script, to compare or merge versions of a file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The first file (ours in a merge), e.g. pr/app.py."},
                    "other": {
                        "type": "string",
                        "description": "The second file (theirs in a merge), e.g. main/app.py.",
                    },
                    "base": {
                        "type": "string",
                        "description": "Optional: the common ancestor; given, the answer is the three-way merge.",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": (
                            f"Context lines around each change (default {filediff.DEFAULT_CONTEXT_LINES}, "
                            f"at most {filediff.MAX_CONTEXT_LINES})."
                        ),
                    },
                    "write_merge_to": {
                        "type": "string",
                        "description": "With base: workspace path to store the merged file at (may be path itself).",
                    },
                },
                "required": ["path", "other"],
            },
            source=source,
        ),
    ]
    if commands_offered():
        definitions.append(run_command_definition)
        definitions.append(
            ToolDefinition(
                name="sandbox_codemod",
                description=(
                    "Run a transform module of the workspace over the files of a path_prefix and/or include_globs, in "
                    "the checkout's declared codemod check (its own node image and parser), written in ONE "
                    "all-or-nothing write: for a sweep a regex cannot express (the enclosing element, an import "
                    "list, a ternary rewritten). The module's default export takes {path, source, parse} and returns "
                    "the new text (null: unchanged). Refused, nothing written, when the transform throws or a changed "
                    "file no longer parses (each named). Answers a diffstat per file and bounded diffs; dry_run=true previews."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "checkout": {
                            "type": "string",
                            "description": "The checkout declaring the codemod check, e.g. main.",
                        },
                        "transform": {
                            "type": "string",
                            "description": "Workspace path of the transform module, e.g. work/spinners.mjs.",
                        },
                        "path_prefix": {"type": "string", "description": "A workspace directory to transform under."},
                        "include_globs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "File name or path globs, e.g. *.tsx.",
                        },
                        "check": {
                            "type": "string",
                            "description": "The codemod check (default: the one named *codemod).",
                        },
                        "dry_run": {"type": "boolean", "description": "Report without writing."},
                    },
                    "required": ["checkout", "transform"],
                },
                source=source,
            )
        )
    if commands_offered() or labs.labs_enabled():
        definitions.extend(service_definitions)
    return definitions
