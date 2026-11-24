# SPDX-License-Identifier: Apache-2.0

"""Which fleets read each part of the organization profile (#9173, S2).

The Organization page shows, on each part of the profile, the fleets that
need it ("Used by"). The answer comes from the fleets' own documentation: a
structured prerequisite ``profile:<field>`` (``fleet_docs.PREREQUISITE_CHECKS``)
names the profile field the fleet reads before it works. Nothing is
hand-written here: a fleet whose documentation gains or drops a prerequisite
moves with it.

The seeded agents that read a field are named too (#10063 r1 A3): their
personas say which fields they use, held in data by
``app.agents.builtin_profile_readers`` and checked against the personas by a
test. That covers the PIRs, the risk appetite, the currency and the ticketing
target, which no fleet prerequisite names.

Rows only: no run starts and no model is called, so reading the page costs no
quota. Only the fleets and agents the reader can see are named.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.models.fleet import Fleet
    from app.models.user import User

#: ``profile:<field>`` prerequisite checks, to the profile field they read.
PROFILE_CHECK_FIELDS: dict[str, str] = {
    "profile:sector": "sector",
    "profile:regions": "regions",
    "profile:crown_jewels": "crown_jewels",
    "profile:tech_stack": "tech_stack",
    # The AI security fleet needs AI targets: the assets of category ai_target (#1844).
    "profile:ai_targets": "assets_exposure",
}


def readers_from_fleets(fleets: Iterable[Fleet]) -> dict[str, list[dict[str, str]]]:
    """Profile field -> the fleets whose documentation lists it as a prerequisite, in the order given (no I/O)."""
    readers: dict[str, list[dict[str, str]]] = {}
    for fleet in fleets:
        doc: Any = fleet.documentation
        structured = doc.get("structured") if isinstance(doc, dict) else None
        prerequisites = structured.get("prerequisites") if isinstance(structured, dict) else None
        if not isinstance(prerequisites, list):
            continue
        seen: set[str] = set()
        for item in prerequisites:
            field = PROFILE_CHECK_FIELDS.get(str(item.get("check") or "")) if isinstance(item, dict) else None
            if field is None or field in seen:
                continue
            seen.add(field)
            readers.setdefault(field, []).append({"fleet_id": str(fleet.id), "name": fleet.name})
    return readers


def readers_from_agents(agents: Iterable[Agent]) -> dict[str, list[dict[str, str]]]:
    """Profile field -> the seeded agents whose persona reads it, in the order given (no I/O)."""
    from app.agents.builtin_profile_readers import BUILTIN_AGENT_PROFILE_FIELDS

    readers: dict[str, list[dict[str, str]]] = {}
    for agent in agents:
        if not getattr(agent, "is_builtin", False):
            continue
        for field in BUILTIN_AGENT_PROFILE_FIELDS.get(str(agent.slug or ""), ()):
            readers.setdefault(field, []).append({"agent_id": str(agent.id), "name": agent.name})
    return readers


async def profile_field_readers(
    db: AsyncSession, user: User
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    """The fleets, then the seeded agents, *user* can see that read each profile field.

    Archived fleets are left out; agents are the seeded ones the table names.
    """
    from app.agents.builtin_profile_readers import BUILTIN_AGENT_PROFILE_FIELDS
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.agent import Agent
    from app.models.fleet import Fleet

    group_ids = await get_user_group_ids(db, user.id)
    query = (
        user_visible_query(Fleet, user.id, group_ids)
        .where(Fleet.is_archived.is_(False))
        .order_by(None)
        .order_by(Fleet.name, Fleet.id)
    )
    fleets = list((await db.execute(query)).scalars().all())
    agent_query = (
        user_visible_query(Agent, user.id, group_ids)
        .where(Agent.is_builtin.is_(True))
        .where(Agent.slug.in_(sorted(BUILTIN_AGENT_PROFILE_FIELDS)))
        .order_by(None)
        .order_by(Agent.name, Agent.id)
    )
    agents = list((await db.execute(agent_query)).scalars().all())
    return readers_from_fleets(fleets), readers_from_agents(agents)
