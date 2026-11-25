# SPDX-License-Identifier: Apache-2.0

"""Single sign-on into the groups an administrator marked "auto join".

A group with ``Group.auto_join`` on takes in every person who signs in through
single sign-on (OIDC or SAML), at every sign-in: the account created on the
fly, the pre-provisioned account at its first sign-in and an existing account
alike. Local, SCIM and API-created accounts are untouched until they sign in
through single sign-on.

This module is the one writer of the memberships it grants
(``GroupMember.source == "auto_join"``):

* a person already in the group, whatever the source, is left as is: no
  second row, no audit entry;
* the insert skips a row a concurrent sign-in wrote first (the unique
  constraint on group and user), so two sign-ins of one account never fail;
* each membership it adds is one audit entry whose actor is the sign-in
  (``system:sso-sign-in``), anchored on the person who joined;
* the SAML group mapping manages only ``saml`` memberships, so it never removes
  one granted here; an administrator can, and the next single sign-on grants it
  again while the toggle stays on.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.audit import log_system_audit

#: ``GroupMember.source`` of a membership granted at a single sign-on.
AUTO_JOIN_SOURCE = "auto_join"

#: The actor named on the audit entry of a membership granted here.
AUTO_JOIN_ACTOR = "sso-sign-in"


async def join_auto_join_groups(db: AsyncSession, user: User, *, provider: str) -> set[uuid.UUID]:
    """Add *user* to every group with auto join on, in the caller's transaction.

    *provider* names the sign-in (``OIDC`` or ``SAML``) on the audit entries.
    Returns the ids of the groups joined now: empty when there is no such group
    or the person is already in each of them. The caller drops the cached
    resolutions that read memberships once its transaction has committed.
    """
    groups = (await db.execute(select(Group.id, Group.name).where(Group.auto_join.is_(True)))).all()
    if not groups:
        return set()
    names = dict(groups)
    inserted = (
        (
            await db.execute(
                pg_insert(GroupMember)
                .values(
                    [
                        {"id": uuid.uuid4(), "group_id": group_id, "user_id": user.id, "source": AUTO_JOIN_SOURCE}
                        for group_id in names
                    ]
                )
                .on_conflict_do_nothing(constraint="uq_group_members_group_user")
                .returning(GroupMember.group_id)
            )
        )
        .scalars()
        .all()
    )
    joined = set(inserted)
    for group_id in sorted(joined, key=lambda gid: names[gid].lower()):
        await log_system_audit(
            db,
            actor=AUTO_JOIN_ACTOR,
            anchor_user_id=user.id,
            action="bind",
            entity_type="group",
            entity_id=group_id,
            entity_name=names[group_id],
            summary=f"Added '{user.email}' to group '{names[group_id]}' (auto join at a {provider} sign-in)",
            details={"user_id": str(user.id), "provider": provider, "source": AUTO_JOIN_SOURCE},
        )
    return joined
