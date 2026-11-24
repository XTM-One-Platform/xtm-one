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

"""The person a ``needs_human`` work item waits for (#11893).

A decision item that names nobody reached no bell: the bell, the home
dashboard notice and the Operations "Waiting for me" view all read
``_waits_for_person_clause``, which matched the item's ``user_id`` alone, and a
company-managed agent running with no acting user stores ``user_id`` NULL.

The recipient is resolved at the write, in this order:

1. an explicit ``recipient`` (a user id or an email), which must be an active
   user who can read the item;
2. the run's acting user (the person the run acts for);
3. the owner of the writing agent;
4. the platform administrators, when none of the above exists.

1 to 3 are stored in ``work_items.waits_for_user_id``; 4 stores nothing and
the clause shows such an item (``needs_human``, no recipient, no user) to every
administrator, which also surfaces the items written before the column. With
no administrator either, the write is refused and nothing is written.

An explicit recipient may also name groups (#1748): ``group:<uuid>``, several
comma-separated. The ids are stored in ``work_items.waits_for_group_ids`` and
membership is read at query time, so a member added later answers and a member
removed cannot; the first answer closes the decision for all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from app.core.json_sql import when_json_parseable
from app.core.utils import safe_uuid

#: The prefix an explicit recipient names a group with.
GROUP_PREFIX = "group:"


@dataclass(frozen=True)
class Recipient:
    """Who a decision item waits for: one person, groups, or the administrators."""

    user_id: uuid.UUID | None
    source: str  # explicit | groups | acting_user | agent_owner | administrators
    group_ids: tuple[str, ...] = ()


class RecipientError(ValueError):
    """No person can be notified, or the recipient named cannot be."""


async def _user_by_ref(db: Any, ref: str) -> Any:
    from app.models.user import User

    uid = safe_uuid(ref)
    clause = User.id == uid if uid is not None else func.lower(User.email) == ref.strip().lower()
    return (await db.execute(select(User).where(clause))).scalar_one_or_none()


async def _may_read(
    db: Any,
    person_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    is_company_managed: bool,
    workspace_id: uuid.UUID | None,
) -> bool:
    """Whether *person_id* reads an item filed this way (the tiers of ``_visible_clause``)."""
    if workspace_id is not None:
        from app.services.work_item import _workspace_tiers

        readable, _writable = await _workspace_tiers(db, str(person_id))
        return workspace_id in readable
    return bool(is_company_managed) or user_id == person_id


async def _any_admin(db: Any) -> bool:
    from app.models.user import User

    return bool((await db.execute(select(exists().where(User.role == "admin", User.is_active.is_(True))))).scalar())


def group_refs(explicit: str | None) -> list[str] | None:
    """The group ids an explicit recipient names (``group:<uuid>``, comma-separated); ``None`` when it names none.

    Raises :class:`RecipientError` for a list that mixes groups with something else or names a malformed id.
    """
    parts = [p.strip() for p in (explicit or "").split(",") if p.strip()]
    if not parts or not any(p.lower().startswith(GROUP_PREFIX) for p in parts):
        return None
    ids: list[str] = []
    for part in parts:
        gid = safe_uuid(part[len(GROUP_PREFIX) :]) if part.lower().startswith(GROUP_PREFIX) else None
        if gid is None:
            raise RecipientError(f"recipient {part!r} is not group: followed by a group id; nothing was written")
        if str(gid) not in ids:
            ids.append(str(gid))
    return ids


async def _resolve_groups(db: Any, ids: list[str]) -> Recipient:
    from app.models.group import Group

    found = {
        str(g)
        for g in (await db.execute(select(Group.id).where(Group.id.in_([uuid.UUID(i) for i in ids])))).scalars()
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise RecipientError(f"recipient group {missing[0]} does not exist; nothing was written")
    return Recipient(None, "groups", tuple(ids))


async def resolve_recipient(
    db: Any,
    *,
    explicit: str | None,
    acting_user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    item_user_id: uuid.UUID | None,
    is_company_managed: bool,
    workspace_id: uuid.UUID | None,
) -> Recipient:
    """The person a ``needs_human`` item filed this way waits for.

    Raises :class:`RecipientError` when *explicit* names no active user, a user
    who cannot read the item, or when nobody at all can be notified.
    """
    groups = group_refs(explicit)
    if groups:
        return await _resolve_groups(db, groups)
    if explicit and explicit.strip():
        person = await _user_by_ref(db, explicit)
        if person is None or not person.is_active:
            raise RecipientError(f"recipient {explicit.strip()!r} is not an active user; nothing was written")
        if not await _may_read(
            db,
            person.id,
            user_id=item_user_id,
            is_company_managed=is_company_managed,
            workspace_id=workspace_id,
        ):
            raise RecipientError(f"recipient {explicit.strip()!r} cannot see this work item; nothing was written")
        return Recipient(person.id, "explicit")
    if acting_user_id is not None:
        return Recipient(acting_user_id, "acting_user")
    if agent_id is not None:
        from app.models.agent import Agent

        owner = (await db.execute(select(Agent.user_id).where(Agent.id == agent_id))).scalar_one_or_none()
        if owner is not None:
            return Recipient(owner, "agent_owner")
    if await _any_admin(db):
        return Recipient(None, "administrators")
    raise RecipientError(
        "nobody can be notified of this decision: the run acts for no person, the agent has no owner "
        "and the platform has no administrator; pass recipient (a user id or an email). Nothing was written"
    )


def group_member_clause(user_uuid: uuid.UUID) -> Any:
    """SQL: an item addressed to groups (``waits_for_group_ids``) *user_uuid* is a member of now."""
    from app.models.group import GroupMember
    from app.models.work_item import WorkItem

    member = exists().where(
        GroupMember.user_id == user_uuid,
        cast(WorkItem.waits_for_group_ids, JSONB).has_key(cast(GroupMember.group_id, String)),
    )
    return and_(
        WorkItem.waits_for_group_ids.is_not(None),
        when_json_parseable(WorkItem.waits_for_group_ids, member),
    )


def recipient_clause(user_uuid: uuid.UUID) -> Any:
    """SQL: a ``needs_human`` item whose recipient is *user_uuid*.

    A member of its groups when it is addressed to groups; else its recipient
    when one is stored, else its user; an item with none of them waits for
    every administrator.
    """
    from app.models.user import User
    from app.models.work_item import WorkItem

    is_admin = exists().where(User.id == user_uuid, User.role == "admin", User.is_active.is_(True))
    no_groups = WorkItem.waits_for_group_ids.is_(None)
    return or_(
        group_member_clause(user_uuid),
        no_groups & (func.coalesce(WorkItem.waits_for_user_id, WorkItem.user_id) == user_uuid),
        no_groups & WorkItem.waits_for_user_id.is_(None) & WorkItem.user_id.is_(None) & is_admin,
    )


async def recipient_line(item: dict[str, Any]) -> str | None:
    """The tool line naming who a ``needs_human`` item waits for; ``None`` when it needs nobody."""
    if not item.get("needs_human"):
        return None
    if item.get("waits_for_group_ids"):
        return "  - waits for: the members of its groups, notified in the bell; the first answer decides."
    ref = item.get("waits_for_user_id") or item.get("user_id")
    if not ref:
        return "  - waits for: the platform administrators, notified in the bell."
    from app.core.database import async_session

    async with async_session() as db:
        person = await _user_by_ref(db, str(ref))
    if person is None:
        return "  - waits for: a person no longer on the platform; set recipient to notify someone."
    return f"  - waits for: {person.full_name} ({person.email}), notified in the bell."
