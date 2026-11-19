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

"""The Platform Health fleet is built in and read-only for everyone (#101).

Its five agents, their assignments, its fleet and its workspace ship with the
platform: the seed (``fleet_seed.seed_health_fleet``) rewrites their whole
definition on every boot, and nobody, administrators included, in any mode,
edits, toggles, deletes, duplicates or forks them. What an administrator tunes
lives in Settings > Platform Health (checks, thresholds, schedule, spend
ceiling, alerts), never on the fleet's rows.

A fleet agent is known by its ``health_role``, a column only the seed writes
(``identity.set_health_role``). The manage rule (``helpers.can_manage_as``)
and the write gates (``helpers.writable_agent_error``,
``helpers.writable_assignment_error``, ``fleets.ensure_builtin_fleet_writable``)
read it through this module, so a REST route, an agent tool and the MCP server
refuse alike, with one reason.
"""

from __future__ import annotations

from typing import Final

#: The one reason every refused write on the fleet carries.
HEALTH_READ_ONLY_REFUSAL: Final = "Platform Health is built in and read-only."

#: The fleet's slug, written by the seed on every boot, never by a person.
HEALTH_FLEET_SLUG: Final = "platform-health"


def is_health_fleet(fleet: object) -> bool:
    """Whether *fleet* is the built-in Platform Health fleet."""
    return bool(getattr(fleet, "is_builtin", False)) and getattr(fleet, "slug", None) == HEALTH_FLEET_SLUG


def is_health_agent(entity: object) -> bool:
    """Whether *entity* is an agent of the Platform Health fleet (it holds a fleet role)."""
    return getattr(entity, "health_role", None) is not None
