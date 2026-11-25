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
    return None, f"No case {case_id}. case_id is the id list_ctem_cases answers, or its first 8+ characters."


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
    """One history entry a write added, as the compact update answer names it (#2092)."""
    return {
        "kind": event.kind,
        "from": event.from_value,
        "to": event.to_value,
        "reason": (event.reason or "")[:300] or None,
    }


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

    async def _shown(db: AsyncSession, case_id: uuid.UUID, org_id: uuid.UUID, vertical: str) -> Any:
        """(stored case, members, rendering) for the acting user, ``None`` when they see none of it."""
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        found = await store.get_case(db, case_id, organization_id=org_id)
        if found is None:
            return None
        case, rows, events = found
        # This case's members only (#2092): an older view or the case's own read answers while
        # a drain rebuilds the whole view, never a whole build on the call.
        members = [f"{r.source}:{r.external_id}" for r in rows]
        view = await view_of.viewer_view(db, str(scope), vertical, members=members)
        rendered = view_of.hydrate(case, rows, events[:_HISTORY_PAGE], view)
        if rendered is None:
            return None
        from app.services import ctem_case_version_history as version_history

        # The list's asset-count rule on the single read (#1704), on every event of the case.
        version_history.with_asset_count(rendered, events)
        return case, rows, rendered

    async def _view_only(db: AsyncSession, rows: list[Any], vertical: str) -> str | None:
        """The refusal of a write on a case the acting person sees only through a CTEM view right (#1748)."""
        from app.services import ctem_access
        from app.services import ctem_cases_view as view_of

        if await view_of.acts_on_case(db, str(scope), rows, vertical):
            return None
        return f"Refused: {ctem_access.VIEW_ONLY_MESSAGE} Nothing was changed."

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
                    return f"No case {case_id}."
                from app.services import ctem_case_versions as case_versions
                from app.services import ctem_version_match as matching

                # The versions the case's product runs on the assets (#1252), and
                # what a proposal may claim from them (#1472): blind when unknown.
                running = await case_versions.for_case(db, shown[0])
                if running is not None:
                    from app.services import ctem_case_version_history as version_history
                    from app.services import ctem_cases_store as store

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
                if await ctem_access.level(db, scope) == "view":
                    # A CTEM view right is strictly read only (#1748).
                    return f"Refused: {ctem_access.VIEW_ONLY_MESSAGE} Nothing was changed."
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
                shown = await _shown(db, wanted, org_id, vertical) if org_id and scope else None
                if shown is None:
                    return f"No case {case_id}."
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
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return f"No case {case_id}."
                shown = await _shown(db, wanted, org_id, vertical)
                if shown is None:
                    return f"No case {case_id}."
                refused = await _view_only(db, shown[1], vertical)
                if refused:
                    return refused
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
                await case_loop.publish_pending(pending)
                refreshed = await _shown(db, wanted, org_id, vertical)
                answer: dict[str, Any] = {"updated": refreshed[2] if refreshed else {"id": str(wanted)}}
                newest = (shown[0].validations or [{}])[-1]
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
            return "Error: the validation could not be recorded right now. Say so; nothing was changed."

    async def _ticket(case_id: str, integration: str = "", key: str = "", url: str = "", status: str = "open") -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules

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
                    return f"No case {case_id}."
                refused = await _view_only(db, shown[1], vertical)
                if refused:
                    return refused
                case_loop.record_ticket(
                    db, shown[0], integration=integration, key=key, url=url or None, status=status, actor=agent_id
                )
                await db.commit()
                refreshed = await _shown(db, wanted, org_id, vertical)
                return _dump({"updated": refreshed[2] if refreshed else {"id": str(wanted)}})
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case ticket failed", exc_info=True)
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
                shown = await _shown(db, wanted, org_id, vertical) if org_id and scope else None
                if shown is None:
                    return f"No case {case_id}."
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
                    return f"No case {case_id}."
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
                        return f"No case {one}."
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

    executor = CallableToolExecutor()
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
                "assets, primary assets and its latest history. A null count is unknown for you, never none."
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
                "Change a CTEM case: owner, due date, a note, the state with its reason, or the exploitation "
                "reading or attack path of the triage step, a loop gate passed or waited, or the risk treatment plan of "
                "the decision, or the threat package of the review step, or a change applied through an integration "
                "(with the gate ctem.production_change). Those writes move the status; "
                "resolved needs evidence; reopening needs a reason; accepted_risk and resolution_kind "
                "dismissed (on a reject_and_close answer) need a person."
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
                        "description": "Per supporting asset: techniques in attack order, exposure, reading. Replaces the stored one.",
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
                            "From get_opencrq_attack_path_reach: origin {asset, exposure}, hops nearest first "
                            "[{from, to, nature: proven|inferred, simulation (id), simulation_name, simulation_url, "
                            "seen_at}], reached [{asset, depth, "
                            "criticality}], primary (asset names). Replaces the stored one."
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
                            "A gate of the fleet's catalog: passed automatically (by: level or pin), "
                            "waited, or answered (by: who, option)."
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
                            "The risk treatment plan the remediation decision created for this case, as the "
                            "plan tool answered it: its id, name and Link url."
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
                            "The Grouping the review step drafted for this case in the threat intelligence "
                            "platform (its id, name and page "
                            "url) and the security coverage requested on it (coverage_id), once requested. "
                            "url is the draft page while the Grouping sits in a draft; a Grouping page is "
                            "checked against the live graph and replaced by the draft page when it is not live, "
                            "and refused while the analyst review is not approved. The case's package state: "
                            "drafted, awaiting_review, replan (answered with free text or another option), "
                            "rejected, validated (approved and live)."
                        ),
                        "properties": {
                            "grouping_id": {"type": "string"},
                            "name": {"type": "string"},
                            "url": {"type": "string"},
                            "coverage_id": {"type": "string"},
                        },
                        "required": ["grouping_id"],
                    },
                    "applied_change": {
                        "type": "object",
                        "description": (
                            "A fix applied through a bound integration, read back: what was pushed (indicators "
                            "with their prevention action, scope, expiry; a rule; a setting), the ids returned "
                            "and how to revert it."
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
                "Write a validation or re-test result on a CTEM case: the validation module's simulation, "
                "scenario or test id and its result. A passed or not_exposed resolves the case "
                "(mitigation_only: mitigated, watching, the re-test stays on) unless you set conclusive false "
                "(the check does not prove the exposure gone): the case stays open. failed reopens a resolved case; "
                "pending only records."
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
                            "remediation: a version inventory, every in-scope endpoint at or above the fixed "
                            "build; validation: a detection or behaviour test, never resolves a patch case; "
                            "exposure: a passive read proving the case still affected (endpoints below the fixed "
                            "build), written with result failed and conclusive true."
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
                "Create or sync the remediation ticket of a CTEM case in the organization's declared ticketing "
                "system (one ticket per case, found again on a retry). create=true opens it when missing (the "
                "Mobilize gate). Without create it pushes the case state or pulls the ticket status. Answers "
                "the ticket, its sync state, or why no ticketing is connected."
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
    ]
    return definitions, executor
