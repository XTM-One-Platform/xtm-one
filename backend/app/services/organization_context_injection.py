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

"""Whether a turn's system prompt starts with the organization context, and what it holds (#11533).

One cascade, one resolver, used by every entry point (chat turns of any
channel, assignment runs whatever their trigger, sub-agent and handover
turns, the general chat): the most specific EXPLICIT value wins,

    assignment.organization_context_mode  (unless "inherit")
    > agent.organization_context_mode     (unless "inherit")
    > platform_settings.organization_context_enabled (default true)

A chat turn has no assignment (agent > platform); the general chat has no
agent either (platform). :func:`resolve` is the pure rule;
:func:`organization_summary_for` reads the settings and the profile and
answers the block to put at the top of the system prompt, or ``""``.

The block is "basic + categories": the identity (name, sector, regions,
size, risk appetite; :data:`SUMMARY_MAX_CHARS`, enforced, never grows with
a list) and the CATEGORY INDEX (one line per category of
``organization_context_categories`` with its size and the call that reads
it). The categories the cascade selects (``organization_context_categories``
on platform, agent and assignment, the same most-specific-explicit rule) are
preloaded after it, each capped, the rest behind the tool. A fleet run keeps
its own fuller block (``organization_context.fleet_run_context``); the
assignment engine then leaves this one out so the profile appears once.

An explicit ``off`` on the agent or the assignment also removes the
``get_organization_context`` tool (:func:`tools_enabled`): the person who
unchecked it chose an agent without organization context. The platform
switch only removes the block.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

OrganizationContextMode = Literal["inherit", "always", "off"]
#: The values an agent or an assignment stores; "inherit" is the default.
ORGANIZATION_CONTEXT_MODES: tuple[str, ...] = ("inherit", "always", "off")

#: Hard cap of the basic block (identity + category index), in characters
#: (about 500 tokens at 4 chars a token).
SUMMARY_MAX_CHARS = 2000
#: Header of the basic block.
SUMMARY_HEADER = "--- Organization summary ---"


def normalize_mode(value: object) -> str:
    """A stored or submitted mode, ``inherit`` for anything unknown or empty."""
    mode = str(value or "").strip().lower()
    return mode if mode in ORGANIZATION_CONTEXT_MODES else "inherit"


def check_mode(value: object) -> str:
    """A submitted mode, validated (``ValueError`` names the accepted values)."""
    mode = str(value or "").strip().lower()
    if mode not in ORGANIZATION_CONTEXT_MODES:
        raise ValueError(f"organization_context_mode {value!r} is not one of {', '.join(ORGANIZATION_CONTEXT_MODES)}")
    return mode


def resolve(
    *,
    platform_enabled: bool,
    agent_mode: object = None,
    assignment_mode: object = None,
) -> bool:
    """The cascade: assignment > agent > platform, the most specific explicit value wins."""
    for mode in (normalize_mode(assignment_mode), normalize_mode(agent_mode)):
        if mode == "always":
            return True
        if mode == "off":
            return False
    return bool(platform_enabled)


async def platform_enabled(db: AsyncSession) -> bool:
    """The platform default (Settings > Organization); true when no settings row exists yet."""
    from app.models.platform_settings import PlatformSettings

    value = (
        await db.execute(
            select(PlatformSettings.organization_context_enabled)
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return True if value is None else bool(value)


def tools_enabled(*, agent_mode: object = None, assignment_mode: object = None) -> bool:
    """Whether the turn offers ``get_organization_context``: not when the most specific explicit mode is ``off``."""
    for mode in (normalize_mode(assignment_mode), normalize_mode(agent_mode)):
        if mode != "inherit":
            return mode == "always"
    return True


def tools_enabled_for(*, agent: object = None, assignment: object = None) -> bool:
    return tools_enabled(
        agent_mode=getattr(agent, "organization_context_mode", None),
        assignment_mode=getattr(assignment, "organization_context_mode", None),
    )


async def platform_categories(db: AsyncSession) -> list[str]:
    """The categories every inheriting agent preloads (Settings > Organization); none by default."""
    from app.models.platform_settings import PlatformSettings
    from app.services.organization_context_categories import normalize_categories

    value = (
        await db.execute(
            select(PlatformSettings.organization_context_categories)
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return normalize_categories(value)


async def categories_for(db: AsyncSession, *, agent: object = None, assignment: object = None) -> list[str]:
    """The preloaded categories of a turn: assignment > agent > platform, most specific explicit list wins."""
    from app.services.organization_context_categories import resolve_categories

    return resolve_categories(
        platform_categories=await platform_categories(db),
        agent_categories=getattr(agent, "organization_context_categories", None),
        assignment_categories=getattr(assignment, "organization_context_categories", None),
    )


async def effective_for(db: AsyncSession, *, agent: object = None, assignment: object = None) -> bool:
    """:func:`resolve` with the platform value read from *db*."""
    return resolve(
        platform_enabled=await platform_enabled(db),
        agent_mode=getattr(agent, "organization_context_mode", None),
        assignment_mode=getattr(assignment, "organization_context_mode", None),
    )


def _short(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def category_counts(
    profile: Any, *, tracked_count: int = 0, asset_counts: dict[str, int] | None = None
) -> dict[str, int]:
    """The size of every category of *profile* (``tracked_organizations`` from *tracked_count*).

    *asset_counts* (``organization_context.asset_list_counts``) gives the asset
    and group sizes the records hold (#1543), counted without loading a row.
    """
    from app.services import organization_context_categories as cats

    lists = asset_counts or {}
    counts = {
        category.name: lists[category.field] if category.field in lists else len(cats.rows_of(profile, category))
        for category in cats.CATEGORIES
        if category.field
    }
    counts[cats.TRACKED_ORGANIZATIONS] = tracked_count
    return counts


def render_summary(
    row: Any, *, tracked_count: int = 0, ctem_work: bool = True, asset_counts: dict[str, int] | None = None
) -> str:
    """The basic block of the primary profile *row*: identity + category index, at most :data:`SUMMARY_MAX_CHARS`.

    ``""`` when no profile is saved. The identity is a handful of short
    fields and the index one line per category of the model, so the block
    never grows with a list's length. *ctem_work* false
    (``organization_context.ctem_work_for``): the CTEM-only fields are left
    out of the "Not filled" line and no brief notice is asked (#1384).
    """
    import json

    from app.services import organization_context_categories as cats
    from app.services.content_safety import wrap_untrusted
    from app.services.organization_context import (
        CONTRACT_FIELDS,
        brief_notice_instruction,
        identity_only,
        missing_fields,
        missing_for_run,
        parse_profile,
    )

    if row is None:
        return ""
    identity = identity_only()
    profile = parse_profile(row.profile)
    facts: dict[str, Any] = {}
    if profile.name:
        facts["name"] = _short(profile.name, 120)
    if profile.sector:
        facts["sector"] = _short(profile.sector + (f" / {profile.sub_sector}" if profile.sub_sector else ""), 120)
    if profile.regions:
        facts["regions"] = profile.regions[:30]
    if profile.size:
        facts["size"] = profile.size
    appetite = profile.risk_appetite
    # Copilot mode (#1690): the identity alone, no risk appetite, currency, category index or CTEM field.
    if not identity and (appetite.annual_loss_threshold is not None or appetite.approval_threshold is not None):
        currency = f" {profile.currency}" if profile.currency else ""
        facts["risk_appetite"] = {
            key: f"{value:g}{currency}"
            for key, value in (
                ("annual_loss_threshold", appetite.annual_loss_threshold),
                ("approval_threshold", appetite.approval_threshold),
            )
            if value is not None
        }
    if profile.currency and not identity:
        facts["currency"] = profile.currency
    if profile.fx_rates and not identity:
        # #1383: the reference rates an agent cites to convert a figure into the currency above.
        facts["fx_rates"] = [
            f"1 {r.from_currency} = {r.rate:g} {r.to_currency} ({r.as_of.isoformat()}, {_short(r.source, 60)})"
            for r in profile.fx_rates[:10]
        ]
    counts = category_counts(profile, tracked_count=tracked_count, asset_counts=asset_counts)
    if identity:
        run_missing = missing_fields(profile)
        labels = dict(CONTRACT_FIELDS)
        missing = [labels[path] for path in run_missing]
        tail = "Not filled: " + (", ".join(missing) if missing else "none") + ". Never guess a missing field."
        head = f"{SUMMARY_HEADER}\nThe organization you work for (profile version {row.version})."
        body = wrap_untrusted(json.dumps(facts, ensure_ascii=False), "organization profile", stable=True)
        return "\n".join([head, body, tail])[:SUMMARY_MAX_CHARS]
    head = (
        f"{SUMMARY_HEADER}\nThe organization you work for (profile version {row.version}). "
        "The rest is in the categories below: read only the one your work needs with "
        'get_organization_context(category="software", query="cisco") (it pages); category="*" lists them.'
    )
    index = "Categories:\n" + "\n".join(
        cats.index_line(category, counts[category.name])
        for category in cats.CATEGORIES
        if cats.indexed(category, counts[category.name])
    )
    category_fields = {category.field for category in cats.CATEGORIES}
    labels = dict(CONTRACT_FIELDS)
    run_missing = missing_for_run(missing_fields(profile), ctem_work=ctem_work)
    missing = [labels[path] for path in run_missing if path not in category_fields]
    tail = "Not filled: " + (", ".join(missing) if missing else "none") + ". Never guess a missing field."
    # Incomplete: every brief of a CTEM run says so, in one info line (#11586, #1384).
    notice = brief_notice_instruction(run_missing) if ctem_work else ""
    if notice:
        tail += " " + notice
    body = wrap_untrusted(json.dumps(facts, ensure_ascii=False), "organization profile", stable=True)
    block = "\n".join([head, body, index, tail])
    if len(block) > SUMMARY_MAX_CHARS:  # bounded fields and a fixed index: never in practice
        block = block[: SUMMARY_MAX_CHARS - 3] + "..."
    return block


async def _tracked(db: AsyncSession) -> list[Any]:
    from app.services import organizations as orgs

    return [o for o in await orgs.list_organizations(db) if o.role == "tracked"]


async def render_preloaded_categories(db: AsyncSession, row: Any, names: list[str]) -> str:
    """The categories the cascade selected, each capped, after the basic block (``""`` when none)."""
    from app.services import organization_context_categories as cats
    from app.services.organization_context import hydrated_profile

    # Copilot mode exposes no category (#1690): a stored selection is kept, never preloaded.
    names = [name for name in names if name in cats.available_names()]
    if row is None or not names:
        return ""
    profile = await hydrated_profile(db, row)  # the asset and group rows from the records (#1543)
    parts: list[str] = []
    for name in names:
        category = cats.get(name)
        if category is None:
            continue
        rows = cats.tracked_rows(await _tracked(db)) if not category.field else cats.rows_of(profile, category)
        parts.append(cats.render_preloaded(category, rows))
    return "\n\n".join(parts)


async def assignment_run_organization_context(
    db: AsyncSession,
    *,
    agent: Any,
    assignment: Any,
    fleet: object | None,
    organization_ids: list[Any] | None = None,
) -> tuple[str, str]:
    """``(run prompt block, system prompt block)`` of one assignment run.

    Resolved off: neither. Resolved on: a run of a fleet member keeps the
    fuller ``fleet_run_context`` block (today's behaviour) and gets no
    basic block, so the profile appears once; any other run gets the basic
    block and its preloaded categories.
    """
    from app.services.organization_context import fleet_run_context

    if not await effective_for(db, agent=agent, assignment=assignment):
        return "", ""
    block = await fleet_run_context(
        db, getattr(agent, "id", None), fleet=fleet, organization_ids=organization_ids, agent=agent
    )
    if block:
        return block, ""
    return "", await organization_summary_for(db, agent=agent, assignment=assignment)


async def organization_summary_for(
    db: AsyncSession,
    *,
    agent: object = None,
    assignment: object = None,
) -> str:
    """The block a turn's system prompt starts with, or ``""`` (resolved off, no profile, unreadable).

    The basic block (identity + category index), then the categories the
    cascade preloads. A failed read never fails the turn: the profile is
    context, and ``get_organization_context`` stays available.
    """
    try:
        if not await effective_for(db, agent=agent, assignment=assignment):
            return ""
        from app.services.organization_context import asset_list_counts, ctem_work_for, current_version

        row = await current_version(db)
        if row is None:
            return ""
        basic = render_summary(
            row,
            tracked_count=len(await _tracked(db)),
            ctem_work=await ctem_work_for(db, agent),
            asset_counts=await asset_list_counts(db, row.organization_id),
        )
        preloaded = await render_preloaded_categories(
            db, row, await categories_for(db, agent=agent, assignment=assignment)
        )
        return basic + (f"\n\n{preloaded}" if preloaded else "")
    except Exception as exc:  # context, never a reason to fail a turn
        from app.core.utils import format_exception_message

        logger.warning("Organization summary unreadable: %s", format_exception_message(exc))
        return ""
