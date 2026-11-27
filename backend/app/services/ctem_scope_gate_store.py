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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
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
#: The states a new sighting of a known signal decides again (#1505 unmatched, #2581 watchlist).
RE_DECIDED_ON_SIGHTING: tuple[str, ...] = ("to_validate", "watchlist")
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

    # Assets from the records (#1543), software from the Software entities (#1495).
    _row, profile = await org_context.software_profile(db, organization_id)
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


#: Supporting assets one page of the loop trace lists at most (#1979).
SUPPORTING_PAGE_MAX = 200
#: Seconds a signal's supporting asset list is kept for the next page, search or sort (UX r1 B4 on #1995).
SUPPORTING_CACHE_SECONDS = 300.0
#: Lists kept at most (one per signal version and primary asset); the oldest goes first.
SUPPORTING_CACHE_MAX = 64
#: The columns the supporting asset table sorts on the server (UX r1 B3 on #1995).
SUPPORTING_SORTS: tuple[str, ...] = ("asset", "through")

#: (organization, signal, signal version, primary asset key) -> (read at, rows sorted by asset, truncated).
_supporting_cache: dict[tuple[str, str, str, str], tuple[float, list[dict[str, Any]], bool]] = {}


def _supporting_cached(cache_key: tuple[str, str, str, str]) -> tuple[list[dict[str, Any]], bool] | None:
    hit = _supporting_cache.get(cache_key)
    if hit is None or time.monotonic() - hit[0] > SUPPORTING_CACHE_SECONDS:
        _supporting_cache.pop(cache_key, None)
        return None
    return hit[1], hit[2]


def _supporting_keep(cache_key: tuple[str, str, str, str], rows: list[dict[str, Any]], truncated: bool) -> None:
    while len(_supporting_cache) >= SUPPORTING_CACHE_MAX:
        _supporting_cache.pop(min(_supporting_cache, key=lambda k: _supporting_cache[k][0]))
    _supporting_cache[cache_key] = (time.monotonic(), rows, truncated)


async def _supporting_index(db: AsyncSession, organization_id: uuid.UUID, key: str, refs: list[str]) -> Any:
    """The link index narrowed to primary asset *key* and the products *refs* name (UX r2 B4 on #1995)."""
    from app.services import ctem_primary_assets

    try:
        async with db.begin_nested():
            return await ctem_primary_assets.load_index(db, organization_id, primary_asset_key=key, software=refs)
    except Exception:
        logger.warning("CTEM loop trace: supporting assets not read for %s", organization_id, exc_info=True)
        return ctem_primary_assets.PrimaryAssetIndex()


async def _reach_inputs(db: AsyncSession, row: CtemSignal) -> tuple[list[str], list[str]]:
    """The software references and subject keys the gate followed for *row*.

    Read from its decision record (UX r2 B4 on #1995: no profile read on the trace's
    path); a record without them (older than #1591 S3) resolves them again.
    """
    decision = getattr(row, "decision", None)
    reconcile = decision.get("reconcile") if isinstance(decision, dict) else None
    if (
        isinstance(reconcile, dict)
        and isinstance(reconcile.get("software_refs"), list)
        and isinstance(reconcile.get("keys"), list)
    ):
        products = reconcile.get("products") if isinstance(reconcile.get("products"), list) else []
        refs = [str(r) for r in dict.fromkeys([*reconcile["software_refs"], *products]) if r]
        return refs, [str(k) for k in reconcile["keys"] if k]
    profile = await _profile(db, row.organization_id)
    resolution = gate.resolve(profile, facts_of(row), None)
    return list(dict.fromkeys([*resolution.software_refs, *resolution.products])), list(resolution.keys)


async def _supporting_rows(db: AsyncSession, row: CtemSignal, key: str) -> tuple[list[dict[str, Any]], bool]:
    """Every supporting asset of *row* for primary asset *key*, read once per signal version (UX r1 B4 on #1995).

    The first read takes the signal's references from its decision record and reads
    the link index of that primary asset alone, with only the installed software rows
    of the products the signal names (UX r2 B4); the page, the search and the sort then
    work on the list kept here for a few minutes, keyed on the signal's ``updated_at``
    so a re-decided signal reads again.
    """
    version = str(getattr(row, "updated_at", "") or "")
    cache_key = (str(row.organization_id), str(row.id), version, key)
    hit = _supporting_cached(cache_key)
    if hit is not None:
        return hit
    refs, keys = await _reach_inputs(db, row)
    index = await _supporting_index(db, row.organization_id, key, refs)
    rows = index.supporting(refs, keys, key)
    truncated = bool(index.truncated)
    _supporting_keep(cache_key, rows, truncated)
    return rows, truncated


async def supporting_assets(
    db: AsyncSession,
    row: CtemSignal,
    primary_asset: str,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = 50,
    sort: str = "asset",
    direction: str = "asc",
) -> dict[str, Any]:
    """The inventory assets through which *row* reaches *primary_asset*, paged (#1979).

    Read live from the link index with the signal's own software and keys (the
    same reach the gate ran), so a primary asset 10,000 workstations support
    lists all of them, not the chains the decision record keeps. *query* filters
    by asset or group name, case-insensitive; *sort* (``asset`` or ``through``) and
    *direction* (``asc`` or ``desc``) order the whole list before the page is cut.
    Answers ``{primary_asset, total, offset, limit, sort, direction, truncated,
    assets}``; ``truncated`` says the index read itself was cut at its bounds.
    """
    key = " ".join(str(primary_asset).split()).casefold()[:200]
    rows, truncated = await _supporting_rows(db, row, key)
    needle = query.strip().casefold()
    if needle:
        rows = [r for r in rows if needle in r["asset"].casefold() or needle in str(r["through"]).casefold()]
    sort = sort if sort in SUPPORTING_SORTS else "asset"
    descending = direction == "desc"
    if sort != "asset" or descending:
        rows = sorted(rows, key=lambda r: (str(r[sort]).casefold(), r["asset"].casefold()), reverse=descending)
    limit = max(1, min(int(limit), SUPPORTING_PAGE_MAX))
    offset = max(0, int(offset))
    return {
        "primary_asset": key,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "direction": "desc" if descending else "asc",
        "truncated": truncated,
        "assets": rows[offset : offset + limit],
    }


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
                sync.section == "primary_assets",
                sync.source == "opencrq",
                sync.enabled.is_(True),
                sync.owner_user_id.is_not(None),
            )
            .order_by(sync.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return owner


async def _session_cached(
    db: AsyncSession,
    key: tuple[Any, ...],
    load: Callable[[], Awaitable[Any]],
    *,
    keep: Callable[[Any], bool] | None = None,
) -> Any:
    """*load* answered once per session and *key* for :data:`READ_CACHE_SECONDS` (#1591 S3 review).

    A listener records many signals in one session (``record_matches`` loops up
    to 2000 matches): the link index and the OpenCRQ read are built once for the
    pass, not once per signal. A session without ``info`` (a test double) loads
    every time. *keep* False on a loaded value leaves it out of the cache (#2542:
    a failed OpenCTI read is never reused), so the next call loads again.
    """
    hit = _session_hit(db, key)
    if hit is not None:
        return hit
    now = time.monotonic()
    value = await load()
    if keep is not None and not keep(value):
        return value
    _session_put(db, key, value, now)
    return value


def _session_put(db: AsyncSession, key: tuple[Any, ...], value: Any, at: float | None = None) -> None:
    """Hold *value* for *key* in this session's read cache (nothing on a session without ``info``)."""
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        info.setdefault(_READ_CACHE_KEY, {})[key] = (time.monotonic() if at is None else at, value)


def _read_ok(value: Any) -> bool:
    """#2542: an OpenCTI software read ``(found, how)`` that answered; only those are cached."""
    return isinstance(value, tuple) and len(value) == 2 and value[1] == gate.LOOKUP_READ


async def _software_read(
    db: AsyncSession,
    key: tuple[Any, ...],
    load: Callable[[], Awaitable[Any]],
    *,
    reuse_failure: bool = False,
) -> tuple[Any, str]:
    """An OpenCTI software read through the session cache (#2542).

    A read that answered is cached for the pass; one that failed is not (the next
    signal of the same threat or CVE list reads again), and is kept apart under
    ``("failed", *key)`` so a ``remote=False`` decision of this pass says why it
    was not read instead of "not read". *reuse_failure* True (the bulk prefetch,
    :func:`prepare_cve_reads`) answers that failure instead of reading again, so a
    slow OpenCTI costs one timeout per key and pass, not one per signal (#2548 review).
    """
    if reuse_failure:
        failed = _session_hit(db, ("failed", *key))
        if isinstance(failed, tuple):
            return failed[0], str(failed[1])
    found, how = await _session_cached(db, key, load, keep=_read_ok)
    if how != gate.LOOKUP_READ:
        _session_put(db, ("failed", *key), (found, how))
    return found, how


def _forget_failures(db: AsyncSession) -> None:
    """Drop the OpenCTI read failures this session met, so a new bulk prefetch reads them again (#2542)."""
    info = getattr(db, "info", None)
    cache = info.get(_READ_CACHE_KEY) if isinstance(info, dict) else None
    if isinstance(cache, dict):
        for key in [key for key in cache if key and key[0] == "failed"]:
            del cache[key]


def _software_hit(db: AsyncSession, key: tuple[Any, ...]) -> Any:
    """What this session read for *key*: the cached answer, else the failure it met, else ``None``."""
    hit = _session_hit(db, key)
    return hit if hit is not None else _session_hit(db, ("failed", *key))


def _session_hit(db: AsyncSession, key: tuple[Any, ...]) -> Any:
    """The value :func:`_session_cached` holds for *key* in this session, ``None`` when none is fresh."""
    info = getattr(db, "info", None)
    cache = info.get(_READ_CACHE_KEY) if isinstance(info, dict) else None
    hit = cache.get(key) if isinstance(cache, dict) else None
    if hit is not None and time.monotonic() - hit[0] < READ_CACHE_SECONDS:
        return hit[1]
    return None


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


async def prepare_system_reads(db: AsyncSession, organization_id: uuid.UUID) -> None:
    """Take the gate's reads of a ``system`` signal now, before any signal row is written (#1953).

    The OpenCRQ risk scenarios (a remote read), the fallback reader and the
    inventory link index land in the session cache, so a recompute that gates
    the signals it records reads nothing remote while it holds their row
    locks. Never raises: an unread value is read again by :func:`evaluate`.
    """
    try:
        async with db.begin_nested():
            fallback = await _session_cached(db, ("fallback_reader",), lambda: default_scenario_reader(db))
        await _session_cached(db, ("scenarios", fallback), lambda: read_scenarios(db, user_id=fallback))
        await _session_cached(db, ("links", organization_id), lambda: _primary_assets(db, organization_id))
    except Exception:
        logger.warning("CTEM scope gate: reads not prepared for %s", organization_id, exc_info=True)


async def _with_cve_software(
    db: AsyncSession,
    row: CtemSignal,
    facts: gate.SignalFacts,
    profile: Any,
    primary_assets: Any,
    *,
    remote: bool = True,
    release: bool = False,
    reuse_failure: bool = False,
) -> gate.SignalFacts:
    """*facts* with the software OpenCTI relates to the CVEs the signal names (#2171).

    Read only when the signal names a CVE and resolves to nothing without it, with
    the recorder's own OpenCTI access (a ``system`` signal: the OpenCRQ sync
    owner's, as :func:`_scenarios_for`), once per session and CVE list. Never raises.
    *remote* False reads nothing, remote or local: only what this session already
    read (:func:`prepare_cve_reads`) is used, else *facts* are answered unchanged.
    *release* True (the bulk prefetch, no savepoint open) hands the pooled
    connection back during the OpenCTI call; the record-time gate never does (#2516).
    *reuse_failure* as :func:`_software_read`.
    """
    from app.services import ctem_cve_software

    cves = gate.signal_cve_ids(facts.subject_ref, facts.subject_label, facts.evidence)
    if not cves or gate.resolve(profile, facts, primary_assets).resolved:
        return facts
    if not remote:
        fallback = _session_hit(db, ("fallback_reader",))
        hit = _software_hit(db, ("cve_software", scenario_reader(row, fallback), tuple(cves)))
        if hit is None:
            return facts
        found, how = hit
        return replace(facts, cve_software=tuple(found), cve_lookup=how)
    try:
        async with db.begin_nested():
            fallback = await _session_cached(db, ("fallback_reader",), lambda: default_scenario_reader(db))
        reader = scenario_reader(row, fallback)
        found, how = await _software_read(
            db,
            ("cve_software", reader, tuple(cves)),
            lambda: ctem_cve_software.cve_software(db, reader, cves, release=release),
            reuse_failure=reuse_failure,
        )
    except Exception:
        logger.warning("CTEM scope gate: CVE software not read for %s", getattr(row, "id", "?"), exc_info=True)
        found, how = (), ctem_cve_software.LOOKUP_FAILED
    return replace(facts, cve_software=tuple(found), cve_lookup=how)


#: #2523: the OpenCTI types a threat signal is recorded from; the pure gate owns them since #2542.
THREAT_STIX_TYPES = gate.THREAT_STIX_TYPES
threat_opencti_id = gate.threat_opencti_id


async def _with_threat_software(
    db: AsyncSession,
    row: CtemSignal,
    facts: gate.SignalFacts,
    profile: Any,
    primary_assets: Any,
    *,
    remote: bool = True,
    release: bool = False,
    reuse_failure: bool = False,
) -> gate.SignalFacts:
    """*facts* with the software OpenCTI says the threat targets or uses (#2523).

    Read only for an OpenCTI threat (:func:`threat_opencti_id`) that resolves to
    nothing on its evidence alone, with the recorder's own OpenCTI access, once
    per session, reader and threat id. Never raises. *remote*, *release* and
    *reuse_failure* as :func:`_with_cve_software`.
    """
    from app.services import ctem_cve_software, ctem_threat_software

    threat_id = threat_opencti_id(facts)
    if not threat_id or gate.resolve(profile, facts, primary_assets).resolved:
        return facts
    if not remote:
        fallback = _session_hit(db, ("fallback_reader",))
        hit = _software_hit(db, ("threat_software", scenario_reader(row, fallback), threat_id))
        if hit is None:
            return facts
        found, how = hit
        return replace(facts, threat_software=tuple(found), threat_lookup=how)
    try:
        async with db.begin_nested():
            fallback = await _session_cached(db, ("fallback_reader",), lambda: default_scenario_reader(db))
        reader = scenario_reader(row, fallback)
        found, how = await _software_read(
            db,
            ("threat_software", reader, threat_id),
            lambda: ctem_threat_software.threat_software(db, reader, threat_id, release=release),
            reuse_failure=reuse_failure,
        )
    except Exception:
        logger.warning("CTEM scope gate: threat software not read for %s", getattr(row, "id", "?"), exc_info=True)
        found, how = (), ctem_cve_software.LOOKUP_FAILED
    return replace(facts, threat_software=tuple(found), threat_lookup=how)


def _needs_scores(profile: Any, facts: gate.SignalFacts) -> list[str]:
    """#2631: the CVEs whose scores OpenCTI is read for: a minimum set and a score missing from the evidence."""
    policy = getattr(profile, "prioritization_policy", None)
    if policy is None or (policy.cvss_min is None and policy.epss_min is None):
        return []
    if gate.scores_in_evidence(facts.evidence):
        return []
    return gate.signal_cve_ids(facts.subject_ref, facts.subject_label, facts.evidence)


async def _with_cve_scores(
    db: AsyncSession,
    row: CtemSignal,
    facts: gate.SignalFacts,
    profile: Any,
    *,
    remote: bool = True,
    release: bool = False,
    reuse_failure: bool = False,
) -> gate.SignalFacts:
    """*facts* with the CVSS, EPSS and KEV OpenCTI holds on the signal's CVEs (#2631).

    Read only when the policy sets a score minimum and the evidence lacks a score,
    with the recorder's own OpenCTI access, once per session, reader and CVE list.
    Never raises. *remote*, *release* and *reuse_failure* as :func:`_with_cve_software`.
    """
    from app.services import ctem_cve_software

    cves = _needs_scores(profile, facts)
    if not cves:
        return facts

    def scored(found: Any, how: str) -> gate.SignalFacts:
        cvss, epss, kev = found if isinstance(found, tuple) and len(found) == 3 else (None, None, False)
        return replace(facts, cve_cvss=cvss, cve_epss=epss, cve_kev=bool(kev), score_lookup=how)

    if not remote:
        fallback = _session_hit(db, ("fallback_reader",))
        hit = _software_hit(db, ("cve_scores", scenario_reader(row, fallback), tuple(cves)))
        return facts if hit is None else scored(*hit)
    try:
        async with db.begin_nested():
            fallback = await _session_cached(db, ("fallback_reader",), lambda: default_scenario_reader(db))
        reader = scenario_reader(row, fallback)
        found, how = await _software_read(
            db,
            ("cve_scores", reader, tuple(cves)),
            lambda: ctem_cve_software.cve_scores(db, reader, cves, release=release),
            reuse_failure=reuse_failure,
        )
    except Exception:
        logger.warning("CTEM scope gate: CVE scores not read for %s", getattr(row, "id", "?"), exc_info=True)
        found, how = (None, None, False), ctem_cve_software.LOOKUP_FAILED
    return scored(found, how)


async def _with_remote_software(
    db: AsyncSession,
    row: CtemSignal,
    facts: gate.SignalFacts,
    profile: Any,
    primary_assets: Any,
    *,
    remote: bool = True,
    release: bool = False,
    reuse_failure: bool = False,
) -> gate.SignalFacts:
    """*facts* with the OpenCTI reads the gate takes: the CVEs' software (#2171), the threat's
    software (#2523), then the CVEs' scores when the policy needs them (#2631)."""
    kw = {"remote": remote, "release": release, "reuse_failure": reuse_failure}
    facts = await _with_cve_software(db, row, facts, profile, primary_assets, **kw)
    facts = await _with_threat_software(db, row, facts, profile, primary_assets, **kw)
    return await _with_cve_scores(db, row, facts, profile, **kw)


async def prepare_cve_reads(
    db: AsyncSession,
    rows: list[CtemSignal],
    *,
    profiles: dict[uuid.UUID, Any] | None = None,
    links: dict[uuid.UUID, Any] | None = None,
) -> None:
    """Take the OpenCTI software reads of *rows* now (CVEs #2171, threats #2523), before any row is written.

    A bulk pass (:func:`evaluate_open`, :func:`recheck_parked`, :func:`recheck_held`,
    :func:`triage`) decides many signals in one transaction; its earlier rows'
    updates are flushed while it decides the next ones, so a remote read inside
    the loop would hold their row locks (#1953). The reads land in the session
    cache and the pass decides with ``remote_reads=False``. *profiles* and *links*
    by organization spare a second build; a missing one is read once per session.
    Never raises: a signal not read here is decided without its CVE software.

    A read that failed is met once per key in this prefetch: the rows sharing that
    threat id or CVE list reuse the failure instead of paying one OpenCTI timeout
    each; the failures of an earlier pass are forgotten first, so the next pass
    reads them again (#2548 review).
    """
    _forget_failures(db)
    for row in rows:
        if row.state not in GATED_STATES:
            continue
        try:
            facts = facts_of(row)
            if not isinstance(facts, gate.SignalFacts):
                continue
            org = row.organization_id
            if profiles is not None and org in profiles:
                profile = profiles[org]
            else:
                profile = await _session_cached(db, ("profile", org), partial(_profile, db, org))
            if links is not None and org in links:
                primary_assets = links[org]
            else:
                primary_assets = await _session_cached(db, ("links", org), partial(_primary_assets, db, org))
            # No savepoint is open here: release the connection during the OpenCTI I/O (#2518 review).
            await _with_remote_software(db, row, facts, profile, primary_assets, release=True, reuse_failure=True)
        except Exception:
            logger.warning("CTEM scope gate: CVE read not prepared for %s", getattr(row, "id", "?"), exc_info=True)


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
    remote_reads: bool = True,
) -> gate.Decision | None:
    """Decide *row* and write its state and decision; ``None`` when it opened a case or a person
    dismissed it (*restore* decides a dismissed one again). The caller commits.

    *remote_reads* False (a pass that holds signal row locks, #1953) skips the
    OpenCTI read of the software of the signal's CVEs (#2171) and uses only what
    :func:`prepare_cve_reads` read before the pass.

    With *events* (``ctem_case_loop.PendingEvents``), a verdict that changes a
    verdict the signal already had adds ``ctem_signal.triaged`` (#1409); the first
    verdict of a signal is reported by ``ctem_signal.recorded`` instead.
    """
    if row.state not in GATED_STATES or (dismissed(row) and not restore):
        return None
    before = signals.verdict_of(row.state)
    at = now or datetime.now(UTC)
    if profile is None:
        # Per-signal callers (a pass recording many signals) build the software profile once per
        # session and organization: rebuilt per signal it held the web loop for seconds on a large store.
        org = row.organization_id
        profile = await _session_cached(db, ("profile", org), lambda: _profile(db, org))
    if primary_assets is None:
        # Per-signal callers (listeners) share one index per session and organization (#1591 S3 review).
        org_id = row.organization_id
        primary_assets = await _session_cached(db, ("links", org_id), lambda: _primary_assets(db, org_id))
    if scenarios is None:
        # #1591 S3: the same OpenCRQ read on every path; a system signal reads with the OpenCRQ sync owner's.
        scenarios = await _scenarios_for(db, row)
    facts = facts_of(row)
    if isinstance(facts, gate.SignalFacts):
        facts = await _with_remote_software(db, row, facts, profile, primary_assets, remote=remote_reads)
    decision = gate.decide(
        profile,
        facts,
        related=await related_signals(db, row),
        scenarios=scenarios,
        primary_assets=primary_assets,
        today=at.date(),
    )
    decision.record["decided_at"] = at.isoformat()
    _count_lookup_attempts(decision.record, row.decision)
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

    A gate failure is retried once with local reads only (no OpenCTI or OpenCRQ
    call); a second failure leaves it ``new`` for :func:`recheck_new`, the
    scheduled pass. A case that could not be opened leaves it ``confirmed`` for
    the scheduled sweep. Answers the
    case events to raise once the caller has committed
    (``ctem_case_loop.publish_pending``).
    """
    from app.services import ctem_case_loop

    pending = ctem_case_loop.PendingEvents()
    # A re-sighting of an unmatched signal is decided again (#1505): the inventory may hold it now.
    # A re-sighting of a watchlist signal too (#2581): a rising EPSS, a new KEV flag or an asset
    # now internet-facing may confirm it and open its case.
    if not created and row.state not in RE_DECIDED_ON_SIGHTING:
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
        logger.warning(
            "CTEM scope gate failed on signal %s; deciding it again without remote reads",
            getattr(row, "id", "?"),
            exc_info=True,
        )
        try:
            async with db.begin_nested():
                await evaluate(db, row, remote_reads=False)
        except Exception:
            logger.warning(
                "CTEM scope gate not run on signal %s; it stays new for the scheduled recheck",
                getattr(row, "id", "?"),
                exc_info=True,
            )
            recorded(None)
            return pending
    verdict = signals.verdict_of(row.state)
    try:
        # #1951: nothing opens while an administrator paused case opening; the signal waits confirmed.
        from app.services import ctem_case_opening

        if await ctem_case_opening.is_on(db, "cyber"):
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
    await prepare_cve_reads(db, list(rows), profiles={organization_id: profile}, links={organization_id: links})
    counts: dict[str, int] = {}
    for row in rows:
        reader = scenario_reader(row, user_id)
        if reader not in by_reader:
            by_reader[reader] = await read_scenarios(db, user_id=reader)
        await evaluate(
            db,
            row,
            profile=profile,
            scenarios=by_reader[reader],
            primary_assets=links,
            now=now,
            events=events,
            remote_reads=False,
        )
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts


#: How many parked signals one tick decides again after a profile change (#1505); the next tick takes the rest.
RECHECK_BATCH = 500

#: #2542: the decisions of a build before #2542 whose OpenCTI software read failed (no ``incomplete`` flag).
LOOKUP_FAILURES: tuple[str, ...] = ("OpenCTI did not answer", "no OpenCTI access")
#: #2542: how many times an incomplete decision is taken (its first decision included) before it stays failed.
LOOKUP_ATTEMPTS_MAX = 6
#: #2542: the wait before the first retry; each next retry waits twice the previous one (5, 10, 20, 40, 80 min).
LOOKUP_RETRY_BASE = timedelta(minutes=5)


def lookup_backoff(attempts: int) -> timedelta:
    """#2542: how long a decision taken *attempts* times waits before the next retry."""
    return LOOKUP_RETRY_BASE * (2 ** max(0, int(attempts) - 1))


def _lookup_failed(record: Any) -> bool:
    """#2542: *record* is a decision whose OpenCTI software read the subject needed did not answer.

    A record of this build says so (``incomplete``); one of an older build is read from its lookups.
    """
    if not isinstance(record, dict):
        return False
    if "incomplete" in record:
        return record.get("incomplete") is True
    reconcile = record.get("reconcile") if isinstance(record.get("reconcile"), dict) else {}
    return record.get("reason_code") == "subject_unresolved" and any(
        reconcile.get(key) in LOOKUP_FAILURES for key in ("threat_lookup", "cve_lookup")
    )


def _count_lookup_attempts(record: dict[str, Any], previous: Any) -> None:
    """#2542: count the decisions taken while an OpenCTI software read kept failing, on *record*.

    A decision that follows an incomplete one carries ``lookup_attempts`` (the earlier
    count plus this one; an older build's failed decision counts as one); an
    incomplete one also says whether the retry pass takes it again
    (``lookup_retry`` ``scheduled``) or gave up after :data:`LOOKUP_ATTEMPTS_MAX`
    (``exhausted``), so the failed state stays visible.
    """
    before = 0
    if _lookup_failed(previous):
        count = previous.get("lookup_attempts")
        before = count if isinstance(count, int) and count > 0 else 1
    if record.get("incomplete"):
        record["lookup_attempts"] = before + 1
        record["lookup_retry"] = "exhausted" if before + 1 >= LOOKUP_ATTEMPTS_MAX else "scheduled"
    elif before:
        record["lookup_attempts"] = before + 1


async def recheck_lookup_failed(
    db: AsyncSession, *, now: datetime | None = None, batch: int = RECHECK_BATCH
) -> dict[str, int]:
    """#2542: the unmatched signals whose OpenCTI software read failed, decided again with the read retried.

    Selects ``to_validate`` signals left ``subject_unresolved`` with an incomplete
    decision (or, decided by a build before #2542, a ``threat_lookup`` /
    ``cve_lookup`` of :data:`LOOKUP_FAILURES`), taken fewer than
    :data:`LOOKUP_ATTEMPTS_MAX` times, whose last decision is older than
    :func:`lookup_backoff`; *batch* per call, oldest first. The OpenCTI reads are
    taken up front by :func:`prepare_cve_reads` (no row lock held, connection
    released), then each row is decided in its own savepoint. The caller commits.
    """
    from sqlalchemy import and_, or_

    at = now or datetime.now(UTC)
    decision = CtemSignal.decision
    attempts = func.coalesce(decision["lookup_attempts"].as_integer(), 0)
    due = or_(
        *(and_(attempts == n, CtemSignal.updated_at < at - lookup_backoff(n)) for n in range(LOOKUP_ATTEMPTS_MAX))
    )
    failed = or_(
        decision["incomplete"].as_boolean().is_(True),
        and_(
            decision["incomplete"].as_boolean().is_(None),
            or_(
                decision[("reconcile", "threat_lookup")].as_string().in_(LOOKUP_FAILURES),
                decision[("reconcile", "cve_lookup")].as_string().in_(LOOKUP_FAILURES),
            ),
        ),
    )
    rows = list(
        (
            await db.execute(
                select(CtemSignal)
                .where(
                    CtemSignal.state == "to_validate",
                    decision["reason_code"].as_string() == "subject_unresolved",
                    failed,
                    due,
                )
                .order_by(CtemSignal.updated_at)
                .limit(max(1, int(batch)))
            )
        ).scalars()
    )
    return await _decide_again(db, rows, now=at, isolate=True)


async def recheck_parked(
    db: AsyncSession, *, now: datetime | None = None, batch: int = RECHECK_BATCH
) -> dict[str, int]:
    """#1505: the unmatched signals decided before the organization's newest profile version, decided again.

    A change to assets, groups, primary assets, owned domains, software or
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


async def _decide_again(
    db: AsyncSession, rows: list[CtemSignal], *, now: datetime | None = None, isolate: bool = False
) -> dict[str, int]:
    """*rows* decided again, the profile, link index and OpenCRQ read once per organization and reader.

    *isolate* decides each row in its own savepoint: a row the gate cannot decide
    is logged, its ``updated_at`` moved so it waits one grace before the next try
    (never starving the batch), and counted under ``failed``; the others still land. (#2516)
    """
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
    await prepare_cve_reads(db, rows, profiles=profiles, links=links)
    for row in rows:
        reader = scenario_reader(row, fallback)
        if reader not in by_reader:
            by_reader[reader] = await read_scenarios(db, user_id=reader)
        decide = partial(
            evaluate,
            db,
            row,
            profile=profiles[row.organization_id],
            scenarios=by_reader[reader],
            primary_assets=links[row.organization_id],
            now=at,
            remote_reads=False,
        )
        if not isolate:
            await decide()
            counts[row.state] = counts.get(row.state, 0) + 1
            continue
        row_id = getattr(row, "id", "?")
        try:
            async with db.begin_nested():
                await decide()
        except Exception:
            logger.warning("CTEM scope gate: signal %s not decided by the recheck", row_id, exc_info=True)
            row.updated_at = at
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts


#: How long a signal may stay ``new`` before :func:`recheck_new` decides it.
UNDECIDED_GRACE = timedelta(minutes=10)
#: How many ``new`` signals one batch decides (the CVE reads are taken in bulk up front).
RECHECK_NEW_BATCH = RECHECK_BATCH
#: The cadence of :func:`drain_new`, on its own slot, never the hourly case sweep's (#2530).
RECHECK_NEW_SECONDS = 90
#: How long one :func:`drain_new` pass may keep deciding batches before it hands the tick back.
RECHECK_NEW_BUDGET_SECONDS = 20.0


async def recheck_new(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    batch: int = RECHECK_NEW_BATCH,
    grace: timedelta = UNDECIDED_GRACE,
) -> dict[str, int]:
    """The signals still ``new`` past *grace*, decided, *batch* per call, oldest first.

    A signal whose record-time gate failed, or that a case delete released
    (``ctem_case_delete._release_signals``) and whose background re-decision
    never ran (a task lost to a pod restart), would otherwise wait "for
    reconcile" for good: nothing else re-gates ``new``. The gate never decides
    ``new``, so a decided signal leaves the selection; ``updated_at`` moves on
    each decision, so a signal is read at most once per *grace*. The CVE reads are taken
    before any row is written (:func:`prepare_cve_reads`). The caller commits.
    """
    at = now or datetime.now(UTC)
    rows = list(
        (
            await db.execute(
                select(CtemSignal)
                .where(CtemSignal.state == "new", CtemSignal.updated_at < at - grace)
                .order_by(CtemSignal.updated_at)
                .limit(max(1, int(batch)))
            )
        ).scalars()
    )
    return await _decide_again(db, rows, now=at, isolate=True)


async def recheck_new_due() -> bool:
    """Take the :func:`drain_new` slot for this cadence window; True when this tick runs it (or Redis is unavailable)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        return bool(await client.set("ctem:signals:recheck-new", "1", nx=True, ex=RECHECK_NEW_SECONDS))
    except Exception:  # no Redis: every tick drains; the recheck is idempotent past the grace
        return True


async def drain_new(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    batch: int = RECHECK_NEW_BATCH,
    grace: timedelta = UNDECIDED_GRACE,
    budget_seconds: float = RECHECK_NEW_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """The signals still ``new`` past *grace*, decided by :func:`recheck_new` batch after batch (#2530).

    Each batch is committed before the next, and the event loop is yielded between
    them; the pass stops when a batch comes back short (nothing past the grace is
    left) or *budget_seconds* is spent, so a backlog of thousands drains in a few
    passes instead of one batch per hourly case sweep. A row the gate cannot
    decide has its ``updated_at`` moved (``isolate``), so it leaves the selection
    for one grace and never loops. No case is opened here: the case sweep does
    that on its own cadence. Answers the counts by state, ``batches`` included.
    """
    import asyncio

    size = max(1, int(batch))
    started = clock()
    totals: dict[str, int] = {}
    batches = 0
    while True:
        counts = await recheck_new(db, now=now, batch=size, grace=grace)
        await db.commit()
        decided = sum(counts.values())
        if decided:
            batches += 1
            for state, count in counts.items():
                totals[state] = totals.get(state, 0) + count
        if decided < size or clock() - started >= budget_seconds:
            break
        await asyncio.sleep(0)
    if batches:
        totals["batches"] = batches
    return totals


#: The watchlist reasons an inventory link or a software change can lift (#1591 S3).
HELD_BY_SCOPE: tuple[str, ...] = ("primary_asset_below_policy", "criticality_unknown", "below_scope_threshold")


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
    a signal held by ``primary_asset_below_policy``, ``criticality_unknown`` or
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
    have_products = {e.product.strip().lower() for e in profile.software}
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
        data["software"] = list(data.get("software") or []) + [
            {"product": name, "source": INVENTORY_SOURCE} for name in added["products"]
        ]
        data["owned_domains"] = list(data.get("owned_domains") or []) + added["domains"]
        updated = org_context.parse_profile(data)
        basis = f"added from the CTEM Signals page by {actor}"[:500]
        touched = [
            f
            for f, key in (("assets_exposure", "assets"), ("software", "products"), ("owned_domains", "domains"))
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
    outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Dismiss, restore or re-check *rows* (#1505); counts ``changed``, ``skipped`` and by state after. The caller commits.

    With *outcomes*, one entry per row is appended (id, state before and after,
    ``changed``, ``skipped`` or ``unchanged``): a dry run reads them, then rolls back.

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
    if action != "dismiss":
        await prepare_cve_reads(db, [row for row in rows if (action == "restore") == dismissed(row)])
    for row in rows:
        before = row.state
        if action == "dismiss":
            if row.state not in GATED_STATES or dismissed(row):
                counts["skipped"] += 1
                _outcome(outcomes, row, before, "skipped")
                continue
            row.decision = dismissal_record(row, by=actor, at=at)
            row.state = "discarded"
            row.updated_at = at
        else:
            if row.state not in GATED_STATES or (action == "restore") != dismissed(row):
                counts["skipped"] += 1
                _outcome(outcomes, row, before, "skipped")
                continue
            if profile is None:
                profile = await _profile(db, row.organization_id)
            reader = scenario_reader(row, evaluator_id)
            if reader not in by_reader:
                by_reader[reader] = await read_scenarios(db, user_id=reader)
            await evaluate(
                db, row, profile=profile, scenarios=by_reader[reader], now=at, restore=True, remote_reads=False
            )
        changed = row.state != before or action != "recheck"
        counts["changed"] += int(changed)
        counts[row.state] = counts.get(row.state, 0) + 1
        _outcome(outcomes, row, before, "changed" if changed else "unchanged")
    return counts


def _outcome(outcomes: list[dict[str, Any]] | None, row: CtemSignal, before: str, outcome: str) -> None:
    """One triage outcome of *row*, read before a dry run rolls the session back (#2341)."""
    if outcomes is not None:
        outcomes.append({"id": str(row.id), "before": before, "after": row.state, "outcome": outcome})
