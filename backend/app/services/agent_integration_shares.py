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

"""An agent's sharing carries its integrations (#2715).

An agent shared with a person or a group is only useful to them when the
integrations bound to it are shared with them too: a chat of a sharee resolves
only the bindings that person may use. So sharing an agent also shares, with
"use" (``read_only``), every personal integration bound to it that the sharer
may manage, with the same audience; unsharing it removes only the grants that
sharing created.

The origin of such a grant is kept on the integration,
``Integration.agent_share_origins`` = ``{"user:<id>" | "group:<id>": [agent ids]}``:

* a grant that existed before (given by hand) has no origin and is never
  removed by an agent unshare;
* a grant several agents gave is removed only when the last of them unshares.

Company-managed integrations are left alone (everyone already uses them, or
an administrator narrowed them on purpose). A ``per_user`` integration keeps
asking each sharee to connect their own account: the grant gives visibility,
never a token. The drift read (:func:`agent_share_drift`) names every sharee of
an agent who cannot use an integration bound to it; the fleet health finding
``integration_not_shared`` reports it and its safe fix calls
:func:`grant_missing_shares`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The tier an agent share gives on its integrations: bind and run ("use").
GRANT_TIER = "read_only"


def _canonical(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def principal_keys(agent: Any) -> set[str]:
    """``user:<id>`` / ``group:<id>`` for everyone *agent* is shared with.

    A company-managed agent open to everyone has no audience to carry (it binds
    company-managed integrations only); one narrowed with ``access_restricted``
    has its named groups and people.
    """
    if getattr(agent, "is_company_managed", False) and not getattr(agent, "access_restricted", False):
        return set()
    keys = {f"user:{c}" for c in (_canonical(u) for u in agent.shared_user_ids or []) if c}
    keys |= {f"group:{c}" for c in (_canonical(g) for g in agent.shared_group_ids or []) if c}
    return keys


def bound_integration_ids(agent: Any) -> list[str]:
    """The canonical ids of the integrations bound to *agent*, in order."""
    from app.services.integration_credentials import flatten_binding_ids

    seen: list[str] = []
    for raw in flatten_binding_ids(getattr(agent, "integration_bindings", None)):
        cid = _canonical(raw)
        if cid and cid not in seen:
            seen.append(cid)
    return seen


def _split(key: str) -> tuple[str, str]:
    kind, _, ident = key.partition(":")
    return kind, ident


def has_grant(integration: Any, key: str) -> bool:
    """Whether *integration* already names the principal *key* in its grants."""
    kind, ident = _split(key)
    if kind == "user" and _canonical(getattr(integration, "user_id", None)) == ident:
        return True
    column = "shared_user_ids" if kind == "user" else "shared_group_ids"
    return ident in {_canonical(v) for v in getattr(integration, column, None) or []}


def add_grant(integration: Any, key: str, agent_id: str) -> bool:
    """Grant *key* "use" on *integration* for *agent_id*; True when a grant was written.

    An existing grant (by hand, or a higher tier) is kept as it is and gets no
    origin, so no agent unshare ever removes it.
    """
    if has_grant(integration, key):
        origins = dict(getattr(integration, "agent_share_origins", None) or {})
        if key in origins and agent_id not in origins[key]:
            # Another agent's grant: this agent shares it too now.
            origins[key] = [*origins[key], agent_id]
            integration.agent_share_origins = origins
        return False
    kind, ident = _split(key)
    if kind == "user":
        integration.shared_user_ids = [*(integration.shared_user_ids or []), ident]
        integration.shared_user_permissions = {**(integration.shared_user_permissions or {}), ident: GRANT_TIER}
    else:
        integration.shared_group_ids = [*(integration.shared_group_ids or []), ident]
        integration.shared_group_permissions = {**(integration.shared_group_permissions or {}), ident: GRANT_TIER}
    origins = dict(getattr(integration, "agent_share_origins", None) or {})
    origins[key] = [agent_id]
    integration.agent_share_origins = origins
    return True


def drop_stale_origins(integration: Any) -> None:
    """Forget the origin of every grant a hand edit of *integration* removed.

    Called after a share write that is not an agent share (the integration's
    REST update and ``update_integration``): a grant removed by hand loses its
    agent origin, so one given again by hand later is the owner's and no agent
    unshare takes it away.
    """
    origins = getattr(integration, "agent_share_origins", None) or {}
    kept = {k: v for k, v in origins.items() if has_grant(integration, k)}
    if kept != origins:
        integration.agent_share_origins = kept or None


def _stored_tier(integration: Any, key: str) -> str | None:
    """The tier *integration* stores for *key*, None when it names no grant."""
    if not has_grant(integration, key):
        return None
    kind, ident = _split(key)
    column = "shared_user_permissions" if kind == "user" else "shared_group_permissions"
    perms: dict[str | None, str] = {
        _canonical(k): str(v) for k, v in (getattr(integration, column, None) or {}).items()
    }
    return perms.get(ident, GRANT_TIER)


def _snapshot(integration: Any) -> dict[str, Any]:
    return {name: getattr(integration, name, None) for name in _GRANT_COLUMNS}


_GRANT_COLUMNS = (
    "shared_user_ids",
    "shared_user_permissions",
    "shared_group_ids",
    "shared_group_permissions",
    "agent_share_origins",
)


async def _normalise(db: AsyncSession, integration: Any, before: dict[str, Any]) -> str | None:
    """Write the grants through the shared normalizers; the refusal, or None.

    :func:`app.api.v1.helpers.normalize_user_grants` and
    :func:`~app.api.v1.helpers.normalize_group_grants` are the one writer of
    the share columns: canonical ids, newly added people active and locked
    ``FOR KEY SHARE``, groups existing. On a refusal the integration is put
    back as it was (*before*, from :func:`_snapshot`) and the reason returned.
    """
    from fastapi import HTTPException

    from app.api.v1.helpers import normalize_group_grants, normalize_user_grants

    try:
        integration.shared_user_ids, integration.shared_user_permissions = await normalize_user_grants(
            db,
            integration.shared_user_ids,
            integration.shared_user_permissions,
            existing_ids=before["shared_user_ids"],
            owner_id=getattr(integration, "user_id", None),
        )
        integration.shared_group_ids, integration.shared_group_permissions = await normalize_group_grants(
            db,
            integration.shared_group_ids,
            integration.shared_group_permissions,
            existing_ids=before["shared_group_ids"],
        )
    except HTTPException as exc:
        for name, value in before.items():
            setattr(integration, name, value)
        return str(exc.detail)
    return None


def remove_grant(integration: Any, key: str, agent_id: str) -> bool:
    """Withdraw *agent_id*'s share of *key*; True when the grant itself was removed.

    Only a grant whose origin names the agent is touched, and it goes only when
    no other agent's share keeps it. A grant the owner changed by hand since
    (raised above "use", or removed then given again) is the owner's now: its
    origin is dropped and the grant stays.
    """
    origins = dict(getattr(integration, "agent_share_origins", None) or {})
    holders = [a for a in origins.get(key, []) if a != agent_id]
    if key not in origins or len(holders) == len(origins[key]):
        return False
    if _stored_tier(integration, key) != GRANT_TIER:
        origins.pop(key)
        integration.agent_share_origins = origins or None
        return False
    if holders:
        origins[key] = holders
        integration.agent_share_origins = origins
        return False
    origins.pop(key)
    integration.agent_share_origins = origins or None
    kind, ident = _split(key)
    if kind == "user":
        integration.shared_user_ids = [u for u in integration.shared_user_ids or [] if _canonical(u) != ident] or None
        perms = {k: v for k, v in (integration.shared_user_permissions or {}).items() if _canonical(k) != ident}
        integration.shared_user_permissions = perms or None
    else:
        integration.shared_group_ids = [g for g in integration.shared_group_ids or [] if _canonical(g) != ident] or None
        perms = {k: v for k, v in (integration.shared_group_permissions or {}).items() if _canonical(k) != ident}
        integration.shared_group_permissions = perms or None
    return True


@dataclass
class ShareSync:
    """What an agent share did on its integrations, for the answer."""

    shared: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: ``(integration name, why)`` for the integrations left as they were.
    not_shared: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.shared or self.removed)

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.shared:
            out.append("Integrations shared with the same audience (use): " + ", ".join(self.shared) + ".")
        if self.removed:
            out.append("Integration shares this agent had given, removed: " + ", ".join(self.removed) + ".")
        if self.not_shared:
            out.append(
                "Integrations NOT shared (the sharees cannot use them in their chats): "
                + "; ".join(f"{name} ({why})" for name, why in self.not_shared)
                + "."
            )
        return out


async def _load_integrations(db: AsyncSession, ids: Iterable[str]) -> list[Any]:
    from app.models.integration import Integration

    parsed = [uuid.UUID(i) for i in ids]
    if not parsed:
        return []
    rows = await db.execute(select(Integration).where(Integration.id.in_(parsed)))
    return list(rows.scalars().all())


async def sync_agent_integration_shares(
    db: AsyncSession,
    agent: Any,
    *,
    before: Sequence[str] | set[str],
    actor_id: Any,
    actor_is_admin: bool,
    actor_group_ids: list[str] | None = None,
) -> ShareSync:
    """Carry *agent*'s audience onto its integrations, after a share or bind change.

    *before* is :func:`principal_keys` read before the write. Every principal the
    agent is shared with now gets "use" on every bound personal integration the
    actor may manage (:func:`app.api.v1.helpers.can_manage_as`); every principal
    it lost loses only the grants this agent gave. Unbound integrations are
    left as they are.
    """
    from app.api.v1.helpers import can_manage_as
    from app.services.integration_user_credentials import is_per_user, prune_unreachable_user_credentials

    result = ShareSync()
    agent_id = str(agent.id)
    after = principal_keys(agent)
    lost = set(before) - after
    if not after and not lost:
        return result
    for integ in await _load_integrations(db, bound_integration_ids(agent)):
        if getattr(integ, "is_company_managed", False):
            continue
        origins = getattr(integ, "agent_share_origins", None) or {}
        missing = [k for k in sorted(after) if not has_grant(integ, k)]
        tracked = [k for k in sorted(lost) if agent_id in origins.get(k, [])]
        manageable = can_manage_as(_canonical_uuid(actor_id), actor_is_admin, integ, actor_group_ids)
        if not manageable:
            if missing:
                result.not_shared.append((integ.name, "you may not manage it: ask its owner to share it"))
            continue
        if not missing and not tracked:
            # Granted already: a grant another agent gave is kept for this one
            # too, so the other agent's unshare does not take it away.
            for key in sorted(after):
                add_grant(integ, key, agent_id)
            continue
        stored = _snapshot(integ)
        granted = [k for k in sorted(after) if add_grant(integ, k, agent_id)]
        withdrawn = [k for k in tracked if remove_grant(integ, k, agent_id)]
        refused = await _normalise(db, integ, stored) if granted else None
        if refused:
            result.not_shared.append((integ.name, refused))
            continue
        if granted:
            result.shared.append(integ.name)
        if withdrawn:
            result.removed.append(integ.name)
        if (granted or withdrawn) and is_per_user(integ):
            await db.flush()
            await prune_unreachable_user_credentials(db, integ)
    return result


def _canonical_uuid(value: Any) -> uuid.UUID | None:
    text = _canonical(value)
    return uuid.UUID(text) if text else None


@dataclass(frozen=True)
class Drift:
    """A sharee of an agent who cannot use an integration bound to it."""

    agent_id: str
    integration_id: str
    principal: str

    @property
    def evidence(self) -> str:
        return f"{self.agent_id}:{self.integration_id}:{self.principal}"

    @classmethod
    def parse(cls, evidence: str) -> Drift | None:
        parts = evidence.split(":")
        if len(parts) != 4 or parts[2] not in ("user", "group"):
            return None
        return cls(parts[0], parts[1], f"{parts[2]}:{parts[3]}")


async def agent_share_drift(db: AsyncSession, agents: Iterable[Any]) -> list[Drift]:
    """Every (agent, integration, sharee) where the sharee cannot use the integration.

    A person is judged by the visibility rule itself
    (:func:`app.services.integration_credentials.users_seeing_integration`: own,
    a person grant, one of their groups); a group by a grant naming it. A
    company-managed integration never drifts: everyone uses it, or an
    administrator narrowed it on purpose, and the safe fix never touches it.
    """
    from app.services.integration_credentials import users_seeing_integration

    drift: list[Drift] = []
    wanted: dict[str, list[tuple[str, set[str]]]] = {}
    for agent in agents:
        keys = principal_keys(agent)
        if not keys:
            continue
        for iid in bound_integration_ids(agent):
            wanted.setdefault(iid, []).append((str(agent.id), keys))
    if not wanted:
        return drift
    for integ in await _load_integrations(db, wanted):
        if getattr(integ, "is_company_managed", False):
            continue
        iid = str(integ.id)
        people = {k for _a, keys in wanted[iid] for k in keys if k.startswith("user:")}
        seeing = (
            {f"user:{u}" for u in await users_seeing_integration(db, integ.id, [_split(k)[1] for k in people])}
            if people
            else set()
        )
        for agent_id, keys in wanted[iid]:
            for key in sorted(keys):
                ok = key in seeing if key.startswith("user:") else has_grant(integ, key)
                if not ok:
                    drift.append(Drift(agent_id, iid, key))
    return drift


async def grant_missing_shares(
    db: AsyncSession,
    drifts: Iterable[Drift],
    *,
    actor_id: Any,
    actor_is_admin: bool,
    actor_group_ids: list[str] | None = None,
) -> int:
    """The safe fix of ``integration_not_shared``: grant each drift, origin recorded.

    Only on an integration the actor may manage; the count of grants written.
    """
    from app.api.v1.helpers import can_manage_as
    from app.services.integration_user_credentials import is_per_user, prune_unreachable_user_credentials

    by_integration: dict[str, list[Drift]] = {}
    for d in drifts:
        by_integration.setdefault(d.integration_id, []).append(d)
    written = 0
    for integ in await _load_integrations(db, by_integration):
        if getattr(integ, "is_company_managed", False):
            continue
        if not can_manage_as(_canonical_uuid(actor_id), actor_is_admin, integ, actor_group_ids):
            continue
        stored = _snapshot(integ)
        changed = 0
        for d in by_integration[str(integ.id)]:
            changed += int(add_grant(integ, d.principal, d.agent_id))
        if changed and await _normalise(db, integ, stored):
            continue
        if changed and is_per_user(integ):
            await db.flush()
            await prune_unreachable_user_credentials(db, integ)
        written += changed
    return written
