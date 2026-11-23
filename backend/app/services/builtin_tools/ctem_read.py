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

"""The ``ctem_read`` built-in group (#1337): read the CTEM signals and cases from any agent.

The signal and case tools (``ctem_signals.py``, ``ctem_cases.py``) are wired
only for the built-in CTEM agents that work the loop. This group offers their
three reads to any agent it is bound to, read-only, with the same rules: it
acts for the person the turn or run acts for, signals are that person's own
unless the administrator reach applies (``agent_tool_admin_bypass``), and a
case renders through that person's own source read (``ctem_cases_view``), so
a case they see none of does not exist. No write function is in the group.
XTM One mode only.

``get_ctem_case`` here also answers who opened the case (``opened_by``, the
actor of its ``created`` event) and why each empty section is empty
(``empty_sections``), so an agent reports a blank as a fact, not a guess.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

CTEM_READ_GROUP = "ctem_read"
CTEM_READ_TOOL_NAMES: tuple[str, ...] = ("list_ctem_signals", "list_ctem_cases", "get_ctem_case")
_SOURCE = f"builtin:{CTEM_READ_GROUP}"
#: History events one ``get_ctem_case`` answer carries (as the case tools).
_HISTORY_PAGE = 20


def offers_ctem_read_tools(builtin_tool_ids: Any) -> bool:
    """Whether a turn or run wires the group: bound to the agent, XTM One mode only."""
    from app.core.config import settings

    if settings.is_copilot_like:
        return False
    return CTEM_READ_GROUP in (builtin_tool_ids or ())


def ctem_read_tools_for(
    agent: Any, builtin_tool_ids: Any, *, user_id: str | None
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """The group's tools for one turn or run, ``None`` when not wired.

    Wired when the group is bound and a person is acted for. A built-in CTEM
    agent already carrying the signal or case tools keeps its own, and this
    group adds only the reads it lacks, never the same function twice.
    """
    from .ctem_cases import offers_ctem_case_tools
    from .ctem_signals import offers_ctem_signal_tools

    if not user_id or not offers_ctem_read_tools(builtin_tool_ids):
        return None
    slug, builtin = getattr(agent, "slug", None), bool(getattr(agent, "is_builtin", False))
    carried: set[str] = set()
    if offers_ctem_signal_tools(slug, is_builtin=builtin):
        carried.add("list_ctem_signals")
    if offers_ctem_case_tools(slug, is_builtin=builtin):
        carried.update({"list_ctem_cases", "get_ctem_case"})
    if carried >= set(CTEM_READ_TOOL_NAMES):
        return None
    defs, executor = build_ctem_read_tools(user_id=user_id, agent_id=str(getattr(agent, "id", "") or ""))
    for name in carried:
        executor.unregister(name)
    return [d for d in defs if d.name not in carried], executor


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def opened_by(events: list[Any]) -> dict[str, Any] | None:
    """Who opened the case: the actor and time of its oldest ``created`` event, ``None`` without one."""
    created = [e for e in events if getattr(e, "kind", None) == "created" and getattr(e, "at", None) is not None]
    if not created:
        return None
    first = min(created, key=lambda e: e.at)
    return {"actor": first.actor, "at": first.at.isoformat(), "how": first.to_value}


def empty_sections(rendered: dict[str, Any], running_versions: Any) -> dict[str, str]:
    """Why each empty section of a rendered case is empty, keyed by the section."""
    reasons: dict[str, str] = {}
    if not rendered.get("exploitation"):
        reasons["exploitation"] = "no exploitation reading recorded on this case yet"
    if rendered.get("attack_path") is None:
        reasons["attack_path"] = "the attack-path reach of this case has not been read yet"
    if rendered.get("treatment_plan") is None:
        reasons["treatment_plan"] = "no treatment plan attached to this case"
    if rendered.get("threat_package") is None:
        reasons["threat_package"] = "no threat package drafted for this case"
    if not rendered.get("tickets"):
        reasons["tickets"] = "no ticket recorded on this case"
    if not rendered.get("validations"):
        reasons["validations"] = "no validation recorded on this case"
    if running_versions is None:
        reasons["running_versions"] = (
            "no running version read for this upgrade"
            if rendered.get("remediation_kind") == "upgrade"
            else "only an upgrade case has running versions"
        )
    if not rendered.get("history"):
        reasons["history"] = "no event recorded on this case"
    if rendered.get("details_unavailable"):
        reasons["issues"] = "these source products did not answer for you: " + ", ".join(
            rendered["details_unavailable"]
        )
    return reasons


def build_ctem_read_tools(*, user_id: str, agent_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The three read tools, acting for *user_id*; nothing is written."""
    from .ctem_cases import _uuid, build_ctem_case_tools
    from .ctem_signals import build_ctem_signal_tools

    signal_defs, signal_exec = build_ctem_signal_tools(user_id=user_id)
    case_defs, case_exec = build_ctem_case_tools(user_id=user_id, agent_id=agent_id)
    scope = _uuid(user_id)

    async def _get(case_id: str = "") -> str:
        from app.core.database import async_session
        from app.models.user import User
        from app.services import ctem_case_versions as case_versions
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of
        from app.services.ctem_verticals import normalize_vertical
        from app.services.organizations import get_primary

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        if scope is None:
            return f"No case {case_id}."
        try:
            async with async_session() as db:
                primary = await get_primary(db)
                if primary is None:
                    return f"No case {case_id}."
                user = await db.get(User, scope)
                vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
                found = await store.get_case(db, wanted, organization_id=primary.id)
                if found is None:
                    return f"No case {case_id}."
                case, rows, events = found
                view = await view_of.viewer_view(db, str(scope), vertical)
                rendered = view_of.hydrate(case, rows, events[:_HISTORY_PAGE], view)
                if rendered is None:
                    return f"No case {case_id}."
                running = await case_versions.for_case(db, case)
                from .ctem_signals import case_signal_scope

                return _dump(
                    {
                        **rendered,
                        "running_versions": running,
                        "opened_by": opened_by(events),
                        "history_total": len(events),
                        "empty_sections": empty_sections(rendered, running),
                        "scope": await case_signal_scope(db, scope, view.signals, case_id=case.id),
                    }
                )
        except Exception:
            logger.warning("CTEM case read failed", exc_info=True)
            return "Error: the case could not be read right now. Say so; do not guess it."

    executor = CallableToolExecutor()
    executor.register("list_ctem_signals", signal_exec.handler("list_ctem_signals"))  # type: ignore[arg-type]
    executor.register("list_ctem_cases", case_exec.handler("list_ctem_cases"))  # type: ignore[arg-type]
    executor.register("get_ctem_case", _get)

    by_name = {d.name: d for d in (*signal_defs, *case_defs)}
    definitions = [
        dataclasses.replace(by_name["list_ctem_signals"], source=_SOURCE),
        dataclasses.replace(by_name["list_ctem_cases"], source=_SOURCE),
        dataclasses.replace(
            by_name["get_ctem_case"],
            description=(
                "Read one CTEM case: its fix, member issues (source product and id), assets, crown jewels, "
                "latest history, who opened it, and why each empty section is empty."
            ),
            source=_SOURCE,
        ),
    ]
    return definitions, executor


__all__ = [
    "CTEM_READ_GROUP",
    "CTEM_READ_TOOL_NAMES",
    "build_ctem_read_tools",
    "ctem_read_tools_for",
    "empty_sections",
    "offers_ctem_read_tools",
    "opened_by",
]
