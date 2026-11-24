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

"""When a change to an agent's tools reaches a run, stated as a fact.

A run builds its tool list, the names the model calls and the gate at the
start of a turn (``run_agent_loop``). An integration change written during the
turn (its spec, credentials or sharing) reloads the integration tools of the
turn that made it at the end of that step (#1874); an agent's bindings or tool
scoping are read by the next turn of a chat and the next run of an
assignment. The write tools that make such a change say so in their answer,
so the model never waits for a tool that cannot appear in the turn it is in.
"""

from __future__ import annotations

from collections.abc import Iterable

#: ``update_integration`` answer labels that leave the tools as they were.
INTEGRATION_FIELDS_WITHOUT_TOOL_EFFECT = frozenset({"name", "description", "tags", "scopes"})

#: ``update_agent`` fields that change which tools an agent's run holds.
AGENT_TOOL_FIELDS = frozenset(
    {
        "integration_bindings",
        "integration_read_only",
        "integration_live_writes",
        "integration_template_bindings",
        "integration_template_read_only",
        "integration_element_enforcement",
        "integration_element_bindings",
        "integration_allowed_tools",
        "mcp_allowed_tools",
        "builtin_tool_ids",
        "builtin_allowed_tools",
        "tool_deny_list",
        "mcp_server_ids",
        "custom_tool_ids",
        "knowledge_base_ids",
        "knowledge_base_permissions",
        "skill_ids",
        "handover_agent_ids",
        "handover_types",
        "handover_conditions",
    }
)

NEXT_TURN_LINE = (
    "\n  \u2022 Tools: a turn keeps the tool list it started with; this change "
    "reaches an agent's tools from its next chat turn or its next assignment run."
)


INTEGRATION_RELOAD_LINE = (
    "\n  \u2022 Tools: a turn bound to this integration reloads its tools after this step; "
    "any other agent reads the change from its next chat turn or its next assignment run."
)


def integration_change_line(updated: Iterable[str]) -> str:
    """The reload line for an integration update that can move its tools, else ``""``."""
    return INTEGRATION_RELOAD_LINE if any(f not in INTEGRATION_FIELDS_WITHOUT_TOOL_EFFECT for f in updated) else ""


def agent_change_line(updated: Iterable[str]) -> str:
    """The next-turn line for an agent update that changes its tool scope, else ``""``."""
    return NEXT_TURN_LINE if any(f in AGENT_TOOL_FIELDS for f in updated) else ""
