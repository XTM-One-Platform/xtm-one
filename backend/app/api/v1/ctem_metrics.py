# SPDX-License-Identifier: Apache-2.0

"""CTEM metrics API for the XTM One command center.

Serves the cross-product Exposure Posture for the **current user**: it queries
OpenCTI / OpenAEV live with that user's per-user Ed25519 JWT (minted by
``ctem_metrics_collector`` via ``integration_tools._get_token``), so the
dashboard shows exactly what the user is allowed to see in each platform. There
is no stored service token and no global scheduled collection. Results are
cached per-user in Redis for a short window. XTM One mode only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_admin, require_ai_enabled
from app.api.v1.helpers import get_user_group_ids, user_visible_filter
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.core.config import settings
from app.core.database import async_session, release_db_connection_before_loop
from app.core.redis import redis_client
from app.core.utils import format_exception_message, is_valid_uuid
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.ctem_posture_snapshot import CtemPostureSnapshot
from app.models.platform_registration import PlatformRegistration
from app.models.user import User
from app.services.ctem_context_scoring import with_context_scores
from app.services.ctem_metrics_collector import build_posture_for_user
from app.services.ctem_movers import DEFAULT_MOVERS, compute_movers, snapshot_objects
from app.services.ctem_scoring import STAGE_KEYS, compute_posture, stage_label
from app.services.ctem_verticals import (
    CTEM_STAGE_AGENT_SLUGS,
    CTEM_VERTICALS,
    STAGE_FACTS,
    normalize_vertical,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_PRODUCTS = ("opencti", "openaev")

# Posture cache (stale-while-revalidate): a cached payload is served fresh within
# the SOFT window; past it the cached copy is STILL served instantly while a
# single background task rebuilds it, so the dashboard is virtually never blocked
# on a live collection. CTEM posture is a slow-moving, daily metric (one snapshot
# per day), so it is collected at most once every 24h per (user, vertical) - a
# 10-minute cadence was needless load on OpenCTI/OpenAEV and PostgreSQL. The HARD
# ttl keeps the stale copy well past the soft window so even a once-a-day visitor
# gets an instant (stale-then-refreshed) load instead of a cold rebuild; it must
# stay > SOFT for stale-while-revalidate to work.
_SOFT_TTL = 86400  # 24 h - refresh in the background past this age
_HARD_TTL = 172800  # 48 h - keep a stale copy this long to serve while refreshing

# The shape of the cached posture and stage facts (#11662). Bump it with any
# change to what a cached payload carries (a new viz key, a renamed fact): a
# posture cached under another shape is stale whatever its age (served once,
# rebuilt at once), and the facts key carries the shape so old facts are never
# read. Without it a key added by a deploy stayed missing for up to 24 h, and
# the page drew its sample in its place. test_ctem_posture_shape pins the viz
# keys of each shape. 8: the Exposed layer reads the profile assets by
# reachability (#12284).
_POSTURE_SHAPE = 8

# A live build hits OpenCTI/OpenAEV for ~20-30s. Two guards keep that load off the
# hot path and off the DB pool:
#  1. a per-(user, vertical) singleflight lock collapses concurrent requests for
#     the SAME key onto ONE build (the rest get its cached result);
#  2. a process-wide semaphore caps how many live builds run at once, so a burst
#     of dashboard loads can never exhaust the DB connection pool or hammer the
#     upstream platforms.
# (Combined with releasing the DB connection across the httpx phase in
# ctem_metrics_collector, a build never pins a pooled connection while it waits on
# external I/O - the root cause of the production pool exhaustion.)
_MAX_CONCURRENT_BUILDS = 4
_build_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BUILDS)
# Per-(user, vertical) singleflight locks, ref-counted so an idle lock is evicted
# the moment no coroutine is using it (see _acquire_build_lock/_release_build_lock).
# Without the refcount this dict would grow without bound on a long-lived worker
# serving many distinct users - a slow memory leak.
_build_locks: dict[str, asyncio.Lock] = {}
_build_lock_refs: dict[str, int] = {}
_build_locks_guard = asyncio.Lock()
_bg_refresh_tasks: set[asyncio.Task] = set()
_bg_refresh_inflight: set[str] = set()
# Cap how many stale-while-revalidate background refreshes are in flight at once.
# Each refresh briefly opens a short-lived session; without a cap, a stale burst
# from many distinct users (e.g. 350 at once) would spawn one task + session per
# user. Capped, the rest keep serving the stale cache and refresh on a later load
# when there is room - build throughput is bounded by the semaphore anyway.
_MAX_INFLIGHT_REFRESHES = _MAX_CONCURRENT_BUILDS * 4


def _require_xtm_one() -> None:
    if settings.is_copilot_like:
        raise HTTPException(
            status_code=404, detail="CTEM metrics are only available in XTM One mode"
        )


def _canon(identifier: str) -> str:
    return "openaev" if identifier in ("openaev", "openbas") else identifier


def _fact(label: str, value: Any, *, unit: str = "", kind: str = "fact") -> dict:
    # Percentages / ratios arrive from pct() as raw floats with full precision
    # (e.g. 5.751765893037336). Round numeric values to whole numbers for display
    # so they match the donut score, the headline meter and the sample
    # placeholders - all of which are integers. (``bool`` is a subclass of
    # ``int`` but never a ``float``, so flags are left untouched.)
    if isinstance(value, float):
        value = round(value)
    return {"label": label, "value": value, "unit": unit, "kind": kind}


def _resolve_vertical(user: User, vertical: str | None) -> str:
    """Resolve the active CTEM vertical: explicit param > user preference > cyber."""
    return normalize_vertical(vertical or getattr(user, "ctem_vertical", None))


def _findings_split(facts: dict[str, Any]) -> dict[str, int] | None:
    """OpenAEV's open findings and the share carrying a CVE, or None without OpenAEV."""
    total = facts.get("findings_total")
    if not isinstance(total, int) or isinstance(total, bool):
        return None
    with_cve = facts.get("findings_cve")
    with_cve = with_cve if isinstance(with_cve, int) and not isinstance(with_cve, bool) else 0
    return {"total": total, "with_cve": min(with_cve, total)}


def _stage_viz(stage: str, facts: dict[str, Any], vertical: str) -> dict[str, Any]:
    """Optional richer viz payload per (vertical, stage)."""
    if vertical == "cyber" and stage == "prioritization":
        return {
            "severity_distribution": facts.get("severity_distribution") or [],
            "top_vulnerabilities": facts.get("top_vulnerabilities") or [],
            # The prioritization funnel (#11546), None before OpenCTI answers.
            "funnel": facts.get("funnel"),
            # The Prioritization page's mixed views (#11998): OpenAEV's findings on
            # the assets, split by whether they carry a CVE; None before OpenAEV answers.
            "findings": _findings_split(facts),
        }
    if vertical == "cyber" and stage == "discovery":
        # The Discovery page's mixed views (#11998): each list only when OpenCTI
        # answered it, so an absent card is a gap filler, never an empty frame.
        keys = ("techniques_by_tactic", "top_threats", "target_sectors", "target_countries")
        return {k: v for k in keys if (v := facts.get(k))}
    if stage == "scoping":
        # The Scoping hero ring: one segment per core OpenAEV attack domain.
        domains = facts.get("attack_domains")
        viz = {"attack_domains": domains} if domains else {}
        if vertical == "cyber":
            # The Scoping page's mixed views (#11998): each list only when OpenCTI
            # answered it, so an absent card is a gap filler, never an empty frame.
            for k in ("observables_by_type", "vulnerabilities_by_severity"):
                if v := facts.get(k):
                    viz[k] = v
        return viz
    if stage == "validation":
        viz: dict[str, Any] = {}
        # OpenAEV per-type success donuts (Prevention/Detection/...) when present.
        buckets = facts.get("expectation_buckets")
        if buckets:
            viz["expectation_buckets"] = buckets
        # The Validation hero: the outcome matrix of whichever kill chains the data carries.
        matrix = facts.get("outcome_matrix")
        if matrix:
            viz["outcome_matrix"] = matrix
        if vertical == "cyber":
            # The Validation page's mixed views (#11998): OpenCTI's rule coverage per
            # tactic and its detection depth, each only when OpenCTI answered it, so an
            # absent card is a gap filler, never an empty frame.
            for k in ("rule_coverage_by_tactic", "detection_depth"):
                if v := facts.get(k):
                    viz[k] = v
        return viz
    if vertical == "cyber" and stage == "remediation":
        # The Mobilization page's mixed views (#11998): OpenCTI's response mix and
        # progress, OpenAEV's fix backlog, each only when its product answered it, so
        # an absent card is a gap filler, never an empty frame.
        keys = ("response_mix", "response_progress", "fix_backlog")
        return {k: v for k in keys if (v := facts.get(k))}
    return {}


def _stage_display(stage: str, facts: dict[str, Any], vertical: str) -> dict[str, Any]:
    """Build the display facts + optional viz payload for a stage in a vertical."""
    specs = STAGE_FACTS.get(vertical, {}).get(stage, [])
    out = [_fact(label, facts.get(key), unit=unit, kind=kind) for (label, key, unit, kind) in specs]
    return {
        "facts": [f for f in out if f["value"] is not None],
        "viz": _stage_viz(stage, facts, vertical),
    }


_HISTORY_DAYS = 90


def _numeric_metrics(facts: dict[str, Any]) -> dict[str, float]:
    """The scalar numeric metrics from a stage's facts (the readiness/efficiency
    signals: %s + counts), excluding bools, None, lists and viz blobs."""
    return {
        k: v
        for k, v in (facts or {}).items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _build_stage_metrics(
    posture: dict[str, Any], stage_facts: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """{stage: {"score": int|null, "<numeric metric>": value, ...}} for snapshotting."""
    return {
        s["key"]: {"score": s.get("score"), **_numeric_metrics(stage_facts.get(s["key"], {}))}
        for s in posture.get("stages", [])
    }


async def _record_and_load_history(
    db: AsyncSession,
    user_id: Any,
    vertical: str,
    posture: dict[str, Any],
    stage_facts: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
    """Upsert today's posture snapshot and return ``(trend, prev_stage_scores)``.

    ``trend`` is ``[{date, score}, ...]`` over the last ``_HISTORY_DAYS`` days
    (oldest first, including today). ``prev_stage_scores`` is the per-stage scores
    from the most recent snapshot *before* today, for period-over-period deltas.
    The full per-stage metric history is served separately by ``/posture/history``.
    Best-effort: history is a nice-to-have and must never break the live posture.
    """
    today = datetime.now(UTC).date()
    stage_metrics = _build_stage_metrics(posture, stage_facts)
    # The per-object values of the risk movers; a build that measured none keeps
    # the day's stored objects instead of erasing them.
    objects = snapshot_objects(stage_facts)
    updates: dict[str, Any] = {
        "posture_score": posture.get("posture_score"),
        "stage_metrics": stage_metrics,
        "updated_at": datetime.now(UTC),
    }
    if objects is not None:
        updates["objects"] = objects
    try:
        await db.execute(
            pg_insert(CtemPostureSnapshot)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                vertical=vertical,
                snapshot_date=today,
                posture_score=posture.get("posture_score"),
                stage_metrics=stage_metrics,
                objects=objects,
            )
            .on_conflict_do_update(
                constraint="uq_ctem_snapshot_user_vertical_date",
                set_=updates,
            )
        )
        await db.commit()
    except Exception as exc:
        logger.warning("CTEM snapshot upsert failed for user %s: %s", user_id, format_exception_message(exc))
        await db.rollback()

    trend: list[dict[str, Any]] = []
    prev_stage_scores: dict[str, int | None] = {}
    try:
        rows = (
            (
                await db.execute(
                    select(CtemPostureSnapshot)
                    .where(
                        CtemPostureSnapshot.user_id == user_id,
                        CtemPostureSnapshot.vertical == vertical,
                        CtemPostureSnapshot.snapshot_date >= today - timedelta(days=_HISTORY_DAYS),
                    )
                    .order_by(CtemPostureSnapshot.snapshot_date)
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            trend.append({"date": r.snapshot_date.isoformat(), "score": r.posture_score})
            # Rows are ascending, so the last one before today wins as "previous".
            if r.snapshot_date < today:
                prev_stage_scores = {
                    k: (m or {}).get("score") for k, m in (r.stage_metrics or {}).items()
                }
    except Exception as exc:
        logger.warning("CTEM history load failed for user %s: %s", user_id, format_exception_message(exc))

    return trend, prev_stage_scores


async def _build_posture_payload(
    db: AsyncSession, user: User, vertical: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Assemble the full posture response for *user* in *vertical* (live, scoped).

    Returns ``(payload, stage_facts)`` so the caller can cache both the display
    payload (``/posture``) and the raw per-stage facts (``/stages/{stage}``) from
    a single live build.
    """
    reg_result = await db.execute(
        select(PlatformRegistration).where(PlatformRegistration.status == "active")
    )
    registrations = reg_result.scalars().all()
    # OpenAEV and OpenBAS both canonicalize to "openaev"; when both are active,
    # deterministically prefer the row whose identifier matches the canonical key
    # so the surfaced url/version does not depend on DB row ordering.
    reg_by_provider: dict[str, PlatformRegistration] = {}
    for r in registrations:
        key = _canon(r.platform_identifier)
        existing = reg_by_provider.get(key)
        if existing is None or r.platform_identifier == key:
            reg_by_provider[key] = r

    stage_facts, present = await build_posture_for_user(db, str(user.id), vertical)
    posture = compute_posture(stage_facts, present, vertical)

    # Record today's snapshot + load history for the trend chart and the
    # period-over-period per-stage deltas.
    trend, prev_stage_scores = await _record_and_load_history(
        db, user.id, vertical, posture, stage_facts
    )

    # Resolve the SINGLE owning analyst per stage (per-user override -> platform
    # default -> seeded ctem-{stage}-analyst) so each card shows one clear owner,
    # vertical-aware. Candidate override ids (user + platform) are batch-loaded.
    ps = await get_platform_settings(db)
    platform_stage_map = getattr(ps, "ctem_stage_agent_ids", None)
    user_stage_map = getattr(user, "ctem_stage_agent_ids", None)
    seeded_agents = {
        a.slug: a
        for a in (
            await db.execute(
                select(Agent).where(Agent.slug.in_(list(CTEM_STAGE_AGENT_SLUGS.values())))
            )
        ).scalars()
    }
    per_stage_ids: dict[str, tuple[str | None, str | None]] = {}
    override_ids: set[str] = set()
    for stage_key in STAGE_KEYS:
        uid = _vertical_stage_override_id(user_stage_map, vertical, stage_key)
        pid = _vertical_stage_override_id(platform_stage_map, vertical, stage_key)
        per_stage_ids[stage_key] = (uid, pid)
        for cid in (uid, pid):
            if cid and is_valid_uuid(cid):
                override_ids.add(cid)
    override_agents: dict[str, Agent] = {}
    if override_ids:
        override_agents = {
            str(a.id): a
            for a in (
                await db.execute(select(Agent).where(Agent.id.in_(override_ids)))
            ).scalars()
        }

    def _stage_owner(stage_key: str) -> Agent | None:
        # per-user override first, then platform default, then seeded.
        uid, pid = per_stage_ids.get(stage_key, (None, None))
        for cid in (uid, pid):
            if cid and cid in override_agents and override_agents[cid].enabled:
                return override_agents[cid]
        return seeded_agents.get(CTEM_STAGE_AGENT_SLUGS.get(stage_key, ""))

    for stage_obj in posture["stages"]:
        key = stage_obj["key"]
        stage_obj.update(_stage_display(key, stage_facts.get(key, {}), vertical))
        stage_obj["products_present"] = [
            p for p in stage_obj["primary_products"] + stage_obj["secondary_products"]
            if p in present
        ]
        # Period-over-period delta: the stage's score in the most recent snapshot
        # before today (None until a prior day's snapshot exists).
        stage_obj["prev_score"] = prev_stage_scores.get(key)
        owner = _stage_owner(key)
        stage_obj["agent"] = (
            {"id": str(owner.id), "name": owner.name, "slug": owner.slug} if owner else None
        )

    now = datetime.now(UTC)
    products: dict[str, Any] = {}
    for provider in _PRODUCTS:
        reg = reg_by_provider.get(provider)
        products[provider] = {
            "present": provider in present,
            "url": reg.platform_url if reg else None,
            "title": reg.platform_title if reg else None,
            "version": reg.platform_version if reg else None,
            # Connectivity health snapshot. The dashboard reads the LIVE value
            # from /platform/registrations (this payload is cached up to ~24h), so
            # a recovered platform turns green immediately instead of staying red
            # until the posture cache expires. Kept here for completeness.
            "healthy": reg.is_healthy(now) if reg else False,
            "last_sync": None,
        }
    # OpenCRQ does not feed CTEM posture facts yet (no stage collectors), so
    # "present" is registration-driven: a registered instance is live (url /
    # version / health surfaced like the other products) and only an
    # unregistered slot keeps the "coming soon" placeholder.
    grc_reg = reg_by_provider.get("opencrq")
    products["opencrq"] = {
        "present": grc_reg is not None,
        "url": grc_reg.platform_url if grc_reg else None,
        "title": grc_reg.platform_title if grc_reg else None,
        "version": grc_reg.platform_version if grc_reg else None,
        "healthy": grc_reg.is_healthy(now) if grc_reg else False,
        "last_sync": None,
        "coming_soon": grc_reg is None,
    }

    blind_spots = [
        {"stage": s["key"], "label": s["label"], "missing_products": s["missing_products"]}
        for s in posture["stages"]
        if not s["covered"] and s["missing_products"]
    ]

    prio = stage_facts.get("prioritization", {})
    disc = stage_facts.get("discovery", {})
    val = stage_facts.get("validation", {})
    scop = stage_facts.get("scoping", {})
    rem = stage_facts.get("remediation", {})
    _vs = [
        v
        for v in (val.get("prevention_success_pct"), val.get("detection_success_pct"))
        if isinstance(v, (int, float))
    ]
    # Vertical-complete headline: each vertical's collector only emits its own
    # facts, so the keys for the other verticals stay None and the UI picks the
    # KPIs relevant to the active vertical.
    headline = {
        # Cyber
        "kev_count": prio.get("kev_count"),
        # Known-exploited restricted to the organization profile's software inventory, and
        # the matched products (at most 3) the Act now row cites; None without a
        # software or when OpenCTI could not answer (#11213).
        "kev_tailored_count": prio.get("kev_tailored_count"),
        "kev_tailored_products": prio.get("kev_tailored_products"),
        "critical_count": prio.get("critical_count"),
        "vulnerabilities_total": prio.get("vulnerabilities_total"),
        "detection_coverage_pct": disc.get("detection_coverage_pct"),
        "prevention_success_pct": val.get("prevention_success_pct"),
        "detection_success_pct": val.get("detection_success_pct"),
        "exercised_coverage_pct": val.get("exercised_coverage_pct"),
        "validation_success_pct": round(sum(_vs) / len(_vs)) if _vs else None,
        # FIMI / disinformation
        "narratives_total": scop.get("narratives_total"),
        "channels_total": scop.get("channels_total"),
        "attribution_pct": prio.get("attribution_pct"),
        "corroboration_pct": val.get("corroboration_pct"),
        "incidents": val.get("incidents"),
        # Fraud
        "phishing_assets": disc.get("phishing_assets"),
        "credentials_total": disc.get("credentials_total"),
        "confirmed_pct": val.get("confirmed_pct"),
        "containment_pct": prio.get("containment_pct"),
        # Shared
        "case_closure_pct": rem.get("case_closure_pct"),
    }
    # Percentages arrive from pct() as raw floats with full precision (e.g.
    # 5.751765893037336). Round every numeric headline value to a whole number
    # so the KPI tiles, the recommended-action copy (e.g. "Only 6% of ATT&CK
    # techniques...") and the AI briefing/assessment prompts all read the same
    # clean integers as the stage facts and donut scores. (``bool`` is a
    # subclass of ``int`` but never a ``float``, so flags are left untouched.)
    headline = {
        key: (round(value) if isinstance(value, float) else value)
        for key, value in headline.items()
    }

    payload = {
        **posture,
        "products": products,
        "trend": trend,
        "blind_spots": blind_spots,
        "headline": headline,
        "last_sync": {},
        # When this live snapshot was built + how long it is cached, so the UI can
        # show "last synced X ago - next in Y" instead of a vague "continuously
        # synced". Past this age the cached copy is still served instantly while a
        # background task refreshes it (stale-while-revalidate).
        "generated_at": now.isoformat(),
        "cache_ttl_seconds": _SOFT_TTL,
    }
    return payload, stage_facts


def _posture_cache_keys(user_id: Any, vertical: str) -> tuple[str, str]:
    """``(display payload key, raw stage-facts key)`` for a (user, vertical).

    The facts key carries the shape: the facts have no ``generated_at`` to age,
    so facts cached by an older build are left behind rather than read.
    """
    return (
        f"ctem:posture:{user_id}:{vertical}",
        f"ctem:facts:v{_POSTURE_SHAPE}:{user_id}:{vertical}",
    )


async def _redis_or_none():
    """The Redis client, or ``None`` if it is unavailable (degrade gracefully)."""
    try:
        return await redis_client()
    except Exception as exc:
        logger.debug("CTEM posture cache unavailable: %s", format_exception_message(exc))
        return None


async def _cache_get_json(client, key: str) -> Any | None:
    if client is None:
        return None
    try:
        raw = await client.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug("CTEM cache read failed (%s): %s", key, format_exception_message(exc))
        return None


async def _cache_set_json(client, key: str, value: Any, ttl: int) -> None:
    if client is None:
        return
    try:
        await client.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception as exc:
        logger.debug("CTEM cache write failed (%s): %s", key, format_exception_message(exc))


def _payload_age_seconds(payload: Any) -> float | None:
    """Seconds since a cached payload was built (from its ``generated_at``)."""
    ts = payload.get("generated_at") if isinstance(payload, dict) else None
    if not ts:
        return None
    try:
        built = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if built.tzinfo is None:
        built = built.replace(tzinfo=UTC)
    return (datetime.now(UTC) - built).total_seconds()


async def _profile_stamp(db: AsyncSession | None) -> str | None:
    """The fingerprint of the organization profile inputs a posture is built from, ``None`` when none is saved.

    The build reads the software and the assets and groups of the profile
    (``ctem_posture_inputs`` declares them), so a posture stamped with another
    fingerprint is stale whatever its age: a change to those inputs shows on
    the next load (rebuilt in the background). A save, an import or a sync that
    leaves them as they were (a description, Owned domains, a PIR detail, a
    sync writing the same rows) keeps the cached posture fresh.
    """
    from app.services.ctem_posture_inputs import posture_inputs_stamp

    return await posture_inputs_stamp(db)


def _cached_is_fresh(payload: Any, *, profile_stamp: str | None) -> bool:
    """A cached posture is served as fresh only when it has today's shape, was
    built from the current organization profile version (*profile_stamp*) and is
    younger than the soft TTL; anything else (older shape, another profile
    version, no or unparseable age) is stale and rebuilt."""
    if not isinstance(payload, dict) or payload.get("shape") != _POSTURE_SHAPE:
        return False
    if payload.get("profile_version") != profile_stamp:
        return False
    age = _payload_age_seconds(payload)
    return age is not None and age < _SOFT_TTL


async def _acquire_build_lock(key: str) -> asyncio.Lock:
    """Return the per-(user, vertical) singleflight lock for *key*, creating it on
    first use and bumping its refcount so it is not evicted while in use."""
    async with _build_locks_guard:
        lock = _build_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _build_locks[key] = lock
        _build_lock_refs[key] = _build_lock_refs.get(key, 0) + 1
        return lock


async def _release_build_lock(key: str) -> None:
    """Drop a reference to *key*'s singleflight lock, evicting it once no coroutine
    is using it so ``_build_locks`` cannot grow without bound on long-lived workers.

    Safe because the cache is re-checked INSIDE the lock: if a later caller races
    in and recreates the lock after eviction, the freshly-cached payload from the
    just-finished build short-circuits the rebuild anyway.
    """
    async with _build_locks_guard:
        remaining = _build_lock_refs.get(key, 0) - 1
        if remaining > 0:
            _build_lock_refs[key] = remaining
        else:
            _build_lock_refs.pop(key, None)
            _build_locks.pop(key, None)


async def _build_and_cache(
    db: AsyncSession, user: User, vertical: str, client, *, force: bool = False
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build the live posture ONCE (singleflight + global concurrency cap) and
    cache both the display payload and the raw per-stage facts.

    Returns ``(payload, stage_facts)``. Concurrent callers for the same
    (user, vertical) collapse onto the first build and receive its cached result
    instead of each launching their own ~20-30s collection.

    When *force* is true the cached short-circuit is skipped and a live build
    always runs (used by ``/sync`` so a forced refresh rebuilds even if a fresh
    cache entry is still present - e.g. when the cache clear failed).
    """
    posture_key, facts_key = _posture_cache_keys(user.id, vertical)
    # Read before the build: a profile saved during the build leaves this
    # posture stamped with the older version, so the next read rebuilds it.
    profile_stamp = await _profile_stamp(db)
    lock = await _acquire_build_lock(posture_key)
    try:
        async with lock:
            # A sibling builder for the same key may have populated the cache while
            # we waited for the lock - serve that instead of rebuilding (unless a
            # forced rebuild was requested). A missing/unparseable ``generated_at``
            # or an older shape is treated as stale and rebuilt, mirroring ``ctem_posture``.
            if not force:
                cached = await _cache_get_json(client, posture_key)
                if _cached_is_fresh(cached, profile_stamp=profile_stamp):
                    facts = await _cache_get_json(client, facts_key) or {}
                    return cached, facts
            # Do NOT hold a pooled DB connection while queued behind the build
            # semaphore. Under a many-user burst (e.g. 350 users hitting a cold or
            # stale cache at once) only _MAX_CONCURRENT_BUILDS build at a time and
            # the rest wait here; a connection pinned across that wait - on top of
            # the one get_current_user already opened - is exactly what exhausts
            # the pool and overloads PostgreSQL. Release it first; the build
            # transparently re-acquires once it holds a slot (and releases again
            # across the httpx collection in ctem_metrics_collector).
            if db is not None:
                await release_db_connection_before_loop(db)
            async with _build_semaphore:
                payload, stage_facts = await _build_posture_payload(db, user, vertical)
            # Stamped where it is cached, so every cached posture carries its
            # shape and the profile version it was built from.
            payload["shape"] = _POSTURE_SHAPE
            payload["profile_version"] = profile_stamp
            await _cache_set_json(client, posture_key, payload, _HARD_TTL)
            await _cache_set_json(client, facts_key, stage_facts, _HARD_TTL)
            return payload, stage_facts
    finally:
        await _release_build_lock(posture_key)


async def _posture_stage_facts(
    db: AsyncSession, user: User, vertical: str, client
) -> dict[str, dict[str, Any]]:
    """Raw per-stage facts for (user, vertical) - from cache, or a singleflight
    live build that also warms the posture cache. Lets ``/stages`` reuse the
    posture build instead of triggering an independent uncached full collection.
    """
    posture_key, facts_key = _posture_cache_keys(user.id, vertical)
    facts = await _cache_get_json(client, facts_key)
    if facts is not None:
        # Facts of another profile version are served this once while the
        # posture (and its facts) is rebuilt in the background.
        profile_stamp = await _profile_stamp(db)
        if not _cached_is_fresh(await _cache_get_json(client, posture_key), profile_stamp=profile_stamp):
            _schedule_posture_refresh(str(user.id), vertical, profile_stamp)
        return facts
    _, stage_facts = await _build_and_cache(db, user, vertical, client)
    return stage_facts


def _schedule_posture_refresh(user_id: str, vertical: str, profile_stamp: str | None) -> None:
    """Fire-and-forget background refresh of a stale posture cache, deduped per
    key. The dashboard keeps serving the stale copy while this runs."""
    posture_key, _ = _posture_cache_keys(user_id, vertical)
    if posture_key in _bg_refresh_inflight:
        return
    # Bound the fan-out: under a stale burst from many users, keep serving the
    # stale cache rather than spawning a task + session per user. A later load
    # refreshes this key once there is room.
    if len(_bg_refresh_inflight) >= _MAX_INFLIGHT_REFRESHES:
        return
    _bg_refresh_inflight.add(posture_key)
    task = asyncio.create_task(_refresh_posture_bg(user_id, vertical, profile_stamp))
    _bg_refresh_tasks.add(task)

    def _cleanup(t: asyncio.Task) -> None:
        _bg_refresh_tasks.discard(t)
        _bg_refresh_inflight.discard(posture_key)
        if not t.cancelled() and t.exception() is not None:
            logger.warning(
                "CTEM background posture refresh failed for %s/%s: %s",
                user_id,
                vertical,
                t.exception(),
            )

    task.add_done_callback(_cleanup)


async def _refresh_posture_bg(user_id: str, vertical: str, profile_stamp: str | None) -> None:
    """Background posture rebuild on its OWN short-lived session (never the
    request's), so it can never pin a request connection. Bounded by the same
    singleflight lock + build semaphore as the foreground path. *profile_stamp*
    is the profile version the request saw, so the pre-check needs no session."""
    try:
        uid = uuid.UUID(user_id)
    except (TypeError, ValueError):
        return
    client = await _redis_or_none()
    # Re-check freshness from Redis (no DB) before opening a session: a sibling
    # refresh may have already rebuilt this key, so under a many-user stale burst
    # we skip the redundant session + build entirely.
    posture_key, _ = _posture_cache_keys(uid, vertical)
    cached = await _cache_get_json(client, posture_key)
    if _cached_is_fresh(cached, profile_stamp=profile_stamp):
        return
    async with async_session() as db:
        user = await db.get(User, uid)
        if user is None:
            return
        await _build_and_cache(db, user, vertical, client)


@router.get("/posture")
async def ctem_posture(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Cross-product Exposure Posture for the current user (live, scoped, cached).

    The active vertical (cyber / fimi / fraud) comes from the ``vertical`` query
    param, then the user's saved preference, then cyber. Served stale-while-
    revalidate from Redis (key ``ctem:posture:{user.id}:{vertical}``): a fresh
    cache hit returns immediately; a stale hit returns immediately AND kicks off a
    single background rebuild; a cold cache builds inline under a singleflight lock
    + concurrency cap. The dashboard renders its greyed "sample" shell while this
    first build runs (it does not gate its render on the load) and swaps in the
    live data when it lands - so the user is never shown a blank screen, and a
    REFRESH keeps the existing live data on screen (the cached value stays until
    the rebuild replaces it). ``/sync`` forces a rebuild.
    """
    _require_xtm_one()
    vertical = _resolve_vertical(user, vertical)

    posture_key, _ = _posture_cache_keys(user.id, vertical)
    client = await _redis_or_none()

    cached = await _cache_get_json(client, posture_key)
    if cached is not None:
        profile_stamp = await _profile_stamp(db)
        stale = not _cached_is_fresh(cached, profile_stamp=profile_stamp)
        if stale:
            # Stale-while-revalidate: serve the cached copy instantly and refresh
            # in the background so the user never waits on a live collection.
            # An older shape (#11662) or another organization profile version
            # is stale too: it is served this once.
            _schedule_posture_refresh(str(user.id), vertical, profile_stamp)
        return {**await _with_context(db, cached), "refreshing": _posture_refreshing(posture_key, stale=stale)}

    logger.info("CTEM posture: cache miss — building live %s for user %s", vertical, user.id)
    payload, _ = await _build_and_cache(db, user, vertical, client)
    return {**await _with_context(db, payload), "refreshing": False}


def _posture_refreshing(posture_key: str, *, stale: bool) -> bool:
    """Whether the posture served is being rebuilt: a stale copy (each read of
    one schedules a deduped rebuild, on whichever worker answers) or a rebuild
    in flight on this worker. The page shows it as a spinner and polls faster
    until the new ``generated_at`` lands (#12183)."""
    return stale or posture_key in _bg_refresh_inflight


async def _with_context(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    """The posture with its qualitative scores and the organization context state.

    Applied on every read, never cached: a context saved in Settings shows on
    the next load without a posture rebuild. The organization context of
    Settings is the only company context read (#11464). Its software is the
    Software entities' (#1495, ``software_profile``): a synced inventory fills
    the field with no profile row typed.
    """
    from app.services import organization_context as org_context

    has_profile = False
    missing: list[str] = []
    try:
        row, profile = await org_context.software_profile(db)
        if row is not None:
            has_profile = True
            missing = org_context.missing_fields(profile)
    except Exception as exc:  # an unreadable profile reads as missing, never a 500
        logger.info("CTEM organization context unreadable: %s", format_exception_message(exc))
        has_profile = False
    return with_context_scores(payload, has_profile=has_profile, missing_fields=missing)


@router.get("/posture/history")
async def ctem_posture_history(
    vertical: str | None = Query(default=None),
    days: int = Query(default=_HISTORY_DAYS, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Historical posture for the current user + vertical (oldest first).

    One entry per recorded day: the composite posture score and, per stage, the
    score + the readiness/efficiency metrics that were snapshotted. Powers the
    dashboard trend chart and the per-stage history charts. Not cached - reads
    straight from ``ctem_posture_snapshots``.
    """
    _require_xtm_one()
    vertical = _resolve_vertical(user, vertical)
    since = datetime.now(UTC).date() - timedelta(days=days)
    rows = (
        (
            await db.execute(
                select(CtemPostureSnapshot)
                .where(
                    CtemPostureSnapshot.user_id == user.id,
                    CtemPostureSnapshot.vertical == vertical,
                    CtemPostureSnapshot.snapshot_date >= since,
                )
                .order_by(CtemPostureSnapshot.snapshot_date)
            )
        )
        .scalars()
        .all()
    )
    return {
        "vertical": vertical,
        "history": [
            {
                "date": r.snapshot_date.isoformat(),
                "posture_score": r.posture_score,
                "stage_metrics": r.stage_metrics or {},
            }
            for r in rows
        ],
    }


@router.get("/posture/movers")
async def ctem_posture_movers(
    vertical: str | None = Query(default=None),
    days: int = Query(default=1, ge=1, le=_HISTORY_DAYS),
    limit: int = Query(default=DEFAULT_MOVERS, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The briefing's risk movers for the current user + vertical (#11547).

    Compares the latest snapshot that holds per-object values with the latest
    one at least ``days`` days older, both the current user's own rows (built
    from their own reads). ``status`` is ``insufficient_history`` until two
    such days exist; never a guessed value. Not cached: two indexed rows.
    """
    _require_xtm_one()
    vertical = _resolve_vertical(user, vertical)
    base = (
        select(CtemPostureSnapshot)
        .where(
            CtemPostureSnapshot.user_id == user.id,
            CtemPostureSnapshot.vertical == vertical,
            CtemPostureSnapshot.objects.is_not(None),
        )
        .order_by(CtemPostureSnapshot.snapshot_date.desc())
        .limit(1)
    )
    current = (await db.execute(base)).scalar_one_or_none()
    previous = None
    if current is not None:
        previous = (
            await db.execute(
                base.where(CtemPostureSnapshot.snapshot_date <= current.snapshot_date - timedelta(days=days))
            )
        ).scalar_one_or_none()
    movers = compute_movers(
        previous.objects if previous is not None else None,
        current.objects if current is not None else None,
        since=previous.snapshot_date.isoformat() if previous is not None else None,
        limit=limit,
    )
    return {
        "vertical": vertical,
        "as_of": current.snapshot_date.isoformat() if current is not None else None,
        **movers,
    }


@router.get("/stages/{stage}")
async def ctem_stage_detail(
    stage: str,
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Live, per-user facts + display payload for a single CTEM stage.

    Reuses the cached posture facts (shared singleflight build) rather than
    triggering an independent full collection on every drill-in.
    """
    _require_xtm_one()
    if stage not in STAGE_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown stage: {stage}")
    vertical = _resolve_vertical(user, vertical)

    stage_facts = await _posture_stage_facts(db, user, vertical, await _redis_or_none())
    merged = stage_facts.get(stage, {})
    # ``_stage_display`` also returns a ``facts`` key (the curated display list),
    # so expose the raw merged dict under ``raw_facts`` to avoid the ``**``
    # expansion clobbering it (the frontend reads ``CtemStageDetail.raw_facts``).
    return {
        "stage": stage,
        "label": stage_label(stage, vertical),
        "raw_facts": merged,
        **_stage_display(stage, merged, vertical),
    }


@router.post("/sync")
async def refresh_ctem_posture(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Force a live rebuild of the user's posture for the ACTIVE vertical.

    Deterministic, so NOT AI-gated: the rebuild aggregates OpenCTI / OpenAEV /
    OpenCRQ figures over HTTP (``ctem_metrics_collector``) and calls no model,
    starts no run and consumes no quota - the same build ``GET /posture`` runs
    on a cache miss with no gate. A Community Edition or AI-disabled XTM One
    must be able to refresh its own posture (#9194). The briefing and stage
    assessment refreshes below stay gated: they start an agent run.

    Clears the user's cached posture (all verticals) and force-rebuilds the
    active vertical INLINE (``force=True`` bypasses the cached short-circuit),
    returning only once that rebuild completes. The cache clear and the re-cache
    of the fresh payload are both best-effort (Redis errors are swallowed), so a
    warmed cache is not guaranteed - but because the rebuild is forced, a failed
    clear can never turn this into a no-op success.

    The dashboard's refresh button spins for the whole rebuild while the existing
    live data stays on screen (never the sample), then swaps to the fresh values
    once this returns. A genuine build failure returns 503 (the client keeps the
    existing data; the next load rebuilds).
    """
    _require_xtm_one()
    active = _resolve_vertical(user, vertical)
    client = await _redis_or_none()
    if client is not None:
        try:
            keys: list[str] = []
            for v in CTEM_VERTICALS:
                posture_key, facts_key = _posture_cache_keys(user.id, v)
                keys.extend((posture_key, facts_key))
            deleted = await client.delete(*keys)
            logger.info(
                "CTEM force-sync: cleared %s cached posture keys for user %s",
                deleted,
                user.id,
            )
        except Exception as exc:
            logger.warning("CTEM force-sync: cache clear failed for user %s: %s", user.id, format_exception_message(exc))
    # Force the rebuild of the active vertical inline (force=True bypasses the
    # cached short-circuit), then re-warm its cache. Forcing matters when the
    # clear above failed: without it _build_and_cache could serve the still-fresh
    # stale entry and make a "force sync" a silent no-op. A genuine build failure
    # surfaces as 503 instead of a misleading "refreshed" (the client keeps the
    # existing live data on screen; the next load rebuilds the key).
    try:
        await _build_and_cache(db, user, active, client, force=True)
    except Exception as exc:
        logger.warning("CTEM force-sync: rebuild failed for user %s: %s", user.id, format_exception_message(exc))
        raise HTTPException(
            status_code=503,
            detail="Could not rebuild exposure posture; existing data was kept.",
        ) from exc
    return {"status": "refreshed"}


# ── Exposure briefing (seeded agent artifact, PER USER + EE-gated) ───────────

_BRIEFING_AGENT_SLUG = "exposure-briefing-analyst"
_BRIEFING_MANUAL_ASSIGNMENT = "Generate my exposure briefing"
_ACTIVE_RUN_STATUSES = ("pending", "delayed", "running", "awaiting_human")


#: How old a stage assessment may be before the stage page regenerates it on
#: visit: the weekly cadence of the ``Weekly <stage> assessment`` fan-out.
STAGE_ASSESSMENT_FRESH_DAYS = 7

#: How old the exposure briefing may be before the command center regenerates
#: it on landing (#12183). The monthly fan-out alone left it weeks old.
BRIEFING_FRESH_HOURS = 24

#: The organization-context info line an agent is told to add to its briefs
#: (``organization_context.BRIEF_NOTICE_LINE``). The CTEM pages already show it
#: as a banner, so it is removed from the prose they render (#12002).
_BRIEF_NOTICE_RE = re.compile(
    r"\s*Organization context incomplete: missing .*?\. The analysis would be sharper with them\.",
    re.DOTALL,
)


def _strip_brief_notice(value: Any) -> Any:
    """*value* with the organization-context info line removed from every string."""
    if isinstance(value, str):
        return _BRIEF_NOTICE_RE.sub("", value).strip()
    if isinstance(value, list):
        return [_strip_brief_notice(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_brief_notice(item) for key, item in value.items()}
    return value


def _parse_briefing(raw: str | None) -> dict | None:
    """Parse the briefing JSON from a run's result_summary (fence-tolerant).

    The organization-context info line is dropped from the prose: the page
    shows it once, as its banner.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return _strip_brief_notice(data) if isinstance(data, dict) else None


def _assessment_is_stale(generated_at: datetime | None, now: datetime | None = None) -> bool:
    """Whether an assessment generated at *generated_at* is past the weekly window."""
    if generated_at is None:
        return False
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - generated_at > timedelta(days=STAGE_ASSESSMENT_FRESH_DAYS)


def _briefing_is_stale(generated_at: datetime | None, now: datetime | None = None) -> bool:
    """Whether a briefing generated at *generated_at* is past ``BRIEFING_FRESH_HOURS``."""
    if generated_at is None:
        return False
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - generated_at > timedelta(hours=BRIEFING_FRESH_HOURS)


async def _briefing_agent(db: AsyncSession) -> Agent | None:
    return (
        await db.execute(select(Agent).where(Agent.slug == _BRIEFING_AGENT_SLUG))
    ).scalar_one_or_none()


# ── Vertical-aware CTEM analyst overrides (platform default + per-user) ───────
#
# Two tiers, mirroring the General Assistant model resolution:
#   1. per-user override  (User.ctem_stage_agent_ids / .ctem_briefing_agent_ids)
#   2. platform default   (PlatformSettings.ctem_stage_agent_ids / .ctem_briefing_agent_ids)
#   3. seeded analyst     (ctem-{stage}-analyst / exposure-briefing-analyst)
# Every map is vertical-aware so a FIMI deployment and a Cyber one can each drive
# a stage (or the briefing) with their own agent.

# Reserved override key for the Exposure Briefing Analyst. It is not a CTEM stage
# but is configured alongside the per-stage analysts in the same overrides map.
CTEM_BRIEFING_KEY = "briefing"
_BRIEFING_LABEL = "Exposure Briefing Analyst"


def _vertical_stage_override_id(stage_map: Any, vertical: str, stage: str) -> str | None:
    """Override agent id for (*vertical*, *stage*) from a ctem_stage_agent_ids map.

    New shape ``{vertical: {stage: id}}`` wins; the legacy flat ``{stage: id}``
    shape (which historically applied to every vertical) is read as a fallback
    only when there is no per-vertical bucket.
    """
    if not isinstance(stage_map, dict):
        return None
    bucket = stage_map.get(vertical)
    if isinstance(bucket, dict):
        val = bucket.get(stage)
        return val.strip() if isinstance(val, str) and val.strip() else None
    val = stage_map.get(stage)
    return val.strip() if isinstance(val, str) and val.strip() else None


def _vertical_briefing_override_id(brief_map: Any, vertical: str) -> str | None:
    """Override agent id for the briefing analyst in *vertical* from a
    ctem_briefing_agent_ids ``{vertical: id}`` map."""
    if not isinstance(brief_map, dict):
        return None
    val = brief_map.get(vertical)
    return val.strip() if isinstance(val, str) and val.strip() else None


def _migrate_flat_stage_map(stage_map: Any) -> dict:
    """Return a pure nested ``{vertical: {stage: id}}`` stage-override map.

    A legacy flat ``{stage: id}`` map (which applied to every vertical) is expanded
    into each vertical so that, going forward, clearing one (vertical, stage)
    actually resets it instead of silently falling back to the legacy entry. An
    already-nested map is returned (copied) unchanged.
    """
    if not isinstance(stage_map, dict) or not stage_map:
        return {}
    flat = {
        k: v.strip()
        for k, v in stage_map.items()
        if k in STAGE_KEYS and isinstance(v, str) and v.strip()
    }
    nested = {
        k: {s: i.strip() for s, i in v.items() if isinstance(i, str) and i.strip()}
        for k, v in stage_map.items()
        if k in CTEM_VERTICALS and isinstance(v, dict)
    }
    if not flat:
        return nested
    for vert in CTEM_VERTICALS:
        merged = dict(flat)
        merged.update(nested.get(vert) or {})
        nested[vert] = merged
    return nested


async def _load_enabled_agent(db: AsyncSession, agent_id: Any) -> Agent | None:
    """Load an Agent by id, returning it only if it exists and is enabled."""
    if not agent_id or not is_valid_uuid(str(agent_id)):
        return None
    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    return agent if (agent and agent.enabled) else None


async def _seeded_stage_agent(db: AsyncSession, stage: str) -> Agent | None:
    slug = CTEM_STAGE_AGENT_SLUGS.get(stage)
    if not slug:
        return None
    return (
        await db.execute(select(Agent).where(Agent.slug == slug))
    ).scalar_one_or_none()


async def resolve_stage_agent(
    db: AsyncSession,
    stage: str,
    vertical: str | None = None,
    user: User | None = None,
) -> Agent | None:
    """Resolve the agent that assesses *stage*, vertical-aware.

    Priority: per-user override -> platform default override -> seeded
    ``ctem-{stage}-analyst``. A disabled/missing override is skipped (falls
    through to the next tier).
    """
    vertical = normalize_vertical(vertical)
    if user is not None:
        agent = await _load_enabled_agent(
            db,
            _vertical_stage_override_id(
                getattr(user, "ctem_stage_agent_ids", None), vertical, stage
            ),
        )
        if agent:
            return agent
    try:
        ps = await get_platform_settings(db)
        platform_id = _vertical_stage_override_id(
            getattr(ps, "ctem_stage_agent_ids", None), vertical, stage
        )
    except Exception:
        platform_id = None
    agent = await _load_enabled_agent(db, platform_id)
    if agent:
        return agent
    return await _seeded_stage_agent(db, stage)


async def resolve_briefing_agent(
    db: AsyncSession, vertical: str | None = None, user: User | None = None
) -> Agent | None:
    """Resolve the Exposure Briefing Analyst, vertical-aware.

    Priority: per-user override -> platform default override -> seeded
    ``exposure-briefing-analyst``.
    """
    vertical = normalize_vertical(vertical)
    if user is not None:
        agent = await _load_enabled_agent(
            db,
            _vertical_briefing_override_id(
                getattr(user, "ctem_briefing_agent_ids", None), vertical
            ),
        )
        if agent:
            return agent
    try:
        ps = await get_platform_settings(db)
        platform_id = _vertical_briefing_override_id(
            getattr(ps, "ctem_briefing_agent_ids", None), vertical
        )
    except Exception:
        platform_id = None
    agent = await _load_enabled_agent(db, platform_id)
    if agent:
        return agent
    return await _briefing_agent(db)


def _apply_overrides(
    stage_map: Any, brief_map: Any, vertical: str, overrides: dict[str, str | None]
) -> tuple[dict | None, dict | None]:
    """Apply an ``{stage | "briefing": id | null}`` patch for *vertical* onto the
    (stage, briefing) override maps. Returns the new ``(stage_map, brief_map)``
    (each ``None`` when empty). Legacy flat stage maps are migrated to nested
    first so a reset is never silently shadowed by a legacy entry."""
    stage_map = _migrate_flat_stage_map(stage_map)
    # Guard the briefing map the same way ``_migrate_flat_stage_map`` guards the
    # stage map: a malformed / non-dict stored value must not raise here.
    brief_map = dict(brief_map) if isinstance(brief_map, dict) else {}
    bucket = dict(stage_map.get(vertical) or {})
    for key, agent_id in overrides.items():
        norm = (
            agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else None
        )
        if key == CTEM_BRIEFING_KEY:
            if norm:
                brief_map[vertical] = norm
            else:
                brief_map.pop(vertical, None)
        elif key in STAGE_KEYS:
            if norm:
                bucket[key] = norm
            else:
                bucket.pop(key, None)
        # unknown keys are ignored
    if bucket:
        stage_map[vertical] = bucket
    else:
        stage_map.pop(vertical, None)
    return (stage_map or None), (brief_map or None)


def _requested_override_ids(overrides: dict[str, str | None]) -> set[str]:
    """Override agent ids for the KNOWN keys only (the CTEM stages + the reserved
    ``briefing`` key).

    Mirrors ``_apply_overrides``, which silently ignores unknown keys: validation
    must use the same key set so a client sending an extra/unknown key with a
    value never gets a 400 for an id the apply step would have dropped anyway.
    """
    valid_keys = {*STAGE_KEYS, CTEM_BRIEFING_KEY}
    return {
        agent_id.strip()
        for key, agent_id in overrides.items()
        if key in valid_keys and isinstance(agent_id, str) and agent_id.strip()
    }


async def _build_agent_row(
    db: AsyncSession,
    key: str,
    label: str,
    default_agent: Agent | None,
    override_id: str | None,
    default_slug: str | None,
) -> dict:
    """One row for the stage-agents config UI: the baseline default, the stored
    override and the effective agent.

    The override id is reported only when its agent STILL EXISTS (a disabled
    agent is kept so the user can re-enable / change it); a stale id whose agent
    was deleted is dropped so the control reverts to "Default" - matching the
    delete-time cleanup so a deleted analyst never leaves a dangling selection.
    """
    override_agent = None
    override_exists = False
    if override_id and is_valid_uuid(str(override_id)):
        existing = (
            await db.execute(select(Agent).where(Agent.id == override_id))
        ).scalar_one_or_none()
        if existing is not None:
            override_exists = True
            if existing.enabled:
                override_agent = existing
    effective = override_agent or default_agent
    return {
        "stage": key,
        "label": label,
        "default_agent_id": str(default_agent.id) if default_agent else None,
        "default_agent_slug": default_agent.slug if default_agent else default_slug,
        "override_agent_id": override_id if override_exists else None,
        "effective_agent_id": str(effective.id) if effective else None,
        "effective_agent_name": effective.name if effective else None,
    }


async def _stage_agents_response(
    db: AsyncSession, vertical: str, *, user: User | None
) -> dict:
    """Build the ``{vertical, stages, briefing}`` config payload.

    Platform scope (``user is None``): default = seeded analyst, override =
    platform default. User scope: default = the EFFECTIVE platform/seeded agent
    (what the user gets without overriding), override = the user's own pick.
    """
    ps = await get_platform_settings(db)
    platform_stage_map = getattr(ps, "ctem_stage_agent_ids", None)
    platform_brief_map = getattr(ps, "ctem_briefing_agent_ids", None)
    user_scope = user is not None
    user_stage_map = getattr(user, "ctem_stage_agent_ids", None) if user_scope else None
    user_brief_map = (
        getattr(user, "ctem_briefing_agent_ids", None) if user_scope else None
    )

    stages: list[dict] = []
    for stage in STAGE_KEYS:
        seeded = await _seeded_stage_agent(db, stage)
        platform_id = _vertical_stage_override_id(platform_stage_map, vertical, stage)
        if user_scope:
            default_agent = await _load_enabled_agent(db, platform_id) or seeded
            override_id = _vertical_stage_override_id(user_stage_map, vertical, stage)
        else:
            default_agent = seeded
            override_id = platform_id
        stages.append(
            await _build_agent_row(
                db,
                stage,
                stage_label(stage, vertical),
                default_agent,
                override_id,
                CTEM_STAGE_AGENT_SLUGS.get(stage),
            )
        )

    seeded_brief = await _briefing_agent(db)
    platform_brief_id = _vertical_briefing_override_id(platform_brief_map, vertical)
    if user_scope:
        default_brief = await _load_enabled_agent(db, platform_brief_id) or seeded_brief
        brief_override_id = _vertical_briefing_override_id(user_brief_map, vertical)
    else:
        default_brief = seeded_brief
        brief_override_id = platform_brief_id
    briefing = await _build_agent_row(
        db,
        CTEM_BRIEFING_KEY,
        _BRIEFING_LABEL,
        default_brief,
        brief_override_id,
        _BRIEFING_AGENT_SLUG,
    )

    return {"vertical": vertical, "stages": stages, "briefing": briefing}


async def _user_has_active_briefing_run(
    db: AsyncSession, agent_id, user_id, vertical: str | None = None
) -> bool:
    conditions = [
        AssignmentRun.agent_id == agent_id,
        AssignmentRun.user_id == user_id,
        AssignmentRun.status.in_(_ACTIVE_RUN_STATUSES),
    ]
    if vertical is not None:
        # Scope to the vertical so an active run for a different vertical does not
        # show a spurious "generating" state on this one (runs are tagged with
        # their vertical in the trigger context).
        conditions.append(
            AssignmentRun.trigger_context["ctem_vertical"].astext == vertical
        )
    res = await db.execute(select(AssignmentRun.id).where(*conditions).limit(1))
    return res.scalar_one_or_none() is not None


async def _latest_artifact_is_stale(
    db: AsyncSession, agent_id, user_id, vertical: str, is_stale: Callable[[datetime | None], bool]
) -> bool:
    """Whether the user's latest stored artifact of *agent_id* for *vertical* is
    past its freshness window (*is_stale*): the server-side check behind the
    quota exemption of an automatic regeneration (#12183)."""
    run, artifact = await _latest_vertical_artifact(db, agent_id, user_id, vertical)
    return bool(artifact) and is_stale(run.created_at if run else None)


async def _is_first_vertical_run(
    db: AsyncSession, agent_id, user_id, vertical: str
) -> bool:
    """True when the user has NO prior COMPLETED artifact for this agent + *vertical*.

    The FIRST auto-population of a seeded CTEM artifact per (user, agent,
    vertical) is a platform-initiated convenience (the dashboard generates it so
    the card isn't empty), so it is flagged ``quota_exempt`` in the trigger
    context — honoured by the engine ONLY for ``is_builtin`` agents. Every later
    regeneration is a deliberate user action and stays metered.

    "First" is gated on a prior *completed, artifact-producing* run (mirroring
    ``_latest_vertical_artifact``), NOT merely any prior run: a failed (or
    completed-but-empty) attempt leaves the card on ``pending``, which the
    dashboard auto-retries - those platform retries must stay exempt instead of
    silently re-metering the quota burn this exemption exists to prevent. The
    scheduled per-user fan-out also produces a completed artifact, so once it has
    populated a vertical a manual regenerate already counts.
    """
    res = await db.execute(
        select(AssignmentRun.id)
        .where(
            AssignmentRun.agent_id == agent_id,
            AssignmentRun.user_id == user_id,
            AssignmentRun.trigger_context["ctem_vertical"].astext == vertical,
            AssignmentRun.status == "completed",
            AssignmentRun.result_summary.isnot(None),
        )
        .limit(1)
    )
    return res.scalar_one_or_none() is None


# How many recent runs to scan when resolving the per-vertical latest artifact.
# Generous enough to find each vertical's last run even after many runs on others.
_VERTICAL_RUN_SCAN = 40


def _run_unreadable(run: Any) -> bool:
    """Whether *run* completed with none of its called integrations readable (#1589)."""
    from app.services.integration_auth_guard import UNREADABLE_STOP_REASON

    return getattr(run, "stop_reason", None) == UNREADABLE_STOP_REASON


async def _latest_run_not_assessable(
    db: AsyncSession, agent_id: Any, user_id: Any, vertical: str
) -> list[dict[str, Any]] | None:
    """The integration auth failures of the user's newest completed run of
    *agent_id* for *vertical* when that run could read none of its
    integrations, else ``None``.

    The card then says "not assessable" (and why) instead of ``pending``, which
    would start another blind run on landing (#1589).
    """
    run = (
        await db.execute(
            select(AssignmentRun)
            .where(
                AssignmentRun.agent_id == agent_id,
                AssignmentRun.user_id == user_id,
                AssignmentRun.trigger_context["ctem_vertical"].astext == vertical,
                AssignmentRun.status == "completed",
            )
            .order_by(AssignmentRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None or not _run_unreadable(run):
        return None
    return list(run.integration_auth_failures or [])


async def _latest_run_no_module_account(
    db: AsyncSession, agent_id: Any, user_id: Any, vertical: str
) -> list[str] | None:
    """The modules the member has no account on when the user's newest run of
    *agent_id* for *vertical* was skipped by the module preflight, else ``None``.

    The card then tells the member to sign in to those modules once instead of
    answering ``pending``, which would start another skipped run on every
    landing (#2042).
    """
    from app.services.ctem_module_preflight import NO_MODULE_ACCOUNT

    run = (
        await db.execute(
            select(AssignmentRun)
            .where(
                AssignmentRun.agent_id == agent_id,
                AssignmentRun.user_id == user_id,
                AssignmentRun.trigger_context["ctem_vertical"].astext == vertical,
                AssignmentRun.status.in_(("completed", "skipped")),
            )
            .order_by(AssignmentRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _no_module_account_modules(run, NO_MODULE_ACCOUNT)


def _no_module_account_modules(run: Any, reason: str) -> list[str] | None:
    """The modules a run skipped for *reason* found without an account (#2042)."""
    if run is None or getattr(run, "status", None) != "skipped" or getattr(run, "error", None) != reason:
        return None
    states = (getattr(run, "trigger_context", None) or {}).get("module_accounts") or {}
    return sorted(m for m, state in states.items() if state == "missing")


def _artifact_status(
    *,
    generating: bool,
    artifact: Any,
    not_assessable: list[dict[str, Any]] | None,
    no_module_account: list[str] | None = None,
) -> str | None:
    """The card status before the AI-lock check: ``None`` means nothing to show.

    ``generating`` outranks a cached artifact; a newest run that read nothing
    answers ``not_assessable`` when no earlier artifact can be shown (#1589); a
    newest run skipped because the member has no module account answers
    ``no_module_account`` (#2042).
    """
    if generating:
        return "generating"
    if artifact:
        return "ready"
    if not_assessable is not None:
        return "not_assessable"
    if no_module_account is not None:
        return "no_module_account"
    return None


def _artifact_stale(
    *,
    artifact: Any,
    generated_at: datetime | None,
    not_assessable: list[dict[str, Any]] | None,
    is_stale: Callable[[datetime | None], bool],
    no_module_account: list[str] | None = None,
) -> bool:
    """Whether the card marks its artifact stale, which makes the page
    regenerate it on landing.

    Never while the newest run read none of its integrations: a regeneration
    would read nothing again and leave the older artifact in place, so every
    landing would start another blind run (#1589). Never while the newest run
    was skipped for a missing module account either (#2042).
    """
    if not artifact or not_assessable is not None or no_module_account is not None:
        return False
    return is_stale(generated_at)


async def _latest_vertical_artifact(
    db: AsyncSession, agent_id, user_id, vertical: str
) -> tuple[Any | None, dict | None]:
    """The user's most recent completed run for *agent_id* whose AUTHORED vertical
    matches *vertical*, returned as ``(run, parsed)``.

    Multi-vertical platforms switch verticals constantly, so we must NOT discard a
    vertical's artifact just because a different vertical was generated more
    recently. Instead of taking the single latest run, scan the recent runs (newest
    first) and return the latest one tagged for this vertical - so each vertical
    keeps its own last-generated briefing / assessment and switching back is instant
    (no forced regeneration). A legacy run with no ``vertical`` tag is used only as a
    fallback when nothing matches.
    """
    rows = (
        await db.execute(
            select(AssignmentRun)
            .where(
                AssignmentRun.agent_id == agent_id,
                AssignmentRun.user_id == user_id,
                AssignmentRun.status == "completed",
                AssignmentRun.result_summary.isnot(None),
            )
            .order_by(AssignmentRun.created_at.desc())
            .limit(_VERTICAL_RUN_SCAN)
        )
    ).scalars().all()
    legacy: tuple[Any, dict] | None = None
    for r in rows:
        # A run that could read none of its integrations is never an
        # artifact: its headline and score were written blind (#1589).
        if _run_unreadable(r):
            continue
        parsed = _parse_briefing(r.result_summary)
        if not parsed:
            continue
        rv = parsed.get("vertical")
        if rv:
            if normalize_vertical(rv) == vertical:
                return r, parsed
        elif legacy is None:
            legacy = (r, parsed)
    return legacy if legacy else (None, None)


async def _ai_lock_status(db: AsyncSession, agent: Agent) -> str | None:
    """Why no AI artifact can be generated now, once the licence is known.

    ``ai_disabled`` when AI is switched off, ``no_model`` when neither the
    analyst nor the platform names a model (the ``ModelConfigWarnings``
    predicate: ``agent_default_model`` missing), ``None`` otherwise. The
    briefing and assessment GETs answer it instead of ``pending``, because the
    UI auto-generates on ``pending`` and the refresh would be refused (AI off)
    or start a run that cannot reach a model (#9207, findings B1 and B2).
    Two cheap reads, no model call.
    """
    from app.api.v1.platform_settings_helpers import peek_platform_settings
    from app.services.ai_state import AI_DISABLED_CODE, is_ai_enabled

    if not await is_ai_enabled(db):
        return AI_DISABLED_CODE
    if getattr(agent, "llm_model", None):
        return None
    ps = await peek_platform_settings(db)
    if ps is None or not getattr(ps, "agent_default_model", None):
        return "no_model"
    return None


@router.get("/briefing")
async def ctem_briefing(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The current user's AI exposure briefing - per user + per vertical, EE-gated.

    Generated by the seeded ``exposure-briefing-analyst`` agent: monthly for all
    users (fan-out) and on demand via ``/briefing/refresh``. Each run executes as
    the user it belongs to, so the data is scoped to what THAT user can see in
    OpenCTI/OpenAEV, and is framed for the user's active vertical. Returns only the
    current user's latest run, and only when it matches the active vertical (a run
    authored for another vertical is treated as pending). EE-gated: needs at least
    one attached EE product (``ee_locked`` otherwise). The widget shows a sample
    preview while ``pending``/``generating`` or when ``ee_locked``.
    """
    _require_xtm_one()
    vertical = _resolve_vertical(user, vertical)

    agent = await resolve_briefing_agent(db, vertical, user)
    if not agent:
        return {"briefing": None, "status": "unavailable"}

    from app.api.v1.platform import ee_features_enabled

    if not await ee_features_enabled(db):
        return {"briefing": None, "status": "ee_locked", "agent_name": agent.name}

    # The latest run AUTHORED for this vertical (each vertical keeps its own cached
    # briefing - switching back never discards it).
    run, briefing = await _latest_vertical_artifact(db, agent.id, user.id, vertical)
    generating = await _user_has_active_briefing_run(db, agent.id, user.id, vertical)
    not_assessable = await _latest_run_not_assessable(db, agent.id, user.id, vertical)

    # ``generating`` takes precedence over a cached result: when a fresh run is in
    # flight we report "generating" (so the UI spins and polls) even though the
    # previous briefing is still cached and returned alongside it.
    # Nothing to show: the UI auto-generates on ``pending``, so answer why
    # it cannot instead (AI off, no model). A cached briefing stays readable.
    status = (
        _artifact_status(generating=generating, artifact=briefing, not_assessable=not_assessable)
        or await _ai_lock_status(db, agent)
        or "pending"
    )

    generated_at = run.created_at if run else None
    return {
        "briefing": briefing,
        "status": status,
        "generating": generating,
        # The newest run could read none of its integrations: the failures,
        # so the card says why (#1589). ``None`` otherwise.
        "not_assessable": not_assessable,
        # Past the daily window: the command center regenerates it on landing
        # and marks it stale meanwhile (#12183).
        # Never while the newest run was unreadable (#1589).
        "stale": _artifact_stale(
            artifact=briefing, generated_at=generated_at, not_assessable=not_assessable, is_stale=_briefing_is_stale
        ),
        "fresh_hours": BRIEFING_FRESH_HOURS,
        "agent_name": agent.name,
        "agent_slug": agent.slug,
        # The effective agent (per-user override -> platform default -> seeded), so
        # the UI can offer a "follow up / implement" action routed to a custom
        # agent (or prompt to assign one when only the seeded analyst is in place).
        "agent_id": str(agent.id),
        "agent_is_builtin": bool(agent.is_builtin),
        "generated_at": generated_at.isoformat() if generated_at else None,
    }


@router.post("/briefing/refresh")
async def refresh_my_briefing(
    vertical: str | None = Query(default=None),
    auto: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Generate the current user's exposure briefing on demand (as that user).

    Triggers the seeded manual assignment with the caller's identity so the run
    is scoped to their OpenCTI/OpenAEV access and framed for their active vertical.
    Deduped per user (no-op if one is already in flight). EE-gated.
    """
    _require_xtm_one()
    vertical = _resolve_vertical(user, vertical)

    from app.api.v1.platform import ee_features_enabled

    if not await ee_features_enabled(db):
        raise HTTPException(
            status_code=403,
            detail="The exposure briefing requires at least one Enterprise product.",
        )

    agent = await resolve_briefing_agent(db, vertical, user)
    if not agent:
        raise HTTPException(status_code=404, detail="Briefing agent not available")

    assignment = (
        await db.execute(
            select(Assignment).where(
                Assignment.agent_id == agent.id,
                Assignment.name == _BRIEFING_MANUAL_ASSIGNMENT,
            )
        )
    ).scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Briefing assignment not available")

    if await _user_has_active_briefing_run(db, agent.id, user.id, vertical):
        return {"status": "generating"}

    from app.services.assignment_engine import trigger_assignment

    # The FIRST auto-population of this vertical's briefing is platform-initiated
    # (the dashboard generates it so the card isn't empty), so it is exempt from
    # the agentic quota; deliberate regenerations stay metered. The engine only
    # honours ``quota_exempt`` for is_builtin agents (this briefing analyst is one).
    trigger_context: dict = {"type": "briefing_refresh", "ctem_vertical": vertical}
    if await _is_first_vertical_run(db, agent.id, user.id, vertical):
        trigger_context["quota_exempt"] = True
    elif auto and await _latest_artifact_is_stale(db, agent.id, user.id, vertical, _briefing_is_stale):
        # The landing regeneration of a stale briefing is platform-initiated too
        # (#12183): exempt, but only when the server sees the stored one stale,
        # so ``auto`` cannot turn a deliberate regeneration into a free one.
        trigger_context["quota_exempt"] = True

    from app.services.assignment_pause import AssignmentOwnerDeactivatedError

    try:
        await trigger_assignment(
            assignment=assignment,
            agent=agent,
            db=db,
            triggered_by_user_id=user.id,
            trigger_context=trigger_context,
        )
    except AssignmentOwnerDeactivatedError:
        # A per-user override agent owned by a deactivated account.
        raise
    except Exception as exc:
        logger.warning("Briefing refresh trigger failed for user %s: %s", user.id, format_exception_message(exc))
        raise HTTPException(
            status_code=502, detail="Could not start briefing generation"
        ) from exc

    return {"status": "generating"}


# ── Per-stage AI assessments (seeded stage analysts, PER USER + EE-gated) ────


@router.get("/stages/{stage}/assessment")
async def ctem_stage_assessment(
    stage: str,
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The current user's AI assessment for one CTEM stage (per user, EE-gated).

    Generated by the seeded ``ctem-{stage}-analyst`` (weekly fan-out + on-demand).
    Returns only the current user's latest run, and only when it matches the
    active vertical (a run authored for another vertical is treated as pending).
    """
    _require_xtm_one()
    if stage not in STAGE_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown stage: {stage}")
    vertical = _resolve_vertical(user, vertical)

    agent = await resolve_stage_agent(db, stage, vertical, user)
    if not agent:
        return {"assessment": None, "status": "unavailable"}

    from app.api.v1.platform import ee_features_enabled

    if not await ee_features_enabled(db):
        return {"assessment": None, "status": "ee_locked", "agent_name": agent.name}

    # The latest run AUTHORED for this vertical (each vertical keeps its own cached
    # assessment - switching back never discards it).
    run, assessment = await _latest_vertical_artifact(db, agent.id, user.id, vertical)
    generating = await _user_has_active_briefing_run(db, agent.id, user.id, vertical)
    not_assessable = await _latest_run_not_assessable(db, agent.id, user.id, vertical)
    no_module_account = await _latest_run_no_module_account(db, agent.id, user.id, vertical)

    # ``generating`` takes precedence over a cached result: when a fresh run is in
    # flight we report "generating" (so the UI spins and polls) even though the
    # previous assessment is still cached and returned alongside it.
    status = (
        _artifact_status(
            generating=generating,
            artifact=assessment,
            not_assessable=not_assessable,
            no_module_account=no_module_account,
        )
        or await _ai_lock_status(db, agent)
        or "pending"
    )

    generated_at = run.created_at if run else None
    return {
        "assessment": assessment,
        "status": status,
        "generating": generating,
        # The newest run could read none of its integrations: the failures,
        # so the card says why (#1589). ``None`` otherwise.
        "not_assessable": not_assessable,
        # The newest run was skipped before any model call: the modules the
        # member must sign in to once (#2042). ``None`` otherwise.
        "no_module_account": no_module_account,
        # Past the weekly window: the stage page regenerates it on visit and
        # marks it stale meanwhile (#12002).
        # Never while the newest run was unreadable (#1589) or skipped (#2042).
        "stale": _artifact_stale(
            artifact=assessment,
            generated_at=generated_at,
            not_assessable=not_assessable,
            is_stale=_assessment_is_stale,
            no_module_account=no_module_account,
        ),
        "fresh_days": STAGE_ASSESSMENT_FRESH_DAYS,
        "agent_name": agent.name,
        "agent_slug": agent.slug,
        # The effective agent (per-user override -> platform default -> seeded), so
        # the UI can offer a "follow up / implement" action routed to a custom
        # agent (or prompt to assign one when only the seeded analyst is in place).
        "agent_id": str(agent.id),
        "agent_is_builtin": bool(agent.is_builtin),
        "generated_at": generated_at.isoformat() if generated_at else None,
    }


@router.post("/stages/{stage}/assessment/refresh")
async def refresh_stage_assessment(
    stage: str,
    vertical: str | None = Query(default=None),
    auto: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Generate the current user's assessment for one stage on demand (as them)."""
    _require_xtm_one()
    if stage not in STAGE_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown stage: {stage}")
    vertical = _resolve_vertical(user, vertical)

    from app.api.v1.platform import ee_features_enabled

    if not await ee_features_enabled(db):
        raise HTTPException(
            status_code=403,
            detail="Per-stage assessments require at least one Enterprise product.",
        )

    agent = await resolve_stage_agent(db, stage, vertical, user)
    if not agent:
        raise HTTPException(status_code=404, detail="Stage analyst not available")

    assignment = (
        await db.execute(
            select(Assignment).where(
                Assignment.agent_id == agent.id,
                Assignment.name == f"Generate my {stage} assessment",
            )
        )
    ).scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Stage assignment not available")

    if await _user_has_active_briefing_run(db, agent.id, user.id, vertical):
        return {"status": "generating"}

    from app.services.assignment_engine import trigger_assignment

    # First auto-population of this stage assessment is platform-initiated, so it
    # is exempt from the agentic quota; regenerations stay metered. Honoured only
    # for is_builtin agents in the engine (this stage analyst is one).
    trigger_context: dict = {"type": "stage_refresh", "ctem_vertical": vertical}
    if await _is_first_vertical_run(db, agent.id, user.id, vertical):
        trigger_context["quota_exempt"] = True
    elif auto and await _latest_artifact_is_stale(db, agent.id, user.id, vertical, _assessment_is_stale):
        # The on-visit regeneration of a stale assessment is platform-initiated
        # (#12183): exempt only when the server sees the stored one stale.
        trigger_context["quota_exempt"] = True

    from app.services.assignment_pause import AssignmentOwnerDeactivatedError

    try:
        await trigger_assignment(
            assignment=assignment,
            agent=agent,
            db=db,
            triggered_by_user_id=user.id,
            trigger_context=trigger_context,
        )
    except AssignmentOwnerDeactivatedError:
        # A per-user override agent owned by a deactivated account.
        raise
    except Exception as exc:
        logger.warning("Stage assessment refresh failed for user %s: %s", user.id, format_exception_message(exc))
        raise HTTPException(
            status_code=502, detail="Could not start assessment generation"
        ) from exc

    return {"status": "generating"}


# ── CTEM analyst overrides (vertical-aware: platform default + per-user) ──────


class StageAgentOverrides(BaseModel):
    # The vertical these overrides apply to (cyber/fimi/fraud). Defaults to the
    # caller's active vertical when omitted.
    vertical: str | None = None
    # {stage_key | "briefing": agent_id | null}. null/empty resets that slot to
    # the inherited default. Unknown keys are ignored.
    overrides: dict[str, str | None]


async def _validate_override_agents(
    db: AsyncSession,
    overrides: dict[str, str | None],
    *,
    visible_filter: Any = None,
) -> None:
    """Reject malformed / unknown (or, when *visible_filter* is set, not-visible)
    override agent ids before they are persisted.

    Override ids flow into ``Agent.id == / Agent.id.in_(...)`` lookups on
    ``/posture``, ``/stages/...`` and the config endpoints for every request, so a
    malformed (non-UUID) or dangling id would 500 those shared endpoints. Filter to
    valid UUIDs first so the existence query can't raise on a bad cast, then reject
    unknown ids.
    """
    requested = _requested_override_ids(overrides)
    if not requested:
        return
    candidate_ids = {v for v in requested if is_valid_uuid(v)}
    existing_ids: set[str] = set()
    if candidate_ids:
        query = select(Agent.id).where(Agent.id.in_(candidate_ids))
        if visible_filter is not None:
            query = query.where(visible_filter)
        existing_ids = {str(row[0]) for row in (await db.execute(query)).all()}
    unknown = sorted(requested - existing_ids)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown or inaccessible agent id(s): {', '.join(unknown)}",
        )


async def _require_ctem_agentic_ai(db: AsyncSession) -> None:
    """Gate CTEM analyst override writes on Enterprise Edition.

    Forking / assigning a custom analyst is an agentic feature (the UI gates it
    behind ``useAgenticAIEnabled`` + the license popup), so the write path must
    check the licence server-side too, or a Community Edition deployment could
    still persist overrides via the API.
    """
    from app.api.deps import licence_required_detail
    from app.api.v1.platform import is_agentic_ai_licensed
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled

    # Two different refusals. AI switched off gets the contract payload so a
    # client branches on ``detail.code``; Community Edition keeps the licence
    # message. Collapsing them told an administrator to buy a licence they
    # already had (FR-009).
    if not await is_ai_enabled(db):
        refuse_ai_disabled()
    if not await is_agentic_ai_licensed(db):
        raise HTTPException(status_code=403, detail=licence_required_detail())


@router.get("/stage-agents")
async def list_stage_agents(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Platform (admin) CTEM analyst defaults for *vertical*: per stage + the
    Exposure Briefing Analyst, each with the seeded default, the active override
    and the effective agent. Admins fork/assign a custom analyst per vertical."""
    _require_xtm_one()
    vertical = normalize_vertical(vertical)
    return await _stage_agents_response(db, vertical, user=None)


@router.put("/stage-agents")
async def set_stage_agents(
    body: StageAgentOverrides,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Set the platform CTEM analyst defaults for a vertical (Agentic AI, admin).
    Pass null for a stage / "briefing" to reset it to the seeded default."""
    _require_xtm_one()
    await _require_ctem_agentic_ai(db)
    vertical = normalize_vertical(body.vertical)
    await _validate_override_agents(db, body.overrides)

    ps = await get_platform_settings(db)
    stage_map, brief_map = _apply_overrides(
        getattr(ps, "ctem_stage_agent_ids", None),
        getattr(ps, "ctem_briefing_agent_ids", None),
        vertical,
        body.overrides,
    )
    ps.ctem_stage_agent_ids = stage_map
    ps.ctem_briefing_agent_ids = brief_map
    await db.commit()
    return await _stage_agents_response(db, vertical, user=None)


@router.get("/my-stage-agents")
async def list_my_stage_agents(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The current user's CTEM analyst overrides for *vertical*. ``default_*`` is
    the EFFECTIVE platform/seeded agent (what the user gets without overriding);
    ``override_*`` is the user's own pick."""
    _require_xtm_one()
    vertical = _resolve_vertical(user, vertical)
    return await _stage_agents_response(db, vertical, user=user)


@router.put("/my-stage-agents")
async def set_my_stage_agents(
    body: StageAgentOverrides,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Set the current user's personal CTEM analyst overrides for a vertical
    (Agentic AI). Pass null to fall back to the platform default. The chosen agent
    must be one the user can access (own / group-shared / company-managed)."""
    _require_xtm_one()
    await _require_ctem_agentic_ai(db)
    vertical = _resolve_vertical(user, body.vertical)

    group_ids = await get_user_group_ids(db, user.id)
    await _validate_override_agents(
        db,
        body.overrides,
        visible_filter=user_visible_filter(Agent, user.id, group_ids),
    )

    stage_map, brief_map = _apply_overrides(
        getattr(user, "ctem_stage_agent_ids", None),
        getattr(user, "ctem_briefing_agent_ids", None),
        vertical,
        body.overrides,
    )
    user.ctem_stage_agent_ids = stage_map
    user.ctem_briefing_agent_ids = brief_map
    await db.commit()
    return await _stage_agents_response(db, vertical, user=user)
