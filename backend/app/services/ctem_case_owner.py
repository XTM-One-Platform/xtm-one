# SPDX-License-Identifier: Apache-2.0

"""The owner a CTEM case is routed to (#1736 slice 3, #2452, #2560).

Three roles, kept apart (#2560):

- the case owner does the fix: the owner of the asset group the affected
  assets belong to;
- the accountable person decides and accepts the risk: the owner of the
  primary asset the case reaches;
- the device user (the ``owner`` of an asset record, which an endpoint source
  such as Intune fills with the employee using the device) is informed only,
  never the case owner.

The owner comes, in order:

1. the asset group owner. A case whose assets span several groups takes the
   owner of the group holding the most affected assets (ties: the group name);
   the owners of the other groups are recorded as contributors;
2. the owner of a primary asset the case reaches (an inventory link of an
   affected record or group, or a ``primary_asset`` scope source of the
   signal), the one reached by the most affected assets first;
3. the instance default ``CTEM_CASE_DEFAULT_OWNER`` (an email);
4. the person who decides CTEM (#2452): the active members of the groups
   granting ``decide`` with notify on, else the active administrators; the
   seed account (``ADMIN_EMAIL``) only when it is the sole candidate. Of
   several, the first by account creation, then id;
5. else none (an instance with no active administrator).

An owner is a platform account: an owner text is matched to an active user by
email, case-insensitive. A name that is not an account's email is skipped,
never guessed. The reason (``asset group: All MacOS x86_64 (12 of 15 affected
assets)``, ``fallback: CTEM decider (administrator)``) is the ``reason`` of the
``owner`` case event, the contributors and the routing version its
``evidence`` (JSON), so a wrong routing is diagnosable from the case.

The owner is set at the opening by this code, never by the loop agent. The case
sweep re-routes the open cases whose owner the system set before this routing
(:func:`route_open_cases`): batched and capped, one ``owner`` event per case,
no platform event and no notification, so no agent run per case. A case a person
gave an owner to, or cleared, is never touched again.

:func:`pick_route` and :func:`pick_decider` are pure (testable); the reads go
through one savepoint and never fail a case opening or a sweep.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The scope sources of a decision that name an asset of the inventory.
ASSET_SOURCES: tuple[str, ...] = ("asset", "asset_group")
#: The scope source of a decision that names a primary asset.
PRIMARY_ASSET_SOURCE = "primary_asset"
#: Most asset names one routing looks up.
MAX_ASSETS = 1000
#: How the owner was found, the head of the reason of the ``owner`` case event.
FROM_GROUP = "asset group"
FROM_PRIMARY = "primary asset"
FROM_DEFAULT = "fallback: instance default owner"
FROM_DECIDER = "fallback: CTEM decider"
FROM_KEPT = "fallback: kept, no owner found"
#: The routing version the ``owner`` event's evidence carries: a case routed by it is not re-routed.
ROUTE_VERSION = 2
#: Most open cases one sweep re-routes (#2560); the rest wait for the next sweep.
ROUTE_BATCH = 100


def _key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _email(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if "@" in text else ""


@dataclass(frozen=True)
class Candidate:
    """An asset group or a primary asset an owner may come from: its name, owner text and affected asset count."""

    name: str
    owner: str
    assets: int


@dataclass(frozen=True)
class Route:
    """Where a case goes: the owner (``None``: nobody), why, and the contributors (other group owners)."""

    owner: uuid.UUID | None = None
    reason: str | None = None
    contributors: tuple[tuple[uuid.UUID, str], ...] = field(default_factory=tuple)

    def evidence(self) -> str:
        """The ``evidence`` of the ``owner`` case event: the routing version, the basis and the contributors (JSON)."""
        return json.dumps(
            {
                "route": ROUTE_VERSION,
                "fallback": self.fallback,
                "basis": basis_of(self.reason),
                "contributors": [{"user_id": str(user), "via": via} for user, via in self.contributors],
            },
            sort_keys=True,
        )

    @property
    def fallback(self) -> bool:
        """Whether no asset group or primary asset owner was found (a default, the decider, or kept)."""
        return not (self.reason or "").startswith((FROM_GROUP, FROM_PRIMARY))


def basis_of(reason: str | None) -> dict[str, Any] | None:
    """The basis of an owner *reason* this module wrote (pure, #2571); ``None`` for any other text.

    Reads an ``owner`` event written before the basis was stored, so every routed
    case explains its owner, never only the new ones.
    """
    text = (reason or "").strip()
    if not text:
        return None
    group = re.fullmatch(rf"{re.escape(FROM_GROUP)}: (.+) \((\d+)(?: of (\d+))? affected assets\)", text, re.S)
    if group:
        return {
            "kind": "asset_group",
            "name": group.group(1),
            "assets": int(group.group(2)),
            "affected": int(group.group(3)) if group.group(3) else None,
        }
    if text.startswith(f"{FROM_PRIMARY}: "):
        return {"kind": "primary_asset", "name": text[len(FROM_PRIMARY) + 2 :]}
    if text == FROM_DEFAULT:
        return {"kind": "default"}
    decider = re.fullmatch(
        rf"{re.escape(FROM_DECIDER)} \((decide group member|administrator)(?:, first of (\d+) by account creation)?\)",
        text,
    )
    if decider:
        return {
            "kind": "decider",
            "source": "decide_group" if decider.group(1) == "decide group member" else "administrator",
            "candidates": int(decider.group(2)) if decider.group(2) else 1,
        }
    if text == FROM_KEPT:
        return {"kind": "kept"}
    return None


def default_owner_email() -> str:
    """``CTEM_CASE_DEFAULT_OWNER`` lower-cased, ``""`` when not set."""
    return (os.environ.get("CTEM_CASE_DEFAULT_OWNER") or "").strip().lower()


def asset_names(signal: Any) -> list[str]:
    """The asset names a signal names: its subject when it is an asset, then its scope's asset sources (pure)."""
    names: list[str] = []
    if getattr(signal, "subject_kind", None) == "asset":
        names.append(getattr(signal, "subject_ref", None) or "")
    names.extend(_scope_names(signal, ASSET_SOURCES))
    keys = [_key(n) for n in names]
    return list(dict.fromkeys(k for k in keys if k))[:MAX_ASSETS]


def primary_asset_names(signal: Any) -> list[str]:
    """The primary assets a signal's scope names (pure), lower-cased."""
    return list(dict.fromkeys(k for k in (_key(n) for n in _scope_names(signal, (PRIMARY_ASSET_SOURCE,))) if k))


def _scope_names(signal: Any, kinds: tuple[str, ...]) -> list[str]:
    decision = getattr(signal, "decision", None)
    scope = decision.get("scope") if isinstance(decision, Mapping) else None
    sources = scope.get("sources") if isinstance(scope, Mapping) else None
    return [str(s.get("name") or "") for s in sources or () if isinstance(s, Mapping) and s.get("from") in kinds]


def _ranked(candidates: Iterable[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: (-c.assets, _key(c.name)))


def pick_route(
    groups: Iterable[Candidate],
    primary_assets: Iterable[Candidate],
    users_by_email: Mapping[str, uuid.UUID],
    *,
    affected: int,
    default: str = "",
) -> Route:
    """The owner of a case: the majority asset group's owner, a primary asset's, then the default (pure, #2560).

    Only an owner that is an account counts: a group whose owner is no account
    is passed over for the next one. The device user (an asset record's owner)
    is never a candidate. Answers an empty :class:`Route` when none is an account.
    """
    ranked = [(c, users_by_email.get(_email(c.owner))) for c in _ranked(groups)]
    owned = [(c, user) for c, user in ranked if user is not None]
    if owned:
        chosen, user = owned[0]
        of = f" of {affected}" if affected else ""
        contributors: dict[uuid.UUID, str] = {}
        for c, other in owned[1:]:
            if other is not None and other != user and other not in contributors:
                contributors[other] = f"{FROM_GROUP}: {c.name}"
        reason = f"{FROM_GROUP}: {chosen.name} ({chosen.assets}{of} affected assets)"
        return Route(user, reason, tuple(contributors.items()))
    for c in _ranked(primary_assets):
        accountable = users_by_email.get(_email(c.owner))
        if accountable is not None:
            return Route(accountable, f"{FROM_PRIMARY}: {c.name}")
    fallback = users_by_email.get(_email(default))
    return Route(fallback, FROM_DEFAULT) if fallback is not None else Route()


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


async def case_asset_names(db: AsyncSession, signal: Any, case: Any = None) -> list[str]:
    """The affected asset names of a case: the signal's, then the assets running an upgrade case's product."""
    names = asset_names(signal) if signal is not None else []
    if case is not None:
        from app.services.ctem_case_versions import running_asset_names

        names += [_key(n) for n in await running_asset_names(db, case)]
    return list(dict.fromkeys(n for n in names if n))[:MAX_ASSETS]


async def route_case(
    db: AsyncSession,
    organization_id: uuid.UUID | None,
    names: Sequence[str],
    primary_names: Sequence[str] = (),
    *,
    profile_owners: Mapping[str, str] | None = None,
    decider: tuple[uuid.UUID | None, str | None] | None = None,
) -> Route:
    """The route of a case over its affected asset *names*: groups, primary assets, default, then the decider.

    One savepoint for the inventory reads: a failed read routes on the default
    and the decider only. *decider* is the decider already read by the caller (one read per sweep).
    """
    route = Route()
    default = default_owner_email()
    try:
        async with db.begin_nested():
            groups, primaries = await _candidates(db, organization_id, names, primary_names, profile_owners)
            emails = sorted({e for e in (_email(c.owner) for c in [*groups, *primaries]) if e} | ({default} - {""}))
            users = await _users(db, emails)
        route = pick_route(groups, primaries, users, affected=len(names), default=default)
    except Exception:  # the inventory never breaks a case opening or a sweep
        logger.info("CTEM case owner routing unavailable", exc_info=True)
    if route.owner is not None:
        return route
    owner, how = decider if decider is not None else await decider_owner(db)
    return Route(owner, how) if owner is not None else Route()


async def owner_for_signal(
    db: AsyncSession, signal: Any, case: Any = None
) -> tuple[uuid.UUID | None, str | None, tuple[tuple[uuid.UUID, str], ...]]:
    """``(owner, reason, contributors)`` of a case opened from *signal* (#2560)."""
    names = await case_asset_names(db, signal, case)
    route = await route_case(db, getattr(signal, "organization_id", None), names, primary_asset_names(signal))
    return route.owner, route.reason, route.contributors


async def _users(db: AsyncSession, emails: Sequence[str]) -> dict[str, uuid.UUID]:
    from app.models.user import User

    if not emails:
        return {}
    rows = await db.execute(
        select(User.id, func.lower(User.email)).where(
            func.lower(User.email).in_(list(emails)), User.is_active.is_(True)
        )
    )
    return {str(email): user_id for user_id, email in rows.all()}


async def primary_asset_owners(db: AsyncSession, organization_id: uuid.UUID | None) -> dict[str, str]:
    """Primary asset name (lower-cased) -> its owner text, from the organization profile; ``{}`` when none."""
    from app.services.organization_context import current_version

    row = await current_version(db, organization_id)
    profile = getattr(row, "profile", None) or {}
    owners: dict[str, str] = {}
    for item in profile.get("primary_assets") or []:
        if isinstance(item, Mapping) and item.get("name") and item.get("owner"):
            owners[_key(item["name"])[:200]] = str(item["owner"])
    return owners


async def _candidates(
    db: AsyncSession,
    organization_id: uuid.UUID | None,
    names: Sequence[str],
    primary_names: Sequence[str],
    profile_owners: Mapping[str, str] | None,
) -> tuple[list[Candidate], list[Candidate]]:
    """The asset groups and primary assets of the affected *names*, each with its owner and affected count."""
    from app.models.organization_asset_record import (
        OrganizationAssetGroup,
        OrganizationAssetGroupMember,
        OrganizationAssetRecord,
        OrganizationPrimaryAssetLink,
    )

    if organization_id is None:
        return [], []
    record, group, member, link = (
        OrganizationAssetRecord,
        OrganizationAssetGroup,
        OrganizationAssetGroupMember,
        OrganizationPrimaryAssetLink,
    )
    keys = [n[:200] for n in names]
    # name key -> record ids, and record id -> name key (several records may hold one asset).
    record_names: dict[uuid.UUID, str] = {}
    if keys:
        for record_id, name_key in (
            await db.execute(
                select(record.id, record.name_key).where(
                    record.organization_id == organization_id, record.name_key.in_(keys)
                )
            )
        ).all():
            record_names[record_id] = str(name_key)
    group_assets: dict[uuid.UUID, set[str]] = {}
    group_rows: dict[uuid.UUID, tuple[str, str]] = {}
    if record_names:
        for group_id, record_id, name, owner in (
            await db.execute(
                select(group.id, member.record_id, group.name, group.fields["owner"].astext)
                .join(member, member.group_id == group.id)
                .where(group.organization_id == organization_id, member.record_id.in_(list(record_names)))
            )
        ).all():
            group_assets.setdefault(group_id, set()).add(record_names[record_id])
            group_rows[group_id] = (str(name or ""), str(owner or ""))
    groups = [
        Candidate(name=group_rows[g][0], owner=group_rows[g][1], assets=len(assets))
        for g, assets in group_assets.items()
    ]
    # Primary assets: the links of the affected records and groups, then the signal's scope.
    reached: dict[str, set[str]] = {}
    if record_names or group_assets:
        for key, record_id, group_id in (
            await db.execute(
                select(link.primary_asset_key, link.record_id, link.group_id).where(
                    link.organization_id == organization_id,
                    link.removed.is_(False),
                    (link.record_id.in_(list(record_names)) | link.group_id.in_(list(group_assets))),
                )
            )
        ).all():
            via = {record_names[record_id]} if record_id in record_names else set(group_assets.get(group_id, ()))
            reached.setdefault(_key(key)[:200], set()).update(via)
    for name in primary_names:
        reached.setdefault(name[:200], set())
    primaries: list[Candidate] = []
    if reached:
        owners = profile_owners if profile_owners is not None else await primary_asset_owners(db, organization_id)
        primaries = [Candidate(name=k, owner=owners.get(k, ""), assets=len(v)) for k, v in reached.items()]
    return groups, primaries


def owner_event(case_id: uuid.UUID, route: Route, *, now: Any, actor: str, previous: uuid.UUID | None = None) -> Any:
    """The ``owner`` case event a routing writes: the reason, and the routing version and contributors as evidence."""
    from app.models.ctem_case import CtemCaseEvent

    return CtemCaseEvent(
        case_id=case_id,
        actor=actor,
        kind="owner",
        at=now,
        from_value=str(previous) if previous else None,
        to_value=str(route.owner) if route.owner else None,
        reason=route.reason,
        evidence=route.evidence(),
    )


def _contributor_group(via: object) -> str | None:
    text = str(via or "")
    prefix = f"{FROM_GROUP}: "
    return (text[len(prefix) :] or None) if text.startswith(prefix) else None


def owner_details(events: Iterable[Any], *, system_actor: str = "system") -> dict[str, Any]:
    """The newest ``owner`` event's reason, basis and contributors, for the case read (pure; *events* newest first).

    ``owner_basis`` (#2571) is the data the case detail words in the reader's
    language: ``{"kind": "asset_group", "name", "assets", "affected"}``,
    ``primary_asset`` (name), ``default``, ``decider`` (source, candidates),
    ``kept``, or ``person`` when a person set or cleared the owner; ``None``
    when the reason is none of these. An event written before the basis was
    stored is read from its reason.
    """
    for event in events:
        if getattr(event, "kind", None) != "owner":
            continue
        contributors: list[dict[str, str]] = []
        basis: Any = None
        try:
            value = json.loads(getattr(event, "evidence", None) or "")
        except ValueError:
            value = None
        if isinstance(value, Mapping):
            contributors = [
                {
                    "user_id": str(c.get("user_id")),
                    "via": str(c.get("via") or ""),
                    # The asset group as data (#2571): the UI never shows the stored sentence.
                    "group": _contributor_group(c.get("via")),
                }
                for c in value.get("contributors") or []
                if isinstance(c, Mapping) and c.get("user_id")
            ]
            basis = value.get("basis") if isinstance(value.get("basis"), Mapping) else None
        reason = getattr(event, "reason", None)
        if basis is None:
            basis = basis_of(reason)
        if basis is None and getattr(event, "actor", system_actor) != system_actor:
            basis = {"kind": "person"}
        return {"owner_reason": reason, "owner_basis": basis, "owner_contributors": contributors}
    return {"owner_reason": None, "owner_basis": None, "owner_contributors": []}


async def route_open_cases(db: AsyncSession, *, now: Any, actor: str = "system", limit: int = ROUTE_BATCH) -> int:
    """Route each open case the system owns (or nobody owns) that this routing did not route yet; how many.

    The caller commits. A case where a person ever set or cleared the owner (an
    ``owner`` event by another actor than *actor*) is left as it is. One
    ``owner`` event per case routed, with this routing version as evidence, keeps
    it out of the next pass; an unchanged owner is still recorded with its reason.
    A case routed to a fallback (no group or primary asset owner then) is routed
    again once an asset group or the organization profile changed after it, so
    the owners filled later reach the open cases. No platform event, no
    notification, no agent run: at most *limit* cases.
    """
    from sqlalchemy import exists, or_

    from app.models.ctem_case import OPEN_STATES, CtemCase, CtemCaseEvent
    from app.models.ctem_signal import CtemSignal

    event = CtemCaseEvent
    changed_at = await _owners_changed_at(db)
    by_person = exists().where(event.case_id == CtemCase.id, event.kind == "owner", event.actor != actor)
    settled: Any = ~event.evidence.like('%"fallback": true%')
    if changed_at is not None:
        settled = or_(settled, event.at >= changed_at)
    routed = exists().where(
        event.case_id == CtemCase.id,
        event.kind == "owner",
        event.evidence.like(f'%"route": {ROUTE_VERSION}%'),
        settled,
    )
    cases = (
        (
            await db.execute(
                select(CtemCase)
                .where(CtemCase.state.in_(OPEN_STATES), ~by_person, ~routed)
                .order_by(CtemCase.created_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not cases:
        return 0
    owners_by_org: dict[Any, dict[str, str]] = {}
    decider = await decider_owner(db)
    count = 0
    for case in cases:
        signals = (
            (await db.execute(select(CtemSignal).where(CtemSignal.case_id == case.id).limit(20))).scalars().all()
        )
        names: list[str] = []
        primaries: list[str] = []
        for signal in signals:
            names += asset_names(signal)
            primaries += primary_asset_names(signal)
        names += await case_asset_names(db, None, case)
        names = list(dict.fromkeys(names))[:MAX_ASSETS]
        org = case.organization_id
        if org not in owners_by_org:
            try:
                async with db.begin_nested():
                    owners_by_org[org] = await primary_asset_owners(db, org)
            except Exception:
                logger.info("CTEM primary asset owners unavailable", exc_info=True)
                owners_by_org[org] = {}
        route = await route_case(
            db, org, names, list(dict.fromkeys(primaries)), profile_owners=owners_by_org[org], decider=decider
        )
        if route.owner is None and case.owner_user_id is None:
            continue
        if route.owner is None:  # nobody to route to: the current owner stays, recorded as such.
            route = Route(case.owner_user_id, FROM_KEPT)
        db.add(owner_event(case.id, route, now=now, actor=actor, previous=case.owner_user_id))
        case.owner_user_id = route.owner
        count += 1
    return count


async def _owners_changed_at(db: AsyncSession) -> Any:
    """When an asset group or the organization profile last changed (where owners are typed), ``None`` unread."""
    from app.models.organization_asset_record import OrganizationAssetGroup
    from app.models.organization_context import OrganizationContextVersion

    try:
        async with db.begin_nested():
            groups = await db.scalar(select(func.max(OrganizationAssetGroup.updated_at)))
            profile = await db.scalar(select(func.max(OrganizationContextVersion.created_at)))
    except Exception:
        logger.info("CTEM owner sources change time unavailable", exc_info=True)
        return None
    known = [t for t in (groups, profile) if t is not None]
    return max(known) if known else None


async def backfill_owners(db: AsyncSession, *, now: Any, actor: str = "system") -> int:
    """The case sweep's owner pass (#2452, #2560): :func:`route_open_cases`. The caller commits."""
    return await route_open_cases(db, now=now, actor=actor)
