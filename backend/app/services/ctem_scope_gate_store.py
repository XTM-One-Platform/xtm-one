# SPDX-License-Identifier: Apache-2.0

"""CTEM reconcile and scope gate, the I/O side (#847).

Reads what :func:`app.services.ctem_scope_gate.decide` needs (the
organization profile, the other signals on the same subject the recorder
may read, the OpenCRQ risk scenarios the recorder's own connection reads),
runs it and writes ``state`` and ``decision`` on the signal. No model call.

Segregation (#933): a signal is decided with its recorder's rights. Its
OpenCRQ scenarios are read with the recorder's connection (a ``system``
signal, which only administrators read: the evaluating administrator's), and
its related signals are those the recorder may read under the signal API's
rule. Nothing read by another identity is stored in a user's signal.

When it runs: after a signal is first recorded (API, agent tool, listener),
and on demand (``POST /api/v1/ctem-signals/{id}/evaluate`` and
``POST /api/v1/ctem-signals/evaluate`` for every open signal, after a policy
or criticality change: a watchlist signal re-enters the gate that way). A
signal that opened a case (``case_opened``) is never moved back. Design:
``dev-docs/ctem-scope-gate.md``.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_signal import CtemSignal
from app.services import ctem_scope_gate as gate
from app.services import ctem_signals as signals

logger = logging.getLogger(__name__)

#: The states the gate may move (``case_opened`` belongs to the case step).
GATED_STATES: tuple[str, ...] = ("new", "confirmed", "to_validate", "watchlist", "discarded")
#: The ``reason_code`` of a signal a person dismissed (#1505): the gate leaves it alone until it is restored.
DISMISSED_CODE = "dismissed_by_user"
#: What a person may do to signals from the Signals page (#1505).
TRIAGE_ACTIONS: tuple[str, ...] = ("dismiss", "restore", "recheck")
#: Signals one bulk evaluation reads at most.
MAX_BULK = 2000
#: Risk scenarios read from OpenCRQ at most, and supporting assets per scenario.
MAX_SCENARIOS = 50
SCENARIO_ASSETS_PAGE = 500
#: OpenCRQ tolerance ratio (expected loss over tolerance) read as scope criticality, highest first.
_RATIO_CRITICALITY: tuple[tuple[float, str], ...] = ((1.0, "critical"), (0.5, "high"), (0.1, "medium"))
#: Where one session keeps the link index and OpenCRQ reads of the per-signal path (#1591 S3), and for how long.
_READ_CACHE_KEY = "ctem_scope_gate_reads"
READ_CACHE_SECONDS = 300.0


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


async def recorder_reads_all(db: AsyncSession, recorded_by: str | None) -> bool:
    """Whether the recorder of a signal reads every signal: ``system`` (read by administrators) or an administrator."""
    from app.models.user import User
    from app.services.ctem_signals import SYSTEM_ACTOR

    if not recorded_by or recorded_by == SYSTEM_ACTOR:
        return True
    try:
        user = await db.get(User, uuid.UUID(str(recorded_by)))
    except (ValueError, TypeError):
        return False
    return bool(getattr(user, "is_admin", False))


async def related_signals(db: AsyncSession, row: CtemSignal) -> list[gate.RelatedSignal]:
    """The other signals on the same subject that the recorder of *row* may read (#933).

    The signal API's rule: an administrator (and a ``system`` signal, which
    only administrators read) sees every signal of the organization, anyone
    else only the signals recorded for them.
    """
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
    if not await recorder_reads_all(db, row.recorded_by):
        statement = statement.where(CtemSignal.recorded_by == row.recorded_by)
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


def scenario_reader(row: CtemSignal, evaluator_id: uuid.UUID | None) -> uuid.UUID | None:
    """Whose OpenCRQ connection reads the risk scenarios of *row* (#933): its recorder's own.

    A ``system`` signal (read by administrators only) uses the evaluating
    administrator's; a recorder id that is no user reads nothing.
    """
    from app.services.ctem_signals import SYSTEM_ACTOR

    if not row.recorded_by or row.recorded_by == SYSTEM_ACTOR:
        return evaluator_id
    try:
        return uuid.UUID(str(row.recorded_by))
    except (ValueError, TypeError):
        return None


async def read_scenarios(db: AsyncSession, *, user_id: uuid.UUID | None) -> gate.ScenarioIndex:
    """The risk scenarios holding each supporting asset, read with *user_id*'s own OpenCRQ connection.

    Never raises: a missing connection or a failed read answers an index
    with ``connected`` false and the reason, which the decision names.
    """
    if user_id is None:
        return gate.ScenarioIndex(note="OpenCRQ not read: re-evaluate the signal to read its recorder's risk scenarios")
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

    _row, profile = await org_context.current_profile(db, organization_id)  # assets from the records (#1543)
    return profile


async def _primary_assets(db: AsyncSession, organization_id: uuid.UUID) -> Any:
    """#1591 S3: the inventory link index every path decides with; an unreadable index is empty, never a failure."""
    from app.services import ctem_primary_assets

    try:
        async with db.begin_nested():
            return await ctem_primary_assets.load_index(db, organization_id)
    except Exception:
        logger.warning("CTEM scope gate: inventory links not read for %s", organization_id, exc_info=True)
        return ctem_primary_assets.PrimaryAssetIndex()


async def default_scenario_reader(db: AsyncSession) -> uuid.UUID | None:
    """#1591 S3: whose OpenCRQ read decides a ``system`` signal no administrator evaluates (the listener path).

    The owner of the enabled OpenCRQ primary-asset sync: the administrator who
    connected OpenCRQ for the organization. ``None`` when there is none.
    """
    from app.models.organization_context import OrganizationContextSync

    sync = OrganizationContextSync
    owner = (
        await db.execute(
            select(sync.owner_user_id)
            .where(
                sync.section == "crown_jewels",
                sync.source == "opencrq",
                sync.enabled.is_(True),
                sync.owner_user_id.is_not(None),
            )
            .order_by(sync.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return owner


async def _session_cached(db: AsyncSession, key: tuple[Any, ...], load: Callable[[], Awaitable[Any]]) -> Any:
    """*load* answered once per session and *key* for :data:`READ_CACHE_SECONDS` (#1591 S3 review).

    A listener records many signals in one session (``record_matches`` loops up
    to 2000 matches): the link index and the OpenCRQ read are built once for the
    pass, not once per signal. A session without ``info`` (a test double) loads
    every time.
    """
    info = getattr(db, "info", None)
    cache = info.setdefault(_READ_CACHE_KEY, {}) if isinstance(info, dict) else None
    now = time.monotonic()
    if cache is not None:
        hit = cache.get(key)
        if hit is not None and now - hit[0] < READ_CACHE_SECONDS:
            return hit[1]
    value = await load()
    if cache is not None:
        cache[key] = (now, value)
    return value


async def _scenarios_for(db: AsyncSession, row: Any) -> gate.ScenarioIndex:
    """The OpenCRQ read of *row* when the caller passed none, once per session and reader; never raises."""
    try:
        async with db.begin_nested():
            fallback = await _session_cached(db, ("fallback_reader",), lambda: default_scenario_reader(db))
        reader = scenario_reader(row, fallback)
        return await _session_cached(db, ("scenarios", reader), lambda: read_scenarios(db, user_id=reader))
    except Exception:
        logger.warning("CTEM scope gate: OpenCRQ reader not resolved", exc_info=True)
        return gate.ScenarioIndex(note="OpenCRQ not read: no OpenCRQ connection to read with")


def dismissed(row: CtemSignal) -> bool:
    """A signal a person dismissed (#1505): no pass of the gate moves it back, only a restore."""
    return row.state == "discarded" and (row.decision or {}).get("reason_code") == DISMISSED_CODE


async def evaluate(
    db: AsyncSession,
    row: CtemSignal,
    *,
    profile: Any = None,
    scenarios: gate.ScenarioIndex | None = None,
    primary_assets: Any = None,
    now: datetime | None = None,
    events: Any = None,
    restore: bool = False,
) -> gate.Decision | None:
    """Decide *row* and write its state and decision; ``None`` when it opened a case or a person
    dismissed it (*restore* decides a dismissed one again). The caller commits.

    With *events* (``ctem_case_loop.PendingEvents``), a verdict that changes a
    verdict the signal already had adds ``ctem_signal.triaged`` (#1409); the first
    verdict of a signal is reported by ``ctem_signal.recorded`` instead.
    """
    if row.state not in GATED_STATES or (dismissed(row) and not restore):
        return None
    before = signals.verdict_of(row.state)
    at = now or datetime.now(UTC)
    profile = profile if profile is not None else await _profile(db, row.organization_id)
    if primary_assets is None:
        # Per-signal callers (listeners) share one index per session and organization (#1591 S3 review).
        org_id = row.organization_id
        primary_assets = await _session_cached(db, ("links", org_id), lambda: _primary_assets(db, org_id))
    if scenarios is None:
        # #1591 S3: the same OpenCRQ read on every path; a system signal reads with the OpenCRQ sync owner's.
        scenarios = await _scenarios_for(db, row)
    decision = gate.decide(
        profile,
        facts_of(row),
        related=await related_signals(db, row),
        scenarios=scenarios,
        primary_assets=primary_assets,
        today=at.date(),
    )
    decision.record["decided_at"] = at.isoformat()
    row.state = decision.state
    row.decision = decision.record
    row.updated_at = at
    after = signals.verdict_of(row.state)
    if events is not None and before is not None and after is not None and after != before:
        events.add(
            signals.SIGNAL_TRIAGED,
            signals.signal_event_data(row, triaged=True, previous_verdict=before),
            signals.signal_event_id(signals.SIGNAL_TRIAGED, row, f"{before}>{after}@{at.isoformat()}"),
        )
    return decision


async def evaluate_after_record(db: AsyncSession, row: CtemSignal, *, created: bool) -> Any:
    """The gate on a signal just recorded, then its case (#857); a failure never refuses the signal.

    A gate failure leaves it ``new`` for an on-demand pass; a case that could
    not be opened leaves it ``confirmed`` for the scheduled sweep. Answers the
    case events to raise once the caller has committed
    (``ctem_case_loop.publish_pending``).
    """
    from app.services import ctem_case_loop

    pending = ctem_case_loop.PendingEvents()
    # A re-sighting of an unmatched signal is decided again (#1505): the inventory may hold it now.
    if not created and row.state != "to_validate":
        return pending  # any other re-sighting raises nothing (#1409)

    def recorded(verdict: str | None) -> None:
        # ctem_signal.recorded first (#1409), with the gate's first verdict, then the case events.
        # Only a new signal is recorded: a re-sighting decided again (#1505) raises none.
        # An event that cannot be built never refuses the signal.
        if not created:
            return
        try:
            item = (
                signals.SIGNAL_RECORDED,
                signals.signal_event_data(row, verdict=verdict),
                signals.signal_event_id(signals.SIGNAL_RECORDED, row, "first"),
            )
        except Exception:
            logger.warning("ctem_signal.recorded not built for %s", getattr(row, "id", "?"), exc_info=True)
            return
        pending.items.insert(0, item)
    try:
        async with db.begin_nested():
            await evaluate(db, row)
    except Exception:
        logger.warning("CTEM scope gate not run on signal %s; it stays new", getattr(row, "id", "?"), exc_info=True)
        recorded(None)
        return pending
    verdict = signals.verdict_of(row.state)
    try:
        async with db.begin_nested():
            await ctem_case_loop.open_from_signal(db, row, events=pending)
    except Exception:
        pending.items.clear()
        logger.warning("CTEM case not opened for signal %s; the sweep retries", getattr(row, "id", "?"), exc_info=True)
    recorded(verdict)
    return pending


async def evaluate_one(
    db: AsyncSession,
    row: CtemSignal,
    *,
    evaluator_id: uuid.UUID | None,
    now: datetime | None = None,
    events: Any = None,
) -> gate.Decision | None:
    """Decide *row* again with its recorder's OpenCRQ read (:func:`scenario_reader`). The caller commits."""
    if row.state not in GATED_STATES:
        return None
    scenarios = await read_scenarios(db, user_id=scenario_reader(row, evaluator_id))
    return await evaluate(db, row, scenarios=scenarios, now=now, events=events)


async def evaluate_open(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
    events: Any = None,
) -> dict[str, int]:
    """Every gated signal of the organization decided again; counts by state after. The caller commits.

    *user_id* is the evaluating administrator. Each signal is decided with its
    recorder's OpenCRQ read, read once per recorder (#933): an administrator's
    read never lands in another user's signal.
    """
    profile = await _profile(db, organization_id)
    links = await _primary_assets(db, organization_id)
    by_reader: dict[uuid.UUID | None, gate.ScenarioIndex] = {}
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
        reader = scenario_reader(row, user_id)
        if reader not in by_reader:
            by_reader[reader] = await read_scenarios(db, user_id=reader)
        await evaluate(
            db, row, profile=profile, scenarios=by_reader[reader], primary_assets=links, now=now, events=events
        )
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts


#: How many parked signals one tick decides again after a profile change (#1505); the next tick takes the rest.
RECHECK_BATCH = 500


async def recheck_parked(
    db: AsyncSession, *, now: datetime | None = None, batch: int = RECHECK_BATCH
) -> dict[str, int]:
    """#1505: the unmatched signals decided before the organization's newest profile version, decided again.

    A change to assets, groups, crown jewels, owned domains, software or
    shipped products writes a new profile version; every ``to_validate``
    signal last decided before it is decided again, *batch* per call, oldest
    first. Each is read with its recorder's OpenCRQ read. A signal the gate
    confirms waits for the case sweep, which groups it by case key, so a
    profile change never opens one case per host here. The caller commits.
    """
    from app.models.organization_context import OrganizationContextVersion

    latest = (
        select(
            OrganizationContextVersion.organization_id,
            func.max(OrganizationContextVersion.created_at).label("changed_at"),
        )
        .group_by(OrganizationContextVersion.organization_id)
        .subquery()
    )
    rows = list(
        (
            await db.execute(
                select(CtemSignal)
                .join(latest, latest.c.organization_id == CtemSignal.organization_id)
                .where(CtemSignal.state == "to_validate", CtemSignal.updated_at < latest.c.changed_at)
                .order_by(CtemSignal.updated_at)
                .limit(max(1, int(batch)))
            )
        ).scalars()
    )
    return await _decide_again(db, rows, now=now)


async def _decide_again(db: AsyncSession, rows: list[CtemSignal], *, now: datetime | None = None) -> dict[str, int]:
    """*rows* decided again, the profile, link index and OpenCRQ read once per organization and reader."""
    counts: dict[str, int] = {}
    profiles: dict[uuid.UUID, Any] = {}
    links: dict[uuid.UUID, Any] = {}
    by_reader: dict[uuid.UUID | None, gate.ScenarioIndex] = {}
    fallback = await default_scenario_reader(db) if rows else None
    at = now or datetime.now(UTC)
    for row in rows:
        if row.organization_id not in profiles:
            profiles[row.organization_id] = await _profile(db, row.organization_id)
            links[row.organization_id] = await _primary_assets(db, row.organization_id)
        reader = scenario_reader(row, fallback)
        if reader not in by_reader:
            by_reader[reader] = await read_scenarios(db, user_id=reader)
        await evaluate(
            db,
            row,
            profile=profiles[row.organization_id],
            scenarios=by_reader[reader],
            primary_assets=links[row.organization_id],
            now=at,
        )
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts


#: The watchlist reasons an inventory link or a software change can lift (#1591 S3).
HELD_BY_SCOPE: tuple[str, ...] = ("crown_jewel_below_policy", "criticality_unknown", "below_scope_threshold")


async def recheck_held(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    since: datetime,
    now: datetime | None = None,
    batch: int = RECHECK_BATCH,
) -> dict[str, int]:
    """#1591 S3: the watchlist signals held for scope, decided before *since*, decided again.

    Run after a primary-asset link sync or an endpoint software change completes:
    a signal held by ``crown_jewel_below_policy``, ``criticality_unknown`` or
    ``below_scope_threshold`` whose decision predates the change is decided
    again, *batch* per call, oldest first. Idempotent: a decided signal's
    ``updated_at`` moves past *since*, so the next call skips it. A signal the
    gate confirms waits for the case sweep (one case per fix key). The caller commits.
    """
    # The held reasons are filtered in SQL before the limit: watchlist rows held for
    # another reason never take the batch from the scope-held ones (#1591 S3 review).
    rows = list(
        (
            await db.execute(
                select(CtemSignal)
                .where(
                    CtemSignal.organization_id == organization_id,
                    CtemSignal.state == "watchlist",
                    CtemSignal.updated_at < since,
                    CtemSignal.decision["reason_code"].as_string().in_(HELD_BY_SCOPE),
                )
                .order_by(CtemSignal.updated_at)
                .limit(max(1, int(batch)))
            )
        ).scalars()
    )
    return await _decide_again(db, rows, now=now)


#: Where a profile row added from the Signals page says it came from (#1505).
INVENTORY_SOURCE = "ctem-signal"


def inventory_additions(profile: Any, rows: list[Any]) -> dict[str, list[Any]]:
    """#1505: the profile rows "Add to inventory" writes for the unmatched *rows*, nothing already there.

    A host or asset becomes an asset (``host`` when it is a hostname or an IP),
    a software subject a software product, an identity its email domain as
    an owned domain. Any other subject kind (a vulnerability, a threat, a third
    party, code) is not an inventory row: it is left out. Only ``to_validate``
    signals count.
    """
    have_assets = {gate.stable_key(a.name) for a in profile.assets_exposure}
    have_products = {e.product.strip().lower() for e in profile.tech_stack}
    have_domains = {d.lower() for d in profile.owned_domains}
    assets: list[str] = []
    products: list[str] = []
    domains: list[str] = []
    for row in rows:
        if row.state != "to_validate":
            continue
        if row.subject_kind == "asset":
            key = gate.stable_key(row.subject_ref)[:200]
            if key and key not in have_assets:
                have_assets.add(key)
                assets.append(key)
        elif row.subject_kind == "software":
            name = str(row.subject_label or row.subject_ref).strip()[:200]
            if name and name.lower() not in have_products:
                have_products.add(name.lower())
                products.append(name)
        elif row.subject_kind == "identity":
            domain = gate.identity_domain(row.subject_ref)
            if domain and domain not in have_domains:
                have_domains.add(domain)
                domains.append(domain)
    return {"assets": assets, "products": products, "domains": domains}


async def add_to_inventory(
    db: AsyncSession, rows: list[CtemSignal], *, organization_id: uuid.UUID, admin_id: uuid.UUID, actor: str
) -> dict[str, Any]:
    """#1505: declare the subjects of the unmatched *rows* in the profile, then decide *rows* again.

    One new profile version holds every addition (``source`` ``ctem-signal`` on
    each row); the gate then runs on *rows* with it. A signal the gate confirms
    opens its case at the next sweep, grouped by case key, never one per row
    here. ``ValueError`` when the profile would not validate (a list full).
    The caller commits.
    """
    from app.services import organization_context as org_context

    current = await org_context.current_version(db, organization_id)
    profile = org_context.parse_profile(current.profile if current else {})
    added = inventory_additions(profile, rows)
    version: int | None = None
    if any(added.values()):
        data = profile.model_dump(mode="json")
        data["assets_exposure"] = list(data.get("assets_exposure") or []) + [
            {
                "name": name,
                "category": "host" if "." in name else "generic_asset",
                "source": INVENTORY_SOURCE,
            }
            for name in added["assets"]
        ]
        data["tech_stack"] = list(data.get("tech_stack") or []) + [
            {"product": name, "source": INVENTORY_SOURCE} for name in added["products"]
        ]
        data["owned_domains"] = list(data.get("owned_domains") or []) + added["domains"]
        updated = org_context.parse_profile(data)
        basis = f"added from the CTEM Signals page by {actor}"[:500]
        touched = [
            f
            for f, key in (("assets_exposure", "assets"), ("tech_stack", "products"), ("owned_domains", "domains"))
            if added[key]
        ]
        saved = await org_context.save_version(
            db,
            updated,
            source="api",
            author_user_id=admin_id,
            organization_id=organization_id,
            sources={field: {"source": INVENTORY_SOURCE, "basis": basis} for field in touched},
        )
        version = saved.version
    parked = [row for row in rows if row.state == "to_validate"]
    counts = await triage(db, parked, action="recheck", actor=actor, evaluator_id=admin_id)
    counts["skipped"] = counts.get("skipped", 0) + len(rows) - len(parked)
    return {"added": added, "version": version, "counts": counts}


def dismissal_record(row: CtemSignal, *, by: str, at: datetime) -> dict[str, Any]:
    """The decision record of a dismissal (#1505): who and when, the state it left, the reconcile kept."""
    previous = dict(row.decision or {})
    return {
        "version": gate.DECISION_VERSION,
        "inputs": previous.get("inputs") or {},
        "reconcile": previous.get("reconcile") or {},
        "scope": None,
        "policy_checks": [],
        "severity": None,
        "sla_hours": None,
        "outcome": "discarded",
        "state": "discarded",
        "reason": f"dismissed by {by}",
        "reason_code": DISMISSED_CODE,
        "reason_params": {"by": by},
        "dismissed": {"by": by, "at": at.isoformat(), "previous_state": row.state},
        "decided_at": at.isoformat(),
    }


async def triage(
    db: AsyncSession,
    rows: list[CtemSignal],
    *,
    action: str,
    actor: str,
    evaluator_id: uuid.UUID | None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Dismiss, restore or re-check *rows* (#1505); counts ``changed``, ``skipped`` and by state after. The caller commits.

    ``dismiss`` moves a signal that opened no case to ``discarded`` with the
    actor's name; ``restore`` decides a dismissed one again; ``recheck`` runs the
    gate again on the others (a dismissed one stays dismissed). A signal that
    opened a case is skipped: the case step owns it. A signal the gate confirms
    opens its case at the next sweep, never one per signal here.
    """
    if action not in TRIAGE_ACTIONS:
        raise ValueError(f"action must be one of: {', '.join(TRIAGE_ACTIONS)}")
    at = now or datetime.now(UTC)
    counts: dict[str, int] = {"changed": 0, "skipped": 0}
    profile: Any = None
    by_reader: dict[uuid.UUID | None, gate.ScenarioIndex] = {}
    for row in rows:
        before = row.state
        if action == "dismiss":
            if row.state not in GATED_STATES or dismissed(row):
                counts["skipped"] += 1
                continue
            row.decision = dismissal_record(row, by=actor, at=at)
            row.state = "discarded"
            row.updated_at = at
        else:
            if row.state not in GATED_STATES or (action == "restore") != dismissed(row):
                counts["skipped"] += 1
                continue
            if profile is None:
                profile = await _profile(db, row.organization_id)
            reader = scenario_reader(row, evaluator_id)
            if reader not in by_reader:
                by_reader[reader] = await read_scenarios(db, user_id=reader)
            await evaluate(db, row, profile=profile, scenarios=by_reader[reader], now=at, restore=True)
        counts["changed"] += int(row.state != before or action != "recheck")
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts
