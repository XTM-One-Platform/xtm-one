"""A scratch PostgreSQL world for the Memory Steward tests (#8258).

Three users (an agent's owner, another user, an administrator), one agent,
and helpers to store memory entries and record consolidations the way a pass
does: the changed entries flagged superseded, one ``MemoryConsolidation`` row.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.memory_consolidation import MemoryConsolidation
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database


@dataclass
class World:
    sessions: object
    agent: uuid.UUID
    owner: uuid.UUID
    other: uuid.UUID
    admin: uuid.UUID


async def make_world(session, sessions, world_type=World, **extra) -> World:
    users = {
        name: User(
            email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
            full_name=name,
            hashed_password="x",
            role="admin" if name == "admin" else "user",
        )
        for name in ("owner", "other", "admin")
    }
    session.add_all(users.values())
    await session.flush()
    agent = Agent(
        name="Analyst",
        slug=f"analyst-{uuid.uuid4().hex[:8]}",
        persona="You help analysts.",
        enabled=True,
        user_id=users["owner"].id,
        is_builtin=False,
        memory_persistent_enabled=True,
    )
    session.add(agent)
    await session.commit()
    return world_type(
        sessions=sessions,
        agent=agent.id,
        owner=users["owner"].id,
        other=users["other"].id,
        admin=users["admin"].id,
        **extra,
    )


@asynccontextmanager
async def world_database():
    async with (
        scratch_database("memsteward", tables=ALL_TABLES, extensions=("vector",), refuse_other_databases=True) as db,
        db.sessions() as session,
    ):
        yield db, session


async def entry(world: World, content: str, *, user: uuid.UUID | None, days_old: float, memory_type="fact") -> uuid.UUID:
    async with world.sessions() as session:
        row = AgentMemoryEntry(
            agent_id=world.agent,
            user_id=user,
            content=content,
            memory_type=memory_type,
            source="chat",
            created_at=datetime.now(UTC) - timedelta(days=days_old),
        )
        session.add(row)
        await session.commit()
        return row.id


async def applied(
    world: World,
    operation: str,
    *,
    user: uuid.UUID | None,
    before: tuple[uuid.UUID, ...] = (),
    after: uuid.UUID | None = None,
    days_ago: float = 0,
    fingerprint: str | None = None,
) -> uuid.UUID:
    """Record *operation* as a pass applies it: *before* flagged superseded by
    *after* (a flag changes nothing), and its row."""
    async with world.sessions() as session:
        if operation in ("merge", "remove", "demote"):
            await session.execute(
                update(AgentMemoryEntry)
                .where(AgentMemoryEntry.id.in_(before))
                .values(superseded_at=datetime.now(UTC), superseded_by_id=after)
            )
        row = MemoryConsolidation(
            agent_id=world.agent,
            user_id=user,
            operation=operation,
            reason={"merge": "duplicate", "demote": "stale", "promote": "recurring"}.get(operation, "contradiction"),
            before_entry_ids=[str(entry_id) for entry_id in before],
            after_entry_id=after,
            fingerprint=fingerprint or uuid.uuid4().hex,
            applied_at=datetime.now(UTC) - timedelta(days=days_ago),
        )
        session.add(row)
        await session.commit()
        return row.id


async def state(world: World) -> dict[uuid.UUID, tuple[bool, uuid.UUID | None]]:
    """Every entry: whether it is superseded, and by what."""
    async with world.sessions() as session:
        rows = (await session.execute(select(AgentMemoryEntry))).scalars().all()
        return {row.id: (row.superseded_at is not None, row.superseded_by_id) for row in rows}


async def count(world: World, model) -> int:
    async with world.sessions() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def user(world: World, user_id: uuid.UUID) -> User:
    async with world.sessions() as session:
        return await session.get(User, user_id)
