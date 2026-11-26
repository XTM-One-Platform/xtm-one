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

"""An agent's tool approval policy as get_agent reads it (#2189).

Read-only lines for the "Human-in-the-loop:" block: the agent's own
"Ask before running tools" setting, what applies once the platform switch,
the install default and the seeded-fleet rule (#1675) are resolved, and the
administrator's platform tool rules. Every value comes from the resolvers the
gate itself uses (``services/hitl_whitelist.py``), so the read never disagrees
with the gate. A failed read says so; it never guesses.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

UNREADABLE = "unreadable (read failed)"

#: Tool keys named one by one on the rules line; the rest are counted.
MAX_NAMED_RULES = 20


def own_setting_label(own: bool | None, install_default_asks: bool | None) -> str:
    """The agent's own switch, with what the install default says when it has none."""
    if own is True:
        return "Ask"
    if own is False:
        return "Don't ask"
    if install_default_asks is None:
        return f"Platform default ({UNREADABLE})"
    return "Platform default (" + ("asks" if install_default_asks else "does not ask") + ")"


def effective_label(*, platform_off: bool | None, seeded_member: bool, gate_on: bool | None) -> str:
    """What happens to a gated tool call of this agent, resolved as the gate resolves it."""
    if platform_off is True:
        return "never pauses: tool approvals are off platform-wide"
    if seeded_member:
        return "never pauses: built-in member of a seeded fleet, its gates are decision items (#1675)"
    if gate_on is None:
        return UNREADABLE
    if gate_on:
        return "asks before a tool off the platform's allowed list (standing approvals of the person apply)"
    return "runs every tool without asking"


def rules_label(overrides: dict[str, str] | None) -> str:
    """The administrator's platform tool rules: tools added to and removed from the allowed list."""
    if overrides is None:
        return UNREADABLE
    from app.services.hitl_whitelist import ACTION_ADD, ACTION_REMOVE

    added = sorted(k for k, a in overrides.items() if a == ACTION_ADD)
    removed = sorted(k for k, a in overrides.items() if a == ACTION_REMOVE)
    if not added and not removed:
        return "None (the shipped default applies)"

    def _named(keys: list[str]) -> str:
        shown = ", ".join(f"`{k}`" for k in keys[:MAX_NAMED_RULES])
        more = len(keys) - MAX_NAMED_RULES
        return shown + (f", +{more} more" if more > 0 else "")

    parts = []
    if added:
        parts.append(f"run without asking: {_named(added)}")
    if removed:
        parts.append(f"always ask: {_named(removed)}")
    return "; ".join(parts)


async def approval_policy_lines(db: AsyncSession, agent: Any, *, caller_is_admin: bool) -> list[str]:
    """The three lines get_agent prints under "Human-in-the-loop:".

    The platform rules line is read for an administrator only, as on the
    Tool approvals settings page; any other caller is told where it lives.
    """
    from app.models.platform_settings import PlatformSettings
    from app.services.hitl_whitelist import (
        install_default_asks,
        load_overrides,
        resolve_approve_tool_calls,
        seeded_fleet_member,
    )

    own = getattr(agent, "approve_tool_calls", None)
    default_asks: bool | None
    platform_off: bool | None
    # Savepoints, so a failed read never rolls back the caller's session
    # (the agent row get_agent is printing stays loaded).
    try:
        async with db.begin_nested():
            row = (
                await db.execute(
                    select(PlatformSettings.approve_tool_calls_default, PlatformSettings.tool_approvals_enabled)
                )
            ).first()
        # No settings row: the defaults (install default off, approvals on).
        default_asks = install_default_asks(row[0] if row else None)
        platform_off = row is not None and row[1] is False
    except Exception:
        logger.exception("get_agent: could not read the platform approval settings")
        default_asks = None
        platform_off = None

    seeded = await seeded_fleet_member(db, agent)
    gate_on: bool | None
    try:
        gate_on = await resolve_approve_tool_calls(db, agent)
    except Exception:
        logger.exception("get_agent: could not resolve the agent's approval gate")
        gate_on = None

    if caller_is_admin:
        overrides: dict[str, str] | None
        try:
            async with db.begin_nested():
                overrides = await load_overrides(db)
        except Exception:
            logger.exception("get_agent: could not read the platform tool rules")
            overrides = None
        rules = rules_label(overrides)
    else:
        rules = "administrators only (Settings > Access and security > Tool approvals)"

    return [
        f"  • Ask before running tools: {own_setting_label(own, default_asks)}",
        f"  • Tool approvals (effective): {effective_label(platform_off=platform_off, seeded_member=seeded, gate_on=gate_on)}",
        f"  • Platform tool rules (every agent): {rules}",
    ]
