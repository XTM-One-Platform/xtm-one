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
=================================  ==========  =============  ==============================================

The four last checks answer one result per subject (an integration, a server, a
knowledge base) and close out the open findings whose subject they no longer
read (:func:`app.services.health.engine.close_out`), so fixing or deleting the
subject resolves its finding.

**Mode gate** (section 1.1): XTM One platform health is domain content. It
runs in the XTM One modes (``xtm_one`` and ``ai_disabled``) and records nothing
in the Copilot modes (``settings.is_copilot_like``), so no XTM One finding ever exists there.
Unlike the agent checks it does not read the AI switch: these reads run with AI
switched off too (``ai_disabled``). Each check reads a setting or one row,
calls no model, reaches no network and is never metered.

The remaining 4.2 checks (scheduler lag, queues, probes, sandbox latency,
providers, quota, version, certificates) follow in a later slice; the security
audit stays its own page.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.health.engine import Check, CheckResult, close_out, register_check

MODULE = "xtm_one"
#: How often the platform checks run (section 3, "Scheduling": aligned on the registration heartbeat).
PLATFORM_CHECK_INTERVAL = timedelta(minutes=5)
#: A validated licence expiring sooner than this raises a warning.
LICENCE_EXPIRY_WARNING = timedelta(days=30)
#: Documents processing for longer than this mean a stalled indexing worker.
KB_STALLED_AFTER = timedelta(hours=1)
#: The integration states that stop its tools (as the agent check reads them).
BROKEN_INTEGRATION_STATUSES = frozenset({"error", "reauthorize"})
#: Where a finding of these checks is explained.
DOC_LINK = "/docs/administration/health#platform-checks"

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
        return await read(db, now)

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
):
    register_check(
        Check(check_id=_check_id, module=MODULE, interval=PLATFORM_CHECK_INTERVAL, run=_platform_check(_run))
    )
