"""OpenCRQ risk treatment tools (read-only): what is planned and what was spent.

* ``list_opencrq_treatment_plans`` - treatment plans with their simulated
                                     loss reduction and return.
* ``get_opencrq_treatment_plan``   - one plan: control moves and the
                                     simulated reduction per risk scenario.
* ``list_opencrq_budget_reviews``  - budget reviews with the loss their spend
                                     avoided.
* ``get_opencrq_budget_review``    - one review: spend lines and return per
                                     basket.

ALE is the annualized loss expectancy a CRQ simulation prices. A budget
review prices loss avoided instead: what the controls a line funds protect
against, which a standing (run) cost does without moving ALE.
"""

from __future__ import annotations

import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    OFFSET_PROP,
    ORGANIZATION_SCOPE_PROP,
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

    executor.register("list_opencrq_treatment_plans", list_opencrq_treatment_plans)
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
    ]
