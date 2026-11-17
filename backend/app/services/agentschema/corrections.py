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

"""Deterministic corrections derivable from a checked import bundle.

The mapping layer imports what it can and *keeps* what it cannot execute —
function tools arrive as stubs with no body, MCP connections as unconfigured
placeholders — so the agent's instructions keep referring to things that
exist, and the warnings say what needs finishing. Some users would rather
import without those placeholders at all. This module derives that choice as
explicit, individually-selectable corrections:

- :func:`derive_corrections` inspects a native-shape bundle and lists what
  *could* be dropped, with a stable self-describing id per entry.
- :func:`apply_corrections` applies a set of those ids to a bundle, returning
  a new bundle. Ids are self-describing (``kind:subject``) so the flow stays
  stateless between the check call and the confirming import.

Only structurally-identifiable placeholders are offered: a custom tool with an
empty implementation or an MCP server with no address, both carrying the
``agentschema-import`` tag the mapping layer stamps on what it reconstructs.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from app.services.agentschema.spec import AgentSchemaError

#: Tag the mapping layer puts on collections it reconstructs from AgentSchema
#: tool entries (see ``mapping._tools_to_bundle``).
AGENTSCHEMA_IMPORT_TAG = "agentschema-import"

DROP_CUSTOM_TOOL = "drop_custom_tool"
DROP_MCP_SERVER = "drop_mcp_server"


@dataclass(slots=True)
class ImportCorrection:
    """One optional, deterministic fix the user can apply before importing.

    The id addresses the entry by its **index** in its bundle collection
    (``"<kind>:<index>"``), not by name: an uploaded document can carry two
    placeholders with the same name, and a name-keyed id would collapse them
    into one correction that silently drops both. Indexes are stable because
    the confirming call replays the exact bundle the check returned.
    """

    #: Stable self-describing id: ``"<kind>:<index>"``.
    id: str
    kind: str
    #: The tool/server name the correction acts on — display only.
    subject: str


def _is_stub_tool(tool: Any) -> bool:
    if not isinstance(tool, dict):
        return False
    tags = tool.get("tags")
    has_tag = isinstance(tags, list) and AGENTSCHEMA_IMPORT_TAG in tags
    return has_tag and not str(tool.get("js_function") or "").strip()


def _is_placeholder_server(server: Any) -> bool:
    if not isinstance(server, dict):
        return False
    tags = server.get("tags")
    has_tag = isinstance(tags, list) and AGENTSCHEMA_IMPORT_TAG in tags
    return has_tag and not server.get("command") and not server.get("url")


def derive_corrections(bundle: dict[str, Any]) -> list[ImportCorrection]:
    """List the placeholder entries of ``bundle`` that could be dropped."""
    corrections: list[ImportCorrection] = []

    tools = bundle.get("custom_tools")
    if isinstance(tools, list):
        for index, tool in enumerate(tools):
            if _is_stub_tool(tool):
                name = str(tool.get("name") or "").strip()
                # A nameless stub has no readable copy ("remove the tool ''"
                # is not a sentence) — leave it to the warnings.
                if not name:
                    continue
                corrections.append(
                    ImportCorrection(
                        id=f"{DROP_CUSTOM_TOOL}:{index}",
                        kind=DROP_CUSTOM_TOOL,
                        subject=name,
                    )
                )

    servers = bundle.get("mcp_servers")
    if isinstance(servers, list):
        for index, server in enumerate(servers):
            if _is_placeholder_server(server):
                name = str(server.get("name") or "").strip()
                if not name:
                    continue
                corrections.append(
                    ImportCorrection(
                        id=f"{DROP_MCP_SERVER}:{index}",
                        kind=DROP_MCP_SERVER,
                        subject=name,
                    )
                )

    return corrections


def apply_corrections(
    bundle: dict[str, Any], correction_ids: list[str]
) -> dict[str, Any]:
    """Return a copy of ``bundle`` with the selected corrections applied.

    Every id must match a correction :func:`derive_corrections` would offer on
    this very bundle — anything else raises :class:`AgentSchemaError`, so a
    client cannot use the correction mechanism to silently drop arbitrary
    (implemented) tools while claiming a checked import.

    Always returns a fresh copy, selected corrections or not — one aliasing
    contract, so no caller can end up mutating the input through the result.
    """
    corrected = copy.deepcopy(bundle)
    if not correction_ids:
        return corrected

    offered = {c.id: c for c in derive_corrections(bundle)}
    unknown = [cid for cid in correction_ids if cid not in offered]
    if unknown:
        raise AgentSchemaError(
            "Unknown import correction(s): " + ", ".join(sorted(unknown)) + ". "
            "Corrections must come from the check endpoint's report for this "
            "same file."
        )

    def _indexes(kind: str) -> set[int]:
        return {
            int(offered[cid].id.rsplit(":", 1)[1])
            for cid in correction_ids
            if offered[cid].kind == kind
        }

    dropped_tools = _indexes(DROP_CUSTOM_TOOL)
    dropped_servers = _indexes(DROP_MCP_SERVER)

    if dropped_tools and isinstance(corrected.get("custom_tools"), list):
        corrected["custom_tools"] = [
            tool
            for index, tool in enumerate(corrected["custom_tools"])
            if index not in dropped_tools
        ]
    if dropped_servers and isinstance(corrected.get("mcp_servers"), list):
        corrected["mcp_servers"] = [
            server
            for index, server in enumerate(corrected["mcp_servers"])
            if index not in dropped_servers
        ]
    return corrected
