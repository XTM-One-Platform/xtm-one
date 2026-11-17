# SPDX-License-Identifier: Apache-2.0

"""Group managers: the people an admin elected to follow a group's AI usage.

The ONE place the rule lives. A manager reads the LLM Analytics of their team:
the CURRENT members of the groups they manage - membership as it is today,
like the analytics group view (``tracing.usage_breakdown``) - administrators
left out. Nothing else: no trace or session list, no trace content.
Electing a manager is an admin's act (``api/v1/groups.py``); being one grants
nothing else - no edit right, no group share, no membership.

``team_scope`` resolves the groups a read is narrowed to and their members,
the same for every surface: the REST analytics routes, and the
``platform_insights`` analytics tool (``platform_insights.scope``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.group import Group, GroupManager, GroupMember
from app.models.user import User

#: Why a non-admin is refused a group's usage.
NOT_A_MANAGER_DETAIL = "You can only read the usage of the groups you manage."

#: The ``user_ids`` filter of a team with no member today. An empty list reads
#: as "no filter" to the trace stores, so a team nobody is in is narrowed to an
#: id no account carries.
NO_MEMBER_ID = uuid.UUID(int=0)


@dataclass(frozen=True)
class ManagedGroup:
    id: str
    name: str


@dataclass(frozen=True)
class TeamScope:
    """The groups a read is narrowed to and the users in them today."""

    group_ids: tuple[str, ...]
    member_ids: frozenset[uuid.UUID]

    def user_ids_csv(self) -> str:
        """The trace stores' ``user_ids`` filter: the members, or an id nobody carries."""
        ids = self.member_ids or frozenset({NO_MEMBER_ID})
        return ",".join(sorted(str(member) for member in ids))

    def includes(self, user_id: object) -> bool:
        parsed = user_id if isinstance(user_id, uuid.UUID) else safe_uuid(str(user_id) if user_id else "")
        return parsed is not None and parsed in self.member_ids


def team_member_clause() -> ColumnElement[bool]:
    """The ``group_members`` rows a manager's team counts: every member but the administrators."""
    return GroupMember.user_id.not_in(select(User.id).where(User.role == "admin"))


async def managed_groups(db: AsyncSession, user_id: uuid.UUID | str) -> list[ManagedGroup]:
    """The groups *user_id* manages, by name."""
    parsed = user_id if isinstance(user_id, uuid.UUID) else safe_uuid(str(user_id))
    if parsed is None:
        return []
    rows = await db.execute(
        select(Group.id, Group.name)
        .join(GroupManager, GroupManager.group_id == Group.id)
        .where(GroupManager.user_id == parsed)
        .order_by(Group.name)
    )
    return [ManagedGroup(id=str(row.id), name=row.name) for row in rows.all()]


async def members_of(
    db: AsyncSession, group_ids: Iterable[str | uuid.UUID], *, team: bool = False
) -> frozenset[uuid.UUID]:
    """The users in *group_ids* today; with *team*, the ones their manager reads (``team_member_clause``)."""
    parsed = [gid if isinstance(gid, uuid.UUID) else safe_uuid(str(gid)) for gid in group_ids]
    ids = [gid for gid in parsed if gid is not None]
    if not ids:
        return frozenset()
    statement = select(GroupMember.user_id).where(GroupMember.group_id.in_(ids))
    if team:
        statement = statement.where(team_member_clause())
    rows = await db.execute(statement.distinct())
    return frozenset(user_id for (user_id,) in rows.all())


async def team_scope(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    platform: bool,
    requested: Iterable[str] | None,
) -> TeamScope | None:
    """The team a read is narrowed to, or ``None`` when the caller names no group and has none.

    *platform* is the caller's admin reach (``user.is_admin`` on the REST API,
    ``agent_tool_admin_bypass`` for an agent tool): any group may be named,
    and it reads every member. A caller without it names only groups they
    manage (403 otherwise, the groups being listable by every user) and,
    naming none, reads all of them - their team, administrators left out.
    A malformed group id is a 422, like every analytics filter id.
    """
    names = [value.strip() for value in (requested or []) if value and value.strip()]
    parsed: list[uuid.UUID] = []
    for value in names:
        gid = safe_uuid(value)
        if gid is None:
            raise HTTPException(status_code=422, detail=f"Invalid group_ids value: {value!r}")
        parsed.append(gid)
    if platform:
        if not parsed:
            return None
        group_ids = tuple(sorted({str(gid) for gid in parsed}))
        return TeamScope(group_ids=group_ids, member_ids=await members_of(db, group_ids))
    managed = {group.id for group in await managed_groups(db, user_id)}
    if not parsed:
        if not managed:
            return None
        group_ids = tuple(sorted(managed))
    else:
        group_ids = tuple(sorted({str(gid) for gid in parsed}))
        if any(gid not in managed for gid in group_ids):
            raise HTTPException(status_code=403, detail=NOT_A_MANAGER_DETAIL)
    return TeamScope(group_ids=group_ids, member_ids=await members_of(db, group_ids, team=True))
