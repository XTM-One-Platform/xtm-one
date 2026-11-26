# SPDX-License-Identifier: Apache-2.0

"""Who reads and who acts on the organization's CTEM signals and cases.

An administrator reads every signal and case of the organization and acts on
them. Anyone else reaches CTEM only through a right one of their groups grants
(``Group.ctem_access``, #2441); without one there is no CTEM at all: no menu,
no signal or case API (403), no CTEM agent tool and no ``ctem_signal.*``
trigger. Two groups are seeded in XTM One (``services/ctem_groups.py``):
"CTEM Viewers" (view, every account) and "CTEM Operators" (decide, empty).

* ``view``: read the system signals and every case of the organization as an
  administrator does (lists, case page, loop trace, history). Nothing more: a
  write on what the right alone shows is refused.
* ``decide``: ``view`` plus signal triage, case state changes and the answer
  to a case's decision gates. ``Group.ctem_notify`` says whether the members
  are notified of those gates (on by default).

This module is the ONE reader of those columns; a second reader is a defect
findable by grep, as for ``group_ai_access``. An agent acts with the right of
the person it acts for, so the tools and the event delivery ask the same
functions with that person's id.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from sqlalchemy import select

from app.models.group import Group, GroupMember

logger = logging.getLogger(__name__)

#: The rights a group grants, weakest first.
CTEM_RIGHTS: tuple[str, ...] = ("view", "decide")

Level = Literal["admin", "decide", "view"]

#: What a member holding ``view`` only is told when they try to act.
VIEW_ONLY_MESSAGE = "Your CTEM access is read only: a CTEM decide right is needed to change this."

#: What a person with no CTEM right is told (#2441).
NO_ACCESS_MESSAGE = "You have no CTEM access: an administrator adds you to a group with a CTEM right."


def normalize_right(value: object) -> str | None:
    """The stored form of a right: ``view``, ``decide`` or ``None``; anything else raises ``ValueError``."""
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in ("none", "off"):
        return None
    if text not in CTEM_RIGHTS:
        raise ValueError(f"A CTEM right must be one of {', '.join(CTEM_RIGHTS)}, or none.")
    return text


def group_ctem_access(group: object) -> str | None:
    """The right *group* grants, for the wire (``None`` without one)."""
    return getattr(group, "ctem_access", None) or None


def group_ctem_notify(group: object) -> bool:
    """Whether *group*'s decide members are notified of the case gates (on unless switched off)."""
    return bool(getattr(group, "ctem_notify", True))


def _as_uuid(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def _user(db: Any, user: object) -> Any:
    """*user* when it already is a user row, else the row of that id (``None`` when unknown)."""
    if hasattr(user, "is_admin"):
        return user
    person = _as_uuid(user)
    if person is None:
        return None
    from app.models.user import User

    try:
        return await db.get(User, person)
    except Exception:  # unreadable: no right is read for them (never a widening)
        logger.warning("CTEM access: user %s unreadable", person, exc_info=True)
        return None


async def group_right(db: Any, user_id: object) -> str | None:
    """The strongest CTEM right *user_id*'s groups grant (``decide`` over ``view``), ``None`` without one."""
    person = _as_uuid(user_id)
    if person is None:
        return None
    try:
        rows = await db.execute(
            select(Group.ctem_access)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == person, Group.ctem_access.is_not(None))
        )
        granted = {str(r) for r in rows.scalars().all() if r}
    except Exception:  # unreadable is no right: refuse, never widen
        logger.warning("CTEM access: group rights of %s unreadable", person, exc_info=True)
        return None
    if "decide" in granted:
        return "decide"
    if "view" in granted:
        return "view"
    return None


async def level(db: Any, user: object) -> Level | None:
    """``admin``, ``decide``, ``view`` or ``None`` (no CTEM access, #2441)."""
    row = await _user(db, user)
    if row is None:
        return None
    if bool(getattr(row, "is_admin", False)):
        return "admin"
    right = await group_right(db, getattr(row, "id", None))
    return right  # type: ignore[return-value]


async def reads_organization(db: Any, user: object) -> bool:
    """Whether *user* reads every signal and case of the organization (administrator, ``view`` or ``decide``)."""
    return await level(db, user) is not None


async def may_decide(db: Any, user: object) -> bool:
    """Whether *user* acts on what the organization-wide read shows (administrator or ``decide``)."""
    return await level(db, user) in ("admin", "decide")


class NoCtemAccess(PermissionError):
    """The person holds no CTEM right (#2441): every signal and case read or write is refused."""


async def signal_reader(db: Any, user: object) -> str | None:
    """The ``recorded_by`` filter of a signal read: ``None`` (the organization) for a person with a right.

    Raises :class:`NoCtemAccess` without one (#2441): there is no own-signals fallback any more.
    """
    if await reads_organization(db, user):
        return None
    raise NoCtemAccess(NO_ACCESS_MESSAGE)


async def gate_recipient(db: Any) -> str | None:
    """The work item ``recipient`` a case gate of a company fleet waits for (#1748).

    ``group:<id>`` for every group granting ``decide`` with ``ctem_notify`` on
    and at least one active member, comma-separated; ``None`` when there is none:
    the routing of the person the run acts for applies, never every administrator
    (#2441: the seeded "CTEM Operators" starts empty).
    """
    from app.models.user import User

    try:
        active_member = (
            select(GroupMember.id)
            .join(User, User.id == GroupMember.user_id)
            .where(GroupMember.group_id == Group.id, User.is_active.is_(True))
            .exists()
        )
        rows = await db.execute(
            select(Group.id)
            .where(Group.ctem_access == "decide", Group.ctem_notify.is_(True), active_member)
            .order_by(Group.name)
        )
        ids = [str(r) for r in rows.scalars().all()]
    except Exception:  # unreadable: today's routing, never a widening
        logger.warning("CTEM access: decide groups unreadable", exc_info=True)
        return None
    return ",".join(f"group:{i}" for i in ids) or None


async def signal_writer(db: Any, user: object) -> str | None:
    """The ``recorded_by`` filter of a signal write (triage): ``None`` with decide.

    Raises :class:`NoCtemAccess` without any right and :class:`PermissionError`
    with ``view`` alone (#2441: no own-signals fallback).
    """
    found = await level(db, user)
    if found in ("admin", "decide"):
        return None
    if found == "view":
        raise PermissionError(VIEW_ONLY_MESSAGE)
    raise NoCtemAccess(NO_ACCESS_MESSAGE)


async def wires_ctem(user_id: object) -> bool:
    """Whether a turn or run acting for *user_id* is offered any CTEM tool (#2441).

    Read once per turn or run, in its own session, before the CTEM groups
    (``ctem``, ``ctem_read``, ``ctem_repair``) and the built-in CTEM agents'
    tools are wired: without a view right or more, none is. Fails closed.
    """
    from app.core.config import settings
    from app.core.database import async_session

    if settings.is_copilot_like or not user_id:
        return False
    try:
        async with async_session() as db:
            return await level(db, str(user_id)) is not None
    except Exception:
        logger.warning("CTEM access: the right of %s is unreadable, no CTEM tool wired", user_id, exc_info=True)
        return False


async def refusal(db: Any, user: object, *, write: bool) -> str | None:
    """Why *user* may not read (``write`` false) or change CTEM signals and cases, ``None`` when they may.

    The one check the API routes and the agent tools share (#2441), read at call
    time for the person a turn or a background run acts for.
    """
    found = await level(db, user)
    if found is None:
        return NO_ACCESS_MESSAGE
    if write and found == "view":
        return VIEW_ONLY_MESSAGE
    return None
