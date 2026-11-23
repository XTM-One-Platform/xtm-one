"""OpenCRQ cyber risk quantification (CRQ) tools.

* ``run_opencrq_crq_simulation``     - enqueue a Monte Carlo simulation.
* ``get_opencrq_crq_run``            - latest run or run detail with scores.
* ``list_opencrq_crq_run_history``   - past simulation runs of a scenario.
* ``get_opencrq_crq_what_if``        - control upgrade what-if results.
* ``get_opencrq_tef_input``          - threat event frequency input.
* ``update_opencrq_tef_input``       - set threat event frequency.
* ``list_opencrq_loss_drivers``      - loss driver taxonomy.
* ``get_opencrq_loss_driver``        - loss driver detail.
* ``update_opencrq_loss_estimates``  - upsert per-driver loss estimates.
* ``get_opencrq_crq_settings``       - CRQ engine settings.
* ``update_opencrq_crq_settings``    - tune CRQ engine settings.
* ``get_opencrq_financial_profile``  - a risk scope's financial questionnaire.
* ``update_opencrq_financial_profile`` - answer or clear questions of it.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    TENANT_PROP,
    _client,
    _fmt_amount,
    _fmt_rate,
    _fmt_usd,
    _grc_link,
    _one_line,
    _organization_scope,
    _quarantined,
    _resolve_tenant,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

#: OpenCRQ's bounds on the CRQ engine settings (``crqIndicatorSettings``).
_MAX_ITERATIONS = 2000
_DECLARED_CRITICALITIES = ("low", "medium", "high", "very_high")
_UNKNOWN_EXPOSURE_COUNTS_AS = ("internal", "internet_facing")
_TEF_MODES = ("manual", "auto")

_ESTIMATE_ACTIONS = ["set", "clear", "revert"]


class _ProfileQuestion(NamedTuple):
    section: str
    name: str
    key: str
    kind: str
    label: str
    #: OpenCRQ's band values when this tool was written, shown to the model as a hint
    #: only: OpenCRQ refuses a value it does not accept and names the ones it does.
    values: tuple[str, ...] = ()


#: The financial profile questionnaire (``financialProfileAnswersSchema`` in OpenCRQ's
#: ``module/src/modules/financial-profiles/validation.ts``), in its order.
_PROFILE_QUESTIONS: tuple[_ProfileQuestion, ...] = (
    _ProfileQuestion("Header", "loaded_daily_labour_cost", "loadedDailyLabourCost", "usd", "Loaded daily labour cost"),
    _ProfileQuestion("Revenue and margin", "annual_revenue", "annualRevenue", "usd", "Annual revenue"),
    _ProfileQuestion("Revenue and margin", "gross_margin_pct", "grossMarginPct", "pct", "Gross margin"),
    _ProfileQuestion(
        "Revenue and margin", "operating_days_band", "operatingDaysBand", "band", "Operating days per year",
        ("about_250", "about_300", "continuous_365"),
    ),
    _ProfileQuestion("Revenue and margin", "seasonal_peak_pct", "seasonalPeakPct", "pct", "Seasonal peak uplift"),
    _ProfileQuestion(
        "Digital dependency", "it_dependent_revenue_pct", "itDependentRevenuePct", "pct",
        "Revenue that stops while systems are down",
    ),
    _ProfileQuestion(
        "Digital dependency", "time_to_revenue_impact_band", "timeToRevenueImpactBand", "band",
        "Delay before an outage costs money", ("under_6h", "6_12h", "12_24h", "24_48h", "over_48h"),
    ),
    _ProfileQuestion(
        "Digital dependency", "full_restore_band", "fullRestoreBand", "band", "Time to full restore, backups intact",
        ("under_1d", "1_3d", "3_7d", "1_2w", "over_2w", "unknown"),
    ),
    _ProfileQuestion(
        "Digital dependency", "revenue_recapture_band", "revenueRecaptureBand", "band",
        "Revenue recaptured after an outage", ("mostly_lost", "partly_deferred", "mostly_deferred"),
    ),
    _ProfileQuestion(
        "Digital dependency", "restore_test_recency", "restoreTestRecency", "band", "Last restore test",
        ("within_12m", "over_12m", "never"),
    ),
    _ProfileQuestion(
        "Data held", "records_held_gate", "recordsHeldGate", "bool",
        "Holds personal data beyond its own employees",
    ),
    _ProfileQuestion(
        "Data held", "individuals_band", "individualsBand", "band", "Individuals whose personal data is held",
        ("under_100k", "100k_500k", "500k_1m", "1m_5m", "over_5m"),
    ),
    _ProfileQuestion(
        "Data held", "card_transactions_band", "cardTransactionsBand", "band", "Card transactions per year",
        ("none", "under_500k", "500k_5m", "over_5m"),
    ),
    _ProfileQuestion(
        "Data held", "sensitive_records_band", "sensitiveRecordsBand", "band",
        "Sensitive records (health, biometric, financial identifiers)", ("none", "under_100k", "100k_1m", "over_1m"),
    ),
    _ProfileQuestion(
        "Data held", "largest_repository_band", "largestRepositoryBand", "band",
        "Individuals held in the largest single system", ("under_100k", "100k_500k", "500k_1m", "1m_5m", "over_5m"),
    ),
    _ProfileQuestion(
        "Regulatory", "regulatory_regime", "regulatoryRegime", "band", "Strictest regulatory regime that applies",
        ("none", "nis2_essential", "nis2_important", "dora", "other_sectoral"),
    ),
    _ProfileQuestion(
        "Customers and contracts", "customer_count_band", "customerCountBand", "band", "Customer count",
        ("under_1k", "1k_10k", "10k_100k", "100k_1m", "over_1m"),
    ),
    _ProfileQuestion("Customers and contracts", "b2c_revenue_pct", "b2cRevenuePct", "pct", "B2C share of revenue"),
    _ProfileQuestion(
        "Customers and contracts", "recurring_revenue_pct", "recurringRevenuePct", "pct", "Recurring share of revenue",
    ),
    _ProfileQuestion(
        "Customers and contracts", "contracted_revenue_band", "contractedRevenueBand", "band",
        "Share of revenue under SLA or breach-liability clauses", ("none", "under_25", "25_75", "over_75"),
    ),
    _ProfileQuestion(
        "Customers and contracts", "contractual_liability_cap_band", "contractualLiabilityCapBand", "band",
        "Contractual liability cap, as a share of that revenue", ("under_25", "25_100", "100_200", "over_200", "no_cap"),
    ),
    _ProfileQuestion(
        "Funds and IP", "payment_flow_daily_value_band", "paymentFlowDailyValueBand", "band",
        "Daily value of outbound electronic payments, in USD",("under_100k", "100k_500k", "500k_1m", "1m_5m", "over_5m"),
    ),
    _ProfileQuestion(
        "Funds and IP", "out_of_band_verification_gate", "outOfBandVerificationGate", "bool",
        "Out-of-band verification above an approval ceiling",
    ),
    _ProfileQuestion("Funds and IP", "out_of_band_threshold", "outOfBandThreshold", "usd", "Approval ceiling"),
    _ProfileQuestion(
        "Funds and IP", "rd_ip_value_band", "rdIpValueBand", "band", "R&D and IP value, as a share of revenue",
        ("none", "under_2", "2_8", "over_8"),
    ),
    _ProfileQuestion(
        "Extortion", "ransom_payment_posture", "ransomPaymentPosture", "bool",
        "Would pay a ransom",
    ),
    _ProfileQuestion("Extortion", "ransom_payment_ceiling", "ransomPaymentCeiling", "usd", "Ransom payment ceiling"),
    _ProfileQuestion(
        "Loss history", "worst_outage_band", "worstOutageBand", "band", "Worst outage, past three years",
        ("none", "under_1d", "1_3d", "3_7d", "1_2w", "over_2w"),
    ),
    _ProfileQuestion(
        "Loss history", "costliest_incident_band", "costliestIncidentBand", "band", "Costliest incident, past three years",
        ("none", "under_25k", "25k_100k", "100k_500k", "500k_2m", "over_2m"),
    ),
)

_PROFILE_QUESTION_BY_NAME = {q.name: q for q in _PROFILE_QUESTIONS}

_PROFILE_ANSWER_TYPES = {"usd": "number", "pct": "number", "bool": "boolean", "band": "string"}


def _fmt_profile_answer(question: _ProfileQuestion, value: object) -> str:
    if value is None:
        return "not answered"
    if question.kind == "usd":
        return _fmt_usd(value)
    if question.kind == "pct":
        return f"{value:g}%"
    if question.kind == "bool":
        return "yes" if value else "no"
    return _one_line(value, 60)


def _fmt_financial_profile(profile: dict, scope: str, link: str) -> str:
    lines = [f"Financial profile of {_one_line(scope, 200)}:"]
    if profile.get("updatedAt"):
        lines.append(f"  Updated: {str(profile['updatedAt'])[:10]}")
    if link:
        lines.append(link)
    section = ""
    for question in _PROFILE_QUESTIONS:
        if question.section != section:
            section = question.section
            lines.append(f"{section}:")
        answer = _fmt_profile_answer(question, profile.get(question.key))
        lines.append(f"  {question.label} ({question.name}): {answer}")
    return "\n".join(lines)


def _fmt_crq_settings(settings: dict) -> str:
    """The CRQ engine settings, one labelled line per setting OpenCRQ returns.

    ``crq-indicator-settings-router.ts`` (``CrqIndicatorSettingsSchema``); a
    key an older OpenCRQ does not return gets no line.
    """
    lines = ["CRQ engine settings:"]
    if "iterations" in settings:
        iterations = settings["iterations"]
        lines.append(
            "  Simulation iterations: "
            + (f"{iterations:,}" if isinstance(iterations, int) else "not set")
        )
    weights = [
        f"{label} {_fmt_rate(settings[key])}"
        for key, label in (
            ("exposureWeightInternetFacing", "internet-facing"),
            ("exposureWeightInternal", "internal"),
            ("exposureWeightUnknown", "unknown"),
        )
        if key in settings
    ]
    if weights:
        lines.append(f"  Exposure weights: {', '.join(weights)}")
    if "susceptibilityGamma" in settings:
        lines.append(
            f"  Susceptibility gamma: {_fmt_rate(settings['susceptibilityGamma'])} "
            "(how stacked control capabilities combine on a technique: 1 counts "
            "each fully, 0 only the strongest)"
        )
    if "measuredValidityDays" in settings:
        days = settings["measuredValidityDays"]
        lines.append(
            "  A test result counts as a measurement for: "
            + (f"{days} days" if days is not None else "no limit (window off)")
        )
    if "unknownCriticalityCountsAs" in settings:
        lines.append(
            f"  Unknown criticality counts as: {settings['unknownCriticalityCountsAs']}"
        )
    if "unknownExposureCountsAs" in settings:
        lines.append(f"  Unknown exposure counts as: {settings['unknownExposureCountsAs']}")
    return "\n".join(lines)


def _band(mean: float | None, p10: float | None, p90: float | None, fmt) -> str:
    """A mean framed by its P10 / P90, or ``not computed`` when the run carries none."""
    if mean is None:
        return "not computed"
    if p10 is None or p90 is None:
        return fmt(mean)
    return f"mean {fmt(mean)} (P10 {fmt(p10)} / P90 {fmt(p90)})"


def _fmt_run(run: dict) -> str:
    lines = [f"[CRQRun] id: {run.get('id', 'N/A')}"]
    if run.get("createdAt"):
        lines.append(f"  Created: {run['createdAt']}")
    if run.get("iterations") is not None:
        lines.append(f"  Iterations: {run['iterations']}")
    if run.get("vulnerabilityP50") is not None:
        lines.append(
            f"  Vulnerability: P5 {_fmt_rate(run.get('vulnerabilityP5'))} / "
            f"P50 {_fmt_rate(run['vulnerabilityP50'])} / "
            f"P95 {_fmt_rate(run.get('vulnerabilityP95'))}"
            + (
                f", mean {_fmt_rate(run['vulnerabilityMean'])}"
                if run.get("vulnerabilityMean") is not None
                else ""
            )
        )
    lines.append(
        "  LEF (loss event frequency): "
        + _band(run.get("lefMean"), run.get("lefP10"), run.get("lefP90"), _fmt_rate)
    )
    lines.append(
        "  ALE (annualized loss expectancy): "
        + _band(run.get("aleMean"), run.get("aleP10"), run.get("aleP90"), _fmt_amount)
    )
    if run.get("magnitudeMean") is not None:
        lines.append(
            "  Loss of one successful attack: "
            + _band(
                run["magnitudeMean"], run.get("magnitudeP10"),
                run.get("magnitudeP90"), _fmt_amount,
            )
        )
    if run.get("tefLikely") is not None:
        lines.append(
            f"  TEF: min {run.get('tefMin')} / likely {run.get('tefLikely')} "
            f"/ max {run.get('tefMax')}"
        )
    outcomes = [
        f"{label} {run[key]:.0%}"
        for key, label in (
            ("outcomeBlocked", "blocked"),
            ("outcomeEvicted", "evicted"),
            ("outcomeImpactDetected", "impact noticed"),
            ("outcomeImpactUndetected", "impact unnoticed"),
        )
        if run.get(key) is not None
    ]
    if outcomes:
        lines.append(f"  Attack outcomes: {', '.join(outcomes)}")
    if run.get("techniqueCount") is not None:
        lines.append(
            f"  Techniques: {run.get('coveredTechniqueCount', 0)} covered "
            f"of {run['techniqueCount']}"
        )
    return "\n".join(lines)


#: The figure families OpenCRQ recomputes alone (crqIndicators.enqueueFamilyRecompute), with what each writes.
FIGURE_FAMILIES: dict[str, str] = {
    "levers": "control upgrade what-ifs",
    "techniques": "technique values",
    "cves": "CVE gains",
}

_LEVERS_RECOMPUTE = 'run_opencrq_crq_simulation(risk_scenario_id, family="levers")'


def _empty_what_if_reason(latest: dict[str, Any] | None, err: str | None) -> str:
    """Why a scenario has no control what-if, read off its latest run (*latest*, or the read's *err*).

    OpenCRQ writes control what-ifs only in a run whose ``profile`` carries ``levers``: a full
    simulation or a levers recompute. The automatic price passes compute the baseline alone, so a
    scenario can hold many runs and no what-if; a run that computed the levers and wrote no row found
    no control acting on the scenario's techniques, which running again does not change.
    """
    if err:
        return (
            "No control what-if results for this risk scenario, and its latest run could not be read "
            f"to say why ({err}). Control what-ifs are computed by {_LEVERS_RECOMPUTE}."
        )
    if not latest:
        return (
            "No control what-if results: this risk scenario has no CRQ run yet. Start a full "
            "simulation with run_opencrq_crq_simulation, then read this again."
        )
    run = f"{latest.get('createdAt', 'undated')} (id: {latest.get('id')})"
    profile = latest.get("profile")
    if isinstance(profile, list) and "levers" in profile:
        return (
            f"No control what-if results: the latest run, {run}, computed the control what-ifs and "
            "found no control acting on this scenario's techniques (no scored control capability "
            "on them). Running again changes nothing until a control capability is assessed on "
            "these techniques (list_opencrq_control_capabilities)."
        )
    computed = (
        f"computed {', '.join(profile)} only"
        if isinstance(profile, list) and profile
        else "computed the baseline only"
        if isinstance(profile, list)
        else "does not say which figures it computed"
    )
    return (
        "No control what-if results: none is stored for this risk scenario. "
        f"The latest run, {run}, {computed} (the automatic price runs compute the baseline, "
        f"never the control what-ifs). Compute them with {_LEVERS_RECOMPUTE}, wait for the job "
        "(get_opencrq_job), then read this again."
    )


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def run_opencrq_crq_simulation(
        risk_scenario_id: str = "", family: str = "", tenant_id: str = ""
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        if family and family not in FIGURE_FAMILIES:
            return f"family must be one of {', '.join(FIGURE_FAMILIES)} (or empty for a full simulation)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        if family:
            what = f"the {family} figures ({FIGURE_FAMILIES[family]})"
            result, err = await _trpc(
                base_url,
                headers,
                "crqIndicators.enqueueFamilyRecompute",
                "enqueue CRQ family recompute",
                input={"riskScenarioId": risk_scenario_id, "family": family},
                mutation=True,
                tenant_id=tenant or "",
            )
        else:
            what = "a CRQ simulation"
            result, err = await _trpc(
                base_url,
                headers,
                "crqIndicators.enqueueSimulation",
                "enqueue CRQ simulation",
                input={"riskScenarioId": risk_scenario_id},
                mutation=True,
                tenant_id=tenant or "",
            )
        if err:
            return err
        job_id = (result or {}).get("jobId")
        read = "get_opencrq_crq_what_if" if family == "levers" else "get_opencrq_crq_run"
        return (
            f"Enqueued {what} for risk scenario {risk_scenario_id}"
            + (f" (job id: {job_id})" if job_id else "")
            + f". Check its status with get_opencrq_job, then read results with {read}."
        )

    async def get_opencrq_crq_run(
        risk_scenario_id: str = "", run_id: str = "", tenant_id: str = ""
    ) -> str:
        if not risk_scenario_id and not run_id:
            return "Provide risk_scenario_id (latest run) or run_id (specific run)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        if run_id:
            run, err = await _trpc(
                base_url, headers, "crqIndicators.getRunWithScores",
                "get CRQ run", input={"runId": run_id}, tenant_id=tenant,
            )
            if err:
                return err
            if not run:
                return f"CRQ run {run_id} not found."
            out = _fmt_run(run)
            values = run.get("techniqueWhatIfResults") or []
            if values:
                out += (
                    f"\n  Technique values ({len(values)}; ALE drop when the "
                    "technique never succeeds, then when its detection is "
                    "fully covered):"
                )
                for v in values[:20]:
                    out += (
                        f"\n    - {_one_line(v.get('externalId'), 20)} {_one_line(v.get('name'), 120)}: "
                        f"prevent {_fmt_amount(v.get('preventValueMean'))} / "
                        f"detect {_fmt_amount(v.get('detectValueMean'))}"
                    )
                if len(values) > 20:
                    out += f"\n    ... and {len(values) - 20} more."
                return _quarantined("", out, "CRQ run (technique names)")
            return out
        run, err = await _trpc(
            base_url, headers, "crqIndicators.getLatestRun",
            "get latest CRQ run",
            input={"riskScenarioId": risk_scenario_id}, tenant_id=tenant,
        )
        if err:
            return err
        if not run:
            return (
                "No CRQ simulation run recorded for this risk scenario. "
                "Start one with run_opencrq_crq_simulation."
            )
        return _fmt_run(run)

    async def list_opencrq_crq_run_history(
        risk_scenario_id: str = "", tenant_id: str = ""
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        runs, err = await _trpc(
            base_url, headers, "crqIndicators.getRunHistory",
            "get CRQ run history",
            input={"riskScenarioId": risk_scenario_id}, tenant_id=tenant,
        )
        if err:
            return err
        runs = runs or []
        if not runs:
            return "No CRQ runs recorded for this risk scenario."
        lines = [f"CRQ run history ({len(runs)}):"]
        for r in runs[:20]:
            lines.append(
                f"- {r.get('createdAt', 'undated')} (id: {r.get('id')}): "
                f"ALE mean {_fmt_amount(r.get('aleMean'))}, "
                f"LEF mean {_fmt_rate(r.get('lefMean'))}"
            )
        if len(runs) > 20:
            lines.append(f"... and {len(runs) - 20} more.")
        return "\n".join(lines)

    async def get_opencrq_crq_what_if(
        risk_scenario_id: str = "", tenant_id: str = ""
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            results, err = await _trpc(
                base_url, headers, "crqIndicators.getWhatIfResults",
                "get CRQ what-if results",
                input={"riskScenarioId": risk_scenario_id}, tenant_id=tenant,
                client=client,
            )
            if err:
                return err
            if not results:
                latest, latest_err = await _trpc(
                    base_url,
                    headers,
                    "crqIndicators.getLatestRun",
                    "get latest CRQ run",
                    input={"riskScenarioId": risk_scenario_id},
                    tenant_id=tenant or "",
                    client=client,
                )
                return _empty_what_if_reason(latest, latest_err)
            capabilities, names_err = await _trpc(
                base_url, headers, "controlCapabilities.getControlCapabilities",
                "list control capabilities", tenant_id=tenant, client=client,
            )
        # Without the names the rows still stand on their capability ids.
        names = {
            c.get("id"): f"{_one_line(c.get('externalId'), 40)} {_one_line(c.get('name'), 200)}"
            for c in ([] if names_err else capabilities or [])
        }
        results = sorted(
            results, key=lambda r: r.get("aleReductionMean") or 0, reverse=True
        )
        lines = []
        for r in results[:25]:
            moves = ", ".join(
                f"{axis} {r.get(f'current{axis.capitalize()}')} -> "
                f"{r.get(f'upgraded{axis.capitalize()}')}"
                for axis in ("strength", "coverage")
            )
            control_id = r.get("controlId", "unknown")
            label = (
                f"{names[control_id]} (id: {control_id})"
                if control_id in names
                else control_id
            )
            lines.append(
                f"- control {label} "
                f"({r.get('dimension', 'unknown')} upgrade): {moves}: "
                f"ALE reduction mean {_fmt_amount(r.get('aleReductionMean'))} "
                f"(P10 {_fmt_amount(r.get('aleReductionP10'))} / "
                f"P90 {_fmt_amount(r.get('aleReductionP90'))})"
                + (f" [no gain: {_one_line(r['zeroReason'], 120)}]" if r.get("zeroReason") else "")
            )
        return _quarantined(
            f"Control upgrade what-if results ({len(results)}, largest ALE reduction first):",
            "\n".join(lines),
            "CRQ what-if results (control capability names)",
            f"... and {len(results) - 25} more." if len(results) > 25 else "",
        )

    async def get_opencrq_tef_input(
        risk_scenario_id: str = "", tenant_id: str = ""
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        scenario = {"riskScenarioId": risk_scenario_id}
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            mode, err = await _trpc(
                base_url, headers, "crqIndicators.getTefMode", "get TEF mode",
                input=scenario, tenant_id=tenant, client=client,
            )
            if err:
                return err
            procedure, action = (
                ("crqIndicators.getAutoTefEstimate", "get automatic TEF estimate")
                if mode == "auto"
                else ("crqIndicators.getTefInput", "get TEF input")
            )
            tef, err = await _trpc(
                base_url, headers, procedure, action,
                input=scenario, tenant_id=tenant, client=client,
            )
        if err:
            return err
        if not tef:
            return (
                f"TEF of risk scenario {risk_scenario_id}: manual, with no value "
                "entered yet."
            )
        triplet = (
            f"min {_fmt_rate(tef.get('min'))} / likely {_fmt_rate(tef.get('likely'))} / "
            f"max {_fmt_rate(tef.get('max'))} events per year"
        )
        if mode == "auto":
            fallback = tef.get("fallbackReason")
            return (
                f"TEF of risk scenario {risk_scenario_id}: automatic estimate, {triplet}"
                + (f" (fallback: {str(fallback).replace('_', ' ')})" if fallback else "")
                + f", computed {str(tef.get('createdAt', '?'))[:10]}. Setting values "
                "with update_opencrq_tef_input switches the scenario to manual."
            )
        return (
            f"TEF of risk scenario {risk_scenario_id}: manual, {triplet} "
            f"(updated {str(tef.get('updatedAt', '?'))[:10]})."
        )

    async def update_opencrq_tef_input(
        risk_scenario_id: str = "",
        min_value: float | None = None,
        likely_value: float | None = None,
        max_value: float | None = None,
        mode: str = "manual",
        tenant_id: str = "",
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        mode = (mode or "manual").strip().lower()
        if mode not in _TEF_MODES:
            return "mode must be manual or auto."
        values = (min_value, likely_value, max_value)
        if mode == "auto":
            if any(v is not None for v in values):
                return "Omit the values with mode=auto: the scenario then uses the automatic estimate."
        else:
            if any(v is None for v in values):
                return "min_value, likely_value and max_value are required with mode=manual."
            if min_value < 0 or likely_value <= 0 or max_value <= 0:
                return (
                    "min_value must be >= 0 and likely_value / max_value must "
                    "be > 0."
                )
            if not min_value <= likely_value <= max_value:
                return "Values must satisfy min_value <= likely_value <= max_value."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            # Leaving auto copies the automatic estimate into the manual values,
            # so the switch goes first and the values land over it.
            _, err = await _trpc(
                base_url, headers, "crqIndicators.setTefMode", "set TEF mode",
                input={"riskScenarioId": risk_scenario_id, "tefMode": mode},
                mutation=True, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if mode == "auto":
                return (
                    f"Risk scenario {risk_scenario_id} uses the automatic TEF "
                    "estimate again."
                )
            _, err = await _trpc(
                base_url, headers, "crqIndicators.upsertTefInput",
                "update TEF input",
                input={
                    "riskScenarioId": risk_scenario_id,
                    "min": min_value,
                    "likely": likely_value,
                    "max": max_value,
                },
                mutation=True, tenant_id=tenant, client=client,
            )
        if err:
            return err
        return (
            f"Set the TEF of risk scenario {risk_scenario_id} by hand: min {min_value} / "
            f"likely {likely_value} / max {max_value}. The next CRQ run uses these values."
        )

    async def list_opencrq_loss_drivers(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        drivers, err = await _trpc(
            base_url, headers, "lossDrivers.getLossDrivers",
            "list loss drivers", tenant_id=tenant,
        )
        if err:
            return err
        drivers = drivers or []
        if not drivers:
            return "No loss drivers found."
        lines = [
            f"- {_one_line(d.get('name'), 200, 'unnamed')} "
            f"({_one_line(d.get('lossCategoryName'), 120, 'uncategorized')}, "
            f"id: {d.get('id')})"
            for d in drivers[:40]
        ]
        return _quarantined(
            f"Loss drivers ({len(drivers)}):",
            "\n".join(lines),
            "loss drivers",
            f"... and {len(drivers) - 40} more." if len(drivers) > 40 else "",
        )

    async def get_opencrq_loss_driver(
        loss_driver_id: str = "", tenant_id: str = ""
    ) -> str:
        if not loss_driver_id:
            return "loss_driver_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        driver, err = await _trpc(
            base_url, headers, "lossDrivers.getLossDriver",
            "get loss driver", input={"id": loss_driver_id}, tenant_id=tenant,
        )
        if err:
            return err
        if not driver:
            return f"Loss driver {loss_driver_id} not found."
        lines = [f"[LossDriver] {_one_line(driver.get('name'), 200, 'unnamed')}"]
        lines.append(f"  ID: {driver.get('id', 'N/A')}")
        link = _grc_link(base_url, tenant, "loss_driver", driver.get("id"))
        if link:
            lines.append(link)
        for key, label in (
            ("lossCategoryName", "Category"),
            ("dwellSensitivity", "Dwell sensitivity"),
            ("scalingVariable", "Scales with"),
        ):
            if driver.get(key):
                lines.append(f"  {label}: {_one_line(driver[key], 120)}")
        if driver.get("description"):
            lines.append(f"  Description: {_one_line(driver['description'], 300)}")
        return _quarantined("", "\n".join(lines), "loss driver")

    async def update_opencrq_loss_estimates(
        risk_scenario_id: str = "",
        estimates: list[dict] | None = None,
        tenant_id: str = "",
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        if not estimates:
            return (
                "estimates is required - a list of objects with "
                "loss_driver_id and min/likely/max values."
            )
        cleaned = []
        reverted: list[str] = []
        seen: set[str] = set()
        for e in estimates:
            if not isinstance(e, dict) or not e.get("loss_driver_id"):
                return "each estimate requires a loss_driver_id."
            driver_id = str(e["loss_driver_id"])
            if driver_id in seen:
                return "each loss_driver_id may appear only once."
            seen.add(driver_id)
            action = str(e.get("action") or "set").strip().lower()
            if action not in _ESTIMATE_ACTIONS:
                return "each estimate's action must be set, clear or revert."
            if action == "revert":
                reverted.append(driver_id)
                continue
            if action == "clear":
                cleaned.append({"lossDriverId": driver_id, "action": "clear"})
                continue
            values = {}
            for src, dst in (
                ("min_value", "minValue"),
                ("likely_value", "likelyValue"),
                ("max_value", "maxValue"),
            ):
                raw = e.get(src)
                if raw is None:
                    values[dst] = None
                    continue
                try:
                    values[dst] = float(raw)
                except (TypeError, ValueError):
                    return f"each estimate's {src} must be a number."
            known = [v for v in values.values() if v is not None]
            if any(v < 0 for v in known):
                return "estimate values must be >= 0."
            if known != sorted(known):
                return (
                    "each estimate must satisfy min_value <= likely_value "
                    "<= max_value (among the provided values)."
                )
            cleaned.append({"lossDriverId": driver_id, "action": "set", **values})
        # The write is guarded by a fingerprint of the rows read last: the
        # newest updatedAt and the row count (a deletion cannot lower the
        # newest date).
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            rows, err = await _trpc(
                base_url, headers,
                "scenarioLossEstimates.getScenarioLossEstimates",
                "list loss estimates",
                input={"riskScenarioId": risk_scenario_id}, tenant_id=tenant,
                client=client,
            )
            if err:
                return err
            rows = rows or []
            stamps = [r["updatedAt"] for r in rows if r.get("updatedAt")]
            _, err = await _trpc(
                base_url, headers,
                "scenarioLossEstimates.upsertScenarioLossEstimates",
                "update loss estimates",
                input={
                    "riskScenarioId": risk_scenario_id,
                    "estimates": cleaned,
                    "updatedAt": max(stamps) if stamps else None,
                    "rowCount": len(rows),
                    **({"revertLossDriverIds": reverted} if reverted else {}),
                },
                mutation=True, tenant_id=tenant, client=client,
            )
        if getattr(err, "reason", None) == "STALE_WRITE":
            return (
                f"Error: the loss estimates of risk scenario {risk_scenario_id} "
                "changed since they were read, so nothing was saved. Read them "
                "again (get_opencrq_financial_summary) and, with the user's "
                "confirmation, retry."
            )
        if err:
            return err
        cleared = sum(1 for c in cleaned if c["action"] == "clear")
        return (
            f"Set {len(cleaned) - cleared}, cleared {cleared} and reverted "
            f"{len(reverted)} loss estimate(s) for risk scenario {risk_scenario_id}."
        )

    async def get_opencrq_crq_settings(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        settings, err = await _trpc(
            base_url, headers, "crqIndicatorSettings.getCrqIndicatorSettings",
            "get CRQ settings", tenant_id=tenant,
        )
        if err:
            return err
        if not settings:
            return "No CRQ settings found for this tenant."
        return _fmt_crq_settings(settings)

    async def update_opencrq_crq_settings(
        iterations: int = 0,
        exposure_weight_internet_facing: float = 0,
        exposure_weight_internal: float = 0,
        susceptibility_gamma: float | None = None,
        measured_validity_days: int | None = None,
        unknown_criticality_counts_as: str = "",
        unknown_exposure_counts_as: str = "",
        tenant_id: str = "",
    ) -> str:
        payload: dict = {}
        if iterations:
            if not 1 <= iterations <= _MAX_ITERATIONS:
                return f"iterations must be between 1 and {_MAX_ITERATIONS}."
            payload["iterations"] = iterations
        for key, value in (
            ("exposureWeightInternetFacing", exposure_weight_internet_facing),
            ("exposureWeightInternal", exposure_weight_internal),
        ):
            if value:
                if value <= 0:
                    return "exposure weights must be positive numbers."
                payload[key] = value
        if susceptibility_gamma is not None:
            if not 0 <= susceptibility_gamma <= 1:
                return "susceptibility_gamma must be between 0 and 1."
            payload["susceptibilityGamma"] = susceptibility_gamma
        if measured_validity_days is not None:
            if measured_validity_days < 0:
                return "measured_validity_days must be >= 0 (0 turns the window off)."
            payload["measuredValidityDays"] = measured_validity_days or None
        if unknown_criticality_counts_as:
            if unknown_criticality_counts_as not in _DECLARED_CRITICALITIES:
                return "unknown_criticality_counts_as must be among: " + ", ".join(
                    _DECLARED_CRITICALITIES
                ) + "."
            payload["unknownCriticalityCountsAs"] = unknown_criticality_counts_as
        if unknown_exposure_counts_as:
            if unknown_exposure_counts_as not in _UNKNOWN_EXPOSURE_COUNTS_AS:
                return "unknown_exposure_counts_as must be internal or internet_facing."
            payload["unknownExposureCountsAs"] = unknown_exposure_counts_as
        if not payload:
            return "Provide at least one setting to update."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers,
            "crqIndicatorSettings.updateCrqIndicatorSettings",
            "update CRQ settings", input=payload, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return "Updated CRQ engine settings."

    async def get_opencrq_financial_profile(
        organization_id: str = "", tenant_id: str = ""
    ) -> str:
        if not organization_id:
            return "organization_id is required."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(base_url, headers, tenant_id, client=client)
            if err:
                return err
            scope, err = await _organization_scope(
                base_url, headers, tenant, organization_id, client=client
            )
            if err:
                return err
            profile, err = await _trpc(
                base_url, headers, "financialProfiles.getFinancialProfile",
                "get financial profile", input={"organizationId": organization_id},
                tenant_id=tenant, client=client,
            )
        if err:
            return err
        if not profile:
            return f"The financial profile of {_one_line(scope, 200)} has not been filled in yet."
        return _quarantined(
            "",
            _fmt_financial_profile(
                profile, scope, _grc_link(base_url, tenant, "organization", organization_id)
            ),
            "risk scope financial profile (scope name and answers)",
        )

    async def update_opencrq_financial_profile(
        organization_id: str = "",
        answers: dict | None = None,
        clear: list[str] | None = None,
        tenant_id: str = "",
    ) -> str:
        if not organization_id:
            return "organization_id is required."
        to_set = answers or {}
        if not isinstance(to_set, dict):
            return "answers must be an object mapping question names to their answers."
        to_clear = set(clear or [])
        if not to_set and not to_clear:
            return "Give at least one answer to set or one question to clear."
        unknown = sorted((to_set.keys() | to_clear) - _PROFILE_QUESTION_BY_NAME.keys())
        if unknown:
            return (
                f"Unknown financial profile question(s): {', '.join(unknown)}. The questions are: "
                + ", ".join(q.name for q in _PROFILE_QUESTIONS) + "."
            )
        both = sorted(to_set.keys() & to_clear)
        if both:
            return f"Both set and cleared: {', '.join(both)}. Give each question one change."
        # OpenCRQ replaces the whole questionnaire on save and requires every key, so
        # the questions this call does not name are sent back as they were read.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(base_url, headers, tenant_id, client=client)
            if err:
                return err
            scope, err = await _organization_scope(
                base_url, headers, tenant, organization_id, client=client
            )
            if err:
                return err
            current, err = await _trpc(
                base_url, headers, "financialProfiles.getFinancialProfile",
                "get financial profile", input={"organizationId": organization_id},
                tenant_id=tenant, client=client,
            )
            if err:
                return err
            merged = {q.key: (current or {}).get(q.key) for q in _PROFILE_QUESTIONS}
            for name, value in to_set.items():
                merged[_PROFILE_QUESTION_BY_NAME[name].key] = value
            for name in to_clear:
                merged[_PROFILE_QUESTION_BY_NAME[name].key] = None
            _, err = await _trpc(
                base_url, headers, "financialProfiles.saveFinancialProfile",
                "save financial profile",
                input={
                    "organizationId": organization_id,
                    "answers": {
                        "loadedDailyLabourCost": merged["loadedDailyLabourCost"],
                        "annualRevenue": merged["annualRevenue"],
                        "grossMarginPct": merged["grossMarginPct"],
                        "operatingDaysBand": merged["operatingDaysBand"],
                        "seasonalPeakPct": merged["seasonalPeakPct"],
                        "itDependentRevenuePct": merged["itDependentRevenuePct"],
                        "timeToRevenueImpactBand": merged["timeToRevenueImpactBand"],
                        "fullRestoreBand": merged["fullRestoreBand"],
                        "revenueRecaptureBand": merged["revenueRecaptureBand"],
                        "restoreTestRecency": merged["restoreTestRecency"],
                        "recordsHeldGate": merged["recordsHeldGate"],
                        "individualsBand": merged["individualsBand"],
                        "cardTransactionsBand": merged["cardTransactionsBand"],
                        "sensitiveRecordsBand": merged["sensitiveRecordsBand"],
                        "largestRepositoryBand": merged["largestRepositoryBand"],
                        "regulatoryRegime": merged["regulatoryRegime"],
                        "customerCountBand": merged["customerCountBand"],
                        "b2cRevenuePct": merged["b2cRevenuePct"],
                        "recurringRevenuePct": merged["recurringRevenuePct"],
                        "contractedRevenueBand": merged["contractedRevenueBand"],
                        "contractualLiabilityCapBand": merged["contractualLiabilityCapBand"],
                        "paymentFlowDailyValueBand": merged["paymentFlowDailyValueBand"],
                        "outOfBandVerificationGate": merged["outOfBandVerificationGate"],
                        "outOfBandThreshold": merged["outOfBandThreshold"],
                        "rdIpValueBand": merged["rdIpValueBand"],
                        "ransomPaymentPosture": merged["ransomPaymentPosture"],
                        "ransomPaymentCeiling": merged["ransomPaymentCeiling"],
                        "worstOutageBand": merged["worstOutageBand"],
                        "costliestIncidentBand": merged["costliestIncidentBand"],
                    },
                },
                mutation=True, tenant_id=tenant, client=client,
            )
        if err:
            return err
        changes = []
        if to_set:
            changes.append(f"set {', '.join(sorted(to_set))}")
        if to_clear:
            changes.append(f"cleared {', '.join(sorted(to_clear))}")
        return (
            f"Saved the financial profile of {_one_line(scope, 200)}: {'; '.join(changes)}. OpenCRQ "
            "re-derives the loss estimates of its risk scenarios from it."
        )

    executor.register("run_opencrq_crq_simulation", run_opencrq_crq_simulation)
    executor.register("get_opencrq_crq_run", get_opencrq_crq_run)
    executor.register("list_opencrq_crq_run_history", list_opencrq_crq_run_history)
    executor.register("get_opencrq_crq_what_if", get_opencrq_crq_what_if)
    executor.register("get_opencrq_tef_input", get_opencrq_tef_input)
    executor.register("update_opencrq_tef_input", update_opencrq_tef_input)
    executor.register("list_opencrq_loss_drivers", list_opencrq_loss_drivers)
    executor.register("get_opencrq_loss_driver", get_opencrq_loss_driver)
    executor.register("update_opencrq_loss_estimates", update_opencrq_loss_estimates)
    executor.register("get_opencrq_crq_settings", get_opencrq_crq_settings)
    executor.register("update_opencrq_crq_settings", update_opencrq_crq_settings)
    executor.register("get_opencrq_financial_profile", get_opencrq_financial_profile)
    executor.register("update_opencrq_financial_profile", update_opencrq_financial_profile)

    _scenario_prop = {
        "risk_scenario_id": {
            "type": "string",
            "description": "Risk scenario UUID.",
        }
    }
    _organization_prop = {
        "organization_id": {
            "type": "string",
            "description": (
                "Organization (risk scope) UUID (use list_opencrq_organizations to discover)."
            ),
        }
    }

    return [
        ToolDefinition(
            name="run_opencrq_crq_simulation",
            description=(
                "Enqueue a cyber risk quantification (CRQ) Monte Carlo "
                "simulation for an OpenCRQ risk scenario, or with family one "
                "figure family alone (levers: the control upgrade what-ifs "
                "get_opencrq_crq_what_if reads). Returns the "
                "background job id (check its status with get_opencrq_job). "
                "Confirm with the user before starting a simulation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_scenario_prop,
                    "family": {
                        "type": "string",
                        "enum": list(FIGURE_FAMILIES),
                        "description": (
                            "Recompute this figure family alone instead of a full "
                            "simulation: levers (control upgrade what-ifs), "
                            "techniques (technique values) or cves (CVE gains)."
                        ),
                    },
                    **TENANT_PROP,
                },
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_crq_run",
            description=(
                "Get a CRQ simulation run of an OpenCRQ risk scenario: pass "
                "risk_scenario_id for the latest run, or run_id for a "
                "specific run including the value of each technique. Returns "
                "vulnerability, LEF, the loss of one successful attack and "
                "ALE (mean with P10 / P90)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_scenario_prop,
                    "run_id": {
                        "type": "string",
                        "description": "Specific CRQ run UUID (with scores).",
                    },
                    **TENANT_PROP,
                },
                # At least one selector so pre-dispatch jsonschema
                # validation rejects an empty call before the tool
                # executes (the runtime check stays as defense in depth).
                "anyOf": [
                    {"required": ["risk_scenario_id"]},
                    {"required": ["run_id"]},
                ],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_crq_run_history",
            description=(
                "List the past CRQ simulation runs of an OpenCRQ risk "
                "scenario with their headline ALE and LEF means."
            ),
            input_schema={
                "type": "object",
                "properties": {**_scenario_prop, **TENANT_PROP},
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_crq_what_if",
            description=(
                "Get the control upgrade what-if results of an OpenCRQ risk "
                "scenario: how much upgrading each control's strength or "
                "coverage would reduce the ALE (useful for "
                "prioritizing remediation). An empty answer says why: no run "
                "computed them yet (compute them with run_opencrq_crq_simulation "
                "family levers) or no control acts on the scenario."
            ),
            input_schema={
                "type": "object",
                "properties": {**_scenario_prop, **TENANT_PROP},
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_tef_input",
            description=(
                "Get the threat event frequency (TEF) of an OpenCRQ risk "
                "scenario (min / likely / max events per year): the automatic "
                "estimate when the scenario is in auto mode, the default, or "
                "the values entered by hand when it is in manual mode."
            ),
            input_schema={
                "type": "object",
                "properties": {**_scenario_prop, **TENANT_PROP},
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_tef_input",
            description=(
                "Set the threat event frequency (TEF) of an OpenCRQ risk "
                "scenario by hand (min <= likely <= max, events per year), "
                "which switches it to manual mode so the next CRQ run uses "
                "these values, or put it back on the automatic estimate with "
                "mode=auto and no values. Confirm with the user before updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_scenario_prop,
                    "min_value": {"type": "number", "minimum": 0},
                    "likely_value": {"type": "number", "exclusiveMinimum": 0},
                    "max_value": {"type": "number", "exclusiveMinimum": 0},
                    "mode": {
                        "type": "string",
                        "enum": list(_TEF_MODES),
                        "default": "manual",
                        "description": (
                            "manual (default) to set the three values, auto to "
                            "return to the automatic estimate."
                        ),
                    },
                    **TENANT_PROP,
                },
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_loss_drivers",
            description=(
                "List the loss drivers of an OpenCRQ tenant (the financial "
                "impact taxonomy used by CRQ estimates) with their loss "
                "category."
            ),
            input_schema={"type": "object", "properties": {**TENANT_PROP}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_loss_driver",
            description=(
                "Get an OpenCRQ loss driver's detail including its loss "
                "category, dwell sensitivity and what it scales with."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "loss_driver_id": {
                        "type": "string",
                        "description": "Loss driver UUID.",
                    },
                    **TENANT_PROP,
                },
                "required": ["loss_driver_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_loss_estimates",
            description=(
                "Set or clear the per-loss-driver financial estimates of an "
                "OpenCRQ risk scenario. Each estimate has a loss_driver_id "
                "and min/likely/max dollar values (min <= likely <= max), or "
                "action=clear to delete that driver's estimate, or "
                "action=revert to give it back its figures derived from the "
                "financial profile; drivers not listed are left untouched. "
                "Confirm with the user before updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_scenario_prop,
                    "estimates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "loss_driver_id": {"type": "string"},
                                "action": {
                                    "type": "string",
                                    "enum": _ESTIMATE_ACTIONS,
                                    "default": "set",
                                },
                                "min_value": {"type": "number", "minimum": 0},
                                "likely_value": {"type": "number", "minimum": 0},
                                "max_value": {"type": "number", "minimum": 0},
                            },
                            "required": ["loss_driver_id"],
                        },
                    },
                    **TENANT_PROP,
                },
                "required": ["risk_scenario_id", "estimates"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_crq_settings",
            description=(
                "Get the OpenCRQ CRQ engine settings: simulation iterations, "
                "exposure weights, how stacked control capabilities combine, "
                "how long a test result counts as a measurement, and how an "
                "asset of unknown criticality or exposure counts."
            ),
            input_schema={"type": "object", "properties": {**TENANT_PROP}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_crq_settings",
            description=(
                "Update the OpenCRQ CRQ engine settings: simulation "
                "iterations, exposure weights, how stacked control "
                "capabilities combine, how long a test result counts as a "
                "measurement, and how an asset of unknown criticality or "
                "exposure counts. Settings you omit stay as they are. They "
                "affect the whole tenant's simulations and finding scores - "
                "confirm with the user before changing them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "iterations": {"type": "integer", "minimum": 1, "maximum": _MAX_ITERATIONS},
                    "exposure_weight_internet_facing": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                    },
                    "exposure_weight_internal": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                    },
                    "susceptibility_gamma": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "How stacked control capabilities combine on a "
                            "technique: 1 counts each fully, 0 only the strongest."
                        ),
                    },
                    "measured_validity_days": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Days a test result keeps counting as a measurement in "
                            "the finding score; 0 turns the window off."
                        ),
                    },
                    "unknown_criticality_counts_as": {
                        "type": "string",
                        "enum": list(_DECLARED_CRITICALITIES),
                        "description": "Criticality the finding score gives an asset of unknown criticality.",
                    },
                    "unknown_exposure_counts_as": {
                        "type": "string",
                        "enum": list(_UNKNOWN_EXPOSURE_COUNTS_AS),
                        "description": "Exposure an asset of unknown exposure counts as.",
                    },
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_financial_profile",
            description=(
                "Get the financial profile of an OpenCRQ risk scope: its answers to "
                "the financial questionnaire (revenue and margin, digital dependency, "
                "data held, regulatory regime, customers and contracts, funds and IP, "
                "extortion, loss history) that OpenCRQ derives the loss estimates of "
                "its risk scenarios from. Each answer is shown with its question name."
            ),
            input_schema={
                "type": "object",
                "properties": {**_organization_prop, **TENANT_PROP},
                "required": ["organization_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_financial_profile",
            description=(
                "Answer or clear questions of an OpenCRQ risk scope's financial "
                "profile. Questions not named keep their current answer, except that "
                "answering no to records_held_gate, out_of_band_verification_gate or "
                "ransom_payment_posture clears individuals_band, out_of_band_threshold "
                "or ransom_payment_ceiling respectively. Saving makes "
                "OpenCRQ re-derive the loss estimates of every risk scenario of the "
                "scope, so confirm the changes with the user before updating. Read "
                "the profile first with get_opencrq_financial_profile."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_organization_prop,
                    "answers": {
                        "type": "object",
                        "description": "The answers to set, by question name.",
                        "properties": {
                            q.name: {
                                "type": _PROFILE_ANSWER_TYPES[q.kind],
                                "description": q.label
                                + (f" (one of: {', '.join(q.values)})" if q.values else "")
                                + (" (USD)" if q.kind == "usd" else "")
                                + (" (0 to 100)" if q.kind == "pct" else ""),
                            }
                            for q in _PROFILE_QUESTIONS
                        },
                        "additionalProperties": False,
                    },
                    "clear": {
                        "type": "array",
                        "items": {"type": "string", "enum": [q.name for q in _PROFILE_QUESTIONS]},
                        "description": "Question names whose answer to remove.",
                    },
                    **TENANT_PROP,
                },
                "required": ["organization_id"],
            },
            source=_SOURCE,
        ),
    ]
