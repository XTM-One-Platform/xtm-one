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

"""Provisioning the built-in AI content.

Extracted verbatim from ``main.py``'s lifespan so the same content can be
created when an administrator turns AI on at runtime, not only at boot
(FR-012). Both callers exist now: boot, and the administrator toggle.

Idempotent because each ``seed_builtin_*`` already is — they run on every boot.
What must not change is their **order**: later steps reference entities earlier
ones create, and nothing else asserts that.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.database import async_session
from app.services.ai_state import content_mode

if TYPE_CHECKING:
    from app.services.brand_asset_source import BrandAssetSourceSync

logger = logging.getLogger(__name__)


async def provision_builtin_content(
    seed_mode: str | None = None,
    *,
    brand_source: tuple[Path | None, BrandAssetSourceSync | None] | None = None,
) -> None:
    """Create the built-in agents, assignments, templates, flows and prices.

    ``seed_mode`` overrides which content set is created; it defaults to
    :func:`content_mode`. Safe to re-run. Callers must hold ``bootstrap_lock()``:
    these are check-then-insert flows against unique-constrained tables.
    *brand_source* is what ``brand_asset_source.downloaded_source`` yielded,
    for a caller that downloaded the brand asset source before taking the
    lock; without it, the source is downloaded here (boot).
    """
    seed_mode = seed_mode or content_mode()
    # Seed built-in agent templates and pre-packaged agents (mode-aware)
    from app.agents.seed import (
        seed_builtin_agents,
        seed_builtin_assignments,
        seed_builtin_integrations,
        seed_builtin_intent_bindings,
        seed_builtin_knowledge_bases,
        seed_builtin_skills,
        seed_builtin_templates,
    )
    from app.services.tracing.price_loader import sync_model_prices

    async with async_session() as db:
        seeded = await seed_builtin_templates(db, platform_mode=seed_mode)
        if seeded:
            logger.info("Provisioned %d built-in agent templates", seeded)

    # Seed pre-packaged agents, each in the modes its platform_targets name
    async with async_session() as db:
        seeded_agents = await seed_builtin_agents(
            db, platform_mode=seed_mode
        )
        if seeded_agents:
            logger.info("Provisioned %d pre-packaged agent(s)", seeded_agents)

    # Seed pre-packaged assignments on built-in agents (XTM One only) — e.g. the
    # weekly Exposure Briefing that powers the dashboard widget.
    async with async_session() as db:
        seeded_asgmts = await seed_builtin_assignments(
            db, platform_mode=seed_mode
        )
        if seeded_asgmts:
            logger.info("Provisioned %d pre-packaged assignment(s)", seeded_asgmts)

    # Seed pre-packaged skills (XTM One only)
    async with async_session() as db:
        seeded_skills = await seed_builtin_skills(
            db, platform_mode=seed_mode
        )
        if seeded_skills:
            logger.info("Provisioned %d pre-packaged skill(s)", seeded_skills)

    # The Platform Health fleet (#8261), after the skills it carries (seeded in every
    # mode), built in since #101; named "Platform Health" in every mode
    # (``concierge.naming``).
    if seed_mode in ("xtm_one", "dev", "copilot"):
        from app.services.health.fleet_seed import seed_health_fleet

        async with async_session() as db:
            seeded_fleet = await seed_health_fleet(db)
            if seeded_fleet:
                logger.info("Provisioned %d Platform Health fleet agent(s)", seeded_fleet)

    # The CTEM access groups (#2441): "CTEM Viewers" (every account) and an
    # empty "CTEM Operators"; xtm_one and dev only.
    from app.services.ctem_groups import seed_ctem_groups

    async with async_session() as db:
        seeded_groups = await seed_ctem_groups(db, platform_mode=seed_mode)
        if seeded_groups:
            logger.info("Provisioned %d CTEM access group(s)", seeded_groups)

    # Built-in fleets (#8988), after the agents, assignments and skills they
    # reference; xtm_one and dev only, copilot sweeps any it finds.
    from app.agents import fleet_seed

    async with async_session() as db:
        seeded_fleets = await fleet_seed.seed_builtin_fleets(db, platform_mode=seed_mode)
        if seeded_fleets:
            logger.info("Provisioned %d built-in fleet(s)", seeded_fleets)

    # Every fleet orchestrator, custom ones included, advises on its fleet's
    # health (#11700): get_fleet_health and the fleet-health-advisory skill.
    from app.services.fleets import backfill_fleet_health_advisory

    async with async_session() as db:
        advised = await backfill_fleet_health_advisory(db)
        if advised:
            logger.info("Gave %d fleet orchestrator(s) the health advisory", advised)

    # Seed pre-packaged integrations (XTM One only)
    async with async_session() as db:
        seeded_ints = await seed_builtin_integrations(
            db, platform_mode=seed_mode
        )
        if seeded_ints:
            logger.info("Provisioned %d pre-packaged integration(s)", seeded_ints)

    # Seed pre-packaged knowledge bases (XTM One only)
    async with async_session() as db:
        seeded_kbs = await seed_builtin_knowledge_bases(
            db, platform_mode=seed_mode
        )
        if seeded_kbs:
            logger.info("Provisioned %d pre-packaged knowledge base(s)", seeded_kbs)

    # Seed the built-in template containers — the brand decks used by
    # generate_presentation: metadata first, in its own session,
    # so the palette every deck depends on lands whatever the object store does.
    async with async_session() as db:
        from app.services.template_container_seed import (
            seed_builtin_template_containers,
        )

        seeded_containers = await seed_builtin_template_containers(
            db, platform_mode=seed_mode
        )
        if seeded_containers:
            logger.info(
                "Provisioned %d built-in template container(s)", seeded_containers
            )

    # Then the brand ASSETS (logos, product marks) those containers reference by
    # role, with the packs of the brand asset source when one is set (Settings >
    # Platform). Idempotent (content-hashed object keys) and failure-isolated:
    # an unavailable S3 or source logs a warning and the next boot retries.
    from app.services.brand_asset_source import seed_template_assets, seed_with_source

    if brand_source is None:
        seeded_assets, _source = await seed_template_assets(seed_mode)
    else:
        seeded_assets = await seed_with_source(seed_mode, *brand_source)
    if seeded_assets:
        logger.info("Provisioned %d built-in template asset(s)", seeded_assets)

    # Seed pre-packaged intent bindings (XTM One only)
    async with async_session() as db:
        seeded_bindings = await seed_builtin_intent_bindings(
            db, platform_mode=seed_mode
        )
        if seeded_bindings:
            logger.info(
                "Provisioned %d pre-packaged intent binding(s)", seeded_bindings
            )

    # Built-in benchmark datasets, then the evaluations that link them and the
    # built-in agents seeded above.
    await seed_builtin_evaluation_content(seed_mode)

    # Sync versioned model pricing seed data. This intentionally fails startup
    # when the seed payload is invalid to avoid silent cost-calculation drift.
    async with async_session() as db:
        synced_count, skipped_custom_count = await sync_model_prices(db)
        logger.info(
            "Synced %d model price row(s); skipped %d custom override(s)",
            synced_count,
            skipped_custom_count,
        )


async def seed_builtin_evaluation_content(seed_mode: str) -> None:
    """The built-in benchmark datasets and evaluations, while evaluation is on.

    *seed_mode* is resolved by the caller (``provision_builtin_content``), never ``None``.

    Optional content, failure-isolated like the brand assets: a failure is logged
    and the next boot retries. Skipped while evaluation is off, not synced to
    nothing: both are full syncs, and an empty one would delete the benchmark and
    its runs.
    """
    from app.evaluations.seed import seed_builtin_datasets, seed_builtin_evaluations
    from app.services.evaluation.availability import evaluation_enabled

    if not evaluation_enabled():
        return
    try:
        async with async_session() as db:
            seeded_datasets = await seed_builtin_datasets(db, platform_mode=seed_mode)
        if seeded_datasets:
            logger.info("Provisioned %d built-in dataset(s)", seeded_datasets)
    except Exception:
        logger.warning("Built-in datasets could not be synced; will retry on next startup", exc_info=True)
    try:
        async with async_session() as db:
            seeded_evaluations = await seed_builtin_evaluations(db, platform_mode=seed_mode)
        if seeded_evaluations:
            logger.info("Provisioned %d built-in evaluation(s)", seeded_evaluations)
    except Exception:
        logger.warning("Built-in evaluations could not be synced; will retry on next startup", exc_info=True)


async def wire_default_agents() -> None:
    """Point users with no default agent at the CTEM Assistant, and link it as
    the General Assistant unless an administrator chose its link
    (``linked_agent_configured``).

    Called from boot *and* the toggle: seeded content nobody points at is still
    an empty platform (FR-012), and users who registered while AI was off keep
    ``default_agent_id IS NULL`` until the next restart otherwise.

    A no-op when the CTEM Assistant is absent — the ``ai_disabled`` state — so
    it is safe to call unconditionally after seeding.
    """
    from sqlalchemy import func as _func
    from sqlalchemy import select as _sel
    from sqlalchemy import update as _upd

    from app.models.agent import Agent as _Agent
    from app.models.general_assistant_config import GeneralAssistantConfig as _GAConfig
    from app.models.user import User as _User

    async with async_session() as db:
        ctem_r = await db.execute(
            _sel(_Agent.id).where(_Agent.slug == "ctem-assistant").limit(1)
        )
        ctem_id = ctem_r.scalar_one_or_none()
        if not ctem_id:
            return

        # Bulk-set the default agent for every user that doesn't have one yet.
        # ``synchronize_session=False`` is safe: this owns its own short-lived
        # session and there's no other in-flight ORM state to keep in sync.
        # Recorded as the platform's wiring, never the person's choice
        # (``platform_insights.default_agents``).
        update_result = await db.execute(
            _upd(_User)
            .where(_User.default_agent_id.is_(None))
            .values(
                default_agent_id=ctem_id,
                default_agent_source="platform",
                default_agent_set_at=_func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        updated = update_result.rowcount or 0
        if updated:
            logger.info("Set CTEM Assistant as default agent for %d user(s)", updated)

        # Only a link no administrator chose: "no agent" is stored as
        # NULL too, so the flag, not the value, says whether one was chosen.
        ga_r = await db.execute(_sel(_GAConfig).limit(1))
        ga = ga_r.scalar_one_or_none()
        if ga and not ga.linked_agent_configured and ga.linked_agent_id != ctem_id:
            ga.linked_agent_id = ctem_id
            logger.info("Linked CTEM Assistant as General Assistant")

        await db.commit()


async def provision_env_providers() -> None:
    """The provider half of a boot: LLM providers from ``LLM_PROVIDER_*``, the
    XTM One embedding endpoint, Langfuse, and pruning what no longer has an env
    var behind it.

    Separate from :func:`provision_builtin_content` because it is provider
    configuration rather than content, but called from the same two places for
    the same reason: a platform enabled at runtime with seeded content and no
    providers fails every chat and every run until it is restarted, which is
    exactly the redeploy the toggle exists to avoid (FR-012).

    The four seeders still live in ``app/main.py``. Moving them is a
    byte-identical refactor worth landing on its own, the way the content
    seeding was, so this orchestrates them where they are.
    """
    from app.core.config import settings
    from app.main import (
        _byollm_install_configured,
        _prune_orphaned_env_providers,
        _seed_langfuse_from_env,
        _seed_llm_providers_from_env,
        _seed_xtm_one_embedding_server_from_env,
    )
    from app.services.model_catalog_announcements import model_catalog_watch

    # Read before either seed writes: the LLM seed stores the operator's
    # DEFAULT_* values on an install that has none, which the embedding seed
    # would otherwise mistake for an administrator's configuration.
    install_configured = False
    if settings.byollm_activated:
        async with async_session() as db:
            install_configured = await _byollm_install_configured(db)

    # Records the model catalog change the seed makes, for the users' "this
    # model is now yours" emails, from both callers alike.
    async with model_catalog_watch():
        active = await _seed_llm_providers_from_env()
        active.update(
            await _seed_xtm_one_embedding_server_from_env(
                fill_embedding_default=not install_configured
            )
        )
        await _prune_orphaned_env_providers(active)
    await _seed_langfuse_from_env()
