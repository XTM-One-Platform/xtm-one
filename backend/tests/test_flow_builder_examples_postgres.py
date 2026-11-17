"""``get_flow_examples`` lists the seeded example flows, against PostgreSQL.

``Flow.tags`` is a ``json`` column, and PostgreSQL has no equality operator for
``json``: the lookup compared it to ``["example"]`` and failed with ``operator
does not exist: json = json`` on every call, so no agent composing a flow got
its few-shot examples. Only a real database shows the filter runs; the unit
fakes never reach the operator.

The cast, seeded fresh: the real educational pack (``seed_example_flow``), and
next to it

* a company-managed flow tagged ``example`` among other tags - listed, since
  the rule is that the tags contain ``example``;
* a private flow tagged ``example`` - not listed, examples are company-managed;
* company-managed flows tagged otherwise, or not at all - not listed;
* a company-managed flow tagged ``example`` whose stored tags ``jsonb``
  refuses (a lone surrogate escape written around the ORM) - the lookup still
  answers, and lists it on its decoded tags, as ``@>`` decides the others.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core.json_sql import json_unparseable
from app.models.flow import Flow
from app.services.builtin_tools.flow_builder import build_flow_builder_tool
from app.services.flow_examples import EXAMPLE_FLOW_NAME, seed_example_flow
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_EXTRA_TAGGED = "Zz extra-tagged example"
_EXTRA_GRAPH = {"nodes": [], "edges": [], "viewport": {"x": 1, "y": 2, "zoom": 1}}
_PRIVATE = "Zz private example"
_OTHER_TAG = "Zz demo flow"
_UNTAGGED = "Zz untagged flow"
_UNREADABLE = "Zz unreadable example tags"


async def _seed(session) -> list[str]:
    """The pack and the decoys; returns the pack's names."""
    assert await seed_example_flow(session) > 0
    await session.commit()
    pack = list((await session.execute(select(Flow.name))).scalars().all())
    session.add_all(
        [
            Flow(name=_EXTRA_TAGGED, graph=_EXTRA_GRAPH, tags=["beta", "example"], is_company_managed=True),
            Flow(name=_PRIVATE, graph={}, tags=["example"], is_company_managed=False),
            Flow(name=_OTHER_TAG, graph={}, tags=["demo"], is_company_managed=True),
            Flow(name=_UNTAGGED, graph={}, tags=None, is_company_managed=True),
            Flow(name=_UNREADABLE, graph={}, tags=[], is_company_managed=True),
        ]
    )
    await session.commit()
    # Around the ORM, whose serializer would repair the escape.
    await session.execute(
        text("UPDATE flows SET tags = CAST(:raw AS json) WHERE name = :name"),
        {"raw": '["example", "\\ud800"]', "name": _UNREADABLE},
    )
    await session.commit()
    unreadable = (
        await session.execute(select(json_unparseable(Flow.tags)).where(Flow.name == _UNREADABLE))
    ).scalar_one()
    assert unreadable is True, "the decoy must hold tags a ``::jsonb`` cast refuses"
    return pack


@asynccontextmanager
async def _session() -> AsyncIterator:
    """One session on a scratch database holding every table."""
    async with (
        scratch_database("flowexamples", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as session,
    ):
        yield session


async def _examples(session, **arguments) -> str:
    _defs, executor = build_flow_builder_tool(session, str(uuid.uuid4()))
    result = await executor.execute_tool(ToolCall(id="1", name="get_flow_examples", arguments=arguments))
    return result.content


@pytest.mark.asyncio
async def test_get_flow_examples_lists_and_fetches_the_seeded_examples_on_postgres():
    async with _session() as session:
        pack = await _seed(session)
        assert EXAMPLE_FLOW_NAME in pack

        # Without ``name``: the listing, exactly the pack plus the two other
        # examples, in the database's name order.
        examples = {*pack, _EXTRA_TAGGED, _UNREADABLE}
        ordered = [
            n
            for n in (await session.execute(select(Flow.name).order_by(Flow.name))).scalars().all()
            if n in examples
        ]
        assert len(ordered) == len(pack) + 2
        listing = await _examples(session)
        header, *lines = listing.splitlines()
        assert header == "Seeded examples (pass `name` to fetch one graph as a template):"
        assert lines == [f"- {n}" for n in ordered]

        # With ``name``: that example's graph, as stored.
        fetched = json.loads(await _examples(session, name="EXTRA-tagged"))
        assert fetched == {"name": _EXTRA_TAGGED, "graph": _EXTRA_GRAPH}

        stored_graph = (
            await session.execute(select(Flow.graph).where(Flow.name == EXAMPLE_FLOW_NAME))
        ).scalar_one()
        fetched = json.loads(await _examples(session, name=EXAMPLE_FLOW_NAME))
        assert fetched == {"name": EXAMPLE_FLOW_NAME, "graph": stored_graph}

        # A name only a non-example carries: no match, and the examples named.
        missing = await _examples(session, name="private example")
        header, *lines = missing.splitlines()
        assert header == "No example matches. Available:"
        assert lines == [f"- {n}" for n in ordered]


@pytest.mark.asyncio
async def test_get_flow_examples_says_none_are_seeded_on_postgres():
    async with _session() as session:
        session.add(Flow(name=_PRIVATE, graph={}, tags=["example"], is_company_managed=False))
        await session.commit()
        assert await _examples(session) == "No example flows are seeded on this platform."
        assert await _examples(session, name="private") == "No example flows are seeded on this platform."
