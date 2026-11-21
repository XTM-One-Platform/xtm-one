"""An owner agent for the work items a PostgreSQL test parks blocked (#762).

A blocked work item is black or white: it has the agent that resumes it. The
scratch-database tests that create blocked items without caring who owns them
give them the agent :func:`add_owner` creates next to the test's user.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.models.agent import Agent

_OWNERS: dict[str, str] = {}


async def add_owner(sessions: Any, user_id: uuid.UUID | str) -> str:
    """Create an agent of *user_id* in the database *sessions* opens; its id."""
    agent = Agent(id=uuid.uuid4(), name="owner", user_id=uuid.UUID(str(user_id)))
    async with sessions() as db:
        db.add(agent)
        await db.commit()
    _OWNERS[str(user_id)] = str(agent.id)
    return str(agent.id)


def owner_of(user_id: uuid.UUID | str) -> str | None:
    """The agent :func:`add_owner` created for *user_id* (each test's user is a fresh id)."""
    return _OWNERS.get(str(user_id))
