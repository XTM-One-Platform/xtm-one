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

"""Seed built-in agent templates and pre-packaged agents into the database.

Behaviour on every startup:
  1. **Insert** templates whose slug doesn't exist yet.
  2. **Full-sync** existing built-in templates (is_builtin=True) — all fields are
     overwritten to match the code, including assignments, persona, icon, etc.
  3. **Delete** built-in templates whose slug is no longer in BUILTIN_TEMPLATES.

In XTM One mode, additionally seeds real Agent/Assignment rows from
``builtin_agents.py``.  These are company-managed, read-only, and synced
on every startup.

Admin-created templates (is_builtin=False) are never touched.
"""

import hashlib
import json
import logging
import os
import pathlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.builtin_agents import (
    BUILTIN_AGENTS,
    BUILTIN_ASSIGNMENTS,
    BUILTIN_INTEGRATIONS,
    BUILTIN_KNOWLEDGE_BASES,
    BUILTIN_SKILLS,
    FEATURE_GATED_BUILTIN_AGENTS,
    FEATURE_GATED_BUILTIN_SKILLS,
    RENAMED_BUILTIN_SKILL_SLUGS,
    get_builtin_integration_credentials,
    log_builtin_integration_status,
)
from app.agents.builtin_roi_units import with_builtin_roi_unit
from app.agents.fleet_opt_in import PROVISIONING_STATE_KEYS
from app.agents.gate_answered import INHERIT_SWITCH_FROM_KEY
from app.agents.templates import BUILTIN_TEMPLATES
from app.core.config import settings
from app.core.feature_flags import is_feature_enabled
from app.core.utils import format_exception_message
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_template import AgentTemplate
from app.models.knowledge_base import KBSyncSource, KnowledgeBase, KnowledgeDocument
from app.models.llm_config import LLMProviderConfig
from app.services.agent_config_versioning import (
    record_agent_version_changes,
    seed_initial_agent_versions,
    versioned_agent_values,
)
from app.services.assignment_activation_check import parse_activation_check
from app.services.assignment_dispatch import seeded_trigger_config
from app.services.assignment_pause import (
    clear_pause_provenance,
    is_emergency_stopped,
    mark_schedule_resumed,
    pause_assignment,
)
from app.services.builtin_customer_layer import seed_keeps_field, seed_keeps_model
from app.services.kb_helpers import bump_kb_revision, reset_changed_document

logger = logging.getLogger(__name__)

SEED_DATA_DIR = pathlib.Path(__file__).parent / "seed_data"

# Pause provenance for seeding-driven disables. Startup seeding is
# the source of truth for built-in assignments, so a seed that defines one as
# disabled is a deployment-configuration decision, not a human one.
_SEED_ACTOR_LABEL = "XTM One — startup seeding"
_SEED_DISABLED_DETAILS = (
    "Disabled by startup seeding: this built-in assignment is switched off "
    "in the current deployment configuration."
)
_SEED_OPT_IN_DETAILS = (
    "Off until an administrator turns it on: choose the account it runs as, "
    "then resume it."
)


def _seed_commit_message() -> str:
    """Commit message of a version the seed records on a built-in agent:
    the change shipped with this release, not an edit by a person."""
    return f"Updated by the built-in agent seed (release {settings.app_version})"


# A slug is lowercase alphanumeric + hyphens — no spaces, no newlines.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


async def _resolve_seed_owner_id(db: AsyncSession):
    """Resolve the admin user to own pre-packaged (company-managed) entities.

    The platform invariant is that ``user_id`` is always set — company-managed
    entities keep a real owner (the admin) and rely on ``is_company_managed``
    for visibility. Resolution order: configured ``ADMIN_EMAIL`` → oldest admin
    → oldest user → ``None`` (only on a brand-new DB before the admin row
    exists, in which case the next startup backfills the owner via the sync
    pass).

    Thin wrapper around the shared
    :func:`app.api.v1.helpers.resolve_company_managed_owner_id` so the seed
    pass and platform registration resolve the company-managed owner the same
    way.
    """
    from app.api.v1.helpers import resolve_company_managed_owner_id

    return await resolve_company_managed_owner_id(db)


def _resolve_seed_content(subdir: str, value: str | None) -> str | None:
    """Resolve a seed content value by slug detection.

    If *value* matches the slug pattern (``^[a-z0-9][a-z0-9-]*$``), load
    ``seed_data/{subdir}/{value}.md`` and return its content (or ``None``
    if the file is absent — surfaced via a WARNING log so a typo or a
    missing seed file is operationally visible instead of silently
    producing an empty persona/skill body).  Any other value is treated
    as inline content and returned unchanged.
    """
    if value is None:
        return None
    if _SLUG_RE.match(value):
        seed_file = SEED_DATA_DIR / subdir / f"{value}.md"
        if seed_file.is_file():
            return seed_file.read_text(encoding="utf-8")
        logger.warning(
            "Seed content file not found for slug '%s' in subdir '%s' "
            "(expected at %s) — falling back to empty content. Check the "
            "agent/skill seed data for typos.",
            value,
            subdir,
            seed_file,
        )
        return None
    # Inline content — return unchanged.
    return value


def _resolve_seed_schema(value: dict | str | None) -> dict | None:
    """Resolve an output schema value by inline-vs-slug detection.

    If *value* is a dict, treat it as inline schema content and return it.
    If *value* is a slug, load ``seed_data/agent_output_schema/{value}.json``
    and return the parsed dict (or ``None`` if the file is absent — logged
    at WARNING level so missing/typo'd schema files don't silently produce
    a schema-less agent).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and _SLUG_RE.match(value):
        seed_file = SEED_DATA_DIR / "agent_output_schema" / f"{value}.json"
        if seed_file.is_file():
            return json.loads(seed_file.read_text(encoding="utf-8"))
        logger.warning(
            "Seed output schema file not found for slug '%s' "
            "(expected at %s) — agent will have no output schema. "
            "Check the agent seed data for typos.",
            value,
            seed_file,
        )
    return None


async def seed_builtin_templates(
    db: AsyncSession, *, platform_mode: str = "copilot"
) -> int:
    """Synchronize built-in templates with the database.

    Each template ships in the platform modes its ``platform_targets`` name
    (:func:`seed_matches_platform`): the general-purpose templates in Copilot
    mode, the CTI templates in XTM One mode, every one of them in dev. A
    built-in template the current mode does not ship is removed.
    Returns the number of templates newly inserted.
    """
    templates = [
        tmpl for tmpl in BUILTIN_TEMPLATES if seed_matches_platform(tmpl, platform_mode)
    ]

    inserted = 0
    current_slugs = {t["slug"] for t in templates}

    # ------------------------------------------------------------------
    # Phase 1: Delete built-in templates that are no longer in the list
    # ------------------------------------------------------------------
    result = await db.execute(
        select(AgentTemplate).where(
            AgentTemplate.is_builtin.is_(True),
            AgentTemplate.slug.notin_(current_slugs),
        )
    )
    removed = 0
    for stale in result.scalars().all():
        logger.info("Removing stale built-in template: %s", stale.slug)
        await db.delete(stale)
        removed += 1

    # ------------------------------------------------------------------
    # Phase 2: Upsert current templates
    # ------------------------------------------------------------------
    for tmpl_data in templates:
        slug = tmpl_data["slug"]

        result = await db.execute(
            select(AgentTemplate).where(AgentTemplate.slug == slug)
        )
        existing = result.scalar_one_or_none()

        if existing:
            if existing.is_builtin:
                # Full sync — overwrite every field so production stays current
                existing.name = tmpl_data["name"]
                existing.description = tmpl_data.get("description")
                existing.icon = tmpl_data.get("icon", "Bot")
                existing.category = tmpl_data.get("category", "general")
                existing.default_persona = tmpl_data.get("default_persona")
                existing.default_output_format = tmpl_data.get(
                    "default_output_format", "markdown"
                )
                existing.disable_chat = tmpl_data.get("disable_chat", False)
                existing.required_integrations = tmpl_data.get(
                    "required_integrations", []
                )
                existing.required_integration_types = tmpl_data.get(
                    "required_integration_types", []
                )
                existing.recommended_mcp = tmpl_data.get("recommended_mcp", [])
                existing.must_be_defined = tmpl_data.get("must_be_defined", [])
                existing.default_skills = tmpl_data.get("default_skills", [])
                existing.default_assignments = tmpl_data.get("default_assignments", [])
            # Admin-created template with same slug — leave it alone
            continue

        template = AgentTemplate(
            slug=slug,
            name=tmpl_data["name"],
            description=tmpl_data.get("description"),
            icon=tmpl_data.get("icon", "Bot"),
            category=tmpl_data.get("category", "general"),
            default_persona=tmpl_data.get("default_persona"),
            default_output_format=tmpl_data.get("default_output_format", "markdown"),
            disable_chat=tmpl_data.get("disable_chat", False),
            required_integrations=tmpl_data.get("required_integrations", []),
            required_integration_types=tmpl_data.get("required_integration_types", []),
            recommended_mcp=tmpl_data.get("recommended_mcp", []),
            must_be_defined=tmpl_data.get("must_be_defined", []),
            default_skills=tmpl_data.get("default_skills", []),
            default_assignments=tmpl_data.get("default_assignments", []),
            is_builtin=True,
            enabled=True,
        )
        db.add(template)
        inserted += 1
        logger.info("Seeded agent template: %s", slug)

    # Always commit — updates + inserts + deletes
    await db.commit()
    if inserted:
        logger.info("Seeded %d built-in agent template(s)", inserted)
    if removed:
        logger.info("Removed %d stale built-in agent template(s)", removed)

    return inserted


# ═══════════════════════════════════════════════════════════════════════════════
# Pre-packaged agents (XTM One mode only)
# ═══════════════════════════════════════════════════════════════════════════════


async def _is_anthropic_enabled(db: AsyncSession) -> bool:
    """Return True if at least one Anthropic LLM provider is configured."""
    result = await db.execute(
        select(LLMProviderConfig.id)
        .where(LLMProviderConfig.provider == "anthropic")
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _resolve_llm_model(data: dict, *, anthropic_enabled: bool) -> str | None:
    """Return the desired llm_model only if its provider is available."""
    model = data.get("llm_model")
    if model and model.startswith("claude") and not anthropic_enabled:
        return None
    return model


def _sync_agent_fields(
    agent: Agent, data: dict, *, anthropic_enabled: bool = False
) -> None:
    """Overwrite mutable fields on an existing built-in agent."""
    from sqlalchemy.orm.attributes import flag_modified

    agent.name = data.get("name", agent.name)
    agent.description = data.get("description")
    agent.icon = data.get("icon", "Bot")
    agent.persona = _resolve_seed_content("agent_persona", data.get("persona", data["slug"]))
    agent.output_format = data.get("output_format", "markdown")
    agent.output_schema = with_builtin_roi_unit(data["slug"], _resolve_seed_schema(data.get("output_schema")))
    # A seeded agent a fleet calls with its own output_schema accepts it (#1837): without the
    # flag the caller's schema is refused before the agent runs.
    agent.accept_caller_output_format = bool(data.get("accept_caller_output_format", False))
    agent.disable_chat = data.get("disable_chat", False)
    agent.hide_runs = data.get("hide_runs", False)
    agent.max_iterations = data.get("max_iterations")
    # The model is customer layer once a person pinned one (#914,
    # ``services/builtin_customer_layer``): the seed then leaves it as set.
    # Every other customer-layer field (bindings, MCP servers, knowledge
    # bases, skills, custom tools, organization notes) is never written here.
    if not seed_keeps_model(agent):
        agent.llm_model = _resolve_llm_model(data, anthropic_enabled=anthropic_enabled)
        # A built-in that takes a narrow decision runs on the platform routing
        # model: the deployment's own lightweight model, which no seed can name.
        agent.llm_model_source = data.get("llm_model_source")
    agent.tool_deny_list = data.get("tool_deny_list") or []
    agent.builtin_tool_ids = data.get("builtin_tool_ids") or []
    # Template-bound providers and their read-only set are seeded defaults of
    # the customer layer (#914): once a person set one, the seed leaves it.
    if not seed_keeps_field(agent, "integration_template_bindings"):
        agent.integration_template_bindings = data.get("integration_template_bindings", [])
    # The template-bound providers a seeded reader desk may only read.
    if not seed_keeps_field(agent, "integration_template_read_only"):
        agent.integration_template_read_only = data.get("integration_template_read_only") or None
    agent.available_channel_types = ["web"]
    agent.tags = data.get("tags", ["builtin"])
    agent.supported_intents = data.get("supported_intents") or {}
    # Pipeline & LLM tuning
    agent.temperature = data.get("temperature")
    # Extended thinking / reasoning effort ("low"|"medium"|"high"|"max"; NULL =
    # platform default). Set explicitly on the OpenAEV orchestrator + Payload
    # Creator (WS4) so their decision loops surface reasoning to the cockpit.
    agent.reasoning_effort = data.get("reasoning_effort")
    # Organization context (#11533): every seeded agent inherits the platform
    # default (on), so the seed never writes the mode and a person's opt-out
    # survives a refresh; a definition's preloaded categories are its own.
    if "organization_context_categories" in data:
        agent.organization_context_categories = list(data["organization_context_categories"])
    agent.finalize_tool_ref = data.get("finalize_tool_ref")
    agent.prepare_tool_ref = data.get("prepare_tool_ref")
    agent.skip_llm = data.get("skip_llm", False)
    # Per-agent runtime config (generic JSON blob for engine-level behaviour)
    agent.config = data.get("config") or None
    # Memory tiers
    agent.memory_short_term_enabled = data.get("memory_short_term_enabled", True)
    agent.memory_medium_term_enabled = data.get("memory_medium_term_enabled", True)
    agent.memory_persistent_enabled = data.get("memory_persistent_enabled", True)
    agent.memory_user_enabled = data.get("memory_user_enabled", True)

    # Plain JSON columns may not trigger SQLAlchemy dirty detection on
    # reassignment — force them into the UPDATE statement.
    for attr in (
        "output_schema", "tool_deny_list", "builtin_tool_ids",
        "integration_template_bindings", "integration_template_read_only", "prepare_tool_ref",
        "finalize_tool_ref", "available_channel_types", "tags",
        "supported_intents", "config",
    ):
        flag_modified(agent, attr)


def _new_builtin_agent(data: dict, owner_id: object, *, anthropic_enabled: bool) -> Agent:
    """A built-in agent row as the seed creates it: its identity, then every mutable
    field written by :func:`_sync_agent_fields`, the one rule for both creation and
    update. A field the update path writes (``config``, ``tool_deny_list``) used to be
    missing from a freshly created row until the next boot."""
    agent = Agent(
        user_id=owner_id,
        is_company_managed=True,
        is_builtin=True,
        slug=data["slug"],
        name=data["name"],
    )
    _sync_agent_fields(agent, data, anthropic_enabled=anthropic_enabled)
    return agent


def _withheld_builtin_agent_slugs() -> set[str]:
    """The built-in agents whose feature flag is off."""
    return {
        slug
        for slug, flag in FEATURE_GATED_BUILTIN_AGENTS.items()
        if not is_feature_enabled(flag)
    }


def _without_specialists(persona: str, names: list[str]) -> str:
    """``persona`` without the list items that introduce one of ``names``.

    A specialist is a ``- **<name>**`` bullet and its indented continuation
    lines; the rest of the persona is kept byte for byte.
    """
    kept: list[str] = []
    skipping = False
    for line in persona.splitlines(keepends=True):
        if any(line.startswith(f"- **{name}**") for name in names):
            skipping = True
            continue
        if skipping and line[:1] in (" ", "\t"):
            continue
        skipping = False
        kept.append(line)
    return "".join(kept)


def _seeded_builtin_agents() -> list[dict]:
    """``BUILTIN_AGENTS`` as this deployment seeds them.

    An agent whose feature flag is off (``FEATURE_GATED_BUILTIN_AGENTS``) is
    left out, and so is every reference the others make to it: their
    handovers, and the bullet their persona gives it as a specialist, which
    would otherwise send the model after an agent it cannot reach.
    """
    withheld = _withheld_builtin_agent_slugs()
    if not withheld:
        return BUILTIN_AGENTS
    names = [a["name"] for a in BUILTIN_AGENTS if a["slug"] in withheld]
    seeded: list[dict] = []
    for agent_data in BUILTIN_AGENTS:
        if agent_data["slug"] in withheld:
            continue
        handovers = agent_data.get("handover_agent_slugs") or []
        if not any(slug in withheld for slug in handovers):
            seeded.append(agent_data)
            continue
        agent_data = dict(agent_data)
        agent_data["handover_agent_slugs"] = [s for s in handovers if s not in withheld]
        for key in ("handover_type_slugs", "handover_condition_slugs"):
            if key in agent_data:
                agent_data[key] = {
                    s: v for s, v in agent_data[key].items() if s not in withheld
                }
        persona = _resolve_seed_content(
            "agent_persona", agent_data.get("persona", agent_data["slug"])
        )
        if persona is not None:
            agent_data["persona"] = _without_specialists(persona, names)
        seeded.append(agent_data)
    return seeded


def _seed_slug_tag(tags: list | None) -> str | None:
    """The ``slug:<slug>`` tag a seeded knowledge base or skill is keyed by."""
    for tag in tags or []:
        if isinstance(tag, str) and tag.startswith("slug:"):
            return tag[5:]
    return None


async def _detach_seed_rows(db: AsyncSession, agent: Agent, slug: str) -> None:
    """Take back what the seed attached to a person's agent it adopted.

    An earlier boot that adopted a person's agent on built-in ``slug``
    then treated it as that built-in: it seeded its assignments and global
    intent bindings onto it, bound the built-in knowledge bases, skills and
    integrations, and wrote the built-in's ``tags`` and ``supported_intents``.
    Once the agent is handed back, the passes keyed by ``slug`` reach the real
    built-in and never see these again, so they are removed here. Only rows
    the seed owns for ``slug`` are touched: anything else on the agent is the
    person's.
    """
    from app.models.assignment import Assignment
    from app.models.integration import Integration
    from app.models.intent import IntentBinding

    # Global intent bindings: a shape the API refuses on a private agent.
    bindings = await db.execute(
        select(IntentBinding).where(
            IntentBinding.agent_id == agent.id,
            IntentBinding.user_id.is_(None),
            IntentBinding.is_builtin.is_(True),
        )
    )
    for binding in bindings.scalars().all():
        await db.delete(binding)

    seeded_names = {a["name"] for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == slug}
    if seeded_names:
        assignments = await db.execute(
            select(Assignment).where(
                Assignment.agent_id == agent.id,
                Assignment.name.in_(seeded_names),
            )
        )
        for assignment in assignments.scalars().all():
            await db.delete(assignment)

    kb_slugs = {
        kb["slug"]
        for kb in BUILTIN_KNOWLEDGE_BASES
        if slug in kb.get("bind_to_agent_slugs", [])
    }
    if kb_slugs and agent.knowledge_base_ids:
        kbs = await db.execute(
            select(KnowledgeBase.id, KnowledgeBase.tags).where(
                KnowledgeBase.is_builtin.is_(True)
            )
        )
        kb_ids = {str(kid) for kid, tags in kbs.all() if _seed_slug_tag(tags) in kb_slugs}
        agent.knowledge_base_ids = [
            kid for kid in agent.knowledge_base_ids if str(kid) not in kb_ids
        ]

    skill_slugs = {
        s["slug"] for s in BUILTIN_SKILLS if slug in s.get("bind_to_agent_slugs", [])
    }
    if skill_slugs and agent.skill_ids:
        skills = await db.execute(
            select(AgentSkill.id, AgentSkill.tags).where(
                AgentSkill.is_company_managed.is_(True)
            )
        )
        skill_ids = {
            str(sid) for sid, tags in skills.all() if _seed_slug_tag(tags) in skill_slugs
        }
        agent.skill_ids = [sid for sid in agent.skill_ids if str(sid) not in skill_ids]

    seeded_integrations = {
        (i["provider"], i["name"])
        for i in BUILTIN_INTEGRATIONS
        if slug in i.get("bind_to_agent_slugs", [])
    }
    if seeded_integrations and agent.integration_bindings:
        integrations = await db.execute(
            select(Integration.id, Integration.provider, Integration.name).where(
                Integration.is_builtin.is_(True)
            )
        )
        integration_ids = {
            str(iid)
            for iid, provider, name in integrations.all()
            if (provider, name) in seeded_integrations
        }
        kept: dict[str, list] = {}
        for provider, ids in agent.integration_bindings.items():
            remaining = [iid for iid in ids or [] if str(iid) not in integration_ids]
            if remaining:
                kept[provider] = remaining
        agent.integration_bindings = kept

    seed_tags = {"builtin"}
    for data in BUILTIN_AGENTS:
        if data["slug"] == slug:
            seed_tags.update(data.get("tags", ["builtin"]))
    agent.tags = [t for t in agent.tags or [] if t not in seed_tags]
    agent.supported_intents = {}


async def seed_builtin_agents(
    db: AsyncSession, *, platform_mode: str = "copilot"
) -> int:
    """Seed the pre-packaged standalone agents of *platform_mode*.

    Each agent ships in the modes its ``platform_targets`` name
    (:func:`seed_matches_platform`; ``xtm_one`` and ``dev`` without any), so a
    mode seeds only its own agents and removes the others.  Agents are
    created with ``is_builtin=True, is_company_managed=True`` and a REAL
    ``user_id`` — the
    always-set-owner invariant, resolved by
    :func:`_resolve_seed_owner_id` and backfilled on legacy rows below.
    ``_bootstrap_platform_data`` provisions the admin before calling this so
    there is always someone to resolve to; that ordering is load-bearing, and
    not only for provenance. A company-managed agent with no owner has no
    identity for external work to run as, so an ownerless built-in could bind
    an A2A integration and never become reachable (``a2a.exposure``).

    Full-sync on every startup: all fields are overwritten. The persona and
    the tracked config fields keep their version history: a new
    built-in starts with its "Initial version", and each value a release
    changes is versioned with no author and :func:`_seed_commit_message`.
    """
    agents = [a for a in _seeded_builtin_agents() if seed_matches_platform(a, platform_mode)]
    current_slugs = {a["slug"] for a in agents}
    anthropic_enabled = await _is_anthropic_enabled(db) if agents else False
    owner_id = await _resolve_seed_owner_id(db) if agents else None

    # Phase 1: Delete stale built-in agents (and their assignments via CASCADE).
    # The Platform Health fleet's agents are built in but seeded by their own
    # seed (``services.health.fleet_seed``), never stale here.
    result = await db.execute(
        select(Agent).where(
            Agent.is_builtin.is_(True),
            Agent.slug.notin_(current_slugs),
            Agent.template_slug.is_(None),
            Agent.health_role.is_(None),
        )
    )
    removed = 0
    removed_agent_ids: list[str] = []
    for stale in result.scalars().all():
        if not stale.is_company_managed:
            # A person's agent an earlier boot adopted: never the
            # company's to delete. Hand it back as an ordinary agent.
            logger.warning(
                "Releasing agent %s (owner %s) from retired built-in slug '%s' "
                "instead of deleting it: it is not company-managed.",
                stale.id,
                stale.user_id,
                stale.slug,
            )
            await _detach_seed_rows(db, stale, stale.slug)
            stale.is_builtin = False
            continue
        logger.info("Removing stale built-in agent: %s", stale.slug)
        removed_agent_ids.append(str(stale.id))
        await db.delete(stale)
        removed += 1

    # Phase 2: Upsert current agents
    inserted = 0
    for agent_data in agents:
        slug = agent_data["slug"]
        result = await db.execute(select(Agent).where(Agent.slug == slug))
        existing = result.scalar_one_or_none()

        if existing and not existing.is_company_managed:
            # A person's own agent holds the slug - created before this
            # built-in shipped, or already adopted by an earlier boot
            # (``is_builtin`` set, still private to them). Adopting it would
            # overwrite their agent and leave a "built-in" private to them,
            # with the real one never created. Move it aside to a free slug,
            # still theirs and an ordinary agent again, and create the
            # built-in. Only a company-managed row - the shape every seeded one
            # has always had, legacy ones included - is adopted below.
            from app.api.v1.helpers import generate_unique_agent_slug

            # The built-in slug is already slug-shaped, so the generator keeps
            # it as the base and appends a counter (``<slug>-2``, ...).
            moved_to = await generate_unique_agent_slug(db, slug)
            logger.warning(
                "Built-in agent slug '%s' was held by agent %s (owner %s); "
                "moved it to '%s' to seed the built-in.",
                slug,
                existing.id,
                existing.user_id,
                moved_to,
            )
            if existing.is_builtin:
                await _detach_seed_rows(db, existing, slug)
            existing.slug = moved_to
            existing.is_builtin = False
            await db.flush()
            existing = None

        if existing:
            if not existing.is_builtin:
                existing.is_builtin = True
            # Backfill the owner on legacy rows that were seeded as NULL before
            # the always-set-owner invariant.
            if existing.user_id is None and owner_id is not None:
                existing.user_id = owner_id
            before = versioned_agent_values(existing)
            _sync_agent_fields(
                existing, agent_data, anthropic_enabled=anthropic_enabled
            )
            # Each persona or tracked field the release changed gets a version
            # (no author: the platform wrote it); an unchanged one writes
            # nothing, so a boot with no content change records no version.
            await record_agent_version_changes(
                db,
                agent=existing,
                before=before,
                author_id=None,
                commit_message=_seed_commit_message(),
            )
            continue

        agent = _new_builtin_agent(agent_data, owner_id, anthropic_enabled=anthropic_enabled)
        db.add(agent)
        # The id the versions reference exists once the row is flushed.
        await db.flush()
        await seed_initial_agent_versions(db, agent=agent, author_id=None)
        inserted += 1
        logger.info("Seeded built-in agent: %s", slug)

    await db.commit()
    if removed_agent_ids:
        from app.services.agent_memory import clear_deleted_agent_memory

        # The removed agents are gone for good: their Redis memory goes with
        # them (best effort, never fails the seed).
        await clear_deleted_agent_memory(*removed_agent_ids)

    # Phase 3: Resolve handover slug references to UUIDs
    if agents:
        slug_to_id: dict[str, str] = {}
        for agent_data in agents:
            res = await db.execute(
                select(Agent.id).where(Agent.slug == agent_data["slug"])
            )
            aid = res.scalar_one_or_none()
            if aid:
                slug_to_id[agent_data["slug"]] = str(aid)

        for agent_data in agents:
            handover_slugs = agent_data.get("handover_agent_slugs")
            if not handover_slugs:
                continue

            res = await db.execute(
                select(Agent).where(Agent.slug == agent_data["slug"])
            )
            agent_obj = res.scalar_one_or_none()
            if not agent_obj:
                continue

            resolved_ids = [slug_to_id[s] for s in handover_slugs if s in slug_to_id]
            type_map = agent_data.get("handover_type_slugs", {})
            resolved_types = {
                slug_to_id[s]: t for s, t in type_map.items() if s in slug_to_id
            }
            condition_map = agent_data.get("handover_condition_slugs", {})
            resolved_conditions = {
                slug_to_id[s]: c
                for s, c in condition_map.items()
                if s in slug_to_id
            }

            agent_obj.handover_agent_ids = resolved_ids
            agent_obj.handover_types = resolved_types
            agent_obj.handover_conditions = resolved_conditions

        await db.commit()
        logger.info("Resolved handover references for built-in agents")

    if inserted:
        logger.info("Seeded %d built-in agent(s)", inserted)
    if removed:
        logger.info("Removed %d stale built-in agent(s)", removed)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════════
# Pre-packaged assignments (XTM One mode only)
# ═══════════════════════════════════════════════════════════════════════════════


def _sync_seeded_gates(assignment: Any, data: dict) -> None:
    """Write the zero-token gates the seed entry declares onto *assignment*
    (``activation_check``, ``ignore_self_triggered_events``).

    Only a key the entry carries is written (``None`` clears a check), so a
    seed entry that declares no gate leaves the row as it is. A check the
    platform cannot parse is not stored: the guard test fails CI on it first.
    """
    if "activation_check" in data:
        check, error, _provided = parse_activation_check(data["activation_check"])
        if error is not None:
            logger.error(
                "Built-in assignment '%s': seeded activation_check refused: %s",
                data.get("name"),
                error,
            )
            check = None
        assignment.activation_check = check
    if "ignore_self_triggered_events" in data:
        assignment.ignore_self_triggered_events = bool(data["ignore_self_triggered_events"])


async def seed_builtin_assignments(
    db: AsyncSession, *, platform_mode: str = "copilot"
) -> int:
    """Seed real Assignment rows on built-in agents (XTM One / dev mode).

    Idempotent upsert keyed by (agent slug + assignment name): existing rows are
    field-synced, missing ones created, and stale seeded names on the same
    built-in agents removed (built-in agents are read-only for non-admins, so
    user automations live elsewhere). Mirrors the parent agent's owner +
    visibility. Execution identity is per-run: ``run_as_all_users`` assignments
    fan out one run per user (each as themselves), and manual runs execute as the
    triggering user, so platform_managed OpenCTI/OpenAEV calls are scoped to that
    user via their own JWT.
    """
    _seed_xtm = platform_mode in ("xtm_one", "dev")
    assignments = BUILTIN_ASSIGNMENTS if _seed_xtm else []
    if not assignments:
        return 0

    from app.models.assignment import Assignment

    # Phase 0: per built-in agent, the set of assignment names we own.
    desired_by_slug: dict[str, set[str]] = {}
    for data in assignments:
        desired_by_slug.setdefault(data["agent_slug"], set()).add(data["name"])

    # Phase 1: remove stale seeded assignments (e.g. renamed ones) on these
    # built-in agents so we don't accumulate orphans across releases. Scoped to
    # agents we seed assignments for; built-in agents are read-only for
    # non-admins, so this won't touch user-authored automations elsewhere.
    # A fleet orchestrator's weekly health check (#11700) and its answer listener (#1406) are not seed rows:
    # the fleet service owns it, so it is never removed as stale here.
    from app.services.fleets import FLEET_HEALTH_ASSIGNMENT_NAMES

    removed = 0
    for slug, names in desired_by_slug.items():
        names = names | FLEET_HEALTH_ASSIGNMENT_NAMES
        res = await db.execute(select(Agent).where(Agent.slug == slug))
        agent = res.scalar_one_or_none()
        if not agent:
            continue
        stale_res = await db.execute(
            select(Assignment).where(
                Assignment.agent_id == agent.id,
                Assignment.name.notin_(names),
            )
        )
        for stale in stale_res.scalars().all():
            logger.info(
                "Removing stale built-in assignment '%s' (agent '%s')",
                stale.name,
                slug,
            )
            await db.delete(stale)
            removed += 1
    if removed:
        await db.flush()

    inserted = 0
    for data in assignments:
        agent_slug = data["agent_slug"]
        res = await db.execute(select(Agent).where(Agent.slug == agent_slug))
        agent = res.scalar_one_or_none()
        if not agent:
            logger.warning(
                "Cannot seed assignment '%s': agent '%s' not found",
                data["name"],
                agent_slug,
            )
            continue

        res = await db.execute(
            select(Assignment)
            .where(
                Assignment.agent_id == agent.id,
                Assignment.name == data["name"],
            )
            .order_by(Assignment.created_at)
            .limit(1)
        )
        # Use first() (not scalar_one_or_none()) so a duplicate (agent_id, name)
        # pair - there is no uniqueness constraint enforcing it - can never raise
        # MultipleResultsFound and crash startup seeding (blocking boot).
        existing = res.scalars().first()

        if existing:
            desired_enabled = data.get("enabled", True)
            existing.description = data.get("description")
            existing.trigger_type = data["trigger_type"]
            # The provisioning state (ids of the OpenCTI / OpenAEV triggers
            # created) survives the rewrite: without it a switched-off
            # assignment's triggers could never be removed (#9160).
            kept = existing.trigger_config if isinstance(existing.trigger_config, dict) else {}
            existing.trigger_config = {
                **seeded_trigger_config(data["trigger_type"], data.get("trigger_config")),
                **{k: kept[k] for k in PROVISIONING_STATE_KEYS if k in kept},
            }
            existing.prompt = data["prompt"]
            was_enabled = existing.enabled
            emergency_stopped = is_emergency_stopped(existing)
            if data.get("admin_opt_in"):
                # The administrator owns this switch: the seed created
                # it off, and from then on whether it runs - and as whom - is
                # their decision, which a boot must neither revert nor re-stamp.
                # The exception to "the seed is the source of truth",
                # for an automation that writes shared data under an identity
                # only an administrator can choose.
                pass
            elif desired_enabled and emergency_stopped:
                # An emergency stop outlives a restart: the stop's resume
                # re-enables this assignment, a boot never does. The stamp
                # stays, so the resume still finds it.
                logger.info(
                    "Built-in assignment '%s' (agent '%s') stays paused: an "
                    "emergency stop is in effect.",
                    data["name"],
                    agent_slug,
                )
            elif desired_enabled:
                existing.enabled = True
                # Drop any pause stamp along with the pause it
                # explains — an enabled assignment must never keep claiming it
                # was paused by the quarantine three boots ago.
                #
                # The seed is the source of truth for built-in assignments:
                # anything that disabled one at runtime (manual toggle, legacy
                # auto-quarantine) is reverted on every boot.
                #
                # Seeding has no acting user, so there is no ``resume`` audit
                # row to pair with the ``pause`` one this clears. Left silent,
                # the audit trail would show a pause that never ends — the
                # "paused since Tuesday" ambiguity the resume row exists to
                # prevent. This log line is where that trail is closed, so it
                # names what is being overridden (one WARNING for the whole
                # event, not an INFO and a WARNING for the same boot).
                if not was_enabled:
                    logger.warning(
                        "Re-enabling built-in assignment '%s' (agent '%s'): "
                        "the seed defines it as enabled, overriding its pause "
                        "(reason=%s, by=%s, at=%s). No resume audit row is "
                        "written — seeding has no acting user.",
                        data["name"],
                        agent_slug,
                        existing.paused_reason or "unrecorded",
                        existing.paused_by_label or "unrecorded",
                        existing.paused_at.isoformat()
                        if existing.paused_at
                        else "unrecorded",
                    )
                clear_pause_provenance(existing)
                if not was_enabled:
                    # Enabled again: the schedule waits for its next
                    # occurrence, no catch-up of the ones missed while off.
                    mark_schedule_resumed(existing)
            else:
                existing.enabled = False
                if was_enabled or emergency_stopped:
                    # Seeding is switching this built-in OFF. Stamp it like
                    # every other pause path, or the assignment page shows a
                    # bare "Paused" badge an operator would read as a
                    # pre-tracking pause. Edge-triggered (``was_enabled``) so a
                    # boot that re-seeds an already-disabled assignment does not
                    # restamp ``paused_at`` with a fresh date on every restart.
                    # An emergency stop's pause is restamped too: lifting the
                    # stop must not re-enable what the deployment switched off.
                    pause_assignment(
                        existing,
                        reason="system_seed",
                        details=_SEED_DISABLED_DETAILS,
                        actor_user_id=None,
                        actor_label=_SEED_ACTOR_LABEL,
                    )
            existing.ai_label = data.get("ai_label", False)
            existing.notify_on_complete = data.get("notify_on_complete", False)
            existing.show_in_digest = data.get("show_in_digest", False)
            existing.max_iterations = data.get("max_iterations")
            existing.timeout_seconds = data.get("timeout_seconds", 300)
            existing.retry_on_failure = data.get("retry_on_failure", False)
            existing.max_retries = data.get("max_retries", 3)
            existing.run_as_all_users = data.get("run_as_all_users", False)
            existing.hide_runs = data.get("hide_runs")
            _sync_seeded_gates(existing, data)
            existing.is_company_managed = agent.is_company_managed
            if existing.user_id is None and agent.user_id is not None:
                existing.user_id = agent.user_id
            continue

        seeded = Assignment(
            agent_id=agent.id,
            user_id=agent.user_id,
            is_company_managed=agent.is_company_managed,
            name=data["name"],
            description=data.get("description"),
            trigger_type=data["trigger_type"],
            trigger_config=seeded_trigger_config(data["trigger_type"], data.get("trigger_config")),
            prompt=data["prompt"],
            enabled=data.get("enabled", True),
            ai_label=data.get("ai_label", False),
            notify_on_complete=data.get("notify_on_complete", False),
            show_in_digest=data.get("show_in_digest", False),
            max_iterations=data.get("max_iterations"),
            timeout_seconds=data.get("timeout_seconds", 300),
            retry_on_failure=data.get("retry_on_failure", False),
            max_retries=data.get("max_retries", 3),
            run_as_all_users=data.get("run_as_all_users", False),
            hide_runs=data.get("hide_runs"),
        )
        _sync_seeded_gates(seeded, data)
        sibling_name = data.get(INHERIT_SWITCH_FROM_KEY)
        if sibling_name and data.get("admin_opt_in"):
            # A listener added to a fleet an administrator already switched on
            # (#1388): born with its sibling's switch and run-as, or the fleet
            # would run without it until someone found it off.
            sibling = (
                (
                    await db.execute(
                        select(Assignment)
                        .where(Assignment.agent_id == agent.id, Assignment.name == sibling_name)
                        .order_by(Assignment.created_at)
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if sibling is not None and sibling.enabled and not is_emergency_stopped(sibling):
                seeded.enabled = True
                seeded.run_as_user_id = sibling.run_as_user_id
        if not seeded.enabled:
            # A seed row defined as disabled is born paused, and gets the same
            # stamp as the update branch above — otherwise a fresh install
            # renders it as "Paused: reason unknown" from the very first boot.
            # An opt-in one is awaiting an administrator, not switched off by
            # the deployment, and says so.
            opt_in = bool(data.get("admin_opt_in"))
            pause_assignment(
                seeded,
                reason="created_disabled" if opt_in else "system_seed",
                details=_SEED_OPT_IN_DETAILS if opt_in else _SEED_DISABLED_DETAILS,
                actor_user_id=None,
                actor_label=_SEED_ACTOR_LABEL,
            )
        db.add(seeded)
        inserted += 1
        logger.info(
            "Seeded built-in assignment '%s' for agent '%s'",
            data["name"],
            agent_slug,
        )

    await db.commit()
    if inserted:
        logger.info("Seeded %d built-in assignment(s)", inserted)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════════
# Pre-packaged knowledge bases (XTM One mode only)
# ═══════════════════════════════════════════════════════════════════════════════


def _sync_kb_fields(kb: KnowledgeBase, data: dict) -> None:
    """Overwrite mutable fields on an existing built-in knowledge base."""
    kb.name = data.get("name", kb.name)
    kb.description = data.get("description")
    kb.tags = data.get("tags", ["builtin"])
    if "chunk_size" in data:
        kb.chunk_size = data["chunk_size"]
    if "chunk_overlap" in data:
        kb.chunk_overlap = data["chunk_overlap"]


#: Where the image ships the repository's ``docs/`` (the Dockerfiles copy it
#: there); ``XTM_ONE_DOCS_DIR`` overrides it, and a source checkout falls back
#: to the repository's own ``docs/``.
_IMAGE_DOCS_DIR = pathlib.Path("/app/xtm_one_docs")
_CHECKOUT_DOCS_DIR = pathlib.Path(__file__).resolve().parents[3] / "docs"


def bundled_docs_dir() -> pathlib.Path | None:
    """The XTM One documentation this deployment ships, or ``None`` when it ships none."""
    override = os.environ.get("XTM_ONE_DOCS_DIR", "").strip()
    for candidate in (pathlib.Path(override) if override else None, _IMAGE_DOCS_DIR, _CHECKOUT_DOCS_DIR):
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


def _seed_files(folder: pathlib.Path, *, recursive: bool) -> list[tuple[str, pathlib.Path]]:
    """The documents of *folder* as (filename, path): the top-level files, or every
    Markdown file below it named by its relative path."""
    if not recursive:
        return [(p.name, p) for p in sorted(folder.iterdir()) if p.is_file()]
    return [
        (p.relative_to(folder).as_posix(), p)
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.suffix in (".md", ".mdx")
    ]


async def _seed_documents_from_folder(
    db: AsyncSession,
    kb: KnowledgeBase,
    folder_name: str,
    *,
    folder: pathlib.Path | None = None,
) -> int:
    """Load seed documents from a folder under seed_data/ (or *folder*, read
    recursively for its Markdown files) into a KB.

    Only seeds files that don't already exist (matched by filename).
    Returns the number of documents added.
    """
    recursive = folder is not None
    folder = folder if folder is not None else SEED_DATA_DIR / "knowledge_base" / folder_name
    if not folder.is_dir():
        logger.warning("Seed data folder not found: %s", folder)
        return 0

    inserted = 0
    updated = 0
    for filename, file_path in _seed_files(folder, recursive=recursive):
        content = file_path.read_text(encoding="utf-8")
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Check if document already exists
        existing = await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_id == kb.id,
                KnowledgeDocument.filename == filename,
            )
        )
        doc = existing.scalar_one_or_none()
        if doc:
            if doc.content_hash == content_hash:
                continue
            # Content changed — update.
            await reset_changed_document(db, doc)
            doc.content = content
            doc.content_hash = content_hash
            logger.info("Updated seed document '%s' in KB '%s'", filename, kb.name)
            updated += 1
            continue

        doc = KnowledgeDocument(
            knowledge_base_id=kb.id,
            filename=filename,
            source_type="text",
            content=content,
            content_hash=content_hash,
            status="pending",
        )
        db.add(doc)
        inserted += 1
        logger.info("Seeded document '%s' into KB '%s'", filename, kb.name)

    if inserted or updated:
        await db.flush()
        kb.document_count = (kb.document_count or 0) + inserted
        await bump_kb_revision(db, kb.id)

    return inserted + updated


async def _seed_kb_documents(db: AsyncSession, kb: KnowledgeBase, kb_data: dict[str, Any]) -> int:
    """Seed *kb* from what its entry names: the documentation the deployment
    ships (``bundled_docs``) or a folder under seed_data/ (``knowledge_base_seed``)."""
    if kb_data.get("bundled_docs"):
        docs = bundled_docs_dir()
        if docs is None:
            logger.warning("No bundled documentation found for knowledge base '%s'", kb_data["slug"])
            return 0
        return await _seed_documents_from_folder(db, kb, kb_data["slug"], folder=docs)
    return await _seed_documents_from_folder(db, kb, kb_data["knowledge_base_seed"])


async def _sync_kb_sources(
    db: AsyncSession,
    kb: KnowledgeBase,
    sync_sources_data: list[dict],
) -> None:
    """Synchronize sync sources on an existing built-in KB.

    Upserts sources by (source_type, config) fingerprint and removes stale ones.
    When sources change, purges existing documents so the new sources start fresh.
    """
    existing_result = await db.execute(
        select(KBSyncSource).where(KBSyncSource.knowledge_base_id == kb.id)
    )
    existing_sources = list(existing_result.scalars().all())

    def _fingerprint(src_type: str, config: dict) -> str:
        return f"{src_type}:{config.get('repo', config.get('url', ''))}"

    existing_map = {
        _fingerprint(s.source_type, s.config or {}): s for s in existing_sources
    }
    desired_fps: set[str] = set()

    for sync_data in sync_sources_data:
        fp = _fingerprint(sync_data["source_type"], sync_data["config"])
        desired_fps.add(fp)

        if fp in existing_map:
            src = existing_map[fp]
            src.source_type = sync_data["source_type"]
            src.config = sync_data["config"]
            src.sync_interval = sync_data["sync_interval"]
            src.enabled = True
        else:
            db.add(
                KBSyncSource(
                    knowledge_base_id=kb.id,
                    user_id=None,
                    source_type=sync_data["source_type"],
                    config=sync_data["config"],
                    sync_interval=sync_data["sync_interval"],
                    enabled=True,
                )
            )

    # Remove stale sources
    stale_removed = False
    for fp, src in existing_map.items():
        if fp not in desired_fps:
            await db.delete(src)
            stale_removed = True

    # If sources changed (stale removed or new added), purge all documents
    # and embeddings so the new sources start fresh.
    has_new = bool(desired_fps - set(existing_map.keys()))
    if stale_removed or has_new:
        from sqlalchemy import delete as sa_delete

        from app.models.embedding import Embedding

        emb_deleted = await db.execute(
            sa_delete(Embedding).where(Embedding.knowledge_base_id == kb.id)
        )
        doc_deleted = await db.execute(
            sa_delete(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_id == kb.id
            )
        )
        doc_count = doc_deleted.rowcount  # type: ignore[union-attr]
        if doc_count:
            kb.document_count = 0
            kb.chunk_count = 0
            await bump_kb_revision(db, kb.id)
            logger.info(
                "Purged %d document(s) + %d embedding(s) from KB '%s' — sync sources changed",
                doc_count,
                emb_deleted.rowcount,  # type: ignore[union-attr]
                kb.name,
            )


async def seed_builtin_knowledge_bases(
    db: AsyncSession, *, platform_mode: str = "copilot"
) -> int:
    """Seed pre-packaged knowledge bases in XTM One mode.

    Only runs when ``platform_mode`` is ``"xtm_one"`` or ``"dev"``.  KBs are
    created with ``is_builtin=True, is_company_managed=True`` and the admin as
    ``user_id`` (:func:`_resolve_seed_owner_id`), backfilled on legacy rows.
    Supports multiple sync sources per KB and document seeding from files.
    Full-sync on every startup: all fields are overwritten.
    """
    _seed_xtm = platform_mode in ("xtm_one", "dev")
    kbs = BUILTIN_KNOWLEDGE_BASES if _seed_xtm else []
    current_slugs = {kb["slug"] for kb in kbs}
    owner_id = await _resolve_seed_owner_id(db) if kbs else None

    # Load all existing built-in KBs once and index by slug tag
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.is_builtin.is_(True))
    )
    all_builtin: list[KnowledgeBase] = list(result.scalars().all())

    slug_to_kb: dict[str, KnowledgeBase] = {}
    for existing_kb in all_builtin:
        s = _seed_slug_tag(existing_kb.tags)
        if s:
            slug_to_kb[s] = existing_kb

    # Phase 1: Delete stale built-in KBs
    removed = 0
    for existing_kb in all_builtin:
        s = _seed_slug_tag(existing_kb.tags)
        if s and s not in current_slugs:
            logger.info("Removing stale built-in knowledge base: %s", existing_kb.name)
            await db.delete(existing_kb)
            removed += 1

    # Phase 2: Upsert current KBs
    inserted = 0
    docs_to_process: list[str] = []

    for kb_data in kbs:
        slug = kb_data["slug"]
        tags = kb_data.get("tags", ["builtin"])
        tags_with_slug = [*tags, f"slug:{slug}"]

        # Normalize sync sources: support both "sync_source" (singular, legacy)
        # and "sync_sources" (plural, preferred)
        sync_sources_data: list[dict] = kb_data.get("sync_sources", [])
        if not sync_sources_data and "sync_source" in kb_data:
            sync_sources_data = [kb_data["sync_source"]]

        existing = slug_to_kb.get(slug)

        if existing:
            if existing.user_id is None and owner_id is not None:
                existing.user_id = owner_id
            _sync_kb_fields(existing, kb_data)
            existing.tags = tags_with_slug

            # Import sources are seeded (and kept in sync) only while the user
            # has NOT customized them. Once an admin edits/adds/removes a source
            # on this built-in KB, ``sync_sources_customized`` is set and we stop
            # overwriting them so internal-mirror / airgapped URLs survive. When
            # not customized we always reconcile — even against an empty desired
            # list — so a seed that drops all sources still removes stale rows.
            # All other built-in fields above are still fully re-synced.
            if not existing.sync_sources_customized:
                await _sync_kb_sources(db, existing, sync_sources_data)

            # Seed/update documents from folder
            seed_folder = kb_data.get("knowledge_base_seed")
            if seed_folder or kb_data.get("bundled_docs"):
                added = await _seed_kb_documents(db, existing, kb_data)
                if added:
                    # Collect document IDs for processing
                    doc_result = await db.execute(
                        select(KnowledgeDocument.id).where(
                            KnowledgeDocument.knowledge_base_id == existing.id,
                            KnowledgeDocument.status == "pending",
                        )
                    )
                    docs_to_process.extend(str(r) for (r,) in doc_result.all())
            continue

        kb = KnowledgeBase(
            user_id=owner_id,
            is_company_managed=True,
            is_builtin=True,
            name=kb_data["name"],
            description=kb_data.get("description"),
            tags=tags_with_slug,
        )
        # The chunking settings must be on the row before its seed documents
        # are processed below: a new KB otherwise chunks them at the model
        # default and keeps those chunks after a later boot syncs the fields.
        _sync_kb_fields(kb, kb_data)
        kb.tags = tags_with_slug
        db.add(kb)
        await db.flush()
        slug_to_kb[slug] = kb

        # Create sync sources
        for sync_data in sync_sources_data:
            db.add(
                KBSyncSource(
                    knowledge_base_id=kb.id,
                    user_id=None,
                    source_type=sync_data["source_type"],
                    config=sync_data["config"],
                    sync_interval=sync_data["sync_interval"],
                    enabled=True,
                )
            )

        # Seed documents from folder
        seed_folder = kb_data.get("knowledge_base_seed")
        if seed_folder or kb_data.get("bundled_docs"):
            added = await _seed_kb_documents(db, kb, kb_data)
            if added:
                await db.flush()
                doc_result = await db.execute(
                    select(KnowledgeDocument.id).where(
                        KnowledgeDocument.knowledge_base_id == kb.id,
                        KnowledgeDocument.status == "pending",
                    )
                )
                docs_to_process.extend(str(r) for (r,) in doc_result.all())

        inserted += 1
        logger.info("Seeded built-in knowledge base: %s", slug)

    # Phase 3: Sync KB ↔ agent bindings (add missing, remove stale)
    await db.flush()

    # Build desired binding map: agent_slug → set of KB IDs
    desired_bindings: dict[str, set[str]] = {}
    for kb_data in kbs:
        kb_row = slug_to_kb.get(kb_data["slug"])
        if not kb_row:
            continue
        kb_id_str = str(kb_row.id)
        for agent_slug in kb_data.get("bind_to_agent_slugs", []):
            desired_bindings.setdefault(agent_slug, set()).add(kb_id_str)

    # Collect all built-in KB IDs for stale removal
    all_builtin_kb_ids = {str(kb.id) for kb in slug_to_kb.values()}

    # Load all built-in agents and sync their knowledge_base_ids
    builtin_agents_result = await db.execute(
        select(Agent).where(Agent.is_builtin.is_(True))
    )
    for agent_row in builtin_agents_result.scalars().all():
        agent_slug = agent_row.slug
        desired_kb_ids: set[str] = (
            desired_bindings.get(agent_slug, set()) if agent_slug else set()
        )
        current_ids = list(agent_row.knowledge_base_ids or [])
        updated = False

        # Add missing bindings
        for kb_id in desired_kb_ids:
            if kb_id not in current_ids:
                current_ids.append(kb_id)
                updated = True
                logger.info("Bound KB to agent '%s'", agent_row.name)

        # Remove stale built-in KB bindings
        stale = [
            kid
            for kid in current_ids
            if kid in all_builtin_kb_ids and kid not in desired_kb_ids
        ]
        for kid in stale:
            current_ids.remove(kid)
            updated = True
            logger.info("Unbound stale KB from agent '%s'", agent_row.name)

        if updated:
            agent_row.knowledge_base_ids = current_ids

    await db.commit()

    # Phase 4: Enqueue seed documents for embedding (after commit)
    if docs_to_process:
        try:
            from app.services.kb_helpers import enqueue_document_processing

            for doc_id in docs_to_process:
                await enqueue_document_processing(doc_id)
            logger.info(
                "Enqueued %d seed document(s) for embedding", len(docs_to_process)
            )
        except Exception as e:
            logger.warning("Could not enqueue seed documents for embedding: %s", format_exception_message(e))

    if inserted:
        logger.info("Seeded %d built-in knowledge base(s)", inserted)
    if removed:
        logger.info("Removed %d stale built-in knowledge base(s)", removed)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════════
# Pre-packaged skills (platform-targeted)
# ═══════════════════════════════════════════════════════════════════════════════


def seed_matches_platform(seed_data: dict, platform_mode: str) -> bool:
    """Return True when a built-in seed entry should exist in *platform_mode*.

    Reads the entry's ``platform_targets``. Supported targets:
    - "global": all platform modes
    - "copilot", "xtm_one", "dev": mode-specific

    Backward-compatible default when no explicit targets are set:
    xtm_one + dev only.

    Shared by the agent seed, the skill seed, the agent-template seed and the
    template-container seed so "which modes does this built-in ship in" is
    spelled exactly one way.
    """
    targets = seed_data.get("platform_targets")
    if not isinstance(targets, list) or not targets:
        return platform_mode in ("xtm_one", "dev")

    normalized = {str(t).strip().lower() for t in targets if str(t).strip()}
    if "global" in normalized:
        return True
    return platform_mode in normalized


# Historical name, kept for the existing call sites and tests.
_skill_matches_platform = seed_matches_platform


SKILL_RESOURCES_DIR = SEED_DATA_DIR / "skill_resources"


def _seed_skill_resource_files(skill_data: dict) -> dict[str, bytes]:
    """``relative path -> bytes`` of the resources a seed skill declares.

    ``"resources": True`` ships every file of ``seed_data/skill_resources/<slug>/``;
    ``"resource_files": {"LICENSE.md": "licenses/x.md"}`` ships one file of
    ``seed_data`` under the given path - a file several skills share (a licence)
    is kept once in the seed and copied into each skill that names it.
    """
    slug = skill_data["slug"]
    files: dict[str, bytes] = {}
    if skill_data.get("resources"):
        root = SKILL_RESOURCES_DIR / slug
        if root.is_dir():
            files.update(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in sorted(root.rglob("*"))
                    if path.is_file() and not path.name.startswith(".")
                }
            )
        else:
            logger.warning(
                "Seed skill '%s' declares resources but %s does not exist.", slug, root
            )
    for path, source in (skill_data.get("resource_files") or {}).items():
        seed_file = SEED_DATA_DIR / source
        if seed_file.is_file():
            files[path] = seed_file.read_bytes()
        else:
            logger.warning(
                "Seed skill '%s' names resource %s but %s does not exist.",
                slug,
                path,
                seed_file,
            )
    return files


def _declares_resources(skill_data: dict) -> bool:
    return bool(skill_data.get("resources") or skill_data.get("resource_files"))


async def _sync_skill_resources(db: AsyncSession, skill: AgentSkill, skill_data: dict) -> None:
    """Make a built-in skill's resources exactly the files its seed entry declares.

    Only for seed entries declaring ``"resources"`` or ``"resource_files"``
    (:func:`_seed_skill_resource_files`): bundled ``references/`` files the
    agent reads with ``read_skill_resource``, or a licence that must travel
    with every copy of the skill (an export, a catalog clone).
    Full sync like the body - a file changed in the seed is rewritten, one
    removed from it is deleted - and keyed by path, so a steady-state boot
    writes nothing.
    """
    import mimetypes

    from app.models.agent_skill_resource import AgentSkillResource
    from app.services.skill_package_manager import sha256_hex

    wanted = _seed_skill_resource_files(skill_data)
    result = await db.execute(
        select(AgentSkillResource).where(AgentSkillResource.skill_id == skill.id)
    )
    existing = {row.path: row for row in result.scalars().all()}
    for path, row in existing.items():
        if path not in wanted:
            await db.delete(row)
    for path, content in wanted.items():
        digest = sha256_hex(content)
        row = existing.get(path)
        if row is not None and row.sha256 == digest:
            continue
        if row is None:
            row = AgentSkillResource(skill_id=skill.id, path=path)
            db.add(row)
        row.content = content
        row.content_type = mimetypes.guess_type(path)[0]
        row.size = len(content)
        row.sha256 = digest


def _seeded_builtin_skills(platform_mode: str) -> list[dict]:
    """``BUILTIN_SKILLS`` as *platform_mode* seeds them, without a skill whose feature flag is off."""
    return [
        skill
        for skill in BUILTIN_SKILLS
        if _skill_matches_platform(skill, platform_mode)
        and (
            skill["slug"] not in FEATURE_GATED_BUILTIN_SKILLS
            or is_feature_enabled(FEATURE_GATED_BUILTIN_SKILLS[skill["slug"]])
        )
    ]


def _sync_skill_fields(skill: AgentSkill, data: dict) -> None:
    """Overwrite mutable fields on an existing built-in skill."""
    skill.name = data.get("name", skill.name)
    skill.description = data.get("description")
    skill.content = _resolve_seed_content("skill_content", data.get("content", data["slug"]))
    skill.compatibility = data.get("compatibility")
    skill.license = data.get("license")
    skill.tags = data.get("tags", ["builtin"])


def _adopt_renamed_skills(
    rows: list[AgentSkill], slug_to_skill: dict[str, AgentSkill], current_slugs: set[str]
) -> set[int]:
    """Key each row still tagged with a former slug (``RENAMED_BUILTIN_SKILL_SLUGS``) under its
    current slug in *slug_to_skill*, so the upsert retags that row in place and every agent's
    ``skill_ids`` keeps pointing at it. A row already carrying the current slug wins: the former
    row is then left to Phase 1 as stale. Answers the ``id()`` of each adopted row.
    """
    adopted: set[int] = set()
    for row in rows:
        current = RENAMED_BUILTIN_SKILL_SLUGS.get(_seed_slug_tag(row.tags) or "")
        if current and current in current_slugs and current not in slug_to_skill:
            slug_to_skill[current] = row
            adopted.add(id(row))
            logger.info("Adopting built-in skill %s under its renamed slug %s", row.name, current)
    return adopted


async def seed_builtin_skills(
    db: AsyncSession, *, platform_mode: str = "copilot"
) -> int:
    """Seed pre-packaged skills for the current platform mode.

    Skills are created with ``is_company_managed=True`` and the admin as
    ``user_id`` (:func:`_resolve_seed_owner_id`), backfilled on legacy rows.
    Full-sync on every startup: all fields are overwritten. A skill whose
    feature flag is off (``FEATURE_GATED_BUILTIN_SKILLS``) is not
    seeded, so Phase 1 removes a copy seeded while the flag was on.
    """
    skills = _seeded_builtin_skills(platform_mode)
    current_slugs = {s["slug"] for s in skills}
    owner_id = await _resolve_seed_owner_id(db) if skills else None

    # Load existing built-in skills by slug tag
    result = await db.execute(
        select(AgentSkill).where(AgentSkill.is_company_managed.is_(True))
    )
    all_managed: list[AgentSkill] = list(result.scalars().all())

    slug_to_skill: dict[str, AgentSkill] = {}
    for existing_skill in all_managed:
        s = _seed_slug_tag(existing_skill.tags)
        if s and s not in RENAMED_BUILTIN_SKILL_SLUGS:
            slug_to_skill[s] = existing_skill
    adopted = _adopt_renamed_skills(all_managed, slug_to_skill, current_slugs)

    # Phase 1: Delete stale built-in skills
    removed = 0
    for existing_skill in all_managed:
        s = _seed_slug_tag(existing_skill.tags)
        if (s and s in current_slugs) or id(existing_skill) in adopted:
            continue
        if s and s not in current_slugs and "builtin" in (existing_skill.tags or []):
            logger.info("Removing stale built-in skill: %s", existing_skill.name)
            await db.delete(existing_skill)
            removed += 1

    # Phase 2: Upsert current skills
    inserted = 0
    for skill_data in skills:
        slug = skill_data["slug"]
        tags = skill_data.get("tags", ["builtin"])
        tags_with_slug = [*tags, f"slug:{slug}"]

        existing = slug_to_skill.get(slug)

        if existing:
            if existing.user_id is None and owner_id is not None:
                existing.user_id = owner_id
            _sync_skill_fields(existing, skill_data)
            existing.tags = tags_with_slug
            if _declares_resources(skill_data):
                await _sync_skill_resources(db, existing, skill_data)
            continue

        skill = AgentSkill(
            user_id=owner_id,
            is_company_managed=True,
            name=skill_data["name"],
            description=skill_data.get("description"),
            url=skill_data.get("url"),
            content=_resolve_seed_content("skill_content", skill_data.get("content", slug)),
            compatibility=skill_data.get("compatibility"),
            license=skill_data.get("license"),
            tags=tags_with_slug,
            enabled=True,
        )
        db.add(skill)
        await db.flush()
        if _declares_resources(skill_data):
            await _sync_skill_resources(db, skill, skill_data)
        slug_to_skill[slug] = skill
        inserted += 1
        logger.info("Seeded built-in skill: %s", slug)

    # Phase 3: Bind skills to their target agents
    await db.flush()
    for skill_data in skills:
        agent_slugs = skill_data.get("bind_to_agent_slugs", [])
        if not agent_slugs:
            continue

        skill_row = slug_to_skill.get(skill_data["slug"])
        if not skill_row:
            continue

        skill_id_str = str(skill_row.id)
        for agent_slug in agent_slugs:
            agent_result = await db.execute(
                select(Agent).where(Agent.slug == agent_slug)
            )
            agent_row = agent_result.scalar_one_or_none()
            if not agent_row:
                continue

            current_ids = list(agent_row.skill_ids or [])
            if skill_id_str not in current_ids:
                current_ids.append(skill_id_str)
                agent_row.skill_ids = current_ids
                logger.info(
                    "Bound skill '%s' to agent '%s'", skill_row.name, agent_row.name
                )

    await db.commit()
    if inserted:
        logger.info("Seeded %d built-in skill(s)", inserted)
    if removed:
        logger.info("Removed %d stale built-in skill(s)", removed)
    return inserted


async def seed_builtin_integrations(
    db: AsyncSession, *, platform_mode: str = "copilot"
) -> int:
    """Seed pre-packaged integrations in XTM One mode.

    Creates company-managed integrations with encrypted credentials on first
    startup.  Every restart rewrites the credentials (``api_key_enc`` /
    ``extra_config_enc``) from the resolved config, env vars included, and
    refreshes agent bindings and the ``status`` field for auth-free
    integrations.
    Returns the number of integrations newly inserted.
    """
    _seed_xtm = platform_mode in ("xtm_one", "dev")
    if not _seed_xtm:
        return 0

    log_builtin_integration_status()

    if not BUILTIN_INTEGRATIONS:
        return 0

    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB

    from app.core.json_sql import when_json_parseable
    from app.core.security import encrypt_value
    from app.models.integration import Integration

    owner_id = await _resolve_seed_owner_id(db)
    # One integration's tags holding a lone surrogate escape aborted this cast,
    # and with it the startup provisioning. Such a row is returned and
    # decided on its decoded tags, as ``@>`` decides the others: skipping it
    # would keep a stale built-in's bindings, the prune below never seeing it.
    tagged_builtin_or_unreadable = when_json_parseable(
        Integration.tags,
        cast(Integration.tags, JSONB).contains(["builtin"]),
        otherwise=True,
    )

    def _tagged_builtin(tags: object) -> bool:
        return isinstance(tags, list) and "builtin" in tags

    # ── Phase 1: Remove stale builtin integrations ──
    current_fingerprints = {
        (d["provider"], d["name"]) for d in BUILTIN_INTEGRATIONS
    }
    stale_result = await db.execute(
        select(Integration).where(
            # access_restricted: bypass - startup seed of the built-in integrations, no acting user.
            Integration.is_company_managed.is_(True),
            tagged_builtin_or_unreadable,
        )
    )
    removed = 0
    for integ in stale_result.scalars().all():
        if _tagged_builtin(integ.tags) and (integ.provider, integ.name) not in current_fingerprints:
            logger.info(
                "Removing stale built-in integration: %s (%s)", integ.name, integ.provider
            )
            await db.delete(integ)
            removed += 1
    if removed:
        await db.flush()
        logger.info("Removed %d stale built-in integration(s)", removed)

    # ── Phase 2: Upsert integrations + build expected bindings ──
    # Map: agent_slug → set of integration IDs that should be bound
    expected_bindings: dict[str, dict[str, set[str]]] = {}  # slug → {provider → {id}}

    inserted = 0

    for data in BUILTIN_INTEGRATIONS:
        provider = data["provider"]
        name = data["name"]

        result = await db.execute(
            select(Integration).where(
                Integration.provider == provider,
                Integration.name == name,
                # access_restricted: bypass - startup seed of the built-in integrations, no acting user.
                Integration.is_company_managed.is_(True),
            )
        )
        integration = result.scalar_one_or_none()

        if integration is None:
            integration = Integration(
                provider=provider,
                name=name,
                auth_type=data.get("auth_type", "api_key"),
                is_company_managed=True,
                is_builtin=True,
                user_id=owner_id,
                status="connected",
                tags=data.get("tags", ["builtin"]),
            )
            if data.get("description"):
                integration.description = data["description"]

            db.add(integration)
            await db.flush()
            inserted += 1
            logger.info("Created built-in integration '%s' (%s)", name, provider)

        # Backfill the owner on legacy rows seeded as NULL before the
        # always-set-owner invariant.
        if integration.user_id is None and owner_id is not None:
            integration.user_id = owner_id

        # Always refresh credentials from resolved config (env var overrides)
        credentials = get_builtin_integration_credentials(provider)
        if credentials.get("api_key"):
            integration.api_key_enc = encrypt_value(credentials["api_key"])
        if credentials.get("extra_config"):
            import json as _json
            integration.extra_config_enc = encrypt_value(
                _json.dumps(credentials["extra_config"])
            )

        # auth_type=none integrations are always connected (no credentials needed)
        if data.get("auth_type") == "none":
            integration.status = "connected"

        # Ensure is_builtin flag is set (backfill for pre-migration rows)
        if not integration.is_builtin:
            integration.is_builtin = True

        int_id_str = str(integration.id)

        # Record expected bindings
        for agent_slug in data.get("bind_to_agent_slugs", []):
            expected_bindings.setdefault(agent_slug, {}).setdefault(provider, set()).add(int_id_str)

        # Add missing bindings
        for agent_slug in data.get("bind_to_agent_slugs", []):
            r = await db.execute(select(Agent).where(Agent.slug == agent_slug))
            agent = r.scalar_one_or_none()
            if not agent:
                continue
            bindings = dict(agent.integration_bindings or {})
            current_ids = bindings.get(provider, [])
            if int_id_str not in current_ids:
                bindings[provider] = [*current_ids, int_id_str]
                agent.integration_bindings = bindings
                logger.info("Bound integration '%s' to agent '%s'", name, agent.name)

    # ── Phase 3: Remove stale bindings from agents ──
    # For each builtin agent, remove integration IDs that are no longer expected
    all_builtin_integration_ids: set[str] = set()
    for per_provider in expected_bindings.values():
        for ids in per_provider.values():
            all_builtin_integration_ids.update(ids)

    # Also collect IDs of all current builtin integrations
    all_bi_result = await db.execute(
        select(Integration.id, Integration.tags).where(
            # access_restricted: bypass - startup seed of the built-in integrations, no acting user.
            Integration.is_company_managed.is_(True),
            tagged_builtin_or_unreadable,
        )
    )
    all_builtin_ids = {str(row[0]) for row in all_bi_result.all() if _tagged_builtin(row[1])}

    for agent_data in BUILTIN_AGENTS:
        slug = agent_data["slug"]
        r = await db.execute(select(Agent).where(Agent.slug == slug))
        agent = r.scalar_one_or_none()
        if not agent or not agent.integration_bindings:
            continue
        bindings = dict(agent.integration_bindings)
        changed = False
        expected_for_agent = expected_bindings.get(slug, {})
        for provider, ids in list(bindings.items()):
            expected_ids = expected_for_agent.get(provider, set())
            # Only remove builtin integration IDs that are stale
            cleaned = [
                iid for iid in ids
                if iid not in all_builtin_ids or iid in expected_ids
            ]
            if len(cleaned) != len(ids):
                bindings[provider] = cleaned
                changed = True
                logger.info(
                    "Cleaned stale integration bindings for agent '%s' provider '%s'",
                    slug, provider,
                )
            # Remove empty provider keys
            if not cleaned:
                del bindings[provider]
                changed = True
        if changed:
            agent.integration_bindings = bindings

    await db.commit()
    if inserted:
        logger.info("Seeded %d built-in integration(s)", inserted)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════════
# Pre-packaged intent bindings (XTM One mode only)
# ═══════════════════════════════════════════════════════════════════════════════

# Editing-style intents bound identically to every vertical assistant —
# declared once and expanded across the (agent, vertical) pairs in
# ``_EDITING_INTENT_AGENTS`` below so a future tweak to a description or
# priority only has to land in one place. A new vertical assistant must be
# registered there as well as in its own ``supported_intents``; the invariant
# test in ``tests/test_builtin_intent_bindings_invariants.py`` fails otherwise.
_EDITING_INTENTS: list[tuple[str, str]] = [
    ("global.make_it_shorter", "Shorten / summarize content"),
    ("global.make_it_longer", "Expand / elaborate content"),
    ("global.fix_spelling", "Fix spelling and grammar"),
    ("global.change_tone", "Change the tone of content"),
    ("global.summarize", "Summarize content"),
    ("global.explain", "Explain content in simple terms"),
]

_EDITING_INTENT_AGENTS: list[tuple[str, str]] = [
    ("opencti-assistant", "cti"),
    ("openaev-assistant", "aev"),
    ("opencrq-assistant", "crq"),
    ("fimi-analyst", "fimi"),
]


def _expand_editing_intent_bindings() -> list[dict]:
    """Cross-product the editing-intent table with the vertical agents.

    Each editing intent gets one row per ``(agent_slug, vertical)``
    pair so a CTI or AEV chat surfaces the same six editing actions
    against its vertical-scoped assistant.
    """
    return [
        {
            "intent_name": name,
            "intent_description": description,
            "agent_slug": agent_slug,
            "vertical": vertical,
            "priority": 0,
        }
        for name, description in _EDITING_INTENTS
        for agent_slug, vertical in _EDITING_INTENT_AGENTS
    ]


# Declarative list: each entry creates an intent (if missing), a vertical (if
# provided and missing), and an IntentBinding linking the agent to the intent.
# ── Intent descriptions (single source of truth) ──
_INTENT_DESCRIPTIONS: dict[str, str] = {
    "global.assistant": "General-purpose assistant",
    "global.make_it_shorter": "Shorten / summarize content",
    "global.make_it_longer": "Expand / elaborate content",
    "global.fix_spelling": "Fix spelling and grammar",
    "global.change_tone": "Change the tone of content",
    "global.summarize": "Summarize content",
    "global.explain": "Explain content in simple terms",
    "aev.attack_path_orchestrator": "Autonomously drive a live, objective-driven attack path (AI penetration testing) in OpenAEV",
    "aev.attack_path_additional_agent": "Specialist agent the OpenAEV attack-path orchestrator can consult during an autonomous run (payload creation, code generation, recon, exploitation support)",
    "aev.detection_rules_generator": "Generate detection rules from malware behavior",
    "aev.phishing_email_html_generator": "Generate phishing lure email HTML content from a user prompt for OpenAEV adversary-emulation exercises",
    "aev.phishing_landing_page_html_generator": "Generate phishing landing page HTML and CSS from a user prompt for OpenAEV credential-capture simulations",
    "aev.message_generator": "Generate messages for adversary emulation exercises",
    "aev.media_article_generator": "Generate media articles for adversary emulation",
    "cti.nlq_search": "Generate an OpenCTI filter from a natural language query",
    "cti.container_summary": "Summarize an OpenCTI container (report, grouping, case)",
    "cti.container_report": "Write a report from an OpenCTI container (report, grouping, case)",
    "cti.containers_digest": "Summarize containers related to an OpenCTI entity",
    "cti.entity_activity": "Analyse activity trends of an OpenCTI entity",
    "cti.entity_forecast": "Forecast future activity of an OpenCTI entity",
    "cti.entity_history": "Summarize internal history of an OpenCTI entity",
    "cti.ttp_harvester": "Extract TTPs (Tactics, Techniques and Procedures) from documents",
    "cti.stix_harvester": "Extract cyber threat intelligence from documents into STIX 2.1 bundles",
    "cti.stix_transformer": "Transform a STIX 2.1 bundle (enrich, filter, rewrite, normalize) and return a valid STIX 2.1 bundle",
    "cti.stix_consumer": "Consume a STIX 2.1 bundle as the final step of an OpenCTI playbook (summarize, alert, post, dispatch)",
    "crq.control_gap_analysis": "Analyze control capability gaps against a compliance framework",
    "crq.risk_scenario_generator": "Draft risk scenarios from business and threat context",
    "crq.assessment_summary": "Summarize a compliance assessment and its requirement answers",
}

def _build_intent_bindings() -> list[dict]:
    """Auto-generate intent bindings from BUILTIN_AGENTS declarations.

    ``supported_intents`` is a dict mapping intent name → priority config:

    - **int** → no vertical scoping, creates a single binding with ``vertical=None``.
      ``{"global.assistant": 0}``
    - **dict[str, int]** → per-vertical priority, creates one binding per vertical.
      ``{"global.assistant": {"cti": 10, "aev": 5}}``
    """
    bindings: list[dict] = []
    for agent_data in BUILTIN_AGENTS:
        intents = agent_data.get("supported_intents") or {}
        # Backward compat: accept list (all priority 0, no vertical)
        if isinstance(intents, list):
            intents = {name: 0 for name in intents}
        slug = agent_data["slug"]

        for intent_name, priority_config in intents.items():
            if isinstance(priority_config, dict):
                # Per-vertical priorities
                for vertical, priority in priority_config.items():
                    bindings.append({
                        "intent_name": intent_name,
                        "intent_description": _INTENT_DESCRIPTIONS.get(intent_name),
                        "agent_slug": slug,
                        "vertical": vertical,
                        "priority": priority,
                    })
            else:
                # Scalar priority → no vertical scoping
                bindings.append({
                    "intent_name": intent_name,
                    "intent_description": _INTENT_DESCRIPTIONS.get(intent_name),
                    "agent_slug": slug,
                    "vertical": None,
                    "priority": priority_config,
                })
    return bindings


BUILTIN_INTENT_BINDINGS: list[dict] = _build_intent_bindings()


async def seed_builtin_intent_bindings(
    db: AsyncSession, *, platform_mode: str = "copilot"
) -> int:
    """Seed pre-packaged intent × agent bindings in XTM One mode.

    Ensures referenced intents, verticals, and binding rows exist.
    Idempotent — skips rows that already exist.
    """
    _seed_xtm = platform_mode in ("xtm_one", "dev")
    if not _seed_xtm:
        return 0

    from app.api.v1.helpers import get_or_create_intent, get_or_create_vertical
    from app.models.intent import Intent, IntentBinding

    # ── Phase 1: Remove stale builtin intent bindings ──
    # Collect current (intent_name, agent_slug, vertical) fingerprints
    current_fingerprints = {
        (e["intent_name"], e["agent_slug"], e.get("vertical"))
        for e in BUILTIN_INTENT_BINDINGS
    }

    # Resolve builtin agent slugs → IDs
    builtin_slugs = {a["slug"] for a in BUILTIN_AGENTS}
    builtin_agent_ids: set = set()
    for slug in builtin_slugs:
        res = await db.execute(select(Agent.id).where(Agent.slug == slug))
        aid = res.scalar_one_or_none()
        if aid:
            builtin_agent_ids.add(aid)

    # Fetch ALL system-level bindings (user_id=NULL) for builtin agents
    # regardless of is_builtin flag — catches legacy bindings too
    all_bindings_result = await db.execute(
        select(IntentBinding).where(
            IntentBinding.user_id.is_(None),
            IntentBinding.agent_id.in_(builtin_agent_ids) if builtin_agent_ids else False,
        )
    )
    removed_bindings = 0
    for binding in all_bindings_result.scalars().all():
        # Resolve intent name and agent slug for this binding
        intent_res = await db.execute(
            select(Intent.name).where(Intent.id == binding.intent_id)
        )
        intent_name = intent_res.scalar_one_or_none()
        agent_res = await db.execute(
            select(Agent.slug).where(Agent.id == binding.agent_id)
        )
        agent_slug = agent_res.scalar_one_or_none()
        if not intent_name or not agent_slug:
            await db.delete(binding)
            removed_bindings += 1
            continue
        if (intent_name, agent_slug, binding.vertical) not in current_fingerprints:
            logger.info(
                "Removing stale intent binding: %s → %s (vertical=%s)",
                intent_name, agent_slug, binding.vertical or "all",
            )
            await db.delete(binding)
            removed_bindings += 1

    # Remove orphaned intents (no remaining bindings)
    if removed_bindings:
        all_intents_result = await db.execute(select(Intent))
        for intent in all_intents_result.scalars().all():
            binding_count = await db.execute(
                select(IntentBinding.id).where(
                    IntentBinding.intent_id == intent.id
                ).limit(1)
            )
            if not binding_count.scalar_one_or_none():
                logger.info("Removing orphaned intent: %s", intent.name)
                await db.delete(intent)

    if removed_bindings:
        await db.flush()
        logger.info("Removed %d stale intent binding(s)", removed_bindings)

    # ── Phase 2: Upsert current bindings ──
    inserted = 0
    # An agent withheld by its feature flag is not seeded, so it has nothing
    # to bind.
    withheld_slugs = _withheld_builtin_agent_slugs()

    for entry in BUILTIN_INTENT_BINDINGS:
        intent_name = entry["intent_name"]
        agent_slug = entry["agent_slug"]
        if agent_slug in withheld_slugs:
            continue
        vertical_name = entry.get("vertical")
        priority = entry.get("priority", 0)

        # ── Ensure intent exists (race-safe ON CONFLICT upsert) ──
        intent, intent_created = await get_or_create_intent(
            db, intent_name, entry.get("intent_description")
        )
        if intent_created:
            logger.info("Created intent: %s", intent_name)

        # ── Ensure vertical exists (if specified) ──
        if vertical_name:
            _, vertical_created = await get_or_create_vertical(
                db, vertical_name, source="builtin"
            )
            if vertical_created:
                logger.info("Created vertical: %s", vertical_name)

        # ── Resolve agent ──
        agent_result = await db.execute(select(Agent).where(Agent.slug == agent_slug))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            logger.warning(
                "Cannot bind intent '%s': agent '%s' not found",
                intent_name,
                agent_slug,
            )
            continue

        # ── Ensure binding exists ──
        vertical_filter = (
            IntentBinding.vertical == vertical_name
            if vertical_name is not None
            else IntentBinding.vertical.is_(None)
        )
        binding_result = await db.execute(
            select(IntentBinding).where(
                IntentBinding.intent_id == intent.id,
                IntentBinding.agent_id == agent.id,
                vertical_filter,
                IntentBinding.user_id.is_(None),
            )
        )
        existing_binding = binding_result.scalar_one_or_none()
        if existing_binding:
            # Sync priority, clear overrides, and ensure is_builtin flag
            changed = False
            if existing_binding.priority != priority:
                existing_binding.priority = priority
                changed = True
            if existing_binding.priority_overrides:
                existing_binding.priority_overrides = {}
                changed = True
            if not existing_binding.is_builtin:
                existing_binding.is_builtin = True
                changed = True
            if changed:
                await db.flush()
                logger.info(
                    "Updated binding '%s' → '%s' (vertical=%s): priority=%d",
                    agent_slug, intent_name, vertical_name or "all", priority,
                )
            continue

        db.add(
            IntentBinding(
                intent_id=intent.id,
                agent_id=agent.id,
                vertical=vertical_name,
                user_id=None,
                is_builtin=True,
                priority=priority,
            )
        )
        inserted += 1
        logger.info(
            "Bound agent '%s' to intent '%s' (vertical=%s)",
            agent_slug,
            intent_name,
            vertical_name or "all",
        )

    await db.commit()
    if inserted:
        logger.info("Seeded %d intent binding(s)", inserted)
    return inserted
