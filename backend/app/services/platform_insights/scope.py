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

"""Who a platform insights call reads for, and the references it names.

``load_scope`` is the one rule: :func:`agent_tool_admin_bypass` on the calling
user decides between the platform (an administrator's agent, unless strict
admin access for agent tools is on) and the caller's own slice - their own
objects, their own usage, their own groups. A group manager's slice also
reaches the LLM ANALYTICS of the groups they manage (the ``team`` scope:
``services/group_managers.py``, administrators left out), which pass
``team=True`` to the resolvers; traces, sessions, objects, adoption and the
audit log stay their own (``InsightScope.without_team``). The people
and groups a call names are resolved here too
(``resolve_users``, ``resolve_groups``), by id or by email / exact name, and
refused when the slice does not reach them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import agent_tool_admin_bypass, get_user_group_ids
from app.core.utils import safe_uuid
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.group_managers import ManagedGroup, managed_groups, members_of
from app.services.tracing.read_store import ANALYTICS_PERIODS

PLATFORM_SCOPE = "platform"
SELF_SCOPE = "self"
TEAM_SCOPE = "team"

#: The most people or groups one call may name.
MAX_REFERENCES = 50

#: The periods every tool reads, as the analytics dashboard offers them.
PERIODS = ANALYTICS_PERIODS

COST_MASKED_NOTE = (
    "Cost data is hidden on this platform (OBSERVABILITY_COST_DATA_ENABLED is off): every cost is "
    "served as null. Do not present it as zero."
)
CURRENT_MEMBERSHIP_NOTE = (
    "Groups are read as they are NOW: a trace counts for the groups its user belongs to today, not "
    "the ones they belonged to when it ran (traces record no group)."
)


class InsightRefusedError(Exception):
    """A call the tool answers with an error and no data, with the sentence to return."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class InsightScope:
    """The caller and how far their read reaches."""

    user: User
    platform: bool
    group_ids: tuple[str, ...]
    #: The groups the caller manages (never set in the platform scope).
    managed: tuple[ManagedGroup, ...] = ()
    #: Their members today but the administrators: the people whose usage a manager reads.
    team_member_ids: frozenset[uuid.UUID] = frozenset()

    @property
    def label(self) -> str:
        if self.platform:
            return PLATFORM_SCOPE
        return TEAM_SCOPE if self.managed else SELF_SCOPE

    @property
    def note(self) -> str:
        if self.platform:
            return "Platform-wide: every user, agent and object of this platform (administrator scope)."
        if self.managed:
            names = ", ".join(group.name for group in self.managed)
            return (
                f"Your own slice, plus the LLM analytics of the current members of the groups you manage, "
                f"administrators excepted: {names}. Traces, sessions, objects, AI adoption and the audit log stay "
                "your own; platform-wide figures need an administrator's agent."
            )
        return (
            "Your own slice only: the objects you own, the usage you generated and the groups you belong "
            "to. Platform-wide figures need an administrator's agent."
        )

    @property
    def managed_group_ids(self) -> frozenset[str]:
        return frozenset(group.id for group in self.managed)

    def usage_reach(self) -> set[str]:
        """Whose usage a non-platform read covers: the caller and the members of their teams."""
        return {str(self.user.id), *(str(member) for member in self.team_member_ids)}

    def without_team(self) -> InsightScope:
        """The caller's own slice: what a read other than LLM analytics serves a group manager."""
        return replace(self, managed=(), team_member_ids=frozenset())

    def header(self) -> dict[str, str]:
        return {"scope": self.label, "scope_note": self.note}


async def load_scope(db: AsyncSession, user_id: str, *, direct: bool = False) -> InsightScope:
    """The scope of *user_id*'s call; :class:`InsightRefusedError` for a caller with no account.

    *direct* is a person's own REST call (the analytics export), not their
    agent's: an administrator reads the platform, as every admin REST route.
    Refused inside a Concierge run or turn, or a sandbox copy's turn: these
    reads apply no consent rule.
    """
    from app.services.concierge.identity import in_concierge_reach

    if not direct and in_concierge_reach():
        raise InsightRefusedError("Error: platform insights are not available to the Concierge.")
    parsed = safe_uuid(user_id) if user_id else None
    if parsed is None:
        raise InsightRefusedError("Error: platform insights need a signed-in user; this caller has no account.")
    user = (await db.execute(select(User).where(User.id == parsed))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise InsightRefusedError("Error: the calling user was not found.")
    group_ids = await get_user_group_ids(db, parsed)
    platform = user.is_admin if direct else agent_tool_admin_bypass(user)
    managed: tuple[ManagedGroup, ...] = ()
    members: frozenset[uuid.UUID] = frozenset()
    if not platform:
        managed = tuple(await managed_groups(db, parsed))
        members = await members_of(db, [group.id for group in managed], team=True)
    return InsightScope(
        user=user,
        platform=platform,
        group_ids=tuple(group_ids),
        managed=managed,
        team_member_ids=members,
    )


def as_list(value: Any) -> list[str]:
    """A list argument however the model wrote it: a list, or one comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list | tuple):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


@dataclass(frozen=True)
class ResolvedGroup:
    id: str
    name: str


async def resolve_groups(
    db: AsyncSession, refs: Any, scope: InsightScope, *, team: bool = False
) -> list[ResolvedGroup]:
    """The groups *refs* name, by id or exact name; one that matches nothing refuses the call.

    An exact name wins; a name matching one group case-insensitively is accepted
    too, an ambiguous one is refused. Outside the platform scope, only the
    caller's own groups can be named - and, for a usage read (*team*), the
    groups they manage.
    """
    entries = as_list(refs)
    if not entries:
        return []
    if len(entries) > MAX_REFERENCES:
        raise InsightRefusedError(f"Error: name at most {MAX_REFERENCES} groups in one call.")
    rows = (await db.execute(select(Group.id, Group.name))).all()
    by_id = {str(row.id): row.name for row in rows}
    resolved: list[ResolvedGroup] = []
    unknown: list[str] = []
    for entry in entries:
        parsed = safe_uuid(entry)
        if parsed is not None and str(parsed) in by_id:
            resolved.append(ResolvedGroup(str(parsed), by_id[str(parsed)]))
            continue
        exact = [gid for gid, name in by_id.items() if name == entry]
        folded = exact or [gid for gid, name in by_id.items() if name.casefold() == entry.casefold()]
        if len(folded) == 1:
            resolved.append(ResolvedGroup(folded[0], by_id[folded[0]]))
        elif len(folded) > 1:
            raise InsightRefusedError(f"Error: the group name {entry!r} matches several groups; name it by id.")
        else:
            unknown.append(entry)
    if unknown:
        raise InsightRefusedError(f"Error: no group matches {', '.join(repr(u) for u in unknown)}.")
    if not scope.platform:
        reachable = set(scope.group_ids) | (scope.managed_group_ids if team else frozenset())
        foreign = [group.name for group in resolved if group.id not in reachable]
        if foreign:
            raise InsightRefusedError(
                "Error: outside the administrator scope you can only name your own groups"
                + (" and the groups you manage" if team else "")
                + f"; {', '.join(repr(name) for name in foreign)} is neither."
            )
    return list({group.id: group for group in resolved}.values())


async def group_member_ids(db: AsyncSession, groups: list[ResolvedGroup]) -> set[str]:
    """The users in *groups* TODAY (current membership, as the dashboard's group view reads it)."""
    if not groups:
        return set()
    rows = await db.execute(
        select(GroupMember.user_id).where(GroupMember.group_id.in_([uuid.UUID(g.id) for g in groups]))
    )
    return {str(user_id) for (user_id,) in rows.all()}


@dataclass(frozen=True)
class ResolvedUser:
    id: str
    email: str


async def resolve_users(
    db: AsyncSession, refs: Any, scope: InsightScope, *, team: bool = False
) -> list[ResolvedUser]:
    """The people *refs* name, by id or email; one that matches nobody refuses the call.

    Outside the platform scope only the caller - and, for a usage read
    (*team*), the members of the groups they manage - can be named.
    """
    entries = as_list(refs)
    if not entries:
        return []
    if len(entries) > MAX_REFERENCES:
        raise InsightRefusedError(f"Error: name at most {MAX_REFERENCES} people in one call.")
    ids = [parsed for entry in entries if (parsed := safe_uuid(entry)) is not None]
    emails = [entry.casefold() for entry in entries if safe_uuid(entry) is None]
    found: dict[str, ResolvedUser] = {}
    if ids:
        for row in (await db.execute(select(User.id, User.email).where(User.id.in_(ids)))).all():
            found[str(row.id)] = ResolvedUser(str(row.id), row.email)
    if emails:
        rows = await db.execute(select(User.id, User.email).where(func.lower(User.email).in_(emails)))
        for row in rows.all():
            found[str(row.id)] = ResolvedUser(str(row.id), row.email)
    known_emails = {user.email.casefold() for user in found.values()}
    unknown: list[str] = []
    for entry in entries:
        parsed = safe_uuid(entry)
        if (str(parsed) not in found) if parsed is not None else (entry.casefold() not in known_emails):
            unknown.append(entry)
    if unknown:
        raise InsightRefusedError(f"Error: no user matches {', '.join(repr(u) for u in unknown)}.")
    if not scope.platform:
        reachable = scope.usage_reach() if team else {str(scope.user.id)}
        if set(found) - reachable:
            raise InsightRefusedError(
                "Error: outside the administrator scope you can only name yourself"
                + (" and the members of the groups you manage." if team else ".")
            )
    return list(found.values())


def parse_instant(value: Any, *, label: str) -> datetime | None:
    """An ISO-8601 date or datetime argument as an aware UTC instant (naive = UTC)."""
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise InsightRefusedError(f"Error: {label} must be an ISO-8601 date or datetime, not {value!r}.") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def resolve_period(period: Any, date_from: Any, date_to: Any) -> tuple[str, datetime, datetime]:
    """``(period, start, end)`` of a call: 7d / 30d / 90d back from now, or custom with both dates."""
    value = str(period or "30d").strip().lower()
    now = datetime.now(UTC)
    if value == "custom":
        start = parse_instant(date_from, label="date_from")
        end = parse_instant(date_to, label="date_to")
        if start is None or end is None:
            raise InsightRefusedError("Error: period custom needs both date_from and date_to.")
        if end <= start:
            raise InsightRefusedError("Error: date_to must be after date_from.")
        return value, start, end
    if value not in PERIODS:
        raise InsightRefusedError(f"Error: period must be one of {', '.join(PERIODS)} or custom.")
    return value, now - PERIODS[value], now
