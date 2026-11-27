# SPDX-License-Identifier: Apache-2.0

"""Prometheus metrics endpoint."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import REGISTRY, Gauge, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import _PROMETHEUS_TOKEN_MIN_LENGTH, settings
from app.core.database import get_db
from app.core.redis import redis_client
from app.core.security import timing_safe_header_eq, timing_safe_str_eq
from app.core.utils import format_exception_message

router = APIRouter(tags=["Metrics"])

# Define the gauge. The registry keeps track of it.
QUOTA_GAUGE = Gauge(
    "xtmone_quota_usage",
    "Total quota usage for a user/model/period",
    ["user_id", "usage_type", "model", "period", "platform_id"]
)

# No assignment name: it is free text a private assignment's owner chose.
_ASSIGNMENT_LABELS = ["assignment_id", "platform_id"]
ASSIGNMENT_QUEUE_DEPTH_GAUGE = Gauge(
    "xtmone_assignment_queue_depth",
    "Deliveries queued for an assignment, waiting for a free slot",
    _ASSIGNMENT_LABELS,
)
ASSIGNMENT_QUEUE_OLDEST_AGE_GAUGE = Gauge(
    "xtmone_assignment_queue_oldest_age_seconds",
    "Age of an assignment's oldest queued delivery",
    _ASSIGNMENT_LABELS,
)
ASSIGNMENT_RUNS_ACTIVE_GAUGE = Gauge(
    "xtmone_assignment_runs_active",
    "Runs of an assignment holding a concurrency slot",
    _ASSIGNMENT_LABELS,
)
ASSIGNMENT_CONCURRENCY_CAP_GAUGE = Gauge(
    "xtmone_assignment_concurrency_cap",
    "How many runs of an assignment may hold a slot at once",
    _ASSIGNMENT_LABELS,
)
ASSIGNMENT_DISPATCH_LAG_GAUGE = Gauge(
    "xtmone_assignment_dispatch_lag_p95_seconds",
    "p95 of created-to-started over an assignment's runs of the last 15 minutes",
    _ASSIGNMENT_LABELS,
)
ASSIGNMENT_QUEUE_HELD_GAUGE = Gauge(
    "xtmone_assignment_queue_held",
    "1 while an assignment's queue waits for a human's answer to one of its runs",
    _ASSIGNMENT_LABELS,
)
ASSIGNMENT_QUEUE_OLDEST_AGE_MAX_GAUGE = Gauge(
    "xtmone_assignment_queue_oldest_age_max_seconds",
    "Age of the oldest queued delivery across every assignment not waiting for a human",
    ["platform_id"],
)
# Read from the database at scrape time, so every worker reports the same
# figures. Labelled by group id, never its name (free text).
RELAY_GROUP_RELAYS_GAUGE = Gauge(
    "xtmone_relay_group_relays",
    "Relays of a relay group: enrolled (active) and online",
    ["group_id", "state", "platform_id"],
)
RELAY_GROUP_ROUTED_GAUGE = Gauge(
    "xtmone_relay_group_routed_resources",
    "Integrations and MCP servers routed through a relay group",
    ["group_id", "platform_id"],
)
SANDBOX_WORKSPACES_GAUGE = Gauge(
    "xtmone_sandbox_workspaces",
    "Code interpreter sandbox workspaces: how many, their bytes and files, kernel restarts summed",
    ["measure", "platform_id"],
)
_PER_ASSIGNMENT_GAUGES = (
    ASSIGNMENT_QUEUE_DEPTH_GAUGE,
    ASSIGNMENT_QUEUE_OLDEST_AGE_GAUGE,
    ASSIGNMENT_RUNS_ACTIVE_GAUGE,
    ASSIGNMENT_CONCURRENCY_CAP_GAUGE,
    ASSIGNMENT_DISPATCH_LAG_GAUGE,
    ASSIGNMENT_QUEUE_HELD_GAUGE,
)

security = HTTPBearer(auto_error=False)

def verify_prometheus_access(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> None:
    """Check if Prometheus metrics are enabled and authenticate the request."""
    if not settings.prometheus_enabled:
        raise HTTPException(status_code=404, detail="Not Found")

    if settings.prometheus_bearer_token and len(settings.prometheus_bearer_token) < _PROMETHEUS_TOKEN_MIN_LENGTH:
        raise HTTPException(status_code=503, detail="Service Unavailable: Prometheus token is dangerously short")

    if settings.prometheus_bearer_token:
        token = None
        compare = timing_safe_str_eq

        # Only allow query parameter token in dev mode (avoids credential leak in production access logs)
        if settings.platform_mode == "dev":
            token = request.query_params.get("token")

        if not token:
            if not credentials or credentials.scheme != "Bearer":
                raise HTTPException(status_code=401, detail="Unauthorized")
            token = credentials.credentials
            # A header: either wire spelling of a non-ASCII token matches.
            compare = timing_safe_header_eq

        # Use constant-time comparison to prevent timing attacks
        if not compare(token, settings.prometheus_bearer_token):
            raise HTTPException(status_code=401, detail="Unauthorized")


def get_current_periods() -> dict[str, str]:
    """Return a mapping of current period keys to their semantic names."""
    now = datetime.now(UTC)
    return {
        now.strftime("%Y-%m-%d"): "daily",
        now.strftime("%Y-%m"): "monthly",
        now.strftime("%Y"): "yearly"
    }


async def _read_quota_total(
    db: AsyncSession,
    redis,
    qt: str,
    period_key: str,
    semantic: str,
    platform_id: str,
) -> None:
    import logging

    from sqlalchemy import func, select

    from app.models.quota import QuotaUsage
    logger = logging.getLogger(__name__)

    try:
        # A) DB sum per user for the prefix
        query = select(
            QuotaUsage.user_id,
            func.sum(QuotaUsage.count)
        ).where(
            QuotaUsage.usage_type == qt,
            QuotaUsage.model.is_(None),
            QuotaUsage.agent_id.is_(None),
            QuotaUsage.period.like(f"{period_key}%")
        ).group_by(QuotaUsage.user_id)

        db_res = await db.execute(query)
        user_totals = {str(row.user_id): row[1] or 0 for row in db_res.all()}

        # B) Redis sum per user for the prefix.  Both the 5-segment ":_all"
        # aggregate keys and the legacy 4-segment keys are scanned: counters
        # written before the agent-attribution deploy live under the legacy
        # shape until the first post-deploy increment folds them in, and the
        # two shapes never overlap, so summing across patterns cannot
        # double-count.
        redis_user_totals = {}
        scan_patterns = [
            f"quota:usage:*:{qt}:_total:{period_key}:_all",
            f"quota:usage:*:{qt}:_total:{period_key}",
        ]
        if semantic == "monthly":
            scan_patterns.append(f"quota:usage:*:{qt}:_total:{period_key}-??:_all")
            scan_patterns.append(f"quota:usage:*:{qt}:_total:{period_key}-??")
        elif semantic == "yearly":
            scan_patterns.append(f"quota:usage:*:{qt}:_total:{period_key}-??-??:_all")
            scan_patterns.append(f"quota:usage:*:{qt}:_total:{period_key}-??:_all")
            scan_patterns.append(f"quota:usage:*:{qt}:_total:{period_key}-??-??")
            scan_patterns.append(f"quota:usage:*:{qt}:_total:{period_key}-??")

        for pattern in scan_patterns:
            async for key in redis.scan_iter(match=pattern, count=500):
                key_str = key if isinstance(key, str) else key.decode()
                parts = key_str.removeprefix("quota:usage:").split(":")
                uid = parts[0] if parts else ""
                if uid == "_global":
                    continue
                val = await redis.get(key_str)
                if val:
                    redis_user_totals[uid] = redis_user_totals.get(uid, 0) + int(val)

        # C) Merge taking max(db, redis) per user, then sum
        all_uids = set(user_totals.keys()) | set(redis_user_totals.keys())
        merged_total = 0.0
        for uid in all_uids:
            user_val = max(user_totals.get(uid, 0), redis_user_totals.get(uid, 0))
            merged_total += user_val

            QUOTA_GAUGE.labels(
                user_id=uid,
                usage_type=qt,
                model="_total",
                period=semantic,
                platform_id=platform_id
            ).set(user_val)

        QUOTA_GAUGE.labels(
            user_id="_global",
            usage_type=qt,
            model="_total",
            period=semantic,
            platform_id=platform_id
        ).set(merged_total)
    except Exception as exc:
        logger.error("Failed to read quota totals for %s %s: %s", qt, semantic, format_exception_message(exc))
        import contextlib
        with contextlib.suppress(Exception):
            await db.rollback()


async def _read_assignment_dispatch(db: AsyncSession, platform_id: str) -> None:
    """Set the assignment dispatch gauges from the run table."""
    import contextlib
    import logging

    from app.services.assignment_dispatch import read_dispatch_stats

    try:
        stats = await read_dispatch_stats(db)
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Failed to read the assignment dispatch stats: %s", format_exception_message(exc)
        )
        with contextlib.suppress(Exception):
            await db.rollback()
        return
    # Only the assignments with something queued, running or recently started
    # are reported; an assignment that went quiet drops out instead of keeping
    # its last value.
    for gauge in _PER_ASSIGNMENT_GAUGES:
        gauge.clear()
    for assignment_id, values in stats["assignments"].items():
        labels = {"assignment_id": str(assignment_id), "platform_id": platform_id}
        ASSIGNMENT_QUEUE_DEPTH_GAUGE.labels(**labels).set(values["depth"])
        ASSIGNMENT_QUEUE_OLDEST_AGE_GAUGE.labels(**labels).set(values["oldest_age_seconds"])
        ASSIGNMENT_RUNS_ACTIVE_GAUGE.labels(**labels).set(values["active"])
        ASSIGNMENT_CONCURRENCY_CAP_GAUGE.labels(**labels).set(values["cap"])
        ASSIGNMENT_QUEUE_HELD_GAUGE.labels(**labels).set(1 if values["held"] else 0)
        if values["lag_p95_seconds"] is not None:
            ASSIGNMENT_DISPATCH_LAG_GAUGE.labels(**labels).set(values["lag_p95_seconds"])
    ASSIGNMENT_QUEUE_OLDEST_AGE_MAX_GAUGE.labels(platform_id=platform_id).set(stats["oldest_age_seconds"])


async def _read_relays(db: AsyncSession, platform_id: str) -> None:
    """Set the relay group gauges; nothing while relays are off."""
    import contextlib
    import logging

    from sqlalchemy import select

    from app.models.relay import RelayGroup
    from app.services.relays import group_relay_counts, relays_active, routed_resource_counts

    RELAY_GROUP_RELAYS_GAUGE.clear()
    RELAY_GROUP_ROUTED_GAUGE.clear()
    if not relays_active():
        return
    try:
        group_ids = list((await db.execute(select(RelayGroup.id))).scalars().all())
        counts = await group_relay_counts(db, group_ids)
        routed = await routed_resource_counts(db, group_ids)
    except Exception as exc:
        logging.getLogger(__name__).error("Failed to read the relay groups: %s", format_exception_message(exc))
        with contextlib.suppress(Exception):
            await db.rollback()
        return
    for gid in group_ids:
        online, total = counts.get(gid, (0, 0))
        labels = {"group_id": str(gid), "platform_id": platform_id}
        RELAY_GROUP_RELAYS_GAUGE.labels(state="online", **labels).set(online)
        RELAY_GROUP_RELAYS_GAUGE.labels(state="enrolled", **labels).set(total)
        RELAY_GROUP_ROUTED_GAUGE.labels(**labels).set(routed.get(gid, 0))


async def _read_sandbox_workspaces(db: AsyncSession, platform_id: str) -> None:
    """Set the sandbox workspace gauges from the workspace table: one query."""
    import contextlib
    import logging

    from sqlalchemy import func, select

    from app.models.sandbox_workspace import SandboxWorkspace as W

    try:
        count, size, files, restarts = (
            await db.execute(
                select(
                    func.count(W.id),
                    func.coalesce(func.sum(W.size_bytes), 0),
                    func.coalesce(func.sum(W.file_count), 0),
                    func.coalesce(func.sum(W.kernel_restarts), 0),
                )
            )
        ).one()
    except Exception as exc:
        logging.getLogger(__name__).error("Failed to read the sandbox workspaces: %s", format_exception_message(exc))
        with contextlib.suppress(Exception):
            await db.rollback()
        return
    for measure, value in (("count", count), ("bytes", size), ("files", files), ("kernel_restarts", restarts)):
        SANDBOX_WORKSPACES_GAUGE.labels(measure=measure, platform_id=platform_id).set(float(value or 0))


@router.get("/metrics", dependencies=[Depends(verify_prometheus_access)])
async def metrics(db: AsyncSession = Depends(get_db)) -> Response:
    """Expose Prometheus metrics, fetching the latest quota usage from Redis and Postgres."""
    from app.api.v1.platform_settings_helpers import get_platform_settings
    ps = await get_platform_settings(db)
    platform_id = str(ps.id) if ps else "unknown"

    redis = await redis_client()

    current_periods = get_current_periods()

    # "sandbox" is unconditional, unlike "llm_proxy": llm_proxy is disabled
    # (and therefore excluded here) in xtm_one mode, but sandbox execution
    # has no such mode restriction (spec 002-sandbox-quota-usage-tracking
    # FR-012, research.md §9).
    usage_types = ["agentic", "sandbox"]
    if settings.is_copilot_like:
        usage_types.append("llm_proxy")

    for period_key, semantic in current_periods.items():
        for qt in usage_types:
            await _read_quota_total(db, redis, qt, period_key, semantic, platform_id)

    await _read_assignment_dispatch(db, platform_id)
    await _read_relays(db, platform_id)
    await _read_sandbox_workspaces(db, platform_id)
    from app.services.ctem_signal_intake import depth as signal_intake_depth

    # Sets xtmone_ctem_signal_intake_queue_depth from Redis, the same for every worker (#2584).
    await signal_intake_depth()

    # Generate the standard Prometheus plain-text response
    data = generate_latest(REGISTRY)
    return Response(content=data, media_type="text/plain; version=0.0.4")
