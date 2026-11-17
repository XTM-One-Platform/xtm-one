# SPDX-License-Identifier: Apache-2.0

"""Nova provider bootstrap — lifecycle hooks and connectivity probe.

Importing this module registers Nova's integration seams:
- ``on_connect``: create the default rule-sync source (the public
  Nova-Hunting/nova-rules repo) so activating the integration starts pulling
  rules on the scheduler.
- ``on_disconnect``: remove the source and the rules it synced.
- ``connectivity``: report whether the guard would actually guard anything.

Nova contributes no agent tools, so it is not in ``PROVIDER_TOOLS``; this module
is imported for its side effects by ``app/api/v1/integrations.py`` to run the
registrations.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.connectivity import (
    ProbeContext,
    ProbeResult,
    register_connectivity,
)
from app.integrations.lifecycle import register_lifecycle
from app.models.integration import Integration
from app.models.nova_rule import NovaRule
from app.models.nova_rule_source import NovaRuleSource
from app.services.nova_rule_sync import _sync_tag, served_sync_error

logger = logging.getLogger(__name__)

PROVIDER = "nova"
DEFAULT_REPO = "Nova-Hunting/nova-rules"
DEFAULT_BRANCH = "main"


async def on_connect(db: AsyncSession, integration: Integration) -> None:
    """Seed the default rule-sync source when Nova is connected (idempotent).

    Company-managed connections only. Nova's rules are platform-wide
    (``is_company_managed=True``) and its guard audits every user's prompts, so
    a personal connection must never seed a source — otherwise any non-admin
    could create platform rules by connecting, and delete them by
    disconnecting.
    """
    if not integration.is_company_managed:
        return
    existing = (
        await db.execute(
            select(NovaRuleSource).where(
                NovaRuleSource.integration_id == integration.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        NovaRuleSource(
            integration_id=integration.id,
            repo=DEFAULT_REPO,
            branch=DEFAULT_BRANCH,
            path="",
            sync_interval="daily",
        )
    )
    logger.info(
        "Nova connected (integration=%s) — created default rule source %s",
        integration.id,
        DEFAULT_REPO,
    )


async def on_disconnect(db: AsyncSession, integration: Integration) -> None:
    """Remove the source(s) and the rules they synced when Nova disconnects.

    Mirrors ``on_connect``: only a company-managed connection owns platform
    state, so a personal one has nothing to tear down (and must not be able to
    delete company-managed rules).
    """
    if not integration.is_company_managed:
        return
    sources = (
        (
            await db.execute(
                select(NovaRuleSource).where(
                    NovaRuleSource.integration_id == integration.id
                )
            )
        )
        .scalars()
        .all()
    )
    if not sources:
        return
    company_rules = (
        (
            await db.execute(
                select(NovaRule).where(NovaRule.is_company_managed.is_(True))
            )
        )
        .scalars()
        .all()
    )
    for source in sources:
        tag = _sync_tag(source.id)
        for rule in company_rules:
            if tag in (rule.tags or []):
                await db.delete(rule)
        await db.delete(source)


async def check_connectivity(ctx: ProbeContext) -> ProbeResult:
    """Report whether the Nova guard would actually guard anything right now.

    Nova authenticates against nothing, so "is the credential still valid" —
    the question every HTTP connectivity check answers — does not apply. What
    an operator needs before trusting the connected badge is: are rules
    loaded, did the last sync succeed, and can every rule tier actually fire.
    All three live in our own state, so the whole check is local and read-only.
    """
    from app.services.nova_engine import NOVA_HUNTING_AVAILABLE

    if ctx.db is None or ctx.integration is None:
        # Only reachable if something calls the helper with credentials alone;
        # the test endpoint always passes the row.
        return ProbeResult.failed(
            "Nova's health check needs the integration record."
        )

    if not ctx.integration.is_company_managed:
        # Mirrors on_connect: a personal connection deliberately seeds nothing,
        # because Nova's rules are platform-wide.
        return ProbeResult.degraded(
            "Personal connection — Nova rules and the guard are platform-wide, "
            "so this connection syncs no rules of its own. Ask an admin for a "
            "company-managed connection."
        )

    source = (
        await ctx.db.execute(
            select(NovaRuleSource).where(
                NovaRuleSource.integration_id == ctx.integration.id
            )
        )
    ).scalars().first()
    if source is None:
        return ProbeResult.failed(
            "No rule source attached — nothing will ever sync. Disconnect and "
            "reconnect the integration to recreate the default source "
            f"({DEFAULT_REPO})."
        )

    rules = (
        await ctx.db.execute(
            select(func.count())
            .select_from(NovaRule)
            .where(
                NovaRule.enabled.is_(True),
                NovaRule.is_company_managed.is_(True),
            )
        )
    ).scalar_one()
    active = f"{rules} active rule{'' if rules == 1 else 's'}"

    if source.last_sync_status == "error":
        detail = served_sync_error(
            source.last_sync_error or "unknown error",
            can_manage=ctx.caller_can_manage,
        ).strip()[:200]
        return ProbeResult.failed(
            f"Last sync of {source.repo} failed "
            f"({source.sync_failure_count} consecutive): {detail}"
        )
    if source.last_synced_at is None:
        return ProbeResult.degraded(
            f"Rule source {source.repo} is configured but has never synced "
            f"({source.sync_interval} schedule) — {active}."
        )
    synced = source.last_synced_at.strftime("%Y-%m-%d %H:%M UTC")
    pulled = source.last_sync_rules
    if rules == 0:
        return ProbeResult.failed(
            f"No active rules — the guard runs but allows every prompt. Last "
            f"sync of {source.repo} succeeded on {synced} with "
            f"{pulled} rule{'' if pulled == 1 else 's'}; check they are enabled."
        )
    if not source.enabled:
        return ProbeResult.degraded(
            f"{active}, but syncing is disabled — the rule set will not be "
            f"refreshed from {source.repo} (last sync {synced})."
        )
    if not NOVA_HUNTING_AVAILABLE:
        return ProbeResult.degraded(
            f"{active}, keyword/regex tier only — the nova-hunting engine is "
            "not installed, so semantics: and llm: rules cannot fire. Last "
            f"sync {synced}."
        )
    return ProbeResult.ok(
        f"{active}, all rule tiers available. Last sync of {source.repo} "
        f"on {synced} ({pulled} rule{'' if pulled == 1 else 's'})."
    )


register_lifecycle(PROVIDER, on_connect=on_connect, on_disconnect=on_disconnect)
register_connectivity(PROVIDER, check_connectivity)
