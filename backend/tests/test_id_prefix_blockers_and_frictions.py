"""An 8+ hex id prefix on the work-item and friction id parameters (#11553).

Every id argument takes a unique prefix and stores or filters on the full id
it starts; a prefix starting several is refused with the candidates; one
starting none is refused and nothing is written or listed. A filter is never
dropped (a dropped owner, agent or run filter widened the listing).
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services import frictions
from app.services.builtin_tools import build_friction_tools, build_work_item_tools
from app.services.builtin_tools import id_prefix as id_prefix_module
from app.tools.base import ToolCall

ITEM = "1205bf16-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
TWIN = "1205bf16-ffff-4e5f-8a9b-0c1d2e3f4a5b"
TARGET = "9c1142f6-d1e1-434a-9099-1c63e3286156"
AGENT_B = "9c1142f6-0000-4000-8000-000000000002"
OWN_ITEM = "77777777-0000-4000-8000-000000000001"


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _tools():
    return build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))


def _items_by_prefix(items: dict[str, str]):
    async def _by_prefix(prefix, *, user_id=None, reader=None, limit=6):
        return [{"id": i, "title": t} for i, t in sorted(items.items()) if i.startswith(prefix)][:limit]

    return _by_prefix


class _StopError(ValueError):
    """Raised by a fake service once it has seen its arguments."""


def _capturing(seen: dict):
    async def _service(*args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)
        raise _StopError("stop")

    return _service


def _refusing(name: str):
    async def _never(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be called")

    return _never


# ── blocked_on on create_work_item and update_work_item ─────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["create_work_item", "update_work_item"])
async def test_a_blocked_on_prefix_is_stored_as_the_full_id(monkeypatch, tool):
    seen: dict = {}
    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _items_by_prefix({ITEM: "The fix", OWN_ITEM: "Mine"}))
    monkeypatch.setattr(wi_service, tool, _capturing(seen))
    _defs, executor = _tools()
    extra = {"title": "x"} if tool == "create_work_item" else {"work_item_id": OWN_ITEM}
    await _call(executor, tool, blocked_on=["1205bf16", TWIN], **extra)
    assert seen["blocked_on"] == [ITEM, TWIN]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["create_work_item", "update_work_item"])
async def test_an_ambiguous_blocked_on_prefix_is_refused_with_the_candidates(monkeypatch, tool):
    monkeypatch.setattr(
        wi_service, "work_items_by_id_prefix", _items_by_prefix({ITEM: "One", TWIN: "Two", OWN_ITEM: "Mine"})
    )
    monkeypatch.setattr(wi_service, tool, _refusing(tool))
    _defs, executor = _tools()
    extra = {"title": "x"} if tool == "create_work_item" else {"work_item_id": OWN_ITEM}
    result = await _call(executor, tool, blocked_on="1205bf16", **extra)
    assert ITEM in result.content and TWIN in result.content
    assert "Nothing was done" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["create_work_item", "update_work_item"])
async def test_an_unknown_blocked_on_prefix_is_refused_and_nothing_written(monkeypatch, tool):
    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _items_by_prefix({OWN_ITEM: "Mine"}))
    monkeypatch.setattr(wi_service, tool, _refusing(tool))
    _defs, executor = _tools()
    extra = {"title": "x"} if tool == "create_work_item" else {"work_item_id": OWN_ITEM}
    result = await _call(executor, tool, blocked_on="deadbeef", **extra)
    assert "no work item you can see has an id starting with `deadbeef`" in result.content


@pytest.mark.asyncio
async def test_update_without_blocked_on_leaves_it_unchanged(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(wi_service, "update_work_item", _capturing(seen))
    _defs, executor = _tools()
    await _call(executor, "update_work_item", work_item_id=OWN_ITEM, note="n")
    assert seen["blocked_on"] is None


# ── agent ids on owner_agent_id ──────────────────────────────────────────────


@pytest.fixture
def agents(monkeypatch):
    """Agents the caller can see, resolved by prefix without a database."""
    visible = {TARGET: "Developer"}

    @asynccontextmanager
    async def _session():
        yield SimpleNamespace()

    async def _groups(_db, _uid):
        return []

    async def _resolve(_db, _model, value, _clauses=(), *, noun, label=None):
        prefix = id_prefix_module.id_prefix(value)
        candidates = [(i, n) for i, n in sorted(visible.items()) if prefix and i.startswith(prefix)]
        return id_prefix_module.resolved_or_refused(value, noun, candidates)

    import app.api.v1.helpers as helpers
    import app.core.database as database

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(helpers, "caller_visible_filter", lambda *_a, **_k: None)
    monkeypatch.setattr(id_prefix_module, "resolve_id_prefix", _resolve)
    return visible


@pytest.mark.asyncio
async def test_an_owner_prefix_is_stored_as_the_full_agent_id(monkeypatch, agents):
    seen: dict = {}
    monkeypatch.setattr(wi_service, "create_work_item", _capturing(seen))
    _defs, executor = _tools()
    await _call(executor, "create_work_item", title="x", owner_agent_id="9c1142f6")
    assert seen["owner_agent_id"] == TARGET


@pytest.mark.asyncio
async def test_an_ambiguous_or_unknown_owner_prefix_is_refused(monkeypatch, agents):
    agents[AGENT_B] = "Other"
    monkeypatch.setattr(wi_service, "create_work_item", _refusing("create_work_item"))
    _defs, executor = _tools()
    ambiguous = await _call(executor, "create_work_item", title="x", owner_agent_id="9c1142f6")
    assert TARGET in ambiguous.content and AGENT_B in ambiguous.content
    unknown = await _call(executor, "create_work_item", title="x", owner_agent_id="deadbeef")
    assert "no agent you can see has an id starting with `deadbeef`" in unknown.content


@pytest.mark.asyncio
async def test_a_roster_owner_resolves_by_prefix_among_the_peers(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(wi_service, "create_work_item", _capturing(seen))
    _defs, executor = build_work_item_tools(
        SimpleNamespace(),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        peer_agents=[{"id": TARGET, "name": "Developer", "slug": "dev"}],
    )
    await _call(executor, "create_work_item", title="x", owner_agent_id="9c1142f6")
    assert seen["owner_agent_id"] == TARGET


@pytest.fixture
def _no_scope(monkeypatch):
    from app.services import coordination_scope

    scope = SimpleNamespace(work_item_clause=lambda: None, label=lambda: "Scope: all.")

    async def _resolve(*_args, **_kwargs):
        return scope

    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _resolve)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_a_list_owner_prefix_filters_on_the_full_id(monkeypatch, agents):
    seen: dict = {}

    async def _list(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    await _call(executor, "list_work_items", owner_agent_id="9c1142f6")
    assert seen["owner_agent_id"] == TARGET


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
@pytest.mark.parametrize("value", ["deadbeef", "not an id"])
async def test_a_list_owner_that_names_no_agent_is_refused_never_dropped(monkeypatch, agents, value):
    monkeypatch.setattr(wi_service, "list_work_items", _refusing("list_work_items"))
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", owner_agent_id=value)
    assert result.content.startswith("Error:")
    assert "Nothing was listed" in result.content


# ── list_frictions agent_id / run_id and dispose_friction id ────────────────


AGENT = uuid.uuid4()
USER = uuid.uuid4()


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def friction_ids(monkeypatch):
    """The agent and run ids of the caller's frictions, faked at the lookup."""
    known: dict[str, list[str]] = {"agent_id": [], "run_id": []}
    seen: dict = {}

    async def _distinct(_db, column, prefix, clauses=()):
        seen.setdefault("clauses", []).append(list(clauses))
        return [(i, "") for i in sorted(known[column.key]) if i.startswith(prefix)][:6]

    async def _list_rows(*_args, **_kwargs):
        return [], 0

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(id_prefix_module, "distinct_ids_by_prefix", _distinct)
    return known, seen


def _filter(column_key: str, value: str):
    column = getattr(frictions.AgentFriction, column_key)
    return asyncio.run(
        frictions._filter_id(_Session(), column, value, column_key[:-3], [frictions.AgentFriction.user_id == USER])
    )


def test_a_friction_filter_prefix_resolves_to_the_full_id(friction_ids):
    known, seen = friction_ids
    known["agent_id"] = [TARGET]
    known["run_id"] = [ITEM]
    assert str(_filter("agent_id", "9c1142f6")) == TARGET
    assert str(_filter("run_id", "1205bf16")) == ITEM
    # The lookup stays inside the caller's frictions.
    assert all(c for c in seen["clauses"])


def test_an_ambiguous_friction_filter_prefix_is_refused_with_the_candidates(friction_ids):
    known, _seen = friction_ids
    known["agent_id"] = [TARGET, AGENT_B]
    with pytest.raises(ValueError) as refused:
        _filter("agent_id", "9c1142f6")
    assert TARGET in str(refused.value) and AGENT_B in str(refused.value)


@pytest.mark.parametrize("value", ["deadbeef", "not-an-id"])
def test_a_friction_filter_naming_nothing_is_refused_never_dropped(friction_ids, value):
    with pytest.raises(ValueError):
        _filter("agent_id", value)


def test_a_full_id_filter_reads_no_candidates(friction_ids):
    _known, seen = friction_ids
    assert str(_filter("run_id", ITEM)) == ITEM
    assert "clauses" not in seen


def test_list_frictions_refuses_an_unknown_agent_prefix(friction_ids):
    _, executor = build_friction_tools(str(USER), str(AGENT))
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="1", name="list_frictions", arguments={"agent_id": "deadbeef"}))
    )
    assert result.content.startswith("Error:") and "Nothing was listed" in result.content


@pytest.fixture
def friction_rows(monkeypatch):
    known: dict[str, str] = {}

    async def _by_prefix(_db, _model, prefix, clauses=(), *, label=None):
        assert clauses, "the lookup must be scoped to the caller's frictions"
        return [(uuid.UUID(i), k) for i, k in sorted(known.items()) if i.startswith(prefix)][:6]

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(id_prefix_module, "ids_by_prefix", _by_prefix)
    return known


def _full_friction(value: str):
    return asyncio.run(frictions._full_friction_id(value, agent_id=AGENT, user_id=USER))


def test_a_friction_id_prefix_resolves_to_the_one_friction(friction_rows):
    friction_rows[ITEM] = "manual"
    assert str(_full_friction("1205bf16")) == ITEM


def test_an_ambiguous_friction_id_prefix_is_refused_with_the_candidates(friction_rows):
    friction_rows[ITEM] = "manual"
    friction_rows[TWIN] = "tool_error"
    with pytest.raises(ValueError) as refused:
        _full_friction("1205bf16")
    assert ITEM in str(refused.value) and TWIN in str(refused.value)


@pytest.mark.parametrize("value", ["deadbeef", "nope"])
def test_an_unknown_friction_id_is_not_found(friction_rows, value):
    with pytest.raises(frictions.FrictionNotFoundError):
        _full_friction(value)
