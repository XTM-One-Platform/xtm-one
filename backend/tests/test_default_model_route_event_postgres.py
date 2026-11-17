"""``platform.default_model_route.changed`` against PostgreSQL.

One event per changed route, after the outer commit, whatever wrote it, with
the cause the writer named; none on a save that changes nothing, none on a
rollback. The catalogue lists it as a platform-wide event with its filter keys.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core.database import Base
from app.models.llm_config import LLMProviderConfig
from app.models.platform_settings import PlatformSettings
from tests.scratch_database import scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

EVENT = "platform.default_model_route.changed"
_TABLES = [table for table in Base.metadata.sorted_tables if table.name != "embeddings"]


async def _drain():
    from app.services import default_model_routes

    for _ in range(5):
        await asyncio.sleep(0)
    if default_model_routes._publish_tasks:
        await asyncio.gather(*default_model_routes._publish_tasks)


@pytest.fixture
def published(monkeypatch):
    from app.services import default_model_routes, platform_events

    events: list[tuple[str, dict]] = []

    async def publish(name, data, *, event_id=None):
        events.append((name, dict(data)))
        return event_id

    monkeypatch.setattr(platform_events, "publish", publish)
    default_model_routes.register_default_route_listeners()
    return events


async def _seed(db):
    a, b = uuid.uuid4(), uuid.uuid4()
    async with db.sessions() as session:
        session.add_all(
            [
                LLMProviderConfig(id=a, provider="anthropic", available_models=["m1"], api_key_enc="x"),
                LLMProviderConfig(id=b, provider="bedrock", available_models=["m1"], api_key_enc="x"),
            ]
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO platform_settings (id, created_at, updated_at, agent_default_provider, "
                "agent_default_provider_id, agent_default_model, routing_provider_id, routing_model) "
                "VALUES (:id, now(), now(), 'anthropic', :a, 'm1', :a, 'm1')"
            ),
            {"id": str(uuid.uuid4()), "a": a},
        )
        await session.commit()
    return a, b


async def _settings(session):
    return (await session.execute(select(PlatformSettings))).scalar_one()


async def test_one_event_per_changed_route_with_cause_and_actor(published):
    from app.services.default_model_routes import mark_cause

    async with scratch_database("routes", tables=_TABLES, extensions=("vector",)) as db:
        a, b = await _seed(db)
        published.clear()
        async with db.sessions() as session:
            ps = await _settings(session)
            mark_cause(session, "agent_tool", "user-1")
            ps.agent_default_provider, ps.agent_default_provider_id = "bedrock", b
            ps.routing_provider_id = b
            await session.commit()
        await _drain()
    events = sorted((d["route"], d) for name, d in published if name == EVENT)
    assert [route for route, _ in events] == ["agent_default", "routing"]
    agent = events[0][1]
    assert agent["before"] == {"provider_id": str(a), "provider": "anthropic", "model": "m1"}
    assert agent["after"] == {"provider_id": str(b), "provider": "bedrock", "model": "m1"}
    assert agent["cause"] == "agent_tool" and agent["actor_user_id"] == "user-1"
    # The routing route has no key column: the key is read from the provider config.
    assert events[1][1]["after"]["provider"] == "bedrock"


@pytest.mark.parametrize("cause", ["settings_page", "provider_deleted", "env_provider_prune", "system"])
async def test_every_cause_is_carried(published, cause):
    from app.services.default_model_routes import mark_cause

    async with scratch_database("routes", tables=_TABLES, extensions=("vector",)) as db:
        _a, b = await _seed(db)
        published.clear()
        async with db.sessions() as session:
            ps = await _settings(session)
            if cause != "system":
                mark_cause(session, cause)
            ps.routing_provider_id = b
            await session.commit()
        await _drain()
    assert [(n, d["cause"]) for n, d in published] == [(EVENT, cause)]


async def test_a_save_that_changes_nothing_or_rolls_back_raises_nothing(published):
    async with scratch_database("routes", tables=_TABLES, extensions=("vector",)) as db:
        a, b = await _seed(db)
        published.clear()
        async with db.sessions() as session:
            ps = await _settings(session)
            ps.routing_provider_id = a  # the same value
            ps.agent_default_model = "m1"
            await session.commit()
        async with db.sessions() as session:
            ps = await _settings(session)
            ps.routing_provider_id = b
            await session.flush()
            await session.rollback()
        async with db.sessions() as session:
            ps = await _settings(session)
            ps.routing_provider_id = b  # moved, then back, in one transaction
            await session.flush()
            ps.routing_provider_id = a
            await session.commit()
        await _drain()
    assert published == []


def test_the_catalogue_lists_it_platform_wide_with_its_filter_keys():
    from app.services import platform_events

    entry = platform_events.PLATFORM_EVENTS[EVENT]
    assert {"route", "cause", "after.provider", "before.provider_id"} <= set(entry["filter_keys"])
    assert EVENT in platform_events.PLATFORM_WIDE_EVENTS
