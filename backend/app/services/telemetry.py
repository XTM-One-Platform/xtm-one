# SPDX-License-Identifier: Apache-2.0

"""Anonymous usage telemetry — adoption metrics over OpenTelemetry / OTLP.

XTM One periodically exports a small set of **anonymous** platform usage
metrics to Filigran's telemetry collector, mirroring the telemetry already
shipped in OpenCTI and OpenAEV.  This lets the team measure product adoption
(how many agents, assignments, integrations, ... are configured across all
installs) and prioritise the roadmap.

What is collected
-----------------
* Resource identity: the platform version, the anonymous per-install
  ``instance_id`` (the ``platform_settings`` row UUID), the install creation
  date, and the platform mode.
* The deployment edition (Enterprise vs Community): Enterprise == AI is
  licensed — a validated Filigran license OR a connected Enterprise Edition
  platform (the same ONE gate that unlocks chat and creating
  agents/KBs/assignments in the UI, ``is_agentic_ai_licensed``). The
  ``agentic_source`` label (xtm_license / ee_platform / none) says where the
  acts ceilings come from, so analytics can still tell an XTM-licensed
  install from one whose Enterprise Edition is inherited from a module.
* Deployment tags: the optional ``TELEMETRY_TAGS`` env var
  (``settings.telemetry_tags``, comma-separated freeform tags such as
  ``saas,eu-west``, normalized via ``telemetry_tags_normalized``) exported as
  the ``filigran.telemetry.tags`` resource attribute on every metric, so
  adoption analytics can slice any metric by deployment dimension.
* Aggregate entity counts: users, agents, assignments, runs, integrations,
  knowledge bases, MCP servers, custom tools, channel bots, prompts, skills,
  groups, conversations, messages, and configured LLM providers
  (broken down by provider type).
* Per-day activity series: assignment runs, conversations, and chat messages
  **created per day** over the rolling :data:`_LLM_BY_DAY_WINDOW_DAYS`-day
  window (same window and ``date``-attribute contract as the LLM/login
  analytics below).  The matching all-time totals above are cumulative
  snapshots kept for continuity — per-day / per-month adoption analytics must
  be computed from these by-day series, never by diffing successive exports
  of the cumulative totals.
* Agentic executions: the number of **top-level agentic loop executions** the
  install performed — chat turns, General Assistant turns, assignment runs,
  background sub-agent tasks, and browser-extension tasks, each counted once
  no matter how many agents it fans out to.  Reported as an all-time total
  and a per-day series over the same rolling window.  This is the volume of
  agentic *work*, which no other metric captures: ``assignment_runs_*`` only
  covers automations and ``messages_*`` counts conversation turns.  Sourced
  from the durable ``agentic_execution_daily`` table (see
  :mod:`app.services.agentic_executions`); counts only, no identities.
* Provider breakdowns: configured integrations and channel bots broken down
  by their catalog ``provider`` identifier (integrations: ``github``,
  ``slack``, ``opencti``, ...; channel bots carry the ``_bot`` suffix:
  ``slack_bot``, ``teams_bot``, ..., ``googlechat_bot`` — set at creation,
  not user-editable, so no user data is involved).  MCP servers and custom
  tools stay count-only: they are fully user-defined, so there is no
  anonymous identity to report.
* Aggregate LLM analytics from the local trace store (``llm_traces`` /
  ``llm_scores``, the same source as the Analytics dashboard): total cost,
  total tokens, average latency, and average user-feedback score — overall and
  per day over the last :data:`_LLM_BY_DAY_WINDOW_DAYS` days.  Aggregate
  numbers only: never per-user / per-agent / per-conversation breakdowns and
  never trace content.
* Aggregate login activity from the audit trail (``audit_logs`` rows with
  ``action = "login"``, covering local JWT, OIDC, and SAML sign-ins
  uniformly; MCP OAuth client authorization grants share the action but are
  excluded — see :func:`_refresh_login_analytics`): the all-time total and a
  per-day series over the same rolling :data:`_LLM_BY_DAY_WINDOW_DAYS`-day
  window as the LLM analytics.  Counts only — no user identity ever leaves
  the install.  This mirrors the login telemetry already shipped by OpenCTI
  (``opencti_user_login_count``), OpenAEV
  (``openaev_count_logins_per_day``), and OpenCRQ
  (``opencrq_user_login_count``).

What is **never** collected
---------------------------
No personal data, no credentials/secrets, no message or document content, no
entity names — only counts and the metadata above.

Transport
---------
Metrics are pushed via **OTLP over HTTP** to XTM One's dedicated Filigran
collector host ``telemetry.one.filigran.io`` (staging in dev mode).  The
collector lands the data in the ``metrics_one`` prefix of the
``filigran-telemetry-*`` S3 bucket downstream — the application only speaks
OTLP.  The ``service.name`` resource attribute is ``xtm-one-telemetry``.

Design (mirrors OpenCTI/OpenAEV)
--------------------------------
* A startup **connectivity probe** gates the OTLP exporter: when the collector
  is unreachable (air-gapped installs, blocked egress) telemetry is silently
  disabled for the process lifetime.  The probe adds at most a short, bounded
  delay to startup (``_PROBE_TIMEOUT_SECONDS``) and never fails it.
* Metric values live in an in-memory snapshot (:data:`_state`) read by
  OpenTelemetry **observable-gauge callbacks**.  Every value is read from the
  database by :func:`refresh_telemetry_data`, so it is install-wide, never a
  process's own counter.
* **One export per install per interval** (~6 hours, shorter in debug/dev
  mode), whatever the number of processes (``WEB_WORKERS`` uvicorn workers x
  platform replicas).  Every process builds the pipeline, but its reader
  never exports on its own (:func:`_new_reader`: no timer, no export at
  shutdown).  The scheduler tick calls :func:`export_telemetry_if_due`, which
  takes a Redis window lease (``SET NX EX`` for the export interval, the same
  pattern as the tick's own lock) and, in the one process that got it,
  refreshes the snapshot and exports.  A process that dies with the lease hands
  over when the lease expires; a Redis outage skips the export (see
  :func:`export_telemetry_if_due`).

Switch
------
Telemetry is on by default, like OpenCTI and OpenAEV. ``TELEMETRY_ENABLED=false``
turns it off: :func:`init_telemetry` returns before the connectivity probe, so
no exporter is built and no export lease is ever taken (OpenCTI's
``TELEMETRY_MANAGER__ENABLED``, documented here). It also stays off when the
collector is unreachable (the startup connectivity probe), under the test
runner, or in a CI/CD pipeline (to keep ephemeral CI instances out of
production metrics).

The user docs page ``docs/docs/deployment/telemetry.md`` lists every gauge
:func:`_register_gauges` registers; ``tests/test_telemetry.py`` fails when a
gauge is added or removed without it.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# OpenTelemetry ``service.name`` — distinguishes this product on the shared
# Filigran collector (OpenCTI uses ``opencti-telemetry``, OpenAEV
# ``openaev-telemetry``).
TELEMETRY_SERVICE_NAME = "xtm-one-telemetry"

# Metric name prefix (matches OpenCTI's ``opencti_*`` convention).
_PREFIX = "xtm_one_"

# Hardcoded Filigran collector endpoints (OTLP/HTTP ``/v1/metrics``).  XTM One
# has its own collector host (``telemetry.one.*``) so its metrics land in the
# dedicated ``metrics_one`` S3 prefix rather than mixing with OpenCTI/OpenAEV.
# Staging is used in dev/debug mode so local development never pollutes
# production analytics.  Overridable via ``TELEMETRY_ENDPOINT``.
_PROD_ENDPOINT = "https://telemetry.one.filigran.io/v1/metrics"
_STAGING_ENDPOINT = "https://telemetry.one.staging.filigran.io/v1/metrics"

# Collect/export cadence.  Production mirrors OpenCTI/OpenAEV (export every
# 6h, once per install: see :func:`export_telemetry_if_due`, which refreshes
# the snapshot right before exporting).  Dev/debug
# mode uses a 3h window instead of the production 6h — keyed off the same dev
# condition as the endpoint (see ``_is_dev_mode``) so dev installs always pair
# the staging collector with the dev cadence.  The window is deliberately NOT
# tighter than hours: the staging collector persists one object per export per
# instance, and a 2-minute dev cadence flooded the XTM Hub staging bucket when
# many long-lived dev/CI instances exported at once.
_ONE_MINUTE_MS = 60 * 1000
_SIX_HOURS_MS = 6 * 60 * _ONE_MINUTE_MS
_DEV_EXPORT_INTERVAL_MS = 3 * 60 * _ONE_MINUTE_MS

# Connectivity-probe timeout (seconds).  Short so a slow/blocked collector
# never delays startup meaningfully.
_PROBE_TIMEOUT_SECONDS = 5.0
# Export HTTP timeout (milliseconds) for the OTLP exporter.
_EXPORT_TIMEOUT_MS = 15_000

# Window lease of the install's single exporter: set NX with a TTL of
# the export interval and never released, so the key also marks "this
# interval's export happened" - the pattern of the scheduler tick's own lock
# (``copilot:scheduler_tick_lock`` in ``main._autonomous_loop``).
_EXPORT_LEASE_KEY = "copilot:telemetry_export_lock"

# Rolling window (days, today included) for the per-day LLM analytics series
# (``llm_*_by_day`` gauges).  Bounded so the by-day gauges keep a fixed, small
# label cardinality on the collector regardless of install age.
_LLM_BY_DAY_WINDOW_DAYS = 30

# ``details.login_method`` values written by MCP OAuth client authorization
# (``api/v1/mcp_oauth.py``).  Those grants are audited with ``action="login"``
# too, but they are consent grants — not platform sign-ins — so the login
# telemetry excludes them (see :func:`_refresh_login_analytics`).
_MCP_OAUTH_LOGIN_METHODS = ("mcp_oauth", "mcp_oauth_session")


def _llm_by_day_window_start() -> datetime:
    """UTC-midnight start of the per-day LLM analytics window.

    Anchored at midnight (not ``now - N days``) and spanning today plus the
    previous ``_LLM_BY_DAY_WINDOW_DAYS - 1`` days, so the DATE-grouped query
    yields at most :data:`_LLM_BY_DAY_WINDOW_DAYS` distinct ``date`` labels —
    a mid-day anchor would slice a partial extra day off the far end and leak
    a 31st label.
    """
    today_utc = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today_utc - timedelta(days=_LLM_BY_DAY_WINDOW_DAYS - 1)


# ── In-memory metric snapshot ────────────────────────────────────────────────


@dataclass
class _TelemetryState:
    """Latest computed metric values, read by observable-gauge callbacks.

    Refreshed in place by :func:`refresh_telemetry_data`.  Plain integers so a
    callback firing mid-refresh observes a coherent (if slightly stale) value
    rather than raising.
    """

    # Licensing / edition
    is_enterprise_edition: int = 0
    registered_platforms_count: int = 0
    registered_ee_platforms_count: int = 0

    # Entity counts
    users_count: int = 0
    active_users_count: int = 0
    # Terms of Use acceptance: 1 when the platform has accepted the
    # currently published ToU version (a single platform-wide acceptance),
    # else 0. Always 0 when the gate is off.
    terms_accepted: int = 0
    agents_count: int = 0
    company_managed_agents_count: int = 0
    assignments_count: int = 0
    active_assignments_count: int = 0
    assignment_runs_count: int = 0
    integrations_count: int = 0
    knowledge_bases_count: int = 0
    mcp_servers_count: int = 0
    custom_tools_count: int = 0
    channel_bots_count: int = 0
    prompts_count: int = 0
    agent_skills_count: int = 0
    groups_count: int = 0
    conversations_count: int = 0
    messages_count: int = 0
    llm_providers_count: int = 0

    # Per-day activity series (rows *created* per UTC day) over the rolling
    # _LLM_BY_DAY_WINDOW_DAYS window, keyed by ISO date (YYYY-MM-DD) and
    # surfaced as a ``date`` gauge attribute.  The cumulative ``*_count``
    # totals above stay as instant snapshots for continuity; these series are
    # the source for per-day / per-month adoption analytics.
    assignment_runs_by_day: dict[str, int] = field(default_factory=dict)
    conversations_by_day: dict[str, int] = field(default_factory=dict)
    messages_by_day: dict[str, int] = field(default_factory=dict)

    # Agentic executions (one top-level agentic loop run per logical request,
    # all entry points combined) from the durable ``agentic_execution_daily``
    # table.  ``agentic_executions_count`` is the all-time cumulative total;
    # ``agentic_executions_by_day`` is the per-day series over the same
    # rolling _LLM_BY_DAY_WINDOW_DAYS window, keyed by ISO date.
    agentic_executions_count: int = 0
    agentic_executions_by_day: dict[str, int] = field(default_factory=dict)

    # Distinct assignments with at least one run created in a UTC month, for
    # the current and the previous month, split by where the assignment comes
    # from: ``{"YYYY-MM": {"builtin": n, "template_default": n, "custom": n}}``.
    # Surfaced as ``month`` and ``origin`` gauge attributes.
    assignments_ran_by_month: dict[str, dict[str, int]] = field(default_factory=dict)

    # Agent portable-format adoption (from the audit trail): how often each
    # export/import format is actually used, and how often an import is
    # rejected before anything is created.  ``by_format`` keys are the format
    # ids ("native" / "agentschema"); the rejected count has no format because
    # a rejection can happen before detection resolves one.
    agent_exports_by_format: dict[str, int] = field(default_factory=dict)
    agent_imports_by_format: dict[str, int] = field(default_factory=dict)
    agent_imports_rejected_count: int = 0

    # Multi-dimensional: configured LLM providers by provider type.
    llm_providers_by_type: dict[str, int] = field(default_factory=dict)

    # Multi-dimensional: configured integrations / channel bots by provider.
    # ``provider`` is the catalog identifier (github, slack, opencti, ...),
    # set at creation and not user-editable — an exact, anonymous identity.
    integrations_by_provider: dict[str, int] = field(default_factory=dict)
    channel_bots_by_provider: dict[str, int] = field(default_factory=dict)

    # LLM analytics aggregates from the local trace store (llm_traces /
    # llm_scores).  Totals cover every retained trace; averages are ``None``
    # (gauge not emitted) until at least one trace / feedback score exists.
    llm_total_cost: float = 0.0
    llm_total_tokens: int = 0
    llm_avg_latency_ms: float | None = None
    llm_avg_feedback: float | None = None
    # Per-day series over the last _LLM_BY_DAY_WINDOW_DAYS days, keyed by
    # ISO date (YYYY-MM-DD) and surfaced as a ``date`` gauge attribute.
    llm_total_cost_by_day: dict[str, float] = field(default_factory=dict)
    llm_total_tokens_by_day: dict[str, int] = field(default_factory=dict)
    llm_avg_latency_ms_by_day: dict[str, float] = field(default_factory=dict)
    llm_avg_feedback_by_day: dict[str, float] = field(default_factory=dict)

    # Login activity from the audit trail (audit_logs rows with
    # action="login" — local JWT, OIDC, and SAML sign-ins all land there;
    # MCP OAuth authorization grants share the action but are excluded, see
    # _refresh_login_analytics).  ``user_login_count`` is the all-time total
    # (a cumulative snapshot like OpenCTI's opencti_user_login_count);
    # ``logins_by_day`` is a per-day series over the rolling
    # _LLM_BY_DAY_WINDOW_DAYS window, keyed by ISO date and surfaced as a
    # ``date`` gauge attribute.  Counts only — no user identity is ever
    # exported.
    user_login_count: int = 0
    logins_by_day: dict[str, int] = field(default_factory=dict)

    # Whether AI is actually running here, resolved (FR-031). Distinct
    # from ``platform_mode`` below, which is only the deployment *default*: the
    # runtime toggle means a ``copilot`` deployment can be sitting AI-off and an
    # ``ai_disabled`` one AI-on, so counting the mode string would answer a
    # different question than "how many deployments run without AI".
    ai_enabled: int = 1

    # String dimensions surfaced as gauge attributes on ``platform_info``.
    platform_mode: str = settings.platform_mode
    # Where the agentic ceilings come from: ``xtm_license`` / ``ee_platform``
    # / ``none`` — the same vocabulary as ``agentic_source`` in
    # ``GET /platform/config``. Replaces the retired three-tier
    # ``deployment_tier`` label (ce_only / ee_platform / xtm_licensed).
    agentic_source: str = "none"
    edition: str = "CE"


_state = _TelemetryState()

# Module-level handle to the SDK MeterProvider, set by :func:`init_telemetry`
# and torn down by :func:`shutdown_telemetry`.  ``None`` when telemetry is
# disabled or could not initialise.
_provider = None
# The pipeline's exporter, wrapped by :func:`_recording_exporter` so an export
# can report whether it was delivered.
_recorder = None
_initialized = False


# ── Data collection ──────────────────────────────────────────────────────────


async def _scalar_count(db: AsyncSession, model) -> int | None:
    """Return ``COUNT(*)`` for a model, or ``None`` on error.

    Returning ``None`` (rather than ``0``) lets :func:`refresh_telemetry_data`
    preserve the previous snapshot value across a transient DB error instead of
    clobbering a good count with a bogus zero.
    """
    try:
        result = await db.execute(select(func.count()).select_from(model))
        return int(result.scalar() or 0)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry count failed for %s", getattr(model, "__name__", model))
        return None


async def _filtered_count(db: AsyncSession, model, *whereclauses) -> int | None:
    """Return ``COUNT(*)`` with WHERE clauses, or ``None`` on error.

    ``None`` on failure (not ``0``) so callers can keep the prior snapshot value
    instead of overwriting it after a transient DB error.
    """
    try:
        stmt = select(func.count()).select_from(model)
        for clause in whereclauses:
            stmt = stmt.where(clause)
        result = await db.execute(stmt)
        return int(result.scalar() or 0)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry filtered count failed for %s", model)
        return None


def _apply(attr: str, value: int | None) -> None:
    """Update a snapshot counter only when the query succeeded.

    A ``None`` value signals a failed read, so the previous value is preserved
    — matching the read-only, graceful-degradation contract of
    :func:`refresh_telemetry_data`.
    """
    if value is not None:
        setattr(_state, attr, value)


async def _refresh_terms_of_use(db: AsyncSession) -> None:
    """Refresh the platform-wide Terms of Use acceptance gauge.

    Terms of Use are accepted once for the whole platform (stored on
    ``PlatformSettings.tou_accepted_version``), not per-user. This reports 1
    when that acceptance matches the currently published version, else 0. It is
    pinned to 0 when the gate is inactive on this instance.

    Uses the read-only ``peek_platform_settings`` (never creates the singleton):
    telemetry refresh runs on the shared scheduler session and must stay free
    of write side effects — same rule ``_refresh_edition`` follows. A missing
    row simply means nothing has been accepted yet.
    """
    if not settings.terms_of_use_active:
        _apply("terms_accepted", 0)
        return

    from app.api.v1.platform_settings_helpers import peek_platform_settings

    ps = await peek_platform_settings(db)
    accepted = (
        ps is not None
        and ps.tou_accepted_version == settings.terms_of_use_current_version
    )
    _apply("terms_accepted", 1 if accepted else 0)


async def _refresh_edition(db: AsyncSession) -> None:
    """Resolve EE-vs-CE, the agentic source, and platform registration counts.

    Enterprise Edition is defined exactly as the product defines it — AI is
    licensed: a validated Filigran license OR a connected Enterprise Edition
    platform (XTM One), a validated license (Copilot). Derived from the same
    single-source-of-truth licensing logic the product reports with
    (``platform.resolve_deployment_edition``, ``xtm_license.ee_platform_status``)
    so telemetry can never diverge from what the product itself reports.
    """
    from app.api.v1.platform import EDITION_ENTERPRISE, resolve_deployment_edition
    from app.models.platform_registration import PlatformRegistration
    from app.models.platform_settings import PlatformSettings
    from app.services.xtm_license import ee_platform_status, resolve_xtm_license

    # Load the singleton READ-ONLY (do not create it): unlike
    # ``get_platform_settings``, this never INSERT+FLUSHes a row, so the refresh
    # stays a pure read on the shared scheduler session.  ``resolve_xtm_license``
    # already tolerates ``ps=None``.
    ps_result = await db.execute(select(PlatformSettings).limit(1))
    ps = ps_result.scalar_one_or_none()
    xtm_info = resolve_xtm_license(ps)

    # Resolve into locals BEFORE mutating any ``_state`` field:
    # ``ee_platform_status`` runs a DB query that can raise, and the caller's
    # try/except only "preserves the previous edition snapshot" if we haven't
    # already half-updated it — so the three edition fields are committed
    # atomically below, once every fallible read succeeds.
    if settings.is_xtm_one:
        has_ee, _active_count = await ee_platform_status(db, xtm_info)
        edition = resolve_deployment_edition(
            True, xtm_validated=xtm_info.license_validated, has_ee=has_ee
        )
        is_ee = edition == EDITION_ENTERPRISE
        agentic_source = (
            "xtm_license"
            if xtm_info.license_validated
            else "ee_platform" if has_ee else "none"
        )
    else:
        is_ee = xtm_info.license_validated
        agentic_source = "xtm_license" if is_ee else "none"

    _state.is_enterprise_edition = 1 if is_ee else 0
    _state.edition = "EE" if is_ee else "CE"
    _state.agentic_source = agentic_source

    # Registered platform counts (active registrations + EE subset).
    _apply(
        "registered_platforms_count",
        await _filtered_count(
            db,
            PlatformRegistration,
            PlatformRegistration.status == "active",
        ),
    )
    _apply(
        "registered_ee_platforms_count",
        await _filtered_count(
            db,
            PlatformRegistration,
            PlatformRegistration.status == "active",
            PlatformRegistration.contract == "EE",
        ),
    )


async def _refresh_llm_providers(db: AsyncSession) -> None:
    """Count configured LLM providers, broken down by provider type."""
    from app.models.llm_config import LLMProviderConfig

    try:
        result = await db.execute(
            select(LLMProviderConfig.provider, func.count())
            .group_by(LLMProviderConfig.provider)
        )
        by_type: dict[str, int] = {}
        total = 0
        for provider, count in result.all():
            key = str(provider or "unknown")
            by_type[key] = int(count or 0)
            total += int(count or 0)
        _state.llm_providers_by_type = by_type
        _state.llm_providers_count = total
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry LLM provider breakdown failed")


async def _refresh_provider_breakdowns(db: AsyncSession) -> None:
    """Count configured integrations and channel bots, broken down by provider.

    ``provider`` is the catalog identifier (``github``, ``slack``,
    ``opencti``, ``hubspot``, ... for integrations; the ``_bot``-suffixed
    keys ``slack_bot``, ``teams_bot``, ``whatsapp_bot``, ``telegram_bot``,
    ``discord_bot``, ``googlechat_bot`` for channel bots — see
    ``ChannelBot.provider`` / ``CHANNEL_TO_BOT_PROVIDER``), set at creation
    and not user-editable — an exact, anonymous identity with no user data.
    MCP servers and custom tools are deliberately excluded: they are fully
    user-defined, so there is no anonymous slug to report.
    """
    from app.models.channel_bot import ChannelBot
    from app.models.integration import Integration

    try:
        result = await db.execute(
            select(Integration.provider, func.count())
            .group_by(Integration.provider)
        )
        _state.integrations_by_provider = {
            str(provider or "unknown"): int(count or 0)
            for provider, count in result.all()
        }
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry integration provider breakdown failed")

    try:
        result = await db.execute(
            select(ChannelBot.provider, func.count())
            .group_by(ChannelBot.provider)
        )
        _state.channel_bots_by_provider = {
            str(provider or "unknown"): int(count or 0)
            for provider, count in result.all()
        }
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry channel bot provider breakdown failed")


async def _refresh_llm_analytics(db: AsyncSession) -> None:
    """Recompute the LLM analytics aggregates from the local trace store.

    Reads the PostgreSQL ``llm_traces`` / ``llm_scores`` tables (the same
    source as the Analytics dashboard and ``trace_storage.aggregate_daily_stats``):

    * **Totals** over every retained trace (bounded by the trace retention
      purge window): total cost, total tokens, average latency, and average
      ``user_feedback`` score.
    * **Per-day series** over the last :data:`_LLM_BY_DAY_WINDOW_DAYS` days,
      keyed by UTC ISO date.

    Aggregate numbers only — no per-user / per-agent / per-conversation
    dimensions and no trace content ever leave the install.  Installs using
    the ClickHouse tracing store keep zeros here (the PG tables are empty);
    guarded like every other refresh step so a failure preserves the previous
    snapshot values.
    """
    from app.models.llm_trace import LLMTrace
    from app.services.tracing.feedback_scores import feedback_per_trace_subquery

    try:
        feedback_sq = feedback_per_trace_subquery()

        totals = (
            await db.execute(
                select(
                    func.coalesce(func.sum(LLMTrace.total_cost), 0).label("total_cost"),
                    func.coalesce(func.sum(LLMTrace.total_tokens), 0).label("total_tokens"),
                    func.avg(LLMTrace.duration_ms).label("avg_latency_ms"),
                    func.avg(feedback_sq.c.feedback_value).label("avg_feedback"),
                )
                .select_from(LLMTrace)
                .outerjoin(feedback_sq, feedback_sq.c.trace_id == LLMTrace.id)
            )
        ).one()

        window_start = _llm_by_day_window_start()
        day_expr = sa.cast(
            func.timezone("UTC", LLMTrace.started_at), sa.Date
        ).label("day")
        rows = (
            await db.execute(
                select(
                    day_expr,
                    func.coalesce(func.sum(LLMTrace.total_cost), 0).label("total_cost"),
                    func.coalesce(func.sum(LLMTrace.total_tokens), 0).label("total_tokens"),
                    func.avg(LLMTrace.duration_ms).label("avg_latency_ms"),
                    func.avg(feedback_sq.c.feedback_value).label("avg_feedback"),
                )
                .select_from(LLMTrace)
                .outerjoin(feedback_sq, feedback_sq.c.trace_id == LLMTrace.id)
                .where(LLMTrace.started_at >= window_start)
                .group_by(day_expr)
                .order_by(day_expr)
            )
        ).all()

        cost_by_day: dict[str, float] = {}
        tokens_by_day: dict[str, int] = {}
        latency_by_day: dict[str, float] = {}
        feedback_by_day: dict[str, float] = {}
        for row in rows:
            if row.day is None:  # pragma: no cover - defensive
                continue
            key = row.day.isoformat()
            cost_by_day[key] = float(row.total_cost or 0)
            tokens_by_day[key] = int(row.total_tokens or 0)
            if row.avg_latency_ms is not None:
                latency_by_day[key] = float(row.avg_latency_ms)
            if row.avg_feedback is not None:
                feedback_by_day[key] = float(row.avg_feedback)

        # Commit the whole analytics snapshot atomically, only after every
        # fallible read succeeded (mirrors the _refresh_edition contract).
        _state.llm_total_cost = float(totals.total_cost or 0)
        _state.llm_total_tokens = int(totals.total_tokens or 0)
        _state.llm_avg_latency_ms = (
            None if totals.avg_latency_ms is None else float(totals.avg_latency_ms)
        )
        _state.llm_avg_feedback = (
            None if totals.avg_feedback is None else float(totals.avg_feedback)
        )
        _state.llm_total_cost_by_day = cost_by_day
        _state.llm_total_tokens_by_day = tokens_by_day
        _state.llm_avg_latency_ms_by_day = latency_by_day
        _state.llm_avg_feedback_by_day = feedback_by_day
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry LLM analytics refresh failed", exc_info=True)


async def _refresh_login_analytics(db: AsyncSession) -> None:
    """Recompute the login-activity metrics from the audit trail.

    ``audit_logs`` rows with ``action = "login"`` record every user sign-in
    (local JWT, OIDC, and SAML flows all write one — see ``api/v1/auth.py``
    and ``api/auth_sso.py``), so both metrics cover every authentication
    method uniformly.  MCP OAuth client authorization grants
    (``api/v1/mcp_oauth.py``) share the same action for audit visibility but
    are consent grants, not platform sign-ins — they are excluded via their
    ``details.login_method`` marker (:data:`_MCP_OAUTH_LOGIN_METHODS`) so
    they can never inflate the counts (the real sign-in rows either carry a
    non-MCP method like ``local`` or no ``login_method`` at all):

    * ``user_login_count`` — all-time total (cumulative snapshot, mirroring
      OpenCTI's ``opencti_user_login_count`` contract).
    * ``logins_by_day`` — per-day series over the same rolling
      :data:`_LLM_BY_DAY_WINDOW_DAYS`-day window as the LLM analytics, keyed
      by UTC ISO date.

    Counts only — user ids/emails never leave the install.  Guarded like
    every other refresh step: a failure preserves the previous snapshot
    values instead of clobbering them with zeros.
    """
    from app.models.audit_log import AuditLog

    # NULL-safe exclusion of MCP OAuth grants: ``details`` may be NULL or
    # missing the key (OIDC/SAML rows), and ``NULL NOT IN (...)`` would
    # silently drop those real sign-ins — COALESCE keeps them counted.
    sign_in_filter = func.coalesce(
        AuditLog.details["login_method"].astext, ""
    ).notin_(_MCP_OAUTH_LOGIN_METHODS)

    try:
        total = (
            await db.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "login", sign_in_filter)
            )
        ).scalar()

        window_start = _llm_by_day_window_start()
        day_expr = sa.cast(
            func.timezone("UTC", AuditLog.created_at), sa.Date
        ).label("day")
        rows = (
            await db.execute(
                select(day_expr, func.count().label("login_count"))
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "login",
                    AuditLog.created_at >= window_start,
                    sign_in_filter,
                )
                .group_by(day_expr)
                .order_by(day_expr)
            )
        ).all()

        logins_by_day: dict[str, int] = {}
        for row in rows:
            if row.day is None:  # pragma: no cover - defensive
                continue
            logins_by_day[row.day.isoformat()] = int(row.login_count or 0)

        # Commit the whole login snapshot atomically, only after every
        # fallible read succeeded (mirrors the _refresh_llm_analytics contract).
        _state.user_login_count = int(total or 0)
        _state.logins_by_day = logins_by_day
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry login analytics refresh failed", exc_info=True)


async def _refresh_agent_format_adoption(db: AsyncSession) -> None:
    """Recompute agent export/import adoption per portable format.

    Reads the audit trail, which is where the export and import endpoints
    record the format they used (``details.format`` on ``export`` rows,
    ``details.source_format`` on ``import`` rows) — see
    ``api/v1/agents.py`` and ``api/v1/agent_import.py``.  Three metrics:

    * ``agent_exports_by_format`` — exports per format, to see whether
      AgentSchema is used at all next to the native bundle.
    * ``agent_imports_by_format`` — successful imports per *detected* format,
      the numerator of the import validation pass rate.
    * ``agent_imports_rejected_count`` — uploads rejected by detection or
      validation (``action = "import_rejected"``), the denominator's other
      half.  Rejections carry no format because detection may be what failed.

    Counts only — no agent names, file names or user identities leave the
    install.  These are cumulative over *retained* audit rows, so they are a
    lower bound if an operator prunes the audit trail.  Guarded like every
    other refresh step: a failure preserves the previous snapshot values
    instead of clobbering them with zeros.
    """
    from app.models.audit_log import AuditLog

    async def _by_format(action: str, details_key: str) -> dict[str, int]:
        # ``details`` is nullable and older rows predate the format keys, so
        # COALESCE the missing case into an explicit "unknown" bucket rather
        # than letting NULL silently drop the row from the GROUP BY.
        format_expr = func.coalesce(
            AuditLog.details[details_key].astext, "unknown"
        ).label("format")
        rows = (
            await db.execute(
                select(format_expr, func.count().label("count"))
                .select_from(AuditLog)
                .where(AuditLog.entity_type == "agent", AuditLog.action == action)
                .group_by(format_expr)
            )
        ).all()
        return {str(row.format): int(row.count or 0) for row in rows}

    try:
        exports = await _by_format("export", "format")
        imports = await _by_format("import", "source_format")
        rejected = (
            await db.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.entity_type == "agent",
                    AuditLog.action == "import_rejected",
                )
            )
        ).scalar()

        # Commit the whole snapshot atomically, only after every fallible read
        # succeeded (mirrors the _refresh_llm_analytics contract).
        _state.agent_exports_by_format = exports
        _state.agent_imports_by_format = imports
        _state.agent_imports_rejected_count = int(rejected or 0)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry agent format adoption refresh failed", exc_info=True)


async def _refresh_activity_by_day(db: AsyncSession) -> None:
    """Recompute the per-day activity series (rows created per UTC day).

    Covers assignment runs, conversations, and chat messages over the rolling
    :data:`_LLM_BY_DAY_WINDOW_DAYS`-day window — the metrics whose purpose is
    to observe activity *variation over time* (runs/day, conversations/day,
    messages/day and their monthly rollups).  Their cumulative ``*_count``
    twins stay as instant snapshots; the warehouse must never have to diff
    successive cumulative exports to derive daily activity.

    Grouped by the UTC date of ``created_at`` and keyed by ISO date, exactly
    like :func:`_refresh_llm_analytics` / :func:`_refresh_login_analytics`.
    Deletions are out of scope by design: the series counts rows created in
    the window.  Each series is individually guarded so a failed read
    preserves that series' previous snapshot instead of clobbering it.
    """
    from app.models.assignment import AssignmentRun
    from app.models.chat import Conversation, Message

    window_start = _llm_by_day_window_start()
    series = (
        (AssignmentRun, AssignmentRun.created_at, "assignment_runs_by_day"),
        (Conversation, Conversation.created_at, "conversations_by_day"),
        (Message, Message.created_at, "messages_by_day"),
    )
    for model, ts_col, attr in series:
        try:
            day_expr = sa.cast(func.timezone("UTC", ts_col), sa.Date).label("day")
            rows = (
                await db.execute(
                    select(day_expr, func.count().label("row_count"))
                    .select_from(model)
                    .where(ts_col >= window_start)
                    .group_by(day_expr)
                    .order_by(day_expr)
                )
            ).all()
            by_day: dict[str, int] = {}
            for row in rows:
                if row.day is None:  # pragma: no cover - defensive
                    continue
                by_day[row.day.isoformat()] = int(row.row_count or 0)
            setattr(_state, attr, by_day)
        except Exception:  # pragma: no cover - defensive
            logger.debug(
                "Telemetry activity by-day refresh failed for %s", attr, exc_info=True
            )


_ASSIGNMENT_ORIGINS = ("builtin", "template_default", "custom")


def _month_starts_utc() -> tuple[datetime, datetime]:
    """UTC-midnight first day of the previous month and of the current month."""
    current = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous = (current - timedelta(days=1)).replace(day=1)
    return previous, current


async def _refresh_assignments_ran_by_month(db: AsyncSession) -> None:
    """Recompute the distinct assignments that ran per UTC month, by origin.

    An assignment counts once in a month when at least one of its runs was
    created in that month (UTC), for the current and the previous month.  The
    origin is derived, there is no column for it:

    * ``builtin``: the assignment's agent is a seeded built-in agent.
    * ``template_default``: the agent was created from a template and the
      assignment's name is one of that template's ``default_assignments``
      names, as :func:`default_assignment_text` stores them at creation.
    * ``custom``: every other assignment, the ones a user wrote.

    Both months are zero-filled so a month with no run still exports zeros.
    Counts only: no assignment, agent or template name leaves the install.
    Guarded like every other refresh step: a failed read preserves the
    previous snapshot instead of clobbering it.
    """
    from collections.abc import Mapping

    from app.models.agent import Agent
    from app.models.agent_template import AgentTemplate, default_assignment_text
    from app.models.assignment import Assignment, AssignmentRun

    previous_start, current_start = _month_starts_utc()
    try:
        month_expr = func.to_char(
            func.timezone("UTC", AssignmentRun.created_at), "YYYY-MM"
        ).label("month")
        rows = (
            await db.execute(
                select(
                    month_expr,
                    Assignment.id.label("assignment_id"),
                    Assignment.name.label("name"),
                    Agent.is_builtin.label("is_builtin"),
                    Agent.template_slug.label("template_slug"),
                )
                .select_from(AssignmentRun)
                .join(Assignment, Assignment.id == AssignmentRun.assignment_id)
                .join(Agent, Agent.id == Assignment.agent_id)
                .where(AssignmentRun.created_at >= previous_start)
                .group_by(
                    month_expr,
                    Assignment.id,
                    Assignment.name,
                    Agent.is_builtin,
                    Agent.template_slug,
                )
            )
        ).all()

        slugs = {row.template_slug for row in rows if row.template_slug}
        default_names: dict[str, set[str]] = {}
        if slugs:
            template_rows = (
                await db.execute(
                    select(AgentTemplate.slug, AgentTemplate.default_assignments).where(
                        AgentTemplate.slug.in_(sorted(slugs))
                    )
                )
            ).all()
            for template in template_rows:
                names: set[str] = set()
                for entry in template.default_assignments or []:
                    if not isinstance(entry, Mapping):
                        continue
                    name = default_assignment_text(
                        template.slug, entry, "name", "Untitled Assignment"
                    )
                    if isinstance(name, str):
                        names.add(name)
                default_names[template.slug] = names

        by_month: dict[str, dict[str, int]] = {
            start.strftime("%Y-%m"): dict.fromkeys(_ASSIGNMENT_ORIGINS, 0)
            for start in (previous_start, current_start)
        }
        for row in rows:
            counts = by_month.get(str(row.month))
            if counts is None:  # pragma: no cover - defensive (clock skew)
                continue
            if row.is_builtin:
                origin = "builtin"
            elif row.template_slug and row.name in default_names.get(
                row.template_slug, set()
            ):
                origin = "template_default"
            else:
                origin = "custom"
            counts[origin] += 1

        _state.assignments_ran_by_month = by_month
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry assignments ran by month refresh failed", exc_info=True)


async def _refresh_agentic_executions(db: AsyncSession) -> None:
    """Recompute the agentic-execution metrics from ``agentic_execution_daily``.

    That table is the durable sink for the Redis-buffered execution counters
    (``services/agentic_executions.py``, flushed every ~5 min by the autonomous
    scheduler), so it is at most a few minutes behind — far inside the ~6h
    export cadence.  Reading the table (rather than Redis) keeps this refresh a
    pure DB read like every other step.

    Rows are per (day, source); both metrics sum across sources.  Guarded like
    every other refresh step: a failure preserves the previous snapshot values
    instead of clobbering them with zeros.
    """
    from app.models.agentic_execution import AgenticExecutionDaily

    try:
        total = (
            await db.execute(
                select(func.coalesce(func.sum(AgenticExecutionDaily.count), 0))
            )
        ).scalar()

        window_start = _llm_by_day_window_start().date()
        rows = (
            await db.execute(
                select(
                    AgenticExecutionDaily.day,
                    func.coalesce(func.sum(AgenticExecutionDaily.count), 0).label(
                        "execution_count"
                    ),
                )
                .where(AgenticExecutionDaily.day >= window_start)
                .group_by(AgenticExecutionDaily.day)
                .order_by(AgenticExecutionDaily.day)
            )
        ).all()

        by_day: dict[str, int] = {}
        for row in rows:
            if row.day is None:  # pragma: no cover - defensive
                continue
            by_day[row.day.isoformat()] = int(row.execution_count or 0)

        # Commit the whole snapshot atomically, only after every fallible read
        # succeeded (mirrors the _refresh_llm_analytics contract).
        _state.agentic_executions_count = int(total or 0)
        _state.agentic_executions_by_day = by_day
    except Exception:  # pragma: no cover - defensive
        logger.debug("Telemetry agentic execution refresh failed", exc_info=True)


async def refresh_telemetry_data(db: AsyncSession) -> None:
    """Recompute the in-memory metric snapshot from the database.

    Called once at init and then periodically by the autonomous scheduler.
    Read-only — never commits, rolls back, or flushes the caller's session
    (reads run under ``no_autoflush`` so any pending ORM changes in the shared
    scheduler session are not flushed as a side effect).  Each query is
    individually guarded so a single failure degrades that metric to its
    previous value (via :func:`_apply`) instead of aborting the whole refresh.
    """
    from app.models.agent import Agent
    from app.models.agent_skill import AgentSkill
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.channel_bot import ChannelBot
    from app.models.chat import Conversation, Message
    from app.models.custom_tool import CustomTool
    from app.models.group import Group
    from app.models.integration import Integration
    from app.models.knowledge_base import KnowledgeBase
    from app.models.mcp_server import MCPServer
    from app.models.prompt import Prompt
    from app.models.user import User

    with db.no_autoflush:
        # Edition/licensing is a multi-step resolve; guard it so a licensing or
        # registration-count failure preserves the previous edition snapshot
        # rather than aborting every entity count below.
        try:
            await _refresh_edition(db)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Telemetry edition refresh failed", exc_info=True)
        try:
            from app.services.ai_state import is_ai_enabled

            _state.ai_enabled = 1 if await is_ai_enabled(db) else 0
        except Exception:  # pragma: no cover - defensive
            # Keep the previous snapshot rather than reporting a blip as
            # "AI off" — this number is read as a deployment count.
            logger.debug("Telemetry AI-state refresh failed", exc_info=True)
        await _refresh_llm_providers(db)
        await _refresh_provider_breakdowns(db)
        await _refresh_llm_analytics(db)
        await _refresh_login_analytics(db)
        await _refresh_activity_by_day(db)
        await _refresh_assignments_ran_by_month(db)
        await _refresh_agentic_executions(db)
        await _refresh_agent_format_adoption(db)

        _apply("users_count", await _scalar_count(db, User))
        _apply(
            "active_users_count",
            await _filtered_count(db, User, User.is_active.is_(True)),
        )
        await _refresh_terms_of_use(db)
        _apply("agents_count", await _scalar_count(db, Agent))
        _apply(
            "company_managed_agents_count",
            await _filtered_count(db, Agent, Agent.is_company_managed.is_(True)),
        )
        _apply("assignments_count", await _scalar_count(db, Assignment))
        _apply(
            "active_assignments_count",
            await _filtered_count(db, Assignment, Assignment.enabled.is_(True)),
        )
        _apply("assignment_runs_count", await _scalar_count(db, AssignmentRun))
        _apply("integrations_count", await _scalar_count(db, Integration))
        _apply("knowledge_bases_count", await _scalar_count(db, KnowledgeBase))
        _apply("mcp_servers_count", await _scalar_count(db, MCPServer))
        _apply("custom_tools_count", await _scalar_count(db, CustomTool))
        _apply("channel_bots_count", await _scalar_count(db, ChannelBot))
        _apply("prompts_count", await _scalar_count(db, Prompt))
        _apply("agent_skills_count", await _scalar_count(db, AgentSkill))
        _apply("groups_count", await _scalar_count(db, Group))
        _apply("conversations_count", await _scalar_count(db, Conversation))
        _apply("messages_count", await _scalar_count(db, Message))

    logger.debug(
        "Telemetry snapshot refreshed: edition=%s agentic_source=%s agents=%d "
        "assignments=%d users=%d integrations=%d",
        _state.edition,
        _state.agentic_source or "n/a",
        _state.agents_count,
        _state.assignments_count,
        _state.users_count,
        _state.integrations_count,
    )


# ── OpenTelemetry wiring ─────────────────────────────────────────────────────


def _is_dev_mode() -> bool:
    """True in dev/debug mode.

    Single source of truth for the dev gate: it selects the staging collector
    (:func:`_resolve_endpoint`) and the tight export cadence
    (:func:`_export_interval_ms`) together, so a dev install never pairs the
    staging endpoint with the production 6h cadence (or vice versa).
    """
    return settings.debug or settings.platform_mode == "dev"


def _export_interval_ms() -> int:
    """Export cadence in milliseconds: 3h in dev/debug, 6h in production."""
    return _DEV_EXPORT_INTERVAL_MS if _is_dev_mode() else _SIX_HOURS_MS


# Environment variables set by common CI/CD systems. CI runs spin up ephemeral
# XTM One instances (a fresh PlatformSettings UUID / instance_id per run); since
# the default endpoint is production, they would otherwise pollute production
# adoption metrics (e.g. inflate the distinct-instance count) with non-real
# installs. OpenCTI/OpenAEV avoid this by routing dev/ci to staging — XTM One
# disables telemetry outright in CI.
_CI_ENV_VARS = (
    "CI",
    "CONTINUOUS_INTEGRATION",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "DRONE",
    "BUILDKITE",
    "CIRCLECI",
    "TEAMCITY_VERSION",
    "JENKINS_URL",
    "BITBUCKET_BUILD_NUMBER",
)


def _is_ci_environment() -> bool:
    """Return True when running inside a CI/CD pipeline."""
    return any(os.environ.get(v) for v in _CI_ENV_VARS)


def _resolve_endpoint() -> str:
    """Return the configured OTLP endpoint (staging in dev/debug mode)."""
    if settings.telemetry_endpoint:
        return settings.telemetry_endpoint
    return _STAGING_ENDPOINT if _is_dev_mode() else _PROD_ENDPOINT


async def _probe_endpoint(endpoint: str) -> bool:
    """Return ``True`` when the collector answers the probe POST with HTTP 200.

    Exact parity with the OpenCTI / OpenAEV startup gates: POST an empty JSON
    object (``{}`` with ``Content-Type: application/json``) and require HTTP
    200.  The OTLP/HTTP receiver accepts this and replies 200; a bodiless POST
    without a ``Content-Type`` would instead be rejected with 415 even though
    the collector is healthy (the bug that previously self-disabled telemetry
    on every install).  Any non-200 status / network error disables OTLP export
    for this process (air-gapped or egress-blocked installs).
    """
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.post(endpoint, json={})
        if resp.status_code != 200:
            # Reachable but rejecting the probe — log the status so operators can
            # tell this apart from an unreachable/air-gapped collector below.
            logger.info(
                "[TELEMETRY] Collector at %s rejected the probe (HTTP %s) — "
                "telemetry disabled",
                endpoint,
                resp.status_code,
            )
            return False
        return True
    except Exception as exc:
        logger.info("[TELEMETRY] Collector unreachable (%s) — telemetry disabled", format_exception_message(exc))
        return False


def _register_gauges(meter, state: _TelemetryState) -> None:
    """Register every observable gauge against the in-memory snapshot."""
    from opentelemetry.metrics import CallbackOptions, Observation

    def _single(attr: str, description: str):
        def _cb(_options: CallbackOptions) -> Iterable[Observation]:
            try:
                return [Observation(int(getattr(state, attr)))]
            except Exception:  # pragma: no cover - defensive
                return [Observation(0)]

        meter.create_observable_gauge(
            f"{_PREFIX}{attr}", callbacks=[_cb], description=description, unit="1"
        )

    def _single_float(attr: str, description: str, unit: str = "1"):
        """Float gauge that is simply omitted while its value is ``None``
        (e.g. an average that has no data yet)."""

        def _cb(_options: CallbackOptions) -> Iterable[Observation]:
            try:
                value = getattr(state, attr)
                if value is None:
                    return []
                return [Observation(float(value))]
            except Exception:  # pragma: no cover - defensive
                return []

        meter.create_observable_gauge(
            f"{_PREFIX}{attr}", callbacks=[_cb], description=description, unit=unit
        )

    def _by_day(attr: str, description: str, unit: str = "1"):
        """Multi-dimensional gauge: one observation per day of the rolling
        window, with the UTC ISO date as the ``date`` attribute.

        Values are emitted as-is (the snapshot dicts are already coerced to
        ``int`` or ``float`` by the refresh) so integer series like the token
        counts keep integer typing downstream, consistent with their totals.
        """

        def _cb(_options: CallbackOptions) -> Iterable[Observation]:
            try:
                return [
                    Observation(value, {"date": day})
                    for day, value in getattr(state, attr).items()
                ]
            except Exception:  # pragma: no cover - defensive
                return []

        meter.create_observable_gauge(
            f"{_PREFIX}{attr}", callbacks=[_cb], description=description, unit=unit
        )

    # Edition / licensing
    _single("is_enterprise_edition", "Whether AI is licensed - a validated Filigran license or a connected Enterprise Edition platform (1 = Enterprise, 0 = Community)")
    _single("ai_enabled", "Whether AI is switched on here, resolved from PLATFORM_MODE and the runtime override (1 = on, 0 = off). Sum the zeros to count deployments running without AI")
    _single("registered_platforms_count", "Number of active registered OpenCTI/OpenAEV platforms")
    _single("registered_ee_platforms_count", "Number of active registered platforms with an EE contract")

    # Entity counts
    _single("users_count", "Total number of users")
    _single("active_users_count", "Number of active users")
    _single("terms_accepted", "Whether the platform has accepted the current Terms of Use version (1=accepted, 0=pending or gate inactive)")
    _single("agents_count", "Total number of agents")
    _single("company_managed_agents_count", "Number of company-managed agents")
    _single("assignments_count", "Total number of assignments")
    _single("active_assignments_count", "Number of enabled assignments")
    _single("assignment_runs_count", "Total number of assignment runs (all-time cumulative; per-day activity in assignment_runs_by_day)")
    _single("integrations_count", "Total number of integrations")
    _single("knowledge_bases_count", "Total number of knowledge bases")
    _single("mcp_servers_count", "Total number of MCP servers")
    _single("custom_tools_count", "Total number of custom tools")
    _single("channel_bots_count", "Total number of channel bots")
    _single("prompts_count", "Total number of prompts")
    _single("agent_skills_count", "Total number of agent skills")
    _single("groups_count", "Total number of groups")
    _single("conversations_count", "Total number of conversations (all-time cumulative; per-day activity in conversations_by_day)")
    _single("messages_count", "Total number of chat messages (all-time cumulative; per-day activity in messages_by_day)")
    _single("llm_providers_count", "Total number of configured LLM providers")

    # Per-day activity series — the source for runs/conversations/messages
    # per day (and monthly rollups) in adoption analytics.
    _activity_window = _LLM_BY_DAY_WINDOW_DAYS
    _by_day("assignment_runs_by_day", f"Assignment runs created per day, last {_activity_window} days, keyed by the date attribute")
    _by_day("conversations_by_day", f"Conversations created per day, last {_activity_window} days, keyed by the date attribute")
    _by_day("messages_by_day", f"Chat messages created per day, last {_activity_window} days, keyed by the date attribute")

    # Agentic executions — the volume of agentic work (chat turns, General
    # Assistant turns, assignment runs, sub-agent tasks, extension tasks), one
    # count per logical request regardless of agent fan-out.
    _single("agentic_executions_count", "Total number of agentic executions (all-time cumulative; per-day activity in agentic_executions_by_day)")
    _by_day("agentic_executions_by_day", f"Agentic executions per day, last {_activity_window} days, keyed by the date attribute")

    # Multi-dimensional: distinct assignments that ran per UTC month, by origin.
    def _assignments_ran_by_month_cb(_options: CallbackOptions) -> Iterable[Observation]:
        try:
            return [
                Observation(int(count), {"month": month, "origin": origin})
                for month, by_origin in state.assignments_ran_by_month.items()
                for origin, count in by_origin.items()
            ]
        except Exception:  # pragma: no cover - defensive
            return []

    meter.create_observable_gauge(
        f"{_PREFIX}assignments_ran_by_month",
        callbacks=[_assignments_ran_by_month_cb],
        description=(
            "Distinct assignments with at least one run created in the UTC month "
            "(current and previous), keyed by the month and origin attributes "
            "(builtin, template_default, custom)"
        ),
        unit="1",
    )

    # Multi-dimensional: LLM providers by provider type.
    def _llm_by_type_cb(_options: CallbackOptions) -> Iterable[Observation]:
        try:
            return [
                Observation(int(count), {"provider": provider})
                for provider, count in state.llm_providers_by_type.items()
            ]
        except Exception:  # pragma: no cover - defensive
            return []

    meter.create_observable_gauge(
        f"{_PREFIX}llm_providers_by_type",
        callbacks=[_llm_by_type_cb],
        description="Configured LLM providers, broken down by provider type",
        unit="1",
    )

    # Multi-dimensional: integrations / channel bots by catalog provider.
    def _by_provider(attr: str, description: str):
        def _cb(_options: CallbackOptions) -> Iterable[Observation]:
            try:
                return [
                    Observation(int(count), {"provider": provider})
                    for provider, count in getattr(state, attr).items()
                ]
            except Exception:  # pragma: no cover - defensive
                return []

        meter.create_observable_gauge(
            f"{_PREFIX}{attr}", callbacks=[_cb], description=description, unit="1"
        )

    _by_provider(
        "integrations_by_provider",
        "Configured integrations, broken down by catalog provider (github, slack, opencti, ...)",
    )
    _by_provider(
        "channel_bots_by_provider",
        "Configured channel bots, broken down by messaging platform provider",
    )

    # Multi-dimensional: agent export/import adoption by portable format.
    def _by_format(attr: str, description: str):
        def _cb(_options: CallbackOptions) -> Iterable[Observation]:
            try:
                return [
                    Observation(int(count), {"format": format_id})
                    for format_id, count in getattr(state, attr).items()
                ]
            except Exception:  # pragma: no cover - defensive
                return []

        meter.create_observable_gauge(
            f"{_PREFIX}{attr}", callbacks=[_cb], description=description, unit="1"
        )

    _by_format(
        "agent_exports_by_format",
        "Agent exports from the audit trail, broken down by target format "
        "(native, agentschema)",
    )
    _by_format(
        "agent_imports_by_format",
        "Successful agent imports from the audit trail, broken down by the "
        "auto-detected source format (native, agentschema)",
    )
    _single(
        "agent_imports_rejected_count",
        "Agent import uploads rejected by format detection or schema validation "
        "before anything was created",
    )

    # LLM analytics aggregates (local trace store) — totals + per-day series.
    _window = _LLM_BY_DAY_WINDOW_DAYS
    _single_float("llm_total_cost", "Total LLM cost across retained traces (USD)", unit="USD")
    _single("llm_total_tokens", "Total LLM tokens across retained traces")
    _single_float("llm_avg_latency_ms", "Average LLM trace latency across retained traces", unit="ms")
    _single_float("llm_avg_feedback", "Average user feedback score across retained traces")
    _by_day("llm_total_cost_by_day", f"LLM cost per day (USD), last {_window} days, keyed by the date attribute", unit="USD")
    _by_day("llm_total_tokens_by_day", f"LLM tokens per day, last {_window} days, keyed by the date attribute")
    _by_day("llm_avg_latency_ms_by_day", f"Average LLM trace latency per day, last {_window} days, keyed by the date attribute", unit="ms")
    _by_day("llm_avg_feedback_by_day", f"Average user feedback score per day, last {_window} days, keyed by the date attribute")

    # Login activity (audit trail) — cumulative total + per-day series.
    _single("user_login_count", "Total number of user logins (all-time, from the audit trail; local, OIDC, and SAML)")
    _by_day("logins_by_day", f"User logins per day, last {_window} days, keyed by the date attribute")

    # Info gauge: always 1, carrying the dynamic string dimensions as labels
    # so analytics can slice by edition / agentic source / platform mode.
    def _info_cb(_options: CallbackOptions) -> Iterable[Observation]:
        return [
            Observation(
                1,
                {
                    "platform_mode": state.platform_mode,
                    "agentic_source": state.agentic_source or "none",
                    "edition": state.edition,
                    # The resolved state, not the mode: see ``ai_enabled`` on
                    # the state class.
                    "ai_enabled": "true" if state.ai_enabled else "false",
                },
            )
        ]

    meter.create_observable_gauge(
        f"{_PREFIX}platform_info",
        callbacks=[_info_cb],
        description="Platform info (mode, agentic source, edition, AI state) as label dimensions",
        unit="1",
    )


def _recording_exporter(inner):
    """Wrap an exporter so the outcome of its last export can be read back.

    The SDK cannot tell: ``PeriodicExportingMetricReader.force_flush`` returns
    ``True`` whatever happened, and an exception inside ``export`` is logged
    and swallowed by the reader. ``last_result`` is ``None`` until an export
    completes, then the exporter's own ``MetricExportResult``.
    """
    from opentelemetry.sdk.metrics.export import MetricExporter

    class _Recording(MetricExporter):
        def __init__(self) -> None:
            super().__init__(
                preferred_temporality=inner._preferred_temporality,
                preferred_aggregation=inner._preferred_aggregation,
            )
            self.last_result = None

        def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs):
            self.last_result = inner.export(metrics_data, timeout_millis=timeout_millis, **kwargs)
            return self.last_result

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            return inner.force_flush(timeout_millis=timeout_millis)

        def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
            inner.shutdown(timeout_millis=timeout_millis, **kwargs)

    return _Recording()


def _new_reader(exporter):
    """The pipeline's reader: it exports only when told to.

    ``export_interval_millis=math.inf`` starts no background thread, so a
    process never exports on a timer of its own, nor at shutdown (the
    periodic thread's final collect). :func:`export_telemetry_if_due` is the
    only path that exports, in the one process holding the install's lease.
    """
    import math

    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    return PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=math.inf,
        export_timeout_millis=_EXPORT_TIMEOUT_MS,
    )


async def export_telemetry_if_due(db: AsyncSession) -> bool:
    """Export the install's telemetry if this interval's export has not happened.

    Called by every scheduler tick (itself once a minute per install). The first
    process to take the Redis window lease exports: it refreshes the snapshot
    from the database, then collects and exports. Everyone else, and every
    later tick of the interval, finds the lease held and does nothing. The
    lease is never released: it expires after the export interval, so a
    process that dies holding it hands over at the next interval.

    Redis unavailable: the export is SKIPPED, never done from every process.
    Exporting everywhere is the duplication this exists to stop, while a skipped
    export loses nothing - every value is a cumulative or rolling-window
    snapshot, so the next export carries the same information. The scheduler
    tick itself needs the same Redis to run at all. Never raises.

    Returns whether this call exported.
    """
    import asyncio

    if not is_telemetry_active():
        return False
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        acquired = await redis.set(
            _EXPORT_LEASE_KEY,
            "1",
            nx=True,
            ex=max(_export_interval_ms() // 1000, 1),
        )
    except Exception as exc:
        logger.warning("[TELEMETRY] Export lease unavailable (%s) - export skipped", format_exception_message(exc))
        return False
    if not acquired:
        return False
    provider, recorder = _provider, _recorder
    if provider is None or recorder is None:
        return False
    try:
        from opentelemetry.sdk.metrics.export import MetricExportResult

        await refresh_telemetry_data(db)
        recorder.last_result = None
        flushed = await asyncio.to_thread(provider.force_flush, _EXPORT_TIMEOUT_MS)
    except Exception:
        logger.warning("[TELEMETRY] Export failed", exc_info=True)
        return False
    if not flushed or recorder.last_result is not MetricExportResult.SUCCESS:
        # The lease stays held: the next attempt is the next interval, like a
        # failed periodic export before - retrying every tick would hammer a
        # collector that is down.
        logger.warning(
            "[TELEMETRY] Export did not complete (%s); next attempt in one export interval",
            "flush timed out" if not flushed else f"exporter result: {recorder.last_result}",
        )
        return False
    logger.info("[TELEMETRY] Exported the usage metrics for this install")
    return True


def _build_resource_attributes(instance_id: str, creation: str) -> dict[str, str]:
    """Build the OTLP resource attributes attached to every metric export.

    Pure (settings + arguments in, dict out) so the ``TELEMETRY_TAGS``
    contract is unit-testable even though :func:`init_telemetry`
    short-circuits under the test runner: the normalized tags are exported
    as ONE ``filigran.telemetry.tags`` attribute — the analytics warehouse
    lands it as a ``tags`` column on every metric row — and the attribute is
    omitted entirely when no tags are configured.
    """
    from opentelemetry.sdk.resources import (
        SERVICE_INSTANCE_ID,
        SERVICE_NAME,
        SERVICE_VERSION,
    )

    attributes = {
        SERVICE_NAME: TELEMETRY_SERVICE_NAME,
        SERVICE_VERSION: settings.app_version,
        SERVICE_INSTANCE_ID: instance_id,
        "service.instance.creation": creation,
        "xtm.platform_mode": settings.platform_mode,
    }
    telemetry_tags = settings.telemetry_tags_normalized
    if telemetry_tags:
        attributes["filigran.telemetry.tags"] = telemetry_tags
    return attributes


async def init_telemetry(db: AsyncSession) -> None:
    """Initialise the telemetry pipeline (called once at app startup).

    Does nothing when ``TELEMETRY_ENABLED`` is false.  Otherwise resolves the
    endpoint, probes connectivity, builds the OpenTelemetry
    MeterProvider with an OTLP/HTTP exporter and a periodic reader, registers
    the observable gauges, and seeds the first metric snapshot.  Fully
    defensive: any failure leaves telemetry disabled without affecting startup.
    """
    global _provider, _recorder, _initialized

    if _initialized:
        return

    # The admin's switch wins over everything, before any network call.
    if not settings.telemetry_enabled:
        logger.info("[TELEMETRY] Disabled by TELEMETRY_ENABLED=false - nothing is sent")
        return

    # Never start telemetry threads / network calls under the test runner.
    if "pytest" in sys.modules:
        return

    # Never emit telemetry from CI/CD: ephemeral instances (fresh instance_id
    # per run) would pollute production adoption metrics with non-real installs.
    if _is_ci_environment():
        logger.info("[TELEMETRY] CI environment detected - telemetry disabled")
        return

    endpoint = _resolve_endpoint()
    if not await _probe_endpoint(endpoint):
        return

    provider = None
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import (
            Counter,
            Histogram,
            MeterProvider,
            ObservableCounter,
            ObservableGauge,
            ObservableUpDownCounter,
            UpDownCounter,
        )
        from opentelemetry.sdk.metrics.export import AggregationTemporality
        from opentelemetry.sdk.resources import Resource

        from app.models.platform_settings import PlatformSettings

        # Read-only load (do not create the row): telemetry must not have a
        # write side effect.  The singleton is created elsewhere at startup; if
        # it is somehow absent we fall back to an "unknown" instance id.
        ps_result = await db.execute(select(PlatformSettings).limit(1))
        ps = ps_result.scalar_one_or_none()
        instance_id = str(ps.id) if ps else "unknown"
        creation = ps.created_at.isoformat() if ps and ps.created_at else ""

        # Includes the optional TELEMETRY_TAGS deployment tags — see
        # ``_build_resource_attributes`` for the tags contract.
        resource = Resource.create(
            _build_resource_attributes(instance_id, creation)
        )

        # DELTA temporality across the board — matches the Filigran collector
        # configured for OpenCTI/OpenAEV.
        delta = AggregationTemporality.DELTA
        preferred_temporality = {
            Counter: delta,
            UpDownCounter: delta,
            Histogram: delta,
            ObservableCounter: delta,
            ObservableUpDownCounter: delta,
            ObservableGauge: delta,
        }

        exporter = OTLPMetricExporter(
            endpoint=endpoint,
            timeout=_EXPORT_TIMEOUT_MS // 1000,
            preferred_temporality=preferred_temporality,
        )
        export_interval_ms = _export_interval_ms()
        recorder = _recording_exporter(exporter)
        provider = MeterProvider(resource=resource, metric_readers=[_new_reader(recorder)])
        meter = provider.get_meter(TELEMETRY_SERVICE_NAME)

        _register_gauges(meter, _state)

        # Seed the snapshot so the first export carries real values.
        await refresh_telemetry_data(db)

        _provider = provider
        _recorder = recorder
        _initialized = True
        logger.info(
            "[TELEMETRY] Initialised — anonymous usage metrics go to %s every %d "
            "minutes, from one process per install",
            endpoint,
            export_interval_ms // _ONE_MINUTE_MS,
        )
    except Exception:
        logger.warning("[TELEMETRY] Initialisation failed — telemetry disabled", exc_info=True)
        # If the failure happened after the provider/reader was built (e.g. in
        # the initial refresh), shut it down so its exporter is released.
        if provider is not None:
            try:
                import asyncio

                await asyncio.to_thread(provider.shutdown)
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    "[TELEMETRY] Provider cleanup after init failure failed",
                    exc_info=True,
                )
        _provider = None
        _recorder = None
        _initialized = False


async def shutdown_telemetry() -> None:
    """Flush and tear down the telemetry pipeline at app shutdown."""
    global _provider, _recorder, _initialized
    if _provider is None:
        return
    try:
        import asyncio

        # The reader has no timer (``_new_reader``), so shutting down exports
        # nothing: a worker recycle or a rollout no longer adds an export per
        # process. Off the event loop all the same, it closes the exporter.
        await asyncio.to_thread(_provider.shutdown)
        logger.info("[TELEMETRY] Shut down")
    except Exception:  # pragma: no cover - defensive
        logger.debug("[TELEMETRY] Shutdown error", exc_info=True)
    finally:
        _provider = None
        _recorder = None
        _initialized = False


def is_telemetry_active() -> bool:
    """Return whether the telemetry pipeline is initialised and exporting."""
    return _initialized and _provider is not None
