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
    "assess_ctem_validation_inject",
    "record_ctem_case_ticket",
    "decide_ctem_case_remediation",
    "sync_ctem_case_ticket",
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


def _when(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


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
        """(stored case, members, rendering, events) for the acting user, ``None`` when they see none of it.

        *events* is the whole case history ``store.get_case`` read, newest first (the
        tool-less gate checks read it, #1644).
        """
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        found = await store.get_case(db, case_id, organization_id=org_id)
        if found is None:
            return None
        case, rows, events = found
        rendered = view_of.hydrate(
            case, rows, events[:_HISTORY_PAGE], await view_of.viewer_view(db, str(scope), vertical)
        )
        return None if rendered is None else (case, rows, rendered, events)

    async def _list(
        state: str = "", priority: str = "", owner_user_id: str = "", limit: int = 20, offset: int = 0
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
                view = await view_of.viewer_view(db, str(scope), vertical)
                rows, counts = view_of.list_for_viewer(
                    cases,
                    view,
                    state=state or None,
                    priority=priority or None,
                    owner_user_id=str(owner) if owner else None,
                    limit=max(1, min(int(limit or 20), 50)),
                    offset=max(0, int(offset or 0)),
                )
                from .ctem_signals import case_signal_scope

                return _dump(
                    {
                        "vertical": vertical,
                        "counts": {s: counts.get(s, 0) for s in _STATES},
                        "cases": rows,
                        # Which signal rule applied, so an empty list is never read as no case (#1359).
                        "scope": await case_signal_scope(db, scope, view.signals),
                    }
                )
        except Exception:
            logger.warning("CTEM case list failed", exc_info=True)
            return "Error: the cases could not be read right now. Say so; do not guess them."

    async def _get(case_id: str = "") -> str:
        from app.core.database import async_session

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                shown = await _shown(db, wanted, org_id, vertical) if org_id and scope else None
                if shown is None:
                    return f"No case {case_id}."
                from app.services import ctem_case_versions as case_versions
                from app.services import ctem_version_match as matching

                # The versions the case's product runs on the assets (#1252), and
                # what a proposal may claim from them (#1472): blind when unknown.
                running = await case_versions.for_case(db, shown[0])
                match = (
                    matching.version_match(
                        running, product_label=shown[0].product_label, fixed_version=shown[0].fixed_version
                    )
                    if shown[0].remediation_kind == "upgrade"
                    else None
                )
                return _dump({**shown[2], "running_versions": running, "version_match": match})
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
                view = await view_of.viewer_view(db, str(scope), vertical)
                pending = case_loop.PendingEvents()
                case = await store.create_manual_case(
                    db,
                    organization_id=org_id,
                    vertical=vertical,
                    title=title,
                    member_ids=[str(i) for i in (issue_ids or [])],
                    visible=set(view.open_ids),
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
        exploitation: list[dict[str, Any]] | None = None,
        attack_path: dict[str, Any] | None = None,
        gate: dict[str, Any] | None = None,
        treatment_plan: dict[str, Any] | None = None,
        threat_package: dict[str, Any] | None = None,
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
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                shown = await _shown(db, wanted, org_id, vertical) if org_id and scope else None
                if shown is None:
                    return f"No case {case_id}."
                case, rows, _, history = shown
                # Before any write: a refusal leaves the case untouched (#1644).
                await _check_tool_less(db, case, history, state=state, gate=gate)
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
                if gate is not None:
                    if not isinstance(gate, dict):
                        raise rules.CaseRuleError("gate must be an object with key and outcome")
                    case_loop.record_gate(
                        db,
                        case,
                        key=str(gate.get("key") or ""),
                        outcome=str(gate.get("outcome") or ""),
                        by=str(gate.get("by") or ""),
                        option=str(gate.get("option") or ""),
                        actor=agent_id,
                    )
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
                refreshed = await _shown(db, wanted, org_id, vertical)
                return _dump({"updated": refreshed[2] if refreshed else {"id": str(wanted)}})
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case update failed", exc_info=True)
            return "Error: the case could not be changed right now. Say so; nothing was changed."

    async def _check_tool_less(db: AsyncSession, case: Any, history: list[Any], *, state: str, gate: Any) -> None:
        """The tool-less gate rules of one ``update_ctem_case`` call (#1644); raises ``CaseRuleError``."""
        from app.services import ctem_cases as rules
        from app.services import ctem_tool_less_gates as tool_less

        gate_key = str(gate.get("key") or "").strip() if isinstance(gate, dict) else ""
        gate_outcome = str(gate.get("outcome") or "") if isinstance(gate, dict) else ""
        if gate_key in tool_less.TOOL_LESS_GATES and gate_outcome == "passed_automatically":
            tool_less.check_record(gate_key, gate_outcome, (await _tool_less_waits(db)).get(gate_key))
        if not state:
            return
        answering = gate_key if gate_outcome == "answered" else ""
        tool_less.check_hold(tool_less.pending(history), "move the case", answering=answering)
        proven = rules.passing(rules.latest_validation(getattr(case, "validations", None)))
        if state in tool_less.CLOSING_STATES and not proven:
            tool_less.check_close(
                state,
                proven=proven,
                waits=(await _tool_less_waits(db)).get(tool_less.CLOSE_WITHOUT_RETEST),
                history=history,
                answering=answering,
            )

    async def _validation(
        case_id: str,
        provider: str = "",
        ref: str = "",
        result: str = "",
        kind: str = "retest",
        mitigation_only: bool = False,
        note: str = "",
        safe_mode: bool | None = None,
        run_order: list[str] | None = None,
        conclusive: bool | None = None,
        inject_class: str = "",
        not_tested: list[str] | None = None,
        evidence_type: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules
        from app.services.ctem_inject_effects import checked_safe_mode

        klass = (inject_class or "").strip() or None
        untested = [str(i).strip() for i in (not_tested or []) if str(i).strip()]
        if untested:
            # #1458: an id with no template was not tested; no negative is ever recorded for it.
            note = ("not tested (no template): " + ", ".join(untested) + ". " + (note or "")).strip()
            if result in rules.PASSING_RESULTS:
                conclusive = False
        safe_mode = safe_mode if isinstance(safe_mode, bool) else None
        warning = None
        if safe_mode is True:
            safe_mode, warning = checked_safe_mode(safe_mode, klass)
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
                await case_loop.record_validation(
                    db,
                    shown[0],
                    provider=provider,
                    kind=kind,
                    ref=ref,
                    result=result,
                    mitigation_only=bool(mitigation_only),
                    note=note or None,
                    safe_mode=safe_mode,
                    run_order=[str(n) for n in run_order] if isinstance(run_order, list) else None,
                    conclusive=conclusive if isinstance(conclusive, bool) else None,
                    inject_class=klass,
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
                if warning:
                    answer["warning"] = warning
                return _dump(answer)
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case validation failed", exc_info=True)
            return "Error: the validation could not be recorded right now. Say so; nothing was changed."

    async def _assess(
        subject: str = "",
        injector: str = "",
        template: str = "",
        contract_read_only: bool | None = None,
        target_is_lab: bool = False,
        passive_option: str = "",
        templates: list[str] | None = None,
        requested_ids: list[str] | None = None,
        authored_payload: bool = False,
        command: str = "",
        target_count: int | None = None,
        inventory_count: int | None = None,
        inventory_label: str = "",
        remediation_kind: str = "",
        passive_reads_version: bool = False,
        version_check: str = "",
        draft_text: str = "",
    ) -> str:
        from app.services.ctem_inject_effects import (
            classify_inject,
            classify_templates,
            mobilize_done_criterion,
            template_coverage,
            validation_gate_ask,
            wording_problems,
        )

        texts = [str(t) for t in (templates or []) if str(t).strip()] + ([template] if template.strip() else [])
        flag = contract_read_only if isinstance(contract_read_only, bool) else None
        authored = bool(authored_payload) or bool((command or "").strip())
        effects = (
            classify_templates(texts)
            if texts and not authored
            else classify_inject(
                injector=injector, template=None, contract_read_only=flag, authored_payload=authored, command=command
            )
        )
        count = target_count if isinstance(target_count, int) and not isinstance(target_count, bool) else None
        cited = inventory_count if isinstance(inventory_count, int) and not isinstance(inventory_count, bool) else None
        wanted = [str(i) for i in (requested_ids or []) if str(i).strip()]
        coverage = template_coverage(texts, wanted) if (texts or wanted) else None
        ask = validation_gate_ask(
            effects,
            subject=(subject or "proposed").strip()[:200],
            target_is_lab=bool(target_is_lab),
            passive_option=passive_option or "",
            coverage=coverage,
            target_count=count,
            inventory_count=cited,
            inventory_label=inventory_label or "",
            remediation_kind=remediation_kind or "",
            passive_reads_version=bool(passive_reads_version),
        )
        body: dict[str, Any] = {
            "effects": effects.to_dict(),
            "gate_ask": ask,
            "mobilize": mobilize_done_criterion(
                effects, remediation_kind=remediation_kind or "", target_count=count, version_check=version_check
            ),
        }
        if coverage is not None:
            body["coverage"] = coverage.to_dict()
        problems = wording_problems(effects.classification, draft_text)
        if problems:
            body["draft_refused"] = problems
        return _dump(body)

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
                _hold(shown, "record its ticket")
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
                case = shown[0]
                _hold(shown, "decide its remediation")
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
                    item_id = await _park(case, parsed, recommendation, decision, await _currency(db, org_id))
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

    async def _tool_less_waits(db: AsyncSession) -> dict[str, bool]:
        """Whether each tool-less CTEM gate waits for the agent's home fleet (#1644).

        A gate the fleet does not declare is left out (nothing enforced for it), and so
        is every gate of an agent with no home fleet: the level and the pins are a
        fleet's own, and a case moved by a person is never held here.
        """
        from app.services import ctem_tool_less_gates as tool_less
        from app.services.fleet_autonomy import fleet_autonomy_for_run, gate_waits
        from app.services.fleet_runs import home_fleet_defaults_of

        homes = await home_fleet_defaults_of(db, [agent_id])
        home = next(iter(homes.values()), None)
        found = await fleet_autonomy_for_run(db, home.fleet_id) if home else None
        if not found:
            return {}
        level, gates = found
        return {str(g.get("key")): gate_waits(level, g) for g in gates if g.get("key") in tool_less.TOOL_LESS_GATES}

    def _hold(shown: Any, action: str) -> None:
        """Refuse *action* while a tool-less gate waits on the case unanswered (#1644)."""
        from app.services import ctem_tool_less_gates as tool_less

        tool_less.check_hold(tool_less.pending(shown[3]), action)

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

    async def _park(case: Any, parsed: list[Any], recommendation: str, decision: Any, currency: str = "") -> str:
        """One work item for a person with the unified decision ask; the existing one when it is still open."""
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
        from app.services import ctem_cases as rules
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
                try:
                    _hold(shown, "sync its ticket")
                except rules.CaseRuleError as exc:
                    return f"Refused: {exc}. Nothing was changed."
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

    executor = CallableToolExecutor()
    executor.register("list_ctem_cases", _list)
    executor.register("get_ctem_case", _get)
    executor.register("create_ctem_case", _create)
    executor.register("update_ctem_case", _update)
    executor.register("record_ctem_case_validation", _validation)
    executor.register("assess_ctem_validation_inject", _assess)
    executor.register("record_ctem_case_ticket", _ticket)
    executor.register("decide_ctem_case_remediation", _remediation)
    executor.register("sync_ctem_case_ticket", _sync_ticket)
    state_enum = {"type": "string", "enum": list(_STATES)}
    definitions = [
        ToolDefinition(
            name="list_ctem_cases",
            description=(
                "List the organization's CTEM cases (open exposures grouped by the fix that closes them), "
                "highest priority first, with the counts by state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": state_enum,
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "owner_user_id": {"type": "string", "description": "Only the cases this user owns."},
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
                "assets, crown jewels and its latest history."
            ),
            input_schema={
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
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
                "the decision, or the threat package of the review step. Those writes move the status; "
                "resolved needs evidence "
                "(a ticket, a change or a validation run); reopening needs a reason; accepted_risk is refused to "
                "an agent (a person accepts risk)."
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
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="record_ctem_case_validation",
            description=(
                "Write a validation or re-test result on a CTEM case: the validation module's simulation, "
                "scenario or test id and its result. A conclusive passed or not_exposed resolves the case "
                "(mitigation_only: mitigated, watching, the re-test stays on); a pass of an intrusive or unknown "
                "inject (inject_class from assess_ctem_validation_inject) is inconclusive unless conclusive is true "
                "(the fixed version proven): the case stays open. failed reopens a resolved case; pending only records."
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
                    "safe_mode": {
                        "type": "boolean",
                        "description": "True only when every inject that ran was classified read_only; else recorded false.",
                    },
                    "inject_class": {
                        "type": "string",
                        "enum": ["read_only", "intrusive", "unknown"],
                        "description": "The classification assess_ctem_validation_inject answered for the inject that ran.",
                    },
                    "not_tested": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Requested ids assess_ctem_validation_inject found no template for.",
                    },
                    "evidence_type": {
                        "type": "string",
                        "enum": ["remediation", "validation"],
                        "description": (
                            "remediation: a version inventory, every in-scope endpoint at or above the fixed "
                            "build; validation: a detection or behaviour test, never resolves a patch case."
                        ),
                    },
                    "conclusive": {
                        "type": "boolean",
                        "description": "True only when the check proves the exposure gone (the fixed version read).",
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
            name="assess_ctem_validation_inject",
            description=(
                "Read what a proposed validation inject does to its target before the gate "
                "ctem.validation.non_readonly: read_only, intrusive or unknown, the requests that change state, "
                "the cleanup, whether it needs credentials, what a no-match proves, the gate ask (title, safe "
                "mode, targets, evidence per option) and the mobilize done criterion built from it. Write the "
                "ask from this answer only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "What it tests, e.g. the CVE id."},
                    "injector": {"type": "string", "description": "The injector, e.g. nuclei."},
                    "template": {"type": "string", "description": "The Nuclei template's YAML, whole."},
                    "templates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Every selected template.",
                    },
                    "requested_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The CVE or template ids asked for; one with no template is not tested.",
                    },
                    "contract_read_only": {"type": "boolean", "description": "The contract's own flag, if any."},
                    "target_is_lab": {"type": "boolean", "description": "True for a lab asset only."},
                    "passive_option": {"type": "string", "description": "A read-only check to offer instead."},
                    "passive_reads_version": {"type": "boolean", "description": "The passive check reads versions."},
                    "authored_payload": {"type": "boolean", "description": "A payload written for this run."},
                    "command": {"type": "string", "description": "The authored payload's exact command text."},
                    "target_count": {"type": "integer", "description": "Targets the inject runs on."},
                    "inventory_count": {"type": "integer", "description": "Targets the context cites."},
                    "inventory_label": {"type": "string", "description": "What it counts, e.g. workstations."},
                    "remediation_kind": {"type": "string", "description": "The case's remediation_kind."},
                    "version_check": {"type": "string", "description": "A non-intrusive re-test, e.g. a version read."},
                    "draft_text": {"type": "string", "description": "Your drafted gate title and text, checked."},
                },
                "required": ["subject"],
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
                        "description": "1 to 6 options; figures from the connected risk quantification, in the organization's currency.",
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
    ]
    return definitions, executor
