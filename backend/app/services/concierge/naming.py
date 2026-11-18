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

"""The name Platform Health goes by with people (#8261, #101).

An administrator may still name it in Settings > Automations > Platform Health
(``ConciergeSettings.name``) until the S1 migration drops the setting; left
empty, the name is "Platform Health" in every platform mode (#101, decided
18 Nov 2026: no mode-specific name). The name is what people read: the
Health page, the inbox, the entry points, the refusals the REST API sends,
and the display name of the orchestrator agent, its fleet and its workspace.
The former defaults ("Concierge", "Alfred") are still known, so a row seeded
under one of them is renamed at the next seed and a hand-renamed row is kept.

It is never written into a system prompt (invariant 16): the personas stay as
seeded, and what a model reads of the platform's Concierge is unchanged.

The name is an administrator's text, so every destination treats it as data:
React renders it as text, never as a message id; logs format it with ``%r``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.concierge.settings import ConciergeSettings, load_concierge_settings

logger = logging.getLogger(__name__)

#: The name in every platform mode.
DEFAULT_NAME: Final = "Platform Health"
#: The defaults of the former Concierge ("Alfred" was the copilot mode's).
_FORMER_DEFAULT_NAMES: Final = frozenset({"Concierge", "Alfred"})
#: Every name the platform gives or gave by itself: a row still carrying one of them was never renamed by hand.
DEFAULT_NAMES: Final[frozenset[str]] = frozenset({DEFAULT_NAME, *_FORMER_DEFAULT_NAMES})


def default_concierge_name() -> str:
    """The default name, the same in every platform mode."""
    return DEFAULT_NAME


def concierge_name(value: ConciergeSettings) -> str:
    """The name *value* gives, its default when it gives none."""
    return value.name or default_concierge_name()


async def load_concierge_name(db: AsyncSession) -> str:
    return concierge_name(await load_concierge_settings(db))


async def rename_concierge(db: AsyncSession, *, previous: Collection[str], name: str) -> int:
    """Give the orchestrator, its fleet and its workspace *name* where they still carry one of *previous*.

    A row an administrator renamed by hand carries another name and is left
    alone. Flushed, not committed; the number of rows renamed.
    """
    from app.models.agent import Agent
    from app.models.fleet import Fleet
    from app.models.workspace import Workspace
    from app.services.concierge.fleet_seed import ORCHESTRATOR_SLUG

    stale = set(previous) - {name}
    if not stale:
        return 0
    orchestrator = (
        await db.execute(
            select(Agent).where(Agent.slug == ORCHESTRATOR_SLUG, Agent.is_company_managed.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    if orchestrator is None:
        return 0
    rows: list[Agent | Fleet | Workspace] = [orchestrator]
    fleet = (
        await db.execute(
            select(Fleet)
            .where(Fleet.orchestrator_agent_id == orchestrator.id, Fleet.is_company_managed.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if fleet is not None:
        rows.append(fleet)
        workspace = await db.get(Workspace, fleet.workspace_id)
        if workspace is not None and workspace.is_company_managed:
            rows.append(workspace)
    renamed = 0
    for row in rows:
        if row.name in stale:
            row.name = name
            renamed += 1
    if renamed:
        await db.flush()
        logger.info("The Concierge is now named %r (%d row(s) renamed)", name, renamed)
    return renamed
