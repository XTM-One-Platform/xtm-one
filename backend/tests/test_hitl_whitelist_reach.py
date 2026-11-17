"""A whitelist entry covers the tool it was decided for.

The whitelist is keyed by bare name, and the executor lets the last tool
registered under a name replace the first. So an MCP server or a custom tool
taking a whitelisted name ran unattended, and a company-managed MCP server
advertising the name of a built-in the product gates (``web_fetch``) exempted
the built-in for every agent on the deployment.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import hitl_whitelist
from app.services import hitl_whitelist_derived as derived
from app.tools.approval import REACH_ANY, REACH_PRODUCT, evaluate_gate
from app.tools.base import HumanInputRequestedError, ToolDefinition


def _decide(name, *, source, entity_id="", reach, whitelist=None):
    return evaluate_gate(
        name,
        hitl_enabled=True,
        whitelist=frozenset(whitelist if whitelist is not None else {name}),
        active_exceptions={},
        current_fingerprint="fp",
        tool_source=source,
        tool_entity_id=entity_id,
        whitelist_reach=reach,
    ).action


# --- the decision table ----------------------------------------------------


def test_a_shipped_name_does_not_exempt_an_mcp_tool_taking_it():
    reach = {"list_agents": frozenset({REACH_PRODUCT})}

    assert _decide("list_agents", source="builtin", reach=reach) == "execute"
    assert (
        _decide("list_agents", source="mcp:Evil", entity_id="srv-1", reach=reach)
        == "pause"
    )


def test_a_derived_read_does_not_exempt_a_custom_tool_named_after_it():
    """A custom tool may take any name, so a derived read must not exempt it."""
    reach = {"search_notion": frozenset({REACH_PRODUCT})}

    assert (
        _decide("search_notion", source="custom:abc", entity_id="abc", reach=reach)
        == "pause"
    )
    assert _decide("search_notion", source="integration:notion", reach=reach) == "execute"


def test_an_mcp_read_covers_the_server_it_was_derived_from_only():
    reach = {"search_wiki": frozenset({"srv-1"})}

    assert (
        _decide("search_wiki", source="mcp:Wiki", entity_id="srv-1", reach=reach)
        == "execute"
    )
    assert (
        _decide("search_wiki", source="mcp:Personal", entity_id="srv-2", reach=reach)
        == "pause"
    )


def test_an_mcp_read_does_not_exempt_the_gated_builtin_of_the_same_name():
    """``web_fetch`` is deliberately gated and the classifier calls it a read."""
    reach = {"web_fetch": frozenset({"srv-1"})}

    assert _decide("web_fetch", source="builtin", reach=reach) == "pause"
    assert (
        _decide("web_fetch", source="mcp:Fetch", entity_id="srv-1", reach=reach)
        == "execute"
    )


@pytest.mark.parametrize(
    ("source", "entity_id"),
    [("builtin", ""), ("custom:1", "1"), ("mcp:Any", "srv-9"), ("integration:x", "")],
)
def test_an_administrators_addition_covers_any_tool_of_the_name(source, entity_id):
    reach = {"my_script": frozenset({REACH_ANY})}

    assert (
        _decide("my_script", source=source, entity_id=entity_id, reach=reach)
        == "execute"
    )


def test_a_name_with_no_recorded_reach_covers_nothing():
    assert (
        _decide("list_agents", source="builtin", reach={"other": frozenset({REACH_PRODUCT})})
        == "pause"
    )


def test_a_caller_without_a_reach_keeps_membership_by_name_for_product_tools():
    assert _decide("list_agents", source="builtin", reach=None) == "execute"
    assert _decide("list_agents", source="integration:x", reach=None) == "execute"


@pytest.mark.parametrize(
    ("source", "entity_id"), [("mcp:S", "srv-1"), ("custom:abc", "abc")]
)
def test_a_tool_that_names_itself_is_never_exempted_by_name_alone(source, entity_id):
    """With no reach, a custom or MCP tool taking a whitelisted name still gates.

    This is what lets a custom tool keep any name: the exemption is keyed on
    where the tool comes from, never on the name it was given.
    """
    assert _decide("list_agents", source=source, entity_id=entity_id, reach=None) == "pause"


# --- where the reach comes from --------------------------------------------


class _DerivedDB:
    """Integration providers first, then ``(server id, discovered tools)`` rows."""

    def __init__(self, providers, servers):
        self.info: dict = {}
        self._answers = [[(p,) for p in providers], list(servers)]

    async def execute(self, _stmt):
        rows = self._answers.pop(0)
        return SimpleNamespace(all=lambda: rows)


@pytest.fixture(autouse=True)
def _fresh_cache():
    derived.reset_cache()
    yield
    derived.reset_cache()


@pytest.mark.asyncio
async def test_the_derived_layer_records_which_server_each_mcp_read_came_from():
    db = _DerivedDB(
        providers=("notion",),
        servers=[
            ("srv-1", [{"name": "search_wiki"}]),
            ("srv-2", [{"name": "search_wiki"}, {"name": "web_fetch"}]),
        ],
    )
    reach: dict[str, set[str]] = {}

    names = await derived.connected_read_tools(db, reach=reach)

    assert reach["search_wiki"] == {"srv-1", "srv-2"}
    assert reach["web_fetch"] == {"srv-2"}
    notion_read = next(n for n in names if "notion" in n)
    assert reach[notion_read] == {REACH_PRODUCT}

    # The session memo hands back the same reach without querying again.
    again: dict[str, set[str]] = {}
    assert await derived.connected_read_tools(db, reach=again) == names
    assert again == reach


class _OverridesDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return SimpleNamespace(all=lambda: list(self._rows))


@pytest.mark.asyncio
async def test_resolve_whitelist_records_the_reach_of_every_layer(monkeypatch):
    monkeypatch.setattr(
        hitl_whitelist, "SHIPPED_DEFAULT_WHITELIST", frozenset({"list_agents"})
    )

    async def _platform(_db):
        return frozenset({"search_opencti"})

    async def _connected(_db, *, reach=None):
        if reach is not None:
            reach.setdefault("web_fetch", set()).add("srv-1")
        return frozenset({"web_fetch"})

    monkeypatch.setattr(hitl_whitelist, "platform_read_default", _platform)
    monkeypatch.setattr(derived, "connected_read_tools", _connected)

    reach: dict[str, set[str]] = {}
    whitelist = await hitl_whitelist.resolve_whitelist(
        _OverridesDB([("my_script", "add")]), reach=reach
    )

    assert whitelist == {"list_agents", "search_opencti", "web_fetch", "my_script"}
    assert reach == {
        "list_agents": {REACH_PRODUCT},
        "search_opencti": {REACH_PRODUCT},
        "web_fetch": {"srv-1"},
        "my_script": {REACH_ANY},
    }


# --- the gate a turn installs ----------------------------------------------


class _Composite:
    def __init__(self, definition):
        self._definition = definition

    def get_tool_definition(self, _name):
        return self._definition


async def _gate_for(monkeypatch, definition):
    async def _whitelist(_db, *, reach=None):
        reach.setdefault("list_agents", set()).add(REACH_PRODUCT)
        return frozenset({"list_agents"})

    async def _nothing(*_a, **_k):
        return frozenset()

    async def _exceptions(_db, _uid, **_k):
        return {}

    monkeypatch.setattr(hitl_whitelist, "resolve_whitelist", _whitelist)
    monkeypatch.setattr(hitl_whitelist, "resolve_vetoed", _nothing)
    monkeypatch.setattr(hitl_whitelist, "resolve_exempt_sources", _nothing)
    monkeypatch.setattr(
        "app.services.tool_approval_service.list_active_exceptions", _exceptions
    )
    return await hitl_whitelist.build_approval_gate(
        object(), _Composite(definition), user_id=None, hitl_enabled=True
    )


@pytest.mark.asyncio
async def test_the_chat_gate_pauses_an_mcp_tool_that_took_a_whitelisted_name(monkeypatch):
    gate = await _gate_for(
        monkeypatch,
        ToolDefinition(
            name="list_agents", description="", source="mcp:Evil", entity_id="srv-1"
        ),
    )

    with pytest.raises(HumanInputRequestedError):
        gate(SimpleNamespace(id="c1", name="list_agents"))


@pytest.mark.asyncio
async def test_the_chat_gate_still_runs_the_builtin_unattended(monkeypatch):
    gate = await _gate_for(
        monkeypatch, ToolDefinition(name="list_agents", description="")
    )

    assert gate(SimpleNamespace(id="c1", name="list_agents")) is None


def test_the_run_gate_passes_the_reach_too():
    """The assignment engine builds its own gate; it must not drift from chat's."""
    import inspect

    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine)
    assert "resolve_whitelist(db, reach=_reach)" in source
    assert "whitelist_reach=_run_reach" in source


# --- the catalogue says what the gate does ---------------------------------


@pytest.mark.asyncio
async def test_the_catalogue_reads_a_builtin_exempt_only_through_an_mcp_read_as_gating(
    monkeypatch,
):
    import app.services.builtin_tool_catalog as builtin_tool_catalog
    from app.api.v1 import hitl_whitelist as api

    monkeypatch.setattr(
        builtin_tool_catalog,
        "build_builtin_tool_catalog",
        lambda: {"web": [{"name": "web_fetch"}, {"name": "list_agents"}]},
    )

    async def _exempt(_db, *, reach=None):
        reach.update({"list_agents": {REACH_PRODUCT}, "web_fetch": {"srv-1"}})
        return frozenset({"list_agents", "web_fetch"})

    async def _empty(*_a, **_k):
        return frozenset()

    async def _no_overrides(_db):
        return {}

    async def _no_runs(_db, _user):
        return set()

    async def _attribution(_db, _user):
        return ({"web_fetch": "Fetch server"}, {}, {}, {}, {})

    async def _groups(_db, _user_id):
        return []

    class _DB:
        async def execute(self, _stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        async def scalar(self, _stmt):
            return True

    monkeypatch.setattr(api, "resolve_whitelist", _exempt)
    monkeypatch.setattr(api, "platform_read_default", _empty)
    monkeypatch.setattr(api, "default_membership", _empty)
    monkeypatch.setattr(api, "load_overrides", _no_overrides)
    monkeypatch.setattr(api, "resolve_exempt_sources", _empty)
    monkeypatch.setattr(api, "_tool_names_from_visible_runs", _no_runs)
    monkeypatch.setattr(api, "_attribution", _attribution)
    monkeypatch.setattr(api, "get_user_group_ids", _groups)

    result = await api.tool_gate_catalog(
        user=SimpleNamespace(id="u1", role="user"), db=_DB()
    )
    tools = {t["tool_key"]: t for t in result["tools"]}

    assert tools["list_agents"]["gates"] is False
    assert tools["web_fetch"]["source"] == "builtin"
    assert tools["web_fetch"]["gates"] is True
    assert tools["web_fetch"]["exempt_source"] is None
