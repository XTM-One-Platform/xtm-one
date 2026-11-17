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

"""Who can open a presentation, and the one write that changes it.

A deck's audience is its visibility columns: ``shared_user_ids`` /
``shared_user_permissions``, ``shared_group_ids`` / ``shared_group_permissions``,
``is_company_managed`` and ``access_restricted``. Two surfaces change it, and
both go through :func:`apply_sharing`, so both apply the same rules:

* the Share dialog, through ``PATCH /presentations/{id}``;
* the ``share_presentation`` built-in tool, which runs AS THE CALLING USER. It
  resolves the people and groups it is handed (``share_principals``: a user id
  or the email of an existing account, a group id or exact name), turns
  ``add`` / ``remove`` / ``replace`` into the grant lists to store
  (:func:`plan_sharing`) and hands them to the same write.

Who may share is who may manage the deck: its owner or a ``config`` grantee
(``MANAGE_TIERS``). The tool never touches ``is_company_managed``: publishing a
deck to the whole platform is an admin's act, from the dialog. An address no
account answers to is refused by name: the tool grants access to accounts of
this platform only, it invites nobody.

:func:`audience_line` is the ``Shared with:`` line of ``get_presentation`` and
of the tool's result, worded like the dialog (``can view`` / ``can edit``), so
the agent reports who has access in the words the user sees.

The reading of people and groups - entries, resolution, the stored grants, the
``add`` / ``remove`` / ``replace`` plan of one kind - is ``share_principals``,
shared with the agent builder's per-user agent sharing.

A video is shared exactly like a deck - same tiers, same dialog, same
rules - so its ``PATCH /video-studio/projects/{id}`` and its ``share_video``
tool go through these same functions (:data:`SharedEntity`).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custom_dashboard import CustomDashboard
from app.models.dashboard_widget_pack import DashboardWidgetPack
from app.models.presentation import Presentation
from app.models.user import User
from app.models.video_studio import VideoProject
from app.services.share_principals import (
    GrantDelta,
    Principal,
    PrincipalIndex,
    SharingChange,
    count_principals,
    load_principals,
    plan_grants,
    stored_grants,
)

#: The tiers a deck grant gives: open, present, export, duplicate and comment
#: (``read_only``), or also edit it, by hand or through one's own assistant.
SHARE_PERMISSIONS: tuple[str, ...] = ("read_only", "config")

#: The dialog's words for the tiers (``PRESENTATION_SHARE_PERMISSION_LABELS``).
#: ``config_with_chats`` means nothing more on a deck than ``config``.
PERMISSION_LABELS: dict[str, str] = {
    "read_only": "can view",
    "config": "can edit",
    "config_with_chats": "can edit",
}

#: Grants the ``Shared with:`` line names before it counts the rest.
MAX_LISTED_GRANTS = 50

#: Request fields that change who may see or edit a deck. ``PresentationUpdate``
#: carries the same fields under the same names, so the route hands its body
#: over as is.
SHARING_FIELDS: frozenset[str] = frozenset(SharingChange.model_fields)

#: What these functions share: a deck, a video, a custom dashboard or a widget pack.
type SharedEntity = Presentation | VideoProject | CustomDashboard | DashboardWidgetPack


async def apply_sharing(
    db: AsyncSession,
    user: User,
    entity: SharedEntity,
    changes: Any,
) -> None:
    """Write the sharing fields *changes* sets onto *entity*.

    The one sharing write of a deck and of a video: their Share dialog's
    ``PATCH`` and their sharing tool end here, once their caller checked that
    *user* may manage the entity. Group grants go through
    ``apply_group_share_fields`` (an added group must exist). Per-user grants go through
    ``apply_user_share_fields`` (canonical ids, existing accounts, an active
    account for a new grant, known tiers - a 422 otherwise); publishing it
    stays admin-only inside ``handle_company_managed_toggle``. Flushes
    nothing and commits nothing.

    A deck or a video that gains a person, a group or the whole company is
    used: the write records ``shared`` on its chat ROI record (#10544, S2c;
    ``roi_chat_records.record_sharing``), in a savepoint that never fails it.
    """
    from app.api.v1.helpers import (
        apply_group_share_fields,
        apply_user_share_fields,
        handle_company_managed_toggle,
        wants_access_restriction,
    )
    from app.services.roi_chat_records import record_sharing
    from app.services.share_principals import grant_snapshot, grants_audit

    before = grant_snapshot(entity)
    was_published = bool(getattr(entity, "is_company_managed", False))
    await apply_group_share_fields(db, entity, changes, partial=True)
    # ``agent_field=None``: agents never bind decks or videos by id.
    await handle_company_managed_toggle(
        db,
        user,
        entity,
        changes.is_company_managed,
        agent_field=None,
        keep_grants=wants_access_restriction(changes, entity),
    )
    await apply_user_share_fields(db, entity, changes, partial=True)
    audit = grants_audit(before, entity)
    if not was_published and bool(getattr(entity, "is_company_managed", False)):
        audit["added_groups"] = [*audit["added_groups"], "company"]
    await record_sharing(db, entity, audit)


# ── The tool's words ──────────────────────────────────────────────────────────


_ACTION_ALIASES: dict[str, str] = {
    "add": "add",
    "share": "add",
    "grant": "add",
    "remove": "remove",
    "revoke": "remove",
    "unshare": "remove",
    "replace": "replace",
}

_PERMISSION_ALIASES: dict[str, str] = {
    "read_only": "read_only",
    "readonly": "read_only",
    "view": "read_only",
    "viewer": "read_only",
    "can_view": "read_only",
    "config": "config",
    "edit": "config",
    "editor": "config",
    "can_edit": "config",
}


def _alias_key(value: object) -> str:
    return re.sub(r"[\s-]+", "_", str(value).strip().lower())


def normalize_share_action(value: object) -> str | None:
    """One of :data:`SHARE_ACTIONS` (``add`` when unset), or ``None``."""
    if value is None or not str(value).strip():
        return "add"
    return _ACTION_ALIASES.get(_alias_key(value))


def normalize_share_permission(value: object) -> str | None:
    """One of :data:`SHARE_PERMISSIONS` (``read_only`` when unset), or ``None``.

    The dialog's own words (``can view`` / ``can edit``) are read too.
    """
    if value is None or not str(value).strip():
        return "read_only"
    return _PERMISSION_ALIASES.get(_alias_key(value))


# ── The audience as stored ────────────────────────────────────────────────────


async def lock_for_sharing(db: AsyncSession, entity: SharedEntity, users: Sequence[Principal]) -> None:
    """Take a grant write's locks in the order every grant write takes them.

    A user deletion (``prune_user_grants``) locks the account, then every deck
    naming it; ``normalize_user_grants`` locks the accounts a write names
    ``FOR KEY SHARE``. Locking the deck first and the accounts after would
    deadlock against the deletion of one of them, so the accounts the call
    names and the deck already grants are locked first, in id order, and only
    then the deck row, re-read as committed (``FOR UPDATE``): a share racing
    another share of the same deck plans from the grants the first one wrote.
    """
    current = stored_grants(entity.shared_user_ids, entity.shared_user_permissions)
    ids = sorted({principal.id for principal in users} | {key for key, _tier in current})
    if ids:
        await db.execute(
            select(User.id)
            .where(User.id.in_([uuid.UUID(value) for value in ids]))
            .order_by(User.id)
            .with_for_update(read=True, key_share=True)
            .execution_options(autoflush=False)
        )
    await db.refresh(entity, with_for_update=True)


def _tier_label(level: str) -> str:
    return PERMISSION_LABELS.get(level, PERMISSION_LABELS["read_only"])


def _grant_text(principal: Principal, level: str) -> str:
    prefix = "group " if principal.kind == "group" else ""
    state = "" if principal.is_active else ", deactivated"
    return f"{prefix}{principal.label} ({_tier_label(level)}{state})"


def audience_line(entity: SharedEntity, index: PrincipalIndex) -> str:
    """The ``Shared with:`` line: who can open the deck or the video, and with which tier.

    Reads the grants as they are on *entity* (written or not), labels them
    from *index*; past :data:`MAX_LISTED_GRANTS` the rest are counted.
    """
    if entity.is_company_managed and not entity.access_restricted:
        return "Shared with: everyone on the platform (company-managed - only admins can edit it)."
    users = stored_grants(entity.shared_user_ids, entity.shared_user_permissions)
    groups = stored_grants(entity.shared_group_ids, entity.shared_group_permissions)
    restricted = " (company-managed, restricted to them)" if entity.is_company_managed else ""
    if not users and not groups:
        if entity.is_company_managed:
            return "Shared with: nobody listed (company-managed, restricted) - only admins can open it."
        return "Shared with: nobody - only its owner can open it."
    entries = [_grant_text(index.user(key), level) for key, level in users]
    entries += [_grant_text(index.group(key), level) for key, level in groups]
    listed = ", ".join(entries[:MAX_LISTED_GRANTS])
    if len(entries) > MAX_LISTED_GRANTS:
        listed += f" and {len(entries) - MAX_LISTED_GRANTS} more"
    return f"Shared with {count_principals(len(users), len(groups))}{restricted}: {listed}."


async def describe_sharing(db: AsyncSession, entity: SharedEntity) -> str:
    """:func:`audience_line` of the deck or video as stored, its principals looked up."""
    return audience_line(entity, await load_principals(db, entity))


# ── Planning a share_presentation call ────────────────────────────────────────


@dataclass
class SharingPlan:
    """The grant lists a call stores, and what that changes, for the result and the audit."""

    action: str
    permission: str | None
    changes: SharingChange
    users: GrantDelta
    groups: GrantDelta
    #: Named, but they own the deck: an owner needs no grant.
    owner: list[Principal] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.users.changed or self.groups.changed

    def audit_details(self) -> dict[str, Any]:
        return {
            "via": "agent",
            "action": self.action,
            "permission": self.permission,
            "added_users": self.users.added,
            "removed_users": self.users.removed,
            "retiered_users": self.users.retiered,
            "added_groups": self.groups.added,
            "removed_groups": self.groups.removed,
            "retiered_groups": self.groups.retiered,
        }


def plan_sharing(
    entity: SharedEntity,
    *,
    action: str,
    permission: str,
    users: Sequence[Principal],
    groups: Sequence[Principal],
) -> SharingPlan:
    """Turn an ``add`` / ``remove`` / ``replace`` call into the lists to store.

    * ``add``: the named people and groups get *permission*; someone already
      shared with gets the tier asked for (an upgrade or a downgrade).
    * ``remove``: the named grants go; the tier is not read.
    * ``replace``: the audience becomes exactly the named people and groups,
      all at *permission*; every other grant, of either kind, goes.

    Only the kinds that change are set on ``changes``, so a write never
    re-validates grants it does not touch. The owner is never granted: named,
    they are reported, not stored.
    """
    owner = str(entity.user_id) if entity.user_id else None
    owned = [principal for principal in users if principal.id == owner]
    people = [principal for principal in users if principal.id != owner]
    user_ids, user_tiers, user_delta = plan_grants(
        action,
        permission,
        stored_grants(entity.shared_user_ids, entity.shared_user_permissions),
        people,
    )
    group_ids, group_tiers, group_delta = plan_grants(
        action,
        permission,
        stored_grants(entity.shared_group_ids, entity.shared_group_permissions),
        list(groups),
    )
    fields: dict[str, Any] = {}
    if user_delta.changed:
        fields["shared_user_ids"] = user_ids or None
        fields["shared_user_permissions"] = user_tiers or None
    if group_delta.changed:
        fields["shared_group_ids"] = group_ids or None
        fields["shared_group_permissions"] = group_tiers or None
    return SharingPlan(
        action=action,
        permission=None if action == "remove" else permission,
        changes=SharingChange(**fields),
        users=user_delta,
        groups=group_delta,
        owner=owned,
    )


def format_sharing_result(title: str, plan: SharingPlan, index: PrincipalIndex) -> str:
    """What a ``share_presentation`` call did, one line per kind of change."""
    tier = _tier_label(plan.permission or "read_only")

    def named(user_ids: list[str], group_ids: list[str]) -> str:
        labels = [index.user(key).label for key in user_ids]
        labels += [f"group {index.group(key).label}" for key in group_ids]
        return f"{count_principals(len(user_ids), len(group_ids))}: {', '.join(labels)}"

    lines = []
    if plan.users.added or plan.groups.added:
        lines.append(f"- Given access ({tier}) to {named(plan.users.added, plan.groups.added)}")
    if plan.users.retiered or plan.groups.retiered:
        lines.append(f"- Now {tier}: {named(plan.users.retiered, plan.groups.retiered)}")
    if plan.users.removed or plan.groups.removed:
        lines.append(f"- Access removed, they can no longer open it: {named(plan.users.removed, plan.groups.removed)}")
    if plan.users.unchanged or plan.groups.unchanged:
        lines.append(f"- Already {tier}, unchanged: {named(plan.users.unchanged, plan.groups.unchanged)}")
    if plan.users.not_shared or plan.groups.not_shared:
        lines.append(
            f"- Not shared with them, nothing to remove: {named(plan.users.not_shared, plan.groups.not_shared)}"
        )
    if plan.owner:
        lines.append(f"- Skipped, they own it: {', '.join(principal.label for principal in plan.owner)}")
    if not lines:
        lines.append("- It was already shared exactly like that.")
    head = f"Sharing of **{title}** updated:" if plan.changed else f"Nothing changed on **{title}**:"
    return "\n".join([head, *lines])
