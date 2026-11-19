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

An administrator may still name it in Settings > Agents and automations > Platform Health
(``HealthSettings.name``) until the S1 migration drops the setting; left
empty, the name is "Platform Health" in every platform mode (#101, decided
18 Nov 2026: no mode-specific name). The name is what people read: the
Health page, the inbox, the entry points and the refusals the REST API
sends. The fleet's own rows (orchestrator, fleet, workspace)
are built in and always carry "Platform Health": the seed rewrites them on
every boot, a row seeded under a former default ("Concierge", "Alfred")
included (``read_only``).

It is never written into a system prompt (invariant 16): the personas stay as
seeded, and what a model reads of the platform's Platform Health is unchanged.

The name is an administrator's text, so every destination treats it as data:
React renders it as text, never as a message id; logs format it with ``%r``.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.health.settings import HealthSettings, load_health_settings

#: The name in every platform mode.
DEFAULT_NAME: Final = "Platform Health"
#: The defaults of the former Concierge ("Alfred" was the copilot mode's).
_FORMER_DEFAULT_NAMES: Final = frozenset({"Concierge", "Alfred"})
#: Every name the platform gives or gave by itself: a row still carrying one of them was never renamed by hand.
DEFAULT_NAMES: Final[frozenset[str]] = frozenset({DEFAULT_NAME, *_FORMER_DEFAULT_NAMES})


def default_health_agent_name() -> str:
    """The default name, the same in every platform mode."""
    return DEFAULT_NAME


def health_agent_name(value: HealthSettings) -> str:
    """The name *value* gives, its default when it gives none."""
    return value.name or default_health_agent_name()


async def load_health_agent_name(db: AsyncSession) -> str:
    return health_agent_name(await load_health_settings(db))
