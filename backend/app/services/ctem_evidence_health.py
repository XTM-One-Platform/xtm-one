# SPDX-License-Identifier: Apache-2.0

"""Evidence decay and blind spots as signals (CTEM-28, #1161): the evidence health pass.

An opt-in, scheduled pass reads from OpenCRQ, per risk scenario, the evidence
health of its in-scope supporting assets (``riskScenarios.getEvidenceHealth``)
and records each gap as a CTEM signal of the inside lens, recorded by
``system`` (``services/ctem_signals.record_signal``):

- path 9, control or evidence decay (``evidence_expired``): the newest
  evidence is older than ``evidence_health.max_age_days`` (``expired``), only a
  person attests the asset (``attested_only``), or a connector reports it but no
  observation was ever dated (``never_measured``);
- path 12, blind spot or coverage gap (``coverage_gap``): no source reports the
  asset (``no_source``), a machine's version is unknown (``version_unknown``),
  or a connector sync emptied (a run far below the previous one).

A gap never reads as safe: it fires on absence and on time, not only on a
fresh observation. The dedupe key holds the state and the date of the last
evidence, so two identical passes record nothing new and the same gap raises
again once evidence came back and decayed anew. The spine (scope gate, cases)
decides what each signal becomes. No model call, no gate. Design:
``dev-docs/ctem-evidence-health.md``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PROVIDER = "opencrq"
#: The two event kinds of the pass, by the trigger path each records on.
EVIDENCE_EXPIRED = "evidence_expired"
COVERAGE_GAP = "coverage_gap"
EVENT_PATHS: dict[str, str] = {EVIDENCE_EXPIRED: "control_decay", COVERAGE_GAP: "blind_spot"}
#: OpenCRQ evidence health state -> the event kind it raises; ``current`` raises nothing.
STATE_EVENTS: dict[str, str] = {
    "expired": EVIDENCE_EXPIRED,
    "attested_only": EVIDENCE_EXPIRED,
    "never_measured": EVIDENCE_EXPIRED,
    "no_source": COVERAGE_GAP,
    "version_unknown": COVERAGE_GAP,
}
#: Plain-language reason of each state, written on the signal.
STATE_REASONS: dict[str, str] = {
    "expired": "no evidence within the re-test delay",
    "attested_only": "attested by a person only, never measured by a connector",
    "never_measured": "reported by a connector but never measured",
    "no_source": "no source reports this asset",
    "version_unknown": "the version is unknown",
}
#: Risk scenarios one pass reads at most, and assets per scenario (OpenCRQ caps it at 500).
MAX_SCENARIOS = 50
ASSETS_PER_SCENARIO = 500


class EvidenceReadError(RuntimeError):
    """OpenCRQ could not be read; the pass records nothing."""


@dataclass(frozen=True)
class Gap:
    """One gap the pass records as a signal."""

    event: str
    subject_ref: str
    subject_label: str
    state: str
    reason: str
    dedupe_key: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def trigger_path(self) -> str:
        return EVENT_PATHS[self.event]


class EvidenceReader(Protocol):
    async def scenarios(self) -> list[dict[str, Any]]: ...

    async def evidence_health(self, scenario_id: str, max_age_days: int) -> dict[str, Any]: ...


def _iso(value: Any) -> str:
    return str(value or "")[:40]


def _unmarked(row: dict[str, Any]) -> bool:
    """True only when OpenCRQ states the row carries no marking (``markingRefs: []``)."""
    refs = row.get("markingRefs")
    return isinstance(refs, list) and not refs


def gaps_of(scenario: dict[str, Any], health: dict[str, Any]) -> list[Gap]:
    """The gaps of one scenario's evidence health, in OpenCRQ order (a pure function).

    One gap per asset whose state is not ``current``, and one per connector sync
    that emptied. The dedupe key names the asset and its state with the date of
    its last evidence (else ``never``): an unchanged gap keeps its key.

    Fail closed on markings: the owner's credential reads marked rows, and every
    administrator reads the signals, so an asset or a scenario that carries an
    OpenCRQ marking, or whose markings the answer does not state, raises no gap
    naming it. The emptied-sync gaps name no asset and no scenario and are kept.
    """
    sid = str(scenario.get("id") or "")
    sname = str(scenario.get("name") or sid)[:200]
    out: list[Gap] = []
    assets = (health.get("assets") or []) if _unmarked(health) else []
    for asset in assets:
        if not _unmarked(asset):
            continue
        state = str(asset.get("state") or "")
        event = STATE_EVENTS.get(state)
        asset_id = str(asset.get("supportingAssetId") or "")
        if event is None or not asset_id:
            continue
        name = str(asset.get("supportingAssetName") or asset.get("supportingAssetDerivedName") or asset_id)[:200]
        last = _iso(asset.get("lastEvidenceAt"))
        reason = STATE_REASONS[state]
        out.append(
            Gap(
                event=event,
                subject_ref=name,
                subject_label=f"{name}: {reason}",
                state=state,
                reason=reason,
                dedupe_key=f"{EVENT_PATHS[event]}:{PROVIDER}:asset:{asset_id}:{state}:{last or 'never'}",
                evidence={
                    "event": event,
                    "state": state,
                    "last_evidence_at": last or None,
                    "opencrq_asset_ids": [asset_id],
                    "risk_scenario_id": sid,
                    "risk_scenario": sname,
                    "impact": asset.get("impact"),
                    "sources": [
                        {"origin": s.get("origin"), "last_seen_at": _iso(s.get("lastSeenAt")) or None}
                        for s in (asset.get("sources") or [])[:10]
                        if isinstance(s, dict)
                    ],
                },
            )
        )
    for source in health.get("sources") or []:
        if not source.get("emptied"):
            continue
        kind = str(source.get("kind") or "")[:100]
        last = _iso(source.get("lastRunAt"))
        reason = (
            f"the {kind} sync emptied: {source.get('latestTotal')} rows after {source.get('previousTotal')}; "
            "its findings are not closed"
        )
        out.append(
            Gap(
                event=COVERAGE_GAP,
                subject_ref=f"{PROVIDER}:{kind}",
                subject_label=reason[:300],
                state="source_emptied",
                reason=reason,
                dedupe_key=f"blind_spot:{PROVIDER}:source:{kind}:{last or 'never'}",
                evidence={
                    "event": COVERAGE_GAP,
                    "state": "source_emptied",
                    "sync_kind": kind,
                    "last_run_at": last or None,
                    "latest_total": source.get("latestTotal"),
                    "previous_total": source.get("previousTotal"),
                },
            )
        )
    return out


async def run_pass(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    reader: EvidenceReader,
    max_age_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read every scenario's evidence health and record each gap; the caller commits.

    Answers the counts and the case events to publish once committed. A source
    sync seen in several scenarios is recorded once.
    """
    from app.services import ctem_case_loop, ctem_scope_gate_store, ctem_signals

    at = now or datetime.now(UTC)
    gaps: dict[str, Gap] = {}
    scenarios = (await reader.scenarios())[:MAX_SCENARIOS]
    for scenario in scenarios:
        if not scenario.get("id"):
            continue
        health = await reader.evidence_health(str(scenario["id"]), max_age_days)
        for gap in gaps_of(scenario, health):
            gaps.setdefault(gap.dedupe_key, gap)

    from app.services.ctem_signal_sources import excluded_sources, is_excluded

    # A path an administrator took the pass off (#1445) records nothing from it.
    excluded = await excluded_sources(db, organization_id)
    pending = ctem_case_loop.PendingEvents()
    counts: dict[str, int] = {"scenarios": len(scenarios), "recorded": 0, "new": 0}
    for gap in gaps.values():
        if is_excluded(excluded, gap.trigger_path, PROVIDER, "pass"):
            counts["excluded"] = counts.get("excluded", 0) + 1
            continue
        signal, created = await ctem_signals.record_signal(
            db,
            organization_id=organization_id,
            signal=ctem_signals.SignalInput(
                trigger_path=gap.trigger_path,
                source_provider=PROVIDER,
                subject_kind="asset",
                subject_ref=gap.subject_ref,
                lens="inside",
                subject_label=gap.subject_label,
                observed_at=at,
                evidence={**gap.evidence, "reason": gap.reason},
                dedupe_key=gap.dedupe_key,
            ),
            recorded_via="listener",
            recorded_by=ctem_signals.SYSTEM_ACTOR,
            now=at,
        )
        counts["recorded"] += 1
        if not created:
            continue
        counts["new"] += 1
        counts[gap.event] = counts.get(gap.event, 0) + 1
        events = await ctem_scope_gate_store.evaluate_after_record(db, signal, created=created)
        # Hand the loop back between signals: a pass of hundreds of gaps never holds it in one go.
        await asyncio.sleep(0)
        if events:
            pending.items.extend(events.items)
            pending.verticals |= events.verticals
            pending.frames.extend(events.frames)
    return {"counts": counts, "pending": pending}


# ── the OpenCRQ reader ──────────────────────────────────────────────────


class OpenCrqEvidenceReader:
    """The evidence health of each risk scenario, read through OpenCRQ's tRPC API."""

    def __init__(self, base_url: str, headers: dict[str, str], tenant: str):
        self._base_url = base_url
        self._headers = headers
        self._tenant = tenant

    async def _call(self, procedure: str, action: str, payload: dict[str, Any]) -> Any:
        from app.integrations.providers.opencrq._common import _trpc

        data, err = await _trpc(self._base_url, self._headers, procedure, action, input=payload, tenant_id=self._tenant)
        if err:
            raise EvidenceReadError(err.splitlines()[0][:300])
        return data

    async def scenarios(self) -> list[dict[str, Any]]:
        data = await self._call("riskScenarios.getRiskScenariosOverview", "list risk scenarios", {})
        return [s for s in data or [] if isinstance(s, dict)]

    async def evidence_health(self, scenario_id: str, max_age_days: int) -> dict[str, Any]:
        data = await self._call(
            "riskScenarios.getEvidenceHealth",
            "read the evidence health of a risk scenario",
            {"riskScenarioId": scenario_id, "maxAgeDays": max_age_days, "limit": ASSETS_PER_SCENARIO},
        )
        return data if isinstance(data, dict) else {}


async def reader_for(db: AsyncSession, profile: Any) -> OpenCrqEvidenceReader | str:
    """The reader of the declared OpenCRQ integration, or the reason there is none.

    The credential is the integration owner's, as the attack surface pass and the
    CTEM ticketing sync do: an administrator declared it in the profile, and the
    token only reads the evidence health of the tenant's risk scenarios.
    """
    from app.integrations.providers.opencrq._common import _headers, _resolve_tenant
    from app.models.integration import Integration
    from app.services.integration_tools import _get_token

    settings = getattr(profile, "evidence_health", None)
    integration_id = (getattr(settings, "integration", "") or "").strip()
    if not integration_id:
        return "no OpenCRQ integration is declared for the evidence health pass"
    try:
        integration = await db.get(Integration, uuid.UUID(integration_id))
    except ValueError:
        return f"the evidence health integration {integration_id!r} is not an integration id"
    if integration is None:
        return "the declared evidence health integration no longer exists"
    if integration.provider != PROVIDER:
        return f"{integration.provider} is not an OpenCRQ integration"
    owner = str(integration.user_id) if integration.user_id else None
    token = await _get_token(db, integration_id, PROVIDER, user_id=owner, integ=integration)
    base_url, sep, api_token = str(token or "").partition("||")
    base_url = base_url.strip().rstrip("/")
    if not sep or not base_url or not api_token.strip():
        return "no OpenCRQ credential could be resolved for the evidence health integration"
    headers = _headers(api_token.strip())
    tenant, err = await _resolve_tenant(base_url, headers, "")
    if err or not tenant:
        return f"OpenCRQ tenant not resolved: {err or 'no tenant'}"
    return OpenCrqEvidenceReader(base_url, headers, tenant)


# ── which integration the pass reads (#1344) ───────────────────────────

#: The states of the pass for one platform: it runs on an integration, an
#: OpenCRQ integration is connected but the pass is off or has none it can pick,
#: or no OpenCRQ integration is connected.
PASS_ON, PASS_OFF, PASS_NONE = "on", "off", "none"


def resolve_pass(settings: Any, connected: Any) -> tuple[str, str]:
    """``(state, integration id)`` of the pass for the connected OpenCRQ integration ids *connected*.

    One rule for the signal sources badge (``ctem_signal_sources.bound_sources``)
    and the tick (:func:`scheduled_pass`), so the badge never reads Connected
    when the pass would skip:

    - no OpenCRQ integration connected: ``none``;
    - turned off by an administrator (``enabled`` false with an integration
      declared): ``off``;
    - an integration declared: ``on`` when it is connected, else ``off``
      (never another one picked silently);
    - none declared and ``enabled`` unset (or the legacy stored default, false
      with none declared): ``on`` on the single connected integration, ``off``
      when several are connected and none is declared.
    """
    ids = sorted({str(i) for i in connected or () if i})
    if not ids:
        return PASS_NONE, ""
    declared = (getattr(settings, "integration", "") or "").strip()
    if getattr(settings, "enabled", None) is False and declared:
        return PASS_OFF, ""
    if declared:
        return (PASS_ON, declared) if declared in ids else (PASS_OFF, "")
    return (PASS_ON, ids[0]) if len(ids) == 1 else (PASS_OFF, "")


async def connected_integration_ids(db: AsyncSession) -> list[str]:
    """The ids of the connected OpenCRQ integrations of the platform (the pass is organization-wide)."""
    from sqlalchemy import select

    from app.models.integration import Integration

    rows = (
        await db.execute(
            select(Integration.id).where(Integration.provider == PROVIDER, Integration.status == "connected")
        )
    ).scalars()
    return [str(i) for i in rows]


# ── the schedule ────────────────────────────────────────────────────────


_CADENCE_SLOT_KEY = "ctem:evidence-health:pass"


async def pass_due(cadence_hours: int) -> bool:
    """Take the pass slot for this cadence window; True when this tick runs it (or Redis is unavailable)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        return bool(await client.set(_CADENCE_SLOT_KEY, "1", nx=True, ex=max(1, cadence_hours) * 3600))
    except Exception:  # no Redis: the pass runs; it is idempotent (signals dedupe on their key)
        return True


async def release_pass_slot() -> None:
    """Give the slot back after a pass that did not complete, so the next tick retries it."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.delete(_CADENCE_SLOT_KEY)
    except Exception:  # no Redis: there is no slot to give back
        logger.debug("CTEM evidence health pass slot not released: Redis unavailable")


async def scheduled_pass(db: AsyncSession) -> dict[str, Any] | None:
    """The tick's pass on the primary organization; ``None`` when off (:func:`resolve_pass`).

    A declared integration is read as declared (:func:`reader_for` checks it);
    with none declared, the connected OpenCRQ integrations decide.
    """
    from app.services import ctem_case_loop
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    primary = await get_primary(db)
    if primary is None:
        return None
    row = await org_context.current_version(db, primary.id)
    profile = org_context.parse_profile(row.profile if row else {})
    settings = profile.evidence_health
    declared = (settings.integration or "").strip()
    if settings.enabled is False and declared:
        return None
    if not declared:
        state, picked = resolve_pass(settings, await connected_integration_ids(db))
        if state != PASS_ON:
            return None
        settings = settings.model_copy(update={"integration": picked})
        profile = profile.model_copy(update={"evidence_health": settings})
    if not await pass_due(settings.cadence_hours):
        return None
    completed = False
    try:
        reader = await reader_for(db, profile)
        if isinstance(reader, str):
            logger.info("CTEM evidence health pass skipped: %s", reader)
            return {"skipped": reader}
        try:
            result = await run_pass(db, organization_id=primary.id, reader=reader, max_age_days=settings.max_age_days)
        except EvidenceReadError as exc:
            await db.rollback()
            logger.warning("CTEM evidence health pass: OpenCRQ read failed: %s", exc)
            return {"skipped": str(exc)}
        await db.commit()
        completed = True
    finally:
        # The slot is kept only by a pass that committed: a skipped or failed
        # pass (no credential yet, an OpenCRQ error) retries next tick.
        if not completed:
            await release_pass_slot()
    await ctem_case_loop.publish_pending(result["pending"])
    counts: dict[str, Any] = result["counts"]
    return counts
