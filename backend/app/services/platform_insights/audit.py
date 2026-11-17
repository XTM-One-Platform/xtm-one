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

"""``query_audit_log``: the audit trail, read by an agent.

The page's own filters (actor, action, entity type, period, free text) plus an
entity id, newest first, at most ``MAX_LIMIT`` rows a call. An administrator's
agent reads every entry, as the audit page does; anyone else reads the entries
of their own actions. Every read is itself audited once per window
(``traces.audit_read_once``, the trace-read semantics): reading who did what
leaves a trace of who looked.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.audit_log import AuditLog
from app.services.platform_insights.scope import (
    InsightRefusedError,
    InsightScope,
    as_list,
    parse_instant,
    resolve_period,
)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
#: Characters of one entry's ``details`` a row carries; the rest is cut and said.
DETAILS_MAX_CHARS = 1500

#: The action the read itself is audited under.
AUDIT_READ_ACTION = "inspect_audit_log"


def _escaped(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _details(value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, False
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= DETAILS_MAX_CHARS:
        return value, False
    return text[:DETAILS_MAX_CHARS], True


async def query_audit_log(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.builtin_tools.traces import audit_read_once

    try:
        limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        raise InsightRefusedError("Error: limit and offset must be whole numbers.") from None

    clauses = []
    actors = as_list(args.get("actor"))
    if actors:
        ids = [parsed for actor in actors if (parsed := safe_uuid(actor)) is not None]
        emails = [actor.casefold() for actor in actors if safe_uuid(actor) is None]
        arms = []
        if ids:
            arms.append(AuditLog.user_id.in_(ids))
        if emails:
            arms.append(func.lower(AuditLog.user_email).in_(emails))
        clauses.append(or_(*arms))
    if not scope.platform:
        clauses.append(AuditLog.user_id == scope.user.id)
    actions = as_list(args.get("action"))
    if actions:
        clauses.append(AuditLog.action.in_(actions))
    entity_types = as_list(args.get("entity_type"))
    if entity_types:
        clauses.append(AuditLog.entity_type.in_(entity_types))
    entity_id = str(args.get("entity_id") or "").strip()
    if entity_id:
        parsed_entity = safe_uuid(entity_id)
        if parsed_entity is None:
            raise InsightRefusedError("Error: entity_id must be a UUID.")
        clauses.append(AuditLog.entity_id == parsed_entity)

    if args.get("period") or args.get("date_from") or args.get("date_to"):
        if args.get("period") and str(args.get("period")).strip().lower() != "custom":
            _, start, end = resolve_period(args.get("period"), None, None)
        else:
            start = parse_instant(args.get("date_from"), label="date_from")
            end = parse_instant(args.get("date_to"), label="date_to")
        if start:
            clauses.append(AuditLog.created_at >= start)
        if end:
            clauses.append(AuditLog.created_at <= end)
    search = str(args.get("search") or "").strip()
    if search:
        pattern = f"%{_escaped(search)}%"
        clauses.append(
            or_(
                AuditLog.summary.ilike(pattern, escape="\\"),
                AuditLog.entity_name.ilike(pattern, escape="\\"),
                AuditLog.user_email.ilike(pattern, escape="\\"),
            )
        )

    filters = {
        "actor": actors,
        "action": actions,
        "entity_type": entity_types,
        "entity_id": entity_id or None,
        "period": args.get("period"),
        "date_from": args.get("date_from"),
        "date_to": args.get("date_to"),
        "search": search or None,
        "offset": offset,
        "limit": limit,
    }
    await audit_read_once(
        db,
        scope.user,
        action=AUDIT_READ_ACTION,
        entity_type="audit_log",
        entity_id=None,
        summary="Read the audit log through an agent" + ("" if scope.platform else " (own entries)"),
        details={"scope": scope.label, "filters": filters},
    )

    total = int(await db.scalar(select(func.count(AuditLog.id)).where(*clauses)) or 0)
    rows = (
        await db.execute(
            select(AuditLog)
            .where(*clauses)
            .order_by(AuditLog.created_at.desc(), AuditLog.id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    entries = []
    for row in rows:
        details, cut = _details(row.details)
        entry = {
            "id": str(row.id),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "actor_id": str(row.user_id),
            "actor_email": row.user_email,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": str(row.entity_id) if row.entity_id else None,
            "entity_name": row.entity_name,
            "summary": row.summary,
            "details": details,
        }
        if cut:
            entry["details_truncated"] = True
        if scope.platform:
            entry["ip_address"] = row.ip_address
        entries.append(entry)
    return {
        **scope.header(),
        "filters": filters,
        "total": total,
        "entries": entries,
        "offset": offset,
        "next_offset": offset + len(entries) if offset + len(entries) < total else None,
        "audited": "This read is recorded in the audit log (once per identical read every 15 minutes).",
    }
