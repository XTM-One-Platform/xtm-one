"""An imported agent on the routing model keeps no model of its own.

A bundle carrying ``llm_model_source: "routing"`` with a model and provider
used to store both: harmless while the source holds, then a stale pin the day
the source is cleared. Through both import routes, against a real PostgreSQL
(same scratch-database recipe as ``test_agent_import_null_fields_postgres``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from tests.test_agent_import_null_fields_postgres import _ROUTES, _user
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


def _bundle(**agent_fields) -> dict:
    return {
        "version": 3,
        "export_type": "agent",
        "agent": {"name": "Imported judge", "persona": "You rule.", **agent_fields},
    }


async def _imported_model(route: str, bundle: dict) -> tuple:
    async with _temp_database("imp_routing") as sessionmaker:
        user = await _user(sessionmaker)

        await _ROUTES[route](sessionmaker, user, bundle)

        async with sessionmaker() as db:
            agent = (await db.execute(select(Agent))).scalar_one()
        return agent.llm_model_source, agent.llm_provider, agent.llm_provider_id, agent.llm_model


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_a_routing_bundle_imports_without_the_model_it_carries(route):
    stored = await _imported_model(
        route,
        _bundle(llm_model_source="routing", llm_provider="openai", llm_model="gpt-5"),
    )

    assert stored == ("routing", None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_an_unknown_source_keeps_the_model_it_carries(route):
    """An unknown source reads as none: the agent's own model stays."""
    stored = await _imported_model(
        route,
        _bundle(llm_model_source="ROUTING", llm_provider="openai", llm_model="gpt-5"),
    )

    assert stored == (None, "openai", None, "gpt-5")
