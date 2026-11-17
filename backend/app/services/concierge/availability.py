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

"""Is the Platform Concierge on? (#8254)

Two answers, kept apart: :func:`concierge_enabled` is the ``CONCIERGE``
feature flag, the deployment's switch (what the evaluation engine gate reads);
:func:`concierge_active` adds the administrator's "enabled" in Settings >
Agents and automations > Concierge, the switch that starts and stops its work.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.feature_flags import is_feature_enabled

FLAG = "CONCIERGE"


def concierge_enabled() -> bool:
    """The ``CONCIERGE`` feature flag."""
    return is_feature_enabled(FLAG)


async def concierge_active(db: AsyncSession) -> bool:
    """The flag AND Settings > Agents and automations > Concierge "enabled": may Concierge work start?"""
    if not concierge_enabled():
        return False
    from app.services.concierge.settings import load_concierge_settings

    return (await load_concierge_settings(db)).enabled

