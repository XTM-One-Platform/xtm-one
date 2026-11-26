# SPDX-License-Identifier: Apache-2.0

"""The two CTEM access groups the platform seeds in XTM One (#2441).

CTEM is reached only through a group's right (``services/ctem_access.py``).
So that nobody loses CTEM at the upgrade that introduced that rule, two groups
are seeded, each found by its stable ``Group.seed_key`` (an administrator may
rename either):

* "CTEM Viewers" (``ctem_viewers``): ``view``, read only. The first seed puts
  every active account in it; every account created afterwards (local
  registration, invitation, administrator, SCIM, single sign-on) joins it once,
  at its creation (:func:`join_viewers_at_creation`). An administrator who
  removes a person is never overridden: nothing adds them back at a later
  sign-in (the group is not ``auto_join``).
* "CTEM Operators" (``ctem_operators``): ``decide``, seeded empty; the
  administrators choose who acts on cases and signals.

Seeded in ``xtm_one`` and ``dev`` content modes only: Copilot serves no CTEM.
Idempotent: a seeded group that exists is never renamed, re-filled or changed.
A seeded group an administrator deleted is created again at the next boot, as
at its first seed (to keep CTEM away from people, an administrator empties the
group or sets its right to none instead of deleting it).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group, GroupMember
from app.models.user import User

logger = logging.getLogger(__name__)

VIEWERS_KEY = "ctem_viewers"
OPERATORS_KEY = "ctem_operators"

#: ``GroupMember.source`` of a membership this module grants.
SEED_SOURCE = "manual"

#: The seeded groups: key -> (name, right, description).
SEEDED_GROUPS: dict[str, tuple[str, str, str]] = {
    VIEWERS_KEY: (
        "CTEM Viewers",
        "view",
        "Read every CTEM signal and case, read only. Every account joins it when it is created; "
        "an administrator may remove anyone.",
    ),
    OPERATORS_KEY: (
        "CTEM Operators",
        "decide",
        "Read and act on CTEM signals and cases, and answer the case decisions. Empty at first: "
        "administrators choose its members.",
    ),
}

#: The content modes that serve CTEM.
CTEM_MODES = ("xtm_one", "dev")


async def _free_name(db: AsyncSession, wanted: str) -> str:
    """*wanted*, or *wanted* with a suffix when an administrator already holds that name."""
    name = wanted
    suffix = 2
    while (await db.execute(select(Group.id).where(Group.name == name))).scalar_one_or_none() is not None:
        name = f"{wanted} ({suffix})"
        suffix += 1
    return name


async def _all_active_into(db: AsyncSession, group_id: uuid.UUID) -> int:
    ids = (await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars().all()
    if not ids:
        return 0
    inserted = (
        (
            await db.execute(
                pg_insert(GroupMember)
                .values(
                    [{"id": uuid.uuid4(), "group_id": group_id, "user_id": uid, "source": SEED_SOURCE} for uid in ids]
                )
                .on_conflict_do_nothing(constraint="uq_group_members_group_user")
                .returning(GroupMember.user_id)
            )
        )
        .scalars()
        .all()
    )
    return len(inserted)


async def seed_ctem_groups(db: AsyncSession, *, platform_mode: str) -> int:
    """Create the seeded CTEM groups missing from the platform; the number created. Commits.

    The first creation of "CTEM Viewers" puts every active account in it.
    """
    if platform_mode not in CTEM_MODES:
        return 0
    created = 0
    for key, (name, right, description) in SEEDED_GROUPS.items():
        found = (await db.execute(select(Group.id).where(Group.seed_key == key))).scalar_one_or_none()
        if found is not None:
            continue
        group = Group(
            id=uuid.uuid4(),
            name=await _free_name(db, name),
            description=description,
            ctem_access=right,
            ctem_notify=True,
            auto_join=False,
            kind="access",
            seed_key=key,
        )
        db.add(group)
        await db.flush()
        created += 1
        if key == VIEWERS_KEY:
            added = await _all_active_into(db, group.id)
            logger.info("CTEM access: seeded %r with %d active account(s)", group.name, added)
        else:
            logger.info("CTEM access: seeded %r, empty", group.name)
    await db.commit()
    return created


async def join_viewers_at_creation(db: AsyncSession, user: User) -> bool:
    """Put a just-created account in "CTEM Viewers", in the caller's transaction.

    Called once by each path that creates an account (#2441), never at a
    sign-in, so an administrator's removal holds. ``False`` when the group is
    not seeded (Copilot mode, or an administrator deleted it).
    """
    if user.id is None:
        await db.flush()
    group_id = (await db.execute(select(Group.id).where(Group.seed_key == VIEWERS_KEY))).scalar_one_or_none()
    if group_id is None:
        return False
    inserted = (
        await db.execute(
            pg_insert(GroupMember)
            .values(id=uuid.uuid4(), group_id=group_id, user_id=user.id, source=SEED_SOURCE)
            .on_conflict_do_nothing(constraint="uq_group_members_group_user")
            .returning(GroupMember.id)
        )
    ).scalar_one_or_none()
    return inserted is not None
