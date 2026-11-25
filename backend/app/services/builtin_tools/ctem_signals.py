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

``record_ctem_signal``, ``record_ctem_signals`` (a sweep's rows in one call, #1726)
and ``list_ctem_signals`` act for the person the turn
or run acts for, through ``services/ctem_signals.py`` (the API's rules and
shapes). XTM One mode only. Granted to the built-in CTEM agents of
:data:`CTEM_SIGNAL_AGENT_SLUGS`: the orchestrators and analysts that see a
trigger first; the seeded fleets are rewired to use them in a later step of
the loop (``dev-docs/ctem-signals.md`` section 6).
"""

from __future__ import annotations

import functools
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.ctem_signal import LENSES, SIGNAL_STATES, SUBJECT_KINDS, TRIGGER_PATHS
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

CTEM_SIGNAL_TOOL_NAMES: tuple[str, ...] = ("record_ctem_signal", "record_ctem_signals", "list_ctem_signals")
#: Rows one ``record_ctem_signals`` call takes; a sweep pages above it.
MAX_BULK_SIGNALS = 100
_SOURCE = "builtin:ctem_signals"
#: #2171: what the gate reads of ``evidence``; any other key is stored but never reconciled.
EVIDENCE_DESCRIPTION = (
    "Facts and a link. The gate matches only these keys (a string or a list of strings): "
    "assets, hostnames, urls, ips for assets; software, products, cpe (CPE 2.3) for software; "
    "cve or cves. Free-text keys are kept but never matched."
)


def evidence_hint(evidence: Any, decision: Any) -> str | None:
    """#2171: the line answered when the gate left the signal unresolved and *evidence* holds no key
    it reads, else ``None``."""
    from app.services.ctem_scope_gate import evidence_read_keys

    code = decision.get("reason_code") if isinstance(decision, dict) else None
    if code != "subject_unresolved" or evidence_read_keys(evidence if isinstance(evidence, dict) else {}):
        return None
    return (
        "Recorded, but evidence holds no key the gate matches (assets, hostnames, urls, ips, software, "
        "products, cpe, cve): hosts or products named only in free text are not reconciled. Record it "
        "again with the same dedupe_key and those keys."
    )


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


def _since(value: str) -> datetime | None:
    """An ISO-8601 date or time as an aware datetime (UTC when it names no zone), ``None`` when blank or bad."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def reader_scope(db: Any, user_id: uuid.UUID, *, agent_reach: bool = True) -> tuple[bool, dict[str, Any]]:
    """Whether *user_id* reads the organization's signals, and the ``scope`` block an answer carries.

    ``agent_reach`` reads the administrator reach of an agent tool
    (``agent_tool_admin_bypass``: strict admin access for agent tools and the
    Platform Health reach keep an administrator to their own); without it the
    admin flag alone decides, as the case view does (``ctem_cases_view.signal_members``).
    Who may read what is unchanged: the block only says which rule applied, so
    an empty answer is never read as an empty organization.
    """
    from app.api.v1.helpers import agent_tool_admin_bypass
    from app.models.user import User
    from app.services import ctem_access
    from app.services.health.identity import in_health_reach

    user = await db.get(User, user_id)
    is_admin = bool(getattr(user, "is_admin", False))
    # A CTEM view or decide right of the person's groups (#1748) is theirs in an agent tool too.
    right = None if is_admin else await ctem_access.group_right(db, user_id)
    whole = (agent_tool_admin_bypass(user) if agent_reach else is_admin) or right is not None
    if right is not None:
        reason = f"CTEM {right} right"
    elif whole:
        reason = "administrator"
    elif not is_admin:
        reason = "not an administrator"
    elif in_health_reach():
        reason = "Platform Health reach"
    else:
        reason = "strict admin access for agent tools"
    block = {
        "applied": "organization" if whole else "own",
        "acting_user": str(user_id),
        "is_admin": is_admin,
        "reason": reason,
    }
    if right is not None:
        block["ctem_right"] = right
    return whole, block


async def case_signal_scope(
    db: Any, user_id: uuid.UUID, seen: set[str], *, case_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """The ``scope`` block of a case answer: the signal members read by the case view's rule.

    *seen* holds the ``signal:<id>`` members the person may read; the case-bound
    signals beyond them are counted (a count only), never named. With *case_id*
    (a single-case read) only the signals bound to that case are counted, so a
    case with no hidden member never reports one.
    """
    from sqlalchemy import func, select

    from app.models.ctem_signal import CtemSignal

    whole, block = await reader_scope(db, user_id, agent_reach=False)
    if whole:
        return block
    statement = select(func.count()).select_from(CtemSignal)
    if case_id is None:
        statement = statement.where(CtemSignal.case_id.is_not(None))
    else:
        # The case view's rule without a CTEM right: the signals recorded for them.
        statement = statement.where(CtemSignal.case_id == case_id, CtemSignal.recorded_by != str(user_id))
    try:
        counted = int((await db.execute(statement)).scalar_one() or 0)
    except Exception:
        logger.info("CTEM cases: case-bound signals not counted for %s", user_id, exc_info=True)
        return block
    hidden = counted if case_id is not None else counted - len(seen)
    if hidden > 0:
        block["hidden_by_scope"] = hidden
        block["note"] = hidden_note(hidden, on_case=case_id is not None)
    return block


def hidden_note(hidden: int, *, on_case: bool = False) -> str:
    """The line an agent repeats when signals exist that the acting person did not record."""
    noun = "signal exists" if hidden == 1 else "signals exist"
    where = " on this case" if on_case else ""
    return f"{hidden} {noun}{where} that this run's person did not record; an administrator's run reads them."


def build_ctem_signal_tools(*, user_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The two signal tools, acting for *user_id*: an administrator lists the organization's signals.

    Whether the user's agent takes the administrator's reach is read at each list,
    through ``agent_tool_admin_bypass``.
    """
    try:
        scope: uuid.UUID | None = uuid.UUID(str(user_id))
    except ValueError:
        scope = None

    def _input(
        trigger_path: Any = "",
        source_provider: Any = "",
        subject_kind: Any = "",
        subject_ref: Any = "",
        lens: Any = "",
        source_external_id: Any = "",
        subject_label: Any = "",
        observed_at: Any = "",
        evidence: Any = None,
        dedupe_key: Any = "",
        **_ignored: Any,
    ) -> Any:
        """One row as a ``SignalInput``; :class:`SignalError` on a bad date."""
        from app.services import ctem_signals as signals

        try:
            text = str(observed_at or "")
            observed = datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
        except ValueError:
            raise signals.SignalError("observed_at must be an ISO-8601 date and time") from None
        return signals.SignalInput(
            trigger_path=str(trigger_path or ""),
            lens=str(lens or ""),
            source_provider=str(source_provider or ""),
            source_external_id=str(source_external_id or "") or None,
            subject_kind=str(subject_kind or ""),
            subject_ref=str(subject_ref or ""),
            subject_label=str(subject_label or "") or None,
            observed_at=observed,
            evidence=evidence if isinstance(evidence, dict) else {},
            dedupe_key=str(dedupe_key or "") or None,
        )

    async def _record_many(signals: list[Any] | None = None) -> str:
        """Every row in one session: one savepoint per row, one commit, one result per row.

        A refused row is rolled back alone and named by its index; the others are kept.
        A repeated dedupe key (in the batch or already stored) only moves last_seen_at.
        """
        from app.core.database import async_session
        from app.services import ctem_case_loop, ctem_scope_gate_store
        from app.services import ctem_signals as store
        from app.services.ctem_loop_stream import publish_signal_update
        from app.services.organizations import get_primary

        rows_in = signals
        if scope is None:
            return "No user: no signal can be recorded."
        if not isinstance(rows_in, list) or not rows_in:
            return "signals must be a non-empty list of rows with record_ctem_signal's fields."
        if len(rows_in) > MAX_BULK_SIGNALS:
            return f"Refused: at most {MAX_BULK_SIGNALS} signals per call; send the rest in another call. Nothing was recorded."
        results: list[dict[str, Any]] = []
        written: list[Any] = []
        pendings: list[Any] = []
        try:
            async with async_session() as db:
                primary = await get_primary(db)
                if primary is None:
                    return "No organization is set up yet: no signal can be recorded."
                for index, raw in enumerate(rows_in):
                    if not isinstance(raw, dict):
                        results.append({"index": index, "error": "not an object"})
                        continue
                    try:
                        async with db.begin_nested():
                            row, created = await store.record_signal(
                                db,
                                organization_id=primary.id,
                                signal=_input(**raw),
                                recorded_via="tool",
                                recorded_by=str(scope),
                            )
                            pendings.append(await ctem_scope_gate_store.evaluate_after_record(db, row, created=created))
                    except store.SignalError as exc:
                        results.append({"index": index, "error": str(exc)})
                        continue
                    written.append(row)
                    result: dict[str, Any] = {
                        "index": index,
                        "created": created,
                        "id": str(row.id),
                        "dedupe_key": row.dedupe_key,
                        "state": row.state,
                        "seen_count": row.seen_count,
                        "case_id": str(row.case_id) if row.case_id else None,
                    }
                    hint = evidence_hint(raw.get("evidence"), row.decision)
                    if hint:
                        result["hint"] = hint
                    results.append(result)
                await db.commit()
                for pending in pendings:
                    await ctem_case_loop.publish_pending(pending)
                for row in written:
                    await publish_signal_update(db, row)
        except Exception:
            logger.warning("CTEM signal bulk record failed", exc_info=True)
            return "Error: the signals could not be recorded right now. Say so; nothing was recorded."
        refused = sum(1 for r in results if "error" in r)
        return _dump(
            {
                "recorded": len(results) - refused,
                "created": sum(1 for r in results if r.get("created")),
                "refused": refused,
                "results": results,
            }
        )

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
        from app.services.ctem_loop_stream import publish_signal_update
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
                from app.services import ctem_case_loop, ctem_scope_gate_store

                pending = await ctem_scope_gate_store.evaluate_after_record(db, row, created=created)
                await db.commit()
                await ctem_case_loop.publish_pending(pending)
                await publish_signal_update(db, row)
                answer: dict[str, Any] = {"created": created, "signal": signals.to_dict(row)}
                hint = evidence_hint(evidence, row.decision)
                if hint:
                    answer["hint"] = hint
                return _dump(answer)
        except signals.SignalError as exc:
            return f"Refused: {exc}. Nothing was recorded."
        except Exception:
            logger.warning("CTEM signal record failed", exc_info=True)
            return "Error: the signal could not be recorded right now. Say so; nothing was recorded."

    async def _list(
        state: str = "",
        trigger_path: str = "",
        lens: str = "",
        limit: int = 20,
        offset: int = 0,
        signal_id: str = "",
        source_provider: str = "",
        since: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_signals as signals
        from app.services.organizations import get_primary

        if scope is None:
            return "No user: there are no signals to read."
        since_at = _since(since)
        if since and since_at is None:
            return "since must be an ISO-8601 date or time, e.g. 2026-11-20 or 2026-11-20T08:00:00Z."
        # The same filters for the page and the organization-wide count below.
        listed = functools.partial(
            signals.list_signals,
            state=state or None,
            trigger_path=trigger_path or None,
            lens=lens or None,
            source_provider=source_provider or None,
            since=since_at,
        )
        try:
            async with async_session() as db:
                primary = await get_primary(db)
                if primary is None:
                    return "No organization is set up yet: there are no signals."
                # The admin bypass of an agent tool goes through the helper (strict admin
                # access for agent tools and Platform Health reach decide it).
                bypass, scope_block = await reader_scope(db, scope)
                if signal_id:
                    # One signal by id (the signal_id of a ctem_signal.* event, #1409), under the same scope.
                    try:
                        sid = uuid.UUID(str(signal_id))
                    except ValueError:
                        return "Refused: signal_id is not a UUID."
                    row = await signals.get_signal(
                        db, sid, organization_id=primary.id, recorded_by=None if bypass else str(scope)
                    )
                    if row is None:
                        return "No such signal in your scope."
                    return _dump({"total": 1, "signals": [signals.to_dict(row)], "scope": scope_block})
                rows, total, counts = await listed(
                    db,
                    organization_id=primary.id,
                    recorded_by=None if bypass else str(scope),
                    limit=max(1, min(int(limit or 20), 50)),
                    offset=max(0, int(offset or 0)),
                )
                if not bypass:
                    # A count only, no content: how many the same filters match beyond the person's own.
                    _, org_total, _ = await listed(
                        db,
                        organization_id=primary.id,
                        recorded_by=None,
                        limit=1,
                        offset=0,
                    )
                    if org_total > total:
                        scope_block["hidden_by_scope"] = org_total - total
                        scope_block["note"] = hidden_note(org_total - total)
                return _dump(
                    {
                        "total": total,
                        "counts": counts,
                        "signals": [signals.to_dict(r) for r in rows],
                        "scope": scope_block,
                    }
                )
        except signals.SignalError as exc:
            return f"Refused: {exc}."
        except Exception:
            logger.warning("CTEM signal list failed", exc_info=True)
            return "Error: the signals could not be read right now. Say so; do not guess them."

    executor = CallableToolExecutor()
    executor.register("record_ctem_signal", _record)
    executor.register("record_ctem_signals", _record_many)
    executor.register("list_ctem_signals", _list)
    path_enum = {"type": "string", "enum": list(TRIGGER_PATHS)}
    lens_enum = {"type": "string", "enum": list(LENSES)}
    definitions = [
        ToolDefinition(
            name="record_ctem_signal",
            description=(
                "Record an exposure change that may start the CTEM loop (a CVE or an actor is optional). "
                "A repeated dedupe_key only moves last_seen_at."
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
                    "evidence": {"type": "object", "description": EVIDENCE_DESCRIPTION},
                    "dedupe_key": {"type": "string"},
                },
                "required": ["trigger_path", "source_provider", "subject_kind", "subject_ref"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="record_ctem_signals",
            description=(
                f"Record up to {MAX_BULK_SIGNALS} signals in one call (a sweep): each row takes "
                "record_ctem_signal's fields; one result per row, a bad row refused alone."
            ),
            input_schema={
                "type": "object",
                "properties": {"signals": {"type": "array", "items": {"type": "object"}}},
                "required": ["signals"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_ctem_signals",
            description="Your CTEM signals (admin: all), newest first, counts by state.",
            input_schema={
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": list(SIGNAL_STATES)},
                    "trigger_path": path_enum,
                    "lens": lens_enum,
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                    "signal_id": {"type": "string"},
                    "source_provider": {"type": "string"},
                    "since": {"type": "string", "description": "ISO-8601, on last seen."},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
    ]
    return definitions, executor
