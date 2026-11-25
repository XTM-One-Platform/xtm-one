# SPDX-License-Identifier: Apache-2.0

"""The owner a new CTEM case starts with (#1736 slice 3).

A case opened from a signal had no owner: the loop then asked nobody in
particular and the case could wait on nobody. The owner now comes, in order:

1. from the inventory: the ``owner`` of an affected asset's records, else the
   ``owner`` of an asset group one of those records belongs to;
2. from the instance default ``CTEM_CASE_DEFAULT_OWNER`` (an email);
3. else none (the loop's ask still names a person or a team).

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


async def owner_for_signal(db: AsyncSession, signal: Any) -> tuple[uuid.UUID | None, str | None]:
    """The owner a case opened from *signal* starts with, read from the inventory and the default.

    One savepoint: an inventory or user read that fails answers ``(None, None)`` and the case opens without owner.
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
