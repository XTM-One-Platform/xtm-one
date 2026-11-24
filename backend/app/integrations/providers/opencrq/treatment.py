"""OpenCRQ risk treatment tools: what is planned and what was spent.

* ``list_opencrq_treatment_plans`` - treatment plans with their simulated
                                     loss reduction and return.
* ``get_opencrq_treatment_plan``   - one plan: control moves and the
                                     simulated reduction per risk scenario.
* ``list_opencrq_budget_reviews``  - budget reviews with the loss their spend
                                     avoided.
* ``get_opencrq_budget_review``    - one review: spend lines and return per
                                     basket.

Writes (a remediation decision recorded in OpenCRQ, CTEM-18), each through
the procedures OpenCRQ already serves and with the acting user's own token,
so a user OpenCRQ refuses gets that refusal and nothing is written for them:

* ``create_opencrq_treatment_plan``   - a plan with its owner, dates, costs,
                                        rationale and first control moves.
* ``update_opencrq_treatment_plan``   - the plan's own fields.
* ``set_opencrq_treatment_plan_moves`` - replace the plan's control moves.
* ``add_opencrq_treatment_plan_moves`` - add or retarget control moves.

OpenCRQ re-prices the plan by itself after a change of its moves.

ALE is the annualized loss expectancy a CRQ simulation prices. A budget
review prices loss avoided instead: what the controls a line funds protect
against, which a standing (run) cost does without moving ALE.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from datetime import date
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    COVERAGE_BUCKETS,
    OFFSET_PROP,
    ORGANIZATION_SCOPE_PROP,
    STRENGTH_RUNGS,
    TENANT_PROP,
    _as_int,
    _client,
    _fmt_usd,
    _grc_link,
    _organization_scope,
    _page,
    _resolve_tenant,
    _scoped,
    _text,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

_PAGE_SIZE = 20

#: How a budget review figure reads when it is not priced on the review's current version.
_FIGURE_STATES = {
    "expired": "out of date: priced on an earlier version of the review",
    "not-priced": "partial: some risk scenarios are not priced yet",
}


def _pct(value) -> str:
    return "n/a" if value is None else f"{float(value):.0f}%"


def _per_year(mean, p10=None, p90=None) -> str:
    if mean is None:
        return "not priced"
    text = f"{_fmt_usd(mean)}/yr"
    if p10 is not None and p90 is not None:
        text += f" (P10 {_fmt_usd(p10)}, P90 {_fmt_usd(p90)})"
    return text


def _owner(entity: dict) -> str:
    owner = entity.get("owner") or {}
    return owner.get("name") or ""


#: The plan fields the create and update tools take, tool argument to OpenCRQ key.
_PLAN_TEXT_FIELDS = {"name": "name", "rationale": "rationale"}
_PLAN_DATE_FIELDS = {"start_date": "startDate", "end_date": "endDate"}
_PLAN_COST_FIELDS = {"cost_one_off": "costOneOff", "cost_recurring_annual": "costRecurringAnnual"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _plan_values(arguments: dict[str, Any], *, partial: bool) -> tuple[dict[str, Any], str | None]:
    """The OpenCRQ plan values of *arguments*, or the refusal of the first one it cannot send.

    ``partial`` (an update): only the arguments given are sent, an empty one
    clears a nullable field. A create sends nothing it was not given.
    """
    values: dict[str, Any] = {}
    for arg, key in _PLAN_TEXT_FIELDS.items():
        value = arguments.get(arg)
        if value is None:
            continue
        text = _text(value)
        if key == "name":
            if not text:
                return {}, "name cannot be empty."
            values[key] = text[:300]
        elif text or partial:
            values[key] = text or None
    owner = arguments.get("owner_id")
    if owner is not None:
        owner = _text(owner)
        if owner and not _is_uuid(owner):
            return {}, "owner_id must be an OpenCRQ user UUID."
        if owner or partial:
            values["ownerId"] = owner or None
    for arg, key in _PLAN_DATE_FIELDS.items():
        value = arguments.get(arg)
        if value is None:
            continue
        text = _text(value)
        if text and not _valid_date(text):
            return {}, f"{arg} must be a date written YYYY-MM-DD."
        if text or partial:
            values[key] = text or None
    for arg, key in _PLAN_COST_FIELDS.items():
        value = arguments.get(arg)
        if value is None or (isinstance(value, str) and not value.strip()):
            if partial and value is not None:
                values[key] = None
            continue
        amount = _amount(value)
        if amount is None or amount < 0:
            return {}, f"{arg} must be a number of at least 0."
        values[key] = amount
    start, end = values.get("startDate"), values.get("endDate")
    if start and end and end < start:
        return {}, "end_date cannot be before start_date."
    return values, None


def _valid_date(text: str) -> bool:
    if not _DATE.match(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _amount(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) else None


def _is_uuid(text: str) -> bool:
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


def _moves(raw: Any, *, required: bool) -> tuple[list[dict[str, Any]], str | None]:
    """OpenCRQ control moves from the tool's ``moves``, or the refusal of the first wrong one.

    A move names one control capability and its target strength rung and
    coverage bucket; a target left out keeps that axis as it is (``null``).
    """
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return [], "moves must be a list of {capability_id, target_strength, target_coverage}."
    moves: dict[str, dict[str, Any]] = {}
    for i, move in enumerate(raw, start=1):
        if not isinstance(move, dict):
            return [], f"move {i} must be an object with capability_id."
        capability = _text(move.get("capability_id"))
        if not _is_uuid(capability):
            return [], f"move {i}: capability_id must be a control capability UUID."
        targets: dict[str, str | None] = {}
        for arg, key, allowed in (
            ("target_strength", "targetStrengthRung", STRENGTH_RUNGS),
            ("target_coverage", "targetCoverageBucket", COVERAGE_BUCKETS),
        ):
            value = _text(move.get(arg)).upper()
            if value and value not in allowed:
                return [], f"move {i}: {arg} must be one of {', '.join(allowed)}."
            targets[key] = value or None
        if not any(targets.values()):
            return [], f"move {i}: give target_strength, target_coverage or both."
        moves[capability] = {"capabilityId": capability, **targets}
    if required and not moves:
        return [], "moves needs at least one move."
    return list(moves.values()), None


_PLAN_FIELD_PROPS: dict[str, Any] = {
    "owner_id": {"type": "string", "description": "OpenCRQ user UUID of the plan owner."},
    "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD."},
    "end_date": {"type": "string", "description": "End date, YYYY-MM-DD."},
    "rationale": {"type": "string", "description": "Why this plan: the decision it records."},
    "cost_one_off": {"type": "number", "description": "One-off cost, in the tenant currency."},
    "cost_recurring_annual": {"type": "number", "description": "Recurring cost per year."},
}

_MOVES_PROP: dict[str, Any] = {
    "type": "array",
    "description": "Control moves: a control capability and its target strength and/or coverage (L0 to L3).",
    "items": {
        "type": "object",
        "properties": {
            "capability_id": {"type": "string", "description": "Control capability UUID."},
            "target_strength": {"type": "string", "enum": STRENGTH_RUNGS},
            "target_coverage": {"type": "string", "enum": COVERAGE_BUCKETS},
        },
        "required": ["capability_id"],
    },
}


def _fmt_moves(moves: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{m['capabilityId']} to strength {m['targetStrengthRung'] or 'unchanged'}, "
        f"coverage {m['targetCoverageBucket'] or 'unchanged'}"
        for m in moves
    )


def _offset(value) -> tuple[int | None, str | None]:
    offset = _as_int(value)
    if offset is None or offset < 0:
        return None, "offset must be an integer >= 0."
    return offset, None


def _fmt_plan_row(p: dict, base_url: str, tenant_id: str) -> str:
    org = (p.get("organization") or {}).get("name") or "?"
    line = (
        f"- {p.get('name', '?')} [{p.get('status', '?')}] in {org}: "
        f"reduces ALE by {_per_year(p.get('totalAleReduction'))}"
    )
    if p.get("costYear1") is not None:
        line += f", year-1 cost {_fmt_usd(p['costYear1'])}, return {_pct(p.get('returnPercent'))}"
    if p.get("expired"):
        line += " (figures out of date, re-simulation needed)"
    line += f" (id: {p.get('id')})"
    link = _grc_link(base_url, tenant_id, "treatment_plan", p.get("id"))
    return f"{line}\n{link}" if link else line


def _fmt_target(move: dict) -> str:
    targets = [
        f"{label} {move[key]}"
        for key, label in (
            ("targetStrengthRung", "strength"),
            ("targetCoverageBucket", "coverage"),
        )
        if move.get(key)
    ]
    return ", ".join(targets) or "no target"


def _fmt_current(c: dict) -> str:
    return (
        f"strength {c.get('currentStrengthRung', '?')}, coverage "
        f"{c.get('currentCoverageBucket', '?')}"
    )


def _fmt_review_row(r: dict, base_url: str, tenant_id: str) -> str:
    org = (r.get("organization") or {}).get("name") or "?"
    line = (
        f"- {r.get('name', '?')} [{r.get('status', '?')}] in {org}, "
        f"{r.get('periodStart', '?')} to {r.get('periodEnd', '?')}: spent "
        f"{_fmt_usd(r.get('spent'))} on {r.get('lineCount', 0)} lines, loss "
        f"avoided {_per_year(r.get('aleReductionMean'))}, return "
        f"{_pct(r.get('returnPercent'))}"
    )
    if r.get("state") in _FIGURE_STATES:
        line += f" ({_FIGURE_STATES[r['state']]})"
    line += f" (id: {r.get('id')})"
    link = _grc_link(base_url, tenant_id, "budget_review", r.get("id"))
    return f"{line}\n{link}" if link else line


def _fmt_figure(f: dict, line_names: dict) -> str:
    basket = f.get("basket", "?")
    label = (
        f"line {line_names.get(f.get('lineId')) or f.get('lineId')}"
        if basket == "line"
        else basket
    )
    text = (
        f"    - {label}: spent {_fmt_usd(f.get('spent'))}, loss avoided "
        f"{_per_year(f.get('aleReductionMean'), f.get('aleReductionP10'), f.get('aleReductionP90'))}"
        f", return {_pct(f.get('returnPercent'))}"
    )
    # Amortizing changes nothing when the yearly spend is the whole spend.
    if f.get("amortizedReturnPercent") is not None and f.get("annualSpent") != f.get("spent"):
        text += f" (amortized {_pct(f['amortizedReturnPercent'])})"
    if f.get("state") in _FIGURE_STATES:
        text += f" [{_FIGURE_STATES[f['state']]}]"
    return text


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_opencrq_treatment_plans(
        organization_id: str = "", offset: int = 0, tenant_id: str = ""
    ) -> str:
        organization_id = _text(organization_id)
        offset, err = _offset(offset)
        if err:
            return err
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            scope, err = await _organization_scope(
                base_url, headers, tenant, organization_id, client=client
            )
            if err:
                return err
            plans, err = await _trpc(
                base_url, headers, "treatmentPlans.getTreatmentPlans",
                "list treatment plans", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )
            if err:
                return err
        plans = plans or []
        if not plans:
            return f"No risk treatment plan ({scope})."
        shown, more = _page(plans, offset, _PAGE_SIZE)
        lines = [f"Risk treatment plans ({scope}, {len(plans)}):"]
        lines += [_fmt_plan_row(p, base_url, tenant) for p in shown]
        if more:
            lines.append(more)
        return "\n".join(lines)

    async def get_opencrq_treatment_plan(
        treatment_plan_id: str = "", offset: int = 0, tenant_id: str = ""
    ) -> str:
        treatment_plan_id = _text(treatment_plan_id)
        if not treatment_plan_id:
            return "treatment_plan_id is required."
        offset, err = _offset(offset)
        if err:
            return err
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            plan, err = await _trpc(
                base_url, headers, "treatmentPlans.getTreatmentPlan",
                "get treatment plan", input={"id": treatment_plan_id},
                tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not plan:
                return f"Treatment plan {treatment_plan_id} not found."
            projection, projection_err = await _trpc(
                base_url, headers, "treatmentPlans.getTreatmentPlanProjection",
                "get treatment plan projection",
                input={"planId": treatment_plan_id}, tenant_id=tenant,
                client=client,
            )
            candidates, candidates_err = await _trpc(
                base_url, headers, "treatmentPlans.getTreatmentPlanCandidates",
                "get treatment plan controls",
                input={"planId": treatment_plan_id}, tenant_id=tenant,
                client=client,
            )

        org = plan.get("organization") or {}
        lines = [f"[Treatment plan] {plan.get('name', '?')}", f"  ID: {plan.get('id')}"]
        link = _grc_link(base_url, tenant, "treatment_plan", plan.get("id"))
        if link:
            lines.append(link)
        lines.append(f"  Status: {plan.get('status', '?')}")
        lines.append(f"  Organization: {org.get('name') or '?'} (id: {org.get('id')})")
        if _owner(plan):
            lines.append(f"  Owner: {_owner(plan)}")
        if plan.get("startDate") or plan.get("endDate"):
            lines.append(
                f"  Period: {plan.get('startDate') or '?'} to {plan.get('endDate') or '?'}"
            )
        costs = []
        if plan.get("costOneOff") is not None:
            costs.append(f"{_fmt_usd(plan['costOneOff'])} one-off")
        if plan.get("costRecurringAnnual") is not None:
            costs.append(f"{_fmt_usd(plan['costRecurringAnnual'])}/yr recurring")
        if costs:
            lines.append(f"  Cost: {', '.join(costs)}")
        if plan.get("promisedAleReduction") is not None:
            lines.append(
                f"  Promised ALE reduction: {_fmt_usd(plan['promisedAleReduction'])}/yr"
                + (f" (stamped {str(plan['promisedAt'])[:10]})" if plan.get("promisedAt") else "")
            )
        if plan.get("rationale"):
            lines.append(f"  Rationale: {str(plan['rationale'])[:500]}")

        names = {c.get("capabilityId"): c for c in candidates or []}
        moves = plan.get("moves") or []
        if moves:
            lines.append(f"  Control moves ({len(moves)}):")
            for m in moves:
                c = names.get(m.get("capabilityId")) or {}
                label = (
                    f"{c.get('externalId')} {c.get('name')}"
                    if c.get("name")
                    else f"control {m.get('capabilityId')}"
                )
                current = f" from {_fmt_current(c)}" if c else ""
                lines.append(f"    - {label}:{current} to {_fmt_target(m)}")
            if candidates_err:
                lines.append(f"    (Control names and current levels unavailable: {candidates_err})")
        else:
            lines.append("  No control move yet.")

        if projection_err:
            lines.append(f"  (Simulated reduction unavailable: {projection_err})")
        elif projection:
            lines.append(
                f"  Simulated ALE reduction: {_per_year(projection.get('totalAleReduction'))}"
                f" over {projection.get('pricedScenarioCount', 0)} of "
                f"{projection.get('scopeScenarioCount', 0)} risk scenarios priced"
            )
            if projection.get("costYear1") is not None:
                lines.append(
                    f"  Year-1 cost {_fmt_usd(projection['costYear1'])}, return "
                    f"{_pct(projection.get('returnPercent'))}"
                )
            if projection.get("expired"):
                lines.append(
                    "  These figures are out of date until the plan's risk "
                    "scenarios are simulated again."
                )
            scenarios = projection.get("scenarios") or []
            if scenarios:
                ranked = sorted(
                    scenarios,
                    key=lambda s: s.get("aleReductionMean") or 0,
                    reverse=True,
                )
                shown, more = _page(ranked, offset, _PAGE_SIZE)
                lines.append("  Per risk scenario:")
                for s in shown:
                    if s.get("state") == "not-priced":
                        figure = f"not priced ({s.get('readinessStatus', '?')})"
                    else:
                        figure = (
                            f"ALE {_fmt_usd(s.get('aleMean'))}/yr to "
                            f"{_fmt_usd(s.get('aleTargetMean'))}/yr, reduction "
                            f"{_per_year(s.get('aleReductionMean'), s.get('aleReductionP10'), s.get('aleReductionP90'))}"
                        )
                        if s.get("state") == "expired":
                            figure += " (out of date)"
                    lines.append(f"    - {s.get('name', '?')}: {figure}")
                if more:
                    lines.append(f"  {more}")
        return "\n".join(lines)

    async def list_opencrq_budget_reviews(
        organization_id: str = "", offset: int = 0, tenant_id: str = ""
    ) -> str:
        organization_id = _text(organization_id)
        offset, err = _offset(offset)
        if err:
            return err
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            scope, err = await _organization_scope(
                base_url, headers, tenant, organization_id, client=client
            )
            if err:
                return err
            reviews, err = await _trpc(
                base_url, headers, "budgetReviews.getBudgetReviews",
                "list budget reviews", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )
            if err:
                return err
        reviews = reviews or []
        if not reviews:
            return f"No budget review ({scope})."
        shown, more = _page(reviews, offset, _PAGE_SIZE)
        lines = [f"Budget reviews ({scope}, {len(reviews)}):"]
        lines += [_fmt_review_row(r, base_url, tenant) for r in shown]
        if more:
            lines.append(more)
        return "\n".join(lines)

    async def get_opencrq_budget_review(
        budget_review_id: str = "", tenant_id: str = ""
    ) -> str:
        budget_review_id = _text(budget_review_id)
        if not budget_review_id:
            return "budget_review_id is required."
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            review, err = await _trpc(
                base_url, headers, "budgetReviews.getBudgetReview",
                "get budget review", input={"id": budget_review_id},
                tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not review:
                return f"Budget review {budget_review_id} not found."
            figures, figures_err = await _trpc(
                base_url, headers, "budgetReviews.getBudgetReviewFigures",
                "get budget review figures",
                input={"reviewId": budget_review_id}, tenant_id=tenant,
                client=client,
            )

        org = review.get("organization") or {}
        lines = [f"[Budget review] {review.get('name', '?')}", f"  ID: {review.get('id')}"]
        link = _grc_link(base_url, tenant, "budget_review", review.get("id"))
        if link:
            lines.append(link)
        lines.append(
            f"  Status: {review.get('status', '?')}, period "
            f"{review.get('periodStart', '?')} to {review.get('periodEnd', '?')}"
        )
        lines.append(f"  Organization: {org.get('name') or '?'} (id: {org.get('id')})")
        if _owner(review):
            lines.append(f"  Owner: {_owner(review)}")
        if review.get("context"):
            lines.append(f"  Context: {str(review['context'])[:500]}")

        review_lines = review.get("lines") or []
        line_names = {line.get("id"): line.get("name") for line in review_lines}
        if review_lines:
            lines.append(f"  Spend lines ({len(review_lines)}):")
            for line in review_lines:
                text = (
                    f"    - {line.get('name', '?')} ({line.get('nature', '?')}): "
                    f"{_fmt_usd(line.get('amountSpent'))}"
                )
                if line.get("amortizationYears"):
                    text += f", amortized over {line['amortizationYears']} years"
                if line.get("csfFunctions"):
                    text += f", CSF {'/'.join(line['csfFunctions'])}"
                if line.get("capabilityIds"):
                    text += f", {len(line['capabilityIds'])} controls"
                lines.append(text)

        if figures_err:
            lines.append(f"  (Return figures unavailable: {figures_err})")
        elif figures:
            lines.append(
                f"  Loss avoided and return (over {figures.get('scopeScenarioCount', 0)} "
                "risk scenarios):"
            )
            lines += [_fmt_figure(f, line_names) for f in figures.get("figures") or []]
            if figures.get("toRepriceCount"):
                lines.append(
                    f"  {figures['toRepriceCount']} risk scenarios need a new "
                    "simulation before these figures are current."
                )
        return "\n".join(lines)

    async def create_opencrq_treatment_plan(
        organization_id: str = "",
        name: str = "",
        owner_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        rationale: str | None = None,
        cost_one_off: float | None = None,
        cost_recurring_annual: float | None = None,
        moves: list[dict[str, Any]] | None = None,
        tenant_id: str = "",
    ) -> str:
        organization_id = _text(organization_id)
        if not _is_uuid(organization_id):
            return "organization_id must be the risk scope UUID (list_opencrq_organizations)."
        if not _text(name):
            return "name is required."
        values, err = _plan_values(
            {
                "name": name,
                "owner_id": owner_id,
                "start_date": start_date,
                "end_date": end_date,
                "rationale": rationale,
                "cost_one_off": cost_one_off,
                "cost_recurring_annual": cost_recurring_annual,
            },
            partial=False,
        )
        if err:
            return err
        planned, err = _moves(moves, required=False)
        if err:
            return err
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err:
            return err
        plan_id, err = await _trpc(
            base_url,
            headers,
            "treatmentPlans.createTreatmentPlan",
            "create treatment plan",
            input={**values, "organizationId": organization_id, **({"moves": planned} if planned else {})},
            mutation=True,
            tenant_id=tenant or "",
        )
        if err:
            return err
        text = f"Created treatment plan '{values['name']}' (id: {plan_id})."
        if planned:
            text += f" {len(planned)} control move(s); OpenCRQ re-prices the plan."
        # CTEM-18-UI (#1068): the link the case records with update_ctem_case treatment_plan.
        link = _grc_link(base_url, tenant or "", "treatment_plan", str(plan_id))
        return f"{text}\n{link}" if link else text

    async def update_opencrq_treatment_plan(
        treatment_plan_id: str = "",
        name: str | None = None,
        owner_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        rationale: str | None = None,
        cost_one_off: float | str | None = None,
        cost_recurring_annual: float | str | None = None,
        tenant_id: str = "",
    ) -> str:
        treatment_plan_id = _text(treatment_plan_id)
        if not _is_uuid(treatment_plan_id):
            return "treatment_plan_id must be the plan UUID."
        values, err = _plan_values(
            {
                "name": name,
                "owner_id": owner_id,
                "start_date": start_date,
                "end_date": end_date,
                "rationale": rationale,
                "cost_one_off": cost_one_off,
                "cost_recurring_annual": cost_recurring_annual,
            },
            partial=True,
        )
        if err:
            return err
        if not values:
            return "Provide at least one field to update."
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err:
            return err
        _, err = await _trpc(
            base_url,
            headers,
            "treatmentPlans.updateTreatmentPlan",
            "update treatment plan",
            input={"id": treatment_plan_id, "values": values},
            mutation=True,
            tenant_id=tenant or "",
        )
        if err:
            return err
        return f"Updated treatment plan {treatment_plan_id}: {', '.join(sorted(values))}."

    async def _write_moves(
        procedure: str,
        action: str,
        treatment_plan_id: str,
        moves: list[dict[str, Any]] | None,
        tenant_id: str,
        *,
        required: bool,
    ) -> tuple[list[dict[str, Any]], str | None]:
        treatment_plan_id = _text(treatment_plan_id)
        if not _is_uuid(treatment_plan_id):
            return [], "treatment_plan_id must be the plan UUID."
        planned, err = _moves(moves, required=required)
        if err:
            return [], err
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err:
            return [], str(err)
        _, err = await _trpc(
            base_url,
            headers,
            procedure,
            action,
            input={"planId": treatment_plan_id, "moves": planned},
            mutation=True,
            tenant_id=tenant or "",
        )
        return planned, (str(err) if err else None)

    async def set_opencrq_treatment_plan_moves(
        treatment_plan_id: str = "", moves: list[dict[str, Any]] | None = None, tenant_id: str = ""
    ) -> str:
        planned, err = await _write_moves(
            "treatmentPlans.setTreatmentPlanScope",
            "set treatment plan moves",
            treatment_plan_id,
            moves,
            tenant_id,
            required=False,
        )
        if err:
            return err
        if not planned:
            return f"Treatment plan {_text(treatment_plan_id)} now plans no control move."
        return (
            f"Treatment plan {_text(treatment_plan_id)} now plans {len(planned)} control move(s): "
            f"{_fmt_moves(planned)}. OpenCRQ re-prices the plan."
        )

    async def add_opencrq_treatment_plan_moves(
        treatment_plan_id: str = "", moves: list[dict[str, Any]] | None = None, tenant_id: str = ""
    ) -> str:
        planned, err = await _write_moves(
            "treatmentPlans.addTreatmentPlanMoves",
            "add treatment plan moves",
            treatment_plan_id,
            moves,
            tenant_id,
            required=True,
        )
        if err:
            return err
        return (
            f"Added {len(planned)} control move(s) to treatment plan {_text(treatment_plan_id)}: "
            f"{_fmt_moves(planned)}. OpenCRQ re-prices the plan."
        )

    executor.register("list_opencrq_treatment_plans", list_opencrq_treatment_plans)
    executor.register("create_opencrq_treatment_plan", create_opencrq_treatment_plan)
    executor.register("update_opencrq_treatment_plan", update_opencrq_treatment_plan)
    executor.register("set_opencrq_treatment_plan_moves", set_opencrq_treatment_plan_moves)
    executor.register("add_opencrq_treatment_plan_moves", add_opencrq_treatment_plan_moves)
    executor.register("get_opencrq_treatment_plan", get_opencrq_treatment_plan)
    executor.register("list_opencrq_budget_reviews", list_opencrq_budget_reviews)
    executor.register("get_opencrq_budget_review", get_opencrq_budget_review)

    return [
        ToolDefinition(
            name="list_opencrq_treatment_plans",
            description=(
                "List the OpenCRQ risk treatment plans (planned control "
                "upgrades) with their status, the annualized loss (ALE) "
                "reduction their simulation prices, year-1 cost and return, "
                f"{_PAGE_SIZE} at a time."
            ),
            input_schema={
                "type": "object",
                "properties": {**ORGANIZATION_SCOPE_PROP, **OFFSET_PROP, **TENANT_PROP},
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_treatment_plan",
            description=(
                "Get an OpenCRQ risk treatment plan: owner, period, cost, the "
                "control moves it plans (strength and coverage "
                "targets), and the simulated ALE reduction per risk scenario, "
                f"{_PAGE_SIZE} scenarios at a time. The control names and their "
                "current levels need a user session (a service account is "
                "refused)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "treatment_plan_id": {
                        "type": "string",
                        "description": "Treatment plan UUID.",
                    },
                    **OFFSET_PROP,
                    **TENANT_PROP,
                },
                "required": ["treatment_plan_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_budget_reviews",
            description=(
                "List the OpenCRQ budget reviews (security spend over a period) "
                "with what was spent, the loss per year its controls avoided and "
                f"the return, {_PAGE_SIZE} at a time."
            ),
            input_schema={
                "type": "object",
                "properties": {**ORGANIZATION_SCOPE_PROP, **OFFSET_PROP, **TENANT_PROP},
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_budget_review",
            description=(
                "Get an OpenCRQ budget review: its spend lines (build or run, "
                "amount, controls, CSF functions) and the loss avoided per year "
                "and return of each line, of the build and run lines, and of "
                "the whole. Loss avoided is what the funded controls protect "
                "against, not a drop in ALE."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "budget_review_id": {
                        "type": "string",
                        "description": "Budget review UUID.",
                    },
                    **TENANT_PROP,
                },
                "required": ["budget_review_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_treatment_plan",
            description=(
                "Create an OpenCRQ risk treatment plan in a risk scope: name, owner, "
                "period, costs, rationale and its first control moves. Use it to "
                "record a remediation decision; OpenCRQ prices the loss reduction. "
                "Outside a fleet gate, confirm with the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string", "description": "Risk scope (organization) UUID."},
                    "name": {"type": "string", "description": "Plan name."},
                    **_PLAN_FIELD_PROPS,
                    "moves": _MOVES_PROP,
                    **TENANT_PROP,
                },
                "required": ["organization_id", "name"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_treatment_plan",
            description=(
                "Update an OpenCRQ treatment plan's own fields (name, owner, period, "
                "costs, rationale). Only the fields given change; an empty value "
                "clears an optional one. Outside a fleet gate, confirm with the user first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "treatment_plan_id": {"type": "string", "description": "Treatment plan UUID."},
                    "name": {"type": "string", "description": "New plan name."},
                    **_PLAN_FIELD_PROPS,
                    **TENANT_PROP,
                },
                "required": ["treatment_plan_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="set_opencrq_treatment_plan_moves",
            description=(
                "Replace the control moves of an OpenCRQ treatment plan with the "
                "list given (an empty list removes them all). OpenCRQ re-prices the plan. "
                "Outside a fleet gate, confirm with the user first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "treatment_plan_id": {"type": "string", "description": "Treatment plan UUID."},
                    "moves": _MOVES_PROP,
                    **TENANT_PROP,
                },
                "required": ["treatment_plan_id", "moves"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="add_opencrq_treatment_plan_moves",
            description=(
                "Add control moves to an OpenCRQ treatment plan, or retarget a "
                "control it already plans. OpenCRQ re-prices the plan. "
                "Outside a fleet gate, confirm with the user first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "treatment_plan_id": {"type": "string", "description": "Treatment plan UUID."},
                    "moves": _MOVES_PROP,
                    **TENANT_PROP,
                },
                "required": ["treatment_plan_id", "moves"],
            },
            source=_SOURCE,
        ),
    ]
