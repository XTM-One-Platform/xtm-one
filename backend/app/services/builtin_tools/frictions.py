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

"""Coordination tools of the friction ledger: record, list and dispose of a run's frictions.

``record_friction`` is the agent's own observation (kind ``manual``);
``list_frictions`` reads what the platform and the agent recorded;
``dispose_friction`` classifies one and names what carries its fix. The
store and the automatic records live in ``services/frictions.py``.

No approval of any kind: these tools write only the caller's own ledger rows.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from app.core.utils import format_exception_message
from app.models.agent_friction import FRICTION_CLASSES, FRICTION_DISPOSITIONS, FRICTION_KINDS
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_CLASSES = [c for c in FRICTION_CLASSES if c != "unclassified"]


def _line(f: dict, *, with_run: bool = False) -> str:
    head = f"- `{f['id']}` {f['kind']}"
    if with_run and f.get("run_id"):
        # Listed across runs: the run a friction belongs to, for get_run or a trace read.
        head += f" in run {f['run_id']}"
    if f.get("tool"):
        head += f" on {f['tool']}"
    if (f.get("count") or 1) > 1:
        head += f" (x{f['count']})"
    disposition = f.get("disposition")
    head += (
        f", {f['class']} -> {disposition['kind']}" + (f" {disposition['ref']}" if disposition.get("ref") else "")
        if disposition
        else ", undisposed"
    )
    evidence = (f.get("evidence") or "").replace("\n", " ")
    if len(evidence) > 200:
        evidence = evidence[:199] + "…"
    return f"{head}\n  {evidence}" if evidence else head


def build_friction_tools(
    user_id: str,
    agent_id: str,
    *,
    run_id: str | uuid.UUID | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The three ledger tools for *agent_id* acting for *user_id*.

    A friction recorded with ``record_friction`` goes under the run's bound
    scope when there is one (the run, its assignment and work item), else
    under *run_id* (a chat turn of a run), else under the agent alone.
    """

    def _scope():
        from app.services.frictions import current_friction_scope, make_scope

        return current_friction_scope() or make_scope(agent_id, user_id=user_id or None, run_id=run_id)

    async def _record_friction(what: str, where: str = "", evidence: str = "") -> str:
        from app.services.frictions import record_friction

        what = (what or "").strip()
        if not what:
            return "Error: what is required: one line saying what the run struggled with."
        scope = _scope()
        if scope is None:
            return "Error: no agent to record the friction for."
        text = what if not evidence.strip() else f"{what}\n{evidence.strip()}"
        try:
            friction, created = await record_friction(scope, kind="manual", tool=(where or "").strip(), evidence=text)
        except Exception as exc:
            logger.error("record_friction failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the friction was not recorded. Please try again."
        verb = "Recorded" if created else f"Already recorded in this run, now x{friction['count']}"
        return (
            f"{verb}: friction `{friction['id']}` (manual{', ' + friction['tool'] if friction['tool'] else ''}). "
            "Dispose of it with dispose_friction once its fix is filed."
        )

    async def _list_frictions(
        run_id: str = "",
        agent_id: str = "",
        undisposed: bool = False,
        since_hours: int = 0,
        limit: int = 50,
        offset: int = 0,
        kind: str = "",
    ) -> str:
        from app.services.frictions import list_frictions

        kinds = [k.strip() for k in (kind or "").split(",") if k.strip()]
        unknown = [k for k in kinds if k not in FRICTION_KINDS]
        if unknown:
            return f"Error: unknown kind {', '.join(unknown)}; kinds: {', '.join(FRICTION_KINDS)}. Nothing was listed."

        scope = _scope()
        target_run = (run_id or "").strip() or None
        target_agent = (agent_id or "").strip() or None
        if target_run is None and target_agent is None:
            # Default: this run's frictions, else this agent's.
            if scope is not None and scope.run_id is not None:
                target_run = str(scope.run_id)
            elif scope is not None:
                target_agent = str(scope.agent_id)
        since = datetime.now(UTC) - timedelta(hours=int(since_hours)) if since_hours and since_hours > 0 else None
        try:
            rows, total = await list_frictions(
                user_id=user_id or None,
                agent_id=target_agent,
                run_id=target_run,
                undisposed=undisposed,
                since=since,
                limit=limit,
                offset=offset,
                kinds=kinds or None,
            )
        except ValueError as exc:
            # An id prefix starting several or none, or a value that is no id.
            return f"Error: {exc}. Nothing was listed."
        except Exception as exc:
            logger.error("list_frictions failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to list the frictions. Please try again."
        what = f"run {target_run}" if target_run else f"agent {target_agent}"
        if kinds:
            what += f" of kind {', '.join(kinds)}"
        if not rows:
            return f"No {'undisposed ' if undisposed else ''}friction recorded for {what}."
        shown_to = int(offset or 0) + len(rows)
        lines = [f"{total} friction(s) for {what}{' (undisposed)' if undisposed else ''}, showing {len(rows)}:"]
        lines.extend(_line(r, with_run=target_run is None) for r in rows)
        if shown_to < total:
            lines.append(f"More: call again with offset={shown_to}.")
        return "\n".join(lines)

    async def _dispose_friction(id: str, friction_class: str, disposition: str, ref: str = "") -> str:
        from app.services.frictions import FrictionNotFoundError, dispose_friction

        try:
            friction = await dispose_friction(
                id,
                agent_id=agent_id,
                user_id=user_id or None,
                friction_class=(friction_class or "").strip(),
                disposition=(disposition or "").strip(),
                ref=ref,
            )
        except FrictionNotFoundError:
            return f"Error: no friction `{id}` of yours: list them with list_frictions."
        except ValueError as exc:
            return f"Error: {exc}. Nothing was changed."
        except Exception as exc:
            logger.error("dispose_friction failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the friction was not disposed of. Please try again."
        return f"Disposed of friction `{friction['id']}`:\n{_line(friction)}"

    executor = CallableToolExecutor()
    executor.register("record_friction", _record_friction)
    executor.register("list_frictions", _list_frictions)
    executor.register("dispose_friction", _dispose_friction)

    definitions = [
        ToolDefinition(
            name="record_friction",
            description=(
                "Record a friction of this run: what a better tool, skill, prompt or document would "
                "have spared (a tool missing or wrong, a rule applied from memory, a step a person "
                "pointed out, a large read for one fact). Tool errors are recorded by the platform. "
                "Repeats in a run are counted. Then file its fix and call dispose_friction. A call "
                "meant to fail takes expect_error (true or a reason): its error is recorded disposed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "One line: what the run struggled with"},
                    "where": {"type": "string", "description": "Tool, skill section, prompt or object (optional)"},
                    "evidence": {"type": "string", "description": "Error, call or observation (max 2,000 chars)"},
                },
                "required": ["what"],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="list_frictions",
            description=(
                "List frictions, newest first (default: this run's), with count, class and "
                "disposition. "
                f"Kinds: {', '.join(FRICTION_KINDS)}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "A run's id or prefix (default: this run)"},
                    "agent_id": {"type": "string", "description": "An agent's id or prefix, across runs"},
                    "kind": {"type": "string", "description": "Kinds, comma-separated"},
                    "undisposed": {"type": "boolean", "description": "No disposition yet"},
                    "since_hours": {"type": "integer", "description": "Seen in the last N hours"},
                    "limit": {"type": "integer", "description": "Page size (default 50, max 100)"},
                    "offset": {"type": "integer", "description": "Frictions to skip (the next offset named)"},
                },
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="dispose_friction",
            description=(
                "Classify a friction and name what carries its fix: class harness (a tool or platform "
                "fix), procedure (a skill), persona_prompt_trigger, knowledge, or platform_acceptable; "
                "disposition work_item (ref = the id or 8+ char prefix of the item with the fix, checked), "
                "shared_state (ref = the key), skill_edit or prompt_edit (ref = the object and section), "
                "or accepted (no ref)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The friction's id or prefix, from list_frictions or record_friction",
                    },
                    "friction_class": {"type": "string", "enum": _CLASSES},
                    "disposition": {"type": "string", "enum": list(FRICTION_DISPOSITIONS)},
                    "ref": {"type": "string", "description": "What carries the fix (required unless accepted)"},
                },
                "required": ["id", "friction_class", "disposition"],
            },
            source="builtin:coordination",
        ),
    ]
    return definitions, executor
