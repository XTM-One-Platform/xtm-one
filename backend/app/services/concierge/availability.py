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

"""Is the Platform Health capability on? (#8254, dev-docs/platform-health.md)

The capability is built in: :func:`concierge_enabled` answers ``True`` on
every deployment since the ``CONCIERGE`` feature flag was removed (slice S1 of
the Platform Health plan). :func:`concierge_active` is the administrator's
"enabled" in the settings, the switch that starts and stops its work; no agent
or fleet is checked until it is enrolled (``consent``).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


def concierge_enabled() -> bool:
    """Always ``True``: the capability is built in (the former ``CONCIERGE`` flag)."""
    return True


async def concierge_active(db: AsyncSession) -> bool:
    """The administrator's "enabled" in the settings: may the work start?"""
    from app.services.concierge.settings import load_concierge_settings

    return (await load_concierge_settings(db)).enabled
