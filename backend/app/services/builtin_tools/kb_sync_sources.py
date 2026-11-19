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

"""Knowledge base sync sources from chat: the agent-tool half of Import Sources.

A knowledge base fed by a sync source (OneDrive / SharePoint, Google Drive,
Notion, GitHub, a website, Drata) had its scope changed and a re-sync started
from the Sources tab only. These tools do the same through the REST handlers
of ``/knowledge-bases/{id}/sync-sources`` (``api/v1/knowledge_bases.py``), so
the gates are the UI's: whoever may see the knowledge base reads its sources,
an administrator or a person who manages it changes one or starts a sync, and
the licence and package-origin locks hold.

* ``get_knowledge_base_sources`` - every source with its scope, schedule and
  last sync (time, status, documents, error);
* ``update_knowledge_base_source`` - the folders, exclusions and recursion of
  a drive source (``folder_paths`` / ``exclude`` / ``recursive`` of its
  config, which the sync engine applies: ``kb_sync.drive_item_excluded``);
* ``resync_knowledge_base`` - start a sync of one source or of every enabled
  one; ``full`` purges the source's documents first (Force re-sync).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.core.utils import safe_uuid
from app.tools.base import ToolDefinition

from . import agent_builder as _builder
from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

LIST_TOOL = "get_knowledge_base_sources"
UPDATE_TOOL = "update_knowledge_base_source"
RESYNC_TOOL = "resync_knowledge_base"
KB_SYNC_SOURCE_TOOL_NAMES = frozenset({LIST_TOOL, UPDATE_TOOL, RESYNC_TOOL})

#: The source types whose scope ``update_knowledge_base_source`` sets.
DRIVE_SCOPE_SOURCE_TYPES = frozenset({"microsoft-drive"})

_ANONYMOUS_REFUSAL = "Knowledge base sync sources are not available for anonymous users."

#: Config keys never echoed back: what the scheduler keeps for itself.
_INTERNAL_CONFIG_KEYS = frozenset({"stale_child_dbs", "stale_child_databases", "skipped_child_db_ids", "stale_pages"})


def as_path_list(value: object) -> list[str]:
    """A list argument as the model sent it: a list, or one string with one
    entry per line or comma; blanks dropped, order and duplicates kept once."""
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = value.replace("\r", "\n").replace(",", "\n").split("\n")
    elif isinstance(value, list | tuple):
        items = list(value)
    else:
        items = [value]
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def source_scope_lines(source: Any) -> list[str]:
    """What one source reads, as ``key: value`` lines of its config."""
    config = dict(getattr(source, "config", None) or {})
    lines: list[str] = []
    folders = as_path_list(config.pop("folder_paths", None))
    if folders:
        config.pop("folder_path", None)
        lines.append("folders: " + "; ".join(f"`{f}`" for f in folders))
    exclude = as_path_list(config.pop("exclude", None))
    for key, value in sorted(config.items()):
        if key in _INTERNAL_CONFIG_KEYS or value in (None, "", [], {}):
            continue
        lines.append(f"{key}: `{str(value)[:300]}`")
    lines.append("excluded: " + ("; ".join(f"`{e}`" for e in exclude) if exclude else "nothing"))
    return lines


def source_block(source: Any) -> str:
    """One source as the list tool answers it."""
    head = (
        f"• **{source.source_type}** source `{source.id}` - "
        f"{'enabled' if source.enabled else 'disabled'}, schedule {source.sync_interval}"
    )
    if source.integration_name:
        head += f", integration {source.integration_name}"
    last = source.last_synced_at or "never"
    status = source.last_sync_status or "not synced yet"
    body = [f"  - {line}" for line in source_scope_lines(source)]
    body.append(f"  - last sync: {last}, status {status}, {source.last_sync_documents} document(s)")
    if source.last_sync_error:
        body.append(f"  - last error: {str(source.last_sync_error)[:500]}")
    return "\n".join([head, *body])


def build_kb_sync_source_tools(
    user_id: str,
) -> tuple[list[ToolDefinition], dict[str, Callable[..., Awaitable[str]]]]:
    """The sync-source tools, as definitions plus the handlers to register."""

    async def _caller(db: AsyncSession) -> User | None:
        uid = safe_uuid(user_id) if user_id else None
        return await _builder._load_caller_user_for_builder(db, uid) if uid else None

    def _refusal(exc: Exception) -> str | None:
        from fastapi import HTTPException

        if isinstance(exc, HTTPException):
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return detail.rstrip(".") + "."
        return None

    async def _sources(db: AsyncSession, user: User, kb_id: str) -> list[Any]:
        from app.api.v1.knowledge_bases import list_sync_sources

        return list(await list_sync_sources(kb_id, user=user, db=db))

    # -- read ------------------------------------------------------------------

    async def _get_knowledge_base_sources(knowledge_base_id: str) -> str:
        from app.core.database import async_session

        if safe_uuid((knowledge_base_id or "").strip()) is None:
            return "knowledge_base_id must be a knowledge base UUID."
        async with async_session() as db:
            user = await _caller(db)
            if user is None:
                return _ANONYMOUS_REFUSAL
            try:
                sources = await _sources(db, user, knowledge_base_id.strip())
            except Exception as exc:
                if (refused := _refusal(exc)) is not None:
                    return refused
                raise
        if not sources:
            return (
                f"Knowledge base `{knowledge_base_id}` has no sync source: its documents "
                "are uploaded or written by agents."
            )
        head = f"Sync sources of knowledge base `{knowledge_base_id}` ({len(sources)}):"
        return "\n".join([head, *(source_block(s) for s in sources)])

    # -- update ------------------------------------------------------------------

    async def _update_knowledge_base_source(
        knowledge_base_id: str,
        source_id: str,
        include_paths: object = None,
        exclude_paths: object = None,
        recursive: object = None,
    ) -> str:
        from app.api.v1.knowledge_bases import update_sync_source
        from app.api.v1.schemas.knowledge_base import UpdateSyncSourceRequest
        from app.core.database import async_session

        kb_ref = (knowledge_base_id or "").strip()
        source_ref = (source_id or "").strip()
        if safe_uuid(kb_ref) is None or safe_uuid(source_ref) is None:
            return "knowledge_base_id and source_id must be UUIDs: read them with get_knowledge_base_sources."
        if include_paths is None and exclude_paths is None and recursive is None:
            return "Nothing to change: give include_paths, exclude_paths or recursive."
        async with async_session() as db:
            user = await _caller(db)
            if user is None:
                return _ANONYMOUS_REFUSAL
            try:
                sources = await _sources(db, user, kb_ref)
            except Exception as exc:
                if (refused := _refusal(exc)) is not None:
                    return refused
                raise
            current = next((s for s in sources if s.id == str(uuid.UUID(source_ref))), None)
            if current is None:
                return f"Sync source `{source_ref}` not found on knowledge base `{kb_ref}`."
            if current.source_type not in DRIVE_SCOPE_SOURCE_TYPES:
                return (
                    f"A {current.source_type} source has no folders or exclusions to set; "
                    f"this tool sets the scope of: {', '.join(sorted(DRIVE_SCOPE_SOURCE_TYPES))}."
                )
            config = dict(current.config or {})
            if include_paths is not None:
                folders = as_path_list(include_paths)
                if not folders:
                    return "include_paths is empty: give at least one folder (/ is the top of the drive)."
                config["folder_paths"] = folders
                config["folder_path"] = folders[0]
            if exclude_paths is not None:
                exclude = as_path_list(exclude_paths)
                if exclude:
                    config["exclude"] = exclude
                else:
                    config.pop("exclude", None)
            if recursive is not None:
                config["recursive"] = str(recursive).strip().lower() in ("true", "1", "yes")
            try:
                updated = await update_sync_source(
                    kb_ref, source_ref, UpdateSyncSourceRequest(config=config), user=user, db=db
                )
            except Exception as exc:
                if (refused := _refusal(exc)) is not None:
                    await db.rollback()
                    return f"Nothing was changed: {refused}"
                logger.exception("Agent tool update of sync source %s failed", source_ref)
                await db.rollback()
                return "Failed to update the sync source. Please try again."
        return "\n".join(
            [
                "Sync source updated. Documents the new scope leaves out are removed at the next sync; "
                f"start it now with `{RESYNC_TOOL}`.",
                source_block(updated),
            ]
        )

    # -- resync ------------------------------------------------------------------

    async def _resync_knowledge_base(knowledge_base_id: str, full: object = False, source_id: str = "") -> str:
        from app.api.v1.knowledge_bases import force_resync_source, trigger_sync_source
        from app.core.database import async_session

        kb_ref = (knowledge_base_id or "").strip()
        source_ref = (source_id or "").strip()
        if safe_uuid(kb_ref) is None or (source_ref and safe_uuid(source_ref) is None):
            return "knowledge_base_id (and source_id when given) must be UUIDs."
        purge = str(full).strip().lower() in ("true", "1", "yes")
        async with async_session() as db:
            user = await _caller(db)
            if user is None:
                return _ANONYMOUS_REFUSAL
            try:
                sources = await _sources(db, user, kb_ref)
            except Exception as exc:
                if (refused := _refusal(exc)) is not None:
                    return refused
                raise
            if source_ref:
                targets = [s for s in sources if s.id == str(uuid.UUID(source_ref))]
                if not targets:
                    return f"Sync source `{source_ref}` not found on knowledge base `{kb_ref}`."
            else:
                targets = [s for s in sources if s.enabled]
                if not targets:
                    return f"Knowledge base `{kb_ref}` has no enabled sync source to sync."
            lines: list[str] = []
            for source in targets:
                try:
                    if purge:
                        answer = await force_resync_source(kb_ref, source.id, user=user, db=db)
                        lines.append(
                            f"• `{source.id}` ({source.source_type}): full re-sync started, "
                            f"{answer.get('purged_documents', 0)} document(s) purged first."
                        )
                    else:
                        await trigger_sync_source(kb_ref, source.id, user=user, db=db)
                        lines.append(f"• `{source.id}` ({source.source_type}): sync started.")
                except Exception as exc:
                    if (refused := _refusal(exc)) is None:
                        raise
                    await db.rollback()
                    lines.append(f"• `{source.id}` ({source.source_type}): not started, {refused}")
        lines.append(
            f"The sync runs in the background: read its status and document count with `{LIST_TOOL}` "
            "(status syncing, then success or error), the documents with `list_kb_documents`."
        )
        return "\n".join(lines)

    handlers: dict[str, Callable[..., Awaitable[str]]] = {
        LIST_TOOL: _get_knowledge_base_sources,
        UPDATE_TOOL: _update_knowledge_base_source,
        RESYNC_TOOL: _resync_knowledge_base,
    }
    return _definitions(), handlers


def _definitions() -> list[ToolDefinition]:
    kb_ref = {"type": "string", "description": "Knowledge base UUID."}
    paths = {"type": "array", "items": {"type": "string"}}
    return [
        ToolDefinition(
            name=LIST_TOOL,
            description=(
                "A knowledge base's sync sources: type, folders, exclusions, schedule, last sync "
                "(time, status, documents, error)."
            ),
            input_schema={
                "type": "object",
                "properties": {"knowledge_base_id": kb_ref},
                "required": ["knowledge_base_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name=UPDATE_TOOL,
            description=(
                "Set what a OneDrive / SharePoint sync source reads: whole folders, exclusions and "
                "recursion; a document left out is removed at the next sync. Admin or manager only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": kb_ref,
                    "source_id": {"type": "string", "description": "Source UUID."},
                    "include_paths": {**paths, "description": "Folders, all their files read."},
                    "exclude_paths": {
                        **paths,
                        "description": "Left out: a name at any depth, a path, or a glob; [] clears.",
                    },
                    "recursive": {"type": "boolean", "description": "Read subfolders."},
                },
                "required": ["knowledge_base_id", "source_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name=RESYNC_TOOL,
            description=(
                "Start a sync of a knowledge base's sources in the background; full purges each "
                "source's documents first. Admin or manager only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": kb_ref,
                    "full": {"type": "boolean", "description": "Purge, then sync."},
                    "source_id": {"type": "string", "description": "One source; empty: every enabled one."},
                },
                "required": ["knowledge_base_id"],
            },
            source="builtin:platform",
        ),
    ]
