# SPDX-License-Identifier: Apache-2.0

"""Who reads and who acts on the organization's CTEM signals and cases.

An administrator reads every signal and case of the organization and acts on
them. Anyone else reads the signals recorded for them and the cases their own
source read shows, unless one of their groups grants a CTEM right
(``Group.ctem_access``):

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
    """``admin``, ``decide``, ``view`` or ``None`` (the signals one records and the cases one's read shows)."""
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


async def signal_reader(db: Any, user: object) -> str | None:
    """The ``recorded_by`` filter of a signal read: ``None`` for the organization, else the person's id."""
    if await reads_organization(db, user):
        return None
    return str(getattr(user, "id", user))


async def signal_writer(db: Any, user: object) -> str | None:
    """The ``recorded_by`` filter of a signal write (triage): ``None`` with decide, else the person's own signals."""
    if await may_decide(db, user):
        return None
    return str(getattr(user, "id", user))
