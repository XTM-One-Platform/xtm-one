# SPDX-License-Identifier: Apache-2.0

"""The owner a new CTEM case starts with (#1736 slice 3).

A case opened from a signal had no owner: the loop then asked nobody in
particular and the case could wait on nobody. The owner now comes, in order:

1. from the inventory: the ``owner`` of an affected asset's records, else the
   ``owner`` of an asset group one of those records belongs to;
2. from the instance default ``CTEM_CASE_DEFAULT_OWNER`` (an email);
3. else the person who decides CTEM (#2452): the same people a case gate's
   decision item reaches (``work_item_recipient``): the active members of the
   groups granting ``decide`` with notify on, else the active administrators;
   the seed account (``ADMIN_EMAIL``) only when it is the sole candidate. Of
   several, the first by account creation, then id: a stable rule, its count
   written in the reason of the ``owner`` case event;
4. else none (an instance with no active administrator).

The owner is set at the opening, by this code, never by the loop agent: a
loop that writes the same owner again changes nothing. The case sweep gives
an open case with no owner and no ``owner`` event the decider the same way
(:func:`backfill_owners`): one ``owner`` event per case, no notification,
and a second pass changes nothing; an owner a person cleared stays cleared.

An owner is a platform account: the inventory's ``owner`` text and the
default are matched to an active user by email, case-insensitive. A name
that is not an account's email is skipped, never guessed. The affected
assets are the signal's subject when it names an asset, and the assets of
its decision's scope sources (``asset`` and ``asset_group``).

:func:`pick_owner` is pure (testable); :func:`owner_for_signal` reads the
store in one savepoint and never fails a case opening.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The scope sources of a decision that name an asset of the inventory.
ASSET_SOURCES: tuple[str, ...] = ("asset", "asset_group")
#: Most asset names one opening looks up.
MAX_ASSETS = 50
#: How the owner was found, written as the reason of the ``owner`` case event.
FROM_ASSET = "asset owner"
FROM_GROUP = "asset group owner"
FROM_DEFAULT = "instance default owner"
FROM_DECIDER = "CTEM decider"
#: Most unowned open cases one sweep gives an owner (#2452).
BACKFILL_BATCH = 500


def _key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def default_owner_email() -> str:
    """``CTEM_CASE_DEFAULT_OWNER`` lower-cased, ``""`` when not set."""
    return (os.environ.get("CTEM_CASE_DEFAULT_OWNER") or "").strip().lower()


def asset_names(signal: Any) -> list[str]:
    """The asset names a signal names: its subject when it is an asset, then its scope's asset sources (pure)."""
    names: list[str] = []
    if getattr(signal, "subject_kind", None) == "asset":
        names.append(getattr(signal, "subject_ref", None) or "")
    decision = getattr(signal, "decision", None)
    scope = decision.get("scope") if isinstance(decision, Mapping) else None
    sources = scope.get("sources") if isinstance(scope, Mapping) else None
    for source in sources or ():
        if isinstance(source, Mapping) and source.get("from") in ASSET_SOURCES:
            names.append(str(source.get("name") or ""))
    keys = [_key(n) for n in names]
    return list(dict.fromkeys(k for k in keys if k))[:MAX_ASSETS]


def pick_owner(
    asset_owners: Iterable[str],
    group_owners: Iterable[str],
    users_by_email: Mapping[str, uuid.UUID],
    default: str = "",
) -> tuple[uuid.UUID | None, str | None]:
    """The first owner that is an account: an asset's, then a group's, then the default (pure).

    Answers ``(user id, how it was found)``, ``(None, None)`` when none is an account.
    """
    for candidates, how in ((asset_owners, FROM_ASSET), (group_owners, FROM_GROUP), ((default,), FROM_DEFAULT)):
        for owner in candidates:
            user = users_by_email.get((owner or "").strip().lower())
            if user is not None:
                return user, how
    return None, None


def pick_decider(
    deciders: Iterable[tuple[uuid.UUID, str]], admins: Iterable[tuple[uuid.UUID, str]], seed_email: str = ""
) -> tuple[uuid.UUID | None, str | None]:
    """The CTEM decider a case falls back to (pure, #2452).

    *deciders* and *admins* are ``(user id, email)`` of active accounts, already
    ordered by account creation then id. The decide groups' members first, else
    the administrators; the seed account only when it is the only one. Answers
    ``(user id, reason)``, ``(None, None)`` when nobody decides.
    """
    seed = (seed_email or "").strip().lower()
    for people, source in ((list(deciders), "decide group member"), (list(admins), "administrator")):
        unique = list(dict.fromkeys(people))
        persons = [p for p in unique if (p[1] or "").strip().lower() != seed] or unique
        if persons:
            how = f"{FROM_DECIDER} ({source}"
            how += ")" if len(persons) == 1 else f", first of {len(persons)} by account creation)"
            return persons[0][0], how
    return None, None


async def decider_owner(db: AsyncSession) -> tuple[uuid.UUID | None, str | None]:
    """The person who decides CTEM, by :func:`pick_decider`; ``(None, None)`` when the read fails."""
    from app.core.config import settings
    from app.models.group import Group, GroupMember
    from app.models.user import User

    order = (User.created_at.asc(), User.id.asc())
    try:
        async with db.begin_nested():
            deciders = (
                await db.execute(
                    select(User.id, User.email)
                    .join(GroupMember, GroupMember.user_id == User.id)
                    .join(Group, Group.id == GroupMember.group_id)
                    .where(Group.ctem_access == "decide", Group.ctem_notify.is_(True), User.is_active.is_(True))
                    .order_by(*order)
                )
            ).all()
            admins = (
                await db.execute(
                    select(User.id, User.email).where(User.role == "admin", User.is_active.is_(True)).order_by(*order)
                )
            ).all()
    except Exception:  # never fails a case opening or a sweep
        logger.info("CTEM decider unavailable", exc_info=True)
        return None, None
    return pick_decider(
        [(r[0], str(r[1] or "")) for r in deciders],
        [(r[0], str(r[1] or "")) for r in admins],
        str(settings.admin_email or ""),
    )


async def owner_for_signal(db: AsyncSession, signal: Any) -> tuple[uuid.UUID | None, str | None]:
    """The owner a case opened from *signal* starts with: inventory, default, else the CTEM decider (#2452)."""
    owner, how = await _inventory_owner(db, signal)
    if owner is not None:
        return owner, how
    return await decider_owner(db)


async def backfill_owners(db: AsyncSession, *, now: Any, actor: str = "system") -> int:
    """Give the CTEM decider to each open case with no owner and no ``owner`` event; how many. The caller commits.

    Idempotent: the ``owner`` event it writes keeps the case out of the next pass, and so does an owner a
    person set or cleared. No platform event, no notification: the case's open decision is unchanged.
    """
    from sqlalchemy import exists

    from app.models.ctem_case import OPEN_STATES, CtemCase, CtemCaseEvent

    owned_once = exists().where(CtemCaseEvent.case_id == CtemCase.id, CtemCaseEvent.kind == "owner")
    cases = (
        (
            await db.execute(
                select(CtemCase)
                .where(CtemCase.state.in_(OPEN_STATES), CtemCase.owner_user_id.is_(None), ~owned_once)
                .order_by(CtemCase.created_at.asc())
                .limit(BACKFILL_BATCH)
            )
        )
        .scalars()
        .all()
    )
    if not cases:
        return 0
    owner, how = await decider_owner(db)
    if owner is None:
        return 0
    for case in cases:
        case.owner_user_id = owner
        db.add(
            CtemCaseEvent(
                case_id=case.id, actor=actor, kind="owner", at=now, to_value=str(owner), reason=f"{how}, backfill"
            )
        )
    return len(cases)


async def _inventory_owner(db: AsyncSession, signal: Any) -> tuple[uuid.UUID | None, str | None]:
    """The owner read from the inventory and the default.

    One savepoint: an inventory or user read that fails answers ``(None, None)``.
    """
    from app.models.organization_asset_record import OrganizationAssetGroup, OrganizationAssetRecord
    from app.models.user import User

    names = asset_names(signal)
    default = default_owner_email()
    if not names and not default:
        return None, None
    organization_id = getattr(signal, "organization_id", None)
    try:
        async with db.begin_nested():
            asset_owners: list[str] = []
            group_owners: list[str] = []
            if names and organization_id is not None:
                record = OrganizationAssetRecord
                rows = (
                    await db.execute(
                        select(record.owner, record.group_key).where(
                            record.organization_id == organization_id,
                            record.name_key.in_([n[:200] for n in names]),
                        )
                    )
                ).all()
                asset_owners = [owner for owner, _ in rows if owner]
                groups = sorted({group for _, group in rows if group})
                if groups:
                    group = OrganizationAssetGroup
                    group_owners = [
                        str(owner)
                        for owner in (
                            await db.execute(
                                select(group.fields["owner"].astext).where(
                                    group.organization_id == organization_id,
                                    group.name_key.in_(groups),
                                )
                            )
                        )
                        .scalars()
                        .all()
                        if owner
                    ]
            emails = {e.strip().lower() for e in [*asset_owners, *group_owners, default] if e and "@" in e}
            if not emails:
                return None, None
            users = {
                email: user_id
                for user_id, email in (
                    await db.execute(
                        select(User.id, func.lower(User.email)).where(
                            func.lower(User.email).in_(sorted(emails)), User.is_active.is_(True)
                        )
                    )
                ).all()
            }
    except Exception:  # the inventory never breaks a case opening
        logger.info("CTEM case owner unavailable", exc_info=True)
        return None, None
    return pick_owner(asset_owners, group_owners, users, default)
