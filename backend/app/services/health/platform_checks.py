# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Platform Health XTM One platform checks (#101 S4a, ``dev-docs/platform-health.md`` section 4.2).

The configuration checks of the XTM One platform itself, registered on the
engine when this module is imported:

=================================  ==========  =============  ==============================================
Check                              Severity    Category       Fails when
=================================  ==========  =============  ==============================================
``xtm_one.registration_token``     critical    security       ``PLATFORM_REGISTRATION_TOKEN`` is the published default
``xtm_one.licence``                critical    licence        a Filigran licence is installed and refused
``xtm_one.licence_expiry``         warning     licence        the validated Filigran licence expires within 30 days
``xtm_one.smtp``                   info        configuration  no SMTP host is configured
``xtm_one.hub_registration``       info        configuration  XTM Hub is not registered, or its registration is not fine
``xtm_one.integration_broken``     warning     availability   a company-managed integration is in error, to re-authorize, or failing its test
``xtm_one.mcp_server_broken``      warning     availability   an enabled company-managed MCP server's OAuth refresh failed
``xtm_one.kb_indexing_stalled``    warning     performance    a knowledge base has documents processing for over 1 hour
``xtm_one.kb_stale_embeddings``    warning     configuration  a knowledge base was embedded with another model than the platform's
``xtm_one.default_model``          critical    configuration  no default model is set, so agents without a model of their own cannot run
``xtm_one.embedding_model``        critical    configuration  no embedding model is set, so knowledge bases cannot index or search
``xtm_one.quota_near_ceiling``     warning     capacity       a platform-wide agentic ceiling is over 90 % used in its current period
``xtm_one.dependency_unreachable`` critical    availability   Redis or the object storage does not answer its probe within 4 s
``xtm_one.trace_backlog``          warning     capacity       over 1,000 traces wait in the tracing overflow queue
``xtm_one.worker_absent``          critical    availability   no worker says it is running, so queued runs, renders and indexing wait
``xtm_one.provider_error_rate``    warning     availability   over 5 % of a model's traces of the last hour failed (20 traces at least)
``xtm_one.maintenance_stalled``    warning     availability   the hourly maintenance (retention sweeps, purges) has not completed in two hours
``xtm_one.version_behind``         info/warn   configuration  a newer XTM One release is published (two or more: warning), read daily
``xtm_one.certificate_expiry``     warning     security       the TLS certificate of the public URL expires within 21 days or is refused, read daily
=================================  ==========  =============  ==============================================

The per-subject checks answer one result per subject (an integration, a server, a
knowledge base) and close out the open findings whose subject they no longer
read (:func:`app.services.health.engine.close_out`), so fixing or deleting the
subject resolves its finding.

**Mode gate** (section 1.1): XTM One platform health is domain content. It
runs in the XTM One modes (``xtm_one`` and ``ai_disabled``) and records nothing
in the Copilot modes (``settings.is_copilot_like``), so no XTM One finding ever exists there.
Unlike the agent checks it does not read the AI switch: these reads run with AI
switched off too (``ai_disabled``). Each check reads a setting or one row,
calls no model and is never metered; only the two daily checks below reach
the network.

The quota check reads the counters the gates read and moves none of them.

The dependency probe is the readiness probe of ``/health/ready`` (a Redis
``PING``, the bucket of the object storage), each bounded by
:data:`PROBE_TIMEOUT_SECONDS`; PostgreSQL needs no probe, the pass itself runs
on it. The trace backlog reads the length of the tracing overflow list.

The worker check reads the presence every worker publishes in Redis
(:mod:`app.services.worker_presence`); an unreadable Redis records nothing, that
is the dependency finding. The provider check counts the traces of the last
:data:`PROVIDER_ERROR_WINDOW` per model in PostgreSQL (every trace store writes
there), one finding per model, closed out when the model no longer fails.

The two daily checks (:data:`DAILY_CHECK_INTERVAL`) are the only ones that
leave the platform: the version check reads the public release list of
:data:`RELEASES_URL` and the certificate check opens a TLS handshake with the
host of ``FRONTEND_URL`` (verified as ``web_fetch`` verifies, administrator
trusted certificates included). A read that cannot reach its source (an
air-gapped platform, a URL the backend cannot resolve) records nothing.

The sandbox create latency needs a store every process writes (the
``xtmone_sandbox_create_seconds`` histogram is per process) and follows in a
later slice; the security audit stays its own page.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.docs_links import docs_url
from app.services.health.engine import (
    Check,
    CheckResult,
    apply_grace,
    close_out,
    in_startup_grace,
    register_check,
)

MODULE = "xtm_one"
#: How often the platform checks run (section 3, "Scheduling": aligned on the registration heartbeat).
PLATFORM_CHECK_INTERVAL = timedelta(minutes=5)
#: A validated licence expiring sooner than this raises a warning.
LICENCE_EXPIRY_WARNING = timedelta(days=30)
#: Documents processing for longer than this mean a stalled indexing worker.
KB_STALLED_AFTER = timedelta(hours=1)
#: The integration states that stop its tools (as the agent check reads them).
BROKEN_INTEGRATION_STATUSES = frozenset({"error", "reauthorize"})
#: A platform-wide agentic ceiling used past this share of its period raises a warning.
QUOTA_WARNING_RATIO = 0.9
#: A dependency probe slower than this counts as unreachable (the readiness probe's bound).
PROBE_TIMEOUT_SECONDS = 4.0
#: Traces waiting in the tracing overflow list past this count raise a warning.
TRACE_BACKLOG_WARNING = 1000
#: The window the provider error rate is read over.
PROVIDER_ERROR_WINDOW = timedelta(hours=1)
#: A model whose failed share of its traces in the window passes this raises a warning.
PROVIDER_ERROR_RATIO = 0.05
#: Fewer traces than this in the window are too few to judge a model.
PROVIDER_MIN_TRACES = 20
#: How often the slow checks run (section 3, "Scheduling": version behind, certificate expiry).
DAILY_CHECK_INTERVAL = timedelta(days=1)
#: The public release list of XTM One the version check reads.
RELEASES_URL = "https://api.github.com/repos/XTM-One-Platform/xtm-one/releases"
#: Newer releases from which being behind is a warning (one newer release is info).
VERSION_BEHIND_WARNING = 2
#: A certificate of the public URL expiring sooner than this raises a warning.
CERTIFICATE_EXPIRY_WARNING = timedelta(days=21)
#: The dependencies probed, in the order findings are recorded.
PROBED_DEPENDENCIES = ("redis", "object_storage")
#: Where a finding of these checks is explained.
DOC_LINK = docs_url("administration/health", "platform-checks")

PlatformRead = Callable[[AsyncSession, datetime], Awaitable[list[CheckResult]]]


def _result(
    check_id: str, severity: str, category: str, failing: bool, subject: dict[str, str] | None = None, **kw: Any
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        module=MODULE,
        category=category,
        severity=severity,
        failing=failing,
        subject=subject or {},
        doc_link=DOC_LINK,
        **kw,
    )


def _platform_check(read: PlatformRead) -> Callable[[AsyncSession, datetime], Awaitable[list[CheckResult]]]:
    """Run ``read`` in the XTM One modes only (section 1.1); Copilot mode records nothing."""

    async def run(db: AsyncSession, now: datetime) -> list[CheckResult]:
        from app.core.config import settings

        if settings.is_copilot_like:
            return []
        results = list(await read(db, now))
        if in_startup_grace(now):
            # XTM One just started: workers and dependencies still boot (#958).
            return apply_grace(results, lambda r: r.module == MODULE)
        return results

    run.__name__ = read.__name__
    run.__doc__ = read.__doc__
    return run


async def registration_token(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """``PLATFORM_REGISTRATION_TOKEN`` is the public default value, which authenticates nobody."""
    from app.core.registration_token import registration_token_state

    state = registration_token_state()
    return [
        _result(
            "xtm_one.registration_token",
            "critical",
            "security",
            state == "published_default",
            evidence={"values": {"state": state}, "source": "registration_token_state"},
            recommendation=(
                "Set PLATFORM_REGISTRATION_TOKEN to a random secret here and on every connected "
                "OpenCTI, OpenAEV or OpenCRQ platform, then restart them."
            ),
        )
    ]


async def _licence(db: AsyncSession) -> tuple[bool, Any]:
    """``(a licence is installed, its decoded info)`` for the Filigran licence of this platform."""
    from app.core.config import settings
    from app.models.platform_settings import PlatformSettings
    from app.services.xtm_license import resolve_xtm_license

    ps = (
        await db.execute(
            select(PlatformSettings).order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc()).limit(1)
        )
    ).scalar_one_or_none()
    installed = bool(settings.enterprise_license or (ps.enterprise_license if ps else None))
    return installed, resolve_xtm_license(ps)


async def licence(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """A Filigran licence is installed and refused (no licence installed passes: Community Edition)."""
    installed, info = await _licence(db)
    refused = installed and not info.license_validated
    return [
        _result(
            "xtm_one.licence",
            "critical",
            "licence",
            refused,
            evidence={
                "values": {"installed": installed, "validated": bool(info.license_validated)},
                "reason": info.license_refusal_reason if refused else "",
            },
            recommendation="Install a valid Filigran licence in Settings > Platform, or contact Filigran.",
        )
    ]


async def licence_expiry(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """The validated Filigran licence expires within :data:`LICENCE_EXPIRY_WARNING`."""
    _installed, info = await _licence(db)
    if not info.license_validated:
        return [
            _result("xtm_one.licence_expiry", "warning", "licence", False, evidence={"values": {"validated": False}})
        ]
    expires = info.license_expiration_date
    left = expires - now
    return [
        _result(
            "xtm_one.licence_expiry",
            "warning",
            "licence",
            left < LICENCE_EXPIRY_WARNING,
            evidence={
                "values": {"expires": expires.isoformat(), "days_left": max(0, left.days)},
                "threshold": f"{LICENCE_EXPIRY_WARNING.days} days",
            },
            recommendation="Renew the Filigran licence before it expires.",
        )
    ]


async def smtp(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """No SMTP host is configured: no email leaves the platform (alerts, invitations, digests)."""
    from app.services.email import is_email_configured

    configured = is_email_configured()
    return [
        _result(
            "xtm_one.smtp",
            "info",
            "configuration",
            not configured,
            evidence={"values": {"configured": configured}, "source": "SMTP_HOSTNAME"},
            recommendation="Set SMTP_HOSTNAME and its credentials so the platform can send email.",
        )
    ]


async def hub_registration(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """XTM Hub is not registered, or its registration lost connectivity or needs registering again."""
    from app.models.xtm_hub_registration import STATUS_REGISTERED, XtmHubRegistration

    status = (await db.execute(select(XtmHubRegistration.status).limit(1))).scalar_one_or_none()
    return [
        _result(
            "xtm_one.hub_registration",
            "info",
            "configuration",
            status != STATUS_REGISTERED,
            evidence={"values": {"status": status or "unregistered"}},
            recommendation="Register this platform with XTM Hub from Settings, or register it again.",
        )
    ]


async def integration_broken(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """A company-managed integration is in error, to re-authorize, or failed its last connection test."""
    from app.models.integration import Integration

    rows = (
        await db.execute(
            select(Integration.id, Integration.provider, Integration.status, Integration.connection_test_status).where(
                # access_restricted: bypass - an administrator health signal on every company-managed
                # integration, read for no acting user; lists nothing to a caller.
                Integration.is_company_managed.is_(True)
            )
        )
    ).all()
    results = [
        _result(
            "xtm_one.integration_broken",
            "warning",
            "availability",
            status in BROKEN_INTEGRATION_STATUSES or tested == "error",
            subject={"integration_id": str(iid)},
            evidence={"values": {"provider": provider, "status": status, "connection_test": tested}},
            recommendation="Re-authorize the integration or fix its credentials, then test its connection.",
        )
        for iid, provider, status, tested in rows
    ]
    return results + await close_out(db, "xtm_one.integration_broken", results)


async def mcp_server_broken(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """An enabled company-managed MCP server's OAuth token refresh failed (``oauth_status`` error)."""
    from app.models.mcp_server import MCPServer

    rows = (
        await db.execute(
            select(MCPServer.id, MCPServer.oauth_status).where(
                MCPServer.is_company_managed.is_(True), MCPServer.enabled.is_(True)
            )
        )
    ).all()
    results = [
        _result(
            "xtm_one.mcp_server_broken",
            "warning",
            "availability",
            oauth_status == "error",
            subject={"mcp_server_id": str(sid)},
            evidence={"values": {"oauth_status": oauth_status}},
            recommendation="Connect the MCP server again so its OAuth token is renewed.",
        )
        for sid, oauth_status in rows
    ]
    return results + await close_out(db, "xtm_one.mcp_server_broken", results)


async def kb_indexing_stalled(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """A knowledge base has documents in ``processing`` untouched for over :data:`KB_STALLED_AFTER`."""
    from app.models.knowledge_base import KnowledgeDocument

    rows = (
        await db.execute(
            select(KnowledgeDocument.knowledge_base_id, func.count())
            .where(KnowledgeDocument.status == "processing", KnowledgeDocument.updated_at < now - KB_STALLED_AFTER)
            .group_by(KnowledgeDocument.knowledge_base_id)
        )
    ).all()
    results = [
        _result(
            "xtm_one.kb_indexing_stalled",
            "warning",
            "performance",
            True,
            subject={"knowledge_base_id": str(kb_id)},
            evidence={"values": {"documents": count}, "threshold": "1 hour"},
            recommendation="Check that the background worker runs, then reprocess the stalled documents.",
        )
        for kb_id, count in rows
    ]
    return results + await close_out(db, "xtm_one.kb_indexing_stalled", results)


async def kb_stale_embeddings(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """A knowledge base's embedding model is not the platform's, so its search reads stale vectors."""
    from app.models.knowledge_base import KnowledgeBase
    from app.models.platform_settings import PlatformSettings

    expected = (
        await db.execute(
            select(PlatformSettings.embedding_model)
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    results: list[CheckResult] = []
    if expected:
        rows = (await db.execute(select(KnowledgeBase.id, KnowledgeBase.embedding_model))).all()
        results = [
            _result(
                "xtm_one.kb_stale_embeddings",
                "warning",
                "configuration",
                (model or "") != expected,
                subject={"knowledge_base_id": str(kb_id)},
                evidence={"values": {"model": model, "platform_model": expected}},
                recommendation="Re-embed the knowledge base with the platform's embedding model.",
            )
            for kb_id, model in rows
        ]
    return results + await close_out(db, "xtm_one.kb_stale_embeddings", results)


async def _platform_settings(db: AsyncSession) -> Any:
    from app.models.platform_settings import PlatformSettings

    return (
        await db.execute(
            select(PlatformSettings).order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc()).limit(1)
        )
    ).scalar_one_or_none()


async def default_model(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """No platform default model: an agent with no model of its own has nothing to run on."""
    from app.llm.router import get_platform_default_model

    model = await get_platform_default_model(db)
    return [
        _result(
            "xtm_one.default_model",
            "critical",
            "configuration",
            not model,
            evidence={"values": {"model": model or "unset"}, "source": "agent_default_model"},
            recommendation="Choose a default model in Settings > Platform.",
        )
    ]


async def embedding_model(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """No platform embedding model: knowledge bases can neither index nor search."""
    ps = await _platform_settings(db)
    model = getattr(ps, "embedding_model", None)
    return [
        _result(
            "xtm_one.embedding_model",
            "critical",
            "configuration",
            not model,
            evidence={"values": {"model": model or "unset"}, "source": "embedding_model"},
            recommendation="Choose an embedding model in Settings > Platform.",
        )
    ]


async def quota_near_ceiling(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """A platform-wide agentic ceiling is used past :data:`QUOTA_WARNING_RATIO` in its current period."""
    from app.services.quota import platform_agentic_usage

    results = [
        _result(
            "xtm_one.quota_near_ceiling",
            "warning",
            "capacity",
            row["used"] >= QUOTA_WARNING_RATIO * row["limit"],
            subject={"period": row["period"]},
            evidence={"values": dict(row), "threshold": "90 %"},
            recommendation=(
                "Agentic runs stop for everyone once the ceiling is reached: raise it with Filigran "
                "or reduce the scheduled and event-driven runs."
            ),
        )
        for row in await platform_agentic_usage(db, now)
    ]
    return results + await close_out(db, "xtm_one.quota_near_ceiling", results)


async def _probe_redis() -> None:
    from app.core.redis import redis_client

    client = await redis_client()
    # redis-py 7.x stubs type ping() as Awaitable[bool] | bool (as /health/ready reads it).
    await cast(Awaitable[bool], client.ping())


async def _probe_object_storage() -> None:
    import asyncio

    from app.services.file_storage import file_storage

    if not await asyncio.to_thread(lambda: file_storage.client.bucket_exists(file_storage._bucket)):
        raise RuntimeError("bucket not found")


_PROBES: dict[str, Callable[[], Awaitable[None]]] = {
    "redis": _probe_redis,
    "object_storage": _probe_object_storage,
}


async def _probe(name: str) -> str:
    """``"ok"`` or the error the probe of ``name`` met, bounded by :data:`PROBE_TIMEOUT_SECONDS`."""
    import asyncio

    try:
        await asyncio.wait_for(_PROBES[name](), timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return f"timed out after {PROBE_TIMEOUT_SECONDS:g} s"
    except Exception as exc:  # any failure is the finding
        return f"{type(exc).__name__}: {exc}"[:200]
    return "ok"


async def dependency_unreachable(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """Redis or the object storage does not answer: runs, files and sessions stop."""
    results = []
    for name in PROBED_DEPENDENCIES:
        outcome = await _probe(name)
        results.append(
            _result(
                "xtm_one.dependency_unreachable",
                "critical",
                "availability",
                outcome != "ok",
                subject={"dependency": name},
                evidence={"values": {"probe": outcome}, "source": "readiness probe"},
                recommendation=f"Check that {name.replace('_', ' ')} is up and reachable from the backend and the worker.",
            )
        )
    return results


async def _trace_backlog_length() -> int:
    """Traces waiting in the tracing overflow list (a read, nothing popped)."""
    from app.core.redis import redis_client
    from app.services.tracing.dispatcher import OVERFLOW_KEY

    client = await redis_client()
    return int(await cast(Awaitable[int], client.llen(OVERFLOW_KEY)) or 0)


async def trace_backlog(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """Traces pile up in the overflow list: the trace store is slow or down, analytics lag."""
    from app.services.tracing.dispatcher import OVERFLOW_KEY

    try:
        waiting = await _trace_backlog_length()
    except Exception:  # Redis down is the dependency check's finding
        return []
    return [
        _result(
            "xtm_one.trace_backlog",
            "warning",
            "capacity",
            waiting > TRACE_BACKLOG_WARNING,
            evidence={"values": {"waiting": waiting}, "threshold": TRACE_BACKLOG_WARNING, "source": OVERFLOW_KEY},
            recommendation="Check the trace store: traces are queued until it accepts them again.",
        )
    ]


async def _running_workers() -> int | None:
    """Workers whose presence says running now; ``None`` when Redis cannot be read."""
    from app.services import worker_presence

    return await worker_presence.count()


async def worker_absent(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """No worker is running: queued runs, renders and indexing wait until one starts."""
    running = await _running_workers()
    if running is None:  # Redis down is the dependency check's finding
        return []
    return [
        _result(
            "xtm_one.worker_absent",
            "critical",
            "availability",
            running == 0,
            evidence={"values": {"running": running}, "source": "worker presence"},
            recommendation="Start the worker deployment, or read its logs for why it stopped.",
        )
    ]


async def maintenance_stalled(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """The hourly maintenance has not completed in two intervals: files, purges and retention pile up."""
    from app.services import wall_clock_schedule

    job = wall_clock_schedule.HOURLY_MAINTENANCE
    state = await wall_clock_schedule.status(job, now=now)
    if state.since is None and state.last_completed_at is None:  # never scheduled yet, or Redis unreadable
        return []
    # The row reads what is true (UX round 3 B1, B2 on #769): when the job became overdue (two
    # intervals after its last completed pass, or after it was first scheduled), and the last
    # completed pass only when one exists, never an English "never" printed as a value.
    reference = state.last_completed_at or state.since
    overdue_since = reference + 2 * job.interval if state.overdue and reference else None
    # A literal "values" block, so the frontend label gate (health-modules.test.ts) sees its keys;
    # an absent value is left out rather than printed as "null".
    evidence = {
        "values": {
            "last_completed_at": state.last_completed_at.isoformat() if state.last_completed_at else None,
            "overdue_since": overdue_since.isoformat() if overdue_since else None,
        },
        "threshold": "2 hours",
        "source": "scheduler schedule hourly_maintenance",
    }
    evidence["values"] = {key: value for key, value in evidence["values"].items() if value is not None}
    if state.last_completed_at:
        recommendation = (
            f"Hourly maintenance has not run since {state.last_completed_at.isoformat()}: read the web process "
            "logs for 'Schedule of hourly_maintenance' and the errors of the scheduler tick."
        )
    else:
        recommendation = (
            "Hourly maintenance has never completed since it was first scheduled: read the web process logs "
            "for 'Schedule of hourly_maintenance' and the errors of the scheduler tick."
        )
    return [
        _result(
            "xtm_one.maintenance_stalled",
            "warning",
            "availability",
            state.overdue,
            evidence=evidence,
            recommendation=recommendation,
        )
    ]


async def _model_trace_counts(db: AsyncSession, since: datetime) -> list[tuple[str, int, int]]:
    """``(model, traces, failed)`` per model of the traces started since ``since``."""
    from app.models.llm_trace import LLMTrace

    rows = await db.execute(
        select(
            LLMTrace.model,
            func.count(LLMTrace.id),
            func.count(LLMTrace.id).filter(LLMTrace.status == "error"),
        )
        .where(LLMTrace.started_at >= since, LLMTrace.model.is_not(None))
        .group_by(LLMTrace.model)
        .order_by(LLMTrace.model)
    )
    return [(str(model), int(total), int(failed)) for model, total, failed in rows.all()]


async def provider_error_rate(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """A model fails over :data:`PROVIDER_ERROR_RATIO` of its traces of the last hour."""
    results = [
        _result(
            "xtm_one.provider_error_rate",
            "warning",
            "availability",
            failed > PROVIDER_ERROR_RATIO * total,
            subject={"model": model},
            evidence={
                "values": {"traces": total, "failed": failed, "rate": round(failed / total, 3)},
                "threshold": "5 %",
                "source": "traces of the last hour",
            },
            recommendation="Check the provider's status and the model's credentials, or switch the agents to another model.",
        )
        for model, total, failed in await _model_trace_counts(db, now - PROVIDER_ERROR_WINDOW)
        if total >= PROVIDER_MIN_TRACES
    ]
    return results + await close_out(db, "xtm_one.provider_error_rate", results)


def _version_core(raw: str) -> tuple[int, int, int] | None:
    """The ``(x, y, z)`` core of a version or release tag (``1.261119.1``, ``v1.2.3-dev.abc``); ``None`` otherwise."""
    import re

    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", raw.strip())
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


async def _released_versions() -> list[str] | None:
    """The tags of the published XTM One releases (drafts and pre-releases left out); ``None`` when unreadable."""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True, headers={"Accept": "application/vnd.github+json"}
        ) as client:
            response = await client.get(RELEASES_URL, params={"per_page": 30})
            response.raise_for_status()
            return [
                str(release.get("tag_name") or "")
                for release in response.json()
                if not release.get("draft") and not release.get("prerelease")
            ]
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return None


async def version_behind(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """A newer XTM One release is published: its fixes and features are not running here."""
    from app.services.xtm_hub_registration import registration_platform_version

    current = registration_platform_version()
    running = _version_core(current)
    if running is None or running == (0, 0, 0):  # a local build has no release to compare
        return []
    released = await _released_versions()
    if released is None:  # no network to the release list: nothing to judge
        return []
    newer = sorted(
        {tag for tag in released if (core := _version_core(tag)) is not None and core > running},
        key=lambda tag: _version_core(tag) or (0, 0, 0),
    )
    return [
        _result(
            "xtm_one.version_behind",
            "warning" if len(newer) >= VERSION_BEHIND_WARNING else "info",
            "configuration",
            bool(newer),
            evidence={
                "values": {"running": current, "newer": newer[-5:], "behind": len(newer)},
                "threshold": f"{VERSION_BEHIND_WARNING} releases behind for a warning",
                "source": RELEASES_URL,
            },
            recommendation="Upgrade XTM One to the latest release; its release notes list what changed.",
        )
    ]


async def _certificate_expiry(host: str, port: int) -> datetime:
    """When the certificate ``host:port`` presents expires; raises when the handshake fails."""
    import asyncio
    import ssl

    from app.core.pinned_request import fetch_ssl_context

    _, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=fetch_ssl_context(), server_hostname=host),
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    try:
        certificate = writer.get_extra_info("peercert") or {}
    finally:
        writer.close()
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(str(certificate["notAfter"])), tz=UTC)


async def certificate_expiry(db: AsyncSession, now: datetime) -> list[CheckResult]:
    """The TLS certificate of the public URL expires soon or is refused: browsers will stop connecting."""
    import ssl
    from urllib.parse import urlsplit

    from app.core.config import settings

    check_id = "xtm_one.certificate_expiry"
    url = urlsplit(settings.frontend_url or "")
    if url.scheme != "https" or not url.hostname:  # no TLS to check: a host read before left the scope
        return await close_out(db, check_id, [])
    values: dict[str, Any]
    try:
        expires = await _certificate_expiry(url.hostname, url.port or 443)
    except ssl.SSLCertVerificationError as exc:  # expired, untrusted or for another name
        failing, values = True, {"error": str(getattr(exc, "verify_message", None) or exc)[:200]}
    except (OSError, TimeoutError, KeyError, ValueError):  # unreachable from here: nothing to judge
        # The finding of this host stands; one of a former host left the scope.
        return [r for r in await close_out(db, check_id, []) if r.subject.get("host") != url.hostname]
    else:
        failing = expires - now < CERTIFICATE_EXPIRY_WARNING
        values = {"expires_at": expires.isoformat(), "days_left": (expires - now).days}
    results = [
        _result(
            check_id,
            "warning",
            "security",
            failing,
            subject={"host": url.hostname},
            evidence={"values": values, "threshold": "21 days", "source": "TLS handshake with FRONTEND_URL"},
            recommendation="Renew the TLS certificate of the public URL, or fix its chain and host name.",
        )
    ]
    return results + await close_out(db, check_id, results)


for _check_id, _run in (
    ("xtm_one.registration_token", registration_token),
    ("xtm_one.licence", licence),
    ("xtm_one.licence_expiry", licence_expiry),
    ("xtm_one.smtp", smtp),
    ("xtm_one.hub_registration", hub_registration),
    ("xtm_one.integration_broken", integration_broken),
    ("xtm_one.mcp_server_broken", mcp_server_broken),
    ("xtm_one.kb_indexing_stalled", kb_indexing_stalled),
    ("xtm_one.kb_stale_embeddings", kb_stale_embeddings),
    ("xtm_one.default_model", default_model),
    ("xtm_one.embedding_model", embedding_model),
    ("xtm_one.quota_near_ceiling", quota_near_ceiling),
    ("xtm_one.dependency_unreachable", dependency_unreachable),
    ("xtm_one.trace_backlog", trace_backlog),
    ("xtm_one.worker_absent", worker_absent),
    ("xtm_one.provider_error_rate", provider_error_rate),
    ("xtm_one.maintenance_stalled", maintenance_stalled),
):
    register_check(
        Check(check_id=_check_id, module=MODULE, interval=PLATFORM_CHECK_INTERVAL, run=_platform_check(_run))
    )

for _check_id, _run in (
    ("xtm_one.version_behind", version_behind),
    ("xtm_one.certificate_expiry", certificate_expiry),
):
    register_check(Check(check_id=_check_id, module=MODULE, interval=DAILY_CHECK_INTERVAL, run=_platform_check(_run)))
