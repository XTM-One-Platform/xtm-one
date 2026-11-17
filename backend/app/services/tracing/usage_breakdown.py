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

"""LLM usage broken down by user and by group (the admin LLM Analytics dashboard).

Both views fold the per-user rows of ``TraceReadStore.analytics_usage_by_user``,
so every trace of the window lands in exactly one row of the user view and that
view adds up to the dashboard summary. The users past the requested ``limit``
are folded together as ``other_users``, and a trace with no user (an anonymous
channel sender, a public widget visitor, the ``/mcp/global`` key, an ownerless
assignment run) is reported apart as ``unattributed``: neither is ever dropped.

Groups are read as they are NOW: a trace counts for the groups its user belongs
to today, not the ones they belonged to when it ran (traces record no group).
A user in several groups counts in each of them, so group rows can add up to
more than the total; ``overlapping_membership`` says when that happened. Users
in no group are reported apart as ``ungrouped``.

A read narrowed to some groups (an admin's group filter, a group manager's
team) passes them as ``group_ids``: only those groups are listed and
named, so a manager never learns the rest of a member's memberships. A
manager's read also passes ``team``: a group's member count is then its team,
administrators left out (``group_managers.team_member_clause``).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.group_managers import team_member_clause

#: Ids per ``IN (...)`` lookup: asyncpg caps one statement at 32767 parameters.
_LOOKUP_CHUNK = 5000


@dataclass
class _Usage:
    trace_count: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    user_ids: set[str] = field(default_factory=set)

    def add(self, row: dict[str, Any]) -> None:
        self.trace_count += int(row["trace_count"])
        self.total_tokens += int(row["total_tokens"])
        self.total_cost += float(row["total_cost"])
        if row.get("user_id"):
            self.user_ids.add(row["user_id"])

    def totals(self) -> dict[str, Any]:
        return {
            "trace_count": self.trace_count,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "avg_cost_per_trace": _per(self.total_cost, self.trace_count),
        }


def _per(total: float, count: int) -> float:
    return total / count if count else 0.0


def _by_cost(item: dict[str, Any]) -> tuple[float, int]:
    return (-float(item["total_cost"]), -int(item["trace_count"]))


def _chunks(values: Sequence[uuid.UUID]) -> Iterable[Sequence[uuid.UUID]]:
    for index in range(0, len(values), _LOOKUP_CHUNK):
        yield values[index : index + _LOOKUP_CHUNK]


def _unattributed(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    usage = _Usage()
    for row in rows:
        if not row.get("user_id"):
            usage.add(row)
    return usage.totals() if usage.trace_count else None


def _folded_users(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    usage = _Usage()
    for row in rows:
        usage.add(row)
    return {"user_count": len(usage.user_ids), **usage.totals()} if usage.user_ids else None


def _attributed(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("user_id") and int(row["trace_count"]) > 0]


def _user_uuids(rows: Iterable[dict[str, Any]]) -> list[uuid.UUID]:
    return [parsed for row in rows if (parsed := safe_uuid(row["user_id"])) is not None]


@dataclass(frozen=True)
class _Account:
    email: str
    full_name: str | None
    is_active: bool


async def _load_accounts(db: AsyncSession, user_ids: Sequence[uuid.UUID]) -> dict[str, _Account]:
    accounts: dict[str, _Account] = {}
    for chunk in _chunks(user_ids):
        result = await db.execute(
            select(User.id, User.email, User.full_name, User.is_active).where(User.id.in_(chunk))
        )
        for user_id, email, full_name, is_active in result.all():
            accounts[str(user_id)] = _Account(email=email, full_name=full_name, is_active=bool(is_active))
    return accounts


async def _load_memberships(
    db: AsyncSession, user_ids: Sequence[uuid.UUID], group_ids: Collection[str] | None = None
) -> dict[str, set[str]]:
    """``user id -> group ids`` of *user_ids* today, only within *group_ids* when set."""
    allowed = None if group_ids is None else {str(group_id) for group_id in group_ids}
    memberships: dict[str, set[str]] = {}
    for chunk in _chunks(user_ids):
        result = await db.execute(
            select(GroupMember.user_id, GroupMember.group_id).where(GroupMember.user_id.in_(chunk))
        )
        for user_id, group_id in result.all():
            if allowed is None or str(group_id) in allowed:
                memberships.setdefault(str(user_id), set()).add(str(group_id))
    return memberships


async def _load_groups(
    db: AsyncSession, memberships: dict[str, set[str]], *, team: bool = False
) -> dict[str, tuple[str, int]]:
    """The groups ``memberships`` names: ``id -> (name, current member count)``, *team* members only when set."""
    group_ids = sorted(
        {parsed for ids in memberships.values() for group_id in ids if (parsed := safe_uuid(group_id)) is not None},
        key=str,
    )
    counted = GroupMember.group_id == Group.id
    if team:
        counted = and_(counted, team_member_clause())
    groups: dict[str, tuple[str, int]] = {}
    for chunk in _chunks(group_ids):
        result = await db.execute(
            select(Group.id, Group.name, func.count(GroupMember.id))
            .outerjoin(GroupMember, counted)
            .where(Group.id.in_(chunk))
            .group_by(Group.id, Group.name)
        )
        for group_id, name, members in result.all():
            groups[str(group_id)] = (name, int(members))
    return groups


async def usage_by_user(
    db: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    limit: int,
    group_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """The ``limit`` users who cost the most over the window, with who they are.

    The users past ``limit`` come back as one ``other_users`` total, so the
    items, that total and ``unattributed`` still add up to the window's cost.
    A user's ``group_names`` are within *group_ids* when set.
    """
    attributed = sorted(_attributed(rows), key=lambda row: (*_by_cost(row), row["user_id"]))
    shown, rest = attributed[:limit], attributed[limit:]
    shown_ids = _user_uuids(shown)
    accounts = await _load_accounts(db, shown_ids)
    memberships = await _load_memberships(db, shown_ids, group_ids)
    groups = await _load_groups(db, memberships)

    items: list[dict[str, Any]] = []
    for row in shown:
        account = accounts.get(row["user_id"])
        group_names = sorted(
            (groups[group_id][0] for group_id in memberships.get(row["user_id"], ()) if group_id in groups),
            key=str.casefold,
        )
        items.append(
            {
                "user_id": row["user_id"],
                "user_email": account.email if account else None,
                "user_name": account.full_name if account else None,
                "is_active": account.is_active if account else None,
                "group_names": group_names,
                "agent_count": int(row["agent_count"]),
                "trace_count": int(row["trace_count"]),
                "total_tokens": int(row["total_tokens"]),
                "total_cost": float(row["total_cost"]),
                "avg_cost_per_trace": _per(float(row["total_cost"]), int(row["trace_count"])),
            }
        )
    return {
        "items": items,
        "total_users": len(attributed),
        "other_users": _folded_users(rest),
        "unattributed": _unattributed(rows),
    }


async def usage_by_group(
    db: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    group_ids: Collection[str] | None = None,
    team: bool = False,
) -> dict[str, Any]:
    """Every group whose members used the platform over the window, costliest first.

    Only the groups in *group_ids* when set: a user in none of them is
    ``ungrouped`` there, and an overlap counts among them alone. With *team*
    (a manager's read), a group's ``member_count`` is its team's.
    """
    attributed = _attributed(rows)
    memberships = await _load_memberships(db, _user_uuids(attributed), group_ids)
    groups = await _load_groups(db, memberships, team=team)

    per_group: dict[str, _Usage] = {}
    ungrouped = _Usage()
    overlapping = False
    for row in attributed:
        group_ids = [group_id for group_id in memberships.get(row["user_id"], ()) if group_id in groups]
        if not group_ids:
            ungrouped.add(row)
            continue
        overlapping = overlapping or len(group_ids) > 1
        for group_id in group_ids:
            per_group.setdefault(group_id, _Usage()).add(row)

    items = [
        {
            "group_id": group_id,
            "group_name": groups[group_id][0],
            "member_count": groups[group_id][1],
            "active_user_count": len(usage.user_ids),
            "avg_cost_per_active_user": _per(usage.total_cost, len(usage.user_ids)),
            **usage.totals(),
        }
        for group_id, usage in per_group.items()
    ]
    items.sort(key=lambda item: (*_by_cost(item), item["group_name"].casefold(), item["group_id"]))
    return {
        "items": items,
        "ungrouped": (
            {
                "active_user_count": len(ungrouped.user_ids),
                "avg_cost_per_active_user": _per(ungrouped.total_cost, len(ungrouped.user_ids)),
                **ungrouped.totals(),
            }
            if ungrouped.trace_count
            else None
        ),
        "unattributed": _unattributed(rows),
        "overlapping_membership": overlapping,
    }
