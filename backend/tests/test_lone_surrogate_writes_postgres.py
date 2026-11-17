"""A lone surrogate is refused because PostgreSQL cannot read it back.

The unit half is ``test_lone_surrogate_writes.py``. Here, against a real
PostgreSQL: what the refusal finds is exactly what a ``json`` column stores and
a ``::jsonb`` read then refuses (a surrogate pair is read back as the character
it encodes), and a valid value created through the REST route is stored as sent
and stays readable. The values are written as stored text, never through the
ORM, so what the engine does with one on its way in does not change
what the parity test reads. Same scratch-database recipe as
``test_agent_duplicate_bindings_postgres.py``.
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.assignment import CreateAssignmentRequest
from app.core.lone_surrogates import find_lone_surrogate
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

EMOJI = "\U0001f600"


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db:
        yield db.sessions


#: Hand-picked shapes: every way a surrogate can sit next to another character.
_SHAPES = [
    "\ud800",
    "\udc00",
    "\udbff\udfff",
    "\ud83d\ude00",
    "\ude00\ud83d",
    "\ud83d\ud83d\ude00",
    "\ud83d\ude00\ude00",
    "x\ud800y",
    EMOJI,
    "caf\u00e9",
    "\\ud800",
    "\\\ud800",
    "plain",
    "",
]


def _generated(count: int) -> list[str]:
    alphabet = ["a", "\u00e9", EMOJI, "\ud83d", "\ude00", "\ud800", "\udfff", "\\", "u", "d", "8", '"']
    rng = random.Random(4726)  # noqa: S311 - a seeded, reproducible corpus
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6))) for _ in range(count)]


async def test_the_refusal_finds_exactly_what_postgres_cannot_read_back():
    """``json.dumps`` writes a surrogate as the escape a ``json`` column keeps
    verbatim; ``::jsonb`` then refuses a lone one and decodes a pair."""
    cases = _SHAPES + _generated(400)
    async with _temp_database("lone_surrogate_parity") as sessions, sessions() as session:
        for value in cases:
            stored = json.dumps({"v": value, "k": {value: [value]}})
            async with session.begin_nested():
                assert await session.scalar(text("SELECT CAST(:t AS json) IS NOT NULL"), {"t": stored})
            try:
                async with session.begin_nested():
                    read = await session.scalar(text("SELECT CAST(CAST(:t AS json) AS jsonb) ->> 'v'"), {"t": stored})
            except DBAPIError:
                readable = False
            else:
                readable = True
                # A pair is read back as the one character it encodes, as json.loads does.
                assert read == json.loads(stored)["v"]
            assert readable is (find_lone_surrogate(value) is None), ascii(value)


async def _seed_agent(sessions) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessions() as session:
        user = User(
            email=f"surrogate-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Surrogate Test",
            hashed_password="x",
        )
        session.add(user)
        await session.flush()
        agent = Agent(name="Hooks", user_id=user.id)
        session.add(agent)
        await session.commit()
        return user.id, agent.id


async def test_a_valid_value_is_created_as_sent_and_stays_readable():
    async with _temp_database("lone_surrogate_create") as sessions:
        user_id, agent_id = await _seed_agent(sessions)
        body = CreateAssignmentRequest.model_validate_json(
            json.dumps(
                {
                    "agent_id": str(agent_id),
                    "name": f"Hook {EMOJI}",
                    "trigger_type": "manual",
                    "prompt": "Caf\u00e9 \\ud800",
                    "trigger_config": {"note": EMOJI, "pair": "\ud83d\ude00", "text": "\\ud800"},
                }
            )
        )
        request = Request({"type": "http", "method": "POST", "path": "/x", "headers": []})
        async with sessions() as db:
            user = await db.get(User, user_id)
            response = await assignments_api.create_assignment(body, request, user, db, None)

        async with sessions() as db:
            read = await db.scalar(
                text("SELECT CAST(trigger_config AS jsonb) ->> 'note' FROM assignments WHERE id = :id"),
                {"id": uuid.UUID(response.id)},
            )
            stored = await db.get(Assignment, uuid.UUID(response.id))
        assert read == EMOJI
        assert stored.name == f"Hook {EMOJI}"
        assert stored.prompt == "Caf\u00e9 \\ud800"
        assert {k: stored.trigger_config[k] for k in ("note", "pair", "text")} == {
            "note": EMOJI,
            "pair": EMOJI,
            "text": "\\ud800",
        }
        json.dumps(response.model_dump(), ensure_ascii=False).encode("utf-8")
