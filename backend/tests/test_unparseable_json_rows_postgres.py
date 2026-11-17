"""One ``json`` row PostgreSQL cannot parse never takes a shared query down, on real PostgreSQL.

A ``json`` column keeps a lone UTF-16 surrogate or ``\\u0000`` escape verbatim, and a ``::jsonb``
cast of that row aborted the whole statement: one agent's ``integration_bindings`` holding
``"\\ud800"`` made the provider webhook route fail for every provider. Each test writes such a
row as stored text (``_store``: how a row written before the write-side guard, or around the ORM,
holds it) and checks that the query answers, that it answers on well-formed rows exactly what the
unguarded cast answered - valid surrogate pairs included - and what it does with the bad row.

``integration``-marked and skips when PostgreSQL is unreachable, like its neighbours; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import random
import uuid
from pathlib import Path

import pytest
from sqlalchemy import JSON, Text, cast, column, func, select, table, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.agents.seed import seed_builtin_integrations
from app.api.v1 import helpers, intents, public_catalog, webhooks
from app.core.config import settings
from app.core.database import json_deserializer, json_serializer
from app.core.json_sql import json_unparseable
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.integration import Integration
from app.models.presentation import Presentation
from app.models.prompt import Prompt
from app.models.user import User
from app.models.variable import Variable
from app.services import integration_notification_auth
from app.services.builtin_tools.agent_builder import _searchable_trigger_payload
from app.services.variable_resolver import load_variables
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

GITHUB_ISSUES = {"provider": "github", "event": "issues"}
#: A lone high surrogate escape, and around it text the log must never repeat.
UNREADABLE_NOTE = r'"top-secret \ud800"'
VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _scratch(prefix: str):
    return scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    )


def _user(name: str) -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=name,
        role="user",
        is_active=True,
    )


def _agent(owner: User, bindings: object = None, *, enabled: bool = True, shared_group_ids=None) -> Agent:
    return Agent(
        name=f"Agent {uuid.uuid4().hex[:8]}",
        slug=f"a-{uuid.uuid4().hex[:8]}",
        user_id=owner.id,
        enabled=enabled,
        integration_bindings=bindings,
        shared_group_ids=shared_group_ids,
    )


def _assignment(agent: Agent, config: dict) -> Assignment:
    return Assignment(
        agent_id=agent.id,
        user_id=agent.user_id,
        name="On issues",
        trigger_type="integration_event",
        trigger_config=config,
        prompt="Handle the event.",
        enabled=True,
    )


async def _store(db, model, row_id, column_name: str, stored: str) -> None:
    """Write *stored* as the column's JSON text, as a row written around the sanitizing serializer holds it."""
    await db.execute(
        text(f"UPDATE {model.__tablename__} SET {column_name} = CAST(:stored AS json) WHERE id = :id"),  # noqa: S608 - the test's own table and column names
        {"stored": stored, "id": row_id},
    )


async def _stored_text(db, model, row_id, column_name: str) -> str | None:
    return (
        await db.execute(
            text(f"SELECT CAST({column_name} AS text) FROM {model.__tablename__} WHERE id = :id"),  # noqa: S608 - the test's own table and column names
            {"id": row_id},
        )
    ).scalar()


def _load_revision(stem: str):
    spec = importlib.util.spec_from_file_location(stem, VERSIONS / f"{stem}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)
    webhooks._invalidate_provider_routing_cache()
    integration_notification_auth._log_state.clear()
    yield
    webhooks._invalidate_provider_routing_cache()
    integration_notification_auth._log_state.clear()


# -- the test itself ---------------------------------------------------------

#: Every shape of escape the test sets aside or keeps, one JSON string each.
_ESCAPE_SHAPES = [
    r'"\ud800"',
    r'"\udc00"',
    r'"\uDBFF"',
    r'"\u0000"',
    r'"\ud83d\ude00"',
    r'"\uD83D\uDE00"',
    r'"\udbff\udfff"',
    r'"\\ud800"',
    r'"\\\ud800"',
    r'"\\\\ud800"',
    r'"\ud83d\\\udc00"',
    r'"\ud83d\\ude00"',
    r'"\ud800\ud800\udc00"',
    r'"\ud800\udc00\udc00"',
    r'"\ud800\n\udc00"',
    r'"\\u0000"',
    r'"\\\u0000"',
    r'"a\u00e9b"',
    r'"\ud7ff\ue000"',
    r'"\"\ud800"',
    r'"\/\udc00"',
    r'{"\ud800": 1}',
    r'{"k": ["\ud83d\ude00", "\udbff\udfff"]}',
    r'["\ud83d\ude00\ud83d"]',
    r'"x\\"',
    # Numbers: jsonb refuses those past numeric's range; a hex string is no number.
    "1e1000000",
    "[1e131072]",
    '{"a": 1e-16384}',
    "[-1E+200000]",
    "[0." + "0" * 16383 + "1]",
    "1e308",
    "[-2.5E-300]",
    '{"sha": "3e12345abc", "n": "1e99999"}',
]

#: Numbers the test reads as unreadable although they cast: the superset's
#: price, on values no write of this platform makes.
_CASTABLE_BUT_FLAGGED = frozenset({"1e10000", "[1e131071]", "[" + "7" * 2000 + "]", "[0e1000000]"})

#: Fragments of a JSON string, each valid on its own, so any sequence of them is valid JSON.
_FRAGMENTS = [
    r"\\", r"\ud83d", r"\ude00", r"\ud800", r"\udbff", r"\udc00", r"\udfff", r"\uD83D", r"\uDE00",
    r"\u0000", r"\u00e9", r"\u0041", r"\n", r"\"", "a", "u", "d", "8", "0",
]


def _generated_shapes() -> list[str]:
    rng = random.Random(4783)  # noqa: S311 - a reproducible corpus, not a secret
    shapes = []
    for _ in range(600):
        body = "".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 7)))
        shapes.append(f'"{body}"' if rng.random() < 0.7 else f'{{"{body}": ["{body}"]}}')
    return shapes


async def test_the_sql_test_refuses_exactly_what_postgresql_refuses_to_cast() -> None:
    corpus = table("corpus", column("t", Text))
    async with _scratch("unparseable_test") as scratch, scratch.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION jsonb_parses(t text) RETURNS boolean LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM t::json::jsonb; RETURN true; EXCEPTION WHEN others THEN RETURN false; END $$"
            )
        )
        await conn.execute(text("CREATE TABLE corpus (t text)"))
        shapes = _ESCAPE_SHAPES + sorted(_CASTABLE_BUT_FLAGGED) + _generated_shapes()
        await conn.execute(corpus.insert(), [{"t": shape} for shape in shapes])
        rows = (
            await conn.execute(
                select(
                    corpus.c.t,
                    func.jsonb_parses(corpus.c.t),
                    json_unparseable(cast(corpus.c.t, JSON)),
                )
            )
        ).all()

    # Never an abort: every text PostgreSQL refuses is flagged.
    assert [stored[:80] for stored, parses, unparseable in rows if not parses and not unparseable] == []
    # Exact but for the listed numbers.
    assert {stored for stored, parses, unparseable in rows if parses and unparseable} == _CASTABLE_BUT_FLAGGED
    assert {parses for _stored, parses, _u in rows} == {True, False}


# -- the webhook routes --------------------------------------------------------


async def test_the_provider_route_skips_an_unreadable_agent_and_names_it_by_id(caplog) -> None:
    async with _scratch("unparseable_router") as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            good = _agent(owner, {"github": [str(uuid.uuid4())]})
            bound_unreadable = _agent(owner)
            elsewhere_unreadable = _agent(owner)
            db.add_all([good, bound_unreadable, elsewhere_unreadable])
            await db.flush()
            reached = _assignment(good, GITHUB_ISSUES)
            db.add_all(
                [reached, _assignment(bound_unreadable, GITHUB_ISSUES), _assignment(elsewhere_unreadable, GITHUB_ISSUES)]
            )
            await _store(db, Agent, bound_unreadable.id, "integration_bindings", f'{{"github": ["x"], "note": {UNREADABLE_NOTE}}}')
            await _store(db, Agent, elsewhere_unreadable.id, "integration_bindings", f'{{"jira": [{UNREADABLE_NOTE}]}}')
            await db.commit()

        with caplog.at_level(logging.WARNING):
            routing = await webhooks._load_provider_routing("github")

    assert {uuid.UUID(matcher.assignment_id) for matcher in routing.matchers} == {reached.id}
    [warning] = [record.getMessage() for record in caplog.records if "integration bindings hold" in record.getMessage()]
    assert warning.startswith("Provider webhook github: skipped 1 bound agent(s)")
    assert str(bound_unreadable.id) in warning
    assert str(elsewhere_unreadable.id) not in warning
    assert "top-secret" not in warning


_WELL_FORMED_BINDINGS = [
    pytest.param({"github": ["g1"]}, id="bound"),
    pytest.param({"github": []}, id="bound-empty"),
    pytest.param({"jira": ["j1"]}, id="other-provider"),
    pytest.param({"github": ["g1"], "note": "caf\u00e9 \U0001f600"}, id="valid-pair"),
    pytest.param({"github": ["g1"], "note": "\\ud800 \\u0000"}, id="escaped-backslash-text"),
    pytest.param({"Github": ["g1"]}, id="other-case"),
    pytest.param({"github": {"g1": True}}, id="object-value"),
    pytest.param(["github", "g1"], id="array"),
    pytest.param("github", id="scalar"),
    pytest.param({}, id="empty"),
    pytest.param(None, id="null"),
]


async def test_the_webhook_routes_select_exactly_what_the_unguarded_cast_selected() -> None:
    providers = ("github", "jira", "note", "Github", "slack")
    pairs = (("github", "g1"), ("jira", "j1"), ("github", "missing"), ("Github", "g1"))
    old_provider = "SELECT id FROM agents WHERE integration_bindings::jsonb ? :provider AND enabled IS true"
    old_integration = (
        "SELECT id FROM agents WHERE (integration_bindings::jsonb -> :provider) ? :integration_id "
        "AND enabled IS true"
    )
    async with _scratch("unparseable_parity") as scratch, scratch.sessions() as db:
        owner = _user("owner")
        db.add(owner)
        await db.flush()
        shapes = [param.values[0] for param in _WELL_FORMED_BINDINGS]
        # A shape no API accepts is stored as text: the ORM's flush hooks expect an object.
        agents = [_agent(owner, shape if shape is None or isinstance(shape, dict) else None) for shape in shapes]
        db.add_all([*agents, _agent(owner, {"github": ["g1"]}, enabled=False)])
        await db.flush()
        for agent, shape in zip(agents, shapes, strict=True):
            if shape is not None and not isinstance(shape, dict):
                await _store(db, Agent, agent.id, "integration_bindings", json.dumps(shape))
        await db.commit()

        async def both(unreadable_added: bool) -> None:
            for provider in providers:
                new = set(await webhooks._agents_bound_to_provider(db, provider))
                if unreadable_added:
                    with pytest.raises(DBAPIError):
                        async with db.begin_nested():
                            await db.execute(text(old_provider), {"provider": provider})
                else:
                    old = set((await db.execute(text(old_provider), {"provider": provider})).scalars())
                    assert new == old, provider
                    baseline[provider] = new
                assert new == baseline[provider], provider
            for provider, integration_id in pairs:
                new = {
                    agent.id
                    for agent in await webhooks._agents_bound_to_integration(
                        db, provider, integration_id, route="Integration webhook test"
                    )
                }
                key = f"{provider}:{integration_id}"
                if not unreadable_added:
                    old = set(
                        (
                            await db.execute(
                                text(old_integration), {"provider": provider, "integration_id": integration_id}
                            )
                        ).scalars()
                    )
                    assert new == old, key
                    baseline[key] = new
                assert new == baseline[key], key

        baseline: dict = {}
        await both(unreadable_added=False)
        assert baseline["github"] and baseline["github:g1"]
        for stored in (f'{{"github": ["g1"], "n": {UNREADABLE_NOTE}}}', r'{"github": ["g1"], "n": "\u0000"}'):
            unreadable = _agent(owner)
            db.add(unreadable)
            await db.flush()
            await _store(db, Agent, unreadable.id, "integration_bindings", stored)
        await db.commit()
        await both(unreadable_added=True)


# -- three-tier visibility and the other shared reads ------------------------


async def test_an_index_on_the_cast_refuses_the_value_at_write() -> None:
    """Why ``jsonb_cast_indexed`` columns keep their bare, indexable predicate."""
    async with _scratch("unparseable_indexed") as scratch, scratch.sessions() as db:
        owner = _user("owner")
        db.add(owner)
        await db.flush()
        agent = _agent(owner)
        integration = Integration(provider="github", name="GitHub", auth_type="api_key", user_id=owner.id)
        db.add_all([agent, integration])
        await db.commit()
        for model, row_id, column_name in (
            (Integration, integration.id, "shared_group_ids"),
            (Integration, integration.id, "shared_user_ids"),
            (Agent, agent.id, "shared_user_ids"),
        ):
            for stored in (r'["\ud800"]', r'["\u0000"]'):
                with pytest.raises(DBAPIError):
                    async with db.begin_nested():
                        await _store(db, model, row_id, column_name, stored)


async def test_one_unreadable_group_list_does_not_break_visibility_for_everyone() -> None:
    group, other_group = str(uuid.uuid4()), str(uuid.uuid4())
    old_arm = "SELECT id FROM agents WHERE shared_group_ids::jsonb ?| CAST(:groups AS text[])"
    async with _scratch("unparseable_visibility") as scratch, scratch.sessions() as db:
        owner, viewer = _user("owner"), _user("viewer")
        db.add_all([owner, viewer])
        await db.flush()
        shared = _agent(owner, shared_group_ids=[group])
        shared_pair = _agent(owner, shared_group_ids=[group, "caf\u00e9 \U0001f600"])
        elsewhere = _agent(owner, shared_group_ids=[other_group])
        private = _agent(owner)
        db.add_all([shared, shared_pair, elsewhere, private])
        await db.commit()

        arm = select(Agent.id).where(helpers._shared_group_ids_overlap(Agent, [group]))
        assert set((await db.execute(arm)).scalars()) == set(
            (await db.execute(text(old_arm), {"groups": [group]})).scalars()
        ) == {shared.id, shared_pair.id}

        unreadable = _agent(owner)
        db.add(unreadable)
        await db.flush()
        await _store(db, Agent, unreadable.id, "shared_group_ids", f'["{group}", {UNREADABLE_NOTE}]')
        await db.commit()

        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(text(old_arm), {"groups": [group]})
        visible = select(Agent.id).where(helpers.user_visible_filter(Agent, viewer.id, [group]))
        assert set((await db.execute(visible)).scalars()) == {shared.id, shared_pair.id}
        # Its owner still reaches it through the owner tier.
        mine = select(Agent.id).where(helpers.user_visible_filter(Agent, owner.id, [group]))
        assert unreadable.id in set((await db.execute(mine)).scalars())

        # The intent catalog reads the stored text without de-escaping it.
        catalog = select(Agent.id).where(intents._agent_shared_scope_filter())
        assert set((await db.execute(catalog)).scalars()) == {shared.id, shared_pair.id, elsewhere.id, unreadable.id}


async def test_variables_and_the_public_catalog_tag_filter_answer_around_an_unreadable_row() -> None:
    group = str(uuid.uuid4())
    async with _scratch("unparseable_catalog") as scratch, scratch.sessions() as db:
        owner, viewer = _user("owner"), _user("viewer")
        db.add_all([owner, viewer])
        await db.flush()
        shared_var = Variable(name="region", value="eu", user_id=owner.id, shared_group_ids=[group])
        unreadable_var = Variable(name="secret-region", value="us", user_id=owner.id)
        tagged = Prompt(name="Tagged", content="x", user_id=owner.id, tags=["cti", 'a"b', "c\\d", "caf\u00e9"])
        other = Prompt(name="Other", content="x", user_id=owner.id, tags=["aev"])
        unreadable_prompt = Prompt(name="Unreadable", content="x", user_id=owner.id)
        db.add_all([shared_var, unreadable_var, tagged, other, unreadable_prompt])
        await db.flush()
        await _store(db, Variable, unreadable_var.id, "shared_group_ids", f'["{group}", {UNREADABLE_NOTE}]')
        await _store(db, Prompt, unreadable_prompt.id, "tags", f'["cti", {UNREADABLE_NOTE}]')
        await db.commit()

        variables = await load_variables(db, str(viewer.id), [group])
        assert set(variables) == {str(shared_var.id)}

        async def tagged_with(tags: str) -> set:
            stmt = public_catalog._apply_catalog_filters(select(Prompt.id), Prompt, search=None, tags=tags)
            return set((await db.execute(stmt)).scalars())

        assert await tagged_with("cti") == {tagged.id}
        # A tag is bound, never spliced into a JSON literal.
        assert await tagged_with('a"b') == {tagged.id}
        assert await tagged_with("c\\d") == {tagged.id}
        assert await tagged_with("caf\u00e9, cti") == {tagged.id}
        assert await tagged_with("aev") == {other.id}


async def test_the_builtin_integrations_seed_survives_an_unreadable_tag_list() -> None:
    async with _scratch("unparseable_seed") as scratch, scratch.sessions() as db:
        owner = _user("owner")
        db.add(owner)
        await db.flush()
        def _company(name: str) -> Integration:
            return Integration(
                provider="github", name=name, auth_type="api_key", is_company_managed=True, user_id=owner.id
            )

        stale_readable, stale_unreadable, other_unreadable = (
            _company("Stale readable"),
            _company("Stale unreadable"),
            _company("Other unreadable"),
        )
        db.add_all([stale_readable, stale_unreadable, other_unreadable])
        await db.flush()
        await _store(db, Integration, stale_readable.id, "tags", '["builtin"]')
        await _store(db, Integration, stale_unreadable.id, "tags", f'["builtin", {UNREADABLE_NOTE}]')
        await _store(db, Integration, other_unreadable.id, "tags", f'["ops", {UNREADABLE_NOTE}]')
        ids = (stale_readable.id, stale_unreadable.id, other_unreadable.id)
        await db.commit()
        # The seed reads the rows as stored, like the fresh session it runs in at startup.
        db.expire_all()

        await seed_builtin_integrations(db, platform_mode="xtm_one")
        await db.commit()
        providers = set((await db.execute(select(Integration.provider).where(Integration.is_builtin))).scalars())
        assert "filigran_ai" in providers
        left = set((await db.execute(select(Integration.id).where(Integration.id.in_(ids)))).scalars())
        # A stale built-in goes whether PostgreSQL can read its tags or not; a non-built-in stays.
        assert left == {other_unreadable.id}


async def test_the_cross_run_payload_search_skips_only_the_rows_it_cannot_parse() -> None:
    async with _scratch("unparseable_runs") as scratch, scratch.sessions() as db:
        owner = _user("owner")
        db.add(owner)
        await db.flush()
        agent = _agent(owner)
        db.add(agent)
        await db.flush()
        payloads = {
            "clean": '{"body": "needle"}',
            "pair": r'{"body": "needle \ud83d\ude00"}',
            "escaped": r'{"body": "needle \\ud800 \\u0000"}',
            "meta-only": '{"_webhook_meta": {"x": "needle"}}',
            "lone": r'{"body": "needle \ud800"}',
            "nul": r'{"body": "needle \u0000"}',
        }
        runs = {}
        for label, stored in payloads.items():
            run = AssignmentRun(agent_id=agent.id, user_id=owner.id, status="completed")
            db.add(run)
            await db.flush()
            await _store(db, AssignmentRun, run.id, "trigger_payload", stored)
            runs[label] = run.id
        await db.commit()

        found = set(
            (
                await db.execute(
                    select(AssignmentRun.id).where(_searchable_trigger_payload().ilike("%needle%"))
                )
            ).scalars()
        )
    assert found == {runs["clean"], runs["pair"], runs["escaped"]}


async def _a2a_runs(db, owner: User, contexts: dict[str, str]) -> dict[str, uuid.UUID]:
    agent = _agent(owner)
    db.add(agent)
    await db.flush()
    runs = {}
    for label, stored in contexts.items():
        run = AssignmentRun(agent_id=agent.id, user_id=owner.id, status="completed")
        db.add(run)
        await db.flush()
        await _store(db, AssignmentRun, run.id, "trigger_context", stored)
        runs[label] = run.id
    return runs


#: The A2A runs of one partner, and a run of another holding a ``\u0000`` escape (#11790).
A2A_CONTEXTS = {
    "mine": '{"source": "a2a", "client_id": "a2a-mine", "a2a_provider": "oauth_a2a"}',
    "mine-unstamped": '{"source": "a2a", "client_id": "a2a-mine"}',
    "other": '{"source": "a2a", "client_id": "a2a-other"}',
    "nul": r'{"source": "a2a", "client_id": "a2a-other", "message": "hi \u0000"}',
}


async def test_the_a2a_activity_filters_answer_around_a_nul_escape_in_another_run() -> None:
    """One run holding ``\\u0000`` in its trigger_context made ``->>`` fail /runs/all and /runs/count for every partner."""
    from app.api.v1.assignments import _filtered_runs_query

    async with _scratch("unparseable_a2a_runs") as scratch, scratch.sessions() as db:
        owner = _user("owner")
        db.add(owner)
        await db.flush()
        runs = await _a2a_runs(db, owner, A2A_CONTEXTS)
        await db.commit()

        query = _filtered_runs_query(owner, a2a_client_ids=["a2a-mine"], a2a_provider="oauth_a2a")
        found = set((await db.execute(query.with_only_columns(AssignmentRun.id))).scalars())
        counted = (
            await db.execute(select(func.count()).select_from(query.with_only_columns(AssignmentRun.id).subquery()))
        ).scalar()
    assert found == {runs["mine"], runs["mine-unstamped"]}
    assert counted == 2


async def test_the_run_context_repair_makes_every_row_readable_and_keeps_the_rest(capsys) -> None:
    revision = _load_revision("a2ah2runctx01_repair_unreadable_run_trigger_context")
    async with _scratch("unparseable_run_repair") as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            runs = await _a2a_runs(
                db, owner, {**A2A_CONTEXTS, "lone": f'{{"client_id": "a2a-other", "n": {UNREADABLE_NOTE}}}'}
            )
            await db.commit()
            before = {
                label: await _stored_text(db, AssignmentRun, run_id, "trigger_context")
                for label, run_id in runs.items()
            }

        for _ in range(2):  # idempotent: the second run finds nothing to repair
            async with scratch.engine.begin() as conn:
                await conn.run_sync(revision._repair_unreadable_trigger_contexts)

        async with scratch.sessions() as db:
            after = {
                label: await _stored_text(db, AssignmentRun, run_id, "trigger_context")
                for label, run_id in runs.items()
            }
            unreadable = (
                await db.execute(select(func.count()).where(json_unparseable(AssignmentRun.trigger_context)))
            ).scalar()
    assert unreadable == 0
    assert "\\u0000" not in after["nul"] and '"hi "' in after["nul"]
    assert "\\ud800" not in after["lone"] and "\\ufffd" in after["lone"]
    for label in ("mine", "mine-unstamped", "other"):
        assert after[label] == before[label]
    out = capsys.readouterr().out
    assert out.count("repaired the trigger_context of 2 run(s)") == 1
    assert "top-secret" not in out


# -- the write path ----------------------------------------------------------


async def test_a_json_write_stores_no_lone_surrogate_and_a_read_returns_none() -> None:
    async with _scratch("unparseable_write") as scratch:
        # Configured like the app's engine: the scratch one keeps SQLAlchemy's defaults.
        engine = create_async_engine(scratch.url, json_serializer=json_serializer, json_deserializer=json_deserializer)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                owner = _user("owner")
                db.add(owner)
                await db.flush()
                agent = _agent(
                    owner, {"github": ["\ud800"], "pair": "\ud83d\ude00", "emoji": "\U0001f600", "low": "x\udc00"}
                )
                db.add(agent)
                await db.commit()
                # The flushed object says what was stored.
                assert agent.integration_bindings == {
                    "github": ["\ufffd"],
                    "pair": "\ud83d\ude00",
                    "emoji": "\U0001f600",
                    "low": "x\ufffd",
                }
                stored = await _stored_text(db, Agent, agent.id, "integration_bindings")
                assert "\\ufffd" in stored and "\\ud83d\\ude00" in stored
                assert "\\ud800" not in stored and "\\udc00" not in stored
                parsed = (
                    await db.execute(select(cast(Agent.integration_bindings, JSONB)).where(Agent.id == agent.id))
                ).scalar()
                assert parsed["emoji"] == parsed["pair"] == "\U0001f600"

                # A column typed by a ``TypeDecorator`` over JSON is kept in step too.
                deck = Presentation(user_id=owner.id, title="Deck", spec={"title": "Deck \ud800", "slides": []})
                db.add(deck)
                await db.commit()
                assert deck.spec["title"] == "Deck \ufffd"
                assert "\\ud800" not in await _stored_text(db, Presentation, deck.id, "spec")

                # A write around the ORM flush goes through the engine's serializer.
                await db.execute(
                    update(Agent).where(Agent.id == agent.id).values(integration_bindings={"github": ["\udbff"]})
                )
                await db.commit()
                assert await _stored_text(db, Agent, agent.id, "integration_bindings") == '{"github": ["\\ufffd"]}'

                # A legacy row reads as the serializer would store it, stays as it is through an
                # unrelated save, and is stored clean by an edit of it.
                agent_id = agent.id
                await _store(db, Agent, agent_id, "integration_bindings", f'{{"github": [{UNREADABLE_NOTE}]}}')
                await db.commit()
                db.expire_all()
                loaded = await db.get(Agent, agent_id)
                assert loaded.integration_bindings == {"github": ["top-secret \ufffd"]}
                loaded.name = "Renamed"
                await db.commit()
                assert "\\ud800" in await _stored_text(db, Agent, agent_id, "integration_bindings")
                loaded.integration_bindings = {**loaded.integration_bindings, "jira": []}
                await db.commit()
                assert await _stored_text(db, Agent, agent_id, "integration_bindings") == (
                    '{"github": ["top-secret \\ufffd"], "jira": []}'
                )
        finally:
            await engine.dispose()


# -- the data migrations -----------------------------------------------------
#
# A data migration must see every row its logic is about: an unreadable row is repaired the way the
# write path now stores it, then goes through the migration's own logic. A row that cannot be read
# even then gets the restrictive outcome - never the permissive one a skip would give.


async def _agent_with_stored(db, owner: User, column: str, stored: str) -> Agent:
    agent = _agent(owner)
    db.add(agent)
    await db.flush()
    await _store(db, Agent, agent.id, column, stored)
    return agent


async def _migrate_opencrq(scratch, revision) -> None:
    async with scratch.engine.begin() as conn:
        await conn.run_sync(lambda sync: revision._migrate(sync, "opengrc", "opencrq", "x-old", "x-new"))


async def test_the_opencrq_rename_repairs_an_unreadable_row_and_renames_it(capsys) -> None:
    revision = _load_revision("o1crq2opencrq1_rename_opengrc_to_opencrq")
    async with _scratch("unparseable_rename") as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            readable = _agent(owner, {"opengrc": ["i1"]})
            db.add(readable)
            await db.flush()
            bindings = await _agent_with_stored(
                db, owner, "integration_bindings", f'{{"opengrc": ["i2"], "n": {UNREADABLE_NOTE}}}'
            )
            denied = await _agent_with_stored(db, owner, "tool_deny_list", f'["delete_opengrc_x", {UNREADABLE_NOTE}]')
            await db.commit()

        await _migrate_opencrq(scratch, revision)

        async with scratch.sessions() as db:
            assert (await db.get(Agent, readable.id)).integration_bindings == {"opencrq": ["i1"]}
            assert (await db.get(Agent, bindings.id)).integration_bindings == {
                "opencrq": ["i2"],
                "n": "top-secret \ufffd",
            }
            # The denied tool stays denied under its new name.
            assert (await db.get(Agent, denied.id)).tool_deny_list == ["delete_opencrq_x", "top-secret \ufffd"]
    out = capsys.readouterr().out
    assert "repaired 1 row(s) of agents.integration_bindings" in out
    assert "repaired 1 row(s) of agents.tool_deny_list" in out
    assert "top-secret" not in out


async def test_the_opencrq_rename_fails_closed_on_a_row_it_cannot_repair(capsys, monkeypatch) -> None:
    revision = _load_revision("o1crq2opencrq1_rename_opengrc_to_opencrq")
    monkeypatch.setattr(revision, "_repaired_json", lambda _stored: None)
    async with _scratch("unparseable_rename_closed") as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            bindings = await _agent_with_stored(db, owner, "integration_bindings", f'{{"opengrc": [{UNREADABLE_NOTE}]}}')
            await db.commit()

        # An allow list or a binding left under the old name grants less: left, and named.
        await _migrate_opencrq(scratch, revision)
        out = capsys.readouterr().out
        assert f"left 1 row(s) of agents.integration_bindings unchanged ({bindings.id})" in out
        assert "top-secret" not in out

        async with scratch.sessions() as db:
            denied = await _agent_with_stored(db, owner, "tool_deny_list", f'["delete_opengrc_x", {UNREADABLE_NOTE}]')
            await db.commit()

        # A deny list left under the old name would un-deny the renamed tool: the upgrade stops.
        with pytest.raises(RuntimeError) as excinfo:
            await _migrate_opencrq(scratch, revision)
        assert str(denied.id) in str(excinfo.value)
        assert "agents.tool_deny_list" in str(excinfo.value)
        assert "top-secret" not in str(excinfo.value)
        async with scratch.sessions() as db:
            assert "delete_opengrc_x" in await _stored_text(db, Agent, denied.id, "tool_deny_list")


async def _live_exposure(db, agent: Agent, owner: User) -> None:
    await db.execute(
        text(
            "INSERT INTO a2a_exposures (id, agent_id, card_token, run_as_user_id, created_at, updated_at) "
            "VALUES (:id, :agent, :token, :owner, now(), now())"
        ),
        {"id": uuid.uuid4(), "agent": agent.id, "token": uuid.uuid4().hex, "owner": owner.id},
    )


async def _a2a_revocation(scratch, revision) -> dict:
    async with scratch.engine.begin() as conn:
        await conn.run_sync(revision._revoke_unbound_exposures)
    async with scratch.sessions() as db:
        return dict((await db.execute(text("SELECT agent_id, revoked_at IS NOT NULL FROM a2a_exposures"))).all())


async def test_the_a2a_revocation_repairs_unreadable_bindings_and_converges(capsys) -> None:
    revision = _load_revision("a2a2intmodel1_a2a_partner_relationship_on_integrations")
    async with _scratch("unparseable_a2a") as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            unbound = _agent(owner, {"github": ["g1"]})
            db.add(unbound)
            await db.flush()
            # No A2A binding, only an unreadable value elsewhere: the state the step exists to revoke.
            unreadable_unbound = await _agent_with_stored(
                db, owner, "integration_bindings", f'{{"github": ["g1"], "n": {UNREADABLE_NOTE}}}'
            )
            unreadable_bound = await _agent_with_stored(
                db, owner, "integration_bindings", f'{{"oauth_a2a": ["{uuid.uuid4()}"], "n": {UNREADABLE_NOTE}}}'
            )
            for agent in (unbound, unreadable_unbound, unreadable_bound):
                await _live_exposure(db, agent, owner)
            await db.commit()

        revoked = await _a2a_revocation(scratch, revision)

        async with scratch.sessions() as db:
            stored = await _stored_text(db, Agent, unreadable_bound.id, "integration_bindings")
    assert revoked == {unbound.id: True, unreadable_unbound.id: True, unreadable_bound.id: False}
    assert "\\ud800" not in stored and "\\ufffd" in stored
    out = capsys.readouterr().out
    assert "revoked 2 exposure(s)" in out
    assert "repaired the integration bindings of 2 agent(s)" in out
    assert "top-secret" not in out


async def test_the_a2a_revocation_fails_closed_on_bindings_it_cannot_repair(capsys, monkeypatch) -> None:
    revision = _load_revision("a2a2intmodel1_a2a_partner_relationship_on_integrations")
    monkeypatch.setattr(revision, "_repaired_json", lambda _stored: None)
    async with _scratch("unparseable_a2a_closed") as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            bound = _agent(owner, {"oauth_a2a": [str(uuid.uuid4())]})
            db.add(bound)
            await db.flush()
            unreadable_bound = await _agent_with_stored(
                db, owner, "integration_bindings", f'{{"oauth_a2a": ["{uuid.uuid4()}"], "n": {UNREADABLE_NOTE}}}'
            )
            for agent in (bound, unreadable_bound):
                await _live_exposure(db, agent, owner)
            await db.commit()

        revoked = await _a2a_revocation(scratch, revision)
    # Nothing shows the binding it may hold: revoked, never left reachable.
    assert revoked == {bound.id: False, unreadable_bound.id: True}
    out = capsys.readouterr().out
    assert f"cannot be read ({unreadable_bound.id})" in out
    assert "top-secret" not in out


async def test_the_migrations_decide_a_number_candidate_by_a_real_cast(capsys) -> None:
    """A number past numeric's range gets the restrictive outcome; a flagged one that casts is migrated as before."""
    rename = _load_revision("o1crq2opencrq1_rename_opengrc_to_opencrq")
    a2a = _load_revision("a2a2intmodel1_a2a_partner_relationship_on_integrations")
    async with _scratch("unparseable_numbers") as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            # Castable, although the candidate test flags it: renamed and kept live, exactly as before.
            band_denied = await _agent_with_stored(db, owner, "tool_deny_list", '["delete_opengrc_x", 1e20000]')
            band_bound = await _agent_with_stored(
                db, owner, "integration_bindings", f'{{"oauth_a2a": ["{uuid.uuid4()}"], "n": 1e20000}}'
            )
            # Past numeric's range: nothing can repair it.
            out_of_range_unbound = await _agent_with_stored(
                db, owner, "integration_bindings", '{"github": ["g1"], "n": 1e1000000}'
            )
            out_of_range_bound = await _agent_with_stored(
                db, owner, "integration_bindings", f'{{"oauth_a2a": ["{uuid.uuid4()}"], "n": 1e1000000}}'
            )
            for agent in (band_bound, out_of_range_unbound, out_of_range_bound):
                await _live_exposure(db, agent, owner)
            # No exposure, nothing to rename: either step may still evaluate it, and must not abort.
            unexposed = await _agent_with_stored(db, owner, "integration_bindings", '{"github": ["g1"], "n": 1e1000000}')
            await db.commit()

        await _migrate_opencrq(scratch, rename)
        revoked = await _a2a_revocation(scratch, a2a)

        async with scratch.sessions() as db:
            # Renamed by the rewrite itself, whose jsonb_agg spells the number out as it always did.
            band_text = await _stored_text(db, Agent, band_denied.id, "tool_deny_list")
            assert band_text.startswith('["delete_opencrq_x", 1') and "opengrc" not in band_text
            assert "1e20000" in await _stored_text(db, Agent, band_bound.id, "integration_bindings")
            assert "1e1000000" in await _stored_text(db, Agent, unexposed.id, "integration_bindings")
            denied = await _agent_with_stored(db, owner, "tool_deny_list", '["delete_opengrc_y", 1e1000000]')
            await db.commit()
        with pytest.raises(RuntimeError) as excinfo:
            await _migrate_opencrq(scratch, rename)
    assert revoked == {band_bound.id: False, out_of_range_unbound.id: True, out_of_range_bound.id: True}
    assert str(denied.id) in str(excinfo.value)
    assert "repaired" not in capsys.readouterr().out
