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

"""Workspace management from chat - the agent-builder half.

A person asks their assistant to "put this conversation in a Research
workspace", "create a workspace for the Acme audit and move my Acme threads
into it", "bind the policy corpus to it": these tools are the platform's own
workspace surface (Operations > Workspaces, the chat's "File in a workspace"),
offered to the model through the ``agent_builder`` suite:

* ``list_workspaces`` / ``get_workspace`` - what the caller sees, what each
  one holds, where the current conversation is filed;
* ``create_workspace`` / ``update_workspace`` (rename, describe, archive,
  restore) / ``delete_workspace`` / ``set_default_workspace``;
* ``update_workspace_knowledge_bases`` - bind or unbind corpora;
* ``list_conversations`` / ``move_conversations_to_workspace`` - find the
  caller's own threads (by title, by the agent they were held with, a deleted
  one by its last name, by the object they are about) and file (or
  unfile) them, the current one by default. ``list_conversations`` is also
  offered with the reader of ``builtin_tools.conversation_history``.

Nothing here decides a rule. Every write goes through the rules the REST
routes apply (``app.services.workspaces``: the default is neither archived nor
deleted, a workspace holding work items is not deleted, an archived one takes
no conversation, a corpus the caller cannot see is never bound) and through
the builder's permission gate (``_load_manageable_entity_for_builder``, the
string-returning ``get_manageable_entity``); a conversation is only ever its
owner's to file, as on ``PATCH /chat/conversations/{id}``. Writes are audited
like the REST ones, marked ``via: agent``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.core.utils import safe_uuid
from app.tools.base import ToolDefinition, consent_approved, consent_screened

from . import agent_builder as _builder
from ._shared import _platform_label, logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User
    from app.models.workspace import Workspace

#: The most conversations one call moves, and the most it lists.
MAX_CONVERSATIONS_PER_CALL = 50
#: The recent conversations ``get_workspace`` shows of the caller's own.
_RECENT_CONVERSATIONS_SHOWN = 10
#: The ``workspace_id`` the schema declares for "take it out of every
#: workspace" (folded): one declared value, never a list of synonyms.
UNFILE_TARGET = "none"
#: The ``conversation_ids`` entry the schema declares for "the conversation we
#: are in" (folded); an empty value means the same.
_CURRENT_CONVERSATION = "current"

_ANONYMOUS_REFUSAL = "Workspace management is not available for anonymous users."

#: Field widths of the model (``app.models.workspace``).
_NAME_MAX = 200
_ICON_MAX = 50
_COLOR_MAX = 30


def _source() -> str:
    # The workspace functions belong to the platform group.
    return "builtin:platform"


def _csv_ids(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _bool_arg(raw: object, label: str, default: bool | None) -> tuple[bool | None, str | None]:
    """A boolean argument as the builder reads one: a JSON bool or 'true' /
    'false'. Tool kwargs reach the handler verbatim, so ``bool("false")`` would
    archive a workspace the model asked to restore."""
    value, err, provided = _builder._parse_bool_arg(raw, label)
    if err:
        return None, err
    return (value if provided else default), None


def _parse_uuid_csv(raw: str, label: str) -> tuple[list[uuid.UUID], str | None]:
    """Comma-separated UUIDs, or the refusal naming the first bad one."""
    ids: list[uuid.UUID] = []
    for part in _csv_ids(raw):
        parsed = safe_uuid(part)
        if parsed is None:
            return [], f"`{part[:80]}` is not a valid {label} id."
        if parsed not in ids:
            ids.append(parsed)
    return ids, None


def _flags(workspace: Workspace, *, manageable: bool, owner_name: str | None, uid) -> str:
    flags: list[str] = []
    if workspace.is_default and workspace.user_id == uid:
        flags.append("default")
    if workspace.is_archived:
        flags.append("archived")
    if workspace.is_company_managed:
        flags.append("company-managed")
    elif workspace.user_id != uid:
        flags.append(f"shared by {owner_name or 'another user'}")
    if not manageable:
        flags.append("read-only for you")
    return "".join(f" [{f}]" for f in flags)


def _shown_name(name: str | None) -> str:
    """A workspace name another person chose, put in a tool result: one line,
    bounded - quoted text, never a sentence the model could read as ours."""
    from app.services.content_safety import single_line

    return single_line(name or "")[:80] or "unnamed"


def _held_with(conversation, agents: dict) -> str:
    """Who a conversation is held with, as a listing line says it: its
    agent, the agent a deletion took from it, the object it is about."""
    current = agents.get(conversation.agent_id) if conversation.agent_id else None
    deleted = agents.get(conversation.deleted_agent_id) if conversation.deleted_agent_id else None
    gone = (
        f"{_shown_name(deleted.name)} (deleted agent `{conversation.deleted_agent_id}`)" if deleted else None
    )
    if conversation.agent_id is None:
        held = f"agent: {gone}" if gone else "agent: General Assistant"
    else:
        held = f"agent: {_shown_name(current.name) if current else 'unknown'}"
        if gone:
            held += f", continued from {gone}"
    if conversation.video_project_id:
        held += f", about video `{conversation.video_project_id}`"
    elif conversation.presentation_id:
        held += f", about presentation `{conversation.presentation_id}`"
    return held


def _contents_line(counts: dict[str, int] | None) -> str:
    counts = counts or {}
    work_items = counts.get("work_items", 0)
    open_items = counts.get("open_work_items", 0)
    parts = [
        f"conversations: {counts.get('conversations', 0)}",
        f"knowledge bases: {counts.get('knowledge_bases', 0)}",
        f"open work items: {open_items}"
        + (f" (of {work_items})" if work_items != open_items else ""),
        f"shared-state entries: {counts.get('shared_states', 0)}",
    ]
    if counts.get("worksets"):
        parts.append(f"worksets: {counts['worksets']}")
    return ", ".join(parts)


def build_workspace_tools(
    user_id: str,
    *,
    conversation_id: str | None = None,
) -> tuple[list[ToolDefinition], dict[str, object]]:
    """The workspace tools, as definitions plus the handlers to register.

    *conversation_id* is the conversation the turn runs in, when there is one:
    ``move_conversations_to_workspace`` files it when no id is named, and the
    listings say where it is filed. An assignment run or an MCP call has none,
    and then an id is required.
    """
    current_conversation = safe_uuid(conversation_id) if conversation_id else None
    platform = _platform_label()

    def _caller_id() -> uuid.UUID | None:
        return safe_uuid(user_id) if user_id else None

    async def _caller(_db: AsyncSession, uid: uuid.UUID):
        return await _builder._load_caller_user_for_builder(_db, uid)

    async def _resolve_workspace_id(
        _db: AsyncSession, user, group_ids: list[str], ref: str
    ) -> tuple[uuid.UUID | None, str | None]:
        """A workspace id from an id, or from the exact name / slug of one the
        caller sees in their own three tiers.

        A name is matched over the three tiers only, never the admin bypass: an
        admin would otherwise meet every user's "General". Among several
        matches the caller's own wins when there is exactly one; anything else
        is ambiguous and says which ids to choose from.
        """
        ref = (ref or "").strip()
        if not ref:
            return None, "workspace_id is required (a workspace id, or its exact name)."
        parsed = safe_uuid(ref)
        if parsed is not None:
            return parsed, None
        rows = await _workspaces_named(_db, user, group_ids, ref.casefold())
        if not rows:
            return None, (
                f"No workspace named '{ref[:_NAME_MAX]}'. Call `list_workspaces` "
                "to see the workspaces you can use."
            )
        if len(rows) > 1:
            own = [w for w in rows if w.user_id == user.id]
            if len(own) == 1:
                return own[0].id, None
            listed = ", ".join(f"`{w.id}`" for w in rows[:10])
            return None, (
                f"Several workspaces are named '{ref[:_NAME_MAX]}': {listed}. "
                "Name the one you mean by its id."
            )
        return rows[0].id, None

    async def _visible_workspace(
        _db: AsyncSession, user, group_ids: list[str], workspace_uuid: uuid.UUID
    ) -> Workspace | None:
        from sqlalchemy import select

        from app.api.v1.helpers import agent_tool_admin_bypass, user_visible_filter
        from app.models.workspace import Workspace

        return (
            await _db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_uuid,
                    user_visible_filter(
                        Workspace,
                        user.id,
                        group_ids,
                        is_admin=agent_tool_admin_bypass(user),
                    ),
                )
            )
        ).scalar_one_or_none()

    async def _workspaces_named(
        _db: AsyncSession, user, group_ids: list[str], folded: str
    ) -> list[Workspace]:
        """The workspaces the caller sees (three tiers) whose name or slug is *folded*."""
        from sqlalchemy import func, or_, select

        from app.api.v1.helpers import user_visible_filter
        from app.models.workspace import Workspace

        return list(
            (
                await _db.execute(
                    select(Workspace).where(
                        user_visible_filter(Workspace, user.id, group_ids, is_admin=False),
                        or_(func.lower(Workspace.name) == folded, Workspace.slug == folded),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _manageable_workspace(
        _db: AsyncSession,
        user,
        group_ids: list[str],
        ref: str,
        *,
        action: str,
        lock: bool = False,
    ) -> tuple[Workspace | None, str | None]:
        """The workspace *ref* names, if the caller may *action* it (the
        builder's gate: three-tier visibility, then ``can_manage``). *lock*
        holds the row ``FOR UPDATE`` and reads it afresh."""
        from app.models.workspace import Workspace

        workspace_uuid, err = await _resolve_workspace_id(_db, user, group_ids, ref)
        if err is not None or workspace_uuid is None:
            return None, err
        return await _builder._load_manageable_entity_for_builder(
            _db,
            Workspace,
            user.id,
            workspace_uuid,
            action=action,
            entity_label="workspace",
            lock=lock,
        )

    async def _audit(_db: AsyncSession, user, workspace, action: str, summary: str, **details):
        from app.services.audit import log_audit

        await log_audit(
            _db,
            user=user,
            action=action,
            entity_type="workspace",
            entity_id=workspace.id,
            entity_name=workspace.name,
            summary=summary,
            details={"slug": workspace.slug, **details, "via": "agent"},
        )

    async def _current_workspace_id(_db: AsyncSession, uid: uuid.UUID) -> uuid.UUID | None:
        """Where the conversation of this turn is filed, if it is the caller's."""
        from sqlalchemy import select

        from app.models.chat import Conversation

        if current_conversation is None:
            return None
        return (
            await _db.execute(
                select(Conversation.workspace_id).where(
                    Conversation.id == current_conversation,
                    Conversation.user_id == uid,
                )
            )
        ).scalar_one_or_none()

    # ── list_workspaces ───────────────────────────────────────────────

    async def _list_workspaces(include_archived: object = False) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import (
            can_manage,
            get_user_group_ids,
            resolve_owner_names,
            user_visible_filter,
        )
        from app.core.database import async_session as _session
        from app.models.workspace import Workspace
        from app.services.workspaces import ensure_default_workspace, load_workspace_contents

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        include_archived, err = _bool_arg(include_archived, "include_archived", False)
        if err:
            return err
        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            # Every user owns a default, created on first use - as the REST
            # list does, so the answer never reads "you have none".
            await ensure_default_workspace(_db, uid)
            await _db.commit()
            group_ids = await get_user_group_ids(_db, uid)
            # The three tiers for admins too, as the library list:
            # an admin bypass would list one "General" per account.
            query = select(Workspace).where(
                user_visible_filter(Workspace, uid, group_ids, is_admin=False)
            )
            if not include_archived:
                query = query.where(Workspace.is_archived.is_(False))
            query = query.order_by(Workspace.is_default.desc(), Workspace.name.asc())
            workspaces = list((await _db.execute(query)).scalars().all())
            contents = await load_workspace_contents(_db, workspaces, user_id=uid)
            owners = await resolve_owner_names(_db, workspaces)
            filed_in = await _current_workspace_id(_db, uid)

            parts = [f"{len(workspaces)} {platform} workspace(s) you can see:"]
            for w in workspaces:
                owner = owners.get(str(w.user_id)) if w.user_id else None
                parts.append(
                    f"\n• **{w.name}** (id: `{w.id}`)"
                    + _flags(w, manageable=can_manage(user, w, group_ids), owner_name=owner, uid=uid)
                    + ("  <- this conversation is filed here" if filed_in == w.id else "")
                )
                if w.description:
                    parts.append(f"  {w.description}")
                parts.append(f"  Holds: {_contents_line(contents.get(str(w.id)))}")
            if current_conversation is not None:
                if filed_in is None:
                    parts.append("\nThis conversation is not filed in any workspace.")
                elif not any(w.id == filed_in for w in workspaces):
                    parts.append(
                        "\nThis conversation is filed in a workspace not listed here "
                        "(archived, or no longer shared with you)."
                    )
            return "\n".join(parts)

    # ── get_workspace ─────────────────────────────────────────────────

    async def _get_workspace(workspace_id: str) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import (
            agent_tool_admin_bypass,
            can_manage,
            get_user_group_ids,
            resolve_owner_names,
        )
        from app.core.database import async_session as _session
        from app.models.chat import Conversation
        from app.services.workspaces import (
            load_workspace_contents,
            visible_bound_knowledge_bases,
        )

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            group_ids = await get_user_group_ids(_db, uid)
            workspace_uuid, err = await _resolve_workspace_id(_db, user, group_ids, workspace_id)
            if err is not None or workspace_uuid is None:
                return err or "workspace_id is required."
            w = await _visible_workspace(_db, user, group_ids, workspace_uuid)
            if w is None:
                return f"workspace `{workspace_uuid}` not found or you don't have access."
            counts = (await load_workspace_contents(_db, [w], user_id=uid)).get(str(w.id))
            owner = (await resolve_owner_names(_db, [w])).get(str(w.user_id)) if w.user_id else None
            manageable = can_manage(user, w, group_ids)
            kbs = await visible_bound_knowledge_bases(
                _db, w, uid, group_ids, is_admin=agent_tool_admin_bypass(user)
            )
            recent = list(
                (
                    await _db.execute(
                        select(Conversation)
                        .where(
                            Conversation.workspace_id == w.id,
                            Conversation.user_id == uid,
                        )
                        .order_by(Conversation.updated_at.desc())
                        .limit(_RECENT_CONVERSATIONS_SHOWN)
                    )
                )
                .scalars()
                .all()
            )
            filed_in = await _current_workspace_id(_db, uid)

            parts = [
                f"**{w.name}**"
                + _flags(w, manageable=manageable, owner_name=owner, uid=uid),
                f"  • ID: `{w.id}`",
                f"  • Slug: {w.slug}",
                f"  • Description: {w.description or 'None'}",
                f"  • Icon: {w.icon or 'default'}; colour: {w.color or 'default'}",
                f"  • Owner: {'you' if w.user_id == uid else owner or 'company-managed'}",
                f"  • You can {'manage it and file conversations into it' if manageable else 'read it only'}",
                f"  • Holds: {_contents_line(counts)}",
            ]
            if filed_in == w.id:
                parts.append("  • This conversation is filed here.")
            parts.append("")
            if kbs:
                parts.append("Knowledge bases bound to it:")
                parts.extend(f"  • {kb.name} (id: `{kb.id}`)" for kb in kbs)
            else:
                parts.append("No knowledge base you can see is bound to it.")
            parts.append("")
            if recent:
                parts.append(
                    f"Your most recent conversations filed here (up to {_RECENT_CONVERSATIONS_SHOWN}):"
                )
                parts.extend(
                    f"  • {c.title} (id: `{c.id}`, updated {c.updated_at:%Y-%m-%d})"
                    for c in recent
                )
            else:
                parts.append("None of your conversations is filed here.")
            return "\n".join(parts)

    # ── create_workspace ──────────────────────────────────────────────

    async def _create_workspace(
        name: str,
        description: str = "",
        icon: str = "",
        color: str = "",
        is_company_managed: object = False,
    ) -> str:
        from app.core.database import async_session as _session
        from app.models.workspace import WORKSPACE_DEFAULT_ICON
        from app.services.workspaces import create_workspace

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        name = (name or "").strip()
        if not name:
            return "Workspace name is required (whitespace-only names are not allowed)."
        if len(name) > _NAME_MAX:
            return f"Workspace name is too long ({len(name)} characters, at most {_NAME_MAX})."
        icon = (icon or "").strip()
        color = (color or "").strip()
        if len(icon) > _ICON_MAX:
            return f"icon is too long (at most {_ICON_MAX} characters): pass a Lucide icon name."
        if len(color) > _COLOR_MAX:
            return f"color is too long (at most {_COLOR_MAX} characters)."
        is_company_managed, err = _bool_arg(is_company_managed, "is_company_managed", False)
        if err:
            return err
        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            if is_company_managed and not user.is_admin:
                return "Only admins can create company-managed workspaces."
            try:
                workspace = await create_workspace(
                    _db,
                    user_id=uid,
                    name=name,
                    description=(description or "").strip(),
                    icon=icon or WORKSPACE_DEFAULT_ICON,
                    color=color,
                    is_company_managed=bool(is_company_managed),
                )
                await _audit(
                    _db,
                    user,
                    workspace,
                    "create",
                    f"Created workspace '{workspace.name}'",
                    is_company_managed=workspace.is_company_managed,
                )
                await _db.commit()
            except Exception:
                logger.exception("Agent builder create_workspace failed for user %s", uid)
                await _db.rollback()
                return "Failed to create the workspace. Please try again."
            return (
                f"Workspace **{workspace.name}** created.\n"
                f"  • ID: `{workspace.id}`\n"
                "File conversations into it with `move_conversations_to_workspace`, "
                "and bind knowledge bases with `update_workspace_knowledge_bases`."
            )

    # ── update_workspace ──────────────────────────────────────────────

    async def _update_workspace(
        workspace_id: str,
        name: str = "",
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        archived: object = None,
        is_company_managed: object = None,
    ) -> str:
        from app.api.v1.helpers import get_user_group_ids
        from app.core.database import async_session as _session
        from app.models.workspace import WORKSPACE_DEFAULT_ICON
        from app.services.workspaces import WorkspaceActionRefusedError, set_workspace_archived

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        archived, err = _bool_arg(archived, "archived", None)
        if err:
            return err
        company, err = _bool_arg(is_company_managed, "is_company_managed", None)
        if err:
            return err
        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            group_ids = await get_user_group_ids(_db, uid)
            workspace, err = await _manageable_workspace(
                _db, user, group_ids, workspace_id, action="edit"
            )
            if err is not None or workspace is None:
                return err or "Workspace not found."

            updated: list[str] = []
            # A name is required, so a blank one leaves it unchanged. The slug
            # never follows a rename (see the model).
            if (name or "").strip():
                new_name = name.strip()
                if len(new_name) > _NAME_MAX:
                    return f"Workspace name is too long (at most {_NAME_MAX} characters)."
                if new_name != workspace.name:
                    workspace.name = new_name
                    updated.append("name")
            # The three optional fields follow `PUT /workspaces/{id}`: omitted
            # leaves them, an empty string clears the description and the
            # colour and puts the default icon back. Tool kwargs arrive
            # verbatim, so omitted (None) and "" are told apart.
            if description is not None:
                new_description = str(description).strip()
                if new_description != (workspace.description or ""):
                    workspace.description = new_description
                    updated.append("description" if new_description else "description cleared")
            if icon is not None:
                new_icon = str(icon).strip() or WORKSPACE_DEFAULT_ICON
                if len(new_icon) > _ICON_MAX:
                    return f"icon is too long (at most {_ICON_MAX} characters)."
                if new_icon != (workspace.icon or WORKSPACE_DEFAULT_ICON):
                    workspace.icon = new_icon
                    updated.append("icon" if str(icon).strip() else "default icon")
            if color is not None:
                new_color = str(color).strip()
                if len(new_color) > _COLOR_MAX:
                    return f"color is too long (at most {_COLOR_MAX} characters)."
                if new_color != (workspace.color or ""):
                    workspace.color = new_color
                    updated.append("color" if new_color else "color cleared")
            if archived is not None and archived != workspace.is_archived:
                try:
                    await set_workspace_archived(_db, workspace, archived)
                except WorkspaceActionRefusedError as exc:
                    return exc.detail
                updated.append("archived" if archived else "restored")
            if company is not None and bool(company) != bool(workspace.is_company_managed):
                # #398: an existing workspace changes audience in place, its id kept.
                refusal = await _set_workspace_company_managed(_db, user, workspace, bool(company))
                if refusal is not None:
                    await _db.rollback()
                    return f"{refusal} Nothing was saved."
                updated.append("company-managed" if company else "personal")

            if not updated:
                return f"Workspace **{workspace.name}** is already as requested; nothing changed."
            try:
                await _audit(
                    _db,
                    user,
                    workspace,
                    "update",
                    f"Updated workspace '{workspace.name}'",
                    is_archived=workspace.is_archived,
                    is_company_managed=workspace.is_company_managed,
                )
                await _db.commit()
            except Exception:
                logger.exception("Agent builder update_workspace failed for %s", workspace_id)
                await _db.rollback()
                return "Failed to update the workspace. Please try again."
            return f"Workspace **{workspace.name}** updated: {', '.join(updated)}."

    async def _set_workspace_company_managed(
        _db: AsyncSession, user: User, workspace: Workspace, company: bool
    ) -> str | None:
        """Toggle *workspace* company-managed as `PUT /workspaces/{id}` does; a refusal text, else None (#398)."""
        from sqlalchemy import func, select

        from app.api.v1.helpers import handle_company_managed_toggle
        from app.models.fleet import Fleet
        from app.services.workspaces import (
            WorkspaceActionRefusedError,
            ensure_can_be_company_managed,
            lock_workspace_for_hosting,
        )

        if not user.is_admin:
            return "Only admins can change whether a workspace is company-managed."
        try:
            ensure_can_be_company_managed(workspace, company)
        except WorkspaceActionRefusedError as exc:
            return exc.detail
        if not company:
            # Serialised with fleet binds: a company-managed fleet never ends up in a personal workspace.
            await lock_workspace_for_hosting(_db, workspace)
            hosted = (
                await _db.execute(
                    select(func.count(Fleet.id)).where(
                        Fleet.workspace_id == workspace.id, Fleet.is_company_managed.is_(True)
                    )
                )
            ).scalar() or 0
            if hosted:
                return (
                    f"Workspace **{workspace.name}** hosts {hosted} company-managed fleet(s): a company-managed "
                    "fleet needs a company-managed workspace. Make the fleet personal or move it first."
                )
        await handle_company_managed_toggle(
            _db,
            user,
            workspace,
            company,
            agent_field=None,
            keep_grants=bool(getattr(workspace, "access_restricted", False)),
        )
        return None

    # ── delete_workspace ──────────────────────────────────────────────

    async def _delete_workspace(workspace_id: str) -> str:
        from app.api.v1.helpers import get_user_group_ids
        from app.core.database import async_session as _session
        from app.services.workspaces import WorkspaceActionRefusedError, remove_workspace

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            group_ids = await get_user_group_ids(_db, uid)
            workspace, err = await _manageable_workspace(
                _db, user, group_ids, workspace_id, action="delete"
            )
            if err is not None or workspace is None:
                return err or "Workspace not found."
            name = workspace.name
            try:
                deletion = await remove_workspace(_db, workspace)
                await _audit(
                    _db,
                    user,
                    workspace,
                    "delete",
                    f"Deleted workspace '{name}'",
                    is_company_managed=workspace.is_company_managed,
                    conversations_unfiled=deletion.conversations_unfiled,
                    worksets_purged=deletion.worksets_purged,
                    knowledge_documents_deleted=deletion.knowledge_documents_deleted,
                )
                await _db.commit()
            except WorkspaceActionRefusedError as exc:
                await _db.rollback()
                return exc.detail
            except Exception:
                logger.exception("Agent builder delete_workspace failed for %s", workspace_id)
                await _db.rollback()
                return "Failed to delete the workspace. Please try again."
            lines = [f"Workspace **{name}** has been deleted."]
            if deletion.conversations_unfiled:
                lines.append(
                    f"  • {deletion.conversations_unfiled} conversation(s) filed in it "
                    "are now in no workspace."
                )
            if deletion.worksets_purged:
                lines.append(f"  • {deletion.worksets_purged} workset(s) purged.")
            if deletion.knowledge_documents_deleted:
                lines.append(
                    f"  • Its own sources and conversation index went with it "
                    f"({deletion.knowledge_documents_deleted} document(s))."
                )
            lines.append(
                "  • Knowledge bases bound to it and skills linked to it were not "
                "touched; only the links went."
            )
            return "\n".join(lines)

    # ── set_default_workspace ─────────────────────────────────────────

    async def _set_default_workspace(workspace_id: str) -> str:
        from app.api.v1.helpers import get_user_group_ids
        from app.core.database import async_session as _session
        from app.services.workspaces import WorkspaceActionRefusedError, make_default_workspace

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            group_ids = await get_user_group_ids(_db, uid)
            # Locked like `POST /workspaces/{id}/default`: the company-managed
            # rule is checked against the row as stored.
            workspace, err = await _manageable_workspace(
                _db, user, group_ids, workspace_id, action="edit", lock=True
            )
            if err is not None or workspace is None:
                return err or "Workspace not found."
            try:
                changed = await make_default_workspace(_db, uid, workspace)
                if not changed:
                    return f"**{workspace.name}** is already your default workspace."
                await _audit(
                    _db,
                    user,
                    workspace,
                    "update",
                    f"Made workspace '{workspace.name}' the default",
                )
                await _db.commit()
            except WorkspaceActionRefusedError as exc:
                await _db.rollback()
                return exc.detail
            except Exception:
                logger.exception("Agent builder set_default_workspace failed for %s", workspace_id)
                await _db.rollback()
                return "Failed to change the default workspace. Please try again."
            return f"**{workspace.name}** is now your default workspace."

    # ── update_workspace_knowledge_bases ──────────────────────────────

    async def _update_workspace_knowledge_bases(
        workspace_id: str,
        add_knowledge_base_ids: str = "",
        remove_knowledge_base_ids: str = "",
    ) -> str:
        from app.api.v1.helpers import agent_tool_admin_bypass, get_user_group_ids
        from app.core.database import async_session as _session
        from app.services.workspaces import (
            WorkspaceActionRefusedError,
            rebind_workspace_knowledge_bases,
            visible_bound_knowledge_bases,
        )

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        add, err = _parse_uuid_csv(add_knowledge_base_ids, "knowledge base")
        if err:
            return err
        remove, err = _parse_uuid_csv(remove_knowledge_base_ids, "knowledge base")
        if err:
            return err
        if not add and not remove:
            return (
                "Name the knowledge bases to bind (add_knowledge_base_ids) or unbind "
                "(remove_knowledge_base_ids)."
            )
        if set(add) & set(remove):
            return "The same knowledge base cannot be both added and removed."
        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            group_ids = await get_user_group_ids(_db, uid)
            workspace, err = await _manageable_workspace(
                _db, user, group_ids, workspace_id, action="edit"
            )
            if err is not None or workspace is None:
                return err or "Workspace not found."
            is_admin = agent_tool_admin_bypass(user)
            try:
                added, removed = await rebind_workspace_knowledge_bases(
                    _db,
                    workspace,
                    uid,
                    group_ids,
                    is_admin=is_admin,
                    add=set(add),
                    remove=set(remove),
                )
                if added or removed:
                    await _audit(
                        _db,
                        user,
                        workspace,
                        "update",
                        f"Updated knowledge bases bound to workspace '{workspace.name}'",
                        added_knowledge_base_ids=sorted(str(k) for k in added),
                        removed_knowledge_base_ids=sorted(str(k) for k in removed),
                    )
                await _db.commit()
            except WorkspaceActionRefusedError as exc:
                await _db.rollback()
                return f"{exc.detail}. Nothing was changed."
            except Exception:
                logger.exception(
                    "Agent builder update_workspace_knowledge_bases failed for %s",
                    workspace_id,
                )
                await _db.rollback()
                return "Failed to update the workspace's knowledge bases. Please try again."
            bound = await visible_bound_knowledge_bases(
                _db, workspace, uid, group_ids, is_admin=is_admin
            )
            lines = [
                f"Workspace **{workspace.name}**: {len(added)} knowledge base(s) bound, "
                f"{len(removed)} unbound."
            ]
            not_removed = set(remove) - removed
            if not_removed:
                lines.append(
                    "  • Not bound to it, or not visible to you (nothing removed): "
                    + ", ".join(f"`{k}`" for k in sorted(str(k) for k in not_removed))
                )
            lines.append(
                "Now bound: " + (", ".join(f"{kb.name} (`{kb.id}`)" for kb in bound) or "none")
            )
            return "\n".join(lines)

    # ── list_conversations ────────────────────────────────────────────

    async def _agents_named(_db: AsyncSession, uid: uuid.UUID, ref: str) -> list[uuid.UUID]:
        """The agents *ref* names among those the caller's own conversations were held
        with: an id, or a name the agent bears or bore when it was deleted."""
        from sqlalchemy import select, union

        from app.api.v1.users import escape_like
        from app.models.agent import Agent
        from app.models.agent_tombstone import AgentTombstone
        from app.models.chat import Conversation
        from app.models.llm_trace import LLMTrace

        parsed = safe_uuid(ref)
        if parsed is not None:
            return [parsed]
        held = select(
            union(
                *(
                    select(column.label("agent_id")).where(Conversation.user_id == uid, column.is_not(None))
                    for column in (Conversation.agent_id, Conversation.original_agent_id, Conversation.deleted_agent_id)
                )
            ).subquery()
        )
        pattern = f"%{escape_like(ref)}%"
        live = select(Agent.id).where(Agent.id.in_(held), Agent.name.ilike(pattern, escape="\\"))
        gone = select(AgentTombstone.agent_id).where(
            AgentTombstone.agent_id.in_(held), AgentTombstone.name.ilike(pattern, escape="\\")
        )
        # An agent deleted before tombstones were written: the name the
        # caller's own traces recorded for it, while they are kept.
        traced = select(LLMTrace.agent_id).where(
            LLMTrace.user_id == uid,
            LLMTrace.agent_id.is_not(None),
            LLMTrace.agent_name_snapshot.ilike(pattern, escape="\\"),
        )
        return list((await _db.execute(union(live, gone, traced))).scalars().all())

    def _held_with_clause(uid: uuid.UUID, agent_ids: list[uuid.UUID]):
        """The caller's conversations those agents were held with, or answered in
        (a mention, a transfer: their traces, while kept)."""
        from sqlalchemy import or_, select

        from app.models.chat import Conversation
        from app.models.llm_trace import LLMTrace

        return or_(
            Conversation.agent_id.in_(agent_ids),
            Conversation.original_agent_id.in_(agent_ids),
            Conversation.deleted_agent_id.in_(agent_ids),
            Conversation.id.in_(
                select(LLMTrace.conversation_id).where(
                    LLMTrace.agent_id.in_(agent_ids),
                    LLMTrace.user_id == uid,
                    LLMTrace.conversation_id.is_not(None),
                )
            ),
        )

    def _carries_clause(text: str):
        """The conversations whose messages carry *text* (an id, a marker's opening)."""
        from sqlalchemy import or_, select

        from app.api.v1.users import escape_like
        from app.models.chat import Conversation, Message

        pattern = f"%{escape_like(text)}%"
        return (
            select(Message.id)
            .where(
                Message.conversation_id == Conversation.id,
                or_(
                    Message.content.ilike(pattern, escape="\\"),
                    Message.tool_calls_json.ilike(pattern, escape="\\"),
                    Message.metadata_json.ilike(pattern, escape="\\"),
                ),
            )
            .exists()
        )

    async def _list_conversations(
        workspace_id: str = "",
        unfiled_only: object = False,
        search: str = "",
        archived: object = False,
        limit: object = 20,
        agent: str = "",
        object_id: str = "",
        object_type: str = "",
    ) -> str:
        from sqlalchemy import false, or_, select

        from app.api.v1.helpers import get_user_group_ids, user_visible_filter
        from app.api.v1.users import escape_like
        from app.channels.base import ARTIFACT_MARKER_OPENINGS
        from app.core.database import async_session as _session
        from app.models.chat import Conversation
        from app.models.workspace import Workspace
        from app.services.conversation_continuity import agent_labels

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        unfiled_only, err = _bool_arg(unfiled_only, "unfiled_only", False)
        if err:
            return err
        archived, err = _bool_arg(archived, "archived", False)
        if err:
            return err
        try:
            limit = max(1, min(int(limit), MAX_CONVERSATIONS_PER_CALL))
        except (TypeError, ValueError):
            limit = 20
        if (workspace_id or "").strip() and unfiled_only:
            return "Pass either workspace_id or unfiled_only, not both."
        object_uuid = None
        if (object_id or "").strip():
            object_uuid = safe_uuid(object_id.strip())
            if object_uuid is None:
                return f"`{object_id.strip()[:80]}` is not a valid id: pass the id of a video, presentation, report or file."
        kind = (object_type or "").strip().casefold()
        if kind and kind not in ARTIFACT_MARKER_OPENINGS:
            return "object_type is one of: " + ", ".join(sorted(ARTIFACT_MARKER_OPENINGS)) + "."
        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            group_ids = await get_user_group_ids(_db, uid)
            # The caller's own threads, as the chat sidebar lists them.
            query = select(Conversation).where(
                Conversation.user_id == uid,
                Conversation.channel == "web",
                Conversation.is_archived == archived,
            )
            if (workspace_id or "").strip():
                workspace_uuid, err = await _resolve_workspace_id(
                    _db, user, group_ids, workspace_id
                )
                if err is not None or workspace_uuid is None:
                    return err or "workspace_id is required."
                # An id answers like a name: a workspace the caller no longer
                # sees (a share revoked) matches nothing, even though their own
                # threads may still be filed there - they are listed, unfiltered,
                # as "in a workspace you no longer see". Same answer as an
                # unknown id: no existence oracle.
                if await _visible_workspace(_db, user, group_ids, workspace_uuid) is None:
                    return "No conversation of yours matches."
                query = query.where(Conversation.workspace_id == workspace_uuid)
            elif unfiled_only:
                query = query.where(Conversation.workspace_id.is_(None))
            if (search or "").strip():
                query = query.where(
                    Conversation.title.ilike(f"%{escape_like(search.strip())}%", escape="\\")
                )
            if (agent or "").strip():
                agent_ids = await _agents_named(_db, uid, agent.strip())
                if not agent_ids:
                    return (
                        f"None of your conversations was held with an agent named "
                        f"'{_shown_name(agent)}'."
                    )
                query = query.where(_held_with_clause(uid, agent_ids))
            if object_uuid is not None:
                query = query.where(
                    or_(
                        Conversation.presentation_id == object_uuid,
                        Conversation.video_project_id == object_uuid,
                        _carries_clause(str(object_uuid)),
                    )
                )
            if kind:
                bound = {
                    "presentation": Conversation.presentation_id.is_not(None),
                    "video": Conversation.video_project_id.is_not(None),
                }.get(kind, false())
                query = query.where(or_(bound, _carries_clause(ARTIFACT_MARKER_OPENINGS[kind])))
            conversations = list(
                (
                    await _db.execute(
                        query.order_by(Conversation.updated_at.desc()).limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
            more = len(conversations) > limit
            conversations = conversations[:limit]
            if not conversations:
                return "No conversation of yours matches."
            workspace_ids = {c.workspace_id for c in conversations if c.workspace_id}
            names = (
                dict(
                    (
                        await _db.execute(
                            select(Workspace.id, Workspace.name).where(
                                Workspace.id.in_(workspace_ids),
                                user_visible_filter(Workspace, uid, group_ids, is_admin=False),
                            )
                        )
                    ).all()
                )
                if workspace_ids
                else {}
            )
            agents = await agent_labels(
                _db, {a for c in conversations for a in (c.agent_id, c.deleted_agent_id)}
            )
            parts = [f"{len(conversations)} of your conversation(s), most recent first:"]
            for c in conversations:
                if c.workspace_id is None:
                    where = "not filed"
                else:
                    where = (
                        f"in **{names[c.workspace_id]}**"
                        if c.workspace_id in names
                        else "in a workspace you no longer see"
                    )
                parts.append(
                    f"• {c.title} (id: `{c.id}`, updated {c.updated_at:%Y-%m-%d}) - {where}"
                    + f" - {_held_with(c, agents)}"
                    + ("  <- this conversation" if c.id == current_conversation else "")
                )
            if more:
                parts.append(
                    f"More conversations match: narrow with `search`, or raise `limit` "
                    f"(at most {MAX_CONVERSATIONS_PER_CALL})."
                )
            return "\n".join(parts)

    # ── move_conversations_to_workspace ───────────────────────────────

    async def _move_conversations_to_workspace(
        workspace_id: str | None,
        conversation_ids: str = "",
    ) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import get_user_group_ids
        from app.core.database import async_session as _session
        from app.models.chat import Conversation
        from app.services.workspaces import (
            WorkspaceActionRefusedError,
            ensure_workspace_accepts_conversations,
            workspace_reaches_others,
        )

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL

        wanted: list[uuid.UUID] = []
        for part in _csv_ids(conversation_ids):
            if part.casefold() == _CURRENT_CONVERSATION:
                if current_conversation is None:
                    return "There is no current conversation here: name the conversation ids."
                parsed = current_conversation
            else:
                parsed = safe_uuid(part)
                if parsed is None:
                    return f"`{part[:80]}` is not a valid conversation id."
            if parsed not in wanted:
                wanted.append(parsed)
        if not wanted:
            if current_conversation is None:
                return (
                    "Name the conversations to move (conversation_ids): this call runs "
                    "outside a conversation."
                )
            wanted = [current_conversation]
        if len(wanted) > MAX_CONVERSATIONS_PER_CALL:
            return (
                f"At most {MAX_CONVERSATIONS_PER_CALL} conversations per call "
                f"({len(wanted)} named)."
            )

        async with _session() as _db:
            user = await _caller(_db, uid)
            if user is None:
                return "User not found."
            group_ids = await get_user_group_ids(_db, uid)

            target = None
            folded = (workspace_id or "").strip().casefold()
            if workspace_id is None or folded == UNFILE_TARGET:
                # "none" also naming a workspace the caller sees is a request
                # that reads both ways: refuse rather than unfile silently. A
                # JSON null names no workspace, so it never collides.
                named = await _workspaces_named(_db, user, group_ids, folded) if workspace_id is not None else []
                if named:
                    return (
                        f"'{(workspace_id or '').strip()}' names a workspace of yours as well as "
                        '"no workspace". To file into that workspace, pass its id ('
                        + ", ".join(f"`{w.id}`" for w in named[:10])
                        + "); to take the conversations out of their workspace, pass "
                        "workspace_id null. Nothing was moved."
                    )
            else:
                # Filing takes the MANAGE right, as on REST: a filed thread
                # works in the workspace's coordination scope, which a
                # read_only grantee may not write to.
                target, err = await _manageable_workspace(
                    _db, user, group_ids, workspace_id, action="use"
                )
                if err is not None or target is None:
                    return err or "Workspace not found."
                try:
                    ensure_workspace_accepts_conversations(target)
                except WorkspaceActionRefusedError as exc:
                    return f"{exc.detail} Restore it with `update_workspace` first."

            # A conversation is its owner's to file, and nobody else's.
            rows = {
                c.id: c
                for c in (
                    await _db.execute(
                        select(Conversation).where(
                            Conversation.id.in_(wanted),
                            Conversation.user_id == uid,
                        )
                    )
                )
                .scalars()
                .all()
            }
            missing = [c for c in wanted if c not in rows]
            if missing:
                # All or nothing: half a move is harder to reason about than
                # a refusal naming what to fix.
                return (
                    "Conversation(s) not found, or not yours: "
                    + ", ".join(f"`{c}`" for c in missing)
                    + ". Nothing was moved."
                )
            target_id = target.id if target is not None else None
            # Into a workspace other people see, only a call the person
            # approved moves anything (spec 032 FR-022): its members
            # could then search what was written. The approval is the consent
            # screen's (``workspace_filing.install_filing_consent``), decided
            # here on the workspace the call resolved to: a call the screen
            # cleared as private is not an approval.
            if (
                target is not None
                and not consent_approved()
                and workspace_reaches_others(target, uid)
                and any(rows[c].workspace_id != target_id for c in wanted)
            ):
                shown = _shown_name(target.name)
                if consent_screened():
                    return (
                        f"Nothing was moved. The workspace '{shown}' is shared with other "
                        "people, who could then search these conversations: call again with "
                        f"its id `{target.id}` so the person is asked to approve the move."
                    )
                return (
                    f"Nothing was moved. The workspace '{shown}' is shared with other "
                    "people, who could then search these conversations, so the person must "
                    "approve the move - which cannot be asked here. They can move a "
                    "conversation from its menu in XTM One."
                )
            moved = [rows[c] for c in wanted if rows[c].workspace_id != target_id]
            already = [rows[c] for c in wanted if rows[c].workspace_id == target_id]
            for conversation in moved:
                conversation.workspace_id = target_id
            if moved:
                try:
                    await _db.commit()
                except Exception:
                    logger.exception(
                        "Agent builder move_conversations_to_workspace failed for user %s", uid
                    )
                    await _db.rollback()
                    return "Failed to move the conversation(s). Please try again."

            where = f"into **{target.name}**" if target is not None else "out of their workspace"
            lines: list[str] = []
            if moved:
                lines.append(f"Moved {len(moved)} conversation(s) {where}:")
                lines.extend(f"  • {c.title} (`{c.id}`)" for c in moved)
            if already:
                lines.append(
                    "Already "
                    + (f"in **{target.name}**" if target is not None else "unfiled")
                    + ": "
                    + ", ".join(f"{c.title} (`{c.id}`)" for c in already)
                )
            if current_conversation is not None and any(c.id == current_conversation for c in moved):
                lines.append(
                    "This conversation's move takes effect from the next message: agents "
                    "then work with "
                    + (
                        "this workspace's instructions, sources, skills, shared state and "
                        "work items."
                        if target is not None
                        else "no workspace's shared state or work items."
                    )
                )
            return "\n".join(lines)

    # ── Definitions ───────────────────────────────────────────────────

    workspace_ref = (
        "The workspace's id (from list_workspaces), or its exact name."
    )
    about = (
        f"{platform} workspaces are the platform's own named environments that "
        "group a person's conversations, the knowledge bases bound to them and "
        "the shared state and work items their agents build (not an OpenCTI "
        "workspace or dashboard)."
    )
    definitions = [
        ToolDefinition(
            name="list_workspaces",
            description=(
                f"List the {platform} workspaces the user can see, with what each "
                "holds (conversations, knowledge bases, work items, shared state), "
                "which one is their default, which are read-only for them, and where "
                f"the current conversation is filed. {about}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_archived": {
                        "type": "boolean",
                        "description": "Also list archived workspaces.",
                        "default": False,
                    },
                },
                "required": [],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="get_workspace",
            description=(
                "Get one workspace: its details, what it holds, the knowledge bases "
                "bound to it, and the user's most recent conversations filed in it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": workspace_ref},
                },
                "required": ["workspace_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="create_workspace",
            description=(
                f"Create a {platform} workspace owned by the user. {about} To create "
                "one and put conversations in it, call this, then "
                "move_conversations_to_workspace."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Workspace name"},
                    "description": {"type": "string", "default": ""},
                    "icon": {
                        "type": "string",
                        "description": "A Lucide icon name (e.g. FolderKanban); empty for the default.",
                        "default": "",
                    },
                    "color": {
                        "type": "string",
                        "description": "A hex colour or Tailwind colour name; empty to let the UI decide.",
                        "default": "",
                    },
                    "is_company_managed": {
                        "type": "boolean",
                        "description": "Visible to every user (admin only).",
                        "default": False,
                    },
                },
                "required": ["name"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="update_workspace",
            description=(
                "Rename, describe, restyle, archive or restore a workspace. Provide "
                "only the fields to change: an omitted field is left as it is, and an "
                "empty description or color clears it (an empty icon restores the "
                "default). Archiving hides it from every picker and "
                "keeps its contents; the default workspace cannot be archived. "
                "Creator, config-tier grantee or admin only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": workspace_ref},
                    "name": {"type": "string", "description": "New name; omit to keep it."},
                    "description": {
                        "type": "string",
                        "description": "New description; omit to keep it, empty to clear it.",
                    },
                    "icon": {
                        "type": "string",
                        "description": "Lucide icon name; omit to keep it, empty for the default.",
                    },
                    "color": {
                        "type": "string",
                        "description": "Color; omit to keep it, empty to clear it.",
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "true to archive, false to restore; omit to leave it.",
                    },
                    "is_company_managed": {"type": "boolean", "description": "Admin only."},
                },
                "required": ["workspace_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="delete_workspace",
            description=(
                "Delete a workspace. Its conversations are kept, in no workspace; "
                "bound knowledge bases are kept (only the binding goes), "
                "its worksets are purged. Refused for the default workspace and while "
                "it holds work items (archive it instead). Cannot be undone: confirm "
                "with the user first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": workspace_ref},
                },
                "required": ["workspace_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="set_default_workspace",
            description=(
                "Make a workspace the user's default (where they land when nothing "
                "else is chosen). Only a workspace they own and that is not archived."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": workspace_ref},
                },
                "required": ["workspace_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="update_workspace_knowledge_bases",
            description=(
                "Bind knowledge bases to a workspace, or unbind them. Binding grants "
                "nothing: each knowledge base keeps its own owner and sharing, and "
                "only one the user can see may be bound. Find ids with "
                "list_knowledge_bases."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": workspace_ref},
                    "add_knowledge_base_ids": {
                        "type": "string",
                        "description": "Comma-separated knowledge base ids to bind.",
                        "default": "",
                    },
                    "remove_knowledge_base_ids": {
                        "type": "string",
                        "description": "Comma-separated knowledge base ids to unbind.",
                        "default": "",
                    },
                },
                "required": ["workspace_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="list_conversations",
            description=(
                "List the user's own chat conversations, most recent first, with the "
                "workspace each is filed in and the agent it is held with (a deleted "
                "agent by its last name); the current conversation is marked. Filter by "
                "workspace, by unfiled, by a word of the title, by the agent it was held "
                "with or answered in (`agent`: an id or a name, deleted agents included), "
                "or by the video / presentation / report / file it is about (`object_id`, "
                "`object_type`) - how to find the conversations to move, or the one to "
                "read back with read_conversation_history."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "type": "string",
                        "description": f"Only conversations filed here. {workspace_ref}",
                        "default": "",
                    },
                    "unfiled_only": {
                        "type": "boolean",
                        "description": "Only conversations filed in no workspace.",
                        "default": False,
                    },
                    "search": {
                        "type": "string",
                        "description": "Case-insensitive text the title contains.",
                        "default": "",
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "List archived conversations instead.",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"How many to list (1-{MAX_CONVERSATIONS_PER_CALL}).",
                        "default": 20,
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Only conversations held with, or answered in by, this agent: its "
                            "id, or a part of its name (a deleted agent's last name too)."
                        ),
                        "default": "",
                    },
                    "object_id": {
                        "type": "string",
                        "description": (
                            "Only conversations about this video, presentation, report or file "
                            "id: bound to it, or whose messages carry it."
                        ),
                        "default": "",
                    },
                    "object_type": {
                        "type": "string",
                        "enum": ["", "video", "presentation", "report", "file"],
                        "description": "Only conversations that carry an object of this kind.",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="move_conversations_to_workspace",
            description=(
                "File conversations of the user into a workspace, or take them out of "
                "any workspace with workspace_id='none'. With no conversation_ids it "
                "moves the current conversation. Only the user's own conversations, "
                "into a workspace they can manage that is not archived; all or nothing. "
                "Into a workspace shared with other people, the person is asked to "
                "approve the call before anything moves."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "type": ["string", "null"],
                        "description": f"{workspace_ref} null or 'none' to unfile.",
                    },
                    "conversation_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated conversation ids (from list_conversations); "
                            "'current' is this conversation. Empty: this conversation."
                        ),
                        "default": "",
                    },
                },
                "required": ["workspace_id"],
            },
            source=_source(),
        ),
    ]
    async def _file_this_conversation(workspace_id: str = "") -> str:
        """File the conversation the turn runs in (spec 032 FR-027b).

        ``move_conversations_to_workspace`` on the current conversation - its
        rules, the shared-workspace approval included (FR-022) - except a group
        channel thread, which never works with a workspace (FR-027c): filing it
        would promise a knowledge and a search it never gets.
        """
        from sqlalchemy import select

        from app.core.database import async_session as _session
        from app.models.chat import Conversation
        from app.services.workspace_knowledge import private_thread_clause

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        if current_conversation is None:
            return (
                "This conversation is not recorded yet - a new channel thread is, "
                "after its first answer. Ask again in your next message."
            )
        async with _session() as _db:
            group_thread = (
                await _db.execute(
                    select(Conversation.id).where(
                        Conversation.id == current_conversation,
                        Conversation.user_id == uid,
                        ~private_thread_clause(),
                    )
                )
            ).scalar_one_or_none()
        if group_thread is not None:
            return (
                "This is a channel thread other people read, so it cannot work with a "
                "workspace's knowledge or be searched in it. File a direct conversation "
                "with me instead. Nothing was moved."
            )
        return await _move_conversations_to_workspace(workspace_id, "")

    handlers = {
        "list_workspaces": _list_workspaces,
        "get_workspace": _get_workspace,
        "create_workspace": _create_workspace,
        "update_workspace": _update_workspace,
        "delete_workspace": _delete_workspace,
        "set_default_workspace": _set_default_workspace,
        "update_workspace_knowledge_bases": _update_workspace_knowledge_bases,
        "list_conversations": _list_conversations,
        "move_conversations_to_workspace": _move_conversations_to_workspace,
        # Offered by the ``workspace_filing`` suite, which declares it.
        "file_this_conversation": _file_this_conversation,
    }
    return definitions, handlers
