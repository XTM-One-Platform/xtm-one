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

"""CTEM signal tools (#826): record and list the changes in exposure that start the loop.

``record_ctem_signal`` and ``list_ctem_signals`` act for the person the turn
or run acts for, through ``services/ctem_signals.py`` (the API's rules and
shapes). XTM One mode only. Granted to the built-in CTEM agents of
:data:`CTEM_SIGNAL_AGENT_SLUGS`: the orchestrators and analysts that see a
trigger first; the seeded fleets are rewired to use them in a later step of
the loop (``dev-docs/ctem-signals.md`` section 6).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.models.ctem_signal import LENSES, SIGNAL_STATES, SUBJECT_KINDS, TRIGGER_PATHS
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

CTEM_SIGNAL_TOOL_NAMES: tuple[str, ...] = ("record_ctem_signal", "list_ctem_signals")
_SOURCE = "builtin:ctem_signals"
#: The built-in agents that carry the tools.
CTEM_SIGNAL_AGENT_SLUGS: frozenset[str] = frozenset(
    {
        "ctem-assistant",
        "ctem-discovery-analyst",
        "ctem-vuln-prioritization-orchestrator",
        "ctem-vuln-triage-analyst",
        "ctem-threat-response-orchestrator",
        "ctem-threat-relevance-analyst",
    }
)


def offers_ctem_signal_tools(agent_slug: str | None, *, is_builtin: bool) -> bool:
    """Whether a turn or run of this agent is offered the signal tools (XTM One mode only)."""
    from app.core.config import settings

    if settings.is_copilot_like:
        return False
    return bool(is_builtin and (agent_slug or "") in CTEM_SIGNAL_AGENT_SLUGS)


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_ctem_signal_tools(*, user_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The two signal tools, acting for *user_id*: an administrator lists the organization's signals.

    Whether the user's agent takes the administrator's reach is read at each list,
    through ``agent_tool_admin_bypass``.
    """
    try:
        scope: uuid.UUID | None = uuid.UUID(str(user_id))
    except ValueError:
        scope = None

    async def _record(
        trigger_path: str = "",
        source_provider: str = "",
        subject_kind: str = "",
        subject_ref: str = "",
        lens: str = "",
        source_external_id: str = "",
        subject_label: str = "",
        observed_at: str = "",
        evidence: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_signals as signals
        from app.services.organizations import get_primary

        if scope is None:
            return "No user: no signal can be recorded."
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")) if observed_at else None
        except ValueError:
            return "observed_at must be an ISO-8601 date and time."
        try:
            async with async_session() as db:
                primary = await get_primary(db)
                if primary is None:
                    return "No organization is set up yet: no signal can be recorded."
                row, created = await signals.record_signal(
                    db,
                    organization_id=primary.id,
                    signal=signals.SignalInput(
                        trigger_path=trigger_path,
                        lens=lens,
                        source_provider=source_provider,
                        source_external_id=source_external_id or None,
                        subject_kind=subject_kind,
                        subject_ref=subject_ref,
                        subject_label=subject_label or None,
                        observed_at=observed,
                        evidence=evidence if isinstance(evidence, dict) else {},
                        dedupe_key=dedupe_key or None,
                    ),
                    recorded_via="tool",
                    recorded_by=str(scope),
                )
                from app.services import ctem_scope_gate_store

                from app.services import ctem_case_loop

                pending = await ctem_scope_gate_store.evaluate_after_record(db, row, created=created)
                await db.commit()
                await ctem_case_loop.publish_pending(pending)
                return _dump({"created": created, "signal": signals.to_dict(row)})
        except signals.SignalError as exc:
            return f"Refused: {exc}. Nothing was recorded."
        except Exception:
            logger.warning("CTEM signal record failed", exc_info=True)
            return "Error: the signal could not be recorded right now. Say so; nothing was recorded."

    async def _list(state: str = "", trigger_path: str = "", lens: str = "", limit: int = 20, offset: int = 0) -> str:
        from app.core.database import async_session
        from app.services import ctem_signals as signals
        from app.services.organizations import get_primary

        if scope is None:
            return "No user: there are no signals to read."
        try:
            async with async_session() as db:
                primary = await get_primary(db)
                if primary is None:
                    return "No organization is set up yet: there are no signals."
                from app.api.v1.helpers import agent_tool_admin_bypass
                from app.models.user import User

                # The admin bypass of an agent tool goes through the helper (strict admin
                # access for agent tools and Platform Health reach decide it).
                bypass = agent_tool_admin_bypass(await db.get(User, scope))
                rows, total, counts = await signals.list_signals(
                    db,
                    organization_id=primary.id,
                    recorded_by=None if bypass else str(scope),
                    state=state or None,
                    trigger_path=trigger_path or None,
                    lens=lens or None,
                    limit=max(1, min(int(limit or 20), 50)),
                    offset=max(0, int(offset or 0)),
                )
                return _dump({"total": total, "counts": counts, "signals": [signals.to_dict(r) for r in rows]})
        except signals.SignalError as exc:
            return f"Refused: {exc}."
        except Exception:
            logger.warning("CTEM signal list failed", exc_info=True)
            return "Error: the signals could not be read right now. Say so; do not guess them."

    executor = CallableToolExecutor()
    executor.register("record_ctem_signal", _record)
    executor.register("list_ctem_signals", _list)
    path_enum = {"type": "string", "enum": list(TRIGGER_PATHS)}
    lens_enum = {"type": "string", "enum": list(LENSES)}
    definitions = [
        ToolDefinition(
            name="record_ctem_signal",
            description=(
                "Record a change in exposure that may start the CTEM loop (a CVE or an actor is optional). "
                "The same dedupe_key again only moves last_seen_at."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "trigger_path": path_enum,
                    "source_provider": {"type": "string", "description": "Who saw it, or 'manual'."},
                    "subject_kind": {"type": "string", "enum": list(SUBJECT_KINDS)},
                    "subject_ref": {"type": "string", "description": "Stable key: host, IP, URL, CPE, CVE, account."},
                    "lens": lens_enum,
                    "source_external_id": {"type": "string"},
                    "subject_label": {"type": "string"},
                    "observed_at": {"type": "string", "description": "ISO-8601."},
                    "evidence": {"type": "object", "description": "Facts and a link."},
                    "dedupe_key": {"type": "string"},
                },
                "required": ["trigger_path", "source_provider", "subject_kind", "subject_ref"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_ctem_signals",
            description="List CTEM signals, latest sighting first, with counts by state.",
            input_schema={
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": list(SIGNAL_STATES)},
                    "trigger_path": path_enum,
                    "lens": lens_enum,
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
    ]
    return definitions, executor
