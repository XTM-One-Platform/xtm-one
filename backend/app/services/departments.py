# SPDX-License-Identifier: Apache-2.0

"""The department a person counts for in LLM analytics: one rule, one module.

A person belongs to many groups and to one department. The department is
resolved from an ordered chain of sources (``PlatformSettings.
department_settings["sources"]``, both by default, in this order):

* ``directory`` - the department the identity source reports
  (``User.directory_department``: the SSO attribute named by
  ``directory_attribute``, refreshed at every sign-in, or an administrator
  import), matched case-insensitively to a department's name or one of its
  ``directory_values``. This is where an HRIS field lands: HRIS -> identity
  provider -> SAML / OIDC claim.
* ``groups`` - the first ``DepartmentGroupRule`` by priority whose group the
  person is a member of.

A person no source resolves is ``Unassigned`` (``None``). The trace sink
resolves the department when a trace is written and stores it on the trace
(``services/tracing/attribution.py``), so history never follows a move; the
read path never resolves a department, it reads the stored one.

The mapping is read through :func:`load_department_map`, cached per process
for :data:`_CACHE_SECONDS` (the sink resolves it for every trace) and dropped
by :func:`invalidate_department_map` when an administrator saves it.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department, DepartmentGroupRule
from app.models.group import Group, GroupMember
from app.models.platform_settings import PlatformSettings
from app.models.user import User

#: The sources of the resolution chain, in their default order.
DEPARTMENT_SOURCES: tuple[str, ...] = ("directory", "groups")
#: The SSO attribute (SAML) / claim (OIDC) read for ``User.directory_department`` by default.
DEFAULT_DIRECTORY_ATTRIBUTE = "department"
#: The dimension key of a person no source resolves, and its label.
UNASSIGNED_KEY = "unassigned"
UNASSIGNED_LABEL = "Unassigned"
#: The dimension key of usage attributed to nobody (``attribution.RESIDUAL_ATTRIBUTIONS``), and its label.
UNATTRIBUTED_KEY = "unattributed"
UNATTRIBUTED_LABEL = "Unattributed (no person)"

_CACHE_SECONDS = 30.0
#: Ids per ``IN (...)`` lookup: asyncpg caps one statement at 32767 parameters.
_LOOKUP_CHUNK = 5000


@dataclass(frozen=True)
class DepartmentSettings:
    sources: tuple[str, ...] = DEPARTMENT_SOURCES
    directory_attribute: str = DEFAULT_DIRECTORY_ATTRIBUTE

    def payload(self) -> dict[str, Any]:
        return {"sources": list(self.sources), "directory_attribute": self.directory_attribute}


def normalize_settings(raw: Any) -> DepartmentSettings:
    """*raw* (the stored JSON, or a request body) as settings: unknown sources dropped, duplicates once."""
    raw = raw if isinstance(raw, dict) else {}
    sources: list[str] = []
    for value in raw.get("sources") or DEPARTMENT_SOURCES:
        source = str(value).strip().lower()
        if source in DEPARTMENT_SOURCES and source not in sources:
            sources.append(source)
    attribute = str(raw.get("directory_attribute") or "").strip() or DEFAULT_DIRECTORY_ATTRIBUTE
    return DepartmentSettings(sources=tuple(sources), directory_attribute=attribute[:200])


async def load_department_settings(db: AsyncSession) -> DepartmentSettings:
    raw = (
        await db.execute(
            select(PlatformSettings.department_settings)
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return normalize_settings(raw)


def directory_department_from_claims(claims: dict[str, Any], attribute: str) -> str | None:
    """The department *claims* (SAML attributes or OIDC userinfo) carry under *attribute*.

    The attribute's exact name first, then a key that equals it case-insensitively
    or ends with ``/<attribute>`` (a namespaced SAML claim URI). A list reads as
    its first non-empty value.
    """
    wanted = attribute.strip()
    if not wanted or not isinstance(claims, dict):
        return None
    folded = wanted.casefold()
    keys = [wanted] + [
        key
        for key in claims
        if isinstance(key, str) and key != wanted and (key.casefold() == folded or key.casefold().endswith(f"/{folded}"))
    ]
    for key in keys:
        value = claims.get(key)
        values = value if isinstance(value, list | tuple) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                return item.strip()[:200]
    return None


async def record_directory_department(db: AsyncSession, user: User, claims: dict[str, Any]) -> None:
    """Refresh *user*'s directory department from a sign-in's *claims*, in the caller's transaction.

    Only when the claims carry the attribute: a provider that sends none never
    clears a department an administrator imported.
    """
    settings = await load_department_settings(db)
    value = directory_department_from_claims(claims, settings.directory_attribute)
    if value is not None and value != user.directory_department:
        user.directory_department = value


@dataclass(frozen=True)
class DepartmentMap:
    """The resolution chain as it stands: what :meth:`resolve` reads."""

    settings: DepartmentSettings = field(default_factory=DepartmentSettings)
    names: dict[uuid.UUID, str] = field(default_factory=dict)
    #: Case-folded directory value -> department.
    by_directory_value: dict[str, uuid.UUID] = field(default_factory=dict)
    #: ``(group, department)``, first match wins.
    group_rules: tuple[tuple[uuid.UUID, uuid.UUID], ...] = ()

    @property
    def configured(self) -> bool:
        """Whether any department exists: until one does, nothing is resolved (the backfill waits)."""
        return bool(self.names)

    def resolve(self, directory_department: str | None, group_ids: Iterable[uuid.UUID]) -> uuid.UUID | None:
        groups = set(group_ids)
        for source in self.settings.sources:
            if source == "directory" and directory_department:
                found = self.by_directory_value.get(directory_department.strip().casefold())
                if found is not None:
                    return found
            elif source == "groups":
                for group_id, department_id in self.group_rules:
                    if group_id in groups:
                        return department_id
        return None


_cache: tuple[float, DepartmentMap] | None = None


def invalidate_department_map() -> None:
    """Drop this process's cached mapping (an administrator saved it); other replicas catch up within the TTL."""
    global _cache
    _cache = None


async def load_department_map(db: AsyncSession, *, fresh: bool = False) -> DepartmentMap:
    global _cache
    now = time.monotonic()
    if not fresh and _cache is not None and now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    settings = await load_department_settings(db)
    departments = (await db.execute(select(Department.id, Department.name, Department.directory_values))).all()
    names = {row.id: row.name for row in departments}
    by_value: dict[str, uuid.UUID] = {}
    # The names first, so an alias never takes another department's own name.
    for row in departments:
        by_value.setdefault(row.name.strip().casefold(), row.id)
    for row in departments:
        for value in row.directory_values or []:
            if isinstance(value, str) and value.strip():
                by_value.setdefault(value.strip().casefold(), row.id)
    rules = (
        await db.execute(
            select(DepartmentGroupRule.group_id, DepartmentGroupRule.department_id)
            .join(Group, Group.id == DepartmentGroupRule.group_id)
            .order_by(DepartmentGroupRule.priority.asc(), Group.name.asc())
        )
    ).all()
    mapping = DepartmentMap(
        settings=settings,
        names=names,
        by_directory_value=by_value,
        group_rules=tuple((row.group_id, row.department_id) for row in rules),
    )
    _cache = (now, mapping)
    return mapping


def _chunks(values: Sequence[uuid.UUID]) -> Iterable[Sequence[uuid.UUID]]:
    for index in range(0, len(values), _LOOKUP_CHUNK):
        yield values[index : index + _LOOKUP_CHUNK]


async def resolve_user_departments(
    db: AsyncSession, user_ids: Iterable[uuid.UUID], mapping: DepartmentMap | None = None
) -> dict[uuid.UUID, uuid.UUID | None]:
    """``user -> department`` of *user_ids* today, ``None`` for Unassigned (and for an unknown id)."""
    mapping = mapping or await load_department_map(db)
    ids = sorted(set(user_ids), key=str)
    resolved: dict[uuid.UUID, uuid.UUID | None] = dict.fromkeys(ids)
    if not ids or not mapping.configured:
        return resolved
    directory: dict[uuid.UUID, str | None] = {}
    memberships: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for chunk in _chunks(ids):
        for user_id, value in (await db.execute(select(User.id, User.directory_department).where(User.id.in_(chunk)))).all():
            directory[user_id] = value
        if "groups" in mapping.settings.sources and mapping.group_rules:
            rows = await db.execute(
                select(GroupMember.user_id, GroupMember.group_id).where(GroupMember.user_id.in_(chunk))
            )
            for user_id, group_id in rows.all():
                memberships[user_id].add(group_id)
    for user_id in ids:
        if user_id in directory:
            resolved[user_id] = mapping.resolve(directory[user_id], memberships.get(user_id, ()))
    return resolved


async def department_preview(db: AsyncSession, *, users_per_department: int = 200) -> dict[str, Any]:
    """Who the mapping resolves where, over the active people: the Departments screen's preview."""
    mapping = await load_department_map(db, fresh=True)
    people = (
        await db.execute(select(User.id, User.email, User.full_name, User.directory_department).where(User.is_active.is_(True)))
    ).all()
    resolved = await resolve_user_departments(db, [row.id for row in people], mapping)
    by_department: dict[uuid.UUID | None, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(people, key=lambda item: item.email.casefold()):
        by_department[resolved.get(row.id)].append(
            {
                "user_id": str(row.id),
                "email": row.email,
                "full_name": row.full_name,
                "directory_department": row.directory_department,
            }
        )
    departments = [
        {
            "department_id": str(department_id),
            "name": name,
            "user_count": len(by_department.get(department_id, [])),
            "users": by_department.get(department_id, [])[:users_per_department],
        }
        for department_id, name in sorted(mapping.names.items(), key=lambda item: item[1].casefold())
    ]
    unassigned = by_department.get(None, [])
    return {
        "configured": mapping.configured,
        "settings": mapping.settings.payload(),
        "user_count": len(people),
        "resolved_count": len(people) - len(unassigned),
        "unassigned_count": len(unassigned),
        "departments": departments,
        "unassigned": unassigned[:users_per_department],
    }


def department_label(department_id: uuid.UUID | str | None, names: dict[uuid.UUID, str]) -> str:
    """How a department key reads in a result; an id no longer known reads as a deleted department."""
    if department_id in (None, UNASSIGNED_KEY):
        return UNASSIGNED_LABEL
    if department_id == UNATTRIBUTED_KEY:
        return UNATTRIBUTED_LABEL
    parsed = department_id if isinstance(department_id, uuid.UUID) else _uuid_or_none(department_id)
    if parsed is not None and parsed in names:
        return names[parsed]
    return f"Deleted department ({str(department_id)[:8]})"


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
