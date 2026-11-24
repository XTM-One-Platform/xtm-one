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

"""The ``organization_context`` built-in group: change the organization profile (#11594).

Every agent reads the profile with ``get_organization_context`` (core). This
group is the write side, opt-in per agent like any other built-in group and
off by default:

- ``update_organization_context`` applies field changes at once, as a new
  profile version authored by the person the turn acts for, the agent and
  the basis kept per field (``organization_context_proposals.apply_changes``);
- ``propose_organization_context_change`` stores a proposal an administrator
  reviews in Settings > Organization (the review path of #9484);
- ``upsert_organization_rows`` and ``remove_organization_rows`` change rows of
  one section keyed on source + external_id, hand rows never touched
  (``organization_context_rows``, #11888), preview first;
- ``run_organization_import`` runs the platform import of Settings >
  Organization (``organization_context_import``, #11780), preview first;
- ``list_organization_syncs``, ``create_organization_sync`` and
  ``run_organization_sync`` read, schedule and run now the scheduled syncs
  of Settings > Organization > Syncs (``organization_context_sync``, #11913),
  saved by the administrator the turn acts for (#11888);
- ``list_organization_proposals`` and ``decide_organization_proposal`` read
  and accept or reject the pending proposals, the review of Settings >
  Organization (``organization_context_proposals.decide``, #475).

Authorization is enforced twice, as Settings > Organization is admin-only:
the turn wires the group only when the person it acts for is a platform
administrator (:func:`offers_organization_context_write`), and each executor
refuses again when that person is not one or is not known. Binding the group
to an agent is an administrator's act too (``ADMIN_BOUND_BUILTIN_TOOL_IDS``,
checked by the agents API and the agent builder).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

ORGANIZATION_CONTEXT_WRITE_GROUP = "organization_context"
ORGANIZATION_UPDATE_TOOL = "update_organization_context"
ORGANIZATION_UPSERT_ROWS_TOOL = "upsert_organization_rows"
ORGANIZATION_REMOVE_ROWS_TOOL = "remove_organization_rows"
ORGANIZATION_IMPORT_TOOL = "run_organization_import"
ORGANIZATION_LIST_SYNCS_TOOL = "list_organization_syncs"
ORGANIZATION_CREATE_SYNC_TOOL = "create_organization_sync"
ORGANIZATION_RUN_SYNC_TOOL = "run_organization_sync"
ORGANIZATION_LIST_PROPOSALS_TOOL = "list_organization_proposals"
ORGANIZATION_DECIDE_PROPOSAL_TOOL = "decide_organization_proposal"
_PROPOSAL_STATUSES = ("pending", "accepted", "rejected")
_SOURCE = f"builtin:{ORGANIZATION_CONTEXT_WRITE_GROUP}"
REFUSAL = "Refused: only an administrator can change the organization profile."
#: The shared-context warning of every organization context import, sync and write (#11994): one text,
#: the one Settings > Organization shows. The agent tells the person before it applies.
SHARED_CONTEXT_NOTICE = (
    "Shared organization context: data is read from the source platform with the permissions of the person "
    "who sets up this import or sync; the source platform's access rules, including organization segregation "
    "and markings, are not carried over; once saved, the organization context is visible to every user and "
    "every agent of this platform. This applies to the organization context only: agent integrations, "
    "dashboards and every other feature keep each user's own permissions. Tell the person before you apply."
)
#: The same three facts, short, at the end of each tool description (the schema budget is shared).
SHARED_CONTEXT_CLAUSE = (
    " Shared context: reads with your access, source segregation and markings not kept, every user and agent sees it."
)

#: Built-in groups only an administrator may bind to an agent.
#: ``user_admin`` (``builtin_tools/user_admin.py``) is named here by its id, not
#: imported, so this module stays the single owner of the set.
ADMIN_BOUND_BUILTIN_TOOL_IDS: frozenset[str] = frozenset({ORGANIZATION_CONTEXT_WRITE_GROUP, "user_admin"})


def offers_organization_context_write(builtin_tool_ids, *, acting_user_id: str | None, acting_is_admin: bool) -> bool:
    """Whether a turn wires the write group: bound to the agent, for a known administrator."""
    return bool(acting_user_id) and acting_is_admin and ORGANIZATION_CONTEXT_WRITE_GROUP in (builtin_tool_ids or ())


def refused_admin_bound_ids(requested, *, before=(), caller_is_admin: bool) -> list[str]:
    """The administrator-only groups *requested* adds over *before*, when the caller is not an administrator."""
    if caller_is_admin:
        return []
    already = set(before or ())
    return sorted({t for t in (requested or ()) if t in ADMIN_BOUND_BUILTIN_TOOL_IDS and t not in already})


def build_organization_context_write_tools(
    *,
    user_id: str | None,
    caller_is_admin: bool,
    agent_name: str = "",
    conversation_id: str | None = None,
    include_proposal: bool = True,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The write tools of the profile, acting for *user_id* (an administrator)."""

    async def _update(organization: str = "", changes: list | None = None) -> str:
        if not caller_is_admin or not user_id:
            return REFUSAL
        from app.core.database import async_session
        from app.models.user import User
        from app.services import organization_context_proposals as proposals
        from app.services.audit import log_audit

        try:
            author = uuid.UUID(str(user_id))
        except ValueError:
            return REFUSAL
        try:
            async with async_session() as db:
                # Read again at the call: a role taken away since the turn began refuses.
                person = await db.get(User, author)
                if person is None or not getattr(person, "is_admin", False):
                    return REFUSAL
                outcome = await proposals.apply_changes(
                    db,
                    author_user_id=author,
                    organization=str(organization or ""),
                    changes=list(changes or []),
                    agent_name=agent_name,
                )
                await log_audit(
                    db,
                    user=person,
                    action="update",
                    entity_type="organization_context",
                    entity_id=outcome["organization_id"],
                    entity_name=outcome["organization"],
                    summary=(
                        f"Organization profile version {outcome['version']} saved by agent "
                        f"{agent_name or 'unknown'}: {', '.join(outcome['fields'])}"
                    ),
                    details={"agent": agent_name, "version": outcome["version"], "fields": outcome["fields"]},
                )
                await db.commit()
        except proposals.ProposalError as exc:
            return f"Not saved: {exc}"
        except Exception:
            logger.warning("Organization profile update failed", exc_info=True)
            return "Error: the profile could not be saved right now. Say so; nothing was changed."
        return (
            f"Saved version {outcome['version']} of the profile of {outcome['organization']}: "
            f"{', '.join(outcome['fields'])}. Settings > Organization shows it in the history; "
            "an administrator can restore a field from an earlier version. "
            f"{SHARED_CONTEXT_NOTICE}"
        )

    async def _admin(db: Any) -> Any:
        """The acting administrator read again at the call, else ``None``."""
        from app.models.user import User

        if not caller_is_admin or not user_id:
            return None
        try:
            author = uuid.UUID(str(user_id))
        except ValueError:
            return None
        person = await db.get(User, author)
        return person if person is not None and getattr(person, "is_admin", False) else None

    async def _save_rows(
        db: Any, person: Any, profile: Any, field: str, source: str, basis: str, version_label: str
    ) -> int:
        from app.services import organization_context as org_context
        from app.services.audit import log_audit

        sources = {field: {"source": f"agent:{agent_name}"[:300] if agent_name else source, "basis": basis[:500]}}
        row = await org_context.save_version(db, profile, source="draft", author_user_id=person.id, sources=sources)
        await log_audit(
            db,
            user=person,
            action="update",
            entity_type="organization_context",
            entity_id=str(row.organization_id),
            entity_name=getattr(profile, "name", "") or "organization",
            summary=f"Organization profile version {row.version} saved by agent {agent_name or 'unknown'}: {basis}"[
                :500
            ],
            details={"agent": agent_name, "version": row.version, "fields": [field], "tool": version_label},
        )
        await db.commit()
        return row.version

    async def _rows(
        mode: str,
        section: str,
        source: str,
        rows: list[Any] | None = None,
        external_ids: list[Any] | None = None,
        rows_format: str = "profile",
        include_owner: bool = False,
        apply: bool = False,
    ) -> str:
        import json

        from app.core.database import async_session
        from app.services import organization_context as org_context
        from app.services import organization_context_rows as org_rows

        try:
            async with async_session() as db:
                person = await _admin(db)
                if person is None:
                    return REFUSAL
                if section in org_rows.SOURCED_SECTIONS:
                    from app.services import organization_asset_records as records

                    org = await records.ensure_records(db)
                    if org is not None:  # no profile yet: the profile path below creates the organization
                        return await _record_rows(
                            db,
                            person,
                            org,
                            mode,
                            section,
                            source,
                            rows,
                            external_ids,
                            rows_format,
                            include_owner,
                            apply,
                        )
                current = await org_context.current_version(db)
                base = org_context.parse_profile(current.profile if current else {})
                if mode == "upsert":
                    mapped = org_rows.map_rows(list(rows or []), rows_format, include_owner=include_owner)
                    plan = org_rows.plan_upsert(base, section, source, mapped)
                else:
                    plan = org_rows.plan_remove(base, section, source, [str(i) for i in (external_ids or [])])
                answer = plan.as_dict()
                answer["based_on_version"] = current.version if current else None
                if not apply or not plan.changed or plan.profile is None:
                    answer["applied"] = False
                    answer["notice"] = SHARED_CONTEXT_NOTICE
                    return json.dumps(answer)
                field = org_rows.ROW_SECTIONS[section][0]
                answer["version"] = await _save_rows(
                    db, person, plan.profile, field, plan.source, org_rows.basis(plan, agent_name), mode
                )
                answer["applied"] = True
                answer["notice"] = SHARED_CONTEXT_NOTICE
                return json.dumps(answer)
        except org_rows.RowError as exc:
            return f"Not saved: {exc}"
        except Exception:
            logger.warning("Organization profile row write failed", exc_info=True)
            return "Error: the profile could not be changed right now. Say so; nothing was changed."

    async def _record_rows(
        db: Any,
        person: Any,
        org: uuid.UUID,
        mode: str,
        section: str,
        source: str,
        rows: list[Any] | None,
        external_ids: list[Any] | None,
        rows_format: str,
        include_owner: bool,
        apply: bool,
    ) -> str:
        """Assets and asset groups since #1543: planned against the records, written row by row (no version)."""
        import json

        from app.services import organization_asset_records as records
        from app.services import organization_context_rows as org_rows

        src = source.strip().lower()
        if mode == "upsert":
            mapped = org_rows.map_rows(list(rows or []), rows_format, include_owner=include_owner)
            org_rows.check_call(section, src, len(mapped))
            refs = [
                (src, str(r.get("external_id") or "").strip())
                for r in mapped
                if isinstance(r, dict) and str(r.get("external_id") or "").strip()
            ]
            stored = await records.rows_by_ref(db, org, section, refs)
            fresh_names = [str(r.get("name") or "") for r in mapped if isinstance(r, dict)]
            names = await records.name_owners(db, org, section, fresh_names, source=src)
            # A row's own record is no clash: drop the names the addressed records hold.
            for row in stored.values():
                names.pop(org_rows.name_key(row.get("name")), None)
            validate = records.validate_asset if section == "assets" else records.validate_group
            plan = org_rows.plan_record_upsert(section, src, mapped, validate=validate, stored=stored, names=names)
        else:
            ids = [str(i).strip() for i in (external_ids or []) if str(i).strip()]
            org_rows.check_call(section, src, len(ids))
            stored = await records.rows_by_ref(db, org, section, [(src, i) for i in ids])
            plan = org_rows.plan_record_remove(section, src, ids, stored=stored)
        answer = plan.as_dict()
        if not apply or not plan.changed:
            answer["applied"] = False
            answer["notice"] = SHARED_CONTEXT_NOTICE
            return json.dumps(answer)
        actor = f"agent:{agent_name}"[:120] if agent_name else f"agent:{person.id}"
        origin = f"agent:{src}"[:80]
        try:
            if mode == "upsert":
                for start in range(0, len(plan.writes), records.MAX_BATCH):
                    await records.write_rows(
                        db, org, section, plan.writes[start : start + records.MAX_BATCH], origin=origin, actor=actor
                    )
            else:
                await records.remove_rows(db, org, section, plan.remove_refs, origin=origin, actor=actor)
        except records.RecordError as exc:
            await db.rollback()
            return f"Not saved: {exc}"
        from app.services.audit import log_audit

        summary = org_rows.basis(plan, agent_name)
        await log_audit(
            db,
            user=person,
            action="update" if mode == "upsert" else "delete",
            entity_type="organization_context",
            entity_id=str(org),
            entity_name=section,
            summary=f"Organization {section} rows written by agent {agent_name or 'unknown'}: {summary}"[:500],
            details={"agent": agent_name, "section": section, "source": src, "tool": mode, **answer["counts"]},
        )
        await db.commit()
        answer["applied"] = True
        answer["history"] = "each row change is in the asset history (Inventory), restorable by an administrator"
        answer["notice"] = SHARED_CONTEXT_NOTICE
        return json.dumps(answer)

    async def _upsert_rows(
        section: str = "",
        source: str = "",
        rows: list[Any] | None = None,
        rows_format: str = "profile",
        include_owner: bool = False,
        apply: bool = False,
    ) -> str:
        return await _rows(
            "upsert", section, source, rows=rows, rows_format=rows_format, include_owner=include_owner, apply=apply
        )

    async def _remove_rows(
        section: str = "", source: str = "", external_ids: list[Any] | None = None, apply: bool = False
    ) -> str:
        return await _rows("remove", section, source, external_ids=external_ids, apply=apply)

    async def _import(
        section: str = "",
        source: str = "",
        filters: dict[str, Any] | None = None,
        batch_type: str = "",
        apply: bool = False,
    ) -> str:
        import json

        from app.core.database import async_session
        from app.services import organization_context as org_context
        from app.services import organization_context_import as imp

        if section not in imp.SECTION_SOURCES:
            return f"Not run: section must be one of {', '.join(imp.SECTION_SOURCES)}"
        try:
            async with async_session() as db:
                person = await _admin(db)
                if person is None:
                    return REFUSAL
                used = dict(filters or {})
                plan, version = await imp.preview_import(
                    db, user_id=person.id, section=section, source=source, filters=used, batch_type=batch_type
                )
                answer = {
                    "based_on_version": version,
                    "plan": plan.as_dict(),
                    "applied": False,
                    "notice": SHARED_CONTEXT_NOTICE,
                }
                if plan.error or not apply:
                    return json.dumps(answer, default=str)
                if plan.on_records:
                    # Assets and asset groups (#1543): row writes on the records, never a profile version.
                    written = await imp.apply_record_import(
                        db, plan, actor=(f"agent:{agent_name}" if agent_name else f"agent:{person.id}")[:120]
                    )
                    if plan.software:
                        written += await imp.store_endpoint_software(db, plan, user_id=person.id)
                    await db.commit()
                    answer.update(applied=bool(written))
                    return json.dumps(answer, default=str)
                if not (plan.new or plan.updated) or plan.profile is None:
                    # An endpoint source (#1251) may change only the software of known devices.
                    if plan.software and await imp.store_endpoint_software(db, plan, user_id=person.id):
                        await db.commit()
                        answer.update(applied=True)
                    return json.dumps(answer, default=str)
                sources = imp.provenance(plan, used)
                row = await org_context.save_version(
                    db, plan.profile, source="draft", author_user_id=person.id, sources=sources
                )
                if plan.software:
                    await imp.store_endpoint_software(db, plan, user_id=person.id)
                await db.commit()
                answer.update(applied=True, version=row.version)
                return json.dumps(answer, default=str)
        except Exception:
            logger.warning("Organization import failed", exc_info=True)
            return "Error: the import could not run right now. Say so; nothing was changed."

    def _sync_dict(sync: Any) -> dict[str, Any]:
        return {
            "id": str(sync.id),
            "section": sync.section,
            "source": sync.source,
            "filters": sync.filters or {},
            "remove_gone": sync.remove_gone,
            "excluded_external_ids": list(getattr(sync, "excluded_external_ids", None) or []),
            "direction": getattr(sync, "direction", None) or "inbound",
            "schedule": {
                "frequency": sync.frequency,
                "hour": sync.hour,
                "minute": sync.minute,
                "weekday": sync.weekday,
                "timezone": sync.timezone,
            },
            "enabled": sync.enabled,
            "paused_reason": sync.paused_reason,
            "next_run_at": sync.next_run_at,
            "last_run_at": sync.last_run_at,
            "last_result": sync.last_result,
        }

    async def _list_syncs() -> str:
        import json

        from app.core.database import async_session
        from app.services import organization_context_sync as osync

        try:
            async with async_session() as db:
                if await _admin(db) is None:
                    return REFUSAL
                return json.dumps({"syncs": [_sync_dict(s) for s in await osync.all_syncs(db)]}, default=str)
        except Exception:
            logger.warning("Organization syncs listing failed", exc_info=True)
            return "Error: the syncs could not be read right now. Say so."

    async def _create_sync(
        section: str = "",
        source: str = "",
        filters: dict[str, Any] | None = None,
        batch_type: str = "",
        remove_gone: bool = True,
        frequency: str = "daily",
        hour: int = 6,
        minute: int = 0,
        weekday: int = 0,
        timezone: str = "UTC",
        direction: str = "inbound",
    ) -> str:
        import json

        from app.core.database import async_session
        from app.models.organization_context import OrganizationContextSync
        from app.services import organization_context_sync as osync
        from app.services.audit import log_audit

        try:
            async with async_session() as db:
                person = await _admin(db)
                if person is None:
                    return REFUSAL
                if section == osync.ALL_SECTIONS and await osync.global_sync_exists(db):
                    return "Not saved: a Sync all schedule exists already; an administrator edits it in Settings."
                sync = OrganizationContextSync()
                osync.define(
                    sync,
                    owner_id=person.id,
                    section=section,
                    source=source,
                    filters=filters,
                    batch_type=batch_type,
                    remove_gone=remove_gone,
                    frequency=frequency,
                    hour=int(hour),
                    minute=int(minute),
                    weekday=int(weekday),
                    timezone=timezone or "UTC",
                    direction=direction or "inbound",
                )
                db.add(sync)
                await db.flush()
                await log_audit(
                    db,
                    user=person,
                    action="create",
                    entity_type="organization_context_sync",
                    entity_id=sync.id,
                    entity_name=f"{sync.section} from {sync.source or 'every source'}",
                    summary=f"Organization sync of {sync.section} scheduled {sync.frequency} by agent "
                    f"{agent_name or 'unknown'}"[:500],
                    details={"agent": agent_name, **_sync_dict(sync)},
                )
                await db.commit()
                return json.dumps(
                    {"saved": True, "sync": _sync_dict(sync), "notice": SHARED_CONTEXT_NOTICE}, default=str
                )
        except osync.SyncRefusedError as exc:
            return f"Not saved: {exc}"
        except Exception:
            logger.warning("Organization sync creation failed", exc_info=True)
            return "Error: the sync could not be saved right now. Say so; nothing was changed."

    async def _run_sync(sync_id: str = "") -> str:
        import json

        from app.core.database import async_session
        from app.models.organization_context import OrganizationContextSync
        from app.services import organization_context_sync as osync

        try:
            wanted = uuid.UUID(str(sync_id))
        except ValueError:
            return "Not run: sync_id must be the id list_organization_syncs gives."
        try:
            async with async_session() as db:
                person = await _admin(db)
                if person is None:
                    return REFUSAL
                sync = await db.get(OrganizationContextSync, wanted)
                if sync is None:
                    return "Not run: no sync with that id. Read list_organization_syncs."
                outcome = await osync.run_now(db, sync, trigger=f"run now by agent {agent_name or 'unknown'}")
                await db.commit()
                return json.dumps(
                    {"result": outcome, "sync": _sync_dict(sync), "notice": SHARED_CONTEXT_NOTICE}, default=str
                )
        except Exception:
            logger.warning("Organization sync run failed", exc_info=True)
            return "Error: the sync could not run right now. Say so; nothing was changed."

    def _proposal_dict(row: Any) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "kind": row.kind,
            "organization_id": str(row.organization_id) if row.organization_id else None,
            "status": row.status,
            "entries": list(row.proposals or []),
            "based_on_version": row.based_on_version,
            "author_user_id": str(row.author_user_id) if row.author_user_id else None,
            "created_at": row.created_at,
        }

    async def _list_proposals(status: str = "pending", limit: int = 50) -> str:
        import json

        from app.core.database import async_session
        from app.services import organization_context_proposals as proposals

        if status not in _PROPOSAL_STATUSES:
            return f"Not read: status is one of {', '.join(_PROPOSAL_STATUSES)}."
        try:
            async with async_session() as db:
                if await _admin(db) is None:
                    return REFUSAL
                rows = await proposals.list_proposals(db, status=status, limit=int(limit or 50))
                return json.dumps({"status": status, "proposals": [_proposal_dict(r) for r in rows]}, default=str)
        except Exception:
            logger.warning("Organization proposals listing failed", exc_info=True)
            return "Error: the proposals could not be read right now. Say so."

    async def _decide_proposal(proposal_id: str = "", accept: list[Any] | None = None) -> str:
        import json

        from app.core.database import async_session
        from app.models.organization_context import OrganizationContextProposal
        from app.services import organization_context_proposals as proposals
        from app.services.audit import log_audit

        try:
            wanted = uuid.UUID(str(proposal_id))
        except ValueError:
            return "Not decided: proposal_id must be an id list_organization_proposals gives."
        accepted = [str(a) for a in (accept or [])]
        try:
            async with async_session() as db:
                person = await _admin(db)
                if person is None:
                    return REFUSAL
                row = await db.get(OrganizationContextProposal, wanted, with_for_update=True)
                if row is None:
                    return "Not decided: no proposal with that id. Read list_organization_proposals."
                # A tracked organization keeps the platform link its draft pre-set; another one is picked in Settings.
                outcome = await proposals.decide(db, row, admin_user_id=person.id, accept=accepted)
                await log_audit(
                    db,
                    user=person,
                    action="update",
                    entity_type="organization_context_proposal",
                    entity_id=str(row.id),
                    entity_name=f"{row.kind} proposal",
                    summary=f"Organization proposal {outcome['status']} by agent {agent_name or 'unknown'}"[:500],
                    details={"agent": agent_name, "accept": accepted, **outcome},
                )
                await db.commit()
        except proposals.ProposalError as exc:
            return f"Not decided: {exc}"
        except Exception:
            logger.warning("Organization proposal decision failed", exc_info=True)
            return "Error: the proposal could not be decided right now. Say so; nothing was changed."
        return json.dumps({"proposal_id": str(wanted), **outcome}, default=str)

    executor = CallableToolExecutor()
    executor.register(ORGANIZATION_UPDATE_TOOL, _update)
    executor.register(ORGANIZATION_UPSERT_ROWS_TOOL, _upsert_rows)
    executor.register(ORGANIZATION_REMOVE_ROWS_TOOL, _remove_rows)
    executor.register(ORGANIZATION_IMPORT_TOOL, _import)
    executor.register(ORGANIZATION_LIST_SYNCS_TOOL, _list_syncs)
    executor.register(ORGANIZATION_CREATE_SYNC_TOOL, _create_sync)
    executor.register(ORGANIZATION_RUN_SYNC_TOOL, _run_sync)
    executor.register(ORGANIZATION_LIST_PROPOSALS_TOOL, _list_proposals)
    executor.register(ORGANIZATION_DECIDE_PROPOSAL_TOOL, _decide_proposal)
    _apply = {"type": "boolean", "description": "false (default): preview only. true: save a new version."}
    # The platforms and formats are declared by the services, never named here (invariant 18).
    from app.services.organization_context_import import SECTION_SOURCES
    from app.services.organization_context_rows import ROW_FORMATS, ROW_SECTIONS

    _import_sources = sorted({s for sources in SECTION_SOURCES.values() for s in sources})
    _section = {"type": "string", "enum": list(ROW_SECTIONS)}
    from app.services.organization_context_sync import ALL_SECTIONS, SYNCABLE_SECTIONS

    definitions = [
        ToolDefinition(
            name=ORGANIZATION_UPDATE_TOOL,
            description=(
                "Change the organization profile now, as a new version authored by the administrator you act for: "
                "field changes on your own organization or a tracked one. Read get_organization_context first and "
                "send the whole new value of each field (a list replaces the list), with its basis. Each value is "
                "validated like the Settings > Organization form; an admin can restore a past version. To leave the "
                "decision to an administrator instead, use propose_organization_context_change."
                + SHARED_CONTEXT_CLAUSE
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization": {
                        "type": "string",
                        "description": "The tracked organization whose profile changes (name or id); omit for your own.",
                    },
                    "changes": {
                        "type": "array",
                        "description": "Field changes: {field (contract path, e.g. tech_stack), value, basis}.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["changes"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=ORGANIZATION_UPSERT_ROWS_TOOL,
            description=(
                "Add or update rows of one profile section without resending the list: assets and asset groups keyed "
                "on source + external_id (rows a person wrote are never touched), software and AI systems by name "
                "(tech_stack: old alias of software). "
                "Validated like the CSV import. Preview first (apply false): counts and refusals with "
                "reasons. A rows_format other than profile maps a platform's raw objects on the server."
                + SHARED_CONTEXT_CLAUSE
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "section": _section,
                    "source": {"type": "string", "description": "Platform the rows come from (its integration name)."},
                    "rows": {"type": "array", "items": {"type": "object"}, "description": "At most 500 rows."},
                    "rows_format": {"type": "string", "enum": list(ROW_FORMATS)},
                    "include_owner": {
                        "type": "boolean",
                        "description": "Device formats: the primary user's name as owner, only if the admin asked.",
                    },
                    "apply": _apply,
                },
                "required": ["section", "rows"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=ORGANIZATION_REMOVE_ROWS_TOOL,
            description=(
                "Remove assets or asset groups a source synced, by source + external_id. "
                "Rows a person wrote are never removed. Preview first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": ["assets", "asset_groups"]},
                    "source": {"type": "string"},
                    "external_ids": {"type": "array", "items": {"type": "string"}},
                    "apply": _apply,
                },
                "required": ["section", "source", "external_ids"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=ORGANIZATION_IMPORT_TOOL,
            description=(
                "Run the Settings > Organization import of one section from a connected platform (hand rows kept). "
                "Preview first: new, updated, skipped, source count." + SHARED_CONTEXT_CLAUSE
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["assets", "asset_groups", "ai_systems", "tech_stack", "crown_jewels", "pirs"],
                    },
                    "source": {"type": "string", "enum": _import_sources},
                    "filters": {"type": "object", "description": "The filters the dialog offers for that source."},
                    "batch_type": {"type": "string"},
                    "apply": _apply,
                },
                "required": ["section", "source"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=ORGANIZATION_LIST_SYNCS_TOOL,
            description=(
                "The scheduled syncs of Settings > Organization > Syncs: section, source, filters, schedule, "
                "enabled, paused reason, next and last run with its counts."
            ),
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=ORGANIZATION_CREATE_SYNC_TOOL,
            description=(
                "Keep one section in sync with a platform on a schedule (the import's rules: hand rows kept), or "
                "section all to run every section sync. Runs with your admin's token. Preview the import "
                "with run_organization_import first." + SHARED_CONTEXT_CLAUSE
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": [*SYNCABLE_SECTIONS, ALL_SECTIONS]},
                    "source": {"type": "string", "enum": _import_sources},
                    "filters": {"type": "object"},
                    "batch_type": {"type": "string"},
                    "remove_gone": {
                        "type": "boolean",
                        "description": "Drop rows gone from the source (default true).",
                    },
                    "direction": {"type": "string", "enum": ["inbound", "outbound"]},
                    "frequency": {"type": "string", "enum": ["hourly", "daily", "weekly"]},
                    "hour": {"type": "integer"},
                    "minute": {"type": "integer"},
                    "weekday": {"type": "integer", "description": "0 Monday to 6 Sunday."},
                    "timezone": {"type": "string", "description": "IANA name, e.g. Europe/Paris."},
                },
                "required": ["section"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=ORGANIZATION_RUN_SYNC_TOOL,
            description="Run a scheduled sync now (its schedule unchanged): added, updated, removed, version, error."
            + SHARED_CONTEXT_CLAUSE,
            input_schema={
                "type": "object",
                "properties": {"sync_id": {"type": "string"}},
                "required": ["sync_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=ORGANIZATION_LIST_PROPOSALS_TOOL,
            description=(
                "The organization profile proposals of Settings > Organization, newest first: id, kind, "
                "organization, entries, based_on_version."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(_PROPOSAL_STATUSES)},
                    "limit": {"type": "integer", "description": "1 to 200 (default 50)."},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=ORGANIZATION_DECIDE_PROPOSAL_TOOL,
            description=(
                "Accept or reject a pending proposal, as the administrator you act for (the Settings > Organization "
                "review). accept names the fields or organizations kept; empty rejects it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string"},
                    "accept": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Field paths or organization names to accept; empty rejects.",
                    },
                },
                "required": ["proposal_id", "accept"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
    if include_proposal:
        from .organization_proposals import build_organization_proposal_tool

        proposal_defs, proposal_exec = build_organization_proposal_tool(
            user_id=str(user_id or ""), caller_is_admin=caller_is_admin and bool(user_id), conversation_id=conversation_id
        )
        for definition in proposal_defs:
            handler = proposal_exec.handler(definition.name)
            if handler is not None:
                executor.register(definition.name, handler)
                definitions.append(dataclasses.replace(definition, source=_SOURCE))
    return definitions, executor
