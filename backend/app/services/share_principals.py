# SPDX-License-Identifier: Apache-2.0

"""The people and groups a share grant names, as an agent tool reads and writes them.

A shareable entity's audience is its grant columns: ``shared_user_ids``
/ ``shared_user_permissions`` and ``shared_group_ids`` /
``shared_group_permissions``. The Share dialogs hand the API canonical ids; an
agent tool is handed what a person says - an email address, a name, an id - and
has to turn that into the same grants without inventing anyone. This module is
the ONE place that reading lives, shared by the ``share_presentation`` tool
(``presentation_sharing``) and the agent builder's ``update_agent`` /
``get_agent`` (per-user agent sharing):

* :func:`principal_entries` reads a ``users`` / ``groups`` argument however the
  model spelled it (a list, a JSON list in a string, a comma / semicolon /
  newline separated string, ``Ada <ada@x.com>``);
* :func:`resolve_principals` matches each entry to an EXISTING account (by id
  or email, case-insensitive) or group (by id or exact name) in one query per
  kind, and :meth:`ResolvedPrincipals.refusal` refuses the whole call naming
  every entry that matches nothing - sharing with nine of the ten people asked
  for, silently, is the failure an access change must never have - or a
  deactivated account to grant;
* :func:`stored_grants` reads one grant list as stored, :func:`plan_grants`
  turns ``add`` / ``remove`` / ``replace`` into the list to store and what that
  changes (:class:`GrantDelta`), for the result and the audit row;
  :func:`grants_audit` reads the same delta off a write that names the whole
  list (a Share dialog's ``PATCH``), from a :func:`grant_snapshot` taken first;
* :func:`load_principals` names every person and group an entity's grants list
  (:class:`PrincipalIndex`), so a tool reports who has access in names, never
  bare UUIDs;
* :class:`SharingChange` is the shape of the grant fields one write sets, with
  ``PATCH`` semantics - what ``apply_user_share_fields`` reads.

Nothing here writes: the grant write stays ``apply_user_share_fields`` /
``normalize_user_grants`` (``api/v1/helpers.py``), which every surface shares.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.group import Group
from app.models.user import User

#: People and groups one call may name - an audience, not a directory import.
MAX_PRINCIPALS_PER_CALL = 100

#: What one sharing call does with the principals it names.
SHARE_ACTIONS: tuple[str, ...] = ("add", "remove", "replace")

_BRACKETED_EMAIL_RE = re.compile(r"<\s*([^<>\s]+@[^<>\s]+)\s*>")
_ENTRY_SEPARATORS_RE = re.compile(r"[,;\n]+")


class SharingChange(BaseModel):
    """The sharing fields of one write, with ``PATCH`` semantics.

    A field left unset keeps what is stored: ``apply_user_share_fields`` reads
    ``model_fields_set``. The REST update bodies carry the same fields under
    the same names, so a route hands its body over as is.
    """

    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


@dataclass(frozen=True)
class Principal:
    """A person or a group a grant names, as the platform knows them."""

    kind: str  # "user" | "group"
    id: str  # canonical UUID string, the form the grant lists store
    label: str  # "Ada Lovelace <ada@example.com>" / the group's name
    is_active: bool = True


@dataclass
class PrincipalIndex:
    """The principals an entity's grants or a call name, by canonical id."""

    users: dict[str, Principal] = field(default_factory=dict)
    groups: dict[str, Principal] = field(default_factory=dict)

    def user(self, user_id: str) -> Principal:
        return self.users.get(user_id) or Principal("user", user_id, "a deleted account")

    def group(self, group_id: str) -> Principal:
        return self.groups.get(group_id) or Principal("group", group_id, "a deleted group")

    def add(self, principals: Iterable[Principal]) -> None:
        for principal in principals:
            (self.users if principal.kind == "user" else self.groups)[principal.id] = principal


@dataclass
class ResolvedPrincipals:
    """What :func:`resolve_principals` made of a call's ``users`` / ``groups``."""

    users: list[Principal] = field(default_factory=list)
    groups: list[Principal] = field(default_factory=list)
    #: Each entry no account / group answers to, as the caller wrote it.
    unknown_users: list[str] = field(default_factory=list)
    unknown_groups: list[str] = field(default_factory=list)

    def refusal(self, *, granting: bool) -> str | None:
        """Why the call must change nothing, naming the entries, or ``None``.

        An entry that matches nothing refuses the whole call: sharing with
        nine of the ten people asked for, silently, is the failure an access
        change must never have. Granting a deactivated account is refused too
        (``normalize_user_grants`` would refuse it by id); taking a grant away
        from one is not.
        """
        missing = []
        if self.unknown_users:
            missing.append("no account on this platform matches " + ", ".join(map(_quoted, self.unknown_users)))
        if self.unknown_groups:
            missing.append("no group matches " + ", ".join(map(_quoted, self.unknown_groups)))
        if missing:
            return (
                f"{'; '.join(missing)}. Nothing was changed. Access can only be given to people "
                "who already have an account here - nobody is invited. Check the addresses with the user."
            )
        inactive = [principal.label for principal in self.users if not principal.is_active]
        if granting and inactive:
            return f"these accounts are deactivated and cannot be given access: {', '.join(inactive)}. Nothing was changed."
        return None


def _quoted(entry: str) -> str:
    return f'"{entry}"'


def principal_entries(value: object) -> list[str]:
    """The distinct, non-empty entries of a ``users`` / ``groups`` argument.

    A list is the schema's form; a JSON list sent as a string and a comma,
    semicolon or newline separated string are read the same way, and an
    object entry gives its ``email`` or ``id``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return principal_entries(parsed)
        raw_items: list[object] = list(_ENTRY_SEPARATORS_RE.split(text))
    elif isinstance(value, (list, tuple)):
        raw_items = [
            (item.get("email") or item.get("id") or item.get("name") or "") if isinstance(item, dict) else item
            for item in value
        ]
    else:
        raw_items = [value]
    entries: list[str] = []
    for item in raw_items:
        entry = str(item or "").strip()
        if entry and entry not in entries:
            entries.append(entry)
    return entries


def _canonical_id(value: str) -> str | None:
    parsed = safe_uuid(value)
    return str(parsed) if parsed is not None else None


def _email_of(entry: str) -> str | None:
    """The lowered address *entry* spells (``Ada <ada@x.com>``, ``mailto:``), or None."""
    text = entry.strip()
    bracketed = _BRACKETED_EMAIL_RE.search(text)
    if bracketed:
        text = bracketed.group(1)
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:") :]
    text = text.strip().rstrip(".,;")
    if "@" not in text or any(char.isspace() for char in text):
        return None
    return text.lower()


def person_label(full_name: str | None, email: str | None) -> str:
    name = (full_name or "").strip()
    address = (email or "").strip()
    if name and address:
        return f"{name} <{address}>"
    return name or address or "an unnamed account"


async def resolve_principals(
    db: AsyncSession,
    users: Sequence[str],
    groups: Sequence[str] = (),
) -> ResolvedPrincipals:
    """Match each entry to a platform account or group: one query per kind.

    A user entry is a user id or an email address (case-insensitive, an
    ``Ada <ada@x.com>`` form read for its address); a group entry is a group id
    or a group's exact name (case-insensitive). Duplicates collapse onto the
    first mention. Nothing is created: an entry that matches nothing is
    returned in ``unknown_*`` for the caller to name.
    """
    resolved = ResolvedPrincipals()

    user_keys = [(entry, _canonical_id(entry), _email_of(entry)) for entry in users]
    ids = {key for _entry, key, _email in user_keys if key}
    emails = {email for _entry, key, email in user_keys if not key and email}
    by_id: dict[str, Principal] = {}
    by_email: dict[str, Principal] = {}
    if ids or emails:
        clauses = []
        if ids:
            clauses.append(User.id.in_([uuid.UUID(value) for value in sorted(ids)]))
        if emails:
            clauses.append(func.lower(User.email).in_(sorted(emails)))
        rows = await db.execute(select(User.id, User.email, User.full_name, User.is_active).where(or_(*clauses)))
        for user_id, email, full_name, is_active in rows.all():
            principal = Principal("user", str(user_id), person_label(full_name, email), bool(is_active))
            by_id[principal.id] = principal
            by_email[(email or "").strip().lower()] = principal
    for entry, key, email in user_keys:
        found = by_id.get(key) if key else by_email.get(email or "")
        if found is None:
            resolved.unknown_users.append(entry)
        elif found not in resolved.users:
            resolved.users.append(found)

    group_keys = [(entry, _canonical_id(entry)) for entry in groups]
    group_ids = {key for _entry, key in group_keys if key}
    names = {entry.strip().lower() for entry, key in group_keys if not key}
    groups_by_id: dict[str, Principal] = {}
    groups_by_name: dict[str, Principal] = {}
    if group_ids or names:
        clauses = []
        if group_ids:
            clauses.append(Group.id.in_([uuid.UUID(value) for value in sorted(group_ids)]))
        if names:
            clauses.append(func.lower(Group.name).in_(sorted(names)))
        rows = await db.execute(select(Group.id, Group.name).where(or_(*clauses)))
        for group_id, name in rows.all():
            principal = Principal("group", str(group_id), name or str(group_id))
            groups_by_id[principal.id] = principal
            groups_by_name[(name or "").strip().lower()] = principal
    for entry, key in group_keys:
        found = groups_by_id.get(key) if key else groups_by_name.get(entry.strip().lower())
        if found is None:
            resolved.unknown_groups.append(entry)
        elif found not in resolved.groups:
            resolved.groups.append(found)
    return resolved


# ── The audience as stored ────────────────────────────────────────────────────


def stored_grants(ids: object, permissions: object) -> list[tuple[str, str]]:
    """``[(canonical id, tier)]`` of one grant list, in stored order.

    A grant with no explicit tier is ``read_only``, as everywhere else
    (``_best_permission``); an entry that is not a UUID grants nothing and is
    skipped, like the SQL predicate skips it.
    """
    tiers: dict[str, str] = {}
    if isinstance(permissions, dict):
        for raw_key, level in permissions.items():
            key = _canonical_id(str(raw_key))
            if key is not None and isinstance(level, str):
                tiers[key] = level
    grants: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in ids if isinstance(ids, list) else []:
        key = _canonical_id(str(raw))
        if key is None or key in seen:
            continue
        seen.add(key)
        grants.append((key, tiers.get(key, "read_only")))
    return grants


def entity_grants(entity: Any, kind: str) -> tuple[object, object]:
    """The stored ``(ids, permissions)`` of one kind of grant on *entity*.

    ``kind`` is ``"user"`` or ``"group"``; a row that predates a column reads
    as unshared, so a partial double never raises.
    """
    return (
        getattr(entity, f"shared_{kind}_ids", None),
        getattr(entity, f"shared_{kind}_permissions", None),
    )


def lists_audience(entity: Any) -> bool:
    """Whether naming the entity's audience needs its grants looked up."""
    if getattr(entity, "is_company_managed", False) and not getattr(entity, "access_restricted", False):
        return False
    return bool(getattr(entity, "shared_user_ids", None) or getattr(entity, "shared_group_ids", None))


async def load_principals(db: AsyncSession, entity: Any) -> PrincipalIndex:
    """Name every person and group the entity's grants list: one query per kind.

    No query at all for an entity no grant names (or published to everyone).
    """
    index = PrincipalIndex()
    if not lists_audience(entity):
        return index
    user_ids = [key for key, _tier in stored_grants(*entity_grants(entity, "user"))]
    group_ids = [key for key, _tier in stored_grants(*entity_grants(entity, "group"))]
    if user_ids:
        rows = await db.execute(
            select(User.id, User.email, User.full_name, User.is_active).where(
                User.id.in_([uuid.UUID(value) for value in user_ids])
            )
        )
        index.add(
            Principal("user", str(user_id), person_label(full_name, email), bool(is_active))
            for user_id, email, full_name, is_active in rows.all()
        )
    if group_ids:
        rows = await db.execute(
            select(Group.id, Group.name).where(Group.id.in_([uuid.UUID(value) for value in group_ids]))
        )
        index.add(Principal("group", str(group_id), name or str(group_id)) for group_id, name in rows.all())
    return index


def count_principals(people: int, groups: int) -> str:
    parts = []
    if people:
        parts.append(f"{people} {'person' if people == 1 else 'people'}")
    if groups:
        parts.append(f"{groups} {'group' if groups == 1 else 'groups'}")
    return " and ".join(parts)


# ── Planning a sharing call ───────────────────────────────────────────────────


@dataclass
class GrantDelta:
    """What one call does to one kind of grant (people or groups), by id."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Already shared, now with the requested tier.
    retiered: list[str] = field(default_factory=list)
    #: Already had exactly the requested access.
    unchanged: list[str] = field(default_factory=list)
    #: Named for removal but not shared with them.
    not_shared: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.retiered)


def stored_delta(before: list[tuple[str, str]], after: list[tuple[str, str]]) -> GrantDelta:
    """What a write did to one kind of grant, read from the grants stored before and after it.

    The audit of a write that names the whole list (the Share dialog's
    ``PATCH``) rather than a change (:func:`plan_grants`): who gained access,
    who lost it, who kept it with another tier.
    """
    old, new = dict(before), dict(after)
    return GrantDelta(
        added=[key for key, _level in after if key not in old],
        removed=[key for key, _level in before if key not in new],
        retiered=[key for key, level in after if key in old and old[key] != level],
    )


def grants_audit(before: Any, entity: Any) -> dict[str, Any]:
    """The audit details of a grant write on *entity*: people and groups added, removed and retiered.

    *before* is :func:`grant_snapshot` of the entity taken before the write.
    The keys are the ones ``share_presentation`` / ``share_video`` log.
    """
    after = grant_snapshot(entity)
    users = stored_delta(before["user"], after["user"])
    groups = stored_delta(before["group"], after["group"])
    return {
        "added_users": users.added,
        "removed_users": users.removed,
        "retiered_users": users.retiered,
        "added_groups": groups.added,
        "removed_groups": groups.removed,
        "retiered_groups": groups.retiered,
    }


def grant_snapshot(entity: Any) -> dict[str, list[tuple[str, str]]]:
    """The grants *entity* stores now, by kind: what :func:`grants_audit` compares against."""
    return {kind: stored_grants(*entity_grants(entity, kind)) for kind in ("user", "group")}


def plan_grants(
    action: str,
    permission: str,
    current: list[tuple[str, str]],
    named: Sequence[Principal],
) -> tuple[list[str], dict[str, str], GrantDelta]:
    """The grant list + tiers of one kind after *action*, and the delta.

    * ``add``: the named principals get *permission*; one already granted gets
      the tier asked for (an upgrade or a downgrade).
    * ``remove``: the named grants go; the tier is not read.
    * ``replace``: the audience of this kind becomes exactly the named
      principals, all at *permission*.
    """
    delta = GrantDelta()
    tiers = dict(current)
    named_ids = [principal.id for principal in named]
    if action == "remove":
        delta.removed = [key for key in named_ids if key in tiers]
        delta.not_shared = [key for key in named_ids if key not in tiers]
        kept = [key for key, _level in current if key not in delta.removed]
        return kept, {key: tiers[key] for key in kept}, delta

    for key in named_ids:
        if key not in tiers:
            delta.added.append(key)
        elif tiers[key] != permission:
            delta.retiered.append(key)
        else:
            delta.unchanged.append(key)
    if action == "replace":
        delta.removed = [key for key, _level in current if key not in named_ids]
        ids = named_ids
    else:
        ids = [key for key, _level in current] + delta.added
    new_tiers = {key: tiers.get(key, permission) for key in ids}
    for key in named_ids:
        new_tiers[key] = permission
    return ids, new_tiers, delta
