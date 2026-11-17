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

"""A work item's fleet impact: the agents its completion concerns.

An item may declare, as data, which agents of the fleet its outcome changes
something for (``fleet_impact``): ``{"targets": [agent ids], "kind": one of
:data:`FLEET_IMPACT_KINDS`, "instructions": text}``. The platform then routes
its ``work_item.completed`` event to those agents too: each target joins the
event's ``participant_agent_ids`` (an assignment's ``own`` scope is satisfied
by "impacted"), and the payload carries ``fleet_impact`` and the item's
``result``, so a listener filters on ``fleet_impact.targets`` (contains) and
``fleet_impact.kind``. The completion writes one claim note naming the agents
it was routed to.

Nothing else changes: the creator, owner and holder keep receiving the event
as before, and an item without targets starts nothing extra.

Validation (:func:`check_targets`): every target is an agent in the home fleet
of the item's owner or creator, refused at write with the reason otherwise.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

#: What a completion changes for its targets.
FLEET_IMPACT_KINDS: tuple[str, ...] = (
    "skill",
    "persona",
    "assignment",
    "runbook",
    "retest",
    "state",
    "capability",
)
#: Bound of the instructions text stored and carried in an event payload.
MAX_INSTRUCTIONS_CHARS = 4000
#: Bound of the number of agents one item may impact.
MAX_TARGETS = 20

# TEMPORARY SHIM, remove after 2026-12-04: an item written before the
# field existed declares its follow-through in a description section with this
# heading; until the agents set ``fleet_impact`` themselves, such an item is
# routed to its creator as a ``capability`` impact. Delete the constant, the
# branch in :func:`effective_fleet_impact` and its test together.
LEGACY_FOLLOW_THROUGH_HEADING = "FOLLOW-THROUGH (EA)"


def normalize_fleet_impact(raw: Any) -> dict[str, Any] | None:
    """*raw* as the stored shape, ``None`` for no impact; ``ValueError`` with the reason when malformed.

    An object with no targets (``{}``, ``{"targets": []}``) is no impact: that
    is how an update clears the field.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("fleet_impact must be an object: {targets: [agent ids], kind, instructions}")
    unknown = sorted(set(raw) - {"targets", "kind", "instructions"})
    if unknown:
        raise ValueError(f"fleet_impact takes targets, kind and instructions; unknown key(s): {', '.join(unknown)}")
    targets_raw = raw.get("targets") or []
    if isinstance(targets_raw, str):
        targets_raw = [targets_raw]
    if not isinstance(targets_raw, list):
        raise ValueError("fleet_impact.targets must be a list of agent ids")
    targets: list[str] = []
    for value in targets_raw:
        try:
            agent = str(uuid.UUID(str(value).strip()))
        except (TypeError, ValueError):
            raise ValueError(f"fleet_impact.targets: '{value}' is not an agent id (UUID)") from None
        if agent not in targets:
            targets.append(agent)
    if not targets:
        return None
    if len(targets) > MAX_TARGETS:
        raise ValueError(f"fleet_impact.targets names at most {MAX_TARGETS} agents ({len(targets)} given)")
    kind = raw.get("kind")
    if not kind:
        raise ValueError(f"fleet_impact.kind is required when targets are set: one of {', '.join(FLEET_IMPACT_KINDS)}")
    if kind not in FLEET_IMPACT_KINDS:
        raise ValueError(f"fleet_impact.kind '{kind}' is not one of {', '.join(FLEET_IMPACT_KINDS)}")
    instructions = raw.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise ValueError("fleet_impact.instructions must be a text")
    return {
        "targets": targets,
        "kind": kind,
        "instructions": (instructions or "").strip()[:MAX_INSTRUCTIONS_CHARS] or None,
    }


async def check_targets(
    db: AsyncSession,
    impact: Mapping[str, Any] | None,
    *,
    owner_agent_id: uuid.UUID | None,
    created_by_agent_id: uuid.UUID | None,
) -> None:
    """Refuse a target that is not in the home fleet of the item's owner or creator (``ValueError``)."""
    if not impact or not impact.get("targets"):
        return
    from app.services.fleets import home_fleets_of

    targets = [uuid.UUID(t) for t in impact["targets"]]
    anchors = [a for a in (owner_agent_id, created_by_agent_id) if a is not None]
    homes = await home_fleets_of(db, [*anchors, *targets])
    fleets = {homes[a].fleet_id for a in anchors if a in homes}
    if not fleets:
        raise ValueError(
            "fleet_impact needs an owner or a creator agent in a fleet: the targets are checked against that fleet"
        )
    outside = [str(t) for t in targets if t not in homes or homes[t].fleet_id not in fleets]
    if outside:
        raise ValueError(
            "fleet_impact.targets names agent(s) outside the fleet of the item's owner or creator: "
            + ", ".join(outside)
        )


def _field(item: Any, name: str) -> Any:
    """*name* of *item*, a ``WorkItem`` row or its serialized dict."""
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def effective_fleet_impact(item: Any) -> dict[str, Any] | None:
    """The impact the platform routes for *item* (a row or its serialized dict): its own field, else the temporary shim's."""
    stored = _field(item, "fleet_impact")
    if isinstance(stored, Mapping) and stored.get("targets"):
        return dict(stored)
    creator = _field(item, "created_by_agent_id")
    description = _field(item, "description") or ""
    if creator is not None and LEGACY_FOLLOW_THROUGH_HEADING in description:
        return {"targets": [str(creator)], "kind": "capability", "instructions": None, "inferred": True}
    return None


def impacted_agent_ids(item: Any) -> list[str]:
    """The agents *item*'s completion is routed to beyond its creator, owner and holder."""
    impact = effective_fleet_impact(item)
    return list(impact["targets"]) if impact else []


def routed_note(names: Iterable[str], at: str) -> str:
    """The claim note a routed completion leaves on the item."""
    return f"impact routed to {', '.join(names)} at {at}"


async def note_impact_routed(db: AsyncSession, item: Any, *, previous_status: str | None, now: datetime) -> None:
    """Add the dated claim note of a completion routed to *item*'s impacted agents, on the caller's session.

    Called by the writers that complete an item, before their commit, with the
    status the item had: only the transition into done or cancelled writes it,
    and only when the item impacts someone. The agents are named (their id when
    the row is gone).
    """
    if item.status not in ("done", "cancelled") or previous_status in ("done", "cancelled"):
        return
    targets = impacted_agent_ids(item)
    if not targets:
        return
    from sqlalchemy import select

    from app.models.agent import Agent

    ids = [uuid.UUID(t) for t in targets]
    rows = (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(ids)))).all()
    names = {str(agent_id): name for agent_id, name in rows}
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    item.claim_notes = [
        *(item.claim_notes or []),
        {"at": now.isoformat(), "text": routed_note((names.get(t) or t for t in targets), stamp)},
    ]
