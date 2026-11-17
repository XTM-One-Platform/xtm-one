"""OpenCRQ risk dashboard tool (read-only).

* ``get_opencrq_risk_dashboard`` - the risk picture of a tenant or an
  organization: priced exposure per risk scenario and its trend, scenarios
  outside their risk tolerance, the control upgrades that reduce it most, and
  the most active threats.
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
    _client_only_hint,
    _fmt_usd,
    _grc_link,
    _organization_scope,
    _page,
    _resolve_tenant,
    _scoped,
    _text,
    _trpc,
)
from .threats import _fmt_activity

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

_SCENARIOS_PAGE = 10
_TOP_SHOWN = 5


def _fmt_probability(value) -> str:
    if value is None:
        return "?"
    return f"{float(value) * 100:.3g}%"


def _priced_ale(s: dict) -> float | None:
    """The scenario's ALE, or ``None`` when OpenCRQ does not count it.

    A scenario whose scope maps no asset keeps its last run on the row, but
    OpenCRQ leaves it out of the total and reads it as not priced.
    """
    if s.get("noMappedAsset"):
        return None
    return (s.get("latestRun") or {}).get("aleMean")


def _fmt_scenario(s: dict) -> str:
    run = s.get("latestRun") or {}
    org = s.get("organizationName")
    label = f"{s.get('name', '?')}" + (f" ({org})" if org else "")
    if s.get("noMappedAsset"):
        return f"- {label}: no asset mapped, not priced"
    if run.get("aleMean") is None:
        return f"- {label}: not simulated yet"
    line = f"- {label}: ALE {_fmt_usd(run['aleMean'])}/yr"
    if run.get("aleP10") is not None and run.get("aleP90") is not None:
        line += f" (P10 {_fmt_usd(run['aleP10'])}, P90 {_fmt_usd(run['aleP90'])})"
    trend = s.get("trend") or {}
    # A ratio of the baseline ALE, not a percentage.
    if trend.get("deltaPct") is not None:
        delta = trend["deltaPct"]
        line += (
            ", stable over the window"
            if abs(delta) < 0.005
            else f", {delta * 100:+.0f}% over the window"
        )
    return line


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def get_opencrq_risk_dashboard(
        organization_id: str = "",
        window_days: int = 90,
        offset: int = 0,
        tenant_id: str = "",
    ) -> str:
        organization_id = _text(organization_id)
        window_days, offset = _as_int(window_days), _as_int(offset)
        if window_days is None or not 1 <= window_days <= 366:
            return "window_days must be an integer between 1 and 366."
        if offset is None or offset < 0:
            return "offset must be an integer >= 0."
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
            scenarios, err = await _trpc(
                base_url, headers, "dashboard.getScenarioSummaries",
                "get risk scenario summaries",
                input=_scoped({"windowDays": window_days}, organization_id),
                tenant_id=tenant, client=client,
            )
            if err:
                return err
            gaps, gaps_err = await _trpc(
                base_url, headers, "dashboard.getToleranceGaps",
                "get risk tolerance gaps", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )
            controls, controls_err = await _trpc(
                base_url, headers, "dashboard.getTopControls",
                "get top controls", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )
            threats, threats_err = await _trpc(
                base_url, headers, "dashboard.getMostActiveThreats",
                "get most active threats", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )

        scenarios = scenarios or []
        lines = [f"Risk dashboard ({scope}, last {window_days} days):"]
        link = _grc_link(
            base_url, tenant, "dashboard", "risk", organization_id=organization_id or None
        )
        if link:
            lines.append(link)
        priced = [ale for ale in map(_priced_ale, scenarios) if ale is not None]
        lines.append(
            f"Total exposure: {_fmt_usd(sum(priced))}/yr over {len(priced)} priced of "
            f"{len(scenarios)} risk scenarios."
        )
        if scenarios:
            ranked = sorted(
                scenarios,
                key=lambda s: (_priced_ale(s) is not None, _priced_ale(s) or 0),
                reverse=True,
            )
            shown, more = _page(ranked, offset, _SCENARIOS_PAGE)
            lines.append("\nRisk scenarios by annualized loss (ALE):")
            lines += [_fmt_scenario(s) for s in shown]
            if more:
                lines.append(more)

        if gaps_err:
            lines.append(f"\n(Risk tolerance unavailable: {gaps_err})")
        elif gaps:
            outside = [r for r in gaps.get("rows") or [] if r.get("outside")]
            lines.append(f"\nScenarios outside their risk tolerance: {len(outside)}")
            for r in outside:
                tolerance = r.get("tolerance") or {}
                lines.append(
                    f"- {r.get('name', '?')}: {_fmt_usd(r.get('loss'))} loss at "
                    f"{_fmt_probability(tolerance.get('maxExceedanceProbability'))} "
                    f"annual probability, above the "
                    f"{_fmt_usd(tolerance.get('annualLoss'))} the "
                    f"{r.get('source', '?')} tolerance accepts"
                )
            if gaps.get("noToleranceCount"):
                lines.append(
                    f"{gaps['noToleranceCount']} scenarios have no risk tolerance set."
                )

        if controls_err:
            lines.append(f"\n(Top controls unavailable: {controls_err})")
        elif controls:
            lines.append("\nControl upgrades that reduce exposure most:")
            for c in controls[:_TOP_SHOWN]:
                lines.append(
                    f"- {c.get('externalId', '')} {c.get('name', '?')}: improve "
                    f"{c.get('suggestedDimension', '?')}, ALE reduction "
                    f"{_fmt_usd(c.get('totalAleReduction'))}/yr across "
                    f"{c.get('scenarioCount', 0)} scenarios"
                )

        if threats_err:
            lines.append(f"\n(Active threats unavailable: {_client_only_hint(threats_err)})")
        elif threats:
            lines.append("\nMost active threats:")
            for t in threats[:_TOP_SHOWN]:
                lines.append(
                    f"- {t.get('name', '?')} ({t.get('entityType', '?')}): "
                    f"{_fmt_activity(t)}, relevant to {t.get('scenarioCount', 0)} "
                    f"scenarios (id: {t.get('id')})"
                )
        return "\n".join(lines)

    executor.register("get_opencrq_risk_dashboard", get_opencrq_risk_dashboard)

    return [
        ToolDefinition(
            name="get_opencrq_risk_dashboard",
            description=(
                "Get the OpenCRQ risk dashboard of a tenant or organization: "
                "total priced exposure, risk scenarios by annualized loss (ALE) "
                f"with their trend ({_SCENARIOS_PAGE} at a time), scenarios "
                "outside their risk tolerance, the control upgrades that reduce "
                "exposure most, and the most active threats. The active threats "
                "need a user session (a service account is refused)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **ORGANIZATION_SCOPE_PROP,
                    "window_days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 366,
                        "default": 90,
                        "description": "Days back the ALE trend compares to.",
                    },
                    **OFFSET_PROP,
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
    ]
