# SPDX-License-Identifier: Apache-2.0

"""XTM One – FastAPI application entry point."""

import asyncio
import functools
import json
import logging
import re
import time
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from html import escape as html_escape
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import structlog
from fastapi import Depends, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from scalar_fastapi import Layout, Theme, get_scalar_api_reference
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from starlette.responses import Response as StarletteResponse
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.a2a import grants as a2a_grants
from app.a2a import hooks as a2a_hooks
from app.a2a import ticket_push as a2a_ticket_push
from app.api import auth_sso, oauth_callbacks
from app.api.deps import CodedHTTPException
from app.api.v1 import (
    a2a_admin,
    a2a_callbacks,
    a2a_client,
    a2a_liaisons,
    a2a_server,
    activity,
    admin_objects,
    agent_accounts,
    agents,
    ai,
    ai_adoption,
    annotations,
    api_keys,
    app_lock,
    arcade,
    assignments,
    audit_logs,
    auth,
    browser_profiles,
    channel_bots,
    chat,
    chat_commands,
    chat_continuation,
    chat_export,
    chat_web_stream,
    csp_reports,
    ctem_cases,
    ctem_metrics,
    ctem_signals,
    custom_dashboards,
    custom_tool_sources,
    custom_tools,
    dashboard_public,
    dashboard_widget_packs,
    datasets,
    decisions,
    desktop_login,
    email_signatures,
    evaluations,
    extension,
    feedback_criteria,
    files,
    fleet_member_presets,
    fleets,
    flow_event_types,
    flow_runs,
    flows,
    groups,
    hitl_whitelist,
    hooks,
    improvement_proposals,
    insights,
    integration_elements,
    integration_exposure,
    integration_github_webhooks,
    integration_notifications,
    integration_openapi,
    integrations,
    intents,
    investigation_packs,
    investigations,
    invitations,
    knowledge_bases,
    learning,
    llm_proxy,
    lmstudio,
    mcp_endpoint,
    mcp_oauth,
    mcp_platform,
    mcp_servers,
    mcp_xtm,
    metrics,
    mobile_devices,
    model_announcements,
    notifications,
    nova_rules,
    opencrq,
    openrouter,
    organization_context,
    organizations,
    panic,
    platform,
    platform_access_notices,
    platform_chat_workspaces,
    platform_health,
    platform_health_analysis,
    platform_health_companion,
    platform_health_cycles,
    platform_health_findings,
    presentation_public,
    presentations,
    prompts,
    public_catalog,
    public_chat,
    relays,
    reports,
    resource_exposure,
    roi,
    runners,
    scim,
    search,
    secret_vaults,
    security_audit,
    shared_state,
    supabase_console,
    tasks,
    template_containers,
    tool_approvals,
    traces,
    users,
    variables,
    video_public,
    video_studio,
    webhooks,
    whatsapp_gateway_pairing,
    work_items,
    worksets,
    workspace_knowledge,
    workspaces,
    xtm_auth,
    xtm_hub_registration,
)
from app.api.v1 import settings as settings_api
from app.api.v1.agent_chats import router as agent_chat_router
from app.api.v1.agent_memory import router as agent_memory_router
from app.api.v1.agent_skills import router as agent_skill_router
from app.api.v1.agent_templates import template_router as agent_template_router
from app.api.v1.file_retention import router as file_retention_router
from app.api.v1.memory_consolidations import agent_router as memory_consolidations_agent_router
from app.api.v1.memory_consolidations import me_router as memory_consolidations_me_router
from app.channels import discord as discord_channel
from app.channels import google_chat as googlechat_channel
from app.channels import slack as slack_channel
from app.channels import teams as teams_channel
from app.channels import telegram as telegram_channel
from app.channels import whatsapp as whatsapp_channel
from app.core.base_path import BasePathMiddleware, route_path, with_base_path
from app.core.config import settings
from app.core.csp import (
    CSP_REPORT_PATH,
    GOOGLE_FONTS_SOURCES,
    ContentSecurityPolicyMiddleware,
    PagePolicy,
    inline_script_hashes,
    register_page_policy,
)
from app.core.db_availability import install_database_unavailable_handler
from app.core.logging import setup_logging
from app.core.lone_surrogates import escape_surrogates
from app.core.shutdown_finalizers import drain_shutdown_finalizers, track_request_task
from app.core.utils import (
    client_address,
    first_forwarded_hop,
    format_exception_message,
    is_graceful_shutdown_cancellation,
)
from app.core.worker_exits import register_worker_and_report_lost, unregister_worker
from app.core.worker_recycle import log_worker_shutdown_summary, recycler, start_recycle_watch
from app.core.worker_shutdown import background_grace_deadline, install_stop_signal_hook
from app.services.branding import (
    configure_branded_index_html,
    get_branded_index_html,
    get_branded_index_script_hashes,
    get_cached_branding,
    refresh_configured_branded_index_html,
)
from app.services.csp_reports import violation_log as csp_violation_log
from app.services.registered_platform_origins import (
    refresh_registered_platform_origins,
    registered_platform_origins,
)
from app.services.vault.errors import SecretUnavailableError

if TYPE_CHECKING:
    from app.llm.env_listing import SeedListing

setup_logging(
    debug=settings.debug, log_level=settings.log_level, log_format=settings.log_format
)
logger = logging.getLogger(__name__)

# Must run after ``setup_logging()`` above — ``settings`` is
# constructed at import time (before logging is configured), so logging this
# from the Settings validator itself would silently be dropped.
if settings.enabled_feature_flags:
    from app.core.feature_flags import resolve_enabled_feature_flags

    # The flags in effect; no line when none is (AGENTIC_CODING alone in XTM One mode).
    if _in_effect := resolve_enabled_feature_flags():
        logger.info("Feature flags enabled: %s", _in_effect)

# Loud, and at WARNING, because this one widens a security boundary rather
# than tuning a limit: these origins are reachable over plain http and at
# addresses the outbound destination guard otherwise refuses. Logged at boot,
# unconditionally when set — a deployment that is running with this on should
# say so on every boot, not only in development.
if settings.a2a_client_dev_trusted_origins:
    logger.warning(
        "A2A outbound destination guard relaxed for %s — development only, and "
        "every other destination is still refused. Unset "
        "A2A_CLIENT_DEV_TRUSTED_ORIGINS outside local testing.",
        ", ".join(settings.a2a_client_dev_trusted_origins),
    )
# Same reasoning for the inbound half: plain http from these peers passes the
# A2A surface's HTTPS guarantee, so a deployment running with it says so.
if settings.test_only_insecure_transport_peers:
    logger.warning(
        "Plain http from %s counts as encrypted on the A2A surface - end-to-end "
        "tests only. Unset TEST_ONLY_INSECURE_TRANSPORT_PEERS outside them.",
        ", ".join(settings.test_only_insecure_transport_peers),
    )

# Same footgun, same treatment: under the strict token URL checks a listed
# origin's token URL may resolve to a loopback address, which they otherwise
# refuse. Read before the Settings > Access and security snapshot is, so only a variable
# that turns the strict checks off proves the list changes nothing; while it is
# unset, the checks may be on from Settings > Access and security.
if settings.oauth_token_dev_trusted_origins:
    from app.core.security_options import is_environment_locked

    if is_environment_locked("oauth_token_url_strict") and not settings.oauth_token_url_strict:
        logger.info(
            "OAUTH_TOKEN_DEV_TRUSTED_ORIGINS (%s) has no effect: "
            "OAUTH_TOKEN_URL_STRICT=false accepts loopback token URLs for every origin.",
            ", ".join(settings.oauth_token_dev_trusted_origins),
        )
    else:
        logger.warning(
            "OAuth token endpoint guard relaxed for %s under the strict token URL "
            "checks - loopback token URLs accepted for these origins, development "
            "only. Unset OAUTH_TOKEN_DEV_TRUSTED_ORIGINS outside local testing.",
            ", ".join(settings.oauth_token_dev_trusted_origins),
        )

ROBOTS_DIRECTIVES = "noindex, nofollow, noarchive, nosnippet, noimageindex"

# ---------------------------------------------------------------------------
# Startup gate
#
# Background startup (migrations, seeding, scheduler) runs AFTER the HTTP
# server has started so liveness probes never stall behind a long DDL
# operation (e.g. HNSW index build).  The variables below track state
# across the lifespan and the middleware / readiness probe.
#
# ``_startup_done``   – cleared at lifespan entry, set when
#                       ``_run_startup_tasks`` completes successfully.
# ``_startup_settled``– cleared at lifespan entry, set once startup is done,
#                       has failed or was cancelled: what a request held by
#                       the gate waits on.
# ``_startup_error``  – the last error message if startup failed.
# ``_startup_retrying`` – the transient database error startup is waiting out.
# ``_scheduler_task`` – asyncio.Task for the assignment scheduler so the
#                       shutdown path can cancel it even when startup ran in
#                       the background.
#
# A worker is also started while its pod is already serving: uvicorn replaces
# a worker recycled after MAX_REQUESTS, and a pod turns Ready on the first
# worker that answers the readiness probe. The load balancer keeps sending
# that worker its share of the traffic, so a request reaching it mid-startup
# is held up to ``STARTUP_HOLD_SECONDS`` rather than refused at once; a
# replacement worker skips the migrations and the seeding its launch already
# applied (``app/core/launch_marker.py``), so the hold lasts about a second.
# ---------------------------------------------------------------------------
_startup_done: asyncio.Event = asyncio.Event()
_startup_settled: asyncio.Event = asyncio.Event()
# The gate defaults to OPEN at import time so consumers that exercise the
# app or the middleware WITHOUT running the lifespan (unit tests, scripts,
# ad-hoc TestClient usage) keep normal behaviour.  There is no production
# window: the lifespan clears the event at entry, which always happens
# BEFORE uvicorn starts accepting connections.
_startup_done.set()
_startup_settled.set()
# Under the 10 s a GitHub webhook delivery waits (it is never redelivered)
# and well under the usual 60 s proxy read timeout.
STARTUP_HOLD_SECONDS = 8.0
_startup_error: str | None = None
_startup_retrying: str | None = None
_scheduler_task: asyncio.Task | None = None
# The sweep resuming the chat turns a platform stop cut: every
# worker, from its start, on its own short cadence.
_cut_turn_sweep_task: asyncio.Task | None = None
# Dispatches a work item to its inbox when it becomes dispatchable: every
# worker listens, one pass at a time across them, the scheduler tick as backstop.
_work_item_wake_task: asyncio.Task | None = None
_ann_index_task: asyncio.Task | None = None
_a2a_reaper_task: asyncio.Task | None = None
_a2a_client_sweep_task: asyncio.Task | None = None
_a2a_owed_cancel_task: asyncio.Task | None = None
_branding_refresh_task: asyncio.Task | None = None
_security_options_refresh_task: asyncio.Task | None = None


def _seed_langfuse_otel_exporter_defaults() -> None:
    """Seed OTEL exporter batch defaults before any Langfuse client is created."""
    from app.services.langfuse_tracing import _seed_otel_exporter_limits_from_env

    _seed_otel_exporter_limits_from_env()


async def _wait_for_startup() -> bool:
    """Whether this worker may serve a request, holding it while startup runs.

    Called only while ``_startup_done`` is unset: a startup that already
    failed answers at once, a running one gets up to ``STARTUP_HOLD_SECONDS``.
    """
    if not _startup_settled.is_set():
        with suppress(TimeoutError):
            await asyncio.wait_for(_startup_settled.wait(), STARTUP_HOLD_SECONDS)
    return _startup_done.is_set()


async def _autonomous_loop() -> None:
    """Background loop that runs the assignment scheduler.

    Uses a Redis lock so that the tick executes at most once per ~60s
    across ALL uvicorn workers and replicas.  The lock is deliberately
    NOT released after the tick: it expires via its 55s TTL, so the key
    doubles as a "tick already ran in this window" marker.  Releasing
    it early (the previous compare-and-delete design) only prevented
    *overlapping* ticks — a typical tick finishes in well under a
    second, so every other worker's loop still won the lock inside the
    same 60s window and the full tick body ran once per worker per
    minute fleet-wide (N workers x M pods), multiplying per-tick log
    warnings and integration event polling.

    A tick that overruns the 55s TTL simply loses the lock to expiry and
    the next worker may start a fresh tick alongside it. Its sweeps are
    idempotent, except a per-user fan-out still in progress, which a
    per-assignment claim keeps to one tick (``_fanout_claim``).

    A tick that RAISES also keeps the window marker: the failed window
    is not retried by another worker (deliberate — every sweep inside
    the tick already has its own try/except, so an exception here is an
    infrastructure-level failure that an immediate retry from a sibling
    worker would only multiply).  The error is logged below and the
    next window's winner runs a fresh tick, so a persistent failure
    surfaces as one ``Assignment scheduler error`` log line per ~60s
    window rather than silence.
    """
    from app.core.redis import redis_client
    from app.services.autonomous import run_autonomous_tick

    while True:
        try:
            redis = await redis_client()
            # Acquire the window lock (55s TTL < 60s sleep, so each
            # worker's next attempt lands after expiry).  Only the first
            # worker to wake after expiry wins; losers skip the window.
            # redis-py type stubs currently expose set() as Awaitable[bool] | bool
            # even on the asyncio client (see redis-py issue #3920).
            acquired = await cast(
                Awaitable[bool],
                redis.set(
                    "copilot:scheduler_tick_lock",
                    "1",
                    nx=True,
                    ex=55,
                ),
            )
            if acquired:
                executed = await run_autonomous_tick()
                if executed:
                    logger.info(
                        "Assignment scheduler tick: %d assignments triggered",
                        executed,
                    )
        except Exception as e:
            logger.error(
                "Assignment scheduler error: %s",
                format_exception_message(e),
                exc_info=True,
            )
        await asyncio.sleep(60)


async def _a2a_client_sweep_loop() -> None:
    """Re-verify partner cards on a schedule.

    The LOOP runs unconditionally: ``sweep_once`` itself asks whether chat is
    enabled each cycle, so a licence or AI toggle that moves at runtime takes
    effect without a restart, and no outbound request leaves while A2A is off.
    A partner cancel a crash interrupted is teardown and goes out regardless,
    on its own cadence (``_a2a_owed_cancel_loop``).

    Best-effort by design. This is an optimisation, not the binding control —
    a delegation re-verifies for itself once its verdict is stale — so a
    failed cycle costs freshness on the integrations page, never correctness.
    """
    from app.a2a.client.sweep import collect_overdue, sweep_once
    from app.core.config import settings as app_settings
    from app.core.database import async_session

    interval = app_settings.a2a_client_sweep_interval_seconds
    while True:
        try:
            async with async_session() as db:
                verified = await sweep_once(db)
                # The floor beneath a registered callback: a partner that
                # stopped trying, or a calling agent that never collected,
                # would otherwise leave the delegation outstanding forever.
                collected = await collect_overdue(db)
                if collected:
                    logger.info(
                        "A2A client: collected %d outstanding partner task(s) "
                        "nobody had asked about",
                        collected,
                    )
            if verified:
                logger.info("A2A client sweep: re-verified %d partner card(s)", verified)
        except Exception as e:
            logger.error("A2A client sweep error: %s", e, exc_info=True)
        await asyncio.sleep(interval)


async def _a2a_converge_exposures_once() -> None:
    """Expose, once per boot, every agent bound to an inbound A2A integration
    with no live exposure.

    The flush listener only acts when an agent changes; a binding made while
    its integration had no direction row (backfilled by a migration later)
    never woke it (#11786). Additive, never revokes; a failure costs only the
    card URL until the next boot or settings change, so it logs and returns.
    """
    from app.a2a.hooks import converge_exposures
    from app.core.database import async_session

    try:
        async with async_session() as db:
            exposed = await converge_exposures(db)
            await db.commit()
        if exposed:
            logger.info("A2A: exposed %d bound agent(s) that had no live exposure", exposed)
    except Exception as e:
        logger.error("A2A exposure converge error: %s", e, exc_info=True)


async def _a2a_owed_cancel_loop() -> None:
    """Re-send the partner cancels a hard crash interrupted.

    A loop of its own, on a FIXED cadence of
    ``a2a_client_sweep_interval_seconds``: each cycle starts one interval
    after the previous one STARTED, so neither card re-verification and the
    floor (``_a2a_client_sweep_loop``, whose network work can run long) nor
    the re-send's own partner calls push the next check back. The first cycle
    runs at startup, which is when a cancel lost to a crash is found.

    Together with ``reissue_owed_cancels`` draining the backlog each cycle,
    that is what makes the documented bound true: an owed cancel reaches its
    partner at most one delegation ceiling plus one interval after the
    decision. With nothing owed, a cycle is one read of the delegations,
    unindexed like the floor's in the card sweep's cycle.

    Runs whether or not A2A is offered: this is teardown, like the fast path.
    """
    from app.a2a.client.sweep import reissue_owed_cancels
    from app.core.config import settings as app_settings
    from app.core.database import async_session

    interval = app_settings.a2a_client_sweep_interval_seconds
    loop = asyncio.get_running_loop()
    next_start = loop.time()
    while True:
        try:
            async with async_session() as db:
                reissued = await reissue_owed_cancels(db)
            if reissued:
                logger.info(
                    "A2A client: re-sent %d partner cancel(s) a restart had "
                    "interrupted",
                    reissued,
                )
        except Exception as e:
            logger.error(
                "A2A client owed-cancel re-send error: %s",
                format_exception_message(e),
                exc_info=True,
            )
        # A cycle that ran past its tick starts the next one right away and
        # re-anchors, rather than firing a burst to catch up.
        next_start = max(next_start + interval, loop.time())
        await asyncio.sleep(next_start - loop.time())


def _render_branding() -> None:
    """Re-render what this process derives from the cached branding."""
    _refresh_openapi_branding()
    _refresh_index_html()


async def _branding_refresh_loop() -> None:
    """Follow an admin branding change made through another process.

    ``get_cached_branding()`` is per process, and the admin save refreshes only
    the process that served it: a sibling uvicorn worker or another replica
    kept the old platform name (e-mails, MFA issuer, system prompts, the
    patched index.html, the OpenAPI title) until it restarted.
    """
    from app.services.branding import keep_branding_current

    await keep_branding_current(render=_render_branding)


async def _a2a_reaper_loop() -> None:
    """Background loop that drives abandoned external A2A runs terminal.

    Enforces ``a2a_run_lease_seconds``: a run whose replica died mid-execution
    leaves its task pollable forever unless something reaps it, which the A2A
    server specification forbids outright ("100% of externally submitted
    tasks reach a terminal state").

    Unlike ``_autonomous_loop`` there is no Redis window lock: the reap
    claims rows with ``FOR UPDATE SKIP LOCKED``, so concurrent sweeps on
    other workers/replicas take disjoint work, and an empty sweep is one
    indexed SELECT — running it on every worker is harmless. It also runs
    whether or not A2A is offered: with the surface unused the sweep finds
    nothing, and chat can go away at runtime with tasks still in flight.

    The commit is load-bearing: ``reap_stale_tasks`` only flushes, so
    closing the session without committing would roll the reap back. The
    announcements come after it, for the same reason.
    """
    from app.a2a.client.delegation import spawn_run_cancel
    from app.a2a.tasks import announce_missed, announce_state, reap_stale_tasks
    from app.core.database import async_session

    while True:
        try:
            async with async_session() as db:
                tasks: list = []
                cancelled_runs: list = []
                reaped = await reap_stale_tasks(
                    db, reaped_out=tasks, cancelled_out=cancelled_runs
                )
                await db.commit()
                spawn_run_cancel(cancelled_runs)
                for task in tasks:
                    await announce_state(db, task)
                # And the transition nothing was executing to notice: a task
                # whose delegated work has since finished.
                missed = await announce_missed(db)
            if reaped:
                logger.info(
                    "A2A reaper: drove %d abandoned external task(s) to a "
                    "terminal state",
                    reaped,
                )
            if missed:
                logger.info(
                    "A2A reaper: announced %d terminal task(s) whose "
                    "transition nothing was executing to see",
                    missed,
                )
        except Exception as e:
            logger.error(
                "A2A reaper error: %s",
                format_exception_message(e),
                exc_info=True,
            )
        await asyncio.sleep(settings.a2a_reap_interval_seconds)


def _env_pinned_model_owner(configs: list, model: str):
    """The config an env-pinned model should route through, or ``None``.

    Prefer an env-seeded row so an env-pinned model uses env-seeded credentials;
    fall back to any configured provider. The result is stored as the model's
    provider reference — without one, resolution falls back to
    newest-config-first and a third config flips the credentials.
    """
    from app.llm.router import _resolve_model_owner_config

    env_seeded = [c for c in configs if c.is_from_env]
    return _resolve_model_owner_config(
        env_seeded, model
    ) or _resolve_model_owner_config(configs, model)


def _load_dotenv_files() -> None:
    """Load the ``.env`` files into ``os.environ`` without overriding it.

    pydantic-settings' ``env_file`` only populates declared model fields
    and silently drops unknown keys when ``extra="ignore"``.  Dynamic
    keys like ``LLM_PROVIDER_*_API_KEY`` therefore never reach
    ``os.environ``.  Explicitly load the same ``.env`` files so the
    provider seed's ``os.environ`` scan (and the later readers of those
    keys, e.g. ``env_models_for``) can discover them.
    """
    from app.core.config import load_dotenv_files

    load_dotenv_files()


async def _seed_listed_models(providers: list[tuple[str, str]], models_editable: bool) -> dict[str, "SeedListing"]:
    """List the models of the seeded providers whose catalog is their listing.

    Which ones: ``app.llm.env_listing.catalogs_to_list``. Done before the
    seed's transaction so no network call holds the provider lock.
    """
    from sqlalchemy import select as _sel

    from app.core.database import async_session
    from app.llm.env_listing import SEED_LISTED_PROVIDERS, catalogs_to_list, list_models_for_seed
    from app.llm.env_models import env_models_for
    from app.models.llm_config import LLMProviderConfig

    if not any(n in SEED_LISTED_PROVIDERS and env_models_for(n) is None for n, _ in providers):
        return {}
    async with async_session() as db:
        rows = (await db.execute(_sel(LLMProviderConfig))).scalars().all()
    names = catalogs_to_list(providers, rows, models_editable)
    listed = {n: await list_models_for_seed(n, k) for n, k in providers if n in names}
    return {n: listing for n, listing in listed.items() if listing}


async def _seed_llm_providers_from_env() -> set[str]:
    """Upsert LLM providers from environment variables on every boot.

    Scans for ``LLM_PROVIDER_{PROVIDER}_API_KEY`` env vars and creates
    or updates ``LLMProviderConfig`` rows.  Existing env-seeded providers
    have their api_key overwritten with the current env value on every
    startup.  ``LLM_PROVIDER_{PROVIDER}_MODELS`` (when set) is either
    *pinned* — ``available_models`` / ``default_model`` overwritten on every
    boot (default) — or, under ``LLM_PROVIDER_MODELS_EDITABLE=true``, a
    *baseline* delta-merged into the admin-curated catalog via
    ``app.llm.env_models.merge_env_models``.  Either way the list
    applied is snapshotted on ``env_models``, and models the seed drops from
    a catalog are cleaned off agents / preferences / platform defaults.
    Unset for the gateway, its own listing stands in (``_seed_listed_models``).

    ``DEFAULT_MODEL``, ``DEFAULT_ROUTING_MODEL``, ``DEFAULT_EMBEDDING_*``,
    ``DEFAULT_IMAGE_*`` and the video, speech, music and decision defaults
    (``app/llm/env_capability_defaults.py``) are applied to
    ``PlatformSettings`` when env vars are present, regardless of whether
    providers were newly seeded — on every
    boot (env wins, which is what locks the Settings controls read-only), or
    ONCE under ``BYOLLM_ACTIVATED=true``: applied only to an install that has
    no LLM configuration of its own yet, so the administrator owns every
    default from the first thing they store and no restart takes a choice
    back — the choice to leave a field empty included (``_env_may_seed``).
    """

    _load_dotenv_files()

    compound_url_providers = {"custom_openai", "litellm", "tei"}

    from sqlalchemy import select as _sel

    from app.api.v1.settings import (
        _cleanup_orphaned_model_references,
        _ensure_platform_default_model,
        _lock_provider_configs,
    )
    from app.core.database import async_session
    from app.core.security import decrypt_stored_value as _dec
    from app.core.security import encrypt_value
    from app.llm import env_capability_defaults
    from app.llm.env_listing import (
        SEED_LISTED_PROVIDERS,
        apply_listing,
        env_provider_keys,
        listing_platform_model,
    )
    from app.llm.env_listing import declared_default as _declared_default
    from app.llm.env_listing import listing_default as _listing_default
    from app.llm.env_listing import replaced_alias as _replaced_alias
    from app.llm.env_models import (
        env_models_for,
        merge_env_models,
        resolve_default_model,
    )
    from app.models.llm_config import LLMProviderConfig

    providers_to_seed = env_provider_keys()

    platform_default_model = settings.default_model
    routing_default_model = settings.default_routing_model.strip() or None
    fallback_default_model = settings.default_fallback_model.strip() or None
    reasoning_default_effort = settings.default_reasoning_effort.strip() or None
    ga_default_model = settings.default_general_assistant_model.strip() or None
    has_platform_overrides = any(
        (
            platform_default_model,
            routing_default_model,
            fallback_default_model,
            reasoning_default_effort,
            ga_default_model,
            settings.default_embedding_provider,
            settings.default_embedding_model,
            settings.default_image_provider,
            settings.default_image_model,
            settings.default_embedding_dimensions is not None,
            env_capability_defaults.env_sets_any(),
        )
    )
    if not providers_to_seed and not has_platform_overrides:
        return set()

    # Resolved: ``LLM_PROVIDER_MODELS_EDITABLE`` or ``BYOLLM_ACTIVATED``.
    models_editable = settings.provider_models_admin_curated
    listed_models = await _seed_listed_models(providers_to_seed, models_editable)

    async with async_session() as db:
        # Also runs when AI is re-enabled at runtime, next to an admin's delete:
        # it edits catalogs and pins defaults to configs, a catalog edit.
        await _lock_provider_configs(db)
        result = await db.execute(
            _sel(LLMProviderConfig).where(LLMProviderConfig.user_id.is_(None))
        )
        existing_by_name = {c.provider: c for c in result.scalars().all()}

        from app.api.v1.platform_settings_helpers import (
            get_platform_settings as _load_ps,
        )

        ps = await _load_ps(db)

        # ── What this install had BEFORE this boot touched anything ──
        # Read here, above the provider loop, because
        # ``_ensure_platform_default_model`` fills empty platform defaults
        # from each provider it seeds (``if not ps.agent_default_model``).
        # Deciding seedability from ``ps`` further down would therefore read
        # a value this same boot had just written and mistake it for an
        # administrator's choice: with two env providers and
        # ``DEFAULT_MODEL`` served by the one seeded second, the first
        # provider's chat model would stick and the operator's baseline
        # would never be applied. Nothing in the predicate is a per-field
        # emptiness test, so no ``DEFAULT_*`` can creep back into one field
        # of an install that is already configured.
        install_configured_before_boot = await _byollm_install_configured(db)

        # provider name -> the row the seed left behind, so the DEFAULT_MODEL
        # pass below can check what each env-seeded provider actually offers.
        seeded_rows: dict[str, LLMProviderConfig] = {}
        # provider name -> the env list, or the provider's own listing, the seed applied.
        seed_lists: dict[str, list[str]] = {}
        for provider_name, api_key in providers_to_seed:
            # ``None`` = ``LLM_PROVIDER_{P}_MODELS`` unset / blank -> the stored
            # catalog is never touched (historical "optional" contract), except
            # by a listing (``_seed_listed_models``).
            env_models = env_models_for(provider_name)
            listing = listed_models.get(provider_name)
            listed = listing.models if listing else None
            # A listing is alphabetical: the provider's own default (the gateway's
            # marked chat model, else its ``default`` alias) when listed, else the
            # stored one, else the first.
            declared_default = _declared_default(provider_name, listing and listing.chat_default, listed or [])
            replaced = _replaced_alias(provider_name, declared_default)
            if listed and not models_editable:
                env_models = listed
            seed_lists[provider_name] = env_models or listed or []

            existing = existing_by_name.get(provider_name)
            if existing:
                # The credential is env-managed in both regimes: env wins.
                existing.api_key_enc = encrypt_value(api_key)
                existing.is_from_env = True
                if env_models:
                    before = set(existing.available_models or [])
                    if models_editable:
                        # Baseline regime: diff the env list against
                        # the snapshot applied at the last boot and fold only
                        # that delta into the admin-curated catalog, so a
                        # model rolled out / retired fleet-wide propagates
                        # while every other admin edit sticks.
                        merged = merge_env_models(
                            existing.available_models,
                            existing.env_models,
                            env_models,
                        )
                        existing.available_models = merged or None
                        existing.default_model = resolve_default_model(
                            existing.default_model, merged
                        )
                    else:
                        # Pinned regime: the env list IS the catalog.
                        existing.available_models = env_models
                        existing.default_model = (
                            _listing_default(existing.default_model, declared_default, env_models, replaced)
                            if listed
                            else env_models[0]
                        )
                    existing.env_models = env_models
                    # A model the seed just dropped from the catalog must not
                    # linger on agents / user preferences / platform defaults
                    # as a ghost reference that 404s at runtime — the same
                    # cleanup a UI shrink and the orphaned-provider prune run.
                    retired = before - set(existing.available_models or [])
                    # A listing may be partial (an upstream down): it hides the
                    # model, it does not clear the choices made on it.
                    if retired and not listed:
                        # Scoped to this config: a model another config
                        # also serves stays on the choices pinned to it.
                        await _cleanup_orphaned_model_references(
                            db, provider_name, retired, provider_id=existing.id
                        )
                elif provider_name not in SEED_LISTED_PROVIDERS or existing.available_models != existing.env_models:
                    # Kept while the catalog still equals it: a failed listing is no
                    # catalog change, and the next boot refreshes it again.
                    existing.env_models = None
                logger.debug("LLM env seed: updated %s from env var", provider_name)
                cfg = existing
            else:
                cfg = LLMProviderConfig(
                    user_id=None,
                    provider=provider_name,
                    api_key_enc=encrypt_value(api_key),
                    default_model=(
                        _listing_default(None, declared_default, env_models)
                        if listed and env_models
                        else (env_models[0] if env_models else None)
                    ),
                    available_models=env_models,
                    env_models=env_models,
                    is_from_env=True,
                )
                db.add(cfg)
                await db.flush()
                logger.info("LLM env seed: provisioned %s provider", provider_name)
            if listing and models_editable and cfg.available_models in (None, cfg.env_models):
                # Editable: refreshed until an admin edits it (it then differs from the snapshot).
                apply_listing(cfg, listing.models, listing.chat_default)
            seeded_rows[provider_name] = cfg

            # The catalog the row actually ends up with (merged in editable
            # mode), not the raw env list — the platform-default seeding
            # below must only ever point at a model the provider offers.
            available_models: list[str] = list(cfg.available_models or [])
            provider_default = cfg.default_model

            if listed:
                model_for_platform = listing_platform_model(
                    available_models, provider_default, platform_default_model, declared_default
                )
            else:
                model_for_platform = (
                    platform_default_model
                    if platform_default_model and platform_default_model in available_models
                    else provider_default
                )
            if model_for_platform:
                await _ensure_platform_default_model(
                    db,
                    provider_name,
                    model_for_platform,
                    provider_id=str(cfg.id),
                    # A listing is chat models only: the chosen one, not its first.
                    available_models=None if listed else available_models or None,
                    # Under BYOLLM an empty default of a configured install is
                    # the administrator's choice, not a gap.
                    fill_empty_defaults=not install_configured_before_boot,
                    replaces=replaced if listed else None,
                )

        async def _sync_compound_provider_base_url(
            *,
            provider: str,
            base_url: str,
            model: str,
            purpose: str,
        ) -> None:
            result = await db.execute(
                _sel(LLMProviderConfig).where(
                    LLMProviderConfig.provider == provider,
                    LLMProviderConfig.user_id.is_(None),
                )
            )
            cfg = result.scalar_one_or_none()
            compound = f"none||{base_url}"
            if cfg is None:
                cfg = LLMProviderConfig(
                    user_id=None,
                    provider=provider,
                    api_key_enc=encrypt_value(compound),
                    default_model=model,
                    available_models=[model],
                    is_from_env=True,
                )
                db.add(cfg)
                await db.flush()
                logger.info(
                    "LLM env seed: provisioned %s %s provider from %s",
                    provider,
                    purpose,
                    f"DEFAULT_{purpose.upper()}_PROVIDER_BASE_URL",
                )
            else:
                cfg.is_from_env = True
                # Update only the base_url portion of an existing compound key.
                existing_key = _dec(cfg.api_key_enc)
                api_part = (
                    existing_key.split("||")[0]
                    if "||" in existing_key
                    else existing_key
                )
                cfg.api_key_enc = encrypt_value(f"{api_part}||{base_url}")

        # ── BYOLLM: the environment furnishes an install, once ──
        # Default (``BYOLLM_ACTIVATED`` unset): env wins on every boot, and
        # ``_env_managed_ga_fields`` locks the matching Settings controls
        # read-only precisely because an admin edit would revert here.
        # With BYOLLM the install's LLM configuration belongs to the tenant
        # administrator: the operator's values furnish an install that has
        # none, and once it is configured every ``DEFAULT_*`` is inert — so
        # a restart can never take a choice back, including the choice to
        # leave a field empty.
        byollm_skipped: list[str] = []

        def _env_may_seed(env_var: str) -> bool:
            """May this boot apply *env_var* to the stored configuration?"""
            if not install_configured_before_boot:
                return True
            byollm_skipped.append(env_var)
            return False

        if platform_default_model and _env_may_seed("DEFAULT_MODEL"):
            # First pass: look for the platform default among providers
            # we just seeded from env vars (their MODELS list).  Env-seeded
            # providers always take precedence so an operator can flip the
            # platform default by editing the env var without removing
            # other UI-managed providers.
            matched_env_provider: str | None = None
            for prov_name, _ in providers_to_seed:
                # The env list, restricted to what the row still offers. In
                # pinned mode that IS the env list; in editable mode
                # an admin may have removed the model from the catalog, and
                # the platform default must never be pointed at a model its
                # provider no longer serves — the fallback / warning below
                # then apply, exactly as for any other unlisted model.
                seeded = seeded_rows.get(prov_name)
                offered = set(seeded.available_models or []) if seeded else set()
                models = [m for m in seed_lists.get(prov_name, []) if m in offered]
                if platform_default_model in models:
                    ps.agent_default_provider = prov_name
                    # Scope the provider_id lookup to env-seeded rows
                    # (``user_id IS NULL``).  Without this, an installation
                    # with BOTH an env-seeded ``openai`` row AND a UI-
                    # configured ``openai`` row (``user_id = admin.id``)
                    # would resolve ``agent_default_provider_id`` to the
                    # most-recently-created row — usually the UI one —
                    # silently routing the platform default away from the
                    # env-seeded provider whose ``MODELS`` list we just
                    # matched against.  The env-seeded first pass MUST
                    # honour the comment above ("env-seeded providers
                    # always take precedence") at the row-id level too,
                    # not just at the provider-name level.
                    result = await db.execute(
                        _sel(LLMProviderConfig)
                        .where(
                            LLMProviderConfig.provider == prov_name,
                            LLMProviderConfig.user_id.is_(None),
                        )
                        .order_by(LLMProviderConfig.created_at.desc())
                        .limit(1)
                    )
                    cfg = result.scalar_one_or_none()
                    ps.agent_default_provider_id = cfg.id if cfg else None
                    ps.agent_default_model = platform_default_model
                    matched_env_provider = prov_name
                    break

            # Fallback: when no provider is being seeded (or none of the
            # seeded providers carries the model in its ``*_MODELS`` env
            # var), search any existing DB provider whose
            # ``available_models`` lists ``DEFAULT_MODEL``.  Without this,
            # setting ``DEFAULT_MODEL`` alone on a deployment that already
            # has UI-configured providers silently does nothing — the
            # docstring above claims ``DEFAULT_MODEL`` is always applied,
            # so honour that contract.
            #
            # We query ``LLMProviderConfig`` directly here rather than
            # reusing ``existing_by_name``.  ``existing_by_name`` is
            # filtered on ``user_id IS NULL`` (env-seeded providers only)
            # so it correctly scopes the env-seed UPSERT loop above, but
            # UI-created providers set ``user_id`` to the admin who
            # created them as an audit trail (see ``create_llm_provider``
            # in ``api/v1/settings.py``) — so they're missing from that
            # dict.  Reusing it for the fallback would silently skip every
            # UI-configured provider, which is the exact deployment shape
            # this fallback was added to support.  Order by ``created_at``
            # DESC so the resolution is deterministic across boots when
            # multiple providers carry the model.
            #
            # Note: the matched provider may or may not have an
            # ``LLM_PROVIDER_*_API_KEY`` env var of its own (operator can
            # set the API key while leaving ``*_MODELS`` empty, in which
            # case the seed loop preserves the DB ``available_models``).
            # The log message therefore describes the match source rather
            # than asserting anything about the env state.
            if matched_env_provider is None:
                fallback_result = await db.execute(
                    _sel(LLMProviderConfig).order_by(
                        LLMProviderConfig.created_at.desc()
                    )
                )
                matched_cfg: LLMProviderConfig | None = None
                for cfg in fallback_result.scalars().all():
                    available = cfg.available_models or []
                    if platform_default_model in available:
                        matched_cfg = cfg
                        break

                if matched_cfg is not None:
                    ps.agent_default_provider = matched_cfg.provider
                    ps.agent_default_provider_id = matched_cfg.id
                    ps.agent_default_model = platform_default_model
                    logger.info(
                        "DEFAULT_MODEL=%s applied to existing DB "
                        "provider %s (matched via available_models "
                        "stored in the database).",
                        platform_default_model,
                        matched_cfg.provider,
                    )
                else:
                    logger.warning(
                        "DEFAULT_MODEL=%s is set but no configured "
                        "provider (env-seeded or DB-stored) lists it in "
                        "available_models — platform default left unchanged.",
                        platform_default_model,
                    )

        # ── General Assistant default chat model (DEFAULT_GENERAL_ASSISTANT_MODEL) ──
        # Distinct from DEFAULT_MODEL (the agent default): this pins the
        # General Assistant's OWN chat model on ``GeneralAssistantConfig``.
        # Re-applied on every boot (env always wins) so the Settings → General
        # Assistant "Chat Model" control can be locked like the other
        # env-managed defaults (see ``_env_managed_ga_fields``). When the env
        # var is unset, ``GeneralAssistantConfig.llm_model`` stays NULL =
        # "Agent default" (it follows ``agent_default_model``) and stays
        # editable. The model must be chat-capable and resolve to a configured
        # provider, otherwise the GA model is left unchanged.
        if ga_default_model:
            from app.llm.router import is_chat_capable_model
            from app.models.general_assistant_config import (
                DEFAULT_GA_BUILTIN_TOOL_IDS as _GA_DEFAULT_TOOLS,
            )
            from app.models.general_assistant_config import (
                GeneralAssistantConfig as _GAConfig,
            )
            from app.models.general_assistant_config import (
                default_system_prompt as _ga_default_prompt,
            )

            ga_row = (await db.execute(_sel(_GAConfig).limit(1))).scalar_one_or_none()

            if not _env_may_seed("DEFAULT_GENERAL_ASSISTANT_MODEL"):
                # BYOLLM on a configured install. ``llm_model`` being NULL is
                # not a gap here: it is the "Agent default" entry of the chat
                # model picker, the configuration BYOLLM exists to allow (the
                # assistant then follows ``agent_default_model``). Re-pinning
                # it on every restart was the silent revert this mode
                # promises not to do.
                pass
            elif not is_chat_capable_model(ga_default_model):
                logger.warning(
                    "DEFAULT_GENERAL_ASSISTANT_MODEL=%s is not a chat-capable "
                    "model (e.g. image/embedding/audio/moderation) and cannot "
                    "be used as a chat model — ignored.",
                    ga_default_model,
                )
            else:
                all_provider_configs = list(
                    (await db.execute(_sel(LLMProviderConfig))).scalars().all()
                )
                ga_owner = _env_pinned_model_owner(
                    all_provider_configs, ga_default_model
                )
                if ga_owner is None:
                    logger.warning(
                        "DEFAULT_GENERAL_ASSISTANT_MODEL=%s does not resolve to "
                        "any configured provider (not in any available_models "
                        "and no provider matches its name prefix) — General "
                        "Assistant chat model left unchanged.",
                        ga_default_model,
                    )
                else:
                    if ga_row is None:
                        ga_row = _GAConfig(
                            llm_provider=ga_owner.provider,
                            llm_provider_id=ga_owner.id,
                            llm_model=ga_default_model,
                            system_prompt=_ga_default_prompt(),
                            available_channel_types=["web"],
                            builtin_tool_ids=list(_GA_DEFAULT_TOOLS),
                        )
                        db.add(ga_row)
                    else:
                        ga_row.llm_provider = ga_owner.provider
                        # Own the reference too. Leaving a previously saved id in
                        # place would outrank this pinned model's own provider —
                        # ``get_llm_for_user`` reads the id before the name — and
                        # the field is env-locked, so nothing could clear it.
                        ga_row.llm_provider_id = ga_owner.id
                        ga_row.llm_model = ga_default_model
                    logger.info(
                        "DEFAULT_GENERAL_ASSISTANT_MODEL=%s applied to the "
                        "General Assistant (provider %s).",
                        ga_default_model,
                        ga_owner.provider,
                    )

        if routing_default_model and _env_may_seed("DEFAULT_ROUTING_MODEL"):
            ps.routing_model = routing_default_model
            # Env owns the model, so it owns the reference: a stale one would
            # point the pinned routing model at a config the env never chose,
            # and the env lock prevents the admin from clearing it. Pin the
            # owner when one resolves — clearing alone left the env path on
            # newest-config-first.
            routing_result = await db.execute(_sel(LLMProviderConfig))
            routing_owner = _env_pinned_model_owner(
                list(routing_result.scalars().all()), routing_default_model
            )
            ps.routing_provider_id = routing_owner.id if routing_owner else None

        # ``DEFAULT_FALLBACK_MODEL`` is re-applied on every boot (env always
        # wins, seed-once under BYOLLM) so the Settings -> AI Models control
        # can lock the field exactly like the other env-seeded defaults — but only when a configured
        # provider can actually serve the model. A dangling value would
        # otherwise env-lock the UI to a model failover can never use (and on a
        # single-provider deployment, prevent the failover wrapper entirely), so
        # warn and leave the stored value unchanged when no provider can serve
        # it.
        if fallback_default_model and _env_may_seed("DEFAULT_FALLBACK_MODEL"):
            # The reference is written only where ``fallback_model`` is (the
            # accepted branch below), never cleared up front: both fields are
            # env-locked off this same var — non-empty, valid or not — so
            # clearing on a rejected model would strip the admin's pin from a
            # model they keep, with the 403 preventing any repoint.
            # Use the SAME resolution the runtime failover uses (explicit
            # ``available_models`` membership OR model-name prefix inference to
            # a configured provider), preferring an env-seeded row. This lets
            # an operator pin a resilient fallback such as
            # ``claude-opus-4-8`` WITHOUT exposing it in the user-selectable
            # ``available_models`` list, while still skipping a model that no
            # configured provider could serve — so seeding never diverges from
            # what ``get_llm_for_user`` can actually wire up.
            #
            # A non-chat model (completion-only, image, embedding, ...) is
            # skipped too: the owner check accepts e.g.
            # ``gpt-3.5-turbo-instruct`` (catalog member / ``gpt-`` prefix),
            # but every failover attempt to it 404s on /v1/chat/completions —
            # re-seeding it on every boot would keep the redundancy chain
            # permanently broken.
            from app.llm.router import is_chat_capable_model

            if not is_chat_capable_model(fallback_default_model):
                logger.warning(
                    "DEFAULT_FALLBACK_MODEL=%s is not a chat-capable model "
                    "(completion-only or non-chat model) — every failover "
                    "attempt to it would fail with HTTP 404. Fallback model "
                    "left unchanged; set a valid chat model instead.",
                    fallback_default_model,
                )
            else:
                fb_result = await db.execute(_sel(LLMProviderConfig))
                fb_owner = _env_pinned_model_owner(
                    list(fb_result.scalars().all()), fallback_default_model
                )
                if fb_owner is not None:
                    ps.fallback_model = fallback_default_model
                    # Keep the owner that was just resolved: discarding it left
                    # failover on newest-config-first, so a third config
                    # advertising the model flipped the credentials.
                    ps.fallback_provider_id = fb_owner.id
                else:
                    logger.warning(
                        "DEFAULT_FALLBACK_MODEL=%s is set but does not resolve "
                        "to any configured provider (not in any "
                        "available_models and no configured provider matches "
                        "its name prefix) — fallback model left unchanged.",
                        fallback_default_model,
                    )

        # ``DEFAULT_REASONING_EFFORT`` is re-applied on every boot (env always
        # wins) so the Settings → AI and models → Models control can lock the field exactly
        # like the other env-seeded defaults — seed-once under BYOLLM, where
        # nothing is locked. The value is validated to ``low|medium|high|max``
        # in ``core/config.py``.
        if reasoning_default_effort and _env_may_seed("DEFAULT_REASONING_EFFORT"):
            ps.default_reasoning_effort = reasoning_default_effort

        # Strip env values so a whitespace-padded ``DEFAULT_EMBEDDING_*`` (e.g.
        # ``" text-embedding-3-large "``) is seeded — and its truthiness gated —
        # consistently with the ``.strip()``-based lock predicate in
        # ``_env_managed_ga_fields`` (api/v1/settings.py). Without this a padded
        # value would be stored verbatim (failing to match the provider's
        # ``available_models`` / UI lists), or a whitespace-only value would be
        # seeded here yet left unlocked there.
        emb_provider = settings.default_embedding_provider.strip()
        emb_model = settings.default_embedding_model.strip()
        # Gate on the stripped base URL too: a whitespace-only
        # ``DEFAULT_EMBEDDING_PROVIDER_BASE_URL`` is otherwise truthy, would run
        # the sync below, and stamp an invalid compound key (``api_part||``) on
        # the provider config.
        emb_base_url = settings.default_embedding_provider_base_url.strip()
        # For compound-URL providers, ensure the provider config carries the
        # right base URL when DEFAULT_EMBEDDING_PROVIDER_BASE_URL is set.
        # Outside the seed-once gate below on purpose: this provisions a
        # *provider credential*, which stays env-managed under BYOLLM like
        # every ``LLM_PROVIDER_*_API_KEY`` — and skipping it would also hide the
        # row from ``active_env_providers``, so the next
        # ``_prune_orphaned_env_providers`` would delete the provider the
        # admin's own embedding model may still be served by.
        if (
            emb_provider
            and emb_model
            and emb_provider in compound_url_providers
            and emb_base_url
        ):
            await _sync_compound_provider_base_url(
                provider=emb_provider,
                base_url=emb_base_url,
                model=emb_model,
                purpose="embedding",
            )
        if emb_provider and emb_model and _env_may_seed("DEFAULT_EMBEDDING_MODEL"):
            ps.embedding_provider = emb_provider
            # Same env-scope precedence rule as the agent_default lookup
            # above: this env-seeded path must resolve ``embedding_provider_id``
            # to the env-seeded row, not to a UI-created row that happens
            # to share the same provider type and was created more
            # recently. ``user_id IS NULL`` is the env-seeded marker
            # (see the loop at the top of this function — every env
            # provider is upserted with ``user_id=None``).
            result = await db.execute(
                _sel(LLMProviderConfig)
                .where(
                    LLMProviderConfig.provider == emb_provider,
                    LLMProviderConfig.user_id.is_(None),
                )
                .order_by(LLMProviderConfig.created_at.desc())
                .limit(1)
            )
            cfg = result.scalar_one_or_none()
            ps.embedding_provider_id = cfg.id if cfg else None
            ps.embedding_model = emb_model
        elif (
            emb_model
            and not emb_provider
            and _env_may_seed("DEFAULT_EMBEDDING_MODEL")
        ):
            ps.embedding_model = emb_model
        # ``DEFAULT_EMBEDDING_DIMENSIONS`` is re-applied on every boot (env
        # always wins), exactly like the provider/model seeding above and the
        # image-generation block below — unless ``BYOLLM_ACTIVATED`` makes it
        # seed-once. The Settings → AI and models → Models Embedding control is
        # correspondingly locked read-only when these env vars are set and
        # BYOLLM is off (see ``_env_managed_ga_fields`` in api/v1/settings.py),
        # so env wins and locks consistently with every other ``DEFAULT_*``.
        # NOTE: changing the embedding model/dimensions to a different vector
        # space after documents are ingested requires re-embedding (the UI path
        # handles this via ``_reset_kb_embeddings_for_new_model``); the env-seed
        # path overwrites the configured value only — re-embedding stays an
        # operator responsibility. Under BYOLLM the admin changes it through
        # that UI path instead, which does run the re-embedding reset.
        if settings.default_embedding_dimensions is not None and _env_may_seed(
            "DEFAULT_EMBEDDING_DIMENSIONS"
        ):
            ps.embedding_dimensions = settings.default_embedding_dimensions
        # Strip the image env values for the same reason as the embedding block
        # above — keep seeding/truthiness consistent with the ``.strip()`` lock
        # predicate in ``_env_managed_ga_fields`` and avoid storing a padded
        # model id that won't match the provider's ``available_models``.
        img_provider = settings.default_image_provider.strip()
        img_model = settings.default_image_model.strip()
        # Gate on the stripped base URL too (same reason as the embedding block).
        img_base_url = settings.default_image_provider_base_url.strip()
        # For compound-URL providers, ensure the provider config carries the
        # right base URL when DEFAULT_IMAGE_PROVIDER_BASE_URL is set. Outside
        # the seed-once gate for the same reason as the embedding block above:
        # it provisions a provider credential, and the row must stay in
        # ``active_env_providers`` so the prune spares it.
        if (
            img_provider
            and img_model
            and img_provider in compound_url_providers
            and img_base_url
        ):
            await _sync_compound_provider_base_url(
                provider=img_provider,
                base_url=img_base_url,
                model=img_model,
                purpose="image",
            )
        if img_provider and img_model and _env_may_seed("DEFAULT_IMAGE_MODEL"):
            ps.image_provider = img_provider
            # Same env-scope precedence rule as the agent_default /
            # embedding lookups above — bind the platform image
            # provider_id to the env-seeded row, never to a UI-created
            # row that shares the provider type.
            result = await db.execute(
                _sel(LLMProviderConfig)
                .where(
                    LLMProviderConfig.provider == img_provider,
                    LLMProviderConfig.user_id.is_(None),
                )
                .order_by(LLMProviderConfig.created_at.desc())
                .limit(1)
            )
            cfg = result.scalar_one_or_none()
            ps.image_provider_id = cfg.id if cfg else None
            ps.image_model = img_model
        elif img_model and not img_provider and _env_may_seed("DEFAULT_IMAGE_MODEL"):
            ps.image_model = img_model

        await env_capability_defaults.seed_env_capability_defaults(
            db, ps, may_seed=_env_may_seed
        )

        if byollm_skipped:
            # One line per boot rather than one per variable: on a configured
            # install this is the steady state, not an incident.
            logger.info(
                "BYOLLM_ACTIVATED=true: %s not applied — this install is "
                "already configured and its LLM settings belong to the "
                "administrator.",
                ", ".join(byollm_skipped),
            )

        await db.commit()
        logger.info(
            "LLM env seed: %d provider(s) synced from env vars",
            len(providers_to_seed),
        )

        active_env_providers = {name for name, _ in providers_to_seed}
        # Compound-URL embedding/image providers can be provisioned purely from a
        # ``DEFAULT_*_PROVIDER_BASE_URL`` (via ``_sync_compound_provider_base_url``
        # above) without an ``LLM_PROVIDER_*_API_KEY`` of their own, so they are
        # absent from ``providers_to_seed``. Mark them active using the stripped
        # locals (matching the row's stored name) so ``_prune_orphaned_env_providers``
        # doesn't delete the row this boot just created. A whitespace-padded
        # provider name would also miss the ``compound_url_providers`` membership
        # check if the raw value were used.
        if (
            emb_provider
            and emb_model
            and emb_provider in compound_url_providers
            and emb_base_url
        ):
            active_env_providers.add(emb_provider)
        if (
            img_provider
            and img_model
            and img_provider in compound_url_providers
            and img_base_url
        ):
            active_env_providers.add(img_provider)

        return active_env_providers


async def _byollm_install_configured(db) -> bool:
    """Has an administrator configured this install's LLM settings?

    The whole question under ``BYOLLM_ACTIVATED``: the environment furnishes an
    install that has no configuration of its own, and from the first thing an
    administrator stores, the configuration is theirs - a cleared routing
    model, a removed fallback and a General Assistant set back to "Agent
    default" are choices, not gaps waiting to be refilled.
    Always ``False`` without the flag, where the environment wins every boot.

    The agent, embedding and image defaults are also written by the platform
    itself, into an empty field, from an env-seeded provider
    (``_ensure_platform_default_model``, the XTM One embedding seed). Such a
    value is no administrator's choice, so it only counts when it is not the
    one the platform fills from the config it is pinned to
    (``platform_filled_models``), or that config is not env-seeded.
    Read it before this boot writes anything.
    """
    if not settings.byollm_activated:
        return False

    from sqlalchemy import select as _sel

    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.api.v1.settings import platform_filled_models
    from app.llm import env_capability_defaults
    from app.models.general_assistant_config import GeneralAssistantConfig
    from app.models.llm_config import LLMProviderConfig

    ps = await _load_ps(db)

    async def _chosen(field: str, provider_id) -> bool:
        model = getattr(ps, field)
        if not model:
            return False
        config = await db.get(LLMProviderConfig, provider_id) if provider_id else None
        if config is None or not config.is_from_env:
            return True
        return model != platform_filled_models(config)[field]

    if (
        ps.routing_model
        or ps.fallback_model
        or ps.default_reasoning_effort
        or env_capability_defaults.stores_a_choice(ps)
        or await _chosen("agent_default_model", ps.agent_default_provider_id)
        or await _chosen("embedding_model", ps.embedding_provider_id)
        or await _chosen("image_model", ps.image_provider_id)
    ):
        return True
    ga = (await db.execute(_sel(GeneralAssistantConfig).limit(1))).scalar_one_or_none()
    return ga is not None and bool(ga.llm_model or ga.llm_provider_id)


async def _seed_xtm_one_embedding_server_from_env(
    *, fill_embedding_default: bool = True
) -> set[str]:
    """Seed a built-in embedding endpoint provider in XTM One mode.

    Uses ``BUILTIN_EMBEDDING_SERVER_*`` env vars to create/update a platform
    provider of type ``tei`` (generic ``POST /embed`` endpoint). If no
    embedding provider/model is currently configured in PlatformSettings,
    this seed is also applied as the active embedding configuration - unless
    ``fill_embedding_default`` is ``False``, which provisioning passes under
    ``BYOLLM_ACTIVATED`` on an install an administrator has configured, where
    an empty embedding default is their choice.
    """
    if settings.is_copilot_like:
        return set()

    base_url = settings.builtin_embedding_server_base_url.strip().rstrip("/")
    model = settings.builtin_embedding_server_model.strip()

    # Nothing configured: no-op.
    if not base_url and not model:
        return set()

    # Partial config: log and skip to keep startup deterministic.
    if not base_url or not model:
        logger.warning(
            "XTM embedding seed skipped: BUILTIN_EMBEDDING_SERVER_BASE_URL and "
            "BUILTIN_EMBEDDING_SERVER_MODEL must both be set"
        )
        return set()

    if not base_url.startswith(("http://", "https://")):
        logger.warning(
            "XTM embedding seed skipped: BUILTIN_EMBEDDING_SERVER_BASE_URL "
            "must start with http:// or https://"
        )
        return set()

    token = settings.builtin_embedding_server_token.strip() or "none"
    compound_key = f"{token}||{base_url}"

    from sqlalchemy import select as _sel

    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.api.v1.settings import _lock_provider_configs
    from app.core.database import async_session
    from app.core.security import encrypt_value
    from app.models.llm_config import LLMProviderConfig

    async with async_session() as db:
        # Also runs when AI is re-enabled at runtime, next to an admin's delete:
        # it rewrites the config's catalog and may pin the embedding default.
        await _lock_provider_configs(db)
        result = await db.execute(
            _sel(LLMProviderConfig)
            .where(
                LLMProviderConfig.provider == "tei",
                LLMProviderConfig.user_id.is_(None),
            )
            .order_by(LLMProviderConfig.created_at.desc())
            .limit(1)
        )
        config = result.scalar_one_or_none()

        if config:
            config.api_key_enc = encrypt_value(compound_key)
            config.default_model = model
            config.available_models = [model]
            config.is_from_env = True
            logger.info("XTM embedding seed: updated tei provider from env")
        else:
            config = LLMProviderConfig(
                user_id=None,
                provider="tei",
                api_key_enc=encrypt_value(compound_key),
                default_model=model,
                available_models=[model],
                is_from_env=True,
            )
            db.add(config)
            await db.flush()
            logger.info("XTM embedding seed: provisioned tei provider from env")

        ps = await _load_ps(db)
        if (
            fill_embedding_default
            and not ps.embedding_provider
            and not ps.embedding_model
        ):
            ps.embedding_provider = "tei"
            ps.embedding_provider_id = config.id
            ps.embedding_model = model
            if settings.builtin_embedding_server_dimensions is not None:
                ps.embedding_dimensions = settings.builtin_embedding_server_dimensions
            logger.info(
                "XTM embedding seed: applied tei provider as platform embedding default"
            )

        await db.commit()

    return {"tei"}


async def _seed_langfuse_from_env() -> None:
    """Seed Langfuse config from ``LANGFUSE_*`` environment variables.

    When ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are both set,
    the DB row is created or overwritten on every startup with the env values
    and tracing is enabled automatically.  ``LANGFUSE_HOST`` defaults to the
    standard Langfuse cloud URL when not provided.
    """
    # Strip so whitespace-only env values are treated as unset (and a padded
    # key is never stored with its padding). Kept in lockstep with
    # ``_langfuse_env_managed`` in api/v1/settings.py, which strips identically
    # to decide whether the Langfuse card is env-locked.
    public_key = settings.langfuse_public_key.strip()
    secret_key = settings.langfuse_secret_key.strip()
    if not public_key or not secret_key:
        return

    try:
        from app.api.v1.settings import LANGFUSE_DEFAULT_HOST
        from app.core.database import async_session
        from app.core.security import encrypt_value

        host = settings.langfuse_host or LANGFUSE_DEFAULT_HOST

        async with async_session() as db:
            from sqlalchemy import select as _sel

            from app.models.langfuse_config import LangfuseConfig

            result = await db.execute(_sel(LangfuseConfig))
            config = result.scalar_one_or_none()

            if config:
                config.enabled = True
                config.host = host
                config.public_key_enc = encrypt_value(public_key)
                config.secret_key_enc = encrypt_value(secret_key)
                logger.info("Langfuse env seed: updated config from env vars (host=%s)", host)
            else:
                config = LangfuseConfig(
                    enabled=True,
                    host=host,
                    public_key_enc=encrypt_value(public_key),
                    secret_key_enc=encrypt_value(secret_key),
                )
                db.add(config)
                logger.info("Langfuse env seed: created config from env vars (host=%s)", host)

            await db.flush()
            await db.commit()

        # Reset cached client so it picks up the new config
        from app.services.langfuse_tracing import reset_langfuse_client

        reset_langfuse_client()
    except Exception:
        logger.exception(
            "Langfuse env seed failed — tracing will not be pre-provisioned"
        )


async def _prune_orphaned_env_providers(active_providers: set[str]) -> None:
    """Delete env-seeded providers that are no longer present in environment variables.

    This ensures that when a user removes an LLM_PROVIDER_*_API_KEY from their .env,
    the provider is completely removed from the database and any platform/agent
    settings that depended on it are cleanly reset.
    """
    from sqlalchemy import select as _sel

    from app.api.v1.settings import _lock_provider_configs, delete_provider_config_as_system
    from app.core.database import async_session
    from app.llm.router import invalidate_provider_cache
    from app.models.llm_config import LLMProviderConfig

    async with async_session() as db:
        # Also runs when AI is re-enabled at runtime, next to an admin's delete.
        await _lock_provider_configs(db)
        result = await db.execute(
            _sel(LLMProviderConfig)
            .where(LLMProviderConfig.is_from_env.is_(True))
            .order_by(LLMProviderConfig.created_at, LLMProviderConfig.id)
        )
        configs = result.scalars().all()

        deleted_count = 0
        for cfg in configs:
            if cfg.provider not in active_providers:
                provider_name = cfg.provider
                # The rule an admin's delete applies: defaults re-seeded
                # on the replacement and pinned, the move audited.
                cleanup = await delete_provider_config_as_system(
                    db,
                    cfg,
                    actor="env_provider_prune",
                    reason="its environment variable was removed",
                )
                deleted_count += 1

                if any(cleanup.values()):
                    logger.info(
                        "Pruned orphaned env provider '%s' — reset %d agent(s), "
                        "%d user pref(s), %d GA config field(s)",
                        provider_name,
                        cleanup["agents"],
                        cleanup["users"],
                        cleanup["ga_fields"],
                    )
                else:
                    logger.info("Pruned orphaned env provider '%s'", provider_name)

        if deleted_count > 0:
            await db.commit()
            invalidate_provider_cache()

async def _bootstrap_platform_data() -> None:
    """Single-writer DB bootstrap: seeding, env provisioning, admin upsert.

    Every uvicorn worker (and every replica during a rolling deploy) runs
    the startup task, but all of these passes are non-atomic
    check-then-insert flows against unique-constrained tables
    (``intents.name``, ``agents.slug``, ``users.email``, ...).  The caller
    MUST hold ``bootstrap_lock()`` so only one worker writes at a time —
    the others block on the lock. A worker of the same launch
    as the writer then only adopts its per-process state
    (``_adopt_launch_bootstrap``); a worker of another launch (another
    replica) re-runs the idempotent sync against the now-populated tables.
    """
    # Reconcile the forced platform instance ID (PLATFORM_ID env var) FIRST,
    # before anything below loads/creates the platform_settings singleton:
    # its primary key is the anonymous telemetry ``instance_id``, and a fresh
    # install must never emit a transient random ID before the forced one
    # lands (mirrors OpenCTI's patchPlatformId on every boot).
    from app.api.v1.platform_settings_helpers import reconcile_platform_id
    from app.core.database import async_session

    async with async_session() as db:
        await reconcile_platform_id(db)
        await db.commit()

    # Ensure the admin user exists BEFORE seeding so pre-packaged
    # (company-managed) agents/KBs/integrations/skills can be owned by a real
    # user. The platform invariant is that ``user_id`` is always set; visibility
    # is controlled by ``is_company_managed``. The authoritative admin sync
    # (password rotation, env token) still runs later in startup.
    async with async_session() as db:
        from sqlalchemy import func
        from sqlalchemy import select as _sel_admin

        from app.core.security import hash_password_async as _hpa_admin
        from app.models.user import User as _UserAdmin

        _admin_existing = (
            await db.execute(
                _sel_admin(_UserAdmin).where(
                    func.lower(_UserAdmin.email) == settings.admin_email
                )
            )
        ).scalar_one_or_none()
        if _admin_existing is None:
            db.add(
                _UserAdmin(
                    email=settings.admin_email,
                    hashed_password=await _hpa_admin(settings.admin_password),
                    full_name="Admin",
                    role="admin",
                    is_active=True,
                )
            )
            await db.commit()
            logger.info(
                "Admin user pre-provisioned for seeding: %s", settings.admin_email
            )

    # Runner event types are NOT behind the AI gate: they are what lets
    # `On event` / `Wait for event` bind at all, and UPGRADING promises
    # deterministic flows keep working with AI disabled (FR-014).
    #
    # ``seed_example_flow`` is deliberately NOT here. It wires an agent node to
    # the built-in ``opencti-assistant``, so it is AI content and it depends on
    # ``seed_builtin_agents`` having run. Seeding it first left the CTI playbook
    # with an unassigned agent node on every fresh install — permanently, since
    # ``_seed_one_flow`` is create-if-absent and never repairs an existing row.
    from app.services.memory_flow_events import ensure_memory_event_types
    from app.services.runner_flow_events import ensure_runner_event_types

    async with async_session() as db:
        await ensure_runner_event_types(db)
        # Same reasoning for the memory event type: a flow reacting to
        # `Memory persisted` needs no model of its own, so it must exist even
        # with AI disabled.
        await ensure_memory_event_types(db)
        await db.commit()

    # Removing a built-in template the mode does not ship is not AI content
    # either: the Filigran Brand Deck carries Filigran's customer logos and must
    # leave an XTM One platform whose AI is switched off as well.
    from app.services.ai_state import content_mode
    from app.services.template_container_seed import remove_unshipped_builtin_containers

    async with async_session() as db:
        await remove_unshipped_builtin_containers(db, platform_mode=content_mode())

    # The built-in dashboard widget packs are data a dashboard inserts, not AI
    # content; a failure never stops startup, the next launch seeds.
    from app.services.dashboard_widget_packs import seed_builtin_packs

    try:
        async with async_session() as db:
            if await seed_builtin_packs(db):
                logger.info("Built-in dashboard widget packs written")
    except Exception as exc:
        logger.warning("Built-in dashboard widget packs not written: %s", exc)

    # The pronunciation lexicon's first version is data the voice-over
    # reads, not AI content; a failure never stops startup, the next launch seeds.
    from app.services.video_studio import lexicon_store

    try:
        async with async_session() as db:
            if await lexicon_store.seed(db):
                logger.info("Video Studio pronunciation lexicon seeded from its data file")
    except Exception as exc:
        logger.warning("Video Studio pronunciation lexicon not seeded: %s", exc)

    # The build this launch runs: the release history the platform
    # insights tools cut counts and costs by. Never stops startup.
    from app.services.platform_builds import record_running_build

    async with async_session() as db:
        await record_running_build(db)

    # Built-in AI content. Lives in ``services/provisioning.py`` because the
    # runtime AI toggle must create exactly the same thing (FR-012).
    from app.services.ai_state import is_ai_enabled_strict
    from app.services.provisioning import provision_builtin_content

    async with async_session() as db:
        _ai_on = await is_ai_enabled_strict(db)
    if _ai_on:
        await provision_builtin_content()
        # After the agents exist: the example flow resolves one by slug.
        #
        # Also behind "FLOW_BUILDER". Seeding an ACTIVE example flow where the
        # builder is hidden is the one case that manufactures flow runs nobody
        # can reach: an `Ask user` node parks the run in ``awaiting_human``,
        # and with the canvas, the Executions feed and the HITL bell all gated
        # there is no affordance left to answer it. Gating the seed keeps a
        # fresh install from creating that situation. It does not affect the
        # agent-creation wizard, which reads the node catalog, not this flow.
        from app.core.feature_flags import is_feature_enabled
        from app.services.flow_examples import seed_example_flow

        if is_feature_enabled("FLOW_BUILDER"):
            async with async_session() as db:
                await seed_example_flow(db)
                await db.commit()
    else:
        logger.info(
            "AI is disabled on this deployment: skipping built-in content "
            "(templates, agents, assignments, skills, integrations, knowledge "
            "bases, the example flow, template containers, intent bindings, "
            "model prices) and provider provisioning. Enable it in "
            "Settings > Platform or set PLATFORM_MODE."
        )

    # Upsert admin user from ADMIN_EMAIL / ADMIN_PASSWORD env vars.
    # Always overwritten on startup so credentials stay in sync with .env.
    #
    # Must run BEFORE the CTEM-default sync below: in XTM One mode the
    # sync block only touches users with ``default_agent_id IS NULL``,
    # so if the admin row is freshly inserted *after* that sync the
    # admin would log in with no default chat agent and the frontend
    # would fall through to its "first company-managed chat agent"
    # heuristic — which picks whichever built-in agent has the most
    # recent ``created_at`` (currently CTI STIX Harvester) instead of
    # the CTEM Assistant.
    from app.core.security import hash_password_async as _hpa
    from app.models.user import User as _User

    async with async_session() as db:
        from sqlalchemy import func
        from sqlalchemy import select as _sel

        result = await db.execute(
            _sel(_User).where(func.lower(_User.email) == settings.admin_email)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.email = settings.admin_email
            existing.hashed_password = await _hpa(settings.admin_password)
            existing.role = "admin"
            existing.is_active = True
            existing.full_name = existing.full_name or "Admin"
            admin_user = existing
            logger.info("Admin user overwritten: %s", settings.admin_email)
        else:
            admin = _User(
                email=settings.admin_email,
                hashed_password=await _hpa(settings.admin_password),
                full_name="Admin",
                role="admin",
                is_active=True,
            )
            db.add(admin)
            admin_user = admin
            logger.info("Admin user created: %s", settings.admin_email)
        await db.flush()
        # Service identity for outbound LLM calls with no user context
        # (background jobs, system tasks): the Filigran AI Gateway provider
        # signs its XTM JWTs as the admin account for those calls.
        from app.llm.identity import set_service_identity

        set_service_identity(admin_user.id, settings.admin_email)
        await db.commit()

        # --- Handle Admin Env Token ---
        from app.core.security import bcrypt_checkpw_async, bcrypt_hashpw_async
        from app.models.api_key import UserApiKey as _UserApiKey

        token_result = await db.execute(
            _sel(_UserApiKey)
            .where(_UserApiKey.user_id == admin_user.id, _UserApiKey.is_from_env.is_(True))
            .order_by(_UserApiKey.created_at.desc())
        )
        env_tokens = token_result.scalars().all()

        env_token = None
        if env_tokens:
            env_token = env_tokens[0]
            if len(env_tokens) > 1:
                for dup in env_tokens[1:]:
                    await db.delete(dup)
                logger.info("Cleaned up %d duplicate admin env token(s)", len(env_tokens) - 1)

        if not settings.admin_token:
            if env_token:
                await db.delete(env_token)
                logger.info("Admin env token cleaned up (ADMIN_TOKEN is empty)")
                await db.commit()
        else:
            token_val = settings.admin_token
            hashed = (await bcrypt_hashpw_async(token_val.encode("utf-8"))).decode("utf-8")
            prefix = token_val[:12] + "..."

            if not env_token:
                new_token = _UserApiKey(
                    user_id=admin_user.id,
                    name="Admin Env Token",
                    key_prefix=prefix,
                    key_hash=hashed,
                    is_active=True,
                    is_from_env=True,
                )
                db.add(new_token)
                logger.info("Admin env token provisioned")
                await db.commit()
            else:
                if env_token.key_prefix != prefix or not await bcrypt_checkpw_async(token_val.encode("utf-8"), env_token.key_hash.encode("utf-8")):
                    env_token.key_hash = hashed
                    env_token.key_prefix = prefix
                    logger.info("Admin env token rotated")
                    await db.commit()

    # Provider provisioning is AI-only, and follows the admin upsert: the
    # gateway signs its boot model listing as that account (service identity).
    if _ai_on:
        # Lives in ``services/provisioning.py`` for the same reason the content
        # seeding does: the runtime AI toggle must create exactly the same
        # thing (FR-012).
        from app.services.provisioning import provision_env_providers

        await provision_env_providers()

        # Diagnose misconfigured chat providers: any provider registered in the
        # chat router (not embedding-only like ``tei``) that exposes no chat-capable
        # model. Best-effort and isolated — a diagnostic must never break startup.
        try:
            from sqlalchemy import select as _sel_llm

            from app.api.v1.platform_settings_helpers import (
                get_platform_settings as _gps,
            )
            from app.core.database import async_session
            from app.llm.router import (
                validate_chat_provider_configs,
                validate_fallback_model,
            )
            from app.models.llm_config import LLMProviderConfig as _LLMProviderConfig

            async with async_session() as db:
                _llm_configs = list(
                    (await db.execute(_sel_llm(_LLMProviderConfig))).scalars().all()
                )
                _ps = await _gps(db)
            validate_chat_provider_configs(_llm_configs)
            # Also validate the admin-configured explicit fallback model:
            # a completion-only or non-chat model will 404 on every failover attempt.
            validate_fallback_model(getattr(_ps, "fallback_model", None), _llm_configs)
        except Exception as exc:  # pragma: no cover - diagnostic only
            logger.debug("Chat provider validation skipped: %s", format_exception_message(exc))

    # In XTM One mode, set CTEM Assistant as default agent for all users
    # (including the admin row just upserted above) and link it as the
    # General Assistant.
    #
    # The user-default sweep is a single bulk ``UPDATE ... WHERE
    # default_agent_id IS NULL`` rather than a Python-side row-by-row
    # loop. On larger installs this avoids hydrating every NULL-default
    # user into the ORM session, holds row locks for the duration of a
    # single statement instead of the duration of the loop, and keeps
    # boot time from scaling linearly with user count.
    if settings.is_xtm_one:
        # In ``provisioning.py`` so the toggle runs the same repair.
        from app.services.provisioning import wire_default_agents

        await wire_default_agents()


async def _adopt_launch_bootstrap() -> bool:
    """Set up what ``_bootstrap_platform_data`` leaves in THIS process.

    For a worker whose launch already ran the bootstrap (``launch_marker``):
    the rows are there, the process state is not - the AI state the boot
    resolves, the ``.env`` keys the provider seed loads into ``os.environ``
    (only with AI on, as the bootstrap does), the LLM service identity.
    ``False`` when the admin row that identity names is gone, so the caller
    bootstraps again - never the migrations, which the record vouches for.
    """
    from app.core.database import async_session
    from app.llm.identity import service_account_id, set_service_identity
    from app.services.ai_state import is_ai_enabled_strict

    async with async_session() as db:
        ai_on = await is_ai_enabled_strict(db)
        admin_id = await service_account_id(db)
    if admin_id is None:
        return False
    if ai_on:
        _load_dotenv_files()
    set_service_identity(admin_id, settings.admin_email)
    return True


async def _bootstrap_or_adopt() -> bool:
    """Seed the platform data under the bootstrap lock, or adopt what this launch seeded."""
    from app.core.launch_marker import launch_bootstrap_done, mark_launch_bootstrap_done
    from app.core.migrations import bootstrap_lock
    from app.core.startup_watchdog import set_startup_step

    set_startup_step("bootstrapping the platform data")
    # All DB bootstrap writes (built-in seeding, env-provider provisioning,
    # admin upsert, defaults sync) are non-atomic check-then-insert flows.
    # Serialize them across uvicorn workers AND replicas with a dedicated
    # advisory lock — without it, concurrent boots raced each other and the
    # loser crashed startup with a UniqueViolationError (e.g. duplicate
    # ``ix_intents_name`` on ``global.assistant``), leaving the worker
    # permanently unready.
    adopted = launch_bootstrap_done() and await _adopt_launch_bootstrap()
    if not adopted:
        async with bootstrap_lock():
            # Re-read under the lock: a sibling booting alongside may have
            # finished while this worker waited on it.
            adopted = launch_bootstrap_done() and await _adopt_launch_bootstrap()
            if not adopted:
                await _bootstrap_platform_data()
                mark_launch_bootstrap_done()
    return adopted


def _report_startup_retry(exc: BaseException | None) -> None:
    """Name the outage startup waits out, or clear it once the wait is over."""
    global _startup_retrying
    from app.core.db_availability import outage_label
    from app.core.startup_watchdog import set_startup_step

    if exc is None:
        _startup_retrying = None
        return
    # The readiness probe is unauthenticated: the message (database host,
    # address) stays in the logs.
    label = outage_label(exc)
    _startup_retrying = f"database unavailable ({label})"
    set_startup_step(f"waiting out a database outage ({label})")


async def _do_all_startup_work() -> None:
    """All startup work: migrations, seeding, scheduler, gateway, etc.

    Called exclusively from ``_run_startup_tasks()`` which wraps it in a
    try/except so errors are caught without killing the process.
    """
    global _scheduler_task, _ann_index_task, _a2a_reaper_task, _a2a_client_sweep_task
    global _a2a_owed_cancel_task, _cut_turn_sweep_task, _work_item_wake_task
    global _branding_refresh_task, _security_options_refresh_task

    from app.core.database import async_session
    from app.core.db_availability import retry_while_database_unavailable
    from app.core.launch_marker import launch_bootstrap_done
    from app.core.migrations import run_migrations
    from app.core.startup_watchdog import set_startup_step

    startup_began = time.monotonic()
    # A PostgreSQL failover mid-boot closes connections in use. Both steps
    # below are safe to re-run, so each waits the outage out instead of
    # leaving the pod unready until someone deletes it.

    # A sibling worker of this launch (the one uvicorn replaced after
    # MAX_REQUESTS, or the one that booted alongside) already migrated and
    # seeded with this very image and environment. Workers that start
    # together both find no record: the later one passes the migration lock
    # with nothing left to apply, then adopts under the bootstrap lock below.
    if not launch_bootstrap_done():
        await retry_while_database_unavailable(
            "The database migrations", run_migrations, on_status=_report_startup_retry
        )

    # Build any missing pgvector HNSW ANN indexes in the BACKGROUND, after
    # migrations and off the startup-blocking path.  An HNSW build on a
    # populated embeddings table takes minutes; running it synchronously here
    # (the original c9d3e7f1a5b2 migration body / boot reconciler) starved the
    # Kubernetes liveness probe and crash-looped the container.
    # The task serializes across workers/replicas via an advisory lock held on
    # the same connection that runs the builds, and a failure only leaves
    # semantic search on the exact-scan path until the next boot retries.
    from app.core.ann_index_builder import build_ann_indexes

    _ann_index_task = asyncio.create_task(build_ann_indexes())

    adopted = await retry_while_database_unavailable(
        "The platform data bootstrap", _bootstrap_or_adopt, on_status=_report_startup_retry
    )
    set_startup_step("starting the core services")

    # The security options an administrator stored in Settings > Access and security,
    # read before the pod is marked ready: the request path reads
    # them from this snapshot (trusted proxy headers, the rate-limit fail
    # mode, ...), so the first request must already see them. The same read
    # gives the tracing pipeline the trace storage and payload switches
    # before the dispatcher below drains its overflow. A failed read
    # leaves the environment / default in force, and the poll below retries.
    from app.services.security_options import (
        refresh_security_options_cache,
        run_security_options_refresh_loop,
    )

    try:
        await refresh_security_options_cache()
    except Exception:
        logger.exception(
            "Could not read the security options at startup; the environment and "
            "the defaults apply until the next refresh"
        )
    # The vault providers connected in the UI, before traffic: a secret
    # field read by the first request may reference one. The loop below keeps
    # them in step with the other replicas.
    from app.services.vault.store import refresh_vault_providers

    try:
        await refresh_vault_providers()
    except Exception:
        logger.exception("Could not read the vault providers at startup; the next refresh retries")
    _security_options_refresh_task = asyncio.create_task(run_security_options_refresh_loop())

    # The IPv6 rate-limit key in use, a value stored in Settings > Access and security
    # included, now that the snapshot is read.
    from app.core.rate_limit import log_rate_limit_configuration

    log_rate_limit_configuration()

    # Initialize tracing dispatcher and drain any Redis overflow backlog before
    # opening traffic. This keeps local trace durability continuous across restarts.
    from app.services.tracing.dispatcher import init_tracing_dispatcher

    try:
        await init_tracing_dispatcher()
    except Exception:
        logger.warning(
            "Tracing dispatcher initialization failed; continuing without startup drain",
            exc_info=True,
        )

    # Crash recovery for runs backed by a durable orchestration execution is
    # intrinsic to the engine: on restart, executions left in ``running`` have
    # stale leases that the worker's reconciler reclaims (re-run) or
    # dead-letters automatically.
    #
    # Runs WITHOUT a live execution (in-process chat tracing runs with
    # ``assignment_id IS NULL``) are the reconciler's blind spot.  A graceful
    # shutdown finalizes this process's own interrupted runs immediately (see
    # the ``fail_orphaned_in_process_runs`` sweep in ``lifespan``).
    # Orphans from an UNGRACEFUL death (SIGKILL / OOM) are swept by
    # ``autonomous._cleanup_stale_assignment_runs`` on every scheduler tick
    # once past the ``stale_assignment_run_seconds`` fallback.  There is
    # deliberately NO global startup sweep here: in a multi-replica rollout a
    # starting pod cannot distinguish a dead process's runs from another live
    # replica's in-flight chat turns (both are ``assignment_id IS NULL`` +
    # ``running``), so failing them at boot would corrupt live runs.

    # Initialize S3/MinIO file storage (ensure bucket exists)
    try:
        from app.services.file_storage import file_storage

        _ = file_storage.client  # triggers lazy init + bucket creation
        logger.info("S3 file storage initialized (bucket: %s)", settings.s3_bucket)
    except Exception as s3_err:
        logger.warning(
            "S3 file storage not available: %s (file uploads will fail)", format_exception_message(s3_err)
        )

    # Probe and log the custom-tool sandbox mode.  Must happen early so that
    # production monitoring sees the security notice immediately on startup
    # (WARNING for the supported subprocess fallback, ERROR only when an
    # explicit docker mode cannot be satisfied) rather than lazily on the
    # first tool execution, and so the per-execution one-time call stays
    # silent (once-latch set here). The SAQ worker runs the same check in
    # its own startup hook (worker.py).
    try:
        from app.services.custom_tool_executor import warn_sandbox_mode_at_startup

        await warn_sandbox_mode_at_startup()
    except asyncio.CancelledError:
        # Cancellation must keep propagating (startup teardown / shutdown).
        # ``CancelledError`` is a ``BaseException``, so the ``except
        # Exception`` below would not catch it anyway; explicit for clarity
        # (mirrors ``release_db_connection_before_loop`` in core/database.py).
        raise
    except Exception:
        logger.warning(
            "Custom tool sandbox startup check failed; continuing.", exc_info=True
        )

    # Start the assignment scheduler
    _scheduler_task = asyncio.create_task(_autonomous_loop())
    logger.info("Assignment scheduler started")

    # Resume the chat turns the rollout that started this pod cut: at
    # once and every few seconds, not at the next scheduler window.
    from app.services.cut_turn_resume import run_sweep_loop

    _cut_turn_sweep_task = asyncio.create_task(run_sweep_loop(), name="cut-turn-resume-sweep")

    from app.services.work_item_dispatch_wake import run_wake_loop

    _work_item_wake_task = asyncio.create_task(run_wake_loop(), name="work-item-dispatch-wake")

    # The core startup path is complete at this point (migrations, bootstrap,
    # dispatcher, storage probe, scheduler). Mark the pod ready now so a
    # single-replica rolling replace does not serve blanket 503s while
    # optional post-ready warmups below run.
    #
    # Before that gate: the registered platforms' origins are in the
    # Content-Security-Policy's ``frame-ancestors`` from the first page served.
    # One query on a database that is up by now; never raises.
    await refresh_registered_platform_origins()
    _startup_done.set()
    _startup_settled.set()
    logger.info(
        "Startup core complete in %.1fs (bootstrap %s) — accepting requests",
        time.monotonic() - startup_began,
        "adopted from this launch" if adopted else "applied",
    )

    # Start the A2A stale-run reaper (see _a2a_reaper_loop for why it needs
    # no cross-worker coordination and ignores whether A2A is offered).
    _a2a_reaper_task = asyncio.create_task(_a2a_reaper_loop())
    logger.info("A2A stale-task reaper started")

    # And the outbound half's card-verification sweep, for the same reasons.
    # Kept as a separate task rather than folded into the reaper: they run on
    # different intervals and one failing must not stop the other.
    _a2a_client_sweep_task = asyncio.create_task(_a2a_client_sweep_loop())
    logger.info("A2A client card sweep started")

    # The re-send of partner cancels a crash interrupted, on a cadence of its
    # own so the sweep's network work never delays it.
    _a2a_owed_cancel_task = asyncio.create_task(_a2a_owed_cancel_loop())
    logger.info("A2A client owed-cancel re-send started")

    # Bindings whose integration gained its direction after them (#11786).
    await _a2a_converge_exposures_once()

    # Start the Discord gateway bot (if configured).  Like the S3 / branding /
    # telemetry blocks around it, the gateway is OPTIONAL: a failure here
    # (DB hiccup loading the bot row, credential decrypt error, missing
    # discord.py) must not abort startup. Log and continue; the bot simply
    # stays offline until the next boot retries.
    try:
        from app.channels.discord import start_discord_gateway

        await start_discord_gateway()
    except Exception:
        logger.exception(
            "Failed to start the Discord gateway bot — continuing startup "
            "without it (the bot stays offline until the next boot retries)"
        )

    # Prime the platform branding cache and patch the bundled index.html so
    # link-preview crawlers see the configured (DB-managed) title, logo
    # description, etc. without executing JavaScript.
    try:
        from app.api.v1.platform_settings_helpers import get_platform_settings
        from app.services.branding import refresh_branding_cache

        async with async_session() as _db:
            # Ensure the platform_settings singleton row exists before
            # priming the branding cache.  Without this, a fresh install
            # leaves the row unbuilt until the first admin write, which
            # means ``instance_id`` / ``instance_host`` / ``platform_name``
            # default to placeholders ("unknown" / "localhost") on every
            # Langfuse trace, license check, and observability tag emitted
            # between cold start and that first write — and the cache is
            # never re-primed when the row eventually appears.
            await get_platform_settings(_db)
            await _db.commit()
            await refresh_branding_cache(_db)
        _refresh_openapi_branding()
        _refresh_index_html()
    except Exception:
        logger.exception("Failed to prime branding cache / patch index.html")
    _branding_refresh_task = asyncio.create_task(_branding_refresh_loop())

    # Initialise anonymous usage telemetry (OTLP/HTTP to the Filigran
    # collector). On unless TELEMETRY_ENABLED=false, which skips it before any
    # network call; otherwise self-gating: silently disables itself when the
    # collector is unreachable, in CI or under the test runner. Adds at most a
    # short, bounded connectivity probe to startup and never fails it.
    try:
        from app.services.telemetry import init_telemetry

        async with async_session() as _tdb:
            await init_telemetry(_tdb)
    except Exception:
        logger.exception("Failed to initialise usage telemetry")


async def _run_startup_tasks() -> None:
    """Thin error-catching wrapper around ``_do_all_startup_work()``.

    A failed migration MUST NOT kill the process.  The pod stays alive so
    liveness probes keep returning 200 while a new deployment with a fixed
    image takes over via rolling replace.
    """
    global _startup_error
    from app.core.startup_watchdog import watch_startup

    # A startup that never completes would otherwise log nothing.
    watchdog = asyncio.create_task(watch_startup(_startup_settled, task=asyncio.current_task()))
    try:
        await _do_all_startup_work()
    except Exception as exc:
        _startup_error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "Background startup FAILED: %s — liveness probe stays healthy; "
            "readiness probe will return 503 until this pod is replaced by a "
            "new deployment with a fixed image.",
            format_exception_message(exc),
            exc_info=True,
        )
        # Do NOT re-raise.  Keeping the process alive prevents the crash loop:
        #   * liveness probe keeps returning 200 → pod is NOT killed & restarted
        #   * readiness probe returns 503 → no traffic is routed to this pod
        #   * a new pod with a fixed migration replaces this one via rolling deploy
    finally:
        # Also on cancellation (shutdown mid-startup): the requests held by
        # the gate are answered now rather than at the end of their hold.
        _startup_settled.set()
        watchdog.cancel()


def _log_base_path_configuration() -> None:
    """Say where the platform is served from, and flag a BASE_URL that disagrees.

    Every absolute link the backend hands out - OAuth ``redirect_uri``, webhook
    URLs, SSO redirects, e-mail links, the MCP resource identifiers - is built
    from ``BASE_URL``, while ``BASE_PATH`` is what makes the requests that come
    back route. The two describe the same public URL, so a mismatch
    (``BASE_PATH=/xtm-one`` with ``BASE_URL=https://host``) means every such
    link lands on the proxy's 404. Logged, not refused: a split-horizon
    deployment may reach the platform under another name and is the operator's
    call - but it must never be a silent one.
    """
    base_path = settings.base_path
    if not base_path:
        return
    logger.info(
        "Serving the platform under the URL path prefix %s (BASE_PATH_MODE=%s)",
        base_path,
        settings.base_path_mode,
    )
    # ``FRONTEND_URL`` is where every post-OAuth / SSO redirect, invitation and
    # password-reset link sends the browser; it must agree just like BASE_URL.
    for name, value in (("BASE_URL", settings.base_url), ("FRONTEND_URL", settings.frontend_url)):
        if not value:
            continue
        try:
            configured_path = urlsplit(value).path.rstrip("/")
        except ValueError:
            configured_path = ""
        if configured_path != base_path:
            logger.warning(
                "BASE_PATH is %s but %s (%s) does not end with it: the absolute "
                "links the platform hands out (OAuth callbacks, webhooks, SSO "
                "redirects, e-mails) will not route back through the proxy. Set "
                "%s to the public URL including the prefix.",
                base_path,
                name,
                value,
                name,
            )


def _log_llm_ownership_policy() -> None:
    """Say who owns the LLM configuration, and flag a contradictory pair.

    ``MODELS_ENV_PROVIDING_ONLY=true`` means "the providers ARE the env vars,
    nothing the UI does can stick"; ``BYOLLM_ACTIVATED=true`` means the
    opposite — the tenant administrator owns providers, catalogs and every
    ``DEFAULT_*``. Setting both is a configuration mistake, and BYOLLM wins
    (``settings.provider_management_env_only``). Logged, not refused: a
    deployment flipping to BYOLLM must not fail to boot because an older
    variable is still in its chart - but the ignored flag must never be a
    silent one.
    """
    if not settings.byollm_activated:
        return
    logger.info(
        "BYOLLM_ACTIVATED=true: administrators own the LLM configuration — "
        "providers and model catalogs are managed from Settings → AI and models → Models, "
        "and the DEFAULT_* variables furnish an install that has no LLM "
        "configuration of its own yet instead of being re-applied on each "
        "restart."
    )
    if settings.models_env_providing_only:
        logger.warning(
            "MODELS_ENV_PROVIDING_ONLY=true is ignored because "
            "BYOLLM_ACTIVATED=true: the two are contradictory and BYOLLM wins, "
            "so LLM provider management stays open to administrators. Remove "
            "MODELS_ENV_PROVIDING_ONLY to make the intent explicit."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle hooks.

    Yields immediately after the fast infrastructure-availability check so
    the HTTP server starts accepting connections right away.  All remaining
    startup work (migrations, seeding, scheduler, …) runs in a background
    asyncio task so liveness probes always get a fast 200 — even while a
    multi-minute HNSW index build is in progress.
    """
    # ``_startup_done`` / ``_startup_settled`` are mutated via .set()/.clear()
    # (no rebinding), so they need no ``global`` declaration — only the
    # rebound names do.
    global _startup_error, _startup_retrying, _scheduler_task, _ann_index_task, _a2a_reaper_task
    global _a2a_client_sweep_task, _a2a_owed_cancel_task, _branding_refresh_task
    global _security_options_refresh_task, _cut_turn_sweep_task, _work_item_wake_task

    # Install the asyncio exception handler on uvicorn's running loop so
    # routine WebSocket keepalive-ping-timeout exceptions are demoted from
    # ERROR (with a full traceback per occurrence) to DEBUG. ``setup_logging``
    # already calls this, but at that point uvicorn's loop doesn't exist
    # yet — this call is the one that actually attaches the handler.
    from app.core.logging import install_asyncio_exception_handler

    install_asyncio_exception_handler()

    # Install a generously sized default ThreadPoolExecutor on uvicorn's loop.
    # asyncpg resolves DNS for every new DB connection on the default executor,
    # which is also shared by all asyncio.to_thread() work (S3/MinIO, migrations,
    # docx, ...). Python's default (min(32, cpu+4)) is too small under load and
    # can starve DB connection establishment; a named, larger pool keeps DNS off
    # the path of application blocking work.
    from app.core.executor import configure_default_executor

    configure_default_executor("xtmone-web")

    # Start the event-loop stall watchdog: a blocked loop (the root cause of
    # the recurring /api/health probe hangs) logs a WARNING
    # with the exact blocking stack — sampled from a sidecar thread — instead
    # of dying silently under the kubelet's liveness kill.
    from app.core.loop_watchdog import start_loop_watchdog

    start_loop_watchdog("web")

    # What stops this worker, said by every turn it cuts: a stop
    # signal is recorded before uvicorn's handler sees it, a recycle waits for
    # the agent turns running in the worker, and a sibling killed without a
    # word (its health check, the OOM killer) is reported by its replacement.
    install_stop_signal_hook()
    _recycle_watch = start_recycle_watch()
    register_worker_and_report_lost()

    # Every Platform Health consent revocation runs all its handlers in this process.
    from app.services.health.consent import register_revocation_handlers

    register_revocation_handlers()

    _log_base_path_configuration()

    _log_llm_ownership_policy()

    from app.core.registration_token import log_registration_token_status

    log_registration_token_status()

    from app.core.xtm_auth import log_accepted_audiences

    log_accepted_audiences()

    # Before the startup work: it reads the FORWARDED_ALLOW_IPS uvicorn read,
    # which the provider seed's load_dotenv could otherwise add to.
    from app.core.proxy_headers import log_proxy_header_configuration

    log_proxy_header_configuration()

    from app.core.csp import log_content_security_policy_configuration

    log_content_security_policy_configuration()

    # A variable the platform mode overrides (QUOTA_FAIL_MODE=open in XTM One)
    # is said once here rather than silently ignored.
    from app.core.security_options import log_ignored_variables

    log_ignored_variables()

    # After the proxy header report: building the resolver loads the .env files.
    from app.services.vault import log_vault_configuration

    log_vault_configuration()

    # The web page fetches connect to the address they checked, never through
    # an environment proxy: a deployment that set one (in the environment or
    # the .env files loaded just above) is told.
    from app.core.pinned_request import log_environment_proxy_bypass

    log_environment_proxy_bypass()

    # The worker may run without this process's ENTERPRISE_LICENSE
    # (``licence_verdict``): it reads the licensed verdict published here.
    from app.services.licence_verdict import share_licensed_verdicts

    share_licensed_verdicts()

    # Must happen at process startup before any code path can instantiate
    # Langfuse, because the SDK builds/reuses its OTEL BatchSpanProcessor
    # singleton from environment values at first initialization.
    _seed_langfuse_otel_exporter_defaults()

    # Reset startup state for this lifecycle (important for test re-use /
    # hot-reload scenarios).
    _startup_done.clear()
    _startup_settled.clear()
    _startup_error = None
    _startup_retrying = None
    _scheduler_task = None
    _cut_turn_sweep_task = None
    _work_item_wake_task = None
    _ann_index_task = None
    _a2a_reaper_task = None
    _a2a_client_sweep_task = None
    _a2a_owed_cancel_task = None
    _branding_refresh_task = None
    _security_options_refresh_task = None

    from app.core.startup_checks import wait_for_dependencies

    # wait_for_dependencies() is fast (a few DB / Redis pings).  It runs
    # before the yield so the readiness probe can surface meaningful infra
    # errors (DB unreachable, Redis unreachable) from the very first request.
    await wait_for_dependencies()

    # Launch migrations + seeding + scheduler in the background so the HTTP
    # server starts immediately.  ConcurrencyLimitMiddleware gates non-health
    # routes until _startup_done is set by _run_startup_tasks().
    _startup_bg_task = asyncio.create_task(_run_startup_tasks())

    yield  # HTTP server starts here; liveness probes are served instantly.

    # -------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------

    # First thing: a WARNING-level trace of WHY this worker is exiting (its
    # recycle or a stop signal). All of uvicorn's own shutdown logs are
    # INFO-only, invisible at the production LOG_LEVEL=warning.
    log_worker_shutdown_summary()
    unregister_worker()
    if _recycle_watch is not None:
        _recycle_watch.cancel()
    # The CSP violations of the current window, which would otherwise be lost
    # with the process.
    csp_violation_log.flush()

    # uvicorn sends this shutdown right after cancelling the requests its
    # bounded drain left running, without awaiting them. A chat turn cut that
    # way stores its partial reply and its memory: wait for them, and
    # for the finalizers they start, while Redis and the database are still
    # here and before ``asyncio.run`` cancels every task left. The background
    # turns (wakes, channel messages) get the requests' grace, then end the
    # same way.
    await drain_shutdown_finalizers(background_grace_until=background_grace_deadline())

    # Cancel the background startup task if it is still running (e.g. pod
    # replaced mid-migration during a rolling deploy).
    if not _startup_bg_task.done():
        _startup_bg_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _startup_bg_task  # wait for clean shutdown

    # Detached A2A work holds a delegation's claim while it runs, so let it
    # finish rather than stranding the claim until it lapses.
    try:
        from app.a2a.client.delegation import drain_background_a2a

        await drain_background_a2a()
    except Exception:
        logger.debug("A2A background drain error", exc_info=True)

    # Gracefully stop the Discord gateway (no-op if it was never started)
    from app.channels.discord import stop_discord_gateway

    await stop_discord_gateway()

    if _scheduler_task is not None:
        _scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _scheduler_task  # wait for clean shutdown
    logger.info("Assignment scheduler stopped")

    if _cut_turn_sweep_task is not None:
        _cut_turn_sweep_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _cut_turn_sweep_task

    if _work_item_wake_task is not None:
        _work_item_wake_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _work_item_wake_task

    if _a2a_reaper_task is not None:
        _a2a_reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _a2a_reaper_task  # wait for clean shutdown

    if _a2a_client_sweep_task is not None:
        _a2a_client_sweep_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _a2a_client_sweep_task  # wait for clean shutdown

    if _a2a_owed_cancel_task is not None:
        _a2a_owed_cancel_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _a2a_owed_cancel_task  # wait for clean shutdown

    if _branding_refresh_task is not None:
        _branding_refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _branding_refresh_task  # wait for clean shutdown

    if _security_options_refresh_task is not None:
        _security_options_refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _security_options_refresh_task  # wait for clean shutdown

    # Memory write screens, once the scheduler and the Discord gateway that
    # can still produce memory writes are stopped.
    from app.services.builtin_tools.memory import drain_memory_screens

    await drain_memory_screens()

    # The open browser sessions' cookies and local storage, kept and shared:
    # a chat turn resumed elsewhere after this stop comes back signed in.
    try:
        from app.services.browser_tool import keep_open_sessions

        await keep_open_sessions()
    except Exception:
        logger.warning("Browser sessions not kept at the API stop", exc_info=True)

    # Proactively finalize THIS process's in-process tracing runs (chat
    # handover parents + consult/transfer children, assignment_id IS NULL)
    # that are being interrupted by this shutdown: shutdown-interrupted work
    # is failed, while a terminal-desync run whose execution actually
    # completed is reconciled to completed.  Without this they sit in
    # 'running' for up to stale_assignment_run_seconds (default 1 800 s)
    # after each rolling deploy — the generous threshold is intentional (it
    # must not fail genuinely in-flight chat turns), but at shutdown this
    # process's own runs are definitely dead.  The sweep is scoped to the
    # process-local registry so another replica's live runs (and
    # worker-managed delegation child runs) are never touched.
    try:
        from app.core.database import async_session as _async_session
        from app.services.autonomous import fail_orphaned_in_process_runs
        from app.services.in_process_runs import get_tracked_in_process_runs

        _own_run_ids = get_tracked_in_process_runs()
        if _own_run_ids:
            async with _async_session() as _sd:
                _finalized_on_shutdown = await fail_orphaned_in_process_runs(
                    _sd, _own_run_ids
                )
            if _finalized_on_shutdown:
                logger.info(
                    "Shutdown: finalized %d orphaned in-process tracing run(s)",
                    _finalized_on_shutdown,
                )
    except Exception:
        logger.warning(
            "Shutdown: in-process tracing run cleanup failed", exc_info=True
        )

    # Cancel an in-flight background ANN index build (asyncpg cancels the
    # server-side CREATE INDEX, which rolls back cleanly; next boot retries).
    if _ann_index_task is not None:
        _ann_index_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _ann_index_task

    # Flush + tear down usage telemetry (no-op when never initialised).
    try:
        from app.services.telemetry import shutdown_telemetry

        await shutdown_telemetry()
    except Exception:
        logger.debug("Telemetry shutdown error", exc_info=True)

    # Close the traces of the turns the drain above did not see end, as
    # interrupted, before the flush and the dispatcher drain below: a turn
    # cut by the stop otherwise leaves no trace (written only at its end).
    try:
        from app.services.langfuse_tracing import close_open_traces_on_shutdown

        await close_open_traces_on_shutdown("web_process_stopped")
    except Exception:
        logger.debug("Open traces not closed on shutdown", exc_info=True)

    # Flush buffered Langfuse traces so OTEL-batched spans queued in the SDK's
    # background exporter are not lost on SIGTERM / deploys (no-op when
    # Langfuse is disabled; never raises).
    try:
        from app.services.langfuse_tracing import flush_langfuse

        flush_langfuse()
    except Exception:
        logger.debug("Langfuse flush error on shutdown", exc_info=True)

    # Drain and stop tracing dispatcher (queue + Redis overflow) with the same
    # bounded shutdown contract as the story requirement.
    try:
        from app.services.tracing.dispatcher import shutdown_tracing_dispatcher
        from app.services.tracing.read_store import close_clickhouse_read_client

        await shutdown_tracing_dispatcher(timeout_seconds=30.0)
        await close_clickhouse_read_client()
    except Exception:
        logger.debug("Tracing dispatcher shutdown error", exc_info=True)

    from app.core.queue import close_queue as close_saq_queue

    await close_saq_queue()
    logger.info("SAQ queue connections closed")

    # Release all Redis connections back to the server
    from app.core.redis import close_pool

    await close_pool()
    logger.info("Redis connection pool closed")

    # Best-effort, non-blocking shutdown of the default executor installed at
    # startup (never wait — a thread may still be blocked on a slow dependency).
    try:
        from app.core.executor import shutdown_default_executor

        shutdown_default_executor()
    except Exception:
        logger.debug("Default executor shutdown error", exc_info=True)

    # Same for the dedicated S3 health-check executor (defined below with the
    # health endpoints) — a worker may still be blocked on a hung MinIO socket.
    try:
        _shutdown_s3_health_executor()
    except Exception:
        logger.debug("S3 health executor shutdown error", exc_info=True)

    # And the dedicated offload thread pools (document parse, image
    # preparation, webhook bodies) — a thread may still be crunching a
    # pathological PDF; never wait on it.
    try:
        from app.core.parse_offload import shutdown_offload_executors

        shutdown_offload_executors()
    except Exception:
        logger.debug("Offload executors shutdown error", exc_info=True)

    # And the SVG raster worker process: never wait on a render.
    try:
        from app.services.svg_raster import shutdown_svg_raster_worker

        shutdown_svg_raster_worker()
    except Exception:
        logger.debug("SVG raster worker shutdown error", exc_info=True)

    # And the Nova rule matching worker process: never wait on a match.
    try:
        from app.services.nova_matching import shutdown_nova_matching_worker

        shutdown_nova_matching_worker()
    except Exception:
        logger.debug("Nova matching worker shutdown error", exc_info=True)

    # And the widget document reader process: never wait on a read.
    try:
        from app.services.widget_document_reader import shutdown_widget_document_reader

        shutdown_widget_document_reader()
    except Exception:
        logger.debug("Widget document reader shutdown error", exc_info=True)

    # And the Teams Graph notification worker process: never wait on a decision.
    try:
        from app.channels.teams_graph_worker import shutdown_teams_graph_worker

        shutdown_teams_graph_worker()
    except Exception:
        logger.debug("Teams Graph notification worker shutdown error", exc_info=True)

    # Stop the event-loop watchdog last so it observes the whole shutdown.
    try:
        from app.core.loop_watchdog import stop_loop_watchdog

        stop_loop_watchdog("web")
    except Exception:
        logger.debug("Loop watchdog shutdown error", exc_info=True)


OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "agents",
        "description": "Create, update, delete, and list AI agents. "
        "Agents are the primary entities — each has a persona, integrations, tools, "
        "knowledge bases, and assignments.",
    },
    {
        "name": "panic",
        "description": "Emergency stop. Halt every agentic activity for the "
        "caller (or, for administrators, the whole platform): cancel runs, "
        "background tasks, chat turns, flow runs and pending runner commands, "
        "pause assignments, and resume them afterwards.",
    },
    {
        "name": "assignments",
        "description": "Manage automation rules attached to agents. "
        "Each assignment defines a trigger (schedule, webhook, integration event, manual), "
        "a prompt, optional delay, tool access, and output behavior.",
    },
    {
        "name": "knowledge-bases",
        "description": "Create and manage vectorized document collections for RAG. "
        "Upload documents, add text, configure sync sources, and search embeddings.",
    },
    {
        "name": "integrations",
        "description": "Connect to external services (Microsoft, Google, GitHub, Slack, "
        "Notion, HubSpot, Jira, and more). All credentials are stored encrypted in the "
        "database.",
    },
    {
        "name": "mcp-servers",
        "description": "Register and manage Model Context Protocol (MCP) tool servers. "
        "Agents use MCP tools during chat and assignment execution.",
    },
    {
        "name": "custom-tools",
        "description": "Define custom tools with input variables and JavaScript/Python "
        "functions. Executed in a hardened Docker sandbox when available "
        "(CUSTOM_TOOL_SANDBOX, default auto) with a timeout-enforced subprocess fallback.",
    },
    {
        "name": "channel-bots",
        "description": "Configure messaging platform bots for Slack, Teams, WhatsApp, "
        "Telegram, Discord, and Google Chat. Separate from integrations — these use "
        "bot tokens for real-time messaging.",
    },
    {
        "name": "chat",
        "description": "Manage conversations and messages. Supports Server-Sent Events for "
        "real-time streaming with agents and the general assistant.",
    },
    {
        "name": "arcade",
        "description": "The waiting arcade: the Space Invaders game a user can take "
        "control of while an agent works. Single-use game sessions, run validation, "
        "cross-user leaderboards, live players and the player profile. Disabled with "
        "ARCADE_ENABLED=false.",
    },
    {
        "name": "skills",
        "description": "Manage reusable instruction sets following the Agent Skills spec. "
        "Skills are URLs to SKILL.md files whose content is fetched, parsed, and injected "
        "into agent system prompts.",
    },
    {
        "name": "prompts",
        "description": "Create and manage reusable prompt templates. Prompts can be "
        "attached to agents or used directly from chat.",
    },
    {
        "name": "flows",
        "description": "Compose automation flows on a visual canvas — node catalog, "
        "graph validation, version history, and JSON import/export (including "
        "LangFlow and n8n exports).",
    },
    {
        "name": "auth",
        "description": "Authentication endpoints — register, login, current user, "
        "and auth configuration.",
    },
    {
        "name": "api-keys",
        "description": "Manage personal API keys for authenticating with the "
        "OpenAI-compatible LLM proxy and the Platform REST API.",
    },
    {
        "name": "llm-proxy",
        "description": "OpenAI-compatible LLM proxy. Use your API keys to access "
        "configured LLM providers through a standard `/v1/chat/completions` interface. "
        "Compatible with any OpenAI SDK client.",
    },
    {
        "name": "mcp-endpoint",
        "description": "Expose Copilot agents as MCP tools via Streamable HTTP "
        "(JSON-RPC 2.0). Two modes: user-scoped and global.",
    },
    {
        "name": "mcp-platform",
        "description": "Native OpenCTI and OpenAEV MCP servers via Streamable "
        "HTTP (JSON-RPC 2.0). Business/knowledge tools only, available as soon "
        "as the platform is registered with XTM One.",
    },
    {
        "name": "settings",
        "description": "Platform configuration — LLM providers, general assistant, "
        "SSO, OAuth defaults, Langfuse observability, and more.",
    },
    {
        "name": "users",
        "description": "User management (admin only). Create, update, and manage "
        "platform users.",
    },
    {
        "name": "groups",
        "description": "Group management (admin only). Create groups and manage "
        "memberships for entity sharing.",
    },
    {
        "name": "audit-logs",
        "description": "Immutable audit trail of user actions including CRUD operations, "
        "config changes, and auth events. Admin only.",
    },
    {
        "name": "hooks",
        "description": "Platform event hook registration. Subscribe to internal "
        "events for external notifications.",
    },
    {
        "name": "security-audit",
        "description": "Security configuration audit. Review platform security "
        "posture and get recommendations.",
    },
    {
        "name": "activity",
        "description": "Activity feed with SSE (Server-Sent Events) for real-time "
        "updates on agent actions, assignment runs, and platform events.",
    },
    {
        "name": "tasks",
        "description": "View task history — sub-agent background tasks delegated "
        "between agents.",
    },
    {
        "name": "webhooks",
        "description": "Webhook receiver for external triggers. POST to "
        "`/api/hooks/assignments/{assignment_id}` to trigger assignments from "
        "GitHub, OpenCTI, or any external system.",
    },
    {
        "name": "channels",
        "description": "Channel webhook endpoints for external messaging platforms "
        "(Slack, Teams, WhatsApp, Telegram, Discord, Google Chat).",
    },
    {
        "name": "public-catalog",
        "description": "Filigran AI Catalog — browse and clone published entities. "
        "No authentication required for reads.",
    },
    {
        "name": "public-chat",
        "description": "Public chat widget for web-embedded agents. "
        "Wide CORS, no authentication required.",
    },
    {
        "name": "ai",
        "description": "AI utility endpoints for prompt generation and "
        "other AI-assisted operations.",
    },
    {
        "name": "ai-adoption",
        "description": "AI Adoption dashboard: agentic AI usage on the platform, "
        "adoption rates and gap analysis across the organization.",
    },
    {
        "name": "learning",
        "description": "Document ingestion and style learning for agents.",
    },
    {
        "name": "admin-objects",
        "description": "Admin object management — bulk operations on "
        "platform entities.",
    },
    {"name": "oauth", "description": "OAuth redirect callback handler."},
    {"name": "sso", "description": "SSO authentication flows (OIDC + SAML 2.0)."},
]

from app.core.feature_flags import is_feature_enabled, require_feature, require_feature_or_404
from app.services.evaluation.availability import evaluation_enabled, require_evaluation
from app.services.improvement_proposals import any_surface_enabled as any_proposal_surface_enabled

# The visual flow builder is not announced externally yet (see FeatureFlag
# "FLOW_BUILDER"), and the published schema at /api/openapi.json is external
# communication like any other. The routes stay mounted — the agent-creation
# wizard reads the flow node catalog, so the flag is UI-only — they are just
# not documented, which is the same bargain the docs site makes.
if not is_feature_enabled("FLOW_BUILDER"):
    OPENAPI_TAGS = [tag for tag in OPENAPI_TAGS if tag["name"] != "flows"]


_STARTUP_PLATFORM_NAME = get_cached_branding().name


def _openapi_description(platform_name: str) -> str:
    if settings.is_xtm_one:
        intro = f"""\
**{platform_name}** is the open source, AI-native cockpit for threat-informed
defense. It connects OpenCTI (cyber threat intelligence) and OpenAEV
(adversarial exposure validation) into a single conversational experience to
operationalize Continuous Threat Exposure Management (CTEM) — threat analysis,
dashboards, threat hunting, detection engineering, and adversary emulation."""
    else:
        intro = f"""\
**{platform_name}** is an open-source, extensible agentic assistant platform.
It runs autonomous AI agents with configurable assignments to automate email triage,
PR reviews, messaging, support questions, and more."""
    return f"""\
{intro}

## Authentication

Most endpoints require a **Bearer token** (JWT or API key).
Create API keys from **Profile → API Keys** in the platform UI.

```
Authorization: Bearer YOUR_API_KEY
```

## Key concepts

| Concept | Description |
|---------|-------------|
| **Agent** | A named AI entity with a persona, integrations, tools, and knowledge bases |
| **Assignment** | An automation rule on an agent with a trigger, prompt, and tool access |
| **Integration** | A connection to an external service (Microsoft, GitHub, Slack, etc.) |
| **Knowledge Base** | A vectorized document collection for RAG (semantic search) |
| **MCP Server** | An external tool server using the Model Context Protocol |

## OpenAI-compatible proxy

The LLM proxy at `/v1/chat/completions` is fully compatible with the OpenAI SDK.
Point any OpenAI client to this endpoint with your API key.
"""


application = FastAPI(
    title=_STARTUP_PLATFORM_NAME,
    version=settings.app_version,
    description=_openapi_description(_STARTUP_PLATFORM_NAME),
    openapi_url="/api/openapi.json",
    docs_url=None,
    redoc_url=None,
    openapi_tags=OPENAPI_TAGS,
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0",
    },
    contact={
        "name": "Filigran",
        "url": "https://filigran.io",
    },
    lifespan=lifespan,
)


def _refresh_openapi_branding() -> None:
    platform_name = get_cached_branding().name
    application.title = platform_name
    application.description = _openapi_description(platform_name)
    application.openapi_schema = None


application.state.refresh_openapi_branding = _refresh_openapi_branding


SCALAR_CUSTOM_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Geologica:wght@400;500;600;700&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap');

.dark-mode {
  --scalar-font: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --scalar-font-code: 'JetBrains Mono', ui-monospace, Menlo, Monaco, monospace;

  --scalar-color-1: rgba(255, 255, 255, 0.95);
  --scalar-color-2: #AFB0B6;
  --scalar-color-3: rgba(175, 176, 182, 0.6);
  --scalar-color-accent: #0fbcff;

  --scalar-background-1: #070d19;
  --scalar-background-2: #09101e;
  --scalar-background-3: #0f1e38;
  --scalar-background-accent: rgba(15, 188, 255, 0.08);

  --scalar-border-color: #2B3447;

  --scalar-color-green: #0fbcff;
  --scalar-color-blue: #0fbcff;

  --scalar-scrollbar-color: rgba(255, 255, 255, 0.12);
  --scalar-scrollbar-color-active: rgba(255, 255, 255, 0.24);

  --scalar-radius: 4px;
  --scalar-radius-lg: 6px;
  --scalar-radius-xl: 8px;
}
.dark-mode .t-doc__sidebar {
  --scalar-sidebar-background-1: #070d19;
  --scalar-sidebar-item-hover-background: #0f1e38;
  --scalar-sidebar-item-active-background: rgba(15, 188, 255, 0.1);
  --scalar-sidebar-border-color: #2B3447;
  --scalar-sidebar-color-1: rgba(255, 255, 255, 0.95);
  --scalar-sidebar-color-2: #AFB0B6;
  --scalar-sidebar-color-active: #0fbcff;
  --scalar-sidebar-search-background: #0f1e38;
  --scalar-sidebar-search-border-color: #2B3447;
  --scalar-sidebar-search-color: #AFB0B6;
}
.dark-mode h1.section-header,
.dark-mode .t-editor__page-title h1,
.dark-mode .markdown h1,
.dark-mode .markdown h2,
.dark-mode .markdown h3 {
  font-family: 'Geologica', sans-serif;
}
.dark-mode .scalar-card {
  background: rgba(255, 255, 255, 0.03) !important;
}
.dark-mode *::selection {
  background-color: rgba(15, 188, 255, 0.3);
}
.section-flare {
  background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(15, 188, 255, 0.15), transparent) !important;
}
"""


# One exact file of one exact release, checked by the browser against its
# hash: the library default is the unversioned package URL, which runs
# whatever the CDN serves next. Bump the version and the hash together.
SCALAR_JS_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.71.0/dist/browser/standalone.js"
SCALAR_JS_INTEGRITY = "sha384-I7aSmSxf06vl5HT10vzNOAryO+PFCAVHIGwhZerHn6yM/O0642381S3kw9o7fFQd"


def _with_scalar_integrity(response: HTMLResponse) -> HTMLResponse:
    """Adds the subresource integrity check ``scalar_fastapi`` has no option for."""
    html = response.body.decode("utf-8").replace(
        f'<script src="{SCALAR_JS_URL}"></script>',
        f'<script src="{SCALAR_JS_URL}" integrity="{SCALAR_JS_INTEGRITY}" crossorigin="anonymous"></script>',
        1,
    )
    return HTMLResponse(html)


_SCALAR_CDN_ORIGIN = "{0.scheme}://{0.netloc}".format(urlsplit(SCALAR_JS_URL))

# The reference runs the Scalar bundle pinned above, which no end-to-end test
# drives: what it loads at run time is not verified, so the page's policy is
# only ever reported, never enforced - a bundle bump cannot break the page.
# The bundle evaluates strings, hence ``'unsafe-eval'`` on this page
# alone - without it every visit would report.
register_page_policy(
    "/api/reference",
    PagePolicy(
        sources={
            "script-src": (_SCALAR_CDN_ORIGIN, "'unsafe-eval'"),
            "style-src": (_SCALAR_CDN_ORIGIN, *GOOGLE_FONTS_SOURCES["style-src"]),
            "font-src": (_SCALAR_CDN_ORIGIN, *GOOGLE_FONTS_SOURCES["font-src"]),
        },
        script_hashes=lambda: _scalar_script_hashes(),
        always_report_only=True,
    ),
)


def _render_scalar_reference(title: str) -> HTMLResponse:
    return _with_scalar_integrity(get_scalar_api_reference(
        scalar_js_url=SCALAR_JS_URL,
        telemetry=False,
        # Both fetched by the browser, so they carry the public path prefix -
        # a root-relative ``/api/openapi.json`` would leave it.
        openapi_url=with_base_path(application.openapi_url or "/api/openapi.json"),
        title=title,
        scalar_favicon_url=with_base_path("/favicon-32x32.png"),
        layout=Layout.MODERN,
        theme=Theme.NONE,
        with_default_fonts=False,
        dark_mode=True,
        force_dark_mode_state="dark",
        hide_dark_mode_toggle=True,
        custom_css=SCALAR_CUSTOM_CSS,
        default_open_all_tags=True,
        authentication={
            "preferredSecurityScheme": "bearerAuth",
            "http": {
                "bearer": {
                    "token": "",
                },
            },
        },
    ))


@functools.cache
def _scalar_script_hashes() -> tuple[str, ...]:
    """Hashes of the reference's inline script, from a render with a constant
    title: only the library's template and this module's own arguments can
    reach the allow-list, never branding or request data."""
    return inline_script_hashes(_render_scalar_reference("API Reference").body.decode("utf-8"))


@application.get("/api/reference", include_in_schema=False)
async def scalar_api_reference():
    # Escaped here: the library writes the title into ``<title>`` as is.
    platform_name = html_escape(get_cached_branding().name)
    return _render_scalar_reference(f"{platform_name} – API Reference")


# ---------------------------------------------------------------------------
# CORS – two layers:
#   1. PublicCORSMiddleware: handles /api/v1/public/* with Access-Control-
#      Allow-Origin: * so the embeddable widget works from ANY domain.
#   2. CORSMiddleware: standard FastAPI CORS for authenticated APIs, locked
#      to settings.cors_origins.
#
# Starlette executes middlewares in reverse-add order (last added = first
# to run), so PublicCORSMiddleware is added AFTER CORSMiddleware to ensure
# it intercepts public paths before the restrictive middleware rejects them.
#
# IMPORTANT: PublicCORSMiddleware is a pure ASGI middleware, NOT a
# BaseHTTPMiddleware subclass.  BaseHTTPMiddleware wraps the handler
# in an internal TaskGroup which can cancel long-running requests
# (e.g. agent chat with LLM + tool calls), causing "No response returned"
# errors and orphaned DB connections.
# ---------------------------------------------------------------------------


class PublicCORSMiddleware:
    """Wide-open CORS for /api/v1/public/* (embeddable chat widget).

    Implemented as a raw ASGI middleware to avoid the BaseHTTPMiddleware
    TaskGroup that silently cancels long-running handlers.
    """

    _CORS_HEADERS: dict[str, str] = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The route path, never ``scope["path"]``: under a base path the
        # latter carries the prefix.
        path = route_path(scope)
        if not path.startswith("/api/v1/public"):
            # Non-public path — pass through without touching
            await self.app(scope, receive, send)
            return

        # Check if this is a preflight OPTIONS request
        method = scope.get("method", "")
        if method == "OPTIONS":
            response = StarletteResponse(status_code=204, headers=self._CORS_HEADERS)
            await response(scope, receive, send)
            return

        # Public path — wrap send to inject CORS headers into the response.
        # We MUST strip any CORS headers that the inner CORSMiddleware may
        # have added (especially Access-Control-Allow-Credentials: true which
        # is incompatible with Access-Control-Allow-Origin: *).
        headers_injected = False
        _cors_prefix = b"access-control-"

        async def send_with_cors(message: Message) -> None:
            nonlocal headers_injected
            if message["type"] == "http.response.start" and not headers_injected:
                headers_injected = True
                # Remove any CORS headers set by the inner CORSMiddleware
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not k.lower().startswith(_cors_prefix)
                ]
                # Add our permissive CORS headers
                for key, value in self._CORS_HEADERS.items():
                    headers.append((key.lower().encode(), value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)


class NoIndexMiddleware:
    """Tell compliant crawlers not to index or follow platform responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_robots_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"x-robots-tag"
                ]
                headers.append((b"x-robots-tag", ROBOTS_DIRECTIVES.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_robots_header)


#: Platform Health's API prefix (#101 S1) and the former prefix it replaces,
#: still served for one minor release with a ``Deprecation`` header.
HEALTH_API_PREFIX = "/api/v1/health"
CONCIERGE_ALIAS_PREFIX = "/api/v1/concierge"


class DeprecatedAliasMiddleware:
    """Mark the answers of a deprecated API prefix (RFC 9745 ``Deprecation``).

    ``aliases`` maps a former prefix to the one that replaced it; a request
    under the former prefix is answered by the same handlers, with
    ``Deprecation: true`` and a ``Link`` to the successor path.
    """

    def __init__(self, app: ASGIApp, aliases: dict[str, str]) -> None:
        self.app = app
        self.aliases = aliases

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = route_path(scope)
        successor = next(
            (new + path[len(old) :] for old, new in self.aliases.items() if path == old or path.startswith(old + "/")),
            None,
        )
        if successor is None:
            await self.app(scope, receive, send)
            return

        async def send_with_deprecation(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"deprecation", b"true"))
                headers.append((b"link", f'<{with_base_path(successor)}>; rel="successor-version"'.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_deprecation)


# ---------------------------------------------------------------------------
# Concurrency-limit middleware (health-exempt load shedding)
#
# Uvicorn's ``--limit-concurrency`` rejects *every* request with a raw 503
# once the protocol-level cap is hit — including Kubernetes liveness /
# readiness probes, which then fail and can trigger an automatic pod restart
# right when the pod is merely busy (a burst produced a storm of
# "Exceeded concurrency limit" 503s that failed the health probes).
#
# This raw ASGI middleware (same rationale as the others — avoid
# BaseHTTPMiddleware) adds an in-app admission control that runs as the
# OUTERMOST layer so it sheds cheaply, with two properties the uvicorn limiter
# cannot offer:
#
#   * Health/readiness probes and non-HTTP scopes (runner and relay
#     WebSockets, lifespan) ALWAYS bypass the limiter, so probes stay green
#     during a burst. The web chat's SSE streams are counted until their
#     response starts (authentication, setup), never while they stream.
#   * Overflow is shed instantly with ``429 Too Many Requests`` + ``Retry-After``
#     (proper backpressure) instead of being queued or blanket-503'd.
#
# Uvicorn's ``--limit-concurrency`` is kept as a high backstop ABOVE this
# limit, so this app-level limiter is the binding constraint and the
# protocol-level 503 path is effectively never reached.
# ---------------------------------------------------------------------------

_overload_logger = logging.getLogger("app.concurrency")

class ConcurrencyLimitMiddleware:
    """Per-worker, health-exempt concurrency limiter with 429 backpressure."""

    _EXEMPT_PREFIXES = ("/api/health",)
    _LOG_THROTTLE_SECONDS = 5.0
    # The web chat's SSE streams last as long as a turn, or a conversation
    # view: their slot is released once the response starts, or a few open
    # tabs would hold every slot of the worker.
    _is_long_lived_stream = staticmethod(chat_web_stream.is_long_lived_stream)

    def __init__(self, app: ASGIApp, *, limit: int) -> None:
        self.app = app
        self._limit = limit
        self._active = 0
        self._shed_since_log = 0
        self._last_log = 0.0

    def _maybe_log_shedding(self, path: str) -> None:
        """Log shed events at most once per interval to avoid a log storm."""
        import time as _time

        self._shed_since_log += 1
        now = _time.monotonic()
        if now - self._last_log >= self._LOG_THROTTLE_SECONDS:
            _overload_logger.warning(
                "Concurrency limit reached (limit=%d): shed %d request(s) with "
                "429 since last report (last path=%s)",
                self._limit,
                self._shed_since_log,
                path,
            )
            self._shed_since_log = 0
            self._last_log = now

    def _shed_response_headers(self, scope: Scope, path: str) -> dict[str, str]:
        """Build the headers for the 429 shed response.

        The limiter is the OUTERMOST middleware, so a shed 429 is returned
        before the inner middlewares run and would otherwise miss the global
        headers they add to every response. Reinstate them
        here so overload behaves like a normal response:

        * ``X-Robots-Tag`` — the noindex/nofollow contract ``NoIndexMiddleware``
          applies to all responses.
        * CORS — mirror the two CORS layers so the 429 stays observable
          client-side instead of surfacing as an opaque CORS failure: ``*`` for
          the public widget paths (matching ``PublicCORSMiddleware``), and the
          origin-echoing + credentials form for allowed authenticated origins
          (matching ``CORSMiddleware``). ``Retry-After`` is exposed so the
          browser can read the backoff hint.
        """
        headers = {"Retry-After": "1", "X-Robots-Tag": ROBOTS_DIRECTIVES}
        if path.startswith("/api/v1/public"):
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Expose-Headers"] = "Retry-After"
            return headers
        origin = ""
        for key, value in scope.get("headers", []):
            if key == b"origin":
                origin = value.decode("latin-1")
                break
        if origin and ("*" in settings.cors_origins or origin in settings.cors_origins):
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Credentials"] = "true"
            headers["Access-Control-Expose-Headers"] = "Retry-After"
            headers["Vary"] = "Origin"
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # WebSocket handshakes are gated on startup like HTTP routes: before
        # the background startup task finishes, the DB may be mid-migration,
        # so letting a runner or relay WebSocket through would hit routers
        # against a half-migrated schema.  A handshake is held like a request, and one
        # the startup did not admit in time is rejected (uvicorn answers the
        # upgrade with an HTTP error), matching the pre-gate behaviour, where
        # uvicorn was not accepting connections at all yet.  Accepted
        # WebSockets are never counted against the concurrency limit.
        if (
            scope["type"] == "websocket"
            and not _startup_done.is_set()
            and not await _wait_for_startup()
        ):
            message = await receive()  # consume the mandatory websocket.connect
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1013})
            return

        # Non-HTTP scopes (runner and relay WebSockets, lifespan) are never counted
        # or shed — pass straight through.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = route_path(scope)
        method: str = scope.get("method", "")

        # The MAX_REQUESTS recycle counts every HTTP request, probes included,
        # as uvicorn's own counter did.
        recycler.record_request()

        # Liveness short-circuit: serve GET/HEAD /api/health directly from this
        # outermost middleware so the response NEVER depends on inner-app pressure
        # (router dispatch, dependency injection, DB connection pool, etc.).
        # Even after the concurrency-limit bypass was added, an
        # exempted liveness request still traversed the inner ASGI app and could
        # stall during DB pool saturation (all 25/25 connections checked out),
        # time out, and — after repeated failures — cause Kubernetes to restart an
        # otherwise-alive pod.  Responding here breaks that dependency chain.
        #
        # Scope is intentionally minimal: only GET and HEAD are short-circuited so
        # that any other method (POST, DELETE, …) still reaches the router and gets
        # the correct 405 Method Not Allowed.  Readiness (/api/health/ready) and
        # every other /api/health/* sub-path always pass through to their
        # deep-check handlers.
        if path == "/api/health" and method in ("GET", "HEAD"):
            # HEAD must not carry a response body (RFC 9110 9.3.2). Since this
            # short-circuit bypasses Starlette's normal HEAD handling, emit an
            # empty body for HEAD (Content-Length: 0) while keeping the JSON
            # payload for GET.
            response = StarletteResponse(
                content="" if method == "HEAD" else '{"status":"ok"}',
                status_code=200,
                media_type="application/json",
                headers={"X-Robots-Tag": ROBOTS_DIRECTIVES},
            )
            await response(scope, receive, send)
            return

        # Startup gate: while the background startup task is running (migrations,
        # seeding, …), hold every non-health request up to STARTUP_HOLD_SECONDS
        # (this worker may be the one uvicorn just started next to a serving
        # sibling), then answer the ones it could not admit - all of them at
        # once when startup failed - with 503 + Retry-After so clients back off
        # and retry instead of receiving confusing errors.
        # /api/health (liveness) is already handled above.
        # /api/health/ready (readiness) passes through so it can return 503 with
        # detailed startup state (which check is pending / what errored).
        if (
            not _startup_done.is_set()
            and not path.startswith("/api/health")
            and not await _wait_for_startup()
        ):
            response = StarletteResponse(
                content='{"detail":"Server is initializing, please retry in a few seconds."}',
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "5", "X-Robots-Tag": ROBOTS_DIRECTIVES},
            )
            await response(scope, receive, send)
            return

        # Concurrency limiter is disabled — pass all remaining requests through.
        if self._limit <= 0:
            await self.app(scope, receive, send)
            return

        # All /api/health* probes bypass the concurrency limiter so a traffic
        # burst never makes Kubernetes kill a busy-but-healthy pod.
        if path.startswith(self._EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        # CORS preflight requests are cheap (the CORS middleware answers them
        # without app/DB work) and must succeed even under overload: if a
        # preflight were shed it would lack the preflight headers, the browser
        # would never send the real request, and a credentialed client would
        # see a CORS/preflight error instead of the readable 429 below. Let
        # them pass through to the CORS layer.
        #
        # Only *true* CORS preflights bypass: an OPTIONS that carries the
        # ``Access-Control-Request-Method`` header (exactly what Starlette's
        # CORSMiddleware keys off). A bare OPTIONS is routed like any other
        # method and stays subject to the limiter, so the bypass can't be used
        # to dodge overload shedding.
        if scope.get("method") == "OPTIONS" and any(
            key == b"access-control-request-method"
            for key, _ in scope.get("headers", [])
        ):
            await self.app(scope, receive, send)
            return

        # Non-blocking admission control: the check and the increment run with
        # no intervening ``await``, so they are atomic on the single-threaded
        # event loop — no semaphore race, and overflow is shed instantly
        # rather than queued behind in-flight work.
        if self._active >= self._limit:
            self._maybe_log_shedding(path)
            response = StarletteResponse(
                content='{"detail":"Server is at capacity, please retry shortly."}',
                status_code=429,
                media_type="application/json",
                headers=self._shed_response_headers(scope, path),
            )
            await response(scope, receive, send)
            return

        self._active += 1
        held = True

        def _release() -> None:
            nonlocal held
            if held:
                held = False
                self._active -= 1

        if self._is_long_lived_stream(method, path):

            async def _send_releasing(message: Message) -> None:
                if message["type"] == "http.response.start":
                    _release()
                await send(message)

            app_send: Send = _send_releasing
        else:
            app_send = send
        try:
            await self.app(scope, receive, app_send)
        finally:
            _release()


# ---------------------------------------------------------------------------
# Graceful-shutdown drain middleware
#
# The worker-recycle drain is BOUNDED on purpose (--timeout-graceful-shutdown)
# and the activity SSE stream is bounded at 15 minutes on
# purpose — which means on every MAX_REQUESTS recycle, any dashboard stream
# younger than ~13 minutes is still open when the drain deadline fires.
# Uvicorn then cancels the remaining ASGI tasks ("Task cancelled, timeout
# graceful shutdown exceeded"); the CancelledError propagated out of
# Starlette's StreamingResponse up to uvicorn's ``run_asgi``, whose blanket
# ``except BaseException`` logged "Exception in ASGI application" WITH A FULL
# TRACEBACK at ERROR level — once per open stream, a dozen+ identical
# tracebacks per recycle, several times a day, burying real errors.
#
# This outermost raw ASGI middleware (raw for the same reason as the others —
# avoid BaseHTTPMiddleware) converts that routine, by-design cancellation
# into a clean response completion instead:
#
#   * response already started (SSE stream mid-flight): send the final empty
#     body frame so uvicorn sees a COMPLETED response — EventSource clients
#     reconnect transparently to a live worker;
#   * response never started: send a minimal 503 + Retry-After so the client
#     retries against a live worker;
#   * one aggregated WARNING per drain (per-request details at DEBUG).
#
# A CancelledError that does NOT carry uvicorn's drain message (e.g. client
# disconnect propagation) keeps propagating unchanged — this middleware only
# absorbs the bounded-drain case.
#
# It also counts every HTTP and WebSocket request in flight
# (``track_request_task``): the lifespan shutdown waits, bounded, for the ones
# the drain cancelled to finish storing what they did.
# ---------------------------------------------------------------------------

_drain_logger = logging.getLogger("app.shutdown_drain")


class GracefulShutdownDrainMiddleware:
    """Complete drain-cancelled responses cleanly instead of erroring."""

    _RETRY_BODY = b'{"detail":"Server is restarting, please retry shortly."}'

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._drained = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            with track_request_task():
                await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with track_request_task():
            await self._http(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        response_started = False
        response_complete = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started, response_complete
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_complete = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except asyncio.CancelledError as exc:
            if not is_graceful_shutdown_cancellation(exc):
                raise
            # We intentionally survive the cancellation to complete the
            # response; clear the task's cancellation counter FULLY so no
            # pending-cancel state is left behind (asyncio contract for
            # suppressing a delivered cancellation). A single uncancel()
            # would leave any additional pending requests armed, and the
            # completion sends below would re-raise CancelledError
            # immediately — reinstating the very ERROR path this
            # middleware exists to absorb.
            task = asyncio.current_task()
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            self._log_drained(route_path(scope), response_started)
            # Best-effort completion: the transport may already be closing —
            # any send failure is irrelevant, the worker is exiting anyway.
            with suppress(Exception):
                if not response_started:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 503,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (
                                    b"content-length",
                                    str(len(self._RETRY_BODY)).encode(),
                                ),
                                (b"retry-after", b"1"),
                                (b"connection", b"close"),
                            ],
                        }
                    )
                    await send(
                        {
                            "type": "http.response.body",
                            "body": self._RETRY_BODY,
                            "more_body": False,
                        }
                    )
                elif not response_complete:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    )

    def _log_drained(self, path: str, response_started: bool) -> None:
        """One WARNING per drain, per-request breadcrumbs at DEBUG.

        Uvicorn already logs a single "Cancel N running task(s)" line when
        the drain deadline fires; per-request WARNINGs on top of it would
        just recreate the log storm this middleware removes. The process
        exits right after the drain, so the once-latch never needs a reset.
        """
        self._drained += 1
        if self._drained == 1:
            _drain_logger.warning(
                "Graceful shutdown drain: in-flight request(s) reached the "
                "GRACEFUL_TIMEOUT deadline and were completed early (first: "
                "%s, started=%s). This is routine worker-recycle hygiene — "
                "SSE/streaming clients reconnect to a live worker. Further "
                "occurrences in this drain are logged at DEBUG.",
                path,
                response_started,
            )
        else:
            _drain_logger.debug(
                "Graceful shutdown drain: completed cancelled request %s "
                "(started=%s)",
                path,
                response_started,
            )


# ---------------------------------------------------------------------------
# License-gate middleware (Copilot mode only)
#
# When running in copilot mode without a valid enterprise license, only a
# small set of endpoints are accessible (health, platform config, auth,
# settings save).  Everything else returns 403.  This is a raw ASGI
# middleware (same reason as PublicCORSMiddleware — avoid BaseHTTPMiddleware).
# ---------------------------------------------------------------------------

UNLICENSED_ALLOWED_PREFIXES = (
    "/api/health",
    "/api/v1/platform/config",
    "/api/v1/platform/manifest.webmanifest",
    "/api/v1/platform/branding/asset/",
    "/api/v1/auth",
    "/api/v1/settings",
    "/api/auth/",
    "/api/oauth/",
    "/api/reference",
    "/api/openapi.json",
    "/xtm/auth",
    # Violation reports from the unlicensed pages above.
    CSP_REPORT_PATH,
)


class LicenseGateMiddleware:
    """Block most endpoints in copilot mode when no valid license is present.

    Uses a short in-memory TTL cache to avoid hitting the DB and running
    crypto on every single request.
    """

    _cache_valid: bool | None = None
    _cache_ts: float = 0.0
    _CACHE_TTL = 300  # 5 minutes

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # "not copilot-like" rather than "is xtm_one": this gate exists for
        # copilot mode, and ai_disabled requires no licence at all
        # (FR-031) — reading it as "not xtm_one" made a licence mandatory in the
        # one mode documented as needing none.
        if scope["type"] != "http" or not settings.is_copilot_like:
            await self.app(scope, receive, send)
            return

        path = route_path(scope)

        # Never block the frontend SPA or its static assets — only gate
        # API endpoints.  Without this, the license splash page itself
        # cannot load when the license is missing.
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        if any(path.startswith(p) for p in UNLICENSED_ALLOWED_PREFIXES):
            await self.app(scope, receive, send)
            return

        import time as _time

        now = _time.monotonic()
        if (
            LicenseGateMiddleware._cache_valid is not None
            and (now - LicenseGateMiddleware._cache_ts)
            < LicenseGateMiddleware._CACHE_TTL
        ):
            if LicenseGateMiddleware._cache_valid:
                await self.app(scope, receive, send)
                return
        else:
            db_pem = None
            db_created_at = None
            db_error = False
            try:
                from sqlalchemy import text as _text

                from app.core.database import engine as _async_engine

                async with _async_engine.connect() as conn:
                    row = (
                        await conn.execute(
                            _text(
                                "SELECT enterprise_license, created_at "
                                "FROM platform_settings LIMIT 1"
                            )
                        )
                    ).first()
                    if row:
                        if not settings.enterprise_license:
                            db_pem = row[0]
                        # Needed for the ci-license expiration cap even when
                        # the PEM itself comes from the environment.
                        db_created_at = row[1]
            except Exception:
                db_error = True

            # Stale-while-revalidate: if the DB is temporarily unreachable
            # but we previously validated the license, extend the cache
            # instead of falsely blocking all requests.
            if (
                db_error
                and not settings.enterprise_license
                and LicenseGateMiddleware._cache_valid
            ):
                LicenseGateMiddleware._cache_ts = now
                await self.app(scope, receive, send)
                return

            from app.enterprise.licensing import get_xtm_license_info
            from app.enterprise.xtm_ca import XTM_CA_PEM

            xtm_info = get_xtm_license_info(
                pem_from_env=settings.enterprise_license,
                pem_from_db=db_pem,
                ca_pem=XTM_CA_PEM,
                platform_created_at=db_created_at,
                cache_key="xtm_gate",
            )
            # A valid Filigran license is a valid license: the retired ``copilot``
            # capability flag is not consulted anymore (a license carries a
            # quota, not a feature switch — see ``XTMLicenseInfo``).
            is_valid = xtm_info.license_validated

            LicenseGateMiddleware._cache_valid = is_valid
            LicenseGateMiddleware._cache_ts = now

            if is_valid:
                await self.app(scope, receive, send)
                return

        response = StarletteResponse(
            content='{"detail":"Enterprise license required. Configure ENTERPRISE_LICENSE or paste the license PEM in Settings."}',
            status_code=403,
            media_type="application/json",
        )
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Access-log middleware
#
# WHY THIS EXISTS: without it, handled responses produced NO application log
# line — uvicorn's access log is muted, and neither Starlette's default
# exception handlers nor other middleware log anything. So every 2xx and every
# handled 4xx (401/403/404/422, …) was visible in nginx but invisible in XTM
# One, making client/integration errors impossible to diagnose from the app.
#
# This raw ASGI middleware (avoids BaseHTTPMiddleware, like the others) emits
# exactly ONE structured line per request (method, path, status, duration). It
# is registered OUTERMOST so it sees the final wire status of every request,
# including 503s from ConcurrencyLimitMiddleware and 403s from LicenseGate.
#
# The exception handlers below don't log themselves; they bind the failure
# reason into structlog contextvars, which propagate outward to this middleware
# within the same request task and enrich its line. They are cleared after
# emit so nothing leaks into the next request.
#
# The logged path is the router's ``http.route`` template (from
# ``scope["route"]``), so no concrete path-parameter value is ever emitted —
# see _safe_path. This is the same low-cardinality, secret-free route the
# OpenTelemetry ``http.route`` convention uses.
#
# If we adopt OpenTelemetry HTTP *tracing* (we already ship the SDK for
# the metrics push, but not request instrumentation), `opentelemetry-
# instrumentation-fastapi` would emit spans carrying this same ``http.route``.
# Note it locks the whole OTel core to its release train (its 3.14-supported
# build pins semconv==0.65b0 → core ~1.44), so adopting it means bumping the
# pinned sdk/exporter (currently 1.35.0) and re-testing the metrics push — do it
# only once we want distributed request tracing, not for logs alone.
# ---------------------------------------------------------------------------

_access_logger = logging.getLogger("app.access")

# Failure-reason fields the exception handlers bind onto the request's
# contextvars and AccessLogMiddleware clears after emitting. A new reason
# handler binding a new key MUST add it here or the field leaks onto later lines.
_ACCESS_REASON_KEYS = ("validation_errors", "error_detail")

# Successful (2xx/3xx) liveness/readiness/metrics probes fire every few seconds
# on every replica; logging them at INFO would drown actionable lines, so they
# are demoted to DEBUG. Any non-2xx/3xx response on these paths is still logged
# at its status-derived level (WARNING/ERROR). CSP violation reports join them:
# a browser can send one per violation, and ``services/csp_reports.py`` logs
# them aggregated instead.
_ACCESS_LOG_QUIET_PATHS = ("/api/health", "/metrics", CSP_REPORT_PATH)


def _is_quiet_path(path: str) -> bool:
    # Match on a path *boundary* (exact, or a "/"-delimited sub-path) so that a
    # sibling route such as ``/api/healthz`` or ``/metrics-export`` is NOT
    # swept into the quiet set by a raw prefix test, while genuine probe
    # sub-paths like ``/api/health/ready`` still qualify.
    return any(
        path == quiet or path.startswith(quiet + "/") for quiet in _ACCESS_LOG_QUIET_PATHS
    )


def _access_log_level(path: str, status: int, *, error: bool, cancelled: bool) -> int:
    # A real server fault takes precedence over cancellation: a 5xx that was
    # already sent and then cancelled mid-body is still an ERROR, not a mere
    # client-disconnect WARNING.
    if error or (status >= 500 and status != 503):
        return logging.ERROR
    if cancelled:
        # Client disconnect / request cancellation is not an application fault.
        return logging.WARNING
    # A refused violation report (rate limited, too large, not a report, shed
    # or refused while Redis is down) is browser traffic, not a fault: one line
    # per request is what the aggregated CSP logging exists to avoid.
    if (status < 500 or status == 503) and (path == CSP_REPORT_PATH or path.startswith(CSP_REPORT_PATH + "/")):
        return logging.DEBUG
    # 503 is retryable back-pressure (startup gate / overload), not a fault.
    if status == 503 or status >= 400:
        return logging.WARNING
    if _is_quiet_path(path):
        return logging.DEBUG
    return logging.INFO


# Error ``type``s raised by user-defined validators (``ValueError`` /
# ``AssertionError`` in a ``field_validator`` / ``model_validator``). Their
# ``msg`` is developer-formatted and frequently interpolates the offending
# value verbatim (e.g. ``f"Unsupported capability '{value}'"``), so keeping it
# would re-leak the submitted secret that stripping ``input`` was meant to keep
# out. For these we log only ``type`` + ``loc``; every built-in pydantic type
# has a static, value-free ``msg`` template that is safe to keep.
_VALUE_ECHOING_ERROR_TYPES = frozenset({"value_error", "assertion_error"})


def _redact_validation_errors(errors: list) -> list:
    """Keep only the value-free diagnostic fields of a validation error.

    Pydantic's ``exc.errors()`` embeds the offending ``input`` (and sometimes
    ``ctx``) value, which for a request body is the *entire submitted payload* —
    including secrets such as passwords or tokens. We log ``type`` / ``loc``
    (which field failed) plus ``msg`` (why) — but drop ``msg`` for custom
    validator errors whose message can echo the submitted value. The submitted
    value itself is never logged.
    """
    redacted = []
    for e in errors:
        if not isinstance(e, dict):
            continue
        keep = ("type", "loc")
        if e.get("type") not in _VALUE_ECHOING_ERROR_TYPES:
            keep = ("type", "loc", "msg")
        redacted.append({k: e[k] for k in keep if k in e})
    return redacted


def _forwarded_for(scope: Scope) -> str | None:
    # First X-Forwarded-For hop (client as seen by nginx), trusted only when
    # trust_proxy_headers is on — otherwise any client can spoof it.
    # Same hop and same reading as request_client_ip(): an IP address or
    # nothing, never text that is not an address.
    from app.core.security_options import security_option

    if not security_option("trust_proxy_headers"):
        return None
    for key, value in scope.get("headers", []):
        if key == b"x-forwarded-for":
            return first_forwarded_hop(value.decode("latin-1"))
    return None


def _safe_path(scope: Scope) -> str:
    # Log the route *template* (e.g. /invitations/validate/{token}), never the
    # concrete path, so a secret in a path segment is never written to the log.
    # The router records the matched route on scope["route"]; requests shed
    # before routing (429/503) or errored pre-routing have none, so replay the
    # router's own matching to recover the same template. A genuine 404 matches
    # nothing and falls back to the raw path (nginx logs it anyway).
    template = getattr(scope.get("route"), "path_format", None)
    if template:
        return template
    for route in application.router.routes:
        if route.matches(scope)[0] == Match.FULL:
            return getattr(route, "path_format", None) or route_path(scope)
    # Route-relative like the templates above, so a quiet-path probe and a
    # 404 read the same way with or without a base path.
    return route_path(scope)


class AccessLogMiddleware:
    """Emit one structured access-log line per HTTP request (entry → exit)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        # ``code`` defaults to 500 so a handler that returns without ever
        # sending a response start (a bug) is still surfaced as an error.
        state = {"code": 500, "started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["code"] = message.get("status", 500)
                state["started"] = True
            await send(message)

        method: str = scope.get("method", "")
        # scope["client"] as uvicorn left it (the peer, or the client it read
        # from a proxy FORWARDED_ALLOW_IPS names), never the trust_proxy_headers
        # preference, which forwarded_for records next to it. Validated all the
        # same: FORWARDED_ALLOW_IPS="*" has uvicorn copy the header in unchecked.
        client = scope.get("client")
        client_ip = client_address(client[0]) if client else None
        forwarded_for = _forwarded_for(scope)

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            # Client disconnected (or the server cancelled the task) before the
            # response completed. These otherwise vanish from the logs entirely.
            code = state["code"] if state["started"] else 499
            self._emit(
                method, scope, code, start, client_ip, forwarded_for, cancelled=True
            )
            raise
        except Exception:
            # If the response had already started (e.g. a streaming body failed
            # mid-flight) log the status actually sent to the client; otherwise
            # this becomes the 500 the outer ServerErrorMiddleware will send.
            code = state["code"] if state["started"] else 500
            self._emit(
                method, scope, code, start, client_ip, forwarded_for, error=True
            )
            raise
        self._emit(method, scope, state["code"], start, client_ip, forwarded_for)

    @staticmethod
    def _emit(
        method: str,
        scope: Scope,
        status: int,
        start: float,
        client_ip: str | None,
        forwarded_for: str | None,
        *,
        error: bool = False,
        cancelled: bool = False,
    ) -> None:
        try:
            # Computed at emit time so the router has populated ``path_params``,
            # letting _safe_path redact any credential-bearing path segment.
            path = _safe_path(scope)
            level = _access_log_level(path, status, error=error, cancelled=cancelled)
            # Bind the request fields transiently so they render on this single
            # access line (via merge_contextvars) without polluting other logs.
            # Any ``validation_errors`` / ``error_detail`` bound by the exception
            # handlers below is still set on the task and auto-merges here.
            fields: dict[str, object] = {
                "http_method": method,
                "http_path": path,
                "http_status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "client_ip": client_ip,
            }
            if forwarded_for:
                fields["forwarded_for"] = forwarded_for
            if cancelled:
                fields["client_disconnected"] = True
            with structlog.contextvars.bound_contextvars(**fields):
                _access_logger.log(level, "http_request")
        except Exception:
            # Access logging must never break a successful request nor mask the
            # application error the outer handler is about to surface. This
            # ``_emit`` runs after the response is already on the wire (or is
            # followed by ``raise`` on the error paths), so swallow any logging
            # failure and record it out-of-band.
            with suppress(Exception):
                _access_logger.exception("access_log_emit_failed")
        finally:
            # Always clear the reason fields bound by the exception handlers so
            # they can never bleed onto a later request, even if emit failed
            # before binding its own fields.
            structlog.contextvars.unbind_contextvars(*_ACCESS_REASON_KEYS)


@application.exception_handler(RequestValidationError)
async def _log_request_validation_error(
    request: Request, exc: RequestValidationError
) -> StarletteResponse:
    """Surface the *reason* for a 422 on the access line, then respond as usual.

    Binds the (redacted) validation errors into contextvars (read by
    AccessLogMiddleware) and delegates to FastAPI's default handler so the 422
    response body is byte-for-byte identical to the framework default - except
    that a surrogate the errors echo is written as its escape: UTF-8 cannot
    encode one, and the default 422 failed as a 500.
    """
    errors = exc.errors()
    encodable = escape_surrogates(errors)
    if encodable is not errors:
        exc = RequestValidationError(encodable, body=exc.body, endpoint_ctx=exc.endpoint_ctx)
    # Redact FIRST, then json-encode only the surviving type/loc/msg fields —
    # never walk/encode the full submitted payload embedded in ``input``/``ctx``.
    structlog.contextvars.bind_contextvars(
        validation_errors=jsonable_encoder(_redact_validation_errors(exc.errors()))
    )
    return await request_validation_exception_handler(request, exc)


@application.exception_handler(StarletteHTTPException)
async def _log_http_exception(
    request: Request, exc: StarletteHTTPException
) -> StarletteResponse:
    """Surface the HTTPException detail on the access line, then respond as usual."""
    if exc.detail:
        # Cap length; detail is developer-set and already returned to the client,
        # but avoid unbounded blobs bloating the log line.
        structlog.contextvars.bind_contextvars(error_detail=str(exc.detail)[:300])
    if isinstance(exc, CodedHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code},
            headers=exc.headers,
        )
    return await http_exception_handler(request, exc)


@application.exception_handler(SecretUnavailableError)
async def _secret_unavailable(request: Request, exc: SecretUnavailableError) -> StarletteResponse:
    """A secret read from a vault failed: a generic 502, the reason in the server log and on the entity."""
    structlog.contextvars.bind_contextvars(error_detail=f"vault reference: {exc.reason}")
    return JSONResponse(status_code=502, content={"detail": str(exc)})


install_database_unavailable_handler(application)


application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
application.add_middleware(PublicCORSMiddleware)
application.add_middleware(LicenseGateMiddleware)
application.add_middleware(NoIndexMiddleware)
application.add_middleware(DeprecatedAliasMiddleware, aliases={CONCIERGE_ALIAS_PREFIX: HEALTH_API_PREFIX})
# Content-Security-Policy + Referrer-Policy on every HTML document.
# Keys on ``route_path``, so it stays inside ``BasePathMiddleware`` below.
application.add_middleware(
    ContentSecurityPolicyMiddleware,
    spa_script_hashes=get_branded_index_script_hashes,
    embedders=registered_platform_origins,
)
# Runs just outside the routers/CORS stack, so overflow is shed before any
# other middleware work. Health probes and WebSockets bypass it internally.
application.add_middleware(
    ConcurrencyLimitMiddleware, limit=settings.app_max_concurrency
)
# Absorbs a bounded-drain cancellation no matter where in the stack it was
# delivered, completing the response instead of erroring
application.add_middleware(GracefulShutdownDrainMiddleware)
# Added second to last → outside every middleware that ANSWERS a request, so it
# observes the final wire status of EVERY request — the raw 503s shed by
# ConcurrencyLimitMiddleware, the 403s from LicenseGate, and the 503 the drain
# layer emits on shutdown — and writes one structured access line for each.
application.add_middleware(AccessLogMiddleware)
# Added last → the true OUTERMOST layer. Answers nothing and reads nothing but
# the path: it normalises a request that reached the platform under its URL
# path prefix (``BASE_PATH``) to the ASGI mounted-app shape, whether
# the reverse proxy forwarded the prefix or stripped it, so every layer below
# — the path-keyed middlewares above, the routers, the ``/office`` mount, the
# SPA catch-all — sees one consistent request. A blank ``BASE_PATH`` makes it
# a pass-through.
application.add_middleware(BasePathMiddleware, base_path=settings.base_path)

# --- API routers ---
application.include_router(
    platform.router, prefix="/api/v1/platform", tags=["platform"]
)
# Workspaces in the embedded chat, next to its sessions.
application.include_router(
    platform_chat_workspaces.router, prefix="/api/v1/platform", tags=["platform"]
)
application.include_router(
    platform_access_notices.router, prefix="/api/v1/platform", tags=["platform"]
)
application.include_router(
    metrics.router
)
application.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
application.include_router(
    invitations.router, prefix="/api/v1/invitations", tags=["invitations"]
)
application.include_router(
    settings_api.router, prefix="/api/v1/settings", tags=["settings"]
)
application.include_router(
    file_retention_router, prefix="/api/v1/settings/file-retention", tags=["settings"]
)
application.include_router(
    model_announcements.router, prefix="/api/v1/settings/model-announcements", tags=["settings"]
)
# Who can use a lent resource, and through what: ``/api/v1/exposure/...``.
application.include_router(
    resource_exposure.router, prefix="/api/v1", tags=["sharing-exposure"]
)
application.include_router(
    integrations.router, prefix="/api/v1/integrations", tags=["integrations"]
)
# The OpenCRQ version card: /api/v1/integrations/{id}/opencrq/compatibility
application.include_router(
    opencrq.router, prefix="/api/v1/integrations", tags=["integrations"]
)
# The WhatsApp gateway Pairing card: /api/v1/integrations/{id}/whatsapp/pairing
application.include_router(
    whatsapp_gateway_pairing.router, prefix="/api/v1/integrations", tags=["integrations"]
)
# The OpenRouter console: /api/v1/integrations/{id}/openrouter/...
application.include_router(
    openrouter.router, prefix="/api/v1/integrations", tags=["integrations"]
)
# The Supabase database access probe: /api/v1/integrations/{id}/supabase/probe
application.include_router(
    supabase_console.router, prefix="/api/v1/integrations", tags=["integrations"]
)
# The LM Studio console: /api/v1/integrations/{id}/lmstudio/...
application.include_router(
    lmstudio.router, prefix="/api/v1/integrations", tags=["integrations"]
)
# The Email signature cards: /api/v1/integrations/{id}/email-signature
# and /api/v1/agents/{id}/email-signatures.
application.include_router(
    email_signatures.integration_router, prefix="/api/v1/integrations", tags=["integrations"]
)
application.include_router(
    email_signatures.agent_router, prefix="/api/v1/agents", tags=["agents"]
)
application.include_router(
    channel_bots.router, prefix="/api/v1/channel-bots", tags=["channel-bots"]
)
# The public push endpoint first: its literal path must win over ``/{account_id}``.
application.include_router(
    agent_accounts.notification_router, prefix="/api/v1/agent-accounts", tags=["agent-accounts"]
)
application.include_router(
    agent_accounts.router, prefix="/api/v1/agent-accounts", tags=["agent-accounts"]
)
application.include_router(
    integration_notifications.notification_router,
    prefix="/api/v1/integrations",
    tags=["integrations"],
)
application.include_router(
    integration_elements.element_router,
    prefix="/api/v1/integrations",
    tags=["integrations"],
)
application.include_router(
    integration_openapi.router,
    prefix="/api/v1/integrations",
    tags=["integrations"],
)
application.include_router(
    integration_github_webhooks.github_webhook_router,
    prefix="/api/v1/integrations",
    tags=["integrations"],
)
application.include_router(
    integration_exposure.exposure_router,
    prefix="/api/v1/integrations",
    tags=["integrations"],
)
application.include_router(
    agent_template_router, prefix="/api/v1/agents", tags=["agents"]
)
application.include_router(
    agent_memory_router, prefix="/api/v1/agents", tags=["agents"]
)
application.include_router(memory_consolidations_agent_router, prefix="/api/v1/agents", tags=["agents"])
application.include_router(memory_consolidations_me_router, prefix="/api/v1/settings", tags=["settings"])
application.include_router(agent_chat_router, prefix="/api/v1/agents", tags=["agents"])
application.include_router(agent_skill_router, prefix="/api/v1/skills", tags=["skills"])
application.include_router(agents.router, prefix="/api/v1/agents", tags=["agents"])
# Platform Health (#101 S1): served under /api/v1/health; the former
# /api/v1/concierge prefix answers the same handlers as a deprecated alias
# (DeprecatedAliasMiddleware, dev-docs/reference/api-routes.md).
for _health_prefix, _in_schema in ((HEALTH_API_PREFIX, True), (CONCIERGE_ALIAS_PREFIX, False)):
    for _health_router in (
        platform_health.router,
        platform_health_cycles.router,
        platform_health_companion.router,
        platform_health_findings.router,
        platform_health_analysis.router,
    ):
        application.include_router(_health_router, prefix=_health_prefix, tags=["health"], include_in_schema=_in_schema)

application.include_router(
    assignments.router, prefix="/api/v1/assignments", tags=["assignments"]
)
application.include_router(
    knowledge_bases.router, prefix="/api/v1/knowledge-bases", tags=["knowledge-bases"]
)
application.include_router(tasks.router, prefix="/api/v1/tasks", tags=["tasks"])
application.include_router(
    annotations.router, prefix="/api/v1/annotations", tags=["annotations"]
)
application.include_router(
    feedback_criteria.router, prefix="/api/v1/feedback-criteria", tags=["annotations"]
)
# Improvement proposals (#8256): a person's persona rewrite stays behind
# "EVALUATION" (its POST /persona route and its rows), what Platform Health filed
# behind its availability; with both off the router is neither served nor published.
application.include_router(
    improvement_proposals.router,
    prefix="/api/v1/improvement-proposals",
    tags=["agents"],
    include_in_schema=any_proposal_surface_enabled(),
)
application.include_router(traces.router, prefix="/api/v1/traces", tags=["traces"])
application.include_router(
    hitl_whitelist.router, prefix="/api/v1/hitl-whitelist", tags=["hitl-whitelist"]
)
application.include_router(
    tool_approvals.router, prefix="/api/v1/tool-approvals", tags=["tool-approvals"]
)
application.include_router(
    work_items.router, prefix="/api/v1/work-items", tags=["work-items"]
)
application.include_router(decisions.router, prefix="/api/v1/decisions", tags=["decisions"])
application.include_router(
    shared_state.router, prefix="/api/v1/shared-state", tags=["shared-state"]
)
application.include_router(
    worksets.router, prefix="/api/v1/worksets", tags=["worksets"]
)
application.include_router(
    workspaces.router, prefix="/api/v1/workspaces", tags=["workspaces"]
)
application.include_router(
    fleets.router, prefix="/api/v1/fleets", tags=["fleets"]
)
application.include_router(
    fleet_member_presets.router,
    prefix="/api/v1/fleet-member-presets",
    tags=["fleets"],
)
application.include_router(
    workspace_knowledge.router, prefix="/api/v1/workspaces", tags=["workspaces"]
)
application.include_router(
    activity.router, prefix="/api/v1/activity", tags=["activity"]
)
application.include_router(
    learning.router, prefix="/api/v1/learning", tags=["learning"]
)
application.include_router(
    mcp_servers.router, prefix="/api/v1/mcp-servers", tags=["mcp-servers"]
)
application.include_router(
    custom_tools.router, prefix="/api/v1/custom-tools", tags=["custom-tools"]
)
application.include_router(
    custom_tool_sources.router,
    prefix="/api/v1/custom-tool-sources",
    tags=["custom-tool-sources"],
)
application.include_router(
    datasets.router,
    prefix="/api/v1/datasets",
    tags=["datasets"],
    dependencies=[Depends(require_evaluation)],
    include_in_schema=evaluation_enabled(),
)
application.include_router(
    evaluations.router,
    prefix="/api/v1/evaluations",
    tags=["evaluations"],
    dependencies=[Depends(require_evaluation)],
    include_in_schema=evaluation_enabled(),
)
application.include_router(
    variables.router, prefix="/api/v1/variables", tags=["variables"]
)
# Secret vaults: the administrators' providers under Settings, and the
# secret picker every secret field opens.
application.include_router(
    secret_vaults.admin_router, prefix="/api/v1/settings/secret-vaults", tags=["secret-vaults"]
)
application.include_router(
    secret_vaults.router, prefix="/api/v1/secret-vaults", tags=["secret-vaults"]
)
# Deliberately its own surface, NOT under /knowledge-bases: template
# containers are structured, non-vectorized metadata and must never be reachable
# through a Knowledge Base code path.
application.include_router(
    template_containers.router,
    prefix="/api/v1/template-containers",
    tags=["template-containers"],
)
# Agent-generated slide decks. Read-only: decks are written exclusively
# through the validated ``generate_presentation`` built-in tool, so no client can
# introduce a spec shape the renderer does not understand.
application.include_router(
    presentations.router,
    prefix="/api/v1/presentations",
    tags=["presentations"],
)
# Global search: the top bar's search field and its results page, one query
# over every entity type the caller may open (``services/global_search.py``).
application.include_router(search.router, prefix="/api/v1/search", tags=["search"])
# Browser profiles: saved sign-ins every browser of the platform
# opens - an agent's ``browser`` tool and a Video Studio recording alike - so
# NOT behind the Video Studio flag.
application.include_router(
    browser_profiles.router,
    prefix="/api/v1/browser-profiles",
    tags=["browser-profiles"],
)
# Video Studio: recordings, AI clips, video projects, renders and their
# media (confined-token byte ranges for the player).
# Behind "VIDEO_STUDIO": off, every route answers 403 and none is
# documented.
application.include_router(
    video_studio.router,
    prefix="/api/v1/video-studio",
    tags=["video-studio"],
    dependencies=[Depends(require_feature("VIDEO_STUDIO"))],
    include_in_schema=is_feature_enabled("VIDEO_STUDIO"),
)
# Agent-generated executive reports. Read-only for the same reason as
# decks: reports are written exclusively through the validated ``generate_report``
# built-in tool, so no client can introduce a spec shape the renderer would have
# to guess at.
application.include_router(
    reports.router,
    prefix="/api/v1/reports",
    tags=["reports"],
)
# Custom dashboards, built by the validated dashboard tools and the studio.
# Behind "DASHBOARD_STUDIO": off, every route answers 404 and none is documented.
application.include_router(
    custom_dashboards.router,
    prefix="/api/v1/custom-dashboards",
    tags=["custom-dashboards"],
    include_in_schema=is_feature_enabled("DASHBOARD_STUDIO"),
)
# The Dashboard Studio's widget packs (behind DASHBOARD_STUDIO, undocumented while off).
application.include_router(
    dashboard_widget_packs.router,
    prefix="/api/v1/dashboard-widget-packs",
    tags=["dashboard-widget-packs"],
    include_in_schema=is_feature_enabled("DASHBOARD_STUDIO"),
)
# The mobile companion's PIN lock (``services/app_lock.py``): the only routes a
# locked companion session reaches. Linked phones (``models/mobile_device.py``)
# and the buttons of their notifications: ``/mobile/push-actions`` takes no
# session, its one-time token is the credential (``services/push_action_tokens.py``).
# Behind "MOBILE_COMPANION": off, every route answers 404 (evaluated before
# authentication) and none is documented.
_companion_gate = [Depends(require_feature_or_404("MOBILE_COMPANION"))]
_companion_documented = is_feature_enabled("MOBILE_COMPANION")
application.include_router(
    app_lock.router,
    prefix="/api/v1/app-lock",
    tags=["app-lock"],
    dependencies=_companion_gate,
    include_in_schema=_companion_documented,
)
application.include_router(
    mobile_devices.router,
    prefix="/api/v1/mobile-devices",
    tags=["mobile-devices"],
    dependencies=_companion_gate,
    include_in_schema=_companion_documented,
)
application.include_router(
    mobile_devices.actions_router,
    prefix="/api/v1/mobile",
    tags=["mobile-devices"],
    dependencies=_companion_gate,
    include_in_schema=_companion_documented,
)
# The notification feed the bell and a linked phone read, with server-side
# read state (``services/notification_feed.py``).
application.include_router(
    notifications.router,
    prefix="/api/v1/notifications",
    tags=["notifications"],
)
# Deep Investigation runs. Read-only for a stronger reason than decks
# and reports: the state machine in ``investigation_state`` is what guarantees
# a failed source can never be stored as a success, so a client-writable
# surface would be a way around that guarantee. This endpoint is also what
# makes the live block robust — it renders identically from the record alone,
# so the streamed frames are an optimisation rather than the carrier.
# Behind "DEEP_INVESTIGATION": off, every route answers 403 and none
# is documented.
application.include_router(
    investigations.router,
    prefix="/api/v1/investigations",
    tags=["investigations"],
    dependencies=[Depends(require_feature("DEEP_INVESTIGATION"))],
    include_in_schema=is_feature_enabled("DEEP_INVESTIGATION"),
)
# The packs users build for Deep Investigation: behind the same flag.
application.include_router(
    investigation_packs.router,
    prefix="/api/v1/investigation-packs",
    tags=["investigation-packs"],
    dependencies=[Depends(require_feature("DEEP_INVESTIGATION"))],
    include_in_schema=is_feature_enabled("DEEP_INVESTIGATION"),
)
# The cross-context file repository behind Work > Files. Read + delete
# only: files are still written by the surfaces that produce them (chat upload,
# generate_file, run outputs), this is where they are found afterwards.
application.include_router(files.router, prefix="/api/v1/files", tags=["files"])
application.include_router(prompts.router, prefix="/api/v1/prompts", tags=["prompts"])

# --- Visual flow builder ---
# Mounted unconditionally, documented only when "FLOW_BUILDER" is on. The
# feature is not announced externally yet and the published schema at
# /api/openapi.json is external communication like any other, but the routes
# themselves must keep answering: the agent-creation wizard reads the flow node
# catalog, so the flag is UI-only (see dev-docs/flow-builder-feature-flag.md).
#
# ``include_in_schema`` is what actually hides them. Dropping the "flows" entry
# from OPENAPI_TAGS (further up) only removes the group's description — every
# path, request/response schema and operation id would stay in the document,
# which is the whole of what there is to read. Both are needed: the tag list
# without the operations renders as an empty section.
_FLOWS_IN_SCHEMA = is_feature_enabled("FLOW_BUILDER")
application.include_router(
    flows.router,
    prefix="/api/v1/flows",
    tags=["flows"],
    include_in_schema=_FLOWS_IN_SCHEMA,
)
application.include_router(
    flow_runs.router,
    prefix="/api/v1/flows",
    tags=["flows"],
    include_in_schema=_FLOWS_IN_SCHEMA,
)
application.include_router(
    flow_event_types.router,
    prefix="/api/v1/flow-event-types",
    tags=["flows"],
    include_in_schema=_FLOWS_IN_SCHEMA,
)

application.include_router(
    nova_rules.router, prefix="/api/v1/nova-rules", tags=["nova-rules"]
)
application.include_router(intents.router, prefix="/api/v1", tags=["intents"])
application.include_router(users.router, prefix="/api/v1/users", tags=["users"])
application.include_router(groups.router, prefix="/api/v1/groups", tags=["groups"])
# SCIM 2.0 provisioning: the identity provider's surface (a SCIM token, nothing
# else) and the administrator's tokens (api/v1/scim.py, dev-docs/scim.md).
application.include_router(scim.scim_router, prefix="/api/v1/scim/v2", tags=["scim"])
application.include_router(scim.tokens_router, prefix="/api/v1/scim/tokens", tags=["scim"])
application.include_router(insights.router, prefix="/api/v1/insights", tags=["insights"])
application.include_router(
    audit_logs.router, prefix="/api/v1/audit-logs", tags=["audit-logs"]
)
application.include_router(
    admin_objects.router, prefix="/api/v1/admin/objects", tags=["admin-objects"]
)
application.include_router(hooks.router, prefix="/api/v1/hooks", tags=["hooks"])
# This platform's registration with the XTM Hub (Settings > Platform, the
# home dashboard pill); the heartbeat is step 14c of the autonomous tick.
application.include_router(
    xtm_hub_registration.router, prefix="/api/v1/xtm-hub", tags=["xtm-hub"]
)
application.include_router(
    security_audit.router, prefix="/api/v1/security-audit", tags=["security-audit"]
)
# Browser-sent Content-Security-Policy violation reports: no auth,
# bounded, rate limited, logged aggregated (``services/csp_reports.py``).
application.include_router(csp_reports.router, prefix=CSP_REPORT_PATH)
application.include_router(chat.router, prefix="/api/v1/chat", tags=["chat"])
# The web chat over HTTP + SSE, on the embedded chat's relative paths.
application.include_router(chat_web_stream.router, prefix=chat_web_stream.WEB_CHAT_PREFIX, tags=["chat"])
application.include_router(chat_continuation.router, prefix="/api/v1/chat", tags=["chat"])
application.include_router(chat_export.router, prefix="/api/v1/chat", tags=["chat"])
application.include_router(chat_commands.router, prefix="/api/v1/chat", tags=["chat"])
# The waiting arcade: mounted unconditionally, every route 404s when
# ARCADE_ENABLED=false (router-level dependency).
application.include_router(arcade.router, prefix="/api/v1/arcade", tags=["arcade"])
application.include_router(ai.router, prefix="/api/v1/ai", tags=["ai"])
application.include_router(runners.router, prefix="/api/v1/runners", tags=["runners"])
application.include_router(panic.router, prefix="/api/v1/panic", tags=["panic"])
# On-premise relays: every route 404s while relays are off.
application.include_router(relays.router, prefix="/api/v1/relays", tags=["relays"])
# XTM One Desktop browser sign-in handoff: runner-authenticated start /
# exchange, user-authenticated complete.
application.include_router(
    desktop_login.router, prefix="/api/v1/desktop-login", tags=["runners"]
)
application.include_router(extension.router, prefix="/api/v1/extension", tags=["extension"])
application.include_router(
    api_keys.router, prefix="/api/v1/api-keys", tags=["api-keys"]
)
application.include_router(
    api_keys.admin_router, prefix="/api/v1/admin/api-keys", tags=["api-keys"]
)
application.include_router(
    ai_adoption.router, prefix="/api/v1/ai-adoption", tags=["ai-adoption"]
)
application.include_router(
    ctem_metrics.router, prefix="/api/v1/ctem-metrics", tags=["ctem-metrics"]
)
application.include_router(ctem_cases.router, prefix="/api/v1/ctem-cases", tags=["ctem-cases"])
application.include_router(ctem_signals.router, prefix="/api/v1/ctem-signals", tags=["ctem-signals"])
application.include_router(
    organization_context.router,
    prefix="/api/v1/organization-context",
    tags=["organization-context"],
)
application.include_router(
    organizations.router,
    prefix="/api/v1/organizations",
    tags=["organizations"],
)
application.include_router(roi.router, prefix="/api/v1/roi", tags=["roi"])
application.include_router(oauth_callbacks.router, prefix="/api/oauth", tags=["oauth"])
application.include_router(auth_sso.router, prefix="/api/auth", tags=["sso"])

# --- OpenAI-compatible LLM proxy (authenticated with user API keys) ---
if settings.is_copilot_like:
    application.include_router(llm_proxy.router, prefix="/v1", tags=["llm-proxy"])

# --- MCP Agent Endpoint (expose agents as MCP tools) ---
application.include_router(mcp_endpoint.router, prefix="/mcp", tags=["mcp-endpoint"])

# --- Platform MCP servers (OpenCTI / OpenAEV business tools via MCP) ---
application.include_router(
    mcp_platform.router, prefix="/mcp", tags=["mcp-platform"]
)

# --- XTM One MCP server (this platform's own tools via MCP) ---
# Always routed, gated at request time by the admin toggle: a disabled server
# must say so rather than 404, the same way the platform servers do.
application.include_router(mcp_xtm.router, prefix="/mcp", tags=["mcp-xtm"])

# --- A2A (external agents task XTM One agents; XTM One agents task partners) ---
# Gated per request by ``require_chat_enabled`` — A2A is offered exactly while
# agentic chat is (AI on, Enterprise Edition); nothing at startup needs
# the answer.
#
# FR-041's automatic revocation is a session-level listener, so it holds no
# matter which code path changes an agent's tier or sharing. Exposure rows
# outlive chat availability, so it runs while A2A is off too: otherwise an
# agent flipped between personal and company-managed in that window would keep
# an exposure whose boundary no longer matches it, and chat coming back would
# resolve it. See ``app.a2a.hooks.register_a2a_hooks``.
a2a_hooks.register_a2a_hooks()

# A ticket a partner's A2A task filed changed status: the partner's callbacks on
# that task are told (identity and status only). See ``app.a2a.ticket_push``.
a2a_ticket_push.register_ticket_push()

# platform.default_model_route.changed: a session listener, so every writer of a
# default model route (Settings page, agent tool, provider deletion, env seed)
# raises it after its commit. See ``app.services.default_model_routes``.
from app.services.default_model_routes import register_default_route_listeners

register_default_route_listeners()

# The partner grant registers itself onto the authorization server, which
# keeps that module free of any import from this feature. Not gated: the task
# surface, where a token is spent, refuses with a stated reason, where this
# grant could only say ``invalid_client``.
a2a_grants.register_a2a_grants()

application.include_router(a2a_server.router, tags=["a2a"])
# The one route a partner may call as part of the OUTBOUND direction. Mounted
# beside the inbound surface because it shares that surface's guards, and at
# the same prefix depth so a deployment fronting ``/a2a`` covers both.
application.include_router(a2a_callbacks.router, tags=["a2a"])
application.include_router(a2a_admin.router, prefix="/api/v1", tags=["a2a-admin"])
application.include_router(
    a2a_client.router, prefix="/api/v1", tags=["a2a-client"]
)
application.include_router(
    a2a_liaisons.router, prefix="/api/v1", tags=["a2a-liaisons"]
)

# --- MCP OAuth 2.1 (authorization flow for MCP endpoints) ---
application.include_router(
    mcp_oauth.well_known_router,
    prefix="/.well-known",
    tags=["mcp-oauth"],
)
application.include_router(mcp_oauth.router, prefix="/mcp/oauth", tags=["mcp-oauth"])

# --- XTM cross-platform JWKS (public, no auth) ---
application.include_router(xtm_auth.router, prefix="/xtm/auth", tags=["xtm-auth"])

# --- Internal catalog (shared resources library, all endpoints require auth) ---
if settings.ai_catalog_enabled:
    application.include_router(
        public_catalog.router, prefix="/api/v1/public", tags=["catalog"]
    )

# --- Public chat (web-embedded agents, wide CORS, no auth) ---
application.include_router(
    public_chat.router, prefix="/api/v1/public", tags=["public-chat"]
)

# --- Public presentation links: the token is the credential, no auth ---
application.include_router(
    presentation_public.router, prefix="/api/v1/public", tags=["presentations"]
)
# A custom dashboard's published link: the token in the path is the credential.
# With "DASHBOARD_STUDIO" off every link answers like an unknown one
# (``dashboard_public_links.public_links_enabled``).
application.include_router(
    dashboard_public.router,
    prefix="/api/v1/public",
    tags=["custom-dashboards"],
    include_in_schema=is_feature_enabled("DASHBOARD_STUDIO"),
)
# A video's public link: the token in the path is the credential.
# With "VIDEO_STUDIO" off every link answers like an unknown one
# (``public_links.public_links_enabled``), and the routes are not documented.
application.include_router(
    video_public.router,
    prefix="/api/v1/public",
    tags=["video-studio"],
    include_in_schema=is_feature_enabled("VIDEO_STUDIO"),
)

# --- Webhook receiver (external triggers for assignments) ---
application.include_router(webhooks.router, prefix="/api/hooks", tags=["webhooks"])

# --- Channel webhooks (external messaging platforms) ---
application.include_router(
    slack_channel.router, prefix="/api/channels", tags=["channels"]
)
application.include_router(
    teams_channel.router, prefix="/api/channels", tags=["channels"]
)
application.include_router(
    whatsapp_channel.router, prefix="/api/channels", tags=["channels"]
)
application.include_router(
    telegram_channel.router, prefix="/api/channels", tags=["channels"]
)
application.include_router(
    discord_channel.router, prefix="/api/channels", tags=["channels"]
)
application.include_router(
    googlechat_channel.router, prefix="/api/channels", tags=["channels"]
)


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------

# Dedicated single-worker executor for the S3 readiness check.
#
# asyncio.wait_for cancels the awaiting coroutine after the per-check
# timeout, but a synchronous bucket_exists() call that already started
# keeps its thread busy until MinIO's socket times out (typically 60 s+).
# Running the check on the shared default executor therefore let stuck
# threads accumulate across probe cycles (every 10 s in k8s) until the
# pool — which also serves asyncpg DNS resolution — was exhausted and
# every new DB connection timed out too.
#
# A dedicated max_workers=1 executor caps the damage at ONE stuck thread
# process-wide: while it is busy, later probes queue behind it and their
# pending work items are cancelled by the per-check timeout before they
# ever run, so nothing accumulates and the default executor is untouched.
#
# Created lazily on first use and shut down in the lifespan teardown
# (wait=False + cancel_futures=True, mirroring shutdown_default_executor)
# so hot-reload / repeated app instantiation in the same process cannot
# leak workers, while a post-shutdown probe can still lazily recreate it.
_s3_health_executor: ThreadPoolExecutor | None = None


def _get_s3_health_executor() -> ThreadPoolExecutor:
    """Return the dedicated S3 health-check executor, creating it if needed.

    Only ever called from the event loop thread (the readiness endpoint),
    so no locking is required.
    """
    global _s3_health_executor
    if _s3_health_executor is None:
        _s3_health_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="s3-health-check"
        )
    return _s3_health_executor


def _shutdown_s3_health_executor() -> None:
    """Best-effort shutdown of the S3 health-check executor (lifespan teardown).

    ``wait=False`` + ``cancel_futures=True`` so a worker still blocked on a
    hung MinIO socket can never make shutdown itself hang.
    """
    global _s3_health_executor
    executor = _s3_health_executor
    _s3_health_executor = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


@application.get("/api/health")
async def health():
    """Basic liveness check – always returns quickly."""
    return {"status": "ok"}


@application.get("/api/health/ready")
async def health_ready():
    """Deep readiness check – verifies startup is done and dependencies are reachable.

    Returns 503 while the background startup task (migrations, seeding, …) is
    running or has failed, and also 503 if DB / Redis are unreachable.

    Use this for Kubernetes readiness probes or load balancer health checks.
    The entire dependency check is bounded by a 5-second timeout so that a
    slow or unresponsive dependency (e.g. MinIO during a network partition)
    cannot block the asyncio event loop and cause the *liveness* probe to
    fail too.
    """
    from fastapi import Response as _Resp

    # Quick startup gate check — no DB/Redis work needed.
    if not _startup_done.is_set():
        return _Resp(
            content=json.dumps(
                {
                    "status": "starting",
                    "startup_error": _startup_error,
                    "startup_retrying": _startup_retrying,
                }
            ),
            status_code=503,
            media_type="application/json",
        )

    from app.core.database import engine
    from app.core.redis import redis_client

    async def _run_checks() -> tuple[dict, bool]:
        checks: dict = {}
        healthy = True

        from sqlalchemy import text

        # Each check has its own per-check timeout, set just below the outer
        # 5 s envelope, so a single hanging dependency (e.g. DB pool exhaustion
        # after many hours of uptime) cannot consume the full budget before the
        # other checks even start.  All three checks run concurrently.
        per_check_timeout = 4.0

        async def _check_database() -> str:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return "ok"

        async def _check_redis() -> str:
            r = await redis_client()
            # redis-py 7.x stubs type ping() as Awaitable[bool]|bool,
            # fixed in 8.0. cast() is the correct workaround for 7.x.
            await cast(Awaitable[bool], r.ping())
            return "ok"

        async def _check_s3() -> str:
            from app.services.file_storage import file_storage

            # bucket_exists() is a synchronous urllib3 call; run it on the
            # dedicated single-worker executor so it never blocks the event
            # loop and a hung MinIO can pin at most one thread process-wide
            # (see _s3_health_executor above).
            def _bucket_exists() -> bool:
                return file_storage.client.bucket_exists(file_storage._bucket)

            exists = await asyncio.get_running_loop().run_in_executor(
                _get_s3_health_executor(), _bucket_exists
            )
            if not exists:
                raise RuntimeError("bucket not found")
            return "ok"

        # Run all three dependency checks concurrently.  Sequential execution
        # was dangerous: a slow DB connection-pool wait consumed the full
        # outer timeout budget before Redis/S3 could even start.
        results = await asyncio.gather(
            asyncio.wait_for(_check_database(), timeout=per_check_timeout),
            asyncio.wait_for(_check_redis(), timeout=per_check_timeout),
            asyncio.wait_for(_check_s3(), timeout=per_check_timeout),
            return_exceptions=True,
        )
        for name, result in zip(("database", "redis", "s3"), results, strict=True):
            if isinstance(result, TimeoutError):
                # str(TimeoutError()) is empty — spell out what happened.
                checks[name] = f"error: timed out after {per_check_timeout}s"
                healthy = False
            elif isinstance(result, Exception):
                checks[name] = f"error: {result}"
                healthy = False
            else:
                checks[name] = result

        return checks, healthy

    try:
        checks, healthy = await asyncio.wait_for(_run_checks(), timeout=5.0)
    except TimeoutError:
        checks = {
            "status": "degraded",
            "error": "readiness check timed out",
        }
        return _Resp(
            content=json.dumps(checks),
            status_code=503,
            media_type="application/json",
        )

    checks["status"] = "ok" if healthy else "degraded"
    if not healthy:
        return _Resp(
            content=json.dumps(checks),
            status_code=503,
            media_type="application/json",
        )
    return checks


# ---------------------------------------------------------------------------
# Frontend static file serving (Docker unified image only)
#
# When the built frontend exists at /app/frontend_dist (copied by the
# platform Dockerfile), serve it directly from FastAPI.  API routes are
# registered above and always take priority over the catch-all.
# In local dev this directory doesn't exist, so the block is skipped.
#
# The index.html is patched once at import time so link-preview crawlers
# (which don't execute JS) see the correct title, description, and OG tags
# for the configured platform mode (XTM One vs Filigran Copilot).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Office Add-in static files (served at /office/)
# Must be mounted BEFORE the frontend catch-all route below.
# Checks two locations:
#   1. office_addin_dist/ (Docker image, copied by Dockerfile)
#   2. ../apps/microsoft-office/dist/ (local dev after `npm run build`)
# ---------------------------------------------------------------------------

_OFFICE_ADDIN_DIR = Path(__file__).resolve().parent.parent / "office_addin_dist"
if not _OFFICE_ADDIN_DIR.is_dir():
    _OFFICE_ADDIN_DIR = (
        Path(__file__).resolve().parent.parent.parent
        / "apps"
        / "microsoft-office"
        / "dist"
    )

# The task pane loads Office.js from Microsoft's CDNs (which reports telemetry
# to Microsoft) and its fonts from Google Fonts, is framed by Office on the web
# and the new Outlook and opened top-level by the desktop hosts - none of which
# CI can exercise, so its policy is only ever reported, never enforced.
register_page_policy(
    "/office",
    PagePolicy(
        sources={
            "script-src": ("https://appsforoffice.microsoft.com", "https://ajax.aspnetcdn.com"),
            "connect-src": (
                "https://*.office.events.data.microsoft.com",
                "https://browser.pipe.aria.microsoft.com",
            ),
            **GOOGLE_FONTS_SOURCES,
        },
        frame_ancestors="any",
        always_report_only=True,
    ),
    include_subpaths=True,
)

if _OFFICE_ADDIN_DIR.is_dir():
    from starlette.staticfiles import StaticFiles

    application.mount(
        "/office",
        StaticFiles(directory=str(_OFFICE_ADDIN_DIR), html=True),
        name="office-addin",
    )
    logger.info("Office Add-in mounted at /office/ from %s", _OFFICE_ADDIN_DIR)

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend_dist"

def _refresh_index_html() -> None:
    """Re-patch the cached frontend index.html using current branding.

    Safe to call from sync contexts (uses the branding sync cache).
    """
    refresh_configured_branded_index_html()


# Initial in-process render uses env-default branding (cache is not yet
# primed when this module is imported).  The lifespan hook re-renders it
# once the DB-stored branding is loaded.
configure_branded_index_html(_FRONTEND_DIR)


_SAFE_STATIC_RE = re.compile(r"^[\w][\w.\-/]*$")

# Machine-facing namespaces that must NEVER fall through to the SPA
# index.html: REST API + webhooks + channels + health (``/api``), the
# OpenAI-compatible LLM proxy (``/v1``), the MCP agent endpoint (``/mcp``),
# the cross-platform JWKS endpoint (``/xtm``), and OAuth discovery metadata
# (``/.well-known``).  An unknown path under these prefixes (e.g. an endpoint
# added in a newer release hit by an up-to-date client against an older
# deployment) must fail as a JSON 404 — returning HTTP 200 + HTML makes
# API clients choke on parsing instead of degrading cleanly (e.g. the
# OpenCTI / OpenAEV chatbot proxies listing chat sessions).
#
# ``a2a`` is here for a sharper reason than the others. A2A Agent Cards live
# at ``/a2a/{token}/.well-known/agent-card.json``, so the *first* segment is
# ``a2a`` and the ``.well-known`` entry above does not cover them. Every
# negative outcome on that surface — unknown token, revoked exposure,
# disabled agent — is required to be an indistinguishable JSON 404. Without this entry each of them instead returns HTTP 200 + the
# SPA's index.html, which both breaks a partner's registration tooling on
# parse and turns a deliberately uniform "not found" into a distinguishable
# signal.
_API_NAMESPACES = ("api", "v1", "mcp", "xtm", ".well-known", "a2a")

# Vite's hashed build output. A file missing there is a chunk of another
# build - typically asked for by a tab opened before a deploy replaced it - and
# never a page (``assets`` is a reserved first segment, ``core/base_path.py``):
# it fails as a 404, where the SPA page would reach the browser as a script with
# the wrong MIME type and look like a success to everything in between.
# The 404 is never stored: during a rolling deploy the new build's chunk can be
# asked of a replica still serving the old one, and a proxy caching that answer
# would keep refusing the chunk once every replica has it.
_BUILD_ASSETS_SEGMENT = "assets"

# Vite names what it writes under ``assets/`` ``<name>-<hash><ext>``
# (``frontend/vite.config.ts`` keeps the default ``[name]-[hash]`` file names;
# the hash is 8 base64url characters): new bytes get a new URL, so a served
# file is cached for a year without being asked again. Nothing else is: the
# SPA page, the web manifest and the unhashed files copied from ``public/``
# keep their own headers.
_HASHED_BUILD_FILE_RE = re.compile(r"^[^/]+-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+$")
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# The installed web app's service worker (``frontend/public/sw.js``): revalidated
# on every check, so a deployment's new worker replaces the old one at once.
_SERVICE_WORKER_FILE = "sw.js"


def _spa_shell() -> HTMLResponse:
    """The SPA page, revalidated on every load: it names the current build's
    chunks, and a stale copy would keep a reload on the replaced ones."""
    return HTMLResponse(content=get_branded_index_html(), headers={"Cache-Control": "no-cache"})


def _install_spa_catchall(application: FastAPI, frontend_dir: Path) -> None:
    """Register the SPA catch-all route serving the built frontend.

    Kept as a standalone function so tests can install it on a throwaway
    ``FastAPI`` app with a temporary frontend directory (the production
    registration below only happens when ``frontend_dist`` exists at import
    time, which is never the case in the test environment).
    """
    frontend_dir_resolved = frontend_dir.resolve()

    @application.get("/{full_path:path}", include_in_schema=False)
    async def _serve_frontend(full_path: str):
        head = full_path.split("/", 1)[0]
        if head in _API_NAMESPACES:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        if full_path in ("", "index.html"):
            return _spa_shell()
        if full_path == "site.webmanifest":
            return RedirectResponse(
                url=with_base_path("/api/v1/platform/manifest.webmanifest"),
                status_code=307,
            )
        if full_path and _SAFE_STATIC_RE.fullmatch(full_path) and ".." not in full_path:
            file = (frontend_dir / full_path).resolve()
            if file.is_file() and file.is_relative_to(frontend_dir_resolved):
                if head == _BUILD_ASSETS_SEGMENT and _HASHED_BUILD_FILE_RE.fullmatch(full_path[len(head) + 1 :]):
                    return FileResponse(file, headers={"Cache-Control": _IMMUTABLE_CACHE_CONTROL})
                if full_path == _SERVICE_WORKER_FILE:
                    return FileResponse(file, headers={"Cache-Control": "no-cache"})
                return FileResponse(file)
            if head == _BUILD_ASSETS_SEGMENT:
                return JSONResponse(
                    status_code=404, content={"detail": "Not Found"}, headers={"Cache-Control": "no-store"}
                )
        return _spa_shell()


if _FRONTEND_DIR.is_dir():
    _install_spa_catchall(application, _FRONTEND_DIR)
