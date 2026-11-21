# SPDX-License-Identifier: Apache-2.0

"""CTEM reconcile and scope gate, the I/O side (#847).

Reads what :func:`app.services.ctem_scope_gate.decide` needs (the
organization profile, the other signals on the same subject, the OpenCRQ
risk scenarios when an administrator's connection can read them), runs it
and writes ``state`` and ``decision`` on the signal. No model call.

When it runs: after a signal is first recorded (API, agent tool, listener),
and on demand (``POST /api/v1/ctem-signals/{id}/evaluate`` and
``POST /api/v1/ctem-signals/evaluate`` for every open signal, after a policy
or criticality change: a watchlist signal re-enters the gate that way). A
signal that opened a case (``case_opened``) is never moved back. Design:
``dev-docs/ctem-scope-gate.md``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_signal import CtemSignal
from app.services import ctem_scope_gate as gate

logger = logging.getLogger(__name__)

#: The states the gate may move (``case_opened`` belongs to the case step).
GATED_STATES: tuple[str, ...] = ("new", "confirmed", "to_validate", "watchlist", "discarded")
#: Signals one bulk evaluation reads at most.
MAX_BULK = 2000
#: Risk scenarios read from OpenCRQ at most, and supporting assets per scenario.
MAX_SCENARIOS = 50
SCENARIO_ASSETS_PAGE = 500
#: OpenCRQ tolerance ratio (expected loss over tolerance) read as scope criticality, highest first.
_RATIO_CRITICALITY: tuple[tuple[float, str], ...] = ((1.0, "critical"), (0.5, "high"), (0.1, "medium"))


def facts_of(row: CtemSignal) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path=row.trigger_path,
        lens=row.lens,
        subject_kind=row.subject_kind,
        subject_ref=row.subject_ref,
        source_provider=row.source_provider,
        source_external_id=row.source_external_id,
        subject_label=row.subject_label,
        evidence=dict(row.evidence or {}),
    )


async def related_signals(db: AsyncSession, row: CtemSignal) -> list[gate.RelatedSignal]:
    """The organization's other signals on the same subject (any recorder, any path)."""
    statement = (
        select(CtemSignal)
        .where(
            CtemSignal.organization_id == row.organization_id,
            CtemSignal.id != row.id,
            CtemSignal.subject_kind == row.subject_kind,
            func.lower(CtemSignal.subject_ref) == row.subject_ref.lower(),
        )
        .limit(100)
    )
    others = (await db.execute(statement)).scalars().all()
    return [
        gate.RelatedSignal(
            id=str(o.id),
            lens=o.lens,
            source_provider=o.source_provider,
            verdict=str((o.evidence or {}).get("verdict") or "").strip().lower(),
        )
        for o in others
    ]


def _scenario_criticality(scenario: dict[str, Any]) -> str:
    """A scenario's weight on the scope scale: its loss against tolerance, else the default."""
    loss = scenario.get("annualLossExposure") or scenario.get("ale") or scenario.get("expectedLoss")
    tolerance = scenario.get("riskTolerance") or scenario.get("tolerance")
    try:
        ratio = float(loss) / float(tolerance) if loss is not None and tolerance else None
    except (TypeError, ValueError, ZeroDivisionError):
        ratio = None
    if ratio is None:
        return gate.SCENARIO_DEFAULT_CRITICALITY
    for floor, criticality in _RATIO_CRITICALITY:
        if ratio >= floor:
            return criticality
    return "low"


async def read_scenarios(db: AsyncSession, *, user_id: uuid.UUID | None) -> gate.ScenarioIndex:
    """The risk scenarios holding each supporting asset, read with an administrator's OpenCRQ connection.

    Never raises: a missing connection or a failed read answers an index
    with ``connected`` false and the reason, which the decision names.
    """
    if user_id is None:
        return gate.ScenarioIndex(note="OpenCRQ not read: an administrator re-evaluates to read risk scenarios")
    try:
        from app.integrations.providers.opencrq._common import _resolve_tenant, _trpc
        from app.services.organization_context_opencrq import _opencrq_connection

        connection = await _opencrq_connection(db, str(user_id))
        if isinstance(connection, str):
            return gate.ScenarioIndex(note=f"OpenCRQ not read: {connection}")
        base_url, headers = connection
        tenant, err = await _resolve_tenant(base_url, headers, "")
        if err:
            return gate.ScenarioIndex(note=f"OpenCRQ not read: {err}")
        scenarios, err = await _trpc(
            base_url,
            headers,
            "riskScenarios.getRiskScenariosOverview",
            "list risk scenarios",
            input={},
            tenant_id=tenant or "",
        )
        if err:
            return gate.ScenarioIndex(note=f"OpenCRQ not read: {err}")
        index = gate.ScenarioIndex(connected=True, note="")
        for scenario in (scenarios or [])[:MAX_SCENARIOS]:
            sid = str(scenario.get("id") or "")
            if not sid:
                continue
            ref = gate.RiskScenarioRef(
                id=sid, name=str(scenario.get("name") or sid)[:200], criticality=_scenario_criticality(scenario)
            )
            derived, err = await _trpc(
                base_url,
                headers,
                "riskScenarios.listDerivedSupportingAssets",
                "list derived supporting assets",
                input={
                    "riskScenarioId": sid,
                    "page": 1,
                    "limit": SCENARIO_ASSETS_PAGE,
                    "sortBy": "impact",
                    "sortDesc": True,
                },
                tenant_id=tenant or "",
            )
            if err:
                continue
            for item in (derived or {}).get("items") or []:
                if item.get("outOfScope"):
                    continue
                asset_id = str(item.get("supportingAssetId") or "")
                if asset_id:
                    index.by_asset_id.setdefault(asset_id, []).append(ref)
                for name in (item.get("supportingAssetName"), item.get("supportingAssetDerivedName")):
                    if name:
                        index.by_asset_name.setdefault(str(name).strip().lower(), []).append(ref)
        return index
    except Exception as exc:  # the gate never fails on OpenCRQ
        logger.warning("CTEM scope gate: OpenCRQ risk scenarios not read: %s", exc)
        return gate.ScenarioIndex(note="OpenCRQ not read: the read failed")


async def _profile(db: AsyncSession, organization_id: uuid.UUID) -> Any:
    from app.services import organization_context as org_context

    row = await org_context.current_version(db, organization_id)
    return org_context.parse_profile(row.profile if row else {})


async def evaluate(
    db: AsyncSession,
    row: CtemSignal,
    *,
    profile: Any = None,
    scenarios: gate.ScenarioIndex | None = None,
    now: datetime | None = None,
) -> gate.Decision | None:
    """Decide *row* and write its state and decision; ``None`` when it opened a case. The caller commits."""
    if row.state not in GATED_STATES:
        return None
    at = now or datetime.now(UTC)
    profile = profile if profile is not None else await _profile(db, row.organization_id)
    decision = gate.decide(
        profile,
        facts_of(row),
        related=await related_signals(db, row),
        scenarios=scenarios,
        today=at.date(),
    )
    decision.record["decided_at"] = at.isoformat()
    row.state = decision.state
    row.decision = decision.record
    row.updated_at = at
    return decision


async def evaluate_after_record(db: AsyncSession, row: CtemSignal, *, created: bool) -> None:
    """The gate on a signal just recorded; a failure leaves it ``new`` for an on-demand pass, never refuses it."""
    if not created:
        return
    try:
        async with db.begin_nested():
            await evaluate(db, row)
    except Exception:
        logger.warning("CTEM scope gate not run on signal %s; it stays new", getattr(row, "id", "?"), exc_info=True)


async def evaluate_open(
    db: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID | None, now: datetime | None = None
) -> dict[str, int]:
    """Every gated signal of the organization decided again; counts by state after. The caller commits."""
    profile = await _profile(db, organization_id)
    scenarios = await read_scenarios(db, user_id=user_id)
    rows = (
        (
            await db.execute(
                select(CtemSignal)
                .where(CtemSignal.organization_id == organization_id, CtemSignal.state.in_(GATED_STATES))
                .order_by(CtemSignal.last_seen_at.desc())
                .limit(MAX_BULK)
            )
        )
        .scalars()
        .all()
    )
    counts: dict[str, int] = {}
    for row in rows:
        await evaluate(db, row, profile=profile, scenarios=scenarios, now=now)
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts
