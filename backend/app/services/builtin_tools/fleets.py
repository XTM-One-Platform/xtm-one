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

"""The ``fleets`` built-in suite: an agent creates and runs fleets.

Eight tools over the fleet rules of ``app.services.fleets`` (phases 1a/1b),
the ones ``api/v1/fleets.py`` applies: ``list_fleets``, ``get_fleet``,
``create_fleet``, ``update_fleet``, ``delete_fleet``, ``set_fleet_members``,
``set_fleet_exposure``, ``share_fleet_workspace`` (the fleet and its workspace
shared in one grant, administrator or workspace owner only) and
``set_fleet_member_parent`` (the hierarchy
``PATCH /fleets/{id}/members/{member_id}`` writes), plus ``move_fleet_member``
(``POST /fleets/{id}/members/{member_id}/move``, one transaction),
``set_fleet_automations`` (Pause all / Resume of ``POST /fleets/{id}/pause|resume``,
a pause applied only once the user confirmed), ``export_fleet_spec`` and
``apply_fleet_spec`` (the fleet as code of
``app.services.fleet_spec``), and ``generate_fleet_spec`` (phase 4b: a fleet
from a prompt, previewed as a dry run by ``app.services.fleet_spec_generation``,
applied by ``apply_fleet_spec``). Nothing here decides a rule: one home fleet per
agent, a company-managed fleet holding company-managed agents only and
coordinating in a company-managed workspace, the orchestrator a home member
and always exposed, are the service's refusals, handed back to the model as
they are.

Permissions are the agent builder's (``_load_manageable_entity_for_builder``,
the string-returning ``get_manageable_entity``): a fleet is read when the
caller sees it and changed when the caller may manage it (owner, ``config``
grantee, admin; company-managed fleets admin-only); a home member is an agent
the caller may manage, a connected agent one the caller sees, as on the REST
routes. Each call opens its own short session, commits once, and rolls back
whole on a refusal: a fleet is never left half-created. Writes are audited like
the REST ones, marked ``via: agent``. Every answer names the fleet's page.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core import database
from app.core.utils import safe_uuid
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from . import agent_builder as _builder
from ._shared import (
    _platform_label,
    load_scoped_file_ref,
    logger,
    persist_tool_artifact,
    resolve_file_ref,
    unresolved_file_message,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.models.fleet import Fleet
    from app.models.user import User
    from app.services.fleet_doc_on_create import DocGeneration
    from app.tools.base import FileContext

FLEETS_SUITE_ID = "fleets"

_ANONYMOUS_REFUSAL = "Fleet management is not available for anonymous users."
_FAILED = "The fleet change failed. Nothing was saved; please try again."
#: A tool lowers a fleet's autonomy freely; it raises it only for an
#: administrator, in a chat, once the administrator confirmed (#10439). Any
#: other raise (a non-administrator, an assignment run) is refused.
_AUTONOMY_RAISE_REFUSAL = (
    "An agent can only lower a fleet's autonomy level. Raising it is an administrator's choice: "
    "ask the user to change it on the fleet page. Nothing was saved."
)
#: Why a raise was refused, appended to :data:`_AUTONOMY_RAISE_REFUSAL` with the
#: level kept, so the caller knows which condition failed and what to do.
_RAISE_NOT_ADMIN = (
    "Why: the user is not an administrator. To do: ask an administrator to raise it on the fleet page, "
    "or from a chat with confirm=true."
)
_RAISE_NOT_IN_CHAT = (
    "Why: this call runs in an assignment or background run, not a chat. To do: an administrator asks "
    "for it in a chat and confirms, or raises it on the fleet page."
)
_RAISE_NO_CHAT = (
    "Why: this call is not in a chat. To do: an administrator asks for it in a chat and confirms, "
    "or raises it on the fleet page."
)
#: Where a fleet's instructions put it is recorded by an agent only for an
#: administrator (#10255): the REST route lets a manager lower it, a tool does not.
_INSTRUCTIONS_POSITION_ADMIN_ONLY = (
    "Only an administrator's agent can set a fleet's instructions position. Nothing was saved."
)
#: Who may share a fleet's workspace from a tool (#24): never wider than the
#: Share dialog, so an administrator, or the workspace's owner who may manage the fleet.
_SHARE_NOT_ALLOWED = (
    "Only a platform administrator or the owner of the fleet's workspace can share it from an agent. "
    "Nothing was changed: ask an administrator, or the owner from the fleet page."
)

#: Field widths of the model (``app.models.fleet``).
_NAME_MAX = 200
#: The most agents one call places in a fleet.
_MAX_AGENTS_PER_CALL = 100
_CONTEXT_INCLUSIONS = ("none", "summary", "full")
#: The largest spec ``export_fleet_spec`` answers inline. A larger one
#: (a fleet of long personas weighs megabytes) is answered as its structure,
#: the full spec written to a file of the conversation or run.
SPEC_INLINE_MAX_CHARS = 60_000


async def documentation_hint(db: AsyncSession, fleet: Fleet) -> str:
    """One answer line when *fleet*'s documentation no longer matches its configuration, else ''.

    Every fleet write answer (``_describe``) and an applied spec end with it, so
    an orchestrator that changed its fleet knows to call
    ``regenerate_fleet_documentation`` once (#9768 S3). A fleet with no
    generated documentation (no fingerprint) or a built-in one gets no hint.
    """
    from app.services.fleet_docs import current_fingerprint

    if getattr(fleet, "is_builtin", False):
        # The seed owns a built-in fleet's structure and documentation (#1956).
        return ""
    stored = getattr(fleet, "documentation", None)
    fingerprint = stored.get("fingerprint") if isinstance(stored, dict) else None
    if not fingerprint or fingerprint == await current_fingerprint(db, fleet):
        return ""
    return (
        "  • documentation_out_of_date: true. Once your changes are done, call "
        f'regenerate_fleet_documentation(fleet_id="{fleet.id}") once.'
    )


def generation_text(state: DocGeneration, *, now: datetime | None = None) -> str:
    """The running documentation generation of a fleet in words, or '' when none runs.

    A generation's server marker lives while its worker refreshes it: a worker
    that stopped frees it within its lifetime, so a generation is never stuck
    for longer and the next ``regenerate_fleet_documentation`` call starts.
    """
    from app.services.fleet_doc_on_create import GENERATING_TTL_SECONDS

    if not state.generating:
        return ""
    started = "start time unknown"
    if state.started_at:
        try:
            since = datetime.fromisoformat(state.started_at)
            elapsed = max(0, int(((now or datetime.now(UTC)) - since).total_seconds()))
            started = f"started {state.started_at} UTC, running for {elapsed // 60} min {elapsed % 60} s"
        except ValueError:
            started = f"started {state.started_at}"
    return (
        f"  • documentation_generating: true ({started}). No ETA: it ends when the model has answered every section. "
        f"Its worker keeps it alive; a stopped worker frees it within {GENERATING_TTL_SECONDS // 60} min, "
        "then regenerate_fleet_documentation starts a new one."
    )


class _RefusedError(Exception):
    """A refusal to hand back to the model; the session is rolled back."""


def _source() -> str:
    # The fleet functions belong to the platform group; ``fleets`` is
    # its [DEPRECATED] alias (``builtin_tool_aliases``).
    return "builtin:platform"


def fleet_link(fleet_id: uuid.UUID | str) -> str:
    """The fleet's page in the app, the link every answer names."""
    from app.core.config import settings

    base = (settings.frontend_url or settings.base_url or "").rstrip("/")
    return f"{base}/fleets/{fleet_id}"


def _id_list(raw: object, label: str) -> list[uuid.UUID]:
    """A list of ids as a model writes one: a JSON array or a comma list."""
    if raw is None or raw == "":
        return []
    items = raw if isinstance(raw, list) else str(raw).split(",")
    out: list[uuid.UUID] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        parsed = safe_uuid(text)
        if parsed is None:
            raise _RefusedError(f"{label}: `{text}` is not an id.")
        if parsed not in out:
            out.append(parsed)
    if len(out) > _MAX_AGENTS_PER_CALL:
        raise _RefusedError(f"{label}: at most {_MAX_AGENTS_PER_CALL} ids per call.")
    return out


def _one_id(raw: object, label: str) -> uuid.UUID:
    parsed = safe_uuid(str(raw or "").strip())
    if parsed is None:
        raise _RefusedError(f"{label} is required: pass the id `list_fleets` shows.")
    return parsed


def _bool(raw: object, label: str, default: bool) -> bool:
    value, err, provided = _builder._parse_bool_arg(raw, label)
    if err:
        raise _RefusedError(err)
    return bool(value) if provided else default


#: An argument the model did not pass (``None`` is a value: null puts a member at top level).
_UNSET: Any = object()


def _level_text(value: object) -> str:
    """A fleet's autonomy level as an answer states it: label and stored value."""
    from app.services.fleet_autonomy import LEVEL_LABELS, effective_level

    level = effective_level(value)
    return f"{LEVEL_LABELS[level]} ({level})"


def _instructions_text(position: object, announced: object) -> str:
    """A fleet's instructions position as an answer states it: label, stored value, announced or not.

    Display only (never a platform gate); unset reads "not set", so a reader
    needs no audit log to learn it (#10000).
    """
    from app.services.fleet_autonomy import INSTRUCTIONS_POSITION_LABELS, INSTRUCTIONS_POSITIONS

    if position not in INSTRUCTIONS_POSITIONS:
        return "not set"
    told = "announced to the members" if announced else "not announced to the members"
    return f"{INSTRUCTIONS_POSITION_LABELS[position]} ({position}), {told}, not enforced by the platform"


def _quoted_label(label: str) -> str:
    """A role label as an answer quotes it; an empty one reads as none."""
    return f'"{label}"' if label else "none"


def _scope_text(scope: dict) -> str:
    """An organization scope as an agent reads it: the mode, and the ids of a list."""
    if scope.get("mode") == "list":
        return "list " + ", ".join(f"`{i}`" for i in scope.get("organization_ids") or [])
    return str(scope.get("mode") or "primary")


_MEMBER_SPEC_KEYS = ("agent_id", "role_label", "parent_agent_id")


def _member_specs(raw: object) -> list[dict[str, Any]]:
    """``members`` of ``create_fleet`` as ``{agent_id, role_label, parent_agent_id}`` (#371).

    A key outside those three is refused by name, never dropped: the fleet would be created
    without what the model asked for. A setting left out is ``None`` (no label, top level).
    """
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise _RefusedError("member_agent_ids must be an array of objects with an agent_id.")
    specs: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"agent_id": item}
        if not isinstance(item, dict):
            raise _RefusedError("member_agent_ids must be an array of objects with an agent_id.")
        unknown = sorted(str(key) for key in item if key not in _MEMBER_SPEC_KEYS)
        if unknown:
            raise _RefusedError(
                f"member_agent_ids: unknown field(s) {', '.join(unknown)}; a member takes "
                f"{', '.join(_MEMBER_SPEC_KEYS)}. Nothing was created."
            )
        agent_id = _one_id(item.get("agent_id"), "member_agent_ids agent_id")
        if agent_id in seen:
            raise _RefusedError(f"member_agent_ids: agent `{agent_id}` is listed twice. Nothing was created.")
        seen.add(agent_id)
        raw_label = item.get("role_label")
        label = None if raw_label is None else " ".join(str(raw_label).split())
        raw_parent = item.get("parent_agent_id")
        parent_text = "" if raw_parent is None else str(raw_parent).strip()
        parent = None
        if parent_text and parent_text.lower() not in ("none", "null"):
            parent = safe_uuid(parent_text)
            if parent is None:
                raise _RefusedError(
                    f"member_agent_ids parent_agent_id: `{parent_text}` is not an id. Nothing was created."
                )
            if parent == agent_id:
                raise _RefusedError(
                    f"member_agent_ids: agent `{agent_id}` cannot report to itself. Nothing was created."
                )
        specs.append({"agent_id": agent_id, "role_label": label, "parent_agent_id": parent})
    if len(specs) > _MAX_AGENTS_PER_CALL:
        raise _RefusedError(f"member_agent_ids: at most {_MAX_AGENTS_PER_CALL} agents per call.")
    return specs


def _connected_specs(raw: object) -> list[dict[str, Any]]:
    """``connected`` as objects ``{agent_id, collaboration_instruction, context_inclusion}``.

    A setting left out is ``None``: the row inherits the fleet's member default.
    """
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise _RefusedError("connected must be an array of objects with an agent_id.")
    specs: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            item = {"agent_id": item}
        if not isinstance(item, dict):
            raise _RefusedError("connected must be an array of objects with an agent_id.")
        agent_id = _one_id(item.get("agent_id"), "connected agent_id")
        raw_inclusion = item.get("context_inclusion")
        inclusion = str(raw_inclusion).strip().lower() if raw_inclusion is not None else None
        if inclusion is not None and inclusion not in _CONTEXT_INCLUSIONS:
            raise _RefusedError(f"context_inclusion is one of {', '.join(_CONTEXT_INCLUSIONS)}.")
        raw_instruction = item.get("collaboration_instruction")
        instruction = str(raw_instruction).strip() if raw_instruction is not None else None
        if instruction is not None and len(instruction) > 4000:
            raise _RefusedError("collaboration_instruction is at most 4000 characters.")
        specs.append(
            {"agent_id": agent_id, "collaboration_instruction": instruction, "context_inclusion": inclusion}
        )
    if len(specs) > _MAX_AGENTS_PER_CALL:
        raise _RefusedError(f"connected: at most {_MAX_AGENTS_PER_CALL} agents per call.")
    return specs


def build_fleet_tools(
    user_id: str,
    *,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The ``fleets`` suite for *user_id*, as definitions and their executor.

    *conversation_id* / *assignment_run_id* are the scope ``export_fleet_spec``
    writes the full spec file under and ``apply_fleet_spec`` reads a ``file_id``
    from; without either, no file is written and only the files of the
    turn's context are read. *agent_id* is the agent acting for the user,
    named in the audit of ``share_fleet_workspace``.
    """
    platform = _platform_label()
    acting_agent_id = str(agent_id) if agent_id else None

    def _caller_id() -> uuid.UUID | None:
        return safe_uuid(user_id) if user_id else None

    async def _run(action: Callable[[AsyncSession, User], Awaitable[str]], *, write: bool) -> str:
        """Open a session, load the caller, run *action*; a refusal rolls back."""
        from app.services.fleets import FleetActionRefusedError

        uid = _caller_id()
        if uid is None:
            return _ANONYMOUS_REFUSAL
        async with database.async_session() as db:
            user = await _builder._load_caller_user_for_builder(db, uid)
            if user is None:
                return "User not found."
            try:
                answer = await action(db, user)
                if write:
                    await db.commit()
                return answer
            except _RefusedError as refusal:
                await db.rollback()
                return str(refusal)
            except FleetActionRefusedError as refusal:
                await db.rollback()
                return f"{refusal.detail} Nothing was saved."
            except Exception:
                logger.exception("fleets tool failed for user %s", uid)
                await db.rollback()
                return _FAILED

    async def _document_created(fleet_ids: list[uuid.UUID]) -> None:
        """Schedule the first documentation of each fleet created and committed by the call (#10316)."""
        uid = _caller_id()
        if not fleet_ids or uid is None:
            return
        from app.services.fleet_doc_on_create import schedule_documentation_on_create

        async with database.async_session() as db:
            for fleet_id in fleet_ids:
                await schedule_documentation_on_create(db, fleet_id, uid)

    # ── Shared loaders ────────────────────────────────────────────────

    async def _group_ids(db: AsyncSession, user: User) -> list[str]:
        from app.api.v1.helpers import get_user_group_ids

        return await get_user_group_ids(db, user.id)

    async def _visible_fleet(db: AsyncSession, user: User, fleet_id: uuid.UUID) -> Fleet:
        from sqlalchemy import select

        from app.api.v1.helpers import agent_tool_admin_bypass, user_visible_filter
        from app.models.fleet import Fleet

        group_ids = await _group_ids(db, user)
        fleet = (
            await db.execute(
                select(Fleet).where(
                    Fleet.id == fleet_id,
                    user_visible_filter(Fleet, user.id, group_ids, is_admin=agent_tool_admin_bypass(user)),
                )
            )
        ).scalar_one_or_none()
        if fleet is None:
            raise _RefusedError(f"Fleet `{fleet_id}` not found or you don't have access.")
        return fleet

    async def _manageable_fleet(
        db: AsyncSession,
        user: User,
        fleet_id: uuid.UUID,
        action: str = "edit",
        fields: set[str] | None = None,
        *,
        builtin_ok: bool = False,
        lock: bool = True,
    ) -> Fleet:
        """A fleet the caller may write, locked; a built-in one only for its writable *fields*.

        *builtin_ok*: a write a built-in fleet takes whatever it is (its
        documentation, regenerated as a local version, as the REST route does).
        *lock*: False for a write serialized another way, which must not hold
        the row for its whole duration (a documentation generation, #429).
        """
        from app.models.fleet import Fleet
        from app.services.fleets import ensure_builtin_fleet_writable

        # Every caller writes the fleet: hold its row FOR UPDATE until the commit,
        # so two concurrent roster or exposure replacements are serialized
        # instead of committing the union of their snapshots.
        fleet, err = await _builder._load_manageable_entity_for_builder(
            db, Fleet, user.id, fleet_id, action=action, entity_label="fleet", lock=lock
        )
        if err is not None or fleet is None:
            raise _RefusedError(err or f"Fleet `{fleet_id}` not found or you don't have access.")
        if not builtin_ok:
            ensure_builtin_fleet_writable(fleet, fields)
        return fleet

    async def _manageable_agent(db: AsyncSession, user: User, agent_id: uuid.UUID):
        from app.models.agent import Agent

        agent, err = await _builder._load_manageable_entity_for_builder(
            db, Agent, user.id, agent_id, action="add to a fleet", entity_label="agent"
        )
        if err is not None or agent is None:
            raise _RefusedError(err or f"agent `{agent_id}` not found or you don't have access.")
        return agent

    async def _visible_agent(db: AsyncSession, user: User, agent_id: uuid.UUID):
        from sqlalchemy import select

        from app.api.v1.helpers import agent_tool_admin_bypass, user_visible_filter
        from app.models.agent import Agent

        group_ids = await _group_ids(db, user)
        agent = (
            await db.execute(
                select(Agent).where(
                    Agent.id == agent_id,
                    user_visible_filter(Agent, user.id, group_ids, is_admin=agent_tool_admin_bypass(user)),
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise _RefusedError(f"agent `{agent_id}` not found or you don't have access.")
        return agent

    async def _host_workspace(db: AsyncSession, user: User, workspace_id: uuid.UUID, *, company_managed: bool):
        from app.models.workspace import Workspace
        from app.services.fleets import ensure_workspace_can_host
        from app.services.workspaces import lock_workspace_for_hosting

        workspace, err = await _builder._load_manageable_entity_for_builder(
            db, Workspace, user.id, workspace_id, action="bind a fleet to", entity_label="workspace"
        )
        if err is not None or workspace is None:
            raise _RefusedError(err or f"workspace `{workspace_id}` not found or you don't have access.")
        # Serialised with an archive of the same workspace.
        await lock_workspace_for_hosting(db, workspace)
        ensure_workspace_can_host(company_managed, workspace)
        return workspace

    async def _audit(db: AsyncSession, user: User, fleet: Fleet, action: str, summary: str, **details: Any) -> None:
        from app.services.audit import log_audit

        await log_audit(
            db,
            user=user,
            action=action,
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=summary,
            details={"slug": fleet.slug, **details, "via": "agent"},
        )

    async def _agent_names(db: AsyncSession, user: User, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        from sqlalchemy import select

        from app.api.v1.helpers import agent_tool_admin_bypass, user_visible_filter
        from app.models.agent import Agent

        if not agent_ids:
            return {}
        group_ids = await _group_ids(db, user)
        rows = await db.execute(
            select(Agent.id, Agent.name).where(
                Agent.id.in_(agent_ids),
                user_visible_filter(Agent, user.id, group_ids, is_admin=agent_tool_admin_bypass(user)),
            )
        )
        return dict(rows.all())

    async def _describe(db: AsyncSession, user: User, fleet: Fleet, *, headline: str) -> str:
        """The fleet as every answer shows it: who, where, exposure, link."""
        from app.models.fleet import FLEET_ROLE_MEMBER
        from app.models.workspace import Workspace
        from app.services.fleets import (
            effective_member_settings,
            exposed_agent_ids,
            fleet_members,
            resolve_organization_scope,
        )

        rows = await fleet_members(db, fleet.id)
        names = await _agent_names(db, user, [r.agent_id for r in rows if r.agent_id is not None])
        workspace = await db.get(Workspace, fleet.workspace_id)
        homes = [r for r in rows if r.role == FLEET_ROLE_MEMBER]
        exposed = exposed_agent_ids(fleet, homes)

        def _name(agent_id: uuid.UUID | None) -> str:
            if agent_id is None:
                return "(a fleet)"
            return names.get(agent_id) or "(an agent you cannot see)"

        lines = [
            headline,
            f"  • ID: `{fleet.id}`",
            f"  • Page: {fleet_link(fleet.id)}",
            f"  • Workspace: {workspace.name if workspace else '?'} (`{fleet.workspace_id}`)",
            f"  • Orchestrator: {_name(fleet.orchestrator_agent_id) if fleet.orchestrator_agent_id else 'none'}",
        ]
        if fleet.description:
            lines.append(f"  • Description: {fleet.description}")
        if fleet.is_company_managed:
            lines.append("  • Company-managed")
        if fleet.is_archived:
            lines.append("  • Archived")
        lines.append(f"  • Autonomy level: {_level_text(getattr(fleet, 'autonomy_level', None))}")
        lines.append(
            "  • Instructions position: "
            + _instructions_text(
                getattr(fleet, "instructions_position", None),
                getattr(fleet, "instructions_position_announced", None),
            )
        )
        fleet_scope = resolve_organization_scope(fleet.organization_scope, None)
        lines.append(f"  • Organization scope: {_scope_text(fleet_scope)}")
        lines.append(f"  • Home members ({len(homes)}):")
        by_member = {r.id: r for r in homes}
        # Same reading as fleet_views.fleet_tree: a member with no parent (or a
        # parent outside the home members) is drawn under the orchestrator.
        has_orchestrator = any(r.agent_id == fleet.orchestrator_agent_id for r in homes)
        for row in homes:
            is_orchestrator = row.agent_id == fleet.orchestrator_agent_id
            marks = [m for m, on in (("orchestrator", is_orchestrator),
                                     ("exposed", row.agent_id in exposed)) if on]
            if row.organization_scope is not None:
                marks.append(f"scope {_scope_text(resolve_organization_scope(None, row.organization_scope))}")
            parent = by_member.get(row.parent_member_id) if row.parent_member_id else None
            if parent is not None and parent.id != row.id and not is_orchestrator:
                reports = f", reports to {_name(parent.agent_id)} `{parent.agent_id}`"
            elif has_orchestrator and not is_orchestrator:
                reports = ", top level, under the orchestrator"
            else:
                reports = ", top level"
            lines.append(
                f"      - {_name(row.agent_id)} `{row.agent_id}` (member `{row.id}`{reports})"
                + (f" ({', '.join(marks)})" if marks else "")
                + (f", role label: {row.role_label}" if row.role_label else "")
            )
        connected = [r for r in rows if r.role != FLEET_ROLE_MEMBER]
        if connected:
            lines.append(f"  • Connected ({len(connected)}):")
            for row in connected:
                target = f"`{row.agent_id}`" if row.agent_id else f"fleet `{row.target_fleet_id}`"
                settings = effective_member_settings(fleet, row)
                origin = "" if "context_inclusion" in settings.overridden else " (fleet default)"
                scope = (
                    f", scope {_scope_text(settings.organization_scope)}"
                    if "organization_scope" in settings.overridden
                    else ""
                )
                lines.append(
                    f"      - {_name(row.agent_id)} {target}, context {settings.context_inclusion}{origin}{scope}"
                )
        hint = await documentation_hint(db, fleet)
        if hint:
            lines.append(hint)
        from app.services.fleet_doc_on_create import generation_state

        running = generation_text(await generation_state(fleet.id))
        if running:
            lines.append(running)
        return "\n".join(lines)

    async def _replace_members(
        db: AsyncSession, user: User, fleet: Fleet, wanted: list[uuid.UUID], *, keep_orchestrator: uuid.UUID | None
    ) -> None:
        """Make the home members exactly *wanted* (plus the orchestrator kept)."""
        from app.models.fleet import FLEET_ROLE_MEMBER
        from app.services.fleets import add_home_member, fleet_members, remove_member

        if keep_orchestrator is not None and keep_orchestrator not in wanted:
            wanted = [keep_orchestrator, *wanted]
        rows = [r for r in await fleet_members(db, fleet.id) if r.role == FLEET_ROLE_MEMBER]
        present = {r.agent_id for r in rows}
        for row in rows:
            if row.agent_id not in wanted:
                await remove_member(db, fleet, row.id)
        for agent_id in wanted:
            if agent_id not in present:
                await add_home_member(db, fleet, await _manageable_agent(db, user, agent_id))

    async def _apply_member_specs(
        db: AsyncSession,
        fleet: Fleet,
        specs: list[dict[str, Any]],
        orchestrator: uuid.UUID | None,
    ) -> None:
        """Role labels and parents of ``create_fleet``'s ``members`` (#371), through the member service.

        ``update_member`` applies the API's rules (self, connected parent, loop); a refusal rolls the
        whole creation back.
        """
        from app.models.fleet import FLEET_ROLE_MEMBER
        from app.services.fleets import fleet_members, update_member

        homes = {r.agent_id: r for r in await fleet_members(db, fleet.id) if r.role == FLEET_ROLE_MEMBER}
        for spec in specs:
            row = homes[spec["agent_id"]]
            fields: dict[str, Any] = {}
            if spec["role_label"] is not None:
                fields["role_label"] = spec["role_label"]
            parent_id = spec["parent_agent_id"]
            if parent_id is not None:
                if spec["agent_id"] == orchestrator:
                    raise _RefusedError(
                        "The orchestrator is the root of the fleet and reports to no one. Nothing was created."
                    )
                parent = homes.get(parent_id)
                if parent is None:
                    raise _RefusedError(
                        f"member_agent_ids: parent `{parent_id}` is not a home member of this fleet: a member reports to "
                        "another home member of the same fleet. Nothing was created."
                    )
                fields["parent_member_id"] = parent.id
            if fields:
                await update_member(db, fleet, row.id, fields=fields)

    async def _replace_connected(db: AsyncSession, user: User, fleet: Fleet, specs: list[dict[str, Any]]) -> None:
        from app.models.fleet import FLEET_ROLE_CONNECTED
        from app.services.fleets import add_connected, fleet_members, remove_member

        for row in await fleet_members(db, fleet.id):
            if row.role == FLEET_ROLE_CONNECTED and row.agent_id is not None:
                await remove_member(db, fleet, row.id)
        for spec in specs:
            await add_connected(
                db,
                fleet,
                agent=await _visible_agent(db, user, spec["agent_id"]),
                collaboration_instruction=spec["collaboration_instruction"],
                context_inclusion=spec["context_inclusion"],
            )

    # ── list_fleets ───────────────────────────────────────────────────

    async def _list_fleets(include_archived: object = False) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import agent_tool_admin_bypass, user_visible_filter
        from app.models.fleet import Fleet
        from app.models.workspace import Workspace

        async def _action(db: AsyncSession, user: User) -> str:
            archived = _bool(include_archived, "include_archived", False)
            group_ids = await _group_ids(db, user)
            query = select(Fleet).where(
                user_visible_filter(Fleet, user.id, group_ids, is_admin=agent_tool_admin_bypass(user))
            )
            if not archived:
                query = query.where(Fleet.is_archived.is_(False))
            fleets = list((await db.execute(query.order_by(Fleet.name, Fleet.id))).scalars().all())
            if not fleets:
                return (
                    "No fleet yet. Create one with `create_fleet`: a name, an orchestrator and its "
                    "members (a workspace named after the fleet is created when none is given)."
                )
            workspaces = dict(
                (
                    await db.execute(
                        select(Workspace.id, Workspace.name).where(Workspace.id.in_({f.workspace_id for f in fleets}))
                    )
                ).all()
            )
            names = await _agent_names(db, user, [f.orchestrator_agent_id for f in fleets if f.orchestrator_agent_id])
            lines = [f"{len(fleets)} fleet(s):"]
            for f in fleets:
                orchestrator = names.get(f.orchestrator_agent_id, "none") if f.orchestrator_agent_id else "none"
                flags = "".join(
                    [", company-managed" if f.is_company_managed else "", ", archived" if f.is_archived else ""]
                )
                level = _level_text(getattr(f, "autonomy_level", None))
                lines.append(
                    f"  • **{f.name}** `{f.id}`: orchestrator {orchestrator}, workspace "
                    f"{workspaces.get(f.workspace_id, '?')}, autonomy {level}{flags}. {fleet_link(f.id)}"
                )
            return "\n".join(lines)

        return await _run(_action, write=False)

    # ── get_fleet ─────────────────────────────────────────────────────

    async def _automation_state(db: AsyncSession, user: User, fleet: Fleet):
        """The fleet's opt-in badge (one computation with the UI), agent names as the caller may see them."""
        from dataclasses import replace

        from app.services.fleet_views import fleet_automation_state

        state = await fleet_automation_state(db, fleet)
        names = await _agent_names(db, user, sorted({o.agent_id for o in state.off}))
        return replace(
            state,
            off=tuple(replace(o, agent_name=names.get(o.agent_id) or "(an agent you cannot see)") for o in state.off),
        )

    async def _suggestion_lines(db: AsyncSession, user: User, fleet: Fleet) -> list[str]:
        """The About page's open suggestions, for a manager only, as the chip counts them (#12099)."""
        from app.api.v1.helpers import can_manage
        from app.services.fleet_docs import open_suggestion_count

        if not can_manage(user, fleet, await _group_ids(db, user)):
            return []
        count = await open_suggestion_count(db, fleet)
        if not count:
            return []
        return [f"  • Suggestions: {count} open on the About page; list_fleet_doc_findings reads them."]

    def _automation_lines(state) -> list[str]:
        """The badge as get_fleet states it, then one line per counted automation that is off."""
        from app.services.fleet_views import switch_on_action

        c = state.counts
        lines = [
            f"  • Automations: {state.label}, {c.enabled} of {c.total} on"
            + (f", {c.paused} paused ({c.fleet_paused} by Pause all)" if c.paused else "")
            + " (members with no assignment count nowhere)"
        ]
        if c.dashboard_total:
            lines.append(
                f"  • Dashboard schedules (not in the badge, they ship on): {c.dashboard_enabled} of "
                f"{c.dashboard_total} on; Pause all switches them off, Resume back on"
            )
        for off in state.off:
            lines.append(
                f"      - off: {off.assignment_name} `{off.assignment_id}` of {off.agent_name} `{off.agent_id}`, "
                f"reason {off.reason or 'none'}; switch on: {switch_on_action(off.reason)}"
            )
        return lines

    async def _get_fleet(fleet_id: str) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            fleet = await _visible_fleet(db, user, _one_id(fleet_id, "fleet_id"))
            text = await _describe(db, user, fleet, headline=f"Fleet **{fleet.name}**")
            state = await _automation_state(db, user, fleet)
            return "\n".join([text, *_automation_lines(state), *await _suggestion_lines(db, user, fleet)])

        return await _run(_action, write=False)

    # ── create_fleet ──────────────────────────────────────────────────

    async def _create_fleet(
        name: str,
        description: str = "",
        workspace_id: str = "",
        orchestrator_agent_id: str = "",
        member_agent_ids: object = None,
        connected: object = None,
        exposed_agent_ids: object = None,
        is_company_managed: object = False,
        autonomy_level: str | None = None,
    ) -> str:
        from app.models.fleet import FLEET_DEFAULT_ICON, FleetMember
        from app.services.fleet_autonomy import normalize_level
        from app.services.fleets import FleetActionRefusedError, create_fleet, set_exposed_agents, set_orchestrator
        from app.services.workspaces import create_workspace

        async def _action(db: AsyncSession, user: User) -> str:
            title = (name or "").strip()
            if not title:
                raise _RefusedError("A fleet needs a name.")
            if len(title) > _NAME_MAX:
                raise _RefusedError(f"Fleet name is too long ({len(title)} characters, at most {_NAME_MAX}).")
            company = _bool(is_company_managed, "is_company_managed", False)
            if company and not user.is_admin:
                raise _RefusedError("Only admins can create company-managed fleets.")
            # #379: the level is set in the creation's transaction, never autonomous first then lowered.
            level_fields: dict[str, str] = {}
            if autonomy_level is not None and str(autonomy_level).strip():
                try:
                    level_fields["autonomy_level"] = normalize_level(str(autonomy_level).strip())
                except FleetActionRefusedError as exc:
                    raise _RefusedError(f"{exc.detail} Nothing was created.") from None
            # #371: an object member carries its role label and parent; plain ids keep their path.
            if isinstance(member_agent_ids, list) and any(isinstance(i, dict) for i in member_agent_ids):
                member_specs = _member_specs(member_agent_ids)
                members_ids = [s["agent_id"] for s in member_specs]
            else:
                member_specs = []
                members_ids = _id_list(member_agent_ids, "member_agent_ids")
            label_limit = getattr(FleetMember.__table__.c.role_label.type, "length", None) or 100
            for spec in member_specs:
                if spec["role_label"] is not None and len(spec["role_label"]) > label_limit:
                    raise _RefusedError(
                        f"member_agent_ids role_label is {len(spec['role_label'])} characters, at most {label_limit}. "
                        "Nothing was created."
                    )
            orchestrator = safe_uuid(str(orchestrator_agent_id or "").strip()) if orchestrator_agent_id else None
            if orchestrator_agent_id and orchestrator is None:
                raise _RefusedError(f"orchestrator_agent_id: `{orchestrator_agent_id}` is not an id.")
            specs = _connected_specs(connected)
            exposed = _id_list(exposed_agent_ids, "exposed_agent_ids")
            workspace_uuid = safe_uuid(str(workspace_id or "").strip()) if workspace_id else None
            if workspace_id and workspace_uuid is None:
                raise _RefusedError(f"workspace_id: `{workspace_id}` is not an id.")
            if workspace_uuid is not None:
                workspace = await _host_workspace(db, user, workspace_uuid, company_managed=company)
                workspace_created = False
            else:
                workspace = await create_workspace(
                    db,
                    user_id=user.id,
                    name=title,
                    description=f"Where the fleet {title} works.",
                    is_company_managed=company,
                )
                workspace_created = True
            fleet = await create_fleet(
                db,
                user_id=user.id,
                name=title,
                workspace_id=workspace.id,
                description=(description or "").strip(),
                icon=FLEET_DEFAULT_ICON,
                is_company_managed=company,
                **level_fields,
            )
            await _replace_members(db, user, fleet, members_ids, keep_orchestrator=orchestrator)
            if orchestrator is not None:
                await set_orchestrator(db, fleet, orchestrator)
            if member_specs:
                await _apply_member_specs(db, fleet, member_specs, orchestrator)
            if specs:
                await _replace_connected(db, user, fleet, specs)
            if exposed:
                await set_exposed_agents(db, fleet, exposed)
            await db.flush()
            await _audit(
                db,
                user,
                fleet,
                "create",
                f"Created fleet '{fleet.name}'",
                workspace_id=str(workspace.id),
                workspace_created=workspace_created,
                is_company_managed=company,
                autonomy_level=fleet.autonomy_level,
            )
            created.append(fleet.id)
            return await _describe(db, user, fleet, headline=f"Fleet **{fleet.name}** created.")

        created: list[uuid.UUID] = []
        answer = await _run(_action, write=True)
        await _document_created(created)
        return answer

    # ── update_fleet ──────────────────────────────────────────────────

    async def _update_fleet(
        fleet_id: str,
        name: str | None = None,
        description: str | None = None,
        workspace_id: str | None = None,
        orchestrator_agent_id: str | None = None,
        archived: object = None,
        member_defaults: object = None,
        autonomy_level: str | None = None,
        instructions_position: str | None = None,
        announce_instructions: object = False,
        confirm: object = False,
        health_check: str | None = None,
        is_company_managed: object = None,
    ) -> str:
        from app.services.fleet_autonomy import (
            effective_level,
            is_raise,
            normalize_instructions_position,
            normalize_level,
        )
        from app.services.fleets import (
            FleetActionRefusedError,
            check_scope_organizations,
            set_fleet_health_check,
            set_member_defaults,
            set_orchestrator,
        )

        async def _action(db: AsyncSession, user: User) -> str:
            requested = {
                field
                for field, value in (
                    ("name", name),
                    ("description", description),
                    ("workspace_id", workspace_id),
                    ("orchestrator_agent_id", orchestrator_agent_id),
                    ("is_archived", archived),
                    ("member_defaults", member_defaults),
                    ("autonomy_level", autonomy_level),
                    ("instructions_position", instructions_position),
                    ("health_check", health_check),
                    ("is_company_managed", is_company_managed),
                )
                if value is not None
            }
            fleet = await _manageable_fleet(db, user, _one_id(fleet_id, "fleet_id"), fields=requested)
            changed: list[str] = []
            level_change: dict[str, Any] | None = None
            level_kept: str | None = None
            position_change: dict[str, Any] | None = None
            if instructions_position is not None and str(instructions_position).strip():
                # Display and, only when asked, one line in the members' run prompt;
                # never a gate (#10168). An agent sets it for an administrator only,
                # whatever the direction (#10255).
                if not user.is_admin:
                    raise _RefusedError(_INSTRUCTIONS_POSITION_ADMIN_ONLY)
                text = str(instructions_position).strip()
                try:
                    new_position = normalize_instructions_position(None if text.lower() == "none" else text)
                except FleetActionRefusedError as exc:
                    raise _RefusedError(exc.detail) from None
                announce = _bool(announce_instructions, "announce_instructions", False)
                previous_position = getattr(fleet, "instructions_position", None)
                fleet.instructions_position = new_position
                fleet.instructions_position_announced = new_position is not None and announce
                position_change = {
                    "from": previous_position,
                    "to": new_position,
                    "announced": new_position is not None and announce,
                }
                changed.append("instructions_position")
            if autonomy_level is not None and str(autonomy_level).strip():
                try:
                    new_level = normalize_level(str(autonomy_level).strip())
                except FleetActionRefusedError as exc:
                    raise _RefusedError(exc.detail) from None
                previous_level = effective_level(fleet.autonomy_level)
                raised = is_raise(previous_level, new_level)
                if raised:
                    _check_autonomy_raise(user, fleet, previous_level, new_level)
                if new_level != previous_level:
                    fleet.autonomy_level = new_level
                    level_change = {"from": previous_level, "to": new_level}
                    if raised:
                        level_change["raised_by_agent_on_instruction_of"] = str(user.id)
                    changed.append("autonomy_level")
                else:
                    level_kept = previous_level
            if name is not None and name.strip():
                if len(name.strip()) > _NAME_MAX:
                    raise _RefusedError(f"Fleet name is too long (at most {_NAME_MAX} characters).")
                fleet.name = name.strip()
                changed.append("name")
            if description is not None:
                fleet.description = description.strip()
                changed.append("description")
            if orchestrator_agent_id is not None:
                text = orchestrator_agent_id.strip()
                if text.lower() in ("", "none", "null"):
                    await set_orchestrator(db, fleet, None)
                else:
                    await set_orchestrator(db, fleet, _one_id(text, "orchestrator_agent_id"))
                changed.append("orchestrator_agent_id")
            if health_check is not None and str(health_check).strip():
                try:
                    await set_fleet_health_check(db, fleet, str(health_check), actor=str(user.id))
                except FleetActionRefusedError as exc:
                    raise _RefusedError(exc.detail) from None
                changed.append("health_check")
            company, err, company_provided = _builder._parse_bool_arg(is_company_managed, "is_company_managed")
            if err:
                raise _RefusedError(err)
            if company_provided and bool(company) != bool(fleet.is_company_managed):
                # #398: an existing fleet changes audience in place, its id kept.
                await _set_fleet_company_managed(db, user, fleet, bool(company), workspace_id)
                changed.append("is_company_managed")
            if workspace_id is not None and workspace_id.strip():
                target = _one_id(workspace_id, "workspace_id")
                if target != fleet.workspace_id:
                    workspace = await _host_workspace(
                        db, user, target, company_managed=bool(fleet.is_company_managed)
                    )
                    fleet.workspace_id = workspace.id
                    changed.append("workspace_id")
            value, err, provided = _builder._parse_bool_arg(archived, "archived")
            if err:
                raise _RefusedError(err)
            if provided:
                fleet.is_archived = bool(value)
                changed.append("is_archived")
            if member_defaults is not None:
                if not isinstance(member_defaults, dict):
                    raise _RefusedError(
                        "member_defaults is an object: context_inclusion, collaboration_instruction, organization_scope."
                    )
                try:
                    names = set_member_defaults(fleet, member_defaults)
                    if "organization_scope" in names:
                        await check_scope_organizations(db, fleet.organization_scope)
                    if names:
                        changed.append("member_defaults")
                except FleetActionRefusedError as exc:
                    raise _RefusedError(exc.detail) from None
            if not changed:
                if level_kept is not None:
                    raise _RefusedError(
                        f"Nothing to change: fleet **{fleet.name}** is already at autonomy level "
                        f"{_level_text(level_kept)}."
                    )
                raise _RefusedError(
                    "Nothing to change: pass a name, description, workspace_id, orchestrator_agent_id, archived, "
                    "member_defaults, health_check, is_company_managed, an autonomy_level or an instructions_position."
                )
            await db.flush()
            await _audit(db, user, fleet, "update", f"Updated fleet '{fleet.name}'", fields=sorted(changed))
            if level_change is not None:
                # The row the fleet page's level control writes, so the seed keeps a person's choice.
                summary = f"Set the autonomy level of fleet '{fleet.name}' to {level_change['to']}"
                if "raised_by_agent_on_instruction_of" in level_change:
                    who = getattr(user, "full_name", "") or getattr(user, "email", "") or str(user.id)
                    summary = (
                        f"Raised the autonomy level of fleet '{fleet.name}' to {level_change['to']}, "
                        f"by an agent on {who}'s instruction"
                    )
                await _audit(db, user, fleet, "autonomy_level_change", summary, **level_change)
            if position_change is not None:
                # The row the Approval tab's instructions position writes.
                await _audit(
                    db,
                    user,
                    fleet,
                    "instructions_position_change",
                    f"Set the instructions position of fleet '{fleet.name}' to {position_change['to'] or 'not set'}",
                    **position_change,
                )
            headline = f"Fleet **{fleet.name}** updated."
            if "is_company_managed" in changed:
                # Read back from the row as flushed.
                headline += " Company-managed." if fleet.is_company_managed else " Personal again."
            if level_change is not None:
                # Read back from the row as flushed, not from the argument.
                headline += f" Autonomy level applied: {_level_text(fleet.autonomy_level)}."
            return await _describe(db, user, fleet, headline=headline)

        async def _set_fleet_company_managed(
            db: AsyncSession, user: User, fleet: Fleet, company: bool, workspace_id: str | None
        ) -> None:
            """Turn *fleet* company-managed or personal; the creation's rules, refused before any write (#398)."""
            from sqlalchemy import select

            from app.api.v1.helpers import handle_company_managed_toggle
            from app.models.agent import Agent
            from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember
            from app.models.workspace import Workspace
            from app.services.workspaces import lock_workspace_for_hosting

            if not user.is_admin:
                raise _RefusedError("Only admins can change whether a fleet is company-managed. Nothing was saved.")
            if company:
                personal = (
                    (
                        await db.execute(
                            select(Agent.name)
                            .join(FleetMember, FleetMember.agent_id == Agent.id)
                            .where(
                                FleetMember.fleet_id == fleet.id,
                                FleetMember.role == FLEET_ROLE_MEMBER,
                                Agent.is_company_managed.is_(False),
                            )
                            .order_by(Agent.name)
                        )
                    )
                    .scalars()
                    .all()
                )
                if personal:
                    raise _RefusedError(
                        "A company-managed fleet contains only company-managed agents; these home members are "
                        f"personal: {', '.join(personal)}. Make them company-managed (update_agent) or remove "
                        "them first. Nothing was saved."
                    )
                moving = (
                    workspace_id is not None
                    and workspace_id.strip()
                    and (_one_id(workspace_id, "workspace_id") != fleet.workspace_id)
                )
                if not moving:
                    current = (
                        await db.execute(select(Workspace).where(Workspace.id == fleet.workspace_id))
                    ).scalar_one()
                    # Serialised with update_workspace turning it personal, as PUT /fleets/{id} does.
                    await lock_workspace_for_hosting(db, current)
                    if not current.is_company_managed:
                        raise _RefusedError(
                            f"A company-managed fleet needs a company-managed workspace; its workspace "
                            f"**{current.name}** (`{current.id}`) is personal. Make it company-managed first "
                            "(update_workspace is_company_managed=true), or pass workspace_id of a company-managed "
                            "one. Nothing was saved."
                        )
            await handle_company_managed_toggle(
                db, user, fleet, company, agent_field=None, keep_grants=bool(getattr(fleet, "access_restricted", False))
            )

        def _check_autonomy_raise(user: User, fleet: Fleet, previous_level: str, new_level: str) -> None:
            """Refuse a raise unless an administrator asked for it in a chat and confirmed it (#10439)."""
            # Never in an unattended run, never for a non-administrator: the fleet page's rule.
            # Each refusal names the condition that failed and the level kept.
            why = (
                _RAISE_NOT_ADMIN
                if not user.is_admin
                else _RAISE_NOT_IN_CHAT
                if assignment_run_id
                else _RAISE_NO_CHAT
                if not conversation_id
                else None
            )
            if why is not None:
                raise _RefusedError(
                    f"{_AUTONOMY_RAISE_REFUSAL} {why} The fleet stays at {_level_text(previous_level)}."
                )
            confirmed, err, _provided = _builder._parse_bool_arg(confirm, "confirm")
            if err:
                raise _RefusedError(err)
            if not confirmed:
                raise _RefusedError(
                    f"Not applied. Raising the autonomy level of fleet **{fleet.name}** from {previous_level} to "
                    f"{new_level} lets more of its actions run without a person's approval. Show this to the user "
                    "and call again with confirm=true only once they agree. Why: confirm=true is missing. "
                    f"Nothing was saved; the fleet stays at {_level_text(previous_level)}."
                )

        return await _run(_action, write=True)

    # ── set_fleet_automations ─────────────────────────────────────────

    async def _set_fleet_automations(
        fleet_id: str, action: str = "", confirm: object = False, run_as: object = None
    ) -> str:
        from app.services.fleet_control import (
            fleet_control_audit_details,
            pausable_count,
            pause_fleet,
            resume_fleet,
            unset_fleet,
            unsettable_count,
        )

        verb = str(action or "").strip().lower()
        if verb not in ("pause", "resume", "unset", "enable"):
            return "action is pause, resume, unset or enable."
        confirmed, err, _provided = _builder._parse_bool_arg(confirm, "confirm")
        if err:
            return err

        async def _action(db: AsyncSession, user: User) -> str:
            fleet = await _manageable_fleet(
                db, user, _one_id(fleet_id, "fleet_id"), action=f"{verb} the automations of", builtin_ok=True
            )
            # The fleet page's rule: a built-in fleet's seeded automations are an administrator's.
            if getattr(fleet, "is_builtin", False) and not user.is_admin:
                raise _RefusedError(f"Only an administrator can {verb} the automations of a built-in fleet.")
            if verb == "unset":
                return await _unset(db, user, fleet)
            if verb == "enable":
                return await _enable(db, user, fleet)
            if verb == "pause" and not confirmed:
                count = await pausable_count(db, fleet)
                return (
                    f"Not applied. Pause all would switch off {count} automation(s) of fleet **{fleet.name}** "
                    "(runs in flight continue; Resume switches back on exactly these). Show this to the user and "
                    "call again with confirm=true only once they agree."
                )
            if verb == "pause":
                outcome = await pause_fleet(db, fleet, actor=user)
            else:
                # Resume restores what the fleet's own pause switched off: no confirmation needed.
                outcome = await resume_fleet(db, fleet, actor=user)
            if outcome.changed:
                done = "Paused" if verb == "pause" else "Resumed"
                await _audit(
                    db,
                    user,
                    fleet,
                    f"fleet_{verb}",
                    f"{done} {outcome.changed} automation(s) of fleet '{fleet.name}'",
                    **fleet_control_audit_details(outcome, fleet.id),
                )
            done = "paused" if verb == "pause" else "resumed"
            answer = f"Fleet **{fleet.name}**: {outcome.changed} automation(s) {done}."
            if verb == "resume" and not outcome.changed:
                answer += " Nothing was switched off by Pause all, so Resume had nothing to switch on."
            if verb == "resume":
                state = await _automation_state(db, user, fleet)
                answer += "\n" + "\n".join(_automation_lines(state))
                if state.off:
                    answer += "\n  • To switch on the ones still off: action=enable (preview, then confirm=true)."
            return answer + f"\n  • Page: {fleet_link(fleet.id)}"

        async def _enable(db: AsyncSession, user: User, fleet: Fleet) -> str:
            # Enable fleet of the setup dialog: every automation off and not held by an emergency stop,
            # the seeded fleet automations acting in each module as the user chosen for it (#2650).
            from app.services.fleet_control import enable_fleet, plan_fleet_enable
            from app.services.module_run_as import RunAsRefusedError, missing_choice_refusal, resolve

            by_module: dict[str, uuid.UUID] = {}
            choices = run_as if isinstance(run_as, dict) else {}
            for module, raw in choices.items():
                who = user.id if str(raw or "").strip().lower() == "me" else raw
                try:
                    by_module[str(module)] = await resolve(db, actor=user, raw=who, providers=[str(module)])
                except RunAsRefusedError as exc:
                    return f"Not applied: run_as for {module}: {exc}"
            plan = await plan_fleet_enable(db, fleet, run_as_user_id=None, run_as_by_module=by_module)
            if not plan.changed:
                return f"Fleet **{fleet.name}**: every automation is already on, or held by an emergency stop."
            if plan.unchosen:
                return (
                    f"Not applied. {missing_choice_refusal(plan.unchosen)} Ask the user who XTM One acts as "
                    f"in each ({', '.join(plan.unchosen)}), then call again with run_as mapping each "
                    "module to me or a user id."
                )
            names = ", ".join(item.name for item in plan.selected)
            if not confirmed:
                missing = (
                    f" Refused as it stands: you have no connected {', '.join(plan.missing)} integration."
                    if plan.missing
                    else ""
                )
                return (
                    f"Not applied. Enable fleet would switch on {plan.changed} automation(s) of fleet "
                    f"**{fleet.name}**, acting in each module as chosen: {names}.{missing} Show this to the user "
                    "and call again with confirm=true only once they agree."
                )
            outcome = await enable_fleet(db, fleet, actor=user, run_as_user_id=None, run_as_by_module=by_module)
            await _audit(
                db,
                user,
                fleet,
                "fleet_enable",
                f"Enabled {outcome.changed} automation(s) of fleet '{fleet.name}'",
                **fleet_control_audit_details(outcome, fleet.id),
                run_as_by_module={m: str(u) for m, u in by_module.items()},
            )
            await db.flush()
            state = await _automation_state(db, user, fleet)
            return "\n".join(
                [
                    f"Fleet **{fleet.name}**: {outcome.changed} automation(s) enabled: {', '.join(outcome.names)}.",
                    *_automation_lines(state),
                    f"  • Page: {fleet_link(fleet.id)}",
                ]
            )

        async def _unset(db: AsyncSession, user: User, fleet: Fleet) -> str:
            # Back to not set up (#11060): every automation off, stamped as never set up; nothing deleted.
            if not confirmed:
                count, held = await unsettable_count(db, fleet)
                held_line = (
                    f" {len(held)} held by an emergency stop stay as they are: {', '.join(held)}." if held else ""
                )
                return (
                    f"Not applied. Back to not set up would switch off and reset {count} automation(s) of fleet "
                    f"**{fleet.name}** (nothing is deleted; runs in flight continue; Enable fleet switches them on "
                    f"again).{held_line} Show this to the user and call again with confirm=true only once they agree."
                )
            outcome = await unset_fleet(db, fleet, actor=user)
            if outcome.changed:
                await _audit(
                    db,
                    user,
                    fleet,
                    "fleet_unset",
                    f"Set {outcome.changed} automation(s) of fleet '{fleet.name}' back to not set up",
                    **fleet_control_audit_details(outcome, fleet.id),
                    held_by_stop=list(outcome.held_by_stop),
                )
            held_line = (
                f"\n  • Left as they are (held by an emergency stop): {', '.join(outcome.held_by_stop)}"
                if outcome.held_by_stop
                else ""
            )
            return (
                f"Fleet **{fleet.name}**: {outcome.changed} automation(s) set back to not set up."
                f"{held_line}\n  • Page: {fleet_link(fleet.id)}"
            )

        return await _run(_action, write=True)

    # ── delete_fleet ──────────────────────────────────────────────────

    async def _delete_fleet(fleet_id: str) -> str:
        from app.services.fleets import fleet_members

        async def _action(db: AsyncSession, user: User) -> str:
            fleet = await _manageable_fleet(db, user, _one_id(fleet_id, "fleet_id"), action="delete")
            rows = await fleet_members(db, fleet.id)
            name_, id_ = fleet.name, fleet.id
            await _audit(db, user, fleet, "delete", f"Deleted fleet '{fleet.name}'", rows_removed=len(rows))
            await db.delete(fleet)
            return (
                f"Fleet **{name_}** (`{id_}`) deleted. Its {len(rows)} member and connected row(s) are gone; "
                "no agent and not the workspace were deleted."
            )

        return await _run(_action, write=True)

    # ── set_fleet_members ─────────────────────────────────────────────

    async def _set_fleet_members(
        fleet_id: str, member_agent_ids: object = None, connected: object = None
    ) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            if member_agent_ids is None and connected is None:
                raise _RefusedError("Pass member_agent_ids, connected, or both.")
            fleet = await _manageable_fleet(db, user, _one_id(fleet_id, "fleet_id"))
            if member_agent_ids is not None:
                await _replace_members(
                    db, user, fleet, _id_list(member_agent_ids, "member_agent_ids"), keep_orchestrator=None
                )
            if connected is not None:
                await _replace_connected(db, user, fleet, _connected_specs(connected))
            await db.flush()
            await db.refresh(fleet)
            await _audit(db, user, fleet, "update", f"Set the members of fleet '{fleet.name}'")
            return await _describe(db, user, fleet, headline=f"Fleet **{fleet.name}** members set.")

        return await _run(_action, write=True)

    # ── set_fleet_exposure ────────────────────────────────────────────

    async def _set_fleet_exposure(fleet_id: str, agent_ids: object = None) -> str:
        from app.services.fleets import set_exposed_agents

        async def _action(db: AsyncSession, user: User) -> str:
            fleet = await _manageable_fleet(db, user, _one_id(fleet_id, "fleet_id"))
            await set_exposed_agents(db, fleet, _id_list(agent_ids, "agent_ids"))
            await _audit(db, user, fleet, "update", f"Set the exposure of fleet '{fleet.name}'")
            return await _describe(db, user, fleet, headline=f"Fleet **{fleet.name}** exposure set.")

        return await _run(_action, write=True)

    # ── share_fleet_workspace ─────────────────────────────────────────

    async def _share_fleet_workspace(
        fleet_id: str,
        action: str = "add",
        users: object = None,
        groups: object = None,
        permission: str = "",
    ) -> str:
        """Share a fleet and its workspace with people or groups, or list who has them (#24).

        One grant, written on both: the fleet page and the workspace it
        coordinates in. The gate is never wider than the Share dialog: a
        platform administrator, or the owner of the fleet's workspace who may
        manage the fleet. The writes are the REST routes' own
        (``apply_group_share_fields``, ``apply_user_share_fields``) and each
        entity gets its audit entry, the acting agent named.
        """
        from fastapi import HTTPException
        from sqlalchemy import select

        from app.api.v1.helpers import apply_group_share_fields, apply_user_share_fields
        from app.models.workspace import Workspace
        from app.services.audit import log_audit
        from app.services.presentation_sharing import (
            audience_line,
            format_sharing_result,
            normalize_share_action,
            normalize_share_permission,
            plan_sharing,
        )
        from app.services.share_principals import (
            MAX_PRINCIPALS_PER_CALL,
            load_principals,
            principal_entries,
            resolve_principals,
        )

        listing = str(action or "").strip().lower() == "list"
        verb = None if listing else normalize_share_action(action)
        if not listing and verb is None:
            return "action must be add, remove, replace or list. Nothing was changed."
        tier = normalize_share_permission(permission)
        if not listing and verb != "remove" and tier is None:
            return "permission must be view or edit. Nothing was changed."
        user_entries = principal_entries(users)
        group_entries = principal_entries(groups)
        if not listing and verb != "replace" and not user_entries and not group_entries:
            return f"Name at least one user (email or id) or group to {verb}. Nothing was changed."
        if len(user_entries) + len(group_entries) > MAX_PRINCIPALS_PER_CALL:
            return f"One call names at most {MAX_PRINCIPALS_PER_CALL} people and groups. Nothing was changed."

        async def _action(db: AsyncSession, user: User) -> str:
            fleet = await _manageable_fleet(db, user, _one_id(fleet_id, "fleet_id"), action="share")
            workspace = (
                await db.execute(
                    select(Workspace)
                    .where(Workspace.id == fleet.workspace_id)
                    .with_for_update(of=Workspace)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if workspace is None:
                raise _RefusedError(f"Fleet **{fleet.name}** has no workspace to share. Nothing was changed.")
            if not user.is_admin and workspace.user_id != user.id:
                raise _RefusedError(_SHARE_NOT_ALLOWED)
            entities: tuple[tuple[str, Any], ...] = (("fleet", fleet), ("workspace", workspace))
            if listing:
                lines = [f"Who has fleet **{fleet.name}** and its workspace **{workspace.name}**:"]
                for label, entity in entities:
                    index = await load_principals(db, entity)
                    lines.append(f"  • {label.capitalize()}: {audience_line(entity, index)}")
                lines.append(f"  • Page: {fleet_link(fleet.id)}")
                return "\n".join(lines)
            for label, entity in entities:
                if entity.is_company_managed and not entity.access_restricted:
                    raise _RefusedError(
                        f"The {label} **{entity.name}** is company-managed: everyone already sees it. "
                        "Nothing was changed."
                    )
            resolved = await resolve_principals(db, user_entries, group_entries)
            refusal = resolved.refusal(granting=verb != "remove")
            if refusal:
                raise _RefusedError(refusal)
            answer: list[str] = []
            for label, entity in entities:
                plan = plan_sharing(
                    entity,
                    action=verb,
                    permission=tier or "read_only",
                    users=resolved.users,
                    groups=resolved.groups,
                )
                if plan.changed:
                    try:
                        await apply_group_share_fields(db, entity, plan.changes, partial=True)
                        await apply_user_share_fields(db, entity, plan.changes, partial=True)
                    except HTTPException as exc:
                        raise _RefusedError(f"{str(exc.detail).rstrip('.')}. Nothing was changed.") from None
                    await log_audit(
                        db,
                        user=user,
                        action="update",
                        entity_type=label,
                        entity_id=entity.id,
                        entity_name=entity.name,
                        summary=f"Changed who can open {label} '{entity.name}' through an agent",
                        details={
                            **plan.audit_details(),
                            "agent_id": acting_agent_id,
                            "fleet_id": str(fleet.id),
                            "workspace_id": str(workspace.id),
                            "sharing_changed": True,
                        },
                    )
                index = await load_principals(db, entity)
                index.add(resolved.users)
                index.add(resolved.groups)
                answer.append(format_sharing_result(f"{label} {entity.name}", plan, index))
                answer.append(audience_line(entity, index))
            answer.append(f"  • Page: {fleet_link(fleet.id)}")
            return "\n".join(answer)

        return await _run(_action, write=not listing)

    # ── export_fleet_spec / apply_fleet_spec ───────────────────

    has_file_scope = bool(conversation_id or assignment_run_id)

    async def _write_spec_file(
        user: User, fleet: Fleet, text: str, file_context: FileContext | None
    ) -> str | None:
        """The full spec as a file of this conversation or run; its id, or None."""
        if not has_file_scope:
            return None
        try:
            ref = await persist_tool_artifact(
                user_id=str(user.id),
                filename=f"fleet-spec-{fleet.slug or fleet.id}.json",
                raw_bytes=text.encode("utf-8"),
                content_type="application/json",
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                file_context=file_context,
                source="agent_working",
            )
        except Exception:
            logger.exception("export_fleet_spec: the spec file of fleet %s was not stored", fleet.id)
            return None
        return str(ref.file_id)  # type: ignore[attr-defined]

    async def _export_fleet_spec(
        fleet_id: str, bundles: object = True, file_context: FileContext | None = None
    ) -> str:
        import json

        from app.services.fleet_spec import export_fleet_spec, structural_spec

        async def _action(db: AsyncSession, user: User) -> str:
            with_bundles = _bool(bundles, "bundles", True)
            fleet = await _visible_fleet(db, user, _one_id(fleet_id, "fleet_id"))
            spec = await export_fleet_spec(db, fleet, user, await _group_ids(db, user))
            members = spec["spec"]["members"]
            head = (
                f"Spec of fleet **{fleet.name}** ({len(members)} member(s), "
                f"{len(spec['spec']['connected'])} connected). Page: {fleet_link(fleet.id)}\n"
            )
            full_text = json.dumps(spec, indent=1, default=str)
            if with_bundles and len(full_text) <= SPEC_INLINE_MAX_CHARS:
                return (
                    head
                    + "Pass it whole to apply_fleet_spec (dry_run first) to recreate or update the fleet.\n"
                    "```json\n" + full_text + "\n```"
                )
            # The structure inline, the whole spec in a file.
            file_id = await _write_spec_file(user, fleet, full_text, file_context)
            why = (
                "Without the agent bundles (bundles=false)"
                if not with_bundles
                else f"The full spec is {len(full_text):,} characters, too long to answer inline"
            )
            if file_id:
                where = (
                    f"The full spec, agent bundles included ({len(full_text):,} characters), is the file "
                    f"`{file_id}` [[FILE:{file_id}]]: pass apply_fleet_spec(file_id=\"{file_id}\") "
                    "(dry_run first) to recreate or update the fleet from it."
                )
            else:
                where = (
                    "No file was written (no conversation or run to keep it in); "
                    "GET /api/v1/fleets/{id}/spec answers the full spec."
                )
            return (
                head
                + f"{why}: each member below is a reference to its agent (agent.ref.slug), "
                "a valid spec that apply_fleet_spec adopts by slug. "
                + where
                + "\n```json\n"
                + json.dumps(structural_spec(spec), indent=1, default=str)
                + "\n```"
            )

        return await _run(_action, write=False)

    async def _spec_from_file(file_id: object, file_context: FileContext | None) -> dict[str, Any]:
        """The spec a conversation or run file holds (JSON or YAML text)."""
        from app.services.fleet_spec import FleetSpecError, parse_fleet_spec_text

        ref = resolve_file_ref(file_context, str(file_id))
        if ref is None:
            ref = await load_scoped_file_ref(
                str(file_id), conversation_id=conversation_id, assignment_run_id=assignment_run_id
            )
            if ref is not None and file_context is not None:
                file_context.add(ref)
        if ref is None:
            raise _RefusedError(unresolved_file_message(file_context, file_id))
        if file_context is not None:
            data = await file_context.read_bytes(ref.file_id)
        else:
            from app.services.file_storage import file_storage

            data = await file_storage.download(ref.s3_object_name)
        if data is None:
            raise _RefusedError(f"The content of '{ref.filename}' could not be loaded from storage.")
        try:
            return parse_fleet_spec_text(data.decode("utf-8"))
        except UnicodeDecodeError:
            raise _RefusedError(f"'{ref.filename}' is not a text file; a spec is JSON or YAML text.") from None
        except FleetSpecError as exc:
            raise _RefusedError(f"'{ref.filename}': {exc}") from None

    async def _apply_fleet_spec(
        spec: object = None,
        dry_run: object = True,
        workspace_id: str = "",
        allow_partial: object = False,
        file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        from app.services.fleet_spec import FleetSpecError, apply_fleet_spec, parse_fleet_spec_text

        async def _action(db: AsyncSession, user: User) -> str:
            dry = _bool(dry_run, "dry_run", True)
            partial = _bool(allow_partial, "allow_partial", False)
            has_spec = spec is not None and spec != ""
            if has_spec and file_id:
                raise _RefusedError("Pass spec or file_id, not both.")
            if not has_spec and not file_id:
                raise _RefusedError("Pass spec (an object or JSON / YAML text) or file_id (a file holding it).")
            try:
                if file_id:
                    parsed = await _spec_from_file(file_id, file_context)
                else:
                    parsed = spec if isinstance(spec, dict) else parse_fleet_spec_text(str(spec))
            except FleetSpecError as exc:
                raise _RefusedError(str(exc)) from None
            workspace_uuid = _one_id(workspace_id, "workspace_id") if workspace_id else None
            try:
                plan = await apply_fleet_spec(
                    db,
                    user,
                    parsed,
                    await _group_ids(db, user),
                    dry_run=dry,
                    allow_partial=partial,
                    workspace_id=workspace_uuid,
                )
            except FleetSpecError as exc:
                raise _RefusedError(str(exc)) from None
            hint = ""
            if plan.applied and plan.fleet_id is not None:
                fleet = await db.get(_fleet_model(), plan.fleet_id)
                if fleet is not None:
                    await _audit(
                        db, user, fleet, "apply_spec", f"Applied a fleet spec to '{fleet.name}'",
                        changes=len(plan.changes), unresolved=len(plan.unresolved),
                    )
                    await db.flush()
                    hint = await documentation_hint(db, fleet)
                if plan.created_fleet and plan.fleet_id is not None:
                    created.append(plan.fleet_id)
            return _plan_text(plan) + (f"\n{hint}" if hint else "")

        try:
            writes = _bool(dry_run, "dry_run", True) is False
        except _RefusedError as refusal:
            return str(refusal)
        created: list[uuid.UUID] = []
        answer = await _run(_action, write=writes)
        if writes:
            await _document_created(created)
        return answer

    async def _generate_fleet_spec(prompt: str = "", current_spec: object = None, workspace_id: str = "") -> str:
        import json

        from app.services.fleet_spec import FleetSpecError, parse_fleet_spec_text
        from app.services.fleet_spec_generation import generate_fleet_spec

        async def _action(db: AsyncSession, user: User) -> str:
            try:
                current = (
                    None
                    if current_spec in (None, "")
                    else current_spec
                    if isinstance(current_spec, dict)
                    else parse_fleet_spec_text(str(current_spec))
                )
                workspace_uuid = _one_id(workspace_id, "workspace_id") if workspace_id else None
                generated = await generate_fleet_spec(
                    db,
                    user,
                    await _group_ids(db, user),
                    str(prompt or ""),
                    current=current,
                    workspace_id=workspace_uuid,
                )
            except FleetSpecError as exc:
                raise _RefusedError(f"No spec was generated: {exc}") from None
            lines = [
                "Generated fleet spec, previewed as a dry run: nothing was written. Show the plan to the user; "
                "apply it with apply_fleet_spec (this spec, dry_run=false) only once they agree, then call "
                "regenerate_fleet_documentation(fleet_id) once so its About page documents it."
            ]
            if generated.notes:
                lines.append("Adjusted:")
                lines.extend(f"  • {note}" for note in generated.notes)
            lines.append(_plan_text(generated.plan))
            lines.append("```json\n" + json.dumps(generated.spec, indent=1, default=str) + "\n```")
            return "\n".join(lines)

        return await _run(_action, write=False)

    def _fleet_model():
        from app.models.fleet import Fleet

        return Fleet

    def _plan_text(plan: Any) -> str:
        head = (
            "Dry run: nothing was written. Apply with dry_run=false to make these changes."
            if plan.dry_run
            else "Applied."
        )
        lines = [head]
        if plan.fleet_id is not None and not plan.dry_run:
            lines.append(f"Fleet `{plan.fleet_slug}` (`{plan.fleet_id}`): {fleet_link(plan.fleet_id)}")
        for change in plan.changes:
            detail = f" ({change['detail']})" if change["detail"] else ""
            lines.append(f"  • {change['action']} {change['object']} {change['name']}{detail}")
        if plan.unresolved:
            lines.append("Unresolved (blocks the apply unless allow_partial):")
            lines.extend(f"  • {item}" for item in plan.unresolved)
        if plan.warnings:
            lines.append("Warnings:")
            lines.extend(f"  • {item}" for item in plan.warnings)
        return "\n".join(lines)

    # ── regenerate_fleet_documentation ──────────────────────────────────────────

    async def _get_fleet_prerequisites(fleet_id: str) -> str:
        from app.services.fleet_about import MET, TO_CONFIRM, live_prerequisites

        async def _action(db: AsyncSession, user: User) -> str:
            # Same read as the About page (#10258 S3): rows only, no model call.
            fleet = await _visible_fleet(db, user, _one_id(fleet_id, "fleet_id"))
            documented = ((fleet.documentation or {}).get("structured") or {}).get("prerequisites") or []
            lines = await live_prerequisites(db, user, fleet, documented)
            checked = [line for line in lines if line.state != TO_CONFIRM]
            met = sum(line.state == MET for line in checked)
            out = [
                f"Fleet **{fleet.name}**: prerequisites {met}/{len(checked)} met.",
                f"  • Page: {fleet_link(fleet.id)}",
            ]
            for line in lines:
                bits = [f"[{line.state}] {line.label}"]
                if line.reason:
                    bits.append(line.reason)
                if line.member_ids:
                    bits.append("members " + ", ".join(line.member_ids))
                if line.fix_route and line.state != MET:
                    bits.append(f"fix at {line.fix_route}")
                out.append("  • " + "; ".join(bits) + f" ({line.check})")
            return "\n".join(out)

        return await _run(_action, write=False)

    async def _get_fleet_health(fleet_id: str, apply_safe_fixes: bool = False) -> ToolResult | str:
        """The fleet's health findings with their evidence (#11700); the structured
        result carries ``healthy`` so an activation check gates on it."""
        from app.api.v1.helpers import run_visible_filter
        from app.services import fleet_health_signals as signals

        holder: dict[str, signals.FleetHealthReport] = {}
        suggestions: list[str] = []
        missing: list[bool] = []

        async def _action(db: AsyncSession, user: User) -> str:
            try:
                fleet = await _visible_fleet(db, user, _one_id(fleet_id, "fleet_id"))
            except _RefusedError:
                # Structured, so a gate check reads a configuration error (#1400).
                missing.append(True)
                raise
            report = await signals.build_fleet_health_report(
                db, fleet, user_id=str(user.id), run_scope=run_visible_filter(user.id)
            )
            holder["report"] = report
            holder["automations"] = (await _automation_state(db, user, fleet)).structured()
            suggestions.extend(await _suggestion_lines(db, user, fleet))
            return signals.format_report(report)

        text = await _run(_action, write=False)
        report = holder.get("report")
        if report is None:
            if missing:
                from app.services.assignment_activation_check import TARGET_MISSING_KEY

                return ToolResult(
                    tool_call_id="",
                    content=text,
                    structured={"fleet_id": str(fleet_id), "healthy": None, TARGET_MISSING_KEY: True},
                )
            return text
        applied = await signals.apply_safe_fixes(report) if apply_safe_fixes else None
        structured = report.structured()
        if "automations" in holder:
            structured["automations"] = holder["automations"]
        if applied is not None:
            structured["applied"] = applied
        content = signals.format_report(report, applied=applied)
        if suggestions:
            # The About page's open suggestions, the chip's count (#12099); not a health finding.
            content = "\n".join([content, *suggestions])
        return ToolResult(tool_call_id="", content=content, structured=structured)

    async def _dismiss_fleet_health_findings(
        fleet_id: str, action: str = "dismiss", ids: object = None, reason: str = "", until: str = ""
    ) -> str:
        """Dismiss or restore health findings of a fleet, with a reason (#12014)."""
        from datetime import UTC, datetime

        from app.api.v1.helpers import run_visible_filter
        from app.services import fleet_health_dismissals as dismissals
        from app.services import fleet_health_signals as signals

        verb = str(action or "dismiss").strip().lower()
        if verb not in ("dismiss", "restore"):
            return "Refused: action is dismiss or restore."
        if ids in (None, "", "all"):
            chosen: list[str] | None = None
        elif isinstance(ids, list):
            chosen = [str(i).strip()[:80] for i in ids if str(i).strip()]
        else:
            chosen = [p.strip()[:80] for p in str(ids).split(",") if p.strip()]

        async def _action(db: AsyncSession, user: User) -> str:
            # A fleet manager's decision, built-in fleets included, audited (as list_fleet_doc_findings).
            fleet = await _manageable_fleet(db, user, _one_id(fleet_id, "fleet_id"), fields={"health_dismissals"})
            now = datetime.now(UTC)
            try:
                if verb == "restore":
                    kept, count = dismissals.restore(fleet.health_dismissals, chosen, now)
                    fleet.health_dismissals = kept or None
                    if count:
                        await _audit(
                            db,
                            user,
                            fleet,
                            "update",
                            f"Restored {count} health finding dismissal(s) of fleet '{fleet.name}'",
                            findings=chosen or "all",
                        )
                    return f"Fleet **{fleet.name}**: {count} dismissal(s) restored."
                report = await signals.build_fleet_health_report(
                    db, fleet, user_id=str(user.id), run_scope=run_visible_filter(user.id), now=now
                )
                stored, written = dismissals.dismiss(
                    fleet.health_dismissals,
                    chosen or [],
                    findings=[*report.findings, *_hidden(report, fleet.id)],
                    known_kinds=signals.FINDING_FIX,
                    reason=reason,
                    by=str(user.id),
                    by_name=getattr(user, "full_name", "") or "",
                    until=until,
                    now=now,
                )
            except dismissals.DismissalRefusedError as exc:
                raise _RefusedError(str(exc)) from None
            fleet.health_dismissals = stored
            await _audit(
                db,
                user,
                fleet,
                "update",
                f"Dismissed {len(written)} health finding(s) of fleet '{fleet.name}'",
                findings=[r["id"] for r in written],
                reason=written[0]["reason"],
            )
            names = ", ".join(f"`{r['id']}`" for r in written)
            return f"Fleet **{fleet.name}**: {len(written)} health finding(s) dismissed ({names})."

        def _hidden(report: Any, fid: object) -> list[Any]:
            # A finding already dismissed may be dismissed again (new reason): read it from the records.
            return [
                signals.FleetFinding(kind=d.get("kind", ""), summary=d.get("summary", ""), id=d["id"])
                for d in report.dismissed
                if d.get("scope") == dismissals.SCOPE_FINDING
            ]

        return await _run(_action, write=True)

    async def _get_fleet_setup(fleet_id: str = "") -> str:
        from sqlalchemy import select

        from app.models.agent import Agent
        from app.services.fleet_about import MET, TO_CONFIRM, live_prerequisites
        from app.services.fleet_practice_suggestions import working_knowledge
        from app.services.fleet_setup import FleetSetupFacts, can_consult, render_setup
        from app.services.fleet_views import home_member_ids
        from app.services.fleets import fleet_members

        if not str(fleet_id or "").strip():
            # No fleet yet: the interview of a new fleet (#11250).
            return render_setup(None)

        async def _action(db: AsyncSession, user: User) -> str:
            # The setup interview (#11250): the next step derived from the fleet as stored.
            fleet = await _visible_fleet(db, user, _one_id(fleet_id, "fleet_id"))
            home = home_member_ids(await fleet_members(db, fleet.id), fleet.orchestrator_agent_id)
            documented = ((fleet.documentation or {}).get("structured") or {}).get("prerequisites") or []
            lines = await live_prerequisites(db, user, fleet, documented)
            # A suggestion (#11948), never a setup step: members that cannot consult the orchestrator.
            cannot_consult: list[str] = []
            without_knowledge = False
            if fleet.orchestrator_agent_id is not None:
                others = [aid for aid in home if aid != fleet.orchestrator_agent_id]
                if others:
                    rows = (
                        await db.execute(
                            select(
                                Agent.id,
                                Agent.name,
                                Agent.handover_agent_ids,
                                Agent.handover_types,
                                Agent.knowledge_base_ids,
                                Agent.knowledge_base_permissions,
                            ).where(Agent.id.in_([*others, fleet.orchestrator_agent_id]))
                        )
                    ).all()
                    lead = next((r for r in rows if r.id == fleet.orchestrator_agent_id), None)
                    members = [r for r in rows if r.id != fleet.orchestrator_agent_id]
                    cannot_consult = sorted(
                        r.name
                        for r in members
                        if not can_consult(r.handover_agent_ids, r.handover_types, fleet.orchestrator_agent_id)
                    )
                    # A suggestion too (#11972): the fleet has no working knowledge base.
                    without_knowledge = lead is not None and bool(members) and not working_knowledge(lead, members)
            facts = FleetSetupFacts(
                name=fleet.name,
                description=fleet.description or "",
                home_members=len(home),
                has_orchestrator=fleet.orchestrator_agent_id is not None,
                unmet=[line.label for line in lines if line.state not in (MET, TO_CONFIRM)],
                to_confirm=[line.label for line in lines if line.state == TO_CONFIRM],
                cannot_consult_orchestrator=cannot_consult,
                without_working_knowledge=without_knowledge,
            )
            return render_setup(facts, fleet_id=str(fleet.id), link=fleet_link(fleet.id))

        return await _run(_action, write=False)

    async def _confirm_fleet_prerequisite(fleet_id: str, check: str, confirmed: object = True) -> str:
        from app.services.fleet_about import set_prerequisite_confirmation

        async def _action(db: AsyncSession, user: User) -> str:
            # The About page's Confirm / Undo (#10469 S2): a fleet manager, built-in fleets included.
            fleet = await _manageable_fleet(
                db, user, _one_id(fleet_id, "fleet_id"), fields={"prerequisite_confirmations"}
            )
            value, err, _provided = _builder._parse_bool_arg(confirmed, "confirmed")
            if err:
                raise _RefusedError(err)
            try:
                line = await set_prerequisite_confirmation(db, user, fleet, str(check or ""), confirmed=bool(value))
            except ValueError as exc:
                raise _RefusedError(str(exc)) from None
            verb = "Confirmed" if value else "Undid the confirmation of"
            await _audit(
                db,
                user,
                fleet,
                "prerequisite_confirmation",
                f"{verb} the prerequisite '{line.label}' of fleet '{fleet.name}'",
                check=line.check,
                label=line.label,
                confirmed=bool(value),
            )
            return f"{verb} **{line.label}** on fleet **{fleet.name}** ({line.check}): now {line.state}."

        return await _run(_action, write=True)

    async def _regenerate_fleet_documentation(fleet_id: str, section: str = "") -> str:
        from app.services.fleet_doc_generation import FleetDocDraftError, check_section, regenerate_fleet_documentation

        async def _action(db: AsyncSession, user: User) -> str:
            fleet_uuid = _one_id(fleet_id, "fleet_id")
            # Same permission as a configuration edit; a built-in fleet is
            # refused below: the seed writes its documentation (#1956).
            # No row lock (#429): the generation marker below serializes two
            # generations, as on the REST route. A lock held across the model
            # call made a second call wait on it until the statement timeout and
            # answer a generic failure instead of the "already running" refusal.
            fleet = await _manageable_fleet(db, user, fleet_uuid, builtin_ok=True, lock=False)
            wanted = str(section or "").strip() or None
            if fleet.is_builtin:
                raise _RefusedError(
                    f"Fleet {fleet.name} is built in: its documentation is the one shipped with the platform, "
                    "kept up to date at each update. Nothing was generated."
                )
            try:
                # Refused before the claim (#393): a wrong name never shows as a failure on the About page.
                check_section(wanted)
            except FleetDocDraftError as exc:
                raise _RefusedError(f"No documentation was generated: {exc} Nothing was saved.") from None
            from app.services.fleet_doc_on_create import (
                GENERATION_FAILED,
                claim_generation,
                generation_running,
                generation_state,
                release_generation,
                schedule_pending_sections,
            )

            # The same server-side record as the About page's button (#10817):
            # the page spins while this runs, and two generations never overlap.
            # Read once: a failed flush or a rollback expires ``fleet``, and the
            # async ``finally`` must not lazy-load it (MissingGreenlet).
            doc_fleet_id = fleet.id
            if not await claim_generation(doc_fleet_id):
                running = generation_text(await generation_state(doc_fleet_id)).removeprefix("  • ")
                raise _RefusedError(
                    f"A generation of the documentation of fleet {fleet.name} is already running. "
                    "Nothing was started: read the page once it lands." + (f" {running}" if running else "")
                )
            failure: str | None = None
            try:
                # The model call runs on the caller's own model, inside this
                # run's tool call: it is counted on the run, never a run of its own.
                async with generation_running(doc_fleet_id):
                    draft = await regenerate_fleet_documentation(db, user.id, fleet, section=wanted)
                    version = (fleet.documentation or {}).get("version")
                    await _audit(
                        db,
                        user,
                        fleet,
                        "update",
                        f"Regenerated the documentation of fleet '{fleet.name}'",
                        documentation_version=version,
                        section=wanted,
                    )
                    # Committed before the marker is cleared, so the page never reads "done" on the old version.
                    await db.commit()
            except FleetDocDraftError as exc:
                failure = str(exc)
                raise _RefusedError(f"No documentation was generated: {exc} Nothing was saved.") from None
            except Exception:
                failure = GENERATION_FAILED
                raise
            finally:
                await release_generation(doc_fleet_id, failure)
            # A section the provider failed on is filled in the background (#11292).
            filling = schedule_pending_sections(doc_fleet_id, user.id, draft.pending)
            what = f"section `{wanted}`" if wanted else "documentation"
            lines = [
                f"Fleet **{fleet.name}**: {what} regenerated, saved as version {version}.",
                f"  • Page: {fleet_link(fleet.id)}",
            ]
            if filling:
                lines.append(
                    f"  • {len(draft.pending)} section(s) the model failed on are being filled in the background: "
                    "nothing to do, the page updates on its own."
                )
            if draft.notes:
                lines.append("Generator notes (kept on this version's history entry):")
                from app.services.fleet_docs import note_text

                lines.extend(f"  • {note_text(note)}" for note in draft.notes)
            if draft.findings:
                lines.append(f"{len(draft.findings)} configuration suggestion(s): list_fleet_doc_findings reads them.")
            return "\n".join(lines)

        return await _run(_action, write=True)

    # ── list_fleet_doc_findings ─────────────────────────────────────────

    async def _list_fleet_doc_findings(
        fleet_id: str, action: str = "list", ids: object = None, reason: str = "", member: str = ""
    ) -> str:
        from app.services.fleet_docs import (
            dismiss_findings,
            documentation_text,
            manager_suggestions,
            restore_findings,
        )

        # Bound here: the list below names a loop variable ``member`` inside the action.
        named = " ".join(str(member or "").split())
        verb = str(action or "list").strip().lower()
        if verb not in ("list", "dismiss", "restore", "read"):
            return "Refused: action is list, dismiss, restore or read."
        if ids in (None, "", "all"):
            chosen: list[str] | None = None
        elif isinstance(ids, list):
            chosen = [str(i).strip()[:40] for i in ids if str(i).strip()]
        else:
            chosen = [p.strip()[:40] for p in str(ids).split(",") if p.strip()]

        async def _action(db: AsyncSession, user: User) -> str:
            # The suggestions are for the people who manage the fleet, as on its About page (#10246);
            # dismiss and restore take the same permission as its buttons, audited (#10536).
            fleet = await _manageable_fleet(db, user, _one_id(fleet_id, "fleet_id"), builtin_ok=True)
            if verb == "read":
                # The stored text a regeneration wrote (#12181), narrowed to one member's lines when asked.
                version = (fleet.documentation or {}).get("version") if isinstance(fleet.documentation, dict) else None
                parts = documentation_text(fleet, named)
                who = f" lines naming `{named}`" if named else ""
                if not parts:
                    return f"Fleet **{fleet.name}**: no documentation{who} (version {version or 'none'})."
                lines = [f"Fleet **{fleet.name}**: documentation version {version}{who}."]
                for key, text in parts:
                    lines += [f"## {key}", text]
                return "\n".join(lines)
            if verb == "dismiss":
                done = await dismiss_findings(
                    db, fleet, chosen, by=str(user.id), by_name=getattr(user, "full_name", "") or "", reason=reason
                )
                if done:
                    await _audit(
                        db,
                        user,
                        fleet,
                        "update",
                        f"Dismissed {len(done)} documentation suggestion(s) of fleet '{fleet.name}'",
                        findings=[f["id"] for f in done],
                        reason=reason,
                    )
                return f"Fleet **{fleet.name}**: {len(done)} suggestion(s) dismissed."
            if verb == "restore":
                restored = restore_findings(fleet, chosen)
                if restored:
                    await _audit(
                        db,
                        user,
                        fleet,
                        "update",
                        f"Restored {restored} documentation suggestion(s) of fleet '{fleet.name}'",
                        findings=chosen or "all",
                    )
                return f"Fleet **{fleet.name}**: {restored} suggestion(s) restored."
            # The stored findings, the good-practice suggestions computed live and the findings of lapsed
            # dismissals, as the page's chip counts them (#12080, #12099): the count here is the chip's.
            shown, held, _prints = await manager_suggestions(db, fleet)
            if not shown and not held:
                return f"Fleet **{fleet.name}**: 0 suggestion(s), 0 dismissed."
            lines = [f"Fleet **{fleet.name}**: {len(shown)} suggestion(s), {len(held)} dismissed."]
            for f in shown:
                member = f" ({f['member']})" if f.get("member") else ""
                fix = f" Fix: {f['suggested_fix']}" if f.get("suggested_fix") else ""
                tags = " [good practice, applicable on the About page]" if f.get("applicable") else ""
                if not tags and f.get("source") == "practice":
                    tags = " [good practice]"
                if f.get("lapsed"):
                    # Why a dismissal stopped hiding its point (#12016): live again, counted (#12099).
                    tags += f" [dismissal lapsed: {f['lapsed']}]"
                lines.append(f"  • `{f['id']}` {f['kind']}{member}{tags}: {f['finding']}{fix}")
            for d in held:
                member = f" ({d.get('member')})" if d.get("member") else ""
                why = f", reason: {d['reason']}" if d.get("reason") else ""
                who = d.get("by_name") or d.get("by") or "?"
                lines.append(
                    f"  • `{d['id']}` [dismissed by {who} at {d.get('at')}{why}] {d.get('kind')}{member}: {d.get('finding')}"
                )
            lines.append(
                "A dismissal hides its point however a regeneration words it, until the part of its member the"
                " point is about changes (a role label edit never) or 90 days pass; a lapsed one counts as open."
                " A fixed suggestion disappears at the next regenerate_fleet_documentation."
            )
            return "\n".join(lines)

        return await _run(_action, write=verb in ("dismiss", "restore"))

    # ── set_fleet_member_parent ─────────────────────────────────────────────

    async def _set_fleet_member_parent(
        fleet_id: str, agent_id: str, parent_agent_id: object = _UNSET, role_label: str | None = None
    ) -> str:
        from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember
        from app.services.fleets import fleet_members, update_member

        async def _action(db: AsyncSession, user: User) -> str:
            fleet_uuid = _one_id(fleet_id, "fleet_id")
            child_id = _one_id(agent_id, "agent_id")
            # With role_label alone the parent is left as it is; without role_label a missing parent is top level.
            set_parent = parent_agent_id is not _UNSET or role_label is None
            parent_text = "" if parent_agent_id is _UNSET else str(parent_agent_id or "").strip()
            top_level = parent_text.lower() in ("", "none", "null")
            parent_id = None if top_level else safe_uuid(parent_text)
            if set_parent and not top_level and parent_id is None:
                raise _RefusedError(f"parent_agent_id: `{parent_text}` is not an id (null puts the member at top level).")
            label = None if role_label is None else " ".join(str(role_label).split())
            limit = getattr(FleetMember.__table__.c.role_label.type, "length", None) or 100
            if label is not None and len(label) > limit:
                raise _RefusedError(f"role_label is {len(label)} characters, at most {limit}. Nothing was saved.")
            fleet = await _manageable_fleet(db, user, fleet_uuid)
            homes = {r.agent_id: r for r in await fleet_members(db, fleet.id) if r.role == FLEET_ROLE_MEMBER}
            child = homes.get(child_id)
            if child is None:
                raise _RefusedError(f"agent `{child_id}` is not a home member of this fleet. Nothing was saved.")
            headlines: list[str] = []
            if set_parent:
                if child_id == fleet.orchestrator_agent_id and parent_id is not None:
                    raise _RefusedError(
                        "The orchestrator is the root of the fleet and reports to no one. Nothing was saved."
                    )
                parent_row_id = None
                if parent_id is not None:
                    parent = homes.get(parent_id)
                    if parent is None:
                        raise _RefusedError(
                            f"agent `{parent_id}` is not a home member of this fleet: a member reports to another "
                            "home member of the same fleet. Nothing was saved."
                        )
                    parent_row_id = parent.id
                before = child.parent_member_id
                # The service applies the API's rules (self, connected parent, loop).
                await update_member(db, fleet, child.id, fields={"parent_member_id": parent_row_id})
                await _audit(
                    db,
                    user,
                    fleet,
                    "update",
                    f"Set the parent of a member of fleet '{fleet.name}'",
                    member_id=str(child.id),
                    agent_id=str(child_id),
                    parent_member_id=str(parent_row_id) if parent_row_id else None,
                    previous_parent_member_id=str(before) if before else None,
                )
                headlines.append(f"Fleet **{fleet.name}**: member parent set.")
            if label is not None:
                previous = child.role_label or ""
                # The service the member API writes through (PATCH /fleets/{id}/members/{member_id}).
                await update_member(db, fleet, child.id, fields={"role_label": label})
                await _audit(
                    db,
                    user,
                    fleet,
                    "update",
                    f"Set the role label of a member of fleet '{fleet.name}'",
                    member_id=str(child.id),
                    agent_id=str(child_id),
                    role_label=label,
                    previous_role_label=previous,
                )
                names = await _agent_names(db, user, [child_id])
                who = names.get(child_id) or "(an agent you cannot see)"
                headlines.append(
                    f"Fleet **{fleet.name}**: role label of {who} `{child_id}` set from "
                    f"{_quoted_label(previous)} to {_quoted_label(label)}."
                )
            return await _describe(db, user, fleet, headline="\n".join(headlines))

        return await _run(_action, write=True)

    # ── move_fleet_member ───────────────────────────────────────────

    async def _move_fleet_member(fleet_id: str, agent_id: str, target_fleet_id: str) -> str:
        from app.models.fleet import FLEET_ROLE_MEMBER
        from app.services.fleets import fleet_members, move_member

        async def _action(db: AsyncSession, user: User) -> str:
            source_uuid = _one_id(fleet_id, "fleet_id")
            mover_id = _one_id(agent_id, "agent_id")
            target_uuid = _one_id(target_fleet_id, "target_fleet_id")
            if source_uuid == target_uuid:
                raise _RefusedError("target_fleet_id is the fleet the agent is already in. Nothing was saved.")
            # Both rows are held FOR UPDATE, in id order, so two opposite moves never deadlock.
            first, second = sorted((source_uuid, target_uuid))
            loaded = {
                first: await _manageable_fleet(db, user, first),
                second: await _manageable_fleet(db, user, second),
            }
            source, target = loaded[source_uuid], loaded[target_uuid]
            row = next(
                (
                    r
                    for r in await fleet_members(db, source.id)
                    if r.role == FLEET_ROLE_MEMBER and r.agent_id == mover_id
                ),
                None,
            )
            if row is None:
                raise _RefusedError(f"agent `{mover_id}` is not a home member of this fleet. Nothing was saved.")
            previous_member_id = row.id

            async def _agent(member_agent_id: uuid.UUID) -> Agent:
                return await _manageable_agent(db, user, member_agent_id)

            moved = await move_member(db, source, previous_member_id, target, manageable_agent=_agent)
            await _audit(
                db,
                user,
                target,
                "update",
                f"Moved a member from fleet '{source.name}' to fleet '{target.name}'",
                member_id=str(moved.id),
                previous_member_id=str(previous_member_id),
                source_fleet_id=str(source.id),
                agent_id=str(mover_id),
            )
            headline = f"Moved from fleet **{source.name}** to fleet **{target.name}**."
            return await _describe(db, user, target, headline=headline)

        return await _run(_action, write=True)

    # ── Definitions ───────────────────────────────────────────────────

    about = (
        f"A {platform} fleet groups agents around an orchestrator (the entry agent) and exactly one "
        "workspace (where they coordinate). An agent has one home fleet and may be connected to others."
    )
    fleet_ref = {"type": "string", "description": "Fleet id (list_fleets)."}
    ids_array = {"type": "array", "items": {"type": "string"}}
    connected_schema = {
        "type": "array",
        "description": (
            "Agents of other fleets or of none, reached by consult or delegation; replaces the list when given."
        ),
        "items": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "collaboration_instruction": {"type": "string", "description": "When and how to work with it."},
                "context_inclusion": {"type": "string", "enum": list(_CONTEXT_INCLUSIONS)},
            },
            "required": ["agent_id"],
        },
    }
    definitions = [
        ToolDefinition(
            name="list_fleets",
            description="List the fleets the user can see, with their orchestrator, workspace and page.",
            input_schema={
                "type": "object",
                "properties": {"include_archived": {"type": "boolean", "default": False}},
                "required": [],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="get_fleet",
            description=(
                "Get one fleet: workspace, orchestrator, home members (id, whom each reports to), "
                "connected agents, exposure, page, automations badge with each one off and why."
            ),
            input_schema={"type": "object", "properties": {"fleet_id": fleet_ref}, "required": ["fleet_id"]},
            source=_source(),
        ),
        ToolDefinition(
            name="create_fleet",
            description=(
                f"Create a fleet with its members in one call. {about} Home members: managed "
                "agents with no home fleet; the orchestrator is one, always exposed. "
                "No workspace_id: a new one. Give the user its page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string", "default": ""},
                    "workspace_id": {
                        "type": "string",
                        "description": "A workspace the user manages; empty: a new one.",
                        "default": "",
                    },
                    "orchestrator_agent_id": {"type": "string", "description": "The entry agent.", "default": ""},
                    "member_agent_ids": {
                        "type": "array",
                        "items": {"type": ["string", "object"]},
                        "description": "Ids or {agent_id, role_label, parent_agent_id}.",
                    },
                    "connected": connected_schema,
                    "exposed_agent_ids": {
                        **ids_array,
                        "description": "Members other fleets reach (the orchestrator always).",
                    },
                    "is_company_managed": {
                        "type": "boolean",
                        "description": "Visible to all (admin; company-managed agents, workspace).",
                        "default": False,
                    },
                    "autonomy_level": {"type": "string", "enum": ["full_gates", "medium", "low", "autonomous"]},
                },
                "required": ["name"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="update_fleet",
            description=(
                "orchestrator: member or 'none'. autonomy_level raise: admin chat, confirm=true. "
                "instructions_position: admin. health_check: default|off|assignment id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "workspace_id": {"type": "string"},
                    "orchestrator_agent_id": {"type": "string"},
                    "archived": {"type": "boolean"},
                    "autonomy_level": {"type": "string", "enum": ["full_gates", "medium", "low", "autonomous"]},
                    "instructions_position": {"type": "string"},
                    "announce_instructions": {"type": "boolean"},
                    "confirm": {"type": "boolean"},
                    "health_check": {"type": "string"},
                    "is_company_managed": {"type": "boolean", "description": "Admin only."},
                    "member_defaults": {
                        "type": "object",
                        "description": "Inherited unless a member sets its own.",
                        "properties": {
                            "context_inclusion": {"type": "string", "enum": list(_CONTEXT_INCLUSIONS)},
                            "collaboration_instruction": {"type": "string"},
                            "organization_scope": {
                                "type": "object",
                                "description": "{mode: primary|monitored|list, organization_ids}",
                            },
                        },
                    },
                },
                "required": ["fleet_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="set_fleet_automations",
            description=(
                "Pause a fleet's automations, resume those its pause stopped, enable every one off, "
                "or unset (back to Not set up, nothing deleted). Pause, enable and unset answer the "
                "count; confirm=true applies once the user agreed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "action": {"type": "string", "enum": ["pause", "resume", "unset", "enable"]},
                    "confirm": {"type": "boolean", "default": False},
                    "run_as": {
                        "type": "object",
                        "description": "enable: who to act as per module, keyed by the module's provider: me or a user id.",
                    },
                },
                "required": ["fleet_id", "action"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="delete_fleet",
            description=("Delete a fleet; its agents (left with no home fleet) and workspace are kept."),
            input_schema={"type": "object", "properties": {"fleet_id": fleet_ref}, "required": ["fleet_id"]},
            source=_source(),
        ),
        ToolDefinition(
            name="set_fleet_members",
            description=(
                "Replace a fleet's home members and/or its connected agents. "
                "A home member left out is removed; removing the orchestrator clears it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "member_agent_ids": {**ids_array, "description": "The complete list of home members."},
                    "connected": connected_schema,
                },
                "required": ["fleet_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="set_fleet_exposure",
            description=(
                "Replace which home members other fleets may reach (the Fleet Card); the orchestrator always is."
            ),
            input_schema={
                "type": "object",
                "properties": {"fleet_id": fleet_ref, "agent_ids": {**ids_array, "description": "Home member ids."}},
                "required": ["fleet_id", "agent_ids"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="share_fleet_workspace",
            description=(
                "Share a fleet and its workspace with people or groups, or list who has them. "
                "Admin or workspace owner only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "action": {"type": "string", "enum": ["add", "remove", "replace", "list"]},
                    "users": {**ids_array, "description": "Emails or ids"},
                    "groups": {**ids_array, "description": "Ids or names"},
                    "permission": {"type": "string", "enum": ["view", "edit"]},
                },
                "required": ["fleet_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="set_fleet_member_parent",
            description=(
                "Set whom a home member reports to (another home member, null for top level) and/or its role "
                "label. Layout only: routing, context, delegation and handovers do not change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "agent_id": {"type": "string", "description": "The home member to move (agent id)."},
                    "parent_agent_id": {
                        "type": "string",
                        "description": "The home member it reports to (agent id); empty or null for top level.",
                    },
                    "role_label": {
                        "type": "string",
                        "description": "What it does in the fleet, <=100 chars; empty clears it. Alone: parent kept.",
                    },
                },
                "required": ["fleet_id", "agent_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="move_fleet_member",
            description=(
                "Move a home member to another fleet the user manages, atomically. It joins at top level, not "
                "exposed; a moved orchestrator leaves this fleet without one. Handovers and assignments are kept."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "agent_id": {"type": "string", "description": "The home member to move (agent id)."},
                    "target_fleet_id": {
                        "type": "string",
                        "description": "The fleet it joins, as list_fleets shows it.",
                    },
                },
                "required": ["fleet_id", "agent_id", "target_fleet_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="get_fleet_prerequisites",
            description="A fleet's prerequisites checked live: state, reason, members, fix path.",
            input_schema={"type": "object", "properties": {"fleet_id": fleet_ref}, "required": ["fleet_id"]},
            source=_source(),
        ),
        ToolDefinition(
            name="get_fleet_setup",
            description="Fleet setup interview: the next step to ask the user (no fleet_id: a new fleet).",
            input_schema={"type": "object", "properties": {"fleet_id": fleet_ref}},
            source=_source(),
        ),
        ToolDefinition(
            name="get_fleet_health",
            description="A fleet's health findings with evidence ids, fix (auto/owner) and advice; healthy when none.",
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "apply_safe_fixes": {"type": "boolean", "description": "Release orphaned claims."},
                },
                "required": ["fleet_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="dismiss_fleet_health_findings",
            description="Dismiss (with a reason) or restore get_fleet_health findings; ids: finding ids or kinds.",
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "action": {"enum": ["dismiss", "restore"]},
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "until": {"type": "string", "description": "Lapse date."},
                },
                "required": ["fleet_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="confirm_fleet_prerequisite",
            description="Confirm a manual prerequisite; confirmed=false undoes.",
            input_schema={
                "type": "object",
                "properties": {"fleet_id": fleet_ref, "check": {"type": "string"}, "confirmed": {"type": "boolean"}},
                "required": ["fleet_id", "check"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="regenerate_fleet_documentation",
            description=(
                "Regenerate a fleet's documentation (one section or all) once after a change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "section": {"type": "string", "description": "Empty: all."},
                },
                "required": ["fleet_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="list_fleet_doc_findings",
            description="A fleet's About page suggestions (list, dismiss, restore) or doc text (read).",
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "action": {"enum": ["list", "dismiss", "restore", "read"]},
                    "member": {"type": "string"},
                    "ids": {"type": "array", "items": {"type": "string"}, "description": "Empty: all."},
                    "reason": {"type": "string"},
                },
                "required": ["fleet_id"],
            },
            source=_source(),
        ),
    ]

    definitions += [
        ToolDefinition(
            name="export_fleet_spec",
            description=(
                "Export a fleet as a spec (apiVersion xtm-one/fleet/v1): metadata, orchestrator, each home member "
                "with its role label, parent, exposure, handovers and agent bundle (no secrets; an agent the user "
                "cannot manage travels as a reference), and the connected agents. bundles=false answers the "
                "structure only and writes the full spec to a conversation file (file_id), as does a spec too "
                "long to answer inline. apply_fleet_spec takes the spec or that file_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fleet_id": fleet_ref,
                    "bundles": {
                        "type": "boolean",
                        "default": True,
                        "description": "false: members as references inline, the full spec in a file.",
                    },
                },
                "required": ["fleet_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="apply_fleet_spec",
            description=(
                "Apply a fleet spec (from export_fleet_spec: spec as an object or JSON / YAML text, or file_id). "
                "Idempotent: the fleet is found by slug among the user's fleets, each member by its key (an agent "
                "slug): a managed agent with that slug is adopted, never edited, else created from its bundle; a "
                "reference-only member is adopted by slug. Home members left out are removed (agents kept). "
                "dry_run (default true) writes nothing: show the plan, then apply with dry_run=false. Unresolved "
                "references block the apply unless allow_partial."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "spec": {"type": ["object", "string"], "description": "The spec, an object or JSON / YAML text."},
                    "file_id": {
                        "type": "string",
                        "description": "Instead of spec: the conversation file export_fleet_spec names.",
                        "default": "",
                    },
                    "dry_run": {"type": "boolean", "default": True},
                    "workspace_id": {
                        "type": "string",
                        "description": "Bind the fleet to this workspace instead of the one the spec names.",
                        "default": "",
                    },
                    "allow_partial": {"type": "boolean", "default": False},
                },
                "required": [],
            },
            source=_source(),
        ),
    ]

    definitions.append(
        ToolDefinition(
            name="generate_fleet_spec",
            description=(
                "Create a fleet from a prompt, step one: write a fleet spec (apiVersion xtm-one/fleet/v1) from what "
                "the user describes and preview it as a dry run. Existing agents the user manages and that have no "
                "home fleet are reused by reference; other members are created as new agents with a name, a "
                "description and a persona only. Nothing is written: show the plan, then apply the returned spec "
                "with apply_fleet_spec (dry_run=false) once the user agrees. Pass current_spec to refine a spec."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The fleet the user wants, in their words."},
                    "current_spec": {
                        "type": ["object", "string"],
                        "description": "A spec to refine (object or JSON / YAML text); empty to start over.",
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": "Preview the fleet in this workspace instead of the one the spec names.",
                        "default": "",
                    },
                },
                "required": ["prompt"],
            },
            source=_source(),
        )
    )

    executor = CallableToolExecutor()
    executor.register("generate_fleet_spec", _generate_fleet_spec)
    executor.register("export_fleet_spec", _export_fleet_spec)
    executor.register("apply_fleet_spec", _apply_fleet_spec)
    executor.register("list_fleets", _list_fleets)
    executor.register("get_fleet", _get_fleet)
    executor.register("create_fleet", _create_fleet)
    executor.register("update_fleet", _update_fleet)
    executor.register("delete_fleet", _delete_fleet)
    executor.register("set_fleet_automations", _set_fleet_automations)
    executor.register("set_fleet_members", _set_fleet_members)
    executor.register("set_fleet_exposure", _set_fleet_exposure)
    executor.register("share_fleet_workspace", _share_fleet_workspace)
    executor.register("set_fleet_member_parent", _set_fleet_member_parent)
    executor.register("move_fleet_member", _move_fleet_member)
    executor.register("regenerate_fleet_documentation", _regenerate_fleet_documentation)
    executor.register("get_fleet_prerequisites", _get_fleet_prerequisites)
    executor.register("get_fleet_setup", _get_fleet_setup)
    executor.register("get_fleet_health", _get_fleet_health)
    executor.register("dismiss_fleet_health_findings", _dismiss_fleet_health_findings)
    executor.register("confirm_fleet_prerequisite", _confirm_fleet_prerequisite)
    executor.register("list_fleet_doc_findings", _list_fleet_doc_findings)
    return definitions, executor
