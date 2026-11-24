# SPDX-License-Identifier: Apache-2.0

"""Fill the organization profile from a connected integration with an agent (#11888, PR 2).

The AI onboarding of Settings > Organization offers "Fill from a connected
integration": the administrator picks an integration (Intune, an EDR, a cloud
inventory, a CMDB) and gets, in one click, an agent bound to it read-only and
to the ``organization_context`` group, then either a chat with that agent that
previews the rows, or a scheduled assignment "Keep my assets in sync from
<integration>".

The agent writes through the row tools of the group (``upsert_organization_rows``,
``remove_organization_rows``): hand rows are never touched, a preview comes
first in chat, and the scheduled run applies. One agent per integration and
administrator is reused (tag :data:`FILL_AGENT_TAG_PREFIX` + integration id),
so a second click never multiplies agents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.assignment import Assignment, new_trigger_config
from app.models.integration import Integration

#: Tag naming the integration a fill agent reads, ``org-fill:<integration id>``.
FILL_AGENT_TAG_PREFIX = "org-fill:"
#: Every day at 06:00 in the administrator's zone.
DEFAULT_SYNC_CRON = "0 6 * * *"
#: The shared-context warning the fill prompts open with (#11994): what the person accepts by applying.
SHARED_CONTEXT_PROMPT = (
    "Note: this reads the integration with my access, does not keep its segregation or markings, and the "
    "organization context it fills is visible to every user and every agent of this platform."
)
#: Providers whose raw objects the row tools map on the server (``rows_format``).
_SERVER_MAPPED: dict[str, tuple[str, str]] = {
    # provider -> (list tool the agent pages through, rows_format)
    "intune": ("intune_list_devices", "intune_device"),
}


@dataclass(frozen=True)
class FillAgent:
    agent: Agent
    assignment: Assignment | None
    created: bool
    prompt: str


def fill_agent_tag(integration_id: object) -> str:
    return f"{FILL_AGENT_TAG_PREFIX}{integration_id}"


def fill_prompt(provider: str, integration_name: str, *, apply: bool) -> str:
    """The instruction a fill agent follows: preview in chat, apply on the schedule.

    Intune devices are mapped on the server (``rows_format intune_device``);
    any other platform's devices or assets are mapped by the agent to profile
    rows with their platform id as ``external_id``.
    """
    source = provider
    mapped = _SERVER_MAPPED.get(provider)
    if mapped:
        list_tool, row_format = mapped
        read = f"List every managed device in {integration_name} with {list_tool}, paging through all of them."
        write = f"upsert_organization_rows, section assets, source {source}, rows_format {row_format}"
    else:
        read = (
            f"List every device, host or cloud asset {integration_name} knows with its read tools, "
            "paging through all of them."
        )
        write = (
            f"upsert_organization_rows, section assets, source {source}, rows_format profile, each row with "
            "name, category and the platform's own id as external_id"
        )
    if apply:
        return (
            f"{SHARED_CONTEXT_PROMPT} {read} Upsert them into the organization assets with {write}, apply true, "
            f"in batches of at most 500 rows. Then remove with remove_organization_rows the {source} assets whose id "
            f"{integration_name} no longer lists. Report the counts and every refused row with its reason."
        )
    return (
        f"{SHARED_CONTEXT_PROMPT} {read} Then preview them into the organization assets with {write}, apply false, "
        "in batches of at most 500 rows. Show me the counts and every refused row with its reason, and tell me "
        "first that the organization context is shared. Apply only when I say so."
    )


def _persona(integration_name: str) -> str:
    return (
        f"You keep the organization profile's assets in line with {integration_name}. You read it with its "
        "read tools only and write the profile with the organization profile row tools, never "
        "update_organization_context on a whole list. Rows a person wrote are never yours to change. "
        "In chat, preview first and apply only on the person's confirmation."
    )


async def _existing_agent(db: AsyncSession, owner_id: uuid.UUID, integration_id: uuid.UUID) -> Agent | None:
    rows = await db.execute(select(Agent).where(Agent.user_id == owner_id, Agent.is_company_managed.is_(False)))
    tag = fill_agent_tag(integration_id)
    return next((agent for agent in rows.scalars() if tag in (agent.tags or [])), None)


async def ensure_fill_agent(
    db: AsyncSession,
    *,
    admin_id: uuid.UUID,
    integration: Integration,
    schedule: bool,
    cron: str = DEFAULT_SYNC_CRON,
    timezone: str | None = None,
) -> FillAgent:
    """The administrator's fill agent for *integration*, created once; with *schedule*, its sync assignment too.

    The caller checked that the administrator may bind *integration* and
    validated *cron*. The binding is read-only: the agent never writes to the
    platform it reads.
    """
    from app.api.v1.helpers import generate_unique_agent_slug
    from app.services.agent_config_versioning import seed_initial_agent_versions
    from app.services.builtin_tools.organization_context_write import ORGANIZATION_CONTEXT_WRITE_GROUP

    provider = integration.provider
    name = integration.name or provider
    agent = await _existing_agent(db, admin_id, integration.id)
    created = agent is None
    if agent is None:
        agent_name = f"Organization assets from {name}"
        agent = Agent(
            user_id=admin_id,
            name=agent_name,
            slug=await generate_unique_agent_slug(db, agent_name),
            description=f"Fills the organization profile's assets from {name} (Settings > Organization).",
            icon="Building2",
            persona=_persona(name),
            tags=[fill_agent_tag(integration.id)],
            builtin_tool_ids=[ORGANIZATION_CONTEXT_WRITE_GROUP],
            integration_bindings={provider: [str(integration.id)]},
            integration_read_only={provider: [str(integration.id)]},
            available_channel_types=["web"],
            enabled=True,
        )
        db.add(agent)
        await db.flush()
        await seed_initial_agent_versions(db, agent=agent, author_id=admin_id)

    assignment: Assignment | None = None
    if schedule:
        assignment_name = f"Keep my assets in sync from {name}"
        assignment = (
            await db.execute(
                select(Assignment).where(Assignment.agent_id == agent.id, Assignment.name == assignment_name)
            )
        ).scalar_one_or_none()
        if assignment is None:
            config: dict[str, object] = {"cron": cron}
            if timezone:
                config["timezone"] = timezone
            assignment = Assignment(
                agent_id=agent.id,
                user_id=admin_id,
                is_company_managed=False,
                name=assignment_name,
                description=f"Upserts the devices {name} lists into the organization assets, then removes retired ones.",
                trigger_type="schedule",
                trigger_config=new_trigger_config("schedule", config),
                prompt=fill_prompt(provider, name, apply=True),
                human_in_the_loop=False,
                # Platform-created: no AI label prompt on what it writes.
                ai_label=False,
                enabled=True,
            )
            db.add(assignment)
            await db.flush()
    return FillAgent(
        agent=agent, assignment=assignment, created=created, prompt=fill_prompt(provider, name, apply=False)
    )
