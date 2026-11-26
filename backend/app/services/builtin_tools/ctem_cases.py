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

"""CTEM Cases tools (#11545, slice S2; #891): list, read, create and update cases from an agent.

Offered to the built-in CTEM agents that work a case (``dev-docs/ctem-cases.md``
section 8): the two loop orchestrators, the Remediation Coordinator and the
Mobilization Analyst, in XTM One mode, as the person
the turn or run acts for. The rules are ``services/ctem_cases.py``'s, the
writes ``services/ctem_cases_store.py``'s: an agent never accepts risk,
resolves only with evidence, and every write is a case event with the agent
as actor. The answers are the API's own shapes (``api/v1/ctem_cases.py``).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import CASE_STATES
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

CTEM_CASE_TOOL_NAMES: tuple[str, ...] = (
    "list_ctem_cases",
    "get_ctem_case",
    "create_ctem_case",
    "update_ctem_case",
    "record_ctem_case_validation",
    "record_ctem_case_ticket",
    "decide_ctem_case_remediation",
    "sync_ctem_case_ticket",
    "bulk_update_ctem_cases",
    "merge_ctem_cases",
    "delete_ctem_cases",
    "record_ctem_case_detection_gap",
)
_SOURCE = "builtin:ctem_cases"
#: The built-in agents that carry the tools (design section 8): the two loop
#: orchestrators run a case from ``ctem_case.opened`` to its proof (#891).
CTEM_CASE_AGENT_SLUGS: frozenset[str] = frozenset(
    {
        "ctem-vuln-prioritization-orchestrator",
        "ctem-threat-response-orchestrator",
        "ctem-remediation-coordinator",
        "ctem-mobilization-analyst",
    }
)
#: The root causes a detection gap carries (#2393), the gap analyst's causes but ``none``.
_GAP_ROOT_CAUSES: tuple[str, ...] = ("no_rule", "no_log", "broken_collector", "matching_defect", "unknown")
#: Every case state, read from the model so a new state reaches the counts and the enum (#857).
_STATES: tuple[str, ...] = CASE_STATES
#: History events one ``get_ctem_case`` answer carries.
_HISTORY_PAGE = 20
#: Issues one ``get_ctem_case`` answer lists (#2092): a 700-issue case answered 300k characters.
_ISSUE_PAGE = 100
#: A case read slower than this is logged with its size (#2092).
_SLOW_READ_SECONDS = 5.0


def offers_ctem_case_tools(agent_slug: str | None, *, is_builtin: bool) -> bool:
    """Whether a turn or run of this agent is offered the case tools (XTM One mode only)."""
    from app.core.config import settings

    if settings.is_copilot_like:
        return False
    return bool(is_builtin and (agent_slug or "") in CTEM_CASE_AGENT_SLUGS)



def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip()) if value else None
    except ValueError:
        return None


async def resolve_case_id(db: AsyncSession, case_id: str, organization_id: uuid.UUID) -> tuple[uuid.UUID | None, str]:
    """The case *case_id* names: a full id, or a hex prefix of 8+ characters naming one case (#2103).

    Answers ``(id, "")`` or ``(None, why)``: an ambiguous prefix lists the ids it matches.
    """
    from app.services import ctem_cases_store as store

    full = _uuid(case_id)
    if full is not None:
        return full, ""
    found = await store.case_ids_with_prefix(db, str(case_id or ""), organization_id=organization_id, limit=5)
    if len(found) == 1:
        return found[0], ""
    if found:
        return None, f"case_id {case_id!r} matches several cases: {', '.join(str(f) for f in found)}. Pass the full id."
    # A merged-away case's prefix names its old id (#2289): the read then answers the survivor.
    merged = await store.merged_with_prefix(db, str(case_id or ""), organization_id=organization_id, limit=5)
    if len(merged) == 1:
        return merged[0].case_id, ""
    if merged:
        ids = ", ".join(str(m.case_id) for m in merged)
        return None, f"case_id {case_id!r} matches several merged cases: {ids}. Pass the full id."
    return None, f"No case {case_id}. case_id is the id list_ctem_cases answers, or its first 8+ characters."


async def missing_case_answer(
    db: AsyncSession, case_id: object, wanted: uuid.UUID | None, organization_id: uuid.UUID | None
) -> str:
    """What a case tool answers when *wanted* names no case the caller sees (#2289).

    A case merged into another answers the survivor, when and by whom, so the caller
    follows it; an id never created (or deleted) keeps ``No case <id>.``.
    """
    from app.services import ctem_cases_store as store

    if wanted is not None and organization_id is not None:
        merged = await store.merged_into(db, wanted, organization_id=organization_id)
        if merged is not None:
            return merged.answer()
    return f"No case {case_id}."


def _when(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _bounded_issues(rendered: dict[str, Any], offset: int = 0) -> dict[str, Any]:
    """*rendered* with at most :data:`_ISSUE_PAGE` issues from *offset*, the total and a truncated flag (#2092).

    The issues come ranked (``ctem_cases_view.hydrate``), Restricted rows last, so
    the page keeps the ones that matter; the counts and scores stay those of the case.
    ``issues_next_offset`` reads the next page (#2103), ``None`` on the last one.
    """
    issues = list(rendered.get("issues") or [])
    start = max(0, int(offset or 0))
    end = start + _ISSUE_PAGE
    return {
        **rendered,
        "issues": issues[start:end],
        "issues_total": len(issues),
        "issues_offset": start,
        "issues_truncated": start > 0 or len(issues) > end,
        "issues_next_offset": end if len(issues) > end else None,
    }


def _same_gate(events: list[Any], gate: dict[str, Any]) -> bool:
    """Whether the newest ``gate`` event of this gate key already records this decision (#2092).

    An identical decision written again (a retried call after a timeout) is not
    recorded twice.
    """
    key = str(gate.get("key") or "").strip()
    newest = next((e for e in reversed(events) if e.kind == "gate" and e.from_value == key), None)
    if newest is None:
        return False
    return (
        (newest.to_value or "") == str(gate.get("outcome") or "")
        and (newest.reason or "") == (str(gate.get("by") or "").strip()[:300])
        and (newest.evidence or "") == (str(gate.get("option") or "").strip()[:300])
    )


def _written(event: Any) -> dict[str, Any]:
    """One history entry a write added, as the compact update answer names it (#2092).

    A threat package row stores the coverage id in ``from`` and the Grouping id in ``to``
    (``ctem_case_loop.record_threat_package``): it is named field by field (#2429), so no
    reader takes the coverage id for a previous value.
    """
    if event.kind == "threat_package":
        return {
            "kind": event.kind,
            "grouping_id": event.to_value,
            "coverage_id": event.from_value,
            "url": event.evidence,
            "reason": (event.reason or "")[:300] or None,
        }
    return {
        "kind": event.kind,
        "from": event.from_value,
        "to": event.to_value,
        "reason": (event.reason or "")[:300] or None,
    }


async def _compact_write(
    db: AsyncSession, case: Any, *, state_before: str | None, known: set[Any], **recorded: Any
) -> dict[str, Any]:
    """The compact answer of a validation or ticket write (#2473), never the whole case re-read.

    The case id, title and state after the write, whether that state moved, what this call
    recorded (*recorded*) and the history events it added; ``get_ctem_case`` reads the rest.
    A history read that fails after the commit leaves ``history_written`` empty.
    """
    from app.services import ctem_cases_store as store

    written: list[dict[str, Any]] = []
    try:
        after = (await store.events_of(db, [case.id])).get(case.id, [])
        written = [_written(e) for e in after if e.id not in known]
    except Exception:
        logger.info("CTEM case write: written events unreadable for %s", case.id, exc_info=True)
    answer: dict[str, Any] = {
        "id": str(case.id),
        "title": case.title,
        "state": case.state,
        "state_changed": case.state != state_before,
    }
    if case.state != state_before:
        answer["previous_state"] = state_before
        answer["resolution_kind"] = getattr(case, "resolution_kind", None)
    answer.update(recorded)
    answer["history_written"] = written
    return answer


def _recorded_validation(case: Any, provider: str, ref: str) -> dict[str, Any] | None:
    """The stored validation entry of *(provider, ref)* with its index, as the write keeps it (#2473)."""
    wanted = ((provider or "").strip()[:40], (ref or "").strip()[:300])
    entries = list(getattr(case, "validations", None) or [])
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if (entry.get("provider"), entry.get("ref")) == wanted:
            shown = {
                k: entry.get(k)
                for k in ("provider", "kind", "ref", "result", "reported_result", "conclusive", "evidence_type", "at")
                if entry.get(k) is not None
            }
            if entry.get("mitigation_only"):
                shown["mitigation_only"] = True
            if entry.get("run_order"):
                shown["run_order"] = entry["run_order"]
            return {"index": index, **shown}
    return None


def _recorded_ticket(case: Any, integration: str, key: str) -> dict[str, Any] | None:
    """The stored ticket reference of *(integration, key)*, as the write keeps it (#2473)."""
    wanted = ((integration or "").strip()[:80], (key or "").strip()[:120])
    for entry in reversed(list(getattr(case, "tickets", None) or [])):
        if (entry.get("integration"), entry.get("key")) == wanted:
            return {k: entry.get(k) for k in ("integration", "key", "url", "status", "at") if entry.get(k) is not None}
    return None


def build_ctem_case_tools(*, user_id: str, agent_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The four case tools, acting for *user_id*; every write is recorded with *agent_id* as actor.

    They render every case through the acting user's own source read (#11613,
    ``services/ctem_cases_view.py``): a case with no issue that user sees does
    not exist for the agent, an issue they cannot see is a Restricted row, and
    a new owner must see the case too. Without a user there is no case.
    """
    scope = _uuid(user_id)

    async def _context(db: AsyncSession) -> tuple[uuid.UUID | None, str]:
        from app.services.ctem_verticals import normalize_vertical
        from app.services.organizations import get_primary

        if scope is None:
            return None, ""
        primary = await get_primary(db)
        if primary is None:
            return None, ""
        from app.models.user import User

        user = await db.get(User, scope)
        return primary.id, normalize_vertical(getattr(user, "ctem_vertical", None))

    async def _shown(
        db: AsyncSession, case_id: uuid.UUID, org_id: uuid.UUID, vertical: str, *, lock: bool = False
    ) -> Any:
        """(stored case, members, rendering) for the acting user, ``None`` when they see none of it.

        *lock* (#2373): a write reads the case row locked until its commit, so a
        concurrent write of the same case waits and then reads what this one wrote.
        """
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        found = await store.get_case(db, case_id, organization_id=org_id, for_update=lock)
        if found is None:
            return None
        case, rows, events = found
        # This case's members only (#2092): an older view or the case's own read answers while
        # a drain rebuilds the whole view, never a whole build on the call.
        members = [f"{r.source}:{r.external_id}" for r in rows]
        view = await view_of.viewer_view(db, str(scope), vertical, members=members)
        # The plan, the package and its review from the whole history, the served page cut (#2429).
        rendered = view_of.hydrate(case, rows, events, view, history_page=_HISTORY_PAGE)
        if rendered is None:
            return None
        from app.services import ctem_case_version_history as version_history

        # The list's asset-count rule on the single read (#1704), on every event of the case.
        version_history.with_asset_count(rendered, events)
        # The detection gaps from the whole history (#2393), never the newest page alone.
        rendered["detection_gaps"] = view_of.detection_gaps_of(events)
        from app.services import ctem_case_assets as case_assets

        # Each attached host's module ids, rows stored before #2448 included: asset_id is the inventory id.
        await case_assets.enrich_source_ids(db, rendered.get("assets"))
        return case, rows, rendered

    async def _view_only(db: AsyncSession, rows: list[Any], vertical: str) -> str | None:
        """The refusal of a write on a case the acting person sees only through a CTEM view right (#1748)."""
        from app.services import ctem_access
        from app.services import ctem_cases_view as view_of

        if await view_of.acts_on_case(db, str(scope), rows, vertical):
            return None
        reason = await ctem_access.refusal(db, str(scope), write=True)
        return f"Refused: {reason or ctem_access.VIEW_ONLY_MESSAGE} Nothing was changed."

    async def _list(
        state: str = "",
        priority: str = "",
        owner_user_id: str = "",
        limit: int = 20,
        offset: int = 0,
        asset: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        if state and state not in _STATES:
            return f"Unknown state {state!r}: one of {', '.join(_STATES)}."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return "No organization is set up yet: there are no cases."
                cases = await store.list_cases(db, organization_id=org_id, vertical=vertical)
                owner = _uuid(owner_user_id)
                try:
                    view = await view_of.viewer_view(db, str(scope), vertical, stale_ok=True)
                except view_of.ViewRebuildingError as exc:
                    # Never a whole view build on the call (#2092): say when to retry.
                    return f"Not available yet: {exc} Nothing is wrong with the cases; do not guess them."
                # A case deleted between the case read and the view read is gone, never hidden (#2113).
                cases = await view_of.drop_gone(db, cases, view)
                rows, counts = view_of.list_for_viewer(
                    cases,
                    view,
                    state=state or None,
                    priority=priority or None,
                    owner_user_id=str(owner) if owner else None,
                    # The cases on one asset (#2103): a duplicate check reads them all in one call.
                    asset=str(asset or "").strip() or None,
                    limit=max(1, min(int(limit or 20), 50)),
                    offset=max(0, int(offset or 0)),
                )
                from app.services import ctem_case_version_history as version_history

                # Issues that name no asset: the inventory count, else "unknown" with its reason (#1694).
                history = await store.events_of(db, [uuid.UUID(row["id"]) for row in rows])
                for row in rows:
                    version_history.with_asset_count(row, history.get(uuid.UUID(row["id"]), []))
                from .ctem_signals import case_signal_scope

                # Which signal rule applied, so an empty list is never read as no case (#1359), and the
                # open cases this person is refused (hidden) or the view could not judge now (unjudged,
                # #2113), each a count only with the view's condition, so a page is never the org total.
                scope_block = await case_signal_scope(db, scope, view.signals)
                scope_block.update(view_of.list_visibility(cases, view))
                from app.services import ctem_case_routing as routing

                # A case the catch-up has not asked yet names its place in the backlog (#2345).
                places = await routing.catch_up_places(db, [row["id"] for row in rows])
                for row in rows:
                    if row["id"] in places:
                        row["catch_up"] = places[row["id"]]
                return _dump(
                    {
                        "vertical": vertical,
                        "counts": {s: counts.get(s, 0) for s in _STATES},
                        "cases": rows,
                        "scope": scope_block,
                    }
                )
        except Exception:
            logger.warning("CTEM case list failed", exc_info=True)
            return "Error: the cases could not be read right now. Say so; do not guess them."

    async def _get(case_id: str = "", issues_offset: int = 0) -> str:
        from app.core.database import async_session

        if not str(case_id or "").strip():
            return "case_id must be the id list_ctem_cases answers."
        started = time.perf_counter()
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return f"No case {case_id}."
                wanted, why = await resolve_case_id(db, case_id, org_id)
                if wanted is None:
                    return why
                shown = await _shown(db, wanted, org_id, vertical)
                if shown is None:
                    from app.services.ctem_case_delete import deleted_case_answer

                    # A deleted case says so, from its audit row (#2328): never a bare miss.
                    return await deleted_case_answer(db, wanted) or await missing_case_answer(
                        db, case_id, wanted, org_id
                    )
                from app.services import ctem_case_versions as case_versions
                from app.services import ctem_version_match as matching

                # The versions the case's product runs on the assets (#1252), and
                # what a proposal may claim from them (#1472): blind when unknown.
                running = await case_versions.for_case(db, shown[0])
                if running is not None:
                    from app.services import ctem_case_version_history as version_history
                    from app.services import ctem_cases_store as store

                    # The asset count agrees with the running versions this read serves (#2225).
                    version_history.with_live_asset_count(shown[2], running, at=datetime.now(UTC))

                    # Why the versions moved, per device (#1694): before, after, when, source, reason.
                    events = (await store.events_of(db, [shown[0].id])).get(shown[0].id, [])
                    running = {**running, "history": version_history.history_of(events)}
                match = (
                    matching.version_match(
                        running,
                        product_label=shown[0].product_label,
                        fixed_version=shown[0].fixed_version,
                        # The running versions OpenCTI places as affected or not (#1783), never a range.
                        affected=matching.affected_from_outcomes(shown[2].get("issues")),
                    )
                    if shown[0].remediation_kind == "upgrade"
                    else None
                )
                rendered = _bounded_issues(shown[2], issues_offset)
                from app.services import ctem_case_routing as routing

                # A case the catch-up has not asked yet names its place in the backlog (#2345).
                place = (await routing.catch_up_places(db, [shown[0].id])).get(str(shown[0].id))
                if place is not None:
                    rendered = {**rendered, "catch_up": place}
                elapsed = time.perf_counter() - started
                if elapsed > _SLOW_READ_SECONDS:
                    logger.info(
                        "CTEM case read of %s took %.1f s (%d issues)", wanted, elapsed, rendered["issues_total"]
                    )
                return _dump({**rendered, "running_versions": running, "version_match": match})
        except Exception:
            logger.warning("CTEM case read failed", exc_info=True)
            return "Error: the case could not be read right now. Say so; do not guess it."

    async def _create(title: str = "", issue_ids: list[str] | None = None, reason: str = "") -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return "No organization is set up yet: no case can be created."
                from app.services import ctem_access

                view = await view_of.viewer_view(db, str(scope), vertical)
                visible = set(view.open_ids)
                refused = await ctem_access.refusal(db, scope, write=True)
                if refused is not None:
                    # Checked at call time for the person the turn or run acts for (#1748, #2441).
                    return f"Refused: {refused} Nothing was changed."
                pending = case_loop.PendingEvents()
                case = await store.create_manual_case(
                    db,
                    organization_id=org_id,
                    vertical=vertical,
                    title=title,
                    member_ids=[str(i) for i in (issue_ids or [])],
                    visible=visible,
                    actor=agent_id,
                    reason=(reason or "").strip() or None,
                    events=pending,
                )
                await db.commit()
                await case_loop.publish_pending(pending)
                shown = await _shown(db, case.id, org_id, vertical)
                return _dump({"created": shown[2] if shown else {"id": str(case.id), "title": case.title}})
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}."
        except Exception:
            logger.warning("CTEM case create failed", exc_info=True)
            return "Error: the case could not be created right now. Say so; nothing was changed."

    async def _update(
        case_id: str = "",
        state: str = "",
        owner_user_id: str = "",
        due_at: str = "",
        reason: str = "",
        evidence: str = "",
        note: str = "",
        resolution_kind: str = "",
        exploitation: list[dict[str, Any]] | None = None,
        attack_path: dict[str, Any] | None = None,
        gate: dict[str, Any] | None = None,
        treatment_plan: dict[str, Any] | None = None,
        threat_package: dict[str, Any] | None = None,
        applied_change: dict[str, Any] | None = None,
        scenario: dict[str, Any] | None = None,
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        if state and state not in _STATES:
            return f"Unknown state {state!r}: one of {', '.join(_STATES)}."
        if owner_user_id and _uuid(owner_user_id) is None:
            return "owner_user_id must be a user id."
        try:
            due = _when(due_at)
        except ValueError:
            return "due_at must be an ISO-8601 date and time."
        committed = False
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                shown = await _shown(db, wanted, org_id, vertical, lock=True) if org_id and scope else None
                if shown is None:
                    return await missing_case_answer(db, case_id, wanted, org_id if scope else None)
                case, rows, _ = shown
                refused = await _view_only(db, rows, vertical)
                if refused:
                    return refused
                before = (await store.events_of(db, [case.id])).get(case.id, [])
                known = {e.id for e in before}
                unchanged: list[str] = []
                if isinstance(gate, dict) and str(gate.get("outcome") or "") == "waited":
                    from app.agents.builtin_fleet_docs import CTEM_GATE_KEYS
                    from app.services import ctem_gate_order

                    if str(gate.get("key") or "").strip() not in CTEM_GATE_KEYS:
                        raise rules.CaseRuleError(
                            f"unknown gate {str(gate.get('key') or '')!r}: one of {', '.join(CTEM_GATE_KEYS)}"
                        )

                    # A gate waits only on its decision (#1954): no open decision item, no waited event,
                    # checked before any write of this call so a refusal changes nothing.
                    refusal = ctem_gate_order.waited_refusal(
                        str(gate.get("key") or "").strip(),
                        str(case.id),
                        await ctem_gate_order.open_gate_item_keys(db, str(case.id)),
                    )
                    if refusal:
                        raise rules.CaseRuleError(refusal)
                owner = _uuid(owner_user_id)
                if owner and owner != case.owner_user_id:
                    owner_view = await view_of.viewer_view(db, str(owner), vertical)
                    if not view_of.owner_sees(rows, owner_view):
                        raise rules.CaseRuleError(
                            "the owner must be able to see this case's issues in the source products"
                        )
                await store.update_case(
                    db,
                    case,
                    actor=agent_id,
                    actor_kind="agent",
                    state=state or None,
                    resolution_kind=resolution_kind or None,
                    owner_user_id=_uuid(owner_user_id),
                    due_at=due,
                    reason=reason or None,
                    evidence=evidence or None,
                    note=note or None,
                )
                if exploitation is not None:
                    case_loop.record_exploitation(db, case, exploitation, actor=agent_id)
                if attack_path is not None:
                    if not isinstance(attack_path, dict):
                        raise rules.CaseRuleError("attack_path must be an object with its origin")
                    case_loop.record_attack_path(db, case, attack_path, actor=agent_id)
                if gate is not None and not isinstance(gate, dict):
                    raise rules.CaseRuleError("gate must be an object with key and outcome")
                if gate is not None and _same_gate(before, gate):
                    # The same decision again (a retry after a timeout, #2092): recorded once.
                    unchanged.append("gate")
                elif gate is not None:
                    case_loop.record_gate(
                        db,
                        case,
                        key=str(gate.get("key") or ""),
                        outcome=str(gate.get("outcome") or ""),
                        by=str(gate.get("by") or ""),
                        option=str(gate.get("option") or ""),
                        actor=agent_id,
                    )
                if applied_change is not None:
                    case_loop.record_applied_change(db, case, applied_change, actor=agent_id)
                if treatment_plan is not None:
                    history = (await store.events_of(db, [case.id])).get(case.id, [])
                    case_loop.record_treatment_plan(
                        db, case, treatment_plan, view_of.treatment_plan_of(history), actor=agent_id
                    )
                if threat_package is not None:
                    from app.services import ctem_threat_package_live as package_live

                    # A Grouping page is recorded only once the Grouping is in OpenCTI's live
                    # graph, read with the acting user's own access (#1547), and only once the
                    # case's analyst review approved the package (#1573).
                    history = (await store.events_of(db, [case.id])).get(case.id, [])
                    threat_package = await package_live.verify_package(
                        db, str(scope), threat_package, view_of.review_outcome(history)
                    )
                    case_loop.record_threat_package(
                        db, case, threat_package, view_of.threat_package_of(history), actor=agent_id
                    )
                if scenario is not None:
                    from app.services import ctem_validation_scenario as case_scenario

                    history = (await store.events_of(db, [case.id])).get(case.id, [])
                    case_scenario.record_scenario(
                        db, case, scenario, case_scenario.scenario_of(history), actor=agent_id
                    )
                await db.commit()
                committed = True
                # A compact answer (#2092): what this call wrote, never the whole case re-read
                # (300k characters and a view build on a 700-issue case); get_ctem_case reads it.
                written: list[dict[str, Any]] = []
                try:
                    after = (await store.events_of(db, [case.id])).get(case.id, [])
                    written = [_written(e) for e in after if e.id not in known]
                except Exception:
                    logger.info("CTEM case update: written events unreadable for %s", wanted, exc_info=True)
                return _dump(
                    {
                        "updated": {
                            "id": str(case.id),
                            "title": case.title,
                            "state": case.state,
                            "owner_user_id": str(case.owner_user_id) if case.owner_user_id else None,
                            "due_at": case.due_at,
                            "changed": sorted(
                                name
                                for name, value in (
                                    ("state", state),
                                    ("owner_user_id", owner_user_id),
                                    ("due_at", due_at),
                                    ("note", note),
                                    ("exploitation", exploitation),
                                    ("attack_path", attack_path),
                                    ("gate", gate),
                                    ("treatment_plan", treatment_plan),
                                    ("threat_package", threat_package),
                                    ("applied_change", applied_change),
                                    ("scenario", scenario),
                                )
                                if value not in (None, "") and name not in unchanged
                            ),
                            "unchanged": unchanged,
                            "history_written": written,
                        }
                    }
                )
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case update failed", exc_info=True)
            if committed:
                # The write is in (#2092): never report a failure for a committed change.
                return _dump({"updated": {"id": str(wanted), "committed": True}})
            return "Error: the case could not be changed right now. Say so; nothing was changed."

    async def _validation(
        case_id: str,
        provider: str = "",
        ref: str = "",
        result: str = "",
        kind: str = "retest",
        mitigation_only: bool = False,
        note: str = "",
        run_order: list[str] | None = None,
        conclusive: bool | None = None,
        not_tested: list[str] | None = None,
        evidence_type: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules

        untested = [str(i).strip() for i in (not_tested or []) if str(i).strip()]
        if untested:
            # #1458: an id with no template was not tested; no negative is ever recorded for it.
            note = ("not tested (no template): " + ", ".join(untested) + ". " + (note or "")).strip()
            if result in rules.PASSING_RESULTS:
                conclusive = False
        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        pending = case_loop.PendingEvents()
        committed = False
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return f"No case {case_id}."
                shown = await _shown(db, wanted, org_id, vertical, lock=True)
                if shown is None:
                    return await missing_case_answer(db, case_id, wanted, org_id)
                refused = await _view_only(db, shown[1], vertical)
                if refused:
                    return refused
                from app.services import ctem_cases_store as store

                case = shown[0]
                state_before = case.state
                known = {e.id for e in (await store.events_of(db, [case.id])).get(case.id, [])}
                await case_loop.record_validation(
                    db,
                    shown[0],
                    provider=provider,
                    kind=kind,
                    ref=ref,
                    result=result,
                    mitigation_only=bool(mitigation_only),
                    note=note or None,
                    run_order=[str(n) for n in run_order] if isinstance(run_order, list) else None,
                    conclusive=conclusive if isinstance(conclusive, bool) else None,
                    evidence_type=(evidence_type or "").strip() or None,
                    actor=agent_id,
                    events=pending,
                )
                await db.commit()
                committed = True
                await case_loop.publish_pending(pending)
                # A compact answer (#2473): what this call wrote, never the whole case re-read.
                recorded = _recorded_validation(case, provider, ref)
                answer: dict[str, Any] = {
                    "updated": await _compact_write(
                        db, case, state_before=state_before, known=known, validation=recorded
                    )
                }
                newest = recorded or {}
                if untested:
                    answer["not_tested"] = untested
                if result in rules.PASSING_RESULTS and newest.get("conclusive") is False:
                    answer["inconclusive"] = (
                        "recorded as inconclusive: the case stays open with its remediation; only a check "
                        "proving the fixed version resolves it"
                    )
                    if newest.get("evidence_type") == "validation":
                        answer["inconclusive"] = (
                            "recorded as inconclusive validation evidence: a patch or upgrade case resolves only on "
                            "remediation "
                            "evidence (a version inventory showing every in-scope endpoint at or above the fixed "
                            "build, evidence_type remediation)"
                        )
                return _dump(answer)
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case validation failed", exc_info=True)
            if committed:
                # The write is in (#2473): never report a failure for a committed validation.
                return _dump({"updated": {"id": str(wanted), "committed": True}})
            return "Error: the validation could not be recorded right now. Say so; nothing was changed."

    async def _ticket(case_id: str, integration: str = "", key: str = "", url: str = "", status: str = "open") -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        committed = False
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return f"No case {case_id}."
                shown = await _shown(db, wanted, org_id, vertical, lock=True)
                if shown is None:
                    return await missing_case_answer(db, case_id, wanted, org_id)
                refused = await _view_only(db, shown[1], vertical)
                if refused:
                    return refused
                from app.services import ctem_cases_store as store

                case = shown[0]
                state_before = case.state
                known = {e.id for e in (await store.events_of(db, [case.id])).get(case.id, [])}
                case_loop.record_ticket(
                    db, case, integration=integration, key=key, url=url or None, status=status, actor=agent_id
                )
                await db.commit()
                committed = True
                # A compact answer (#2473): the ticket recorded, never the whole case re-read.
                recorded = _recorded_ticket(case, integration, key)
                return _dump(
                    {"updated": await _compact_write(db, case, state_before=state_before, known=known, ticket=recorded)}
                )
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case ticket failed", exc_info=True)
            if committed:
                # The write is in (#2473): never report a failure for a committed ticket reference.
                return _dump({"updated": {"id": str(wanted), "committed": True}})
            return "Error: the ticket reference could not be recorded right now. Say so; nothing was changed."

    async def _remediation(
        case_id: str = "",
        options: list[dict[str, Any]] | None = None,
        recommendation: str = "",
        answered_by: str = "",
        answered_option: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules
        from app.services import ctem_remediation_decision as decision_rules

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        try:
            parsed = decision_rules.parse_options(options)
            decision_rules.recommended(parsed, recommendation)
        except decision_rules.DecisionInputError as exc:
            return f"Refused: {exc}. Nothing was changed."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                shown = await _shown(db, wanted, org_id, vertical, lock=True) if org_id and scope else None
                if shown is None:
                    return await missing_case_answer(db, case_id, wanted, org_id if scope else None)
                refused = await _view_only(db, shown[1], vertical)
                if refused:
                    return refused
                case = shown[0]
                # An option citing a version the case does not carry needs its source (partner 0ae1fa2e).
                fixed = [case.fixed_version or "", *(case.fixed_versions or {}).values()]
                unbacked = decision_rules.unverified_versions(options, fixed)
                if unbacked and not answered_option:
                    return (
                        f"Refused: the options cite version(s) {', '.join(unbacked)} that this case does not "
                        f"carry (fixed version: {case.fixed_version or 'unknown'}). Read the fixed version from "
                        "the advisory or release notes and give it as version_source on that option, or "
                        "word the option without a version. Nothing was changed."
                    )
                figures = await _opencrq_readable(db)
                if answered_option:
                    chosen = decision_rules.recommended(parsed, answered_option)
                    case_loop.record_gate(
                        db,
                        case,
                        key=decision_rules.GATE_KEY,
                        outcome="answered",
                        by=(answered_by or "a person")[:300],
                        option=chosen.key,
                        actor=agent_id,
                    )
                    answered = decision_rules.Decision(
                        False, f"answered by {answered_by or 'a person'}", chosen=chosen.key, figures_shown=figures
                    )
                    db.add(
                        case_loop._event(
                            case.id,
                            agent_id,
                            "note",
                            datetime.now(UTC),
                            reason=decision_rules.outcome_note(answered, parsed),
                        )
                    )
                    await db.commit()
                    return _dump({"decided": True, "option": chosen.key, "by": answered_by or "a person"})
                waits, by = await _gate_state(db)
                decision = decision_rules.decide(
                    parsed,
                    recommendation,
                    gate_waits=waits,
                    gate_by=by,
                    approval_threshold=await _approval_threshold(db, org_id),
                    figures_visible=figures,
                )
                item_id = ""
                if decision.waits:
                    item_id = await _park(
                        case,
                        parsed,
                        recommendation,
                        decision,
                        await _currency(db, org_id),
                        await _gate_recipient(db),
                    )
                case_loop.record_gate(
                    db,
                    case,
                    key=decision_rules.GATE_KEY,
                    outcome="waited" if decision.waits else "passed_automatically",
                    by=decision.case_reason[:300],
                    option=decision.chosen,
                    actor=agent_id,
                )
                db.add(
                    case_loop._event(
                        case.id,
                        agent_id,
                        "note",
                        datetime.now(UTC),
                        reason=decision_rules.outcome_note(decision, parsed),
                    )
                )
                await db.commit()
                return _dump(
                    {
                        "decided": not decision.waits,
                        "option": decision.chosen or None,
                        "reason": decision.reason,
                        "decision_item_id": item_id or None,
                        "figures_shown": decision.figures_shown,
                    }
                )
        except (rules.CaseRuleError, decision_rules.DecisionInputError) as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM remediation decision failed", exc_info=True)
            return "Error: the remediation decision could not be recorded right now. Say so; nothing was changed."

    async def _opencrq_readable(db: AsyncSession) -> bool:
        """Whether the acting user may read OpenCRQ figures (the option figures are left out otherwise)."""
        from app.services.organization_context_opencrq import _opencrq_connection

        try:
            return not isinstance(await _opencrq_connection(db, str(scope)), str)
        except Exception:
            logger.warning("Risk figures access check failed", exc_info=True)
            return False

    async def _gate_state(db: AsyncSession) -> tuple[bool, str]:
        """Whether ``ctem.remediation.decision`` waits for the agent's home fleet, and what decides it.

        No home fleet, or a fleet that does not declare the gate: it waits (fail closed).
        """
        from app.services import ctem_remediation_decision as decision_rules
        from app.services.fleet_autonomy import fleet_autonomy_for_run, gate_waits
        from app.services.fleet_runs import home_fleet_defaults_of

        homes = await home_fleet_defaults_of(db, [agent_id])
        home = next(iter(homes.values()), None)
        found = await fleet_autonomy_for_run(db, home.fleet_id) if home else None
        if not found:
            return True, "no fleet catalog read"
        level, gates = found
        gate = next((g for g in gates if g.get("key") == decision_rules.GATE_KEY), None)
        if gate is None:
            return True, "gate not declared by the fleet"
        pin = gate.get("pin")
        return gate_waits(level, gate), f"pin {pin}" if pin else f"level {level}"

    async def _approval_threshold(db: AsyncSession, org_id: uuid.UUID | None) -> float | None:
        from app.services import organization_context as org_context

        row = await org_context.current_version(db, org_id)
        return org_context.parse_profile(row.profile if row else {}).risk_appetite.approval_threshold

    async def _currency(db: AsyncSession, org_id: uuid.UUID | None) -> str:
        """The organization profile's currency the option figures read in (#1598); empty when it names none."""
        from app.services import organization_context as org_context

        try:
            row = await org_context.current_version(db, org_id)
            return org_context.parse_profile(row.profile if row else {}).currency or ""
        except Exception:  # the card is written whatever the profile answers
            logger.info("Organization currency unavailable for the remediation ask", exc_info=True)
            return ""

    async def _gate_recipient(db: AsyncSession) -> str | None:
        """The CTEM decide groups a company agent's gate waits for (#1748); ``None``: today's routing."""
        from sqlalchemy import select

        from app.models.agent import Agent
        from app.services import ctem_access

        try:
            company = (
                await db.execute(select(Agent.is_company_managed).where(Agent.id == agent_id))
            ).scalar_one_or_none()
        except Exception:  # unreadable: today's routing, never a widening
            logger.info("CTEM gate recipient: agent unreadable", exc_info=True)
            return None
        return await ctem_access.gate_recipient(db) if company is True else None

    async def _park(
        case: Any,
        parsed: list[Any],
        recommendation: str,
        decision: Any,
        currency: str = "",
        recipient: str | None = None,
    ) -> str:
        """One work item for a person with the unified decision ask; the existing one when it is still open.

        *recipient* addresses it to the CTEM decide groups (#1748); ``None`` keeps today's routing.
        """
        from app.services import ctem_remediation_decision as decision_rules
        from app.services.company_managed_writes import agent_writer
        from app.services.work_item import create_work_item
        ask = decision_rules.decision_ask(
            case.title,
            parsed,
            recommendation,
            figures=decision.figures_shown,
            reason=decision.reason,
            currency=currency,
            severity=str(getattr(case, "severity", None) or ""),
            sla_due_at=getattr(case, "sla_due_at", None),
        )
        item = await create_work_item(
            user_id=str(scope),
            title=f"Remediation decision: {case.title}"[:200],
            description=f"CTEM case {case.id}. Gate {decision_rules.GATE_KEY}. {decision.reason}.",
            owner_agent_id=agent_id,
            created_by_agent_id=agent_id,
            priority="high",
            correlation_key=f"ctem:remediation:{case.id}",
            payload={"ctem_case_id": str(case.id), "gate": decision_rules.GATE_KEY},
            needs_human=True,
            ask=ask,
            recipient=recipient,
            dedupe=True,
            dedupe_by="key",
            writer=agent_writer(is_company_managed=False, agent_id=agent_id),
        )
        return str(item.get("id") or "")

    async def _sync_ticket(case_id: str, create: bool = False) -> str:
        """#917 slice 4: the Mobilization role works the ticket through the sync layer only."""
        from app.core.config import settings as app_settings
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_ticketing

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return f"No case {case_id}."
                shown = await _shown(db, wanted, org_id, vertical)
                if shown is None:
                    return await missing_case_answer(db, case_id, wanted, org_id)
                refused = await _view_only(db, shown[1], vertical)
                if refused:
                    return refused
                pending = case_loop.PendingEvents()
                answer = await ctem_ticketing.sync_one(
                    db,
                    shown[0],
                    create=bool(create),
                    events=pending,
                    base_url=getattr(app_settings, "frontend_url", "") or "",
                )
                await db.commit()
                await case_loop.publish_pending(pending)
                return _dump(answer)
        except Exception:
            logger.warning("CTEM case ticket sync failed", exc_info=True)
            return "Error: the ticket could not be synced right now. Say so; the scheduled sync retries it."

    async def _bulk_update(case_ids: list[str] | None = None, **change: Any) -> str:
        """One change on several cases (#1723): each case is checked and written as update_ctem_case does."""
        ids = list(dict.fromkeys(case_ids or []))
        if not 1 <= len(ids) <= 200:
            return "case_ids must hold 1 to 200 case ids."
        updated: list[str] = []
        failed: list[dict[str, str]] = []
        for one in ids:
            answer = await _update(case_id=one, **change)
            if answer.startswith("{"):
                updated.append(one)
            else:
                failed.append({"id": one, "reason": answer})
        return _dump({"updated": updated, "failed": failed})

    async def _merge(target_id: str = "", case_ids: list[str] | None = None, reason: str = "") -> str:
        """Fold duplicate cases into *target_id* on the rules of POST /ctem-cases/merge (#2037).

        Every case named must be one the acting person sees and acts on; the
        survivor keeps its state, owner and SLA and takes the keys, members,
        history and signals of the others, so a reconcile maps them to it.
        """
        from app.core.database import async_session
        from app.services import ctem_cases as rules
        from app.services import ctem_cases_store as store

        target = _uuid(target_id)
        sources = [_uuid(c) for c in case_ids or []]
        if target is None or not sources or any(s is None for s in sources):
            return "target_id and case_ids must be ids list_ctem_cases answers."
        if len(sources) > 50:
            return "At most 50 cases at once."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return "No organization is set up yet: there are no cases."
                for one in [target, *sources]:
                    shown = await _shown(db, one, org_id, vertical)
                    if shown is None:
                        return await missing_case_answer(db, one, one, org_id)
                    refused = await _view_only(db, shown[1], vertical)
                    if refused:
                        return refused
                await store.merge_cases(
                    db,
                    organization_id=org_id,
                    vertical=vertical,
                    target_id=target,
                    source_ids=[s for s in sources if s],
                    actor=agent_id,
                    reason=(reason or "").strip() or None,
                )
                await db.commit()
                shown = await _shown(db, target, org_id, vertical)
                return _dump({"merged": [str(s) for s in sources], "into": shown[2] if shown else {"id": str(target)}})
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case merge failed", exc_info=True)
            return "Error: the cases could not be merged right now. Say so; nothing was changed."

    async def _delete(
        case_ids: list[str] | None = None,
        states: list[str] | None = None,
        opened_by: str = "",
        reason: str = "",
        dry_run: bool = True,
    ) -> str:
        """Delete cases as the acting administrator (#1723); a dry run (default) writes nothing."""
        from app.core.database import async_session
        from app.models.user import User
        from app.services import ctem_case_delete as case_delete

        ids = [_uuid(c) for c in case_ids or []]
        if any(i is None for i in ids):
            return "case_ids must be ids list_ctem_cases answers."
        if len(ids) > case_delete.MAX_CASES:
            return f"At most {case_delete.MAX_CASES} cases at once."
        bad = [s for s in states or [] if s not in _STATES]
        if bad:
            return f"Unknown state {bad[0]!r}: one of {', '.join(_STATES)}."
        if opened_by and opened_by not in ("legacy", "signal", "split"):
            return "opened_by is legacy, signal or split."
        if not ids and not states and not opened_by:
            return "Name the cases (case_ids) or a filter (states, opened_by)."
        try:
            async with async_session() as db:
                user = await db.get(User, scope) if scope else None
                if user is None or not getattr(user, "is_admin", False):
                    return "Refused: deleting CTEM cases needs an administrator as the acting person."
                org_id, _vertical = await _context(db)
                if org_id is None:
                    return "No organization is set up yet: there are no cases."
                cases, unknown = await case_delete.select_cases(
                    db,
                    organization_id=org_id,
                    case_ids=[i for i in ids if i],
                    states=states or [],
                    opened_by=opened_by or None,
                )
                result = await case_delete.delete_cases(
                    db, cases, actor=user, reason=reason or f"by agent {agent_id}", dry_run=bool(dry_run)
                )
                await db.commit()
                # Same hand-off as the REST route: released signals decided again after the commit (#1950).
                return _dump({**case_delete.hand_off_released(result), "unknown": unknown})
        except Exception:
            logger.warning("CTEM case delete failed", exc_info=True)
            return "Error: the cases could not be deleted right now. Say so; nothing was changed."

    async def _detection_gap(
        action: str = "propose",
        case_id: str = "",
        decision_key: str = "",
        validation_provider: str = "",
        simulation_id: str = "",
        inject_id: str = "",
        asset_id: str = "",
        technique: str = "",
        root_cause: str = "",
        remediation: str = "",
        evidence: str = "",
        rule_draft: str = "",
        rule_language: str = "",
        recommendation: str = "done",
        outcome: str = "",
        by: str = "",
    ) -> str:
        """#2393: a detection gap of the desk proposed on the case its simulation validates, or its answer."""
        from app.core.database import async_session
        from app.models.ctem_case import CtemCaseEvent as CtemCaseEventRow
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        if action not in ("propose", "answer"):
            return "action is propose or answer."
        inject, asset = str(inject_id or "").strip()[:120], str(asset_id or "").strip()[:120]
        if not inject or not asset:
            return "inject_id and asset_id name the gap (the ids of its desk item key)."
        gap_key = f"{inject}:{asset}"
        if action == "propose" and root_cause not in _GAP_ROOT_CAUSES:
            return f"root_cause must be one of {', '.join(_GAP_ROOT_CAUSES)}."
        if action == "propose" and not str(decision_key or "").strip():
            return "decision_key is the correlation key of the gap's decision item (your gap key)."
        if action == "answer" and not str(outcome or "").strip():
            return "outcome is the answer the person gave (done, declined, deferred)."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return "No case: file today's desk item (no CTEM case is readable for this run)."
                wanted = _uuid(case_id)
                if wanted is None and str(case_id or "").strip():
                    wanted, why = await resolve_case_id(db, case_id, org_id)
                    if wanted is None:
                        return why
                if wanted is None and str(simulation_id or "").strip() and str(validation_provider or "").strip():
                    found = await store.case_ids_for_validation_ref(
                        db,
                        organization_id=org_id,
                        provider=str(validation_provider).strip(),
                        ref=str(simulation_id).strip(),
                    )
                    wanted = found[0] if len(found) == 1 else None
                    if len(found) > 1:
                        return (
                            f"Simulation {simulation_id} validates several cases: "
                            f"{', '.join(str(f) for f in found)}. Pass case_id."
                        )
                if wanted is None:
                    return "No case: this simulation validates no CTEM case. File your own decision item as before."
                shown = await _shown(db, wanted, org_id, vertical)
                if shown is None:
                    return await missing_case_answer(db, case_id or simulation_id, wanted, org_id)
                refused = await _view_only(db, shown[1], vertical)
                if refused:
                    return refused
                case = shown[0]
                now = datetime.now(UTC)
                if action == "answer":
                    db.add(
                        CtemCaseEventRow(
                            case_id=case.id,
                            actor=agent_id,
                            kind=view_of.DETECTION_GAP_KIND,
                            at=now,
                            from_value=gap_key,
                            to_value=f"answered:{str(outcome).strip()[:40]}",
                            reason=(str(by or "").strip()[:300] or None),
                        )
                    )
                    await db.commit()
                    refreshed = await _shown(db, wanted, org_id, vertical)
                    gaps = (refreshed[2] if refreshed else {}).get("detection_gaps") or []
                    return _dump({"case_id": str(case.id), "gap": next((g for g in gaps if g["key"] == gap_key), None)})
                detail = {
                    "technique": str(technique or "").strip()[:200] or None,
                    "simulation_id": str(simulation_id or "").strip()[:120] or None,
                    "evidence": str(evidence or "").strip()[:2000] or None,
                    "rule_draft": str(rule_draft or "").strip()[:20000] or None,
                    "rule_language": str(rule_language or "").strip()[:40] or None,
                }
                item_id = await _file_desk_gap(
                    case, str(decision_key).strip()[:200], root_cause, remediation, detail, recommendation
                )
                detail["item_id"] = item_id or None
                db.add(
                    CtemCaseEventRow(
                        case_id=case.id,
                        actor=agent_id,
                        kind=view_of.DETECTION_GAP_KIND,
                        at=now,
                        from_value=gap_key,
                        to_value=root_cause,
                        reason=str(remediation or "").strip()[:1000] or None,
                        evidence=json.dumps(detail, ensure_ascii=False),
                    )
                )
                await db.commit()
                return _dump(
                    {"case_id": str(case.id), "gap": gap_key, "decision_item_id": item_id, "recorded": "detection_gap"}
                )
        except Exception:
            logger.warning("CTEM case detection gap failed", exc_info=True)
            return "Error: the detection gap could not be recorded on the case right now. Say so; nothing was changed."

    async def _file_desk_gap(
        case: Any, decision_key: str, root_cause: str, remediation: str, detail: dict[str, Any], recommendation: str
    ) -> str:
        """The case-scoped decision of one gap: the caller's own item under its own key, carrying the case (#2393)."""
        from app.services.company_managed_writes import agent_writer
        from app.services.work_item import create_work_item

        fix = {
            "no_rule": "Deploy the drafted detection rule?",
            "no_log": "Repair the missing log source?",
            "broken_collector": "Repair the broken collector?",
        }.get(root_cause, "Fix this detection gap?")
        ask = {
            "question": fix,
            "context": f"CTEM case {case.title}: {str(remediation or '').strip()[:400]}",
            "options": [
                {"key": "done", "label": "Done", "effect": "The fix is made; the desk re-tests the gap."},
                {"key": "declined", "label": "Declined", "effect": "The gap stays open on the case, recorded."},
            ],
            "recommendation": recommendation if recommendation in ("done", "declined") else "done",
        }
        body = [f"CTEM case {case.id}.", f"Root cause: {root_cause}.", str(remediation or "").strip()]
        if detail.get("rule_draft"):
            body.append(f"Drafted rule ({detail.get('rule_language') or 'rule'}):\n{detail['rule_draft']}")
        item = await create_work_item(
            user_id=str(scope),
            title=f"Detection gap on case: {case.title}"[:200],
            description="\n\n".join(b for b in body if b),
            owner_agent_id=agent_id,
            created_by_agent_id=agent_id,
            priority="high",
            correlation_key=decision_key,
            payload={
                "ctem_case_id": str(case.id),
                "simulation_id": detail.get("simulation_id"),
                "root_cause": root_cause,
            },
            needs_human=True,
            ask=ask,
            dedupe=True,
            dedupe_by="key",
            writer=agent_writer(is_company_managed=False, agent_id=agent_id),
        )
        return str(item.get("id") or "")

    executor = CallableToolExecutor()
    executor.register("record_ctem_case_detection_gap", _detection_gap)
    executor.register("bulk_update_ctem_cases", _bulk_update)
    executor.register("merge_ctem_cases", _merge)
    executor.register("delete_ctem_cases", _delete)
    executor.register("list_ctem_cases", _list)
    executor.register("get_ctem_case", _get)
    executor.register("create_ctem_case", _create)
    executor.register("update_ctem_case", _update)
    executor.register("record_ctem_case_validation", _validation)
    executor.register("record_ctem_case_ticket", _ticket)
    executor.register("decide_ctem_case_remediation", _remediation)
    executor.register("sync_ctem_case_ticket", _sync_ticket)
    state_enum = {"type": "string", "enum": list(_STATES)}
    definitions = [
        ToolDefinition(
            name="list_ctem_cases",
            description=(
                "List the CTEM cases you can see (open exposures grouped by the fix that closes them), "
                "highest priority first, with the counts by state. A null count is unknown for you, never "
                "none (aggregates_reason says why); scope.hidden_cases counts cases you are refused, "
                "scope.unjudged_cases those not readable now (scope.view says why)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": state_enum,
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "owner_user_id": {"type": "string", "description": "Only the cases this user owns."},
                    "asset": {"type": "string", "description": "Only the cases with an issue on this asset name."},
                    "limit": {"type": "integer", "description": "Cases a page (default 20, max 50)."},
                    "offset": {"type": "integer"},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_ctem_case",
            description=(
                "Read one CTEM case: its fix, its member issues with their source product and id, "
                "assets, primary assets and its latest history. A null count is unknown for you, never none. "
                "An asset's asset_id is the inventory id; its id in a source product is in source_ids[].external_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "The id, or its first 8+ characters."},
                    "issues_offset": {
                        "type": "integer",
                        "description": "First issue of the page (issues_next_offset).",
                    },
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_ctem_case",
            description=(
                "Split open issues out of their cases into a new manual case. issue_ids are the "
                "'source:external_id' of issues get_ctem_case lists as open."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "issue_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string", "description": "Why they are handled apart."},
                },
                "required": ["title", "issue_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_ctem_case",
            description=(
                "Change a CTEM case: owner, due date, note, state with its reason, exploitation, attack "
                "path, gate, treatment plan, threat package, scenario, or a change applied through an "
                "integration (gate ctem.production_change); these move the status. resolved needs evidence, "
                "reopening a reason; accepted_risk and dismissed need a person."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "state": state_enum,
                    "owner_user_id": {"type": "string"},
                    "due_at": {"type": "string", "description": "ISO-8601; without an offset it is read as UTC."},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                    "note": {"type": "string"},
                    "resolution_kind": {"type": "string"},
                    "exploitation": {
                        "type": "array",
                        "description": "Per supporting asset: techniques in attack order, exposure, reading. Replaces it.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "risk_scenario_id": {"type": "string"},
                                "asset": {"type": "string"},
                                "exposure": {"type": "string", "enum": ["internet_facing", "internal", "unknown"]},
                                "techniques": {"type": "array", "items": {"type": "string"}},
                                "reading": {"type": "string"},
                            },
                            "required": ["asset", "techniques"],
                        },
                    },
                    "attack_path": {
                        "type": "object",
                        "description": (
                            "As get_opencrq_attack_path_reach answers it: origin, hops nearest first, reached, "
                            "primary (asset names). Replaces the stored one."
                        ),
                        "properties": {
                            "origin": {"type": "object"},
                            "hops": {"type": "array", "items": {"type": "object"}},
                            "reached": {"type": "array", "items": {"type": "object"}},
                            "primary": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["origin"],
                    },
                    "gate": {
                        "type": "object",
                        "description": (
                            "A catalog gate: passed_automatically (by: level|pin), waited, answered (by, option)."
                        ),
                        "properties": {
                            "key": {"type": "string", "description": "The gate key, e.g. ctem.mobilize."},
                            "outcome": {"type": "string", "enum": ["passed_automatically", "waited", "answered"]},
                            "by": {"type": "string"},
                            "option": {"type": "string"},
                        },
                        "required": ["key", "outcome"],
                    },
                    "treatment_plan": {
                        "type": "object",
                        "description": (
                            "The remediation decision's risk treatment plan, as the plan tool answered: id, name, Link url."
                        ),
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "url": {"type": "string"},
                        },
                        "required": ["id"],
                    },
                    "threat_package": {
                        "type": "object",
                        "description": (
                            "The Grouping the review drafted (id, name, page url) and its coverage_id. A url not "
                            "live becomes the draft page; refused until the analyst review approves."
                        ),
                        "properties": {
                            "grouping_id": {"type": "string"},
                            "name": {"type": "string"},
                            "url": {"type": "string"},
                            "coverage_id": {"type": "string"},
                        },
                        "required": ["grouping_id"],
                    },
                    "scenario": {
                        "type": "object",
                        "description": (
                            "Validation scenario: scenario_id, name, url, techniques_total, techniques_playable, "
                            "playable_tests."
                        ),
                    },
                    "applied_change": {
                        "type": "object",
                        "description": (
                            "A fix applied through a bound integration, read back: pushed, ids, how to revert."
                        ),
                        "properties": {
                            "integration": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["ioc_prevention", "detection_rule", "configuration", "control_restored"],
                            },
                            "pushed": {"type": "string"},
                            "ids": {"type": "array", "items": {"type": "string"}},
                            "rollback": {"type": "string"},
                        },
                        "required": ["integration", "kind", "pushed", "rollback"],
                    },
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="record_ctem_case_validation",
            description=(
                "Write a validation or re-test result on a CTEM case. passed or not_exposed resolves it "
                "(mitigation_only: mitigated, re-test kept on) unless conclusive is false; failed reopens a "
                "resolved case; pending only records."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "provider": {
                        "type": "string",
                        "description": "The integration the test ran on, by its provider key.",
                    },
                    "ref": {"type": "string", "description": "The run, simulation or scenario id."},
                    "result": {"type": "string", "enum": ["passed", "not_exposed", "failed", "pending"]},
                    "kind": {"type": "string", "description": "validation or retest (default retest)."},
                    "mitigation_only": {"type": "boolean"},
                    "note": {"type": "string"},
                    "not_tested": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Requested ids with no template: not tested, never recorded as a negative.",
                    },
                    "evidence_type": {
                        "type": "string",
                        "enum": ["remediation", "validation", "exposure"],
                        "description": (
                            "remediation: every in-scope endpoint at or above the fixed build; validation: a "
                            "detection test, never resolves a patch case; exposure: endpoints still below it "
                            "(result failed, conclusive true)."
                        ),
                    },
                    "conclusive": {
                        "type": "boolean",
                        "description": (
                            "True when the check proves its claim: the exposure gone for a pass (the fixed "
                            "version read), the exposure present for exposure evidence."
                        ),
                    },
                    "run_order": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Target asset groups in the order they were tested.",
                    },
                },
                "required": ["case_id", "provider", "ref", "result"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="record_ctem_case_ticket",
            description=(
                "Write or update the reference of a remediation ticket on a CTEM case (integration, key, "
                "url, status). One entry per ticket; never changes the case state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "integration": {"type": "string", "description": "The ticketing integration, by its provider key."},
                    "key": {"type": "string", "description": "The ticket key or number."},
                    "url": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "in_progress", "done", "cancelled"]},
                },
                "required": ["case_id", "integration", "key"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="decide_ctem_case_remediation",
            description=(
                "Remediation decision of a CTEM case (gate ctem.remediation.decision): options with figures "
                "and your recommendation. Decided by policy unless the gate waits or the approval threshold "
                "needs a person (one decision item opens). answered_option records a person's answer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "options": {
                        "type": "array",
                        "description": "1 to 6 options; figures from the connected risk quantification, in the organization's currency. Texts over their room are refused.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                                "cost": {"type": "number"},
                                "expected_loss_before": {"type": "number"},
                                "expected_loss_after": {"type": "number"},
                                "residual_risk": {"type": "string"},
                                "time_to_apply": {"type": "string"},
                                "runbook": {"type": "string"},
                                "preapproved": {"type": "boolean"},
                                "version_source": {
                                    "type": "string",
                                    "description": "Where a version the option names comes from, when the case has none.",
                                },
                                "not_priced_reason": {
                                    "type": "string",
                                    "description": "Why the option carries no figures; the card prints it after 'not priced:'.",
                                },
                            },
                            "required": ["key", "label"],
                        },
                    },
                    "recommendation": {"type": "string", "description": "The key of the recommended option."},
                    "answered_by": {"type": "string"},
                    "answered_option": {"type": "string", "description": "The option key a person chose."},
                },
                "required": ["case_id", "options", "recommendation"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="sync_ctem_case_ticket",
            description=(
                "Create or sync the one remediation ticket of a CTEM case in the declared ticketing system. "
                "create=true opens it when missing (Mobilize gate); otherwise it pushes the case state or "
                "pulls the ticket status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "create": {"type": "boolean", "description": "Open the ticket when the case has none."},
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="bulk_update_ctem_cases",
            description=(
                "Apply one change (state with reason, owner, due date, note) to 1-200 CTEM cases; each is "
                "checked as update_ctem_case. Answers updated ids and failed {id, reason}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_ids": {"type": "array", "items": {"type": "string"}},
                    "state": state_enum,
                    "owner_user_id": {"type": "string"},
                    "due_at": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["case_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="merge_ctem_cases",
            description=(
                "Merge duplicate CTEM cases of one fix into target_id (the case a person holds, else the "
                "oldest): it keeps its state, owner and SLA and takes their keys, issues and history; a "
                "reconcile never reopens them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "The surviving case."},
                    "case_ids": {"type": "array", "items": {"type": "string"}, "description": "Cases folded in."},
                    "reason": {"type": "string"},
                },
                "required": ["target_id", "case_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_ctem_cases",
            description=(
                "Administrator only: delete CTEM cases by id or filter (states, opened_by legacy). Cancels "
                "their work items and timers, sends their signals back to the gate, frees the fix key: a "
                "case whose signals still match reopens at the next reconcile, so merge duplicates instead. "
                "dry_run (default true) lists what would go."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_ids": {"type": "array", "items": {"type": "string"}},
                    "states": {"type": "array", "items": state_enum},
                    "opened_by": {"type": "string", "enum": ["legacy", "signal", "split"]},
                    "reason": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="record_ctem_case_detection_gap",
            description=(
                "Detection gap on the CTEM case a simulation validates (case_id, or validation_provider and "
                "simulation_id). propose records it on the case and files its decision under decision_key; "
                "answer records the person's answer. No case: file your own decision item."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["propose", "answer"]},
                    "case_id": {"type": "string"},
                    "decision_key": {"type": "string"},
                    "validation_provider": {"type": "string"},
                    "simulation_id": {"type": "string"},
                    "inject_id": {"type": "string"},
                    "asset_id": {"type": "string"},
                    "technique": {"type": "string"},
                    "root_cause": {"type": "string", "enum": list(_GAP_ROOT_CAUSES)},
                    "remediation": {"type": "string"},
                    "evidence": {"type": "string"},
                    "rule_draft": {"type": "string"},
                    "rule_language": {"type": "string"},
                    "recommendation": {"type": "string", "enum": ["done", "declined"]},
                    "outcome": {"type": "string", "description": "done, declined or deferred."},
                    "by": {"type": "string"},
                },
                "required": ["inject_id", "asset_id"],
            },
            source=_SOURCE,
        ),
    ]
    return definitions, executor
