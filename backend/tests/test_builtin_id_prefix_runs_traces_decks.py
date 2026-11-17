"""The second slice of the id-prefix resolution (#8852): get_run, the
list_runs filters, get_integration, get_trace_payload and the presentation
tools take an unambiguous id prefix of 8+ hexadecimal characters.

The resolver itself (``builtin_tools/id_prefix.py``) is covered by
``test_builtin_id_prefix.py``; these tests pin each tool to it: one visible
match stands for the full id, several are refused naming them, none is "not
found", and a sub-8 prefix keeps the malformed-argument refusal.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.builtin_tools import id_prefix as idp
from app.tools.base import ToolCall

FULL = "48b4e177-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
OTHER = "48b4e177-ffff-4e5f-8a9b-0c1d2e3f4a5b"


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ── the lookup ───────────────────────────────────────────────────────────────


def test_the_prefix_is_a_uuid_range_the_primary_key_index_serves():
    from app.models.llm_trace import LLMTrace

    low, high = idp.prefix_bounds("48b4e177-1c")
    assert str(low) == "48b4e177-1c00-0000-0000-000000000000"
    assert str(high) == "48b4e177-1cff-ffff-ffff-ffffffffffff"
    sql = str(idp.id_prefix_clause(LLMTrace.id, "48b4e177").compile(dialect=postgresql.dialect()))
    assert "BETWEEN" in sql and "CAST" not in sql


@pytest.mark.parametrize("candidate", [FULL, OTHER, "48b4e177-0000-0000-0000-000000000000"])
def test_every_id_starting_the_prefix_falls_inside_its_range(candidate):
    low, high = idp.prefix_bounds("48b4e177")
    assert low <= uuid.UUID(candidate) <= high
    assert not (low <= uuid.UUID("48b4e178-0000-0000-0000-000000000000") <= high)


# ── platform tools: get_run, list_runs, get_integration ─────────────────────


def _builder(monkeypatch, resolve):
    from app.api.v1 import helpers
    from app.core import database
    from app.services.builtin_tools import agent_builder

    async def _groups(db, uid):
        return []

    async def _caller(db, uid):
        return None

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(idp, "resolve_id_prefix", resolve)
    _, executor = agent_builder.build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    return executor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arg", "noun"),
    [
        ("get_run", "run_id", "runs"),
        ("get_integration", "integration_id", "integrations"),
        ("list_runs", "assignment_id", "assignments"),
        ("list_runs", "agent_id", "agents"),
    ],
)
async def test_an_ambiguous_prefix_is_refused_naming_the_candidates(monkeypatch, tool, arg, noun):
    seen: list = []

    async def _ambiguous(db, model, value, clauses=(), *, noun, label=None):
        seen.append(noun)
        assert clauses, "the tool's visibility clause is applied"
        return value, idp.ambiguous_text(value, noun, [(FULL, "one"), (OTHER, "two")])

    executor = _builder(monkeypatch, _ambiguous)
    result = await _call(executor, tool, **{arg: "48b4e177"})
    assert seen == [noun]
    assert FULL in result.content and OTHER in result.content
    assert "must be a valid UUID" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arg", "entity"),
    [
        ("get_run", "run_id", "Run"),
        ("get_integration", "integration_id", "Integration"),
        ("list_runs", "assignment_id", "Assignment"),
        ("list_runs", "agent_id", "Agent"),
    ],
)
async def test_a_prefix_starting_nothing_visible_is_answered_not_found(monkeypatch, tool, arg, entity):
    async def _none(db, model, value, clauses=(), *, noun, label=None):
        return value, None

    executor = _builder(monkeypatch, _none)
    result = await _call(executor, tool, **{arg: "61ba923c"})
    assert f"{entity} `61ba923c` not found or you don't have access" in result.content
    assert "must be a valid UUID" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arg"),
    [("get_run", "run_id"), ("get_integration", "integration_id"), ("list_runs", "assignment_id")],
)
async def test_a_seven_character_prefix_keeps_the_malformed_argument_refusal(monkeypatch, tool, arg):
    async def _never(db, model, value, clauses=(), *, noun, label=None):
        raise AssertionError("a 7-character value is not looked up")

    executor = _builder(monkeypatch, _never)
    result = await _call(executor, tool, **{arg: "61ba923"})
    assert f"{arg} must be a valid UUID" in result.content


@pytest.mark.asyncio
async def test_a_run_prefix_resolves_under_the_run_visibility(monkeypatch):
    from app.models.assignment import AssignmentRun

    captured: dict = {}

    async def _one(db, model, value, clauses=(), *, noun, label=None):
        captured.update(model=model, clauses=list(clauses), label=label)
        return FULL, None

    executor = _builder(monkeypatch, _one)
    # The run read itself then finds nothing in the stub session: what matters
    # is that the full id reached it, not the malformed-argument refusal.
    try:
        result = await _call(executor, "get_run", run_id="48b4e177")
        content = result.content
    except Exception as exc:  # the stub session has no execute
        content = str(exc)
    assert captured["model"] is AssignmentRun
    assert captured["label"] is AssignmentRun.status
    sql = str(captured["clauses"][0].compile(dialect=postgresql.dialect()))
    # Own and company-managed runs, as get_run reads them.
    assert "user_id" in sql
    assert "must be a valid UUID" not in content


# ── observability: get_trace_payload ────────────────────────────────────────


def _trace_tools(monkeypatch, resolve_trace):
    from app.services.builtin_tools import traces

    async def _ctx(db, user_id):
        return SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user"), []

    monkeypatch.setattr(traces, "async_session", lambda: _Session())
    monkeypatch.setattr(traces, "_load_user_context", _ctx)
    monkeypatch.setattr(traces, "_resolve_trace_prefix", resolve_trace)

    async def _no_run(_db, _value, *, own_agent_id):
        return None, None

    monkeypatch.setattr(traces, "_own_trace_of_run", _no_run)
    _, executor = traces.build_trace_tools(SimpleNamespace(), str(uuid.uuid4()), agent_id=str(uuid.uuid4()))
    return executor


@pytest.mark.asyncio
async def test_an_ambiguous_trace_prefix_is_refused_naming_the_traces(monkeypatch):
    async def _ambiguous(db, prefix, value, **kw):
        assert prefix == "48b4e177"
        return None, idp.ambiguous_text(value, "traces", [(FULL, "run a"), (OTHER, "run b")])

    executor = _trace_tools(monkeypatch, _ambiguous)
    result = await _call(executor, "get_trace_payload", trace_id="48b4e177")
    assert FULL in result.content and OTHER in result.content


@pytest.mark.asyncio
async def test_a_trace_prefix_starting_no_readable_trace_is_answered_not_found(monkeypatch):
    async def _none(db, prefix, value, **kw):
        return None, None

    executor = _trace_tools(monkeypatch, _none)
    result = await _call(executor, "get_trace_payload", trace_id="48b4e177")
    assert "trace not found, or not visible to you" in result.content


@pytest.mark.asyncio
async def test_a_seven_character_trace_prefix_keeps_the_malformed_argument_refusal(monkeypatch):
    async def _never(db, prefix, value, **kw):
        raise AssertionError("a 7-character value is not looked up")

    executor = _trace_tools(monkeypatch, _never)
    result = await _call(executor, "get_trace_payload", trace_id="48b4e17")
    assert "trace_id must be a valid UUID" in result.content
    result = await _call(executor, "get_trace_payload", trace_id=FULL, span_id="48b4e1z")
    assert "span_id must be a valid UUID" in result.content


@pytest.mark.asyncio
async def test_a_trace_prefix_lookup_keeps_the_trace_explorer_visibility():
    from app.services.builtin_tools import traces

    class _Db:
        def __init__(self):
            self.statements: list = []

        async def execute(self, stmt):
            self.statements.append(stmt)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(id=uuid.UUID(FULL), name="run a")]),
                all=lambda: [],
            )

    db = _Db()
    viewer = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    resolved, refusal = await traces._resolve_trace_prefix(
        db, "48b4e177", "48b4e177", viewer=viewer, group_ids=[], own_agent_id=None
    )
    assert (resolved, refusal) == (uuid.UUID(FULL), None)
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "BETWEEN" in sql
    # trace_visible_query's per-user arms are kept: never a cross-user lookup.
    assert "llm_traces.user_id" in sql


# ── presentations ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_presentation_tool_taking_a_presentation_id_is_wrapped():
    from app.services.builtin_tools.presentation import build_presentation_tools

    _, executor = build_presentation_tools(SimpleNamespace(), str(uuid.uuid4()))
    for name in ("get_presentation", "edit_presentation", "duplicate_presentation", "delete_presentation"):
        assert getattr(executor.handler(name), "__wrapped__", None) is not None, name


@pytest.mark.asyncio
async def test_a_deck_prefix_reaches_the_tool_with_the_full_id(monkeypatch):
    from app.core import database
    from app.services.builtin_tools import presentation as pres

    async def _resolve(db, value, user_id):
        return FULL, None

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(pres, "resolve_presentation_id_prefix", _resolve)
    seen: list = []

    async def _probe(presentation_id: str = "", slides=None):
        seen.append(presentation_id)
        return "ok"

    wrapped = pres._with_presentation_id_prefix(_probe, str(uuid.uuid4()))
    assert await wrapped(presentation_id="48b4e177") == "ok"
    assert seen == [FULL]
    seen.clear()
    await wrapped(presentation_id=OTHER)
    assert seen == [OTHER]


@pytest.mark.asyncio
async def test_an_ambiguous_deck_prefix_is_refused_before_the_tool_runs(monkeypatch):
    from app.core import database
    from app.services.builtin_tools import presentation as pres

    refusal = idp.ambiguous_text("48b4e177", "presentations", [(FULL, "Board"), (OTHER, "Kick-off")])

    async def _resolve(db, value, user_id):
        return value, refusal

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(pres, "resolve_presentation_id_prefix", _resolve)

    async def _probe(presentation_id: str = "", slides=None):
        raise AssertionError("an ambiguous prefix must not reach the tool")

    wrapped = pres._with_presentation_id_prefix(_probe, str(uuid.uuid4()))
    result = await wrapped(presentation_id="48b4e177")
    assert FULL in result and OTHER in result


@pytest.mark.asyncio
async def test_a_deck_prefix_resolves_under_the_deck_visibility(monkeypatch):
    from app.api.v1 import helpers
    from app.services.builtin_tools import presentation as pres

    async def _groups(db, uid):
        return []

    class _Db:
        def __init__(self, rows):
            self.rows = rows
            self.statements: list = []

        async def execute(self, stmt):
            self.statements.append(stmt)
            rows = self.rows
            return SimpleNamespace(all=lambda: rows)

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    db = _Db([(uuid.UUID(FULL), "Board")])
    assert await pres.resolve_presentation_id_prefix(db, "48b4e177", str(uuid.uuid4())) == (FULL, None)
    assert await pres.resolve_presentation_id_prefix(db, FULL, str(uuid.uuid4())) == (FULL, None)
    assert await pres.resolve_presentation_id_prefix(db, "48b4e17", str(uuid.uuid4())) == ("48b4e17", None)
    assert len(db.statements) == 1
    db.rows = []
    assert await pres.resolve_presentation_id_prefix(db, "48b4e177", str(uuid.uuid4())) == ("48b4e177", None)
