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

"""The ``user_admin`` built-in group: users and groups, administered by an agent.

What Administration > Users and groups offers, callable by an agent acting for a
platform administrator:

- ``list_platform_users`` / ``get_platform_user``: the accounts, their source
  (SCIM, SSO, local), status, groups, last sign-in and what they own;
- ``preview_user_deactivation``: the cascade of one or many accounts, read
  without writing (``user_deactivation.preview_deactivation``);
- ``deactivate_platform_users`` / ``reactivate_platform_user``: the ONE cascade
  every deactivation path shares (``user_deactivation.deactivate_user`` and
  ``reactivate_user``), never a second implementation; a deactivation is a dry
  run unless ``dry_run`` is false;
- ``list_platform_groups`` / ``get_platform_group`` / ``add_group_members`` /
  ``remove_group_members``: group membership, a SCIM-managed group read-only;
- ``list_deactivation_review`` / ``resolve_deactivation_review_item``: the
  review queue the cascade fills.

Authorization is enforced twice: the turn wires the group only when the person
it acts for is a platform administrator (:func:`offers_user_admin`), and every
executor reads that person again and refuses when they are not one. Binding
the group to an agent is an administrator's act (``ADMIN_BOUND_BUILTIN_TOOL_IDS``).
Every write is audited with the administrator, the agent and the run.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

USER_ADMIN_GROUP = "user_admin"
_SOURCE = f"builtin:{USER_ADMIN_GROUP}"
REFUSAL = "Refused: only a platform administrator can administer users and groups."
#: At most this many rows a list call, and accounts a batch call.
MAX_PAGE = 200
MAX_BATCH = 100
#: Seconds a deactivation batch may spend before it answers, under the 300 s
#: tool timeout: the accounts not reached by then are answered ``not_reached``
#: so the caller always gets the per-user result of what was done (#2182).
DEACTIVATE_TIME_BUDGET_SECONDS = 240.0
#: The clock the budget reads (a test replaces it).
_clock = time.monotonic
_STATUSES = ("active", "inactive", "deprovisioned")
_SOURCES = ("scim", "sso", "local")


def offers_user_admin(builtin_tool_ids: Any, *, acting_user_id: str | None, acting_is_admin: bool) -> bool:
    """Whether a turn wires the group: bound to the agent, for a known administrator."""
    return bool(acting_user_id) and acting_is_admin and USER_ADMIN_GROUP in (builtin_tool_ids or ())


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _source_of(user: Any) -> str:
    if getattr(user, "scim_external_id", None):
        return "scim"
    return "sso" if getattr(user, "sso_provider", None) else "local"


def _status_of(user: Any) -> str:
    if getattr(user, "deprovisioned_at", None):
        return "deprovisioned"
    return "active" if getattr(user, "is_active", False) else "inactive"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clamp(value: Any, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


def user_filter_clauses(
    *,
    status: str | None = None,
    source: str | None = None,
    role: str | None = None,
    group_id: str | None = None,
    search: str | None = None,
    created_from: Any = None,
    created_to: Any = None,
) -> list[Any]:
    """The WHERE clauses of ``list_platform_users``; an unknown value raises ``ValueError``."""
    from sqlalchemy import or_, select

    from app.models.group import GroupMember
    from app.models.user import User

    clauses: list[Any] = []
    if status:
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {', '.join(_STATUSES)}")
        if status == "deprovisioned":
            clauses.append(User.deprovisioned_at.is_not(None))
        else:
            clauses.append(User.is_active.is_(status == "active"))
            if status == "inactive":
                clauses.append(User.deprovisioned_at.is_(None))
    if source:
        if source not in _SOURCES:
            raise ValueError(f"source must be one of {', '.join(_SOURCES)}")
        if source == "scim":
            clauses.append(User.scim_external_id.is_not(None))
        else:
            clauses.append(User.scim_external_id.is_(None))
            clauses.append(User.sso_provider.is_not(None) if source == "sso" else User.sso_provider.is_(None))
    if role:
        clauses.append(User.role == role)
    if group_id:
        clauses.append(User.id.in_(select(GroupMember.user_id).where(GroupMember.group_id == uuid.UUID(group_id))))
    term = (search or "").strip()
    if term:
        pattern = f"%{term}%"
        clauses.append(or_(User.full_name.ilike(pattern), User.email.ilike(pattern)))
    start, end = _parse_time(created_from), _parse_time(created_to)
    if start:
        clauses.append(User.created_at >= start)
    if end:
        clauses.append(User.created_at < end)
    return clauses


async def _last_logins(db: Any, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """The last sign-in of each account: its newest ``login`` audit row."""
    from sqlalchemy import func, select

    from app.models.audit_log import AuditLog

    if not user_ids:
        return {}
    rows = await db.execute(
        select(AuditLog.user_id, func.max(AuditLog.created_at))
        .where(AuditLog.action == "login", AuditLog.user_id.in_(user_ids))
        .group_by(AuditLog.user_id)
    )
    return {uid: at for uid, at in rows.all()}


async def _owned_counts(db: Any, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    from sqlalchemy import func, select

    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.integration import Integration

    counts: dict[uuid.UUID, dict[str, int]] = {
        uid: {"agents": 0, "assignments": 0, "integrations": 0} for uid in user_ids
    }
    if not user_ids:
        return counts
    queries = {
        "agents": select(Agent.user_id, func.count(Agent.id))
        .where(Agent.user_id.in_(user_ids))
        .group_by(Agent.user_id),
        "assignments": select(Assignment.user_id, func.count(Assignment.id))
        .where(Assignment.user_id.in_(user_ids))
        .group_by(Assignment.user_id),
        "integrations": select(Integration.user_id, func.count(Integration.id))
        .where(Integration.user_id.in_(user_ids))
        .group_by(Integration.user_id),
    }
    for key, query in queries.items():
        for uid, count in (await db.execute(query)).all():
            counts.setdefault(uid, {})[key] = int(count or 0)
    return counts


async def _groups_of(db: Any, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, str]]]:
    from sqlalchemy import select

    from app.models.group import Group, GroupMember

    groups: dict[uuid.UUID, list[dict[str, str]]] = {uid: [] for uid in user_ids}
    if not user_ids:
        return groups
    rows = await db.execute(
        select(GroupMember.user_id, Group.id, Group.name)
        .join(Group, Group.id == GroupMember.group_id)
        .where(GroupMember.user_id.in_(user_ids))
    )
    for uid, gid, name in rows.all():
        groups.setdefault(uid, []).append({"id": str(gid), "name": name})
    return groups


def _user_row(
    user: Any, *, groups: list[dict[str, str]], last_login: datetime | None, owned: dict[str, int]
) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "name": user.full_name,
        "email": user.email,
        "status": _status_of(user),
        "source": _source_of(user),
        "scim_external_id": user.scim_external_id,
        "role": user.role,
        "protected": bool(getattr(user, "deactivation_protected", False)),
        "groups": groups,
        "last_login": _iso(last_login),
        "created": _iso(user.created_at),
        "owned": owned,
    }


async def _resolve_users(db: Any, refs: list[str]) -> tuple[list[Any], list[dict[str, str]]]:
    """Accounts by id or email, in the order given; the unknown ones named."""
    from sqlalchemy import func, select

    from app.models.user import User

    found: list[Any] = []
    missing: list[dict[str, str]] = []
    for ref in refs:
        ref = str(ref).strip()
        user = None
        try:
            user = await db.get(User, uuid.UUID(ref))
        except ValueError:
            user = (await db.execute(select(User).where(func.lower(User.email) == ref.lower()))).scalar_one_or_none()
        if user is None:
            missing.append({"user": ref, "status": "refused", "reason": "No such user"})
        elif all(u.id != user.id for u in found):
            found.append(user)
    return found, missing


async def deactivation_refusal(db: Any, user: Any, actor: Any) -> str | None:
    """Why the tools refuse to deactivate *user*, else ``None`` (the cascade adds its own)."""
    from sqlalchemy import func, select

    from app.models.user import User, is_platform_admin

    if user.id == actor.id:
        return "Cannot deactivate yourself"
    if is_platform_admin(user):
        return "The platform admin account cannot be disabled"
    if getattr(user, "deactivation_protected", False):
        return "The account is protected (break-glass or service account)"
    if user.is_active and user.role == "admin":
        others = await db.scalar(
            select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True), User.id != user.id)
        )
        if not others:
            return "The last active administrator cannot be deactivated"
    return None


def build_user_admin_tools(
    *,
    user_id: str | None,
    caller_is_admin: bool,
    agent_name: str | None = None,
    run_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The group's definitions and executor for the administrator *user_id*."""

    async def _admin(db: Any) -> Any:
        from app.models.user import User

        if not caller_is_admin or not user_id:
            return None
        try:
            uid = uuid.UUID(str(user_id))
        except ValueError:
            return None
        person = await db.get(User, uid)
        if person is None or not getattr(person, "is_active", False) or not getattr(person, "is_admin", False):
            return None
        return person

    async def _audit(
        db: Any, admin: Any, *, action: str, entity_type: str, entity: Any, summary: str, details: dict[str, Any]
    ) -> None:
        from app.services.audit import log_audit

        await log_audit(
            db,
            user=admin,
            action=action,
            entity_type=entity_type,
            entity_id=getattr(entity, "id", None),
            entity_name=getattr(entity, "full_name", None) or getattr(entity, "name", None),
            summary=f"{summary} (agent {agent_name or 'unknown'})"[:500],
            details={"tool_group": USER_ADMIN_GROUP, "agent": agent_name, "run_id": run_id, **details},
        )

    async def _list_users(**args: Any) -> str:
        from sqlalchemy import func, select

        from app.core.database import async_session
        from app.models.user import User

        async with async_session() as db:
            if await _admin(db) is None:
                return REFUSAL
            try:
                clauses = user_filter_clauses(
                    status=args.get("status"),
                    source=args.get("source"),
                    role=args.get("role"),
                    group_id=args.get("group_id"),
                    search=args.get("search"),
                    created_from=args.get("created_from"),
                    created_to=args.get("created_to"),
                )
            except ValueError as exc:
                return f"Refused: {exc}"
            limit = _clamp(args.get("limit"), 50, MAX_PAGE)
            offset = max(0, int(args.get("offset") or 0))
            total = int(await db.scalar(select(func.count(User.id)).where(*clauses)) or 0)
            users = (
                (
                    await db.execute(
                        select(User)
                        .where(*clauses)
                        .order_by(func.lower(User.email), User.id)
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
            ids = [u.id for u in users]
            logins, owned, groups = await _last_logins(db, ids), await _owned_counts(db, ids), await _groups_of(db, ids)
            since, until = _parse_time(args.get("last_login_from")), _parse_time(args.get("last_login_to"))
            rows = []
            for user in users:
                last = logins.get(user.id)
                if (since and (last is None or last < since)) or (until and last is not None and last >= until):
                    continue
                rows.append(
                    _user_row(user, groups=groups.get(user.id, []), last_login=last, owned=owned.get(user.id, {}))
                )
            next_offset = offset + len(users) if offset + len(users) < total else None
            return _json({"total": total, "offset": offset, "next_offset": next_offset, "users": rows})

    async def _get_user(**args: Any) -> str:
        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.agent import Agent
        from app.models.assignment import Assignment
        from app.models.integration import Integration

        async with async_session() as db:
            if await _admin(db) is None:
                return REFUSAL
            users, missing = await _resolve_users(db, [args.get("user", "")])
            if not users:
                return f"Not found: {missing[0]['user']}"
            user = users[0]
            logins, owned, groups = (
                await _last_logins(db, [user.id]),
                await _owned_counts(db, [user.id]),
                await _groups_of(db, [user.id]),
            )
            row = _user_row(user, groups=groups[user.id], last_login=logins.get(user.id), owned=owned[user.id])
            row["deactivated_at"] = _iso(user.deactivated_at)
            row["deactivation_source"] = user.deactivation_source
            row["deprovisioned_at"] = _iso(user.deprovisioned_at)
            agents = (await db.execute(select(Agent).where(Agent.user_id == user.id).limit(MAX_PAGE))).scalars().all()
            row["agents"] = [
                {"id": str(a.id), "name": a.name, "enabled": a.enabled, "company_managed": bool(a.is_company_managed)}
                for a in agents
            ]
            acting = (
                (await db.execute(select(Assignment).where(Assignment.run_as_user_id == user.id).limit(MAX_PAGE)))
                .scalars()
                .all()
            )
            row["assignments_acting_as"] = [
                {"id": str(a.id), "name": a.name, "agent_id": str(a.agent_id), "enabled": a.enabled} for a in acting
            ]
            integrations = (
                (await db.execute(select(Integration).where(Integration.user_id == user.id).limit(MAX_PAGE)))
                .scalars()
                .all()
            )
            row["integrations"] = [
                {"id": str(i.id), "provider": i.provider, "name": i.name, "status": i.status} for i in integrations
            ]
            return _json(row)

    async def _preview(db: Any, admin: Any, users: list[Any]) -> list[dict[str, Any]]:
        from app.services import user_deactivation

        answers = []
        for user in users:
            preview = await user_deactivation.preview_deactivation(db, user)
            refusal = await deactivation_refusal(db, user, admin) or preview.get("refusal")
            answers.append({"email": user.email, **preview, "refusal": refusal})
        return answers

    async def _preview_deactivation(**args: Any) -> str:
        from app.core.database import async_session

        refs = list(args.get("users") or [])
        if not refs or len(refs) > MAX_BATCH:
            return f"Refused: give 1 to {MAX_BATCH} user ids or emails."
        async with async_session() as db:
            admin = await _admin(db)
            if admin is None:
                return REFUSAL
            users, missing = await _resolve_users(db, refs)
            return _json({"dry_run": True, "users": await _preview(db, admin, users), "unknown": missing})

    async def _deactivate(**args: Any) -> str:
        from app.core.database import async_session
        from app.services import user_deactivation

        refs = list(args.get("users") or [])
        reason = str(args.get("reason") or "").strip()
        dry_run = args.get("dry_run", True) is not False
        if not refs or len(refs) > MAX_BATCH:
            return f"Refused: give 1 to {MAX_BATCH} user ids or emails."
        if not reason:
            return "Refused: a reason is required."
        async with async_session() as db:
            admin = await _admin(db)
            if admin is None:
                return REFUSAL
            users, missing = await _resolve_users(db, refs)
            if dry_run:
                return _json({"dry_run": True, "users": await _preview(db, admin, users), "unknown": missing})
            label = (admin.full_name or "").strip() or admin.email
            results: list[dict[str, Any]] = list(missing)
            # rollback() expires every instance of the session (expire_on_commit
            # does not apply to it), the accounts not processed yet included:
            # the ids and emails answered are read up front, and once a rollback
            # happened ``admin`` and every later account are refreshed with an
            # awaited load before any attribute is read, for the rest of the batch.
            targets = [(user, user.id, user.email) for user in users]
            rolled_back = False
            started = _clock()
            not_reached: list[str] = []
            for user, user_id, email in targets:
                if not_reached or _clock() - started >= DEACTIVATE_TIME_BUDGET_SECONDS:
                    not_reached.append(email)
                    results.append({"user": email, "user_id": str(user_id), "status": "not_reached"})
                    continue
                if rolled_back:
                    await db.refresh(admin)
                    await db.refresh(user)
                refusal = await deactivation_refusal(db, user, admin)
                if refusal:
                    results.append({"user": email, "user_id": str(user_id), "status": "refused", "reason": refusal})
                    continue
                try:
                    outcome = await user_deactivation.deactivate_user(
                        db, user, source="admin", actor=admin, actor_label=label
                    )
                    await _audit(
                        db,
                        admin,
                        action="update",
                        entity_type="user",
                        entity=user,
                        summary=f"Deactivated {email} through the user-admin tools: {reason}",
                        details={"tool": "deactivate_platform_users", "reason": reason, "cascade": outcome.as_dict()},
                    )
                    await db.commit()
                except user_deactivation.DeactivationError as exc:
                    await db.rollback()
                    rolled_back = True
                    results.append({"user": email, "user_id": str(user_id), "status": "refused", "reason": str(exc)})
                    continue
                except Exception:
                    await db.rollback()
                    rolled_back = True
                    logger.warning("User-admin deactivation failed", exc_info=True)
                    results.append(
                        {"user": email, "user_id": str(user_id), "status": "error", "reason": "not deactivated"}
                    )
                    continue
                await user_deactivation.after_commit(async_session, user, outcome, label)
                results.append(
                    {
                        "user": email,
                        "status": "already_inactive" if outcome.already else "done",
                        **outcome.as_dict(),
                        "review_items": outcome.review_items,
                    }
                )
            answer: dict[str, Any] = {"dry_run": False, "reason": reason, "results": results}
            if not_reached:
                answer["not_reached"] = not_reached
                answer["next_step"] = (
                    f"The time budget of {int(DEACTIVATE_TIME_BUDGET_SECONDS)} s ran out: call again with "
                    "the accounts in not_reached, which were left untouched."
                )
            return _json(answer)

    async def _reactivate(**args: Any) -> str:
        from app.core.database import async_session
        from app.services import user_deactivation

        async with async_session() as db:
            admin = await _admin(db)
            if admin is None:
                return REFUSAL
            users, missing = await _resolve_users(db, [args.get("user", "")])
            if not users:
                return f"Not found: {missing[0]['user']}"
            user = users[0]
            if user.is_active:
                return _json({"user": user.email, "status": "already_active"})
            label = (admin.full_name or "").strip() or admin.email
            try:
                outcome = await user_deactivation.reactivate_user(
                    db, user, source="admin", actor=admin, actor_label=label
                )
                await _audit(
                    db,
                    admin,
                    action="update",
                    entity_type="user",
                    entity=user,
                    summary=f"Reactivated {user.email} through the user-admin tools",
                    details={"tool": "reactivate_platform_user", "cascade": outcome.as_dict()},
                )
                await db.commit()
            except user_deactivation.DeactivationError as exc:
                await db.rollback()
                return f"Refused: {exc}"
            return _json({"user": user.email, "status": "done", **outcome.as_dict()})

    async def _group_or_none(db: Any, group_id: Any) -> Any:
        from app.models.group import Group

        try:
            return await db.get(Group, uuid.UUID(str(group_id)))
        except ValueError:
            return None

    async def _list_groups(**args: Any) -> str:
        from sqlalchemy import func, or_, select

        from app.core.database import async_session
        from app.models.group import Group, GroupMember

        async with async_session() as db:
            if await _admin(db) is None:
                return REFUSAL
            limit = _clamp(args.get("limit"), 50, MAX_PAGE)
            offset = max(0, int(args.get("offset") or 0))
            clauses: list[Any] = []
            term = str(args.get("search") or "").strip()
            if term:
                clauses.append(or_(Group.name.ilike(f"%{term}%"), Group.description.ilike(f"%{term}%")))
            members = (
                select(func.count(GroupMember.id))
                .where(GroupMember.group_id == Group.id)
                .correlate(Group)
                .scalar_subquery()
            )
            total = int(await db.scalar(select(func.count(Group.id)).where(*clauses)) or 0)
            rows = await db.execute(
                select(Group, members)
                .where(*clauses)
                .order_by(func.lower(Group.name), Group.id)
                .limit(limit)
                .offset(offset)
            )
            groups = [
                {
                    "id": str(g.id),
                    "name": g.name,
                    "description": g.description,
                    "kind": g.kind or "auto",
                    "scim_managed": bool(g.scim_external_id),
                    "member_count": int(count or 0),
                }
                for g, count in rows.all()
            ]
            next_offset = offset + len(groups) if offset + len(groups) < total else None
            return _json({"total": total, "offset": offset, "next_offset": next_offset, "groups": groups})

    async def _get_group(**args: Any) -> str:
        from sqlalchemy import func, select

        from app.core.database import async_session
        from app.models.group import GroupMember
        from app.models.user import User

        async with async_session() as db:
            if await _admin(db) is None:
                return REFUSAL
            group = await _group_or_none(db, args.get("group_id"))
            if group is None:
                return "Not found: no such group"
            limit = _clamp(args.get("limit"), 100, MAX_PAGE)
            offset = max(0, int(args.get("offset") or 0))
            total = int(
                await db.scalar(select(func.count(GroupMember.id)).where(GroupMember.group_id == group.id)) or 0
            )
            rows = await db.execute(
                select(GroupMember, User)
                .join(User, User.id == GroupMember.user_id)
                .where(GroupMember.group_id == group.id)
                .order_by(func.lower(User.email))
                .limit(limit)
                .offset(offset)
            )
            members = [
                {
                    "user_id": str(u.id),
                    "email": u.email,
                    "name": u.full_name,
                    "status": _status_of(u),
                    "member_source": getattr(m, "source", None),
                }
                for m, u in rows.all()
            ]
            return _json(
                {
                    "id": str(group.id),
                    "name": group.name,
                    "description": group.description,
                    "kind": group.kind or "auto",
                    "scim_managed": bool(group.scim_external_id),
                    "member_count": total,
                    "offset": offset,
                    "members": members,
                }
            )

    async def _change_members(args: dict[str, Any], *, add: bool) -> str:
        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.group import GroupMember
        from app.services.quota import invalidate_membership_quota_cache

        refs = list(args.get("users") or [])
        if not refs or len(refs) > MAX_BATCH:
            return f"Refused: give 1 to {MAX_BATCH} user ids or emails."
        async with async_session() as db:
            admin = await _admin(db)
            if admin is None:
                return REFUSAL
            group = await _group_or_none(db, args.get("group_id"))
            if group is None:
                return "Not found: no such group"
            if group.scim_external_id:
                return (
                    f"Refused: the group {group.name} is managed by the identity provider (SCIM); "
                    "change its members there."
                )
            users, missing = await _resolve_users(db, refs)
            results: list[dict[str, Any]] = list(missing)
            changed: list[uuid.UUID] = []
            for user in users:
                existing = (
                    await db.execute(
                        select(GroupMember).where(GroupMember.group_id == group.id, GroupMember.user_id == user.id)
                    )
                ).scalar_one_or_none()
                if add and existing is None:
                    db.add(GroupMember(group_id=group.id, user_id=user.id, added_by=admin.id))
                    changed.append(user.id)
                    results.append({"user": user.email, "status": "added"})
                elif not add and existing is not None:
                    await db.delete(existing)
                    changed.append(user.id)
                    results.append({"user": user.email, "status": "removed"})
                else:
                    results.append({"user": user.email, "status": "already_member" if add else "not_a_member"})
            if changed:
                await db.flush()
                verb = "Added" if add else "Removed"
                await _audit(
                    db,
                    admin,
                    action="bind" if add else "unbind",
                    entity_type="group",
                    entity=group,
                    summary=f"{verb} {len(changed)} member(s) {'to' if add else 'from'} group '{group.name}' through the user-admin tools",
                    details={
                        "tool": "add_group_members" if add else "remove_group_members",
                        "user_ids": [str(u) for u in changed],
                    },
                )
            await db.commit()
            if changed:
                try:
                    await invalidate_membership_quota_cache(changed)
                except Exception:
                    logger.warning("Quota cache not invalidated after a membership change", exc_info=True)
            return _json({"group": group.name, "results": results})

    async def _add_members(**args: Any) -> str:
        return await _change_members(args, add=True)

    async def _remove_members(**args: Any) -> str:
        return await _change_members(args, add=False)

    async def _list_review(**args: Any) -> str:
        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.user import User
        from app.models.user_deactivation import UserDeactivation

        async with async_session() as db:
            if await _admin(db) is None:
                return REFUSAL
            include_resolved = bool(args.get("include_resolved"))
            query = (
                select(UserDeactivation, User)
                .join(User, User.id == UserDeactivation.user_id)
                .where(UserDeactivation.reactivated_at.is_(None))
                .order_by(UserDeactivation.deactivated_at.desc())
            )
            if args.get("user"):
                users, _missing = await _resolve_users(db, [args["user"]])
                if not users:
                    return "Not found: no such user"
                query = query.where(UserDeactivation.user_id == users[0].id)
            entries = []
            for episode, user in (await db.execute(query.limit(MAX_PAGE))).all():
                for item in episode.review_items or []:
                    if item.get("resolution") and not include_resolved:
                        continue
                    entries.append(
                        {
                            "deactivation_id": str(episode.id),
                            "user": user.email,
                            "deactivated_at": _iso(episode.deactivated_at),
                            **item,
                        }
                    )
                    if len(entries) >= MAX_PAGE:
                        break
            return _json({"items": entries})

    async def _resolve_review(**args: Any) -> str:
        from app.core.database import async_session
        from app.models.user_deactivation import UserDeactivation
        from app.services import user_deactivation

        async with async_session() as db:
            admin = await _admin(db)
            if admin is None:
                return REFUSAL
            try:
                episode = await db.get(UserDeactivation, uuid.UUID(str(args.get("deactivation_id"))))
            except ValueError:
                episode = None
            if episode is None:
                return "Not found: no such deactivation"
            resolution = str(args.get("resolution") or "")
            try:
                item = await user_deactivation.resolve_review_item(
                    db, episode, str(args.get("key") or ""), resolution=resolution, admin=admin
                )
            except user_deactivation.DeactivationError as exc:
                return f"Refused: {exc}"
            await _audit(
                db,
                admin,
                action="update",
                entity_type="user",
                entity=None,
                summary=f"Review item {item.get('key')} marked {resolution} through the user-admin tools",
                details={
                    "tool": "resolve_deactivation_review_item",
                    "deactivation_id": str(episode.id),
                    "key": item.get("key"),
                },
            )
            await db.commit()
            return _json({"status": "done", "item": item})

    users_param = {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": MAX_BATCH,
        "description": "User ids or emails, 1 to 100.",
    }
    page = {
        "limit": {"type": "integer", "description": "Rows a call, at most 200 (default 50)."},
        "offset": {"type": "integer", "description": "Rows to skip: the next_offset of the previous call."},
    }
    group_id = {"type": "string", "description": "Group id (list_platform_groups)."}
    specs: list[tuple[str, str, dict[str, Any], list[str], Any]] = [
        (
            "list_platform_users",
            "Platform accounts, paged: id, name, email, status (active, inactive, deprovisioned), source (scim, "
            "sso, local), SCIM externalId, role, groups, last sign-in, created, counts of owned agents, assignments, "
            "integrations. Filters: status, source, role, group_id, search (name or email), created and last sign-in "
            "ranges (ISO). Administrators only.",
            {
                "status": {"type": "string", "enum": list(_STATUSES)},
                "source": {"type": "string", "enum": list(_SOURCES)},
                "role": {"type": "string", "description": "admin or user"},
                "group_id": group_id,
                "search": {"type": "string"},
                "created_from": {"type": "string"},
                "created_to": {"type": "string"},
                "last_login_from": {"type": "string", "description": "Keeps the page rows signed in since."},
                "last_login_to": {"type": "string", "description": "Keeps the page rows not signed in since."},
                **page,
            },
            [],
            _list_users,
        ),
        (
            "get_platform_user",
            "One account in full: status, source, groups, last sign-in, deactivation state, its agents, the "
            "assignments that run as it and its integrations. Administrators only.",
            {"user": {"type": "string", "description": "User id or email."}},
            ["user"],
            _get_user,
        ),
        (
            "preview_user_deactivation",
            "Dry run of the deactivation cascade for 1 to 100 accounts: per account what would be revoked, "
            "cancelled, disabled, paused or handed over, the successor and the refusal if any. Writes nothing.",
            {"users": users_param},
            ["users"],
            _preview_deactivation,
        ),
        (
            "deactivate_platform_users",
            "Deactivate 1 to 100 accounts through the platform's deactivation cascade (keys revoked, runs, timers and "
            "scheduled actions cancelled, private agents and integrations off, shared objects handed to a successor "
            "administrator with review entries and a notification). dry_run is true unless set false: preview first. "
            "Refuses yourself, the last active administrator, the platform admin and protected accounts. Idempotent.",
            {
                "users": users_param,
                "reason": {"type": "string", "description": "Why, kept in the audit log."},
                "dry_run": {"type": "boolean", "description": "Default true: preview only."},
            },
            ["users", "reason"],
            _deactivate,
        ),
        (
            "reactivate_platform_user",
            "Reactivate one account and restore what its deactivation turned off, from the cascade's snapshot.",
            {"user": {"type": "string", "description": "User id or email."}},
            ["user"],
            _reactivate,
        ),
        (
            "list_platform_groups",
            "Groups, paged: id, name, description, kind, member count, scim_managed (read-only here).",
            {"search": {"type": "string"}, **page},
            [],
            _list_groups,
        ),
        (
            "get_platform_group",
            "One group with its members, paged.",
            {"group_id": group_id, **page},
            ["group_id"],
            _get_group,
        ),
        (
            "add_group_members",
            "Add 1 to 100 accounts to a group. Refused on a SCIM-managed group.",
            {"group_id": group_id, "users": users_param},
            ["group_id", "users"],
            _add_members,
        ),
        (
            "remove_group_members",
            "Remove 1 to 100 accounts from a group. Refused on a SCIM-managed group.",
            {"group_id": group_id, "users": users_param},
            ["group_id", "users"],
            _remove_members,
        ),
        (
            "list_deactivation_review",
            "The review queue of the deactivations still in force: objects handed over or kept, to confirm.",
            {
                "user": {"type": "string", "description": "Only this deactivated account (id or email)."},
                "include_resolved": {"type": "boolean"},
            },
            [],
            _list_review,
        ),
        (
            "resolve_deactivation_review_item",
            "Mark one review item kept, reassigned or deleted, as the review screen does (the act itself is yours).",
            {
                "deactivation_id": {"type": "string"},
                "key": {"type": "string", "description": "The item key (list_deactivation_review)."},
                "resolution": {"type": "string", "enum": ["keep", "reassign", "delete"]},
            },
            ["deactivation_id", "key", "resolution"],
            _resolve_review,
        ),
    ]
    executor = CallableToolExecutor()
    definitions: list[ToolDefinition] = []
    for name, description, properties, required, handler in specs:
        executor.register(name, handler)
        definitions.append(
            ToolDefinition(
                name=name,
                description=description,
                input_schema={"type": "object", "properties": properties, "required": required},
                source=_SOURCE,
                priority=0,
            )
        )
    return definitions, executor


__all__ = [
    "MAX_BATCH",
    "MAX_PAGE",
    "REFUSAL",
    "USER_ADMIN_GROUP",
    "build_user_admin_tools",
    "deactivation_refusal",
    "offers_user_admin",
    "user_filter_clauses",
]
