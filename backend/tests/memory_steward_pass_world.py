"""The Memory Steward pass world (#8258): ``memory_steward_world`` plus stubs.

Stubbed: the lower Concierge levels' consent, budget, audit and dry-run
(levels 1 and 2 test their own), the model's reading of a lane, the Redis
medium-term summary and the embedding. Who is the Concierge stays level 1's
real rule.
"""

from __future__ import annotations

import contextlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.services import persistent_memory as pm
from app.services.health import identity
from app.services.health import memory_steward as steward
from app.services.health import memory_steward_deps as deps
from app.services.health.memory_history import Lane
from app.services.health.memory_steward_plan import Verdict
from tests.memory_steward_world import World, entry, make_world, world_database

_DIMENSIONS = 8


@dataclass
class PassWorld(World):
    readable: set = field(default_factory=set)
    dry_run: bool = False
    verdicts: dict = field(default_factory=dict)
    summaries: dict = field(default_factory=dict)
    model_calls: list = field(default_factory=list)
    audits: list = field(default_factory=list)
    reads: list = field(default_factory=list)
    intercepted: list = field(default_factory=list)
    busy: set = field(default_factory=set)

    def lane(self, user: uuid.UUID | None) -> Lane:
        return Lane(self.agent, user)


def _stub(monkeypatch, world: PassWorld) -> None:
    async def readable(_db, agent_id, user_id):
        return (agent_id, user_id) in world.readable

    async def audit(_db, **kwargs):
        world.audits.append(kwargs)

    async def audit_read(_db, **kwargs):
        world.reads.append(kwargs)

    async def intercepted(_db, **kwargs):
        world.intercepted.append(kwargs)

    async def ask(_db, lane, candidates, **_kwargs):
        world.model_calls.append(lane)
        verdict = world.verdicts.get(lane.user_id)
        return verdict(candidates) if callable(verdict) else (verdict or Verdict())

    async def medium(lane):
        return world.summaries.get(lane.user_id, "")

    async def summary_lanes(_db, *, agent_id):
        return {Lane(world.agent, user) for user in world.summaries}

    async def embed(_db, content, **_kwargs):
        return [1.0] + [0.0] * (_DIMENSIONS - 1), "test-embedding"

    async def no_budget_check(_db, _cycle_id=None):
        return None

    async def dry_run(_db):
        return world.dry_run

    async def scope(_db):
        return frozenset(agent for agent, _user in world.readable), frozenset()

    @contextlib.asynccontextmanager
    async def lease(lane):
        # Redis connections belong to the event loop that opened them; the
        # lease is a set here, held while the pass runs.
        held = lane.key not in world.busy
        world.busy.add(lane.key)
        try:
            yield held
        finally:
            if held:
                world.busy.discard(lane.key)

    monkeypatch.setattr(identity, "health_enabled", lambda: True)
    monkeypatch.setattr(deps, "audit_lane_read", audit_read)
    monkeypatch.setattr(deps, "memory_lane_readable", readable)
    monkeypatch.setattr(deps, "ensure_budget", no_budget_check)
    monkeypatch.setattr(deps, "audit", audit)
    monkeypatch.setattr(deps, "dry_run_active", dry_run)
    monkeypatch.setattr(deps, "record_intercepted_write", intercepted)
    monkeypatch.setattr(deps, "readable_scope", scope)
    monkeypatch.setattr(steward, "_lane_lease", lease)
    monkeypatch.setattr(steward, "_ask_model", ask)
    monkeypatch.setattr(steward, "_medium_term", medium)
    monkeypatch.setattr(steward, "_lanes_with_summaries", summary_lanes)
    monkeypatch.setattr(pm, "embed_text_with_model", embed)


@asynccontextmanager
async def pass_world(monkeypatch):
    async with world_database() as (db, session):
        world = await make_world(session, db.sessions, PassWorld)
        world.readable = {(world.agent, None), (world.agent, world.owner), (world.agent, world.other)}
        _stub(monkeypatch, world)
        yield world


async def run_pass(world: PassWorld, user: uuid.UUID | None):
    async with world.sessions() as session:
        result = await steward.consolidate_lane(session, world.lane(user))
        await session.commit()
        return result


async def seed_owner_lane(world: PassWorld) -> dict[str, uuid.UUID]:
    """A user lane with a duplicate, a contradiction and a stale entry, beside
    an identical duplicate in the agent lane and in another user's lane; the
    model names the contradiction and the time-bound entry."""
    ids = {
        "dup_old": await entry(world, "Prefers answers in French", user=world.owner, days_old=9),
        "dup_new": await entry(world, "prefers answers in french", user=world.owner, days_old=1),
        "edr_old": await entry(world, "Their EDR is Defender", user=world.owner, days_old=20),
        "edr_new": await entry(world, "Their EDR is CrowdStrike", user=world.owner, days_old=2),
        "trip": await entry(world, "Travels to Tokyo next Monday", user=world.owner, days_old=12),
        "agent_dup": await entry(world, "Prefers answers in French", user=None, days_old=3),
        "other_dup": await entry(world, "Prefers answers in French", user=world.other, days_old=3),
    }

    def owner_verdict(candidates):
        # Names only the entries still weighed: a later pass no longer sees
        # what an earlier one changed.
        position = {c.id: n for n, c in enumerate(candidates)}
        pair = (position.get(ids["edr_old"]), position.get(ids["edr_new"]))
        return Verdict(
            contradictions=(pair,) if None not in pair else (),
            time_bound=(position[ids["trip"]],) if ids["trip"] in position else (),
        )

    world.verdicts[world.owner] = owner_verdict
    return ids
