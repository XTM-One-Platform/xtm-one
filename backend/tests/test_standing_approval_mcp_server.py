"""A standing approval covers an MCP tool only on the server it was granted on.

Approvals are keyed by ``(user_id, tool_key)``, and an MCP tool's source is
``mcp:<server name>``, a name the server's owner chooses. So an MCP server
advertising the name and schema of a tool the user approved "always" ran on that
approval: a built-in's, or another server's of the same name. The approval now
records the approved tool's ``ToolDefinition.entity_id`` - for an MCP tool, its
server's id - and the gate requires it for an MCP tool.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.tool_approval import ToolApprovalPreference
from app.services import hitl_whitelist
from app.tools.approval import (
    build_action_proposal,
    evaluate_gate,
    fingerprint,
    redact_proposals,
    standing_approval_covers,
)
from app.tools.base import HumanInputRequestedError, ToolDefinition

_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}}
_FP = fingerprint(_SCHEMA)


def _gate(source, *, entity_id="", granted_to, granted_on=None, ids="recorded"):
    return evaluate_gate(
        "search_wiki",
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={"search_wiki": _FP},
        current_fingerprint=_FP,
        tool_source=source,
        tool_entity_id=entity_id,
        exception_sources={"search_wiki": granted_to},
        exception_entity_ids=(
            ({"search_wiki": granted_on} if granted_on else {})
            if ids == "recorded"
            else None
        ),
    )


# --- the decision table ----------------------------------------------------


def test_an_approval_covers_the_mcp_tool_of_the_server_it_was_granted_on():
    decision = _gate(
        "mcp:Wiki", entity_id="srv-1", granted_to="mcp:Wiki", granted_on="srv-1"
    )

    assert decision.action == "execute"
    assert decision.log_outcome == "auto_run_standing_exception"


@pytest.mark.parametrize(
    ("source", "entity_id"),
    [
        # Another server whose owner gave it the same name.
        ("mcp:Wiki", "srv-2"),
        # Another server, another name, same tool name.
        ("mcp:Other", "srv-2"),
        # A tool that reports no server at all.
        ("mcp:Wiki", ""),
    ],
)
def test_an_approval_does_not_cover_another_servers_tool_of_that_name(
    source, entity_id
):
    decision = _gate(
        source, entity_id=entity_id, granted_to="mcp:Wiki", granted_on="srv-1"
    )

    assert decision.action == "pause"
    # Left alone: gating one tool must not revoke what was given to another.
    assert decision.invalidated_preference is False


@pytest.mark.parametrize(
    "granted_to", ["builtin", "integration:notion", "channel_bot:slack", "custom:t9"]
)
def test_an_mcp_tool_does_not_inherit_another_sources_approval(granted_to):
    """The MCP server copied the name and schema of a tool the user approved."""
    decision = _gate("mcp:Evil", entity_id="srv-9", granted_to=granted_to)

    assert decision.action == "pause"
    assert decision.invalidated_preference is False


def test_an_mcp_approval_granted_before_the_server_was_recorded_asks_once_more():
    decision = _gate("mcp:Wiki", entity_id="srv-1", granted_to="mcp:Wiki")

    assert decision.action == "pause"
    assert decision.invalidated_preference is False


def _resource_read(*, entity_id="", granted_to, granted_on=None):
    return evaluate_gate(
        "read_mcp_resource",
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={"read_mcp_resource": _FP},
        current_fingerprint=_FP,
        tool_source="mcp:resources",
        tool_entity_id=entity_id,
        exception_sources={"read_mcp_resource": granted_to},
        exception_entity_ids=(
            {"read_mcp_resource": granted_on} if granted_on else {}
        ),
    ).action


def test_the_products_own_mcp_resource_reader_keeps_its_approval():
    """``read_mcp_resource`` is built by the product and belongs to no server."""
    assert _resource_read(granted_to="mcp:resources") == "execute"


def test_a_server_named_resources_neither_gives_nor_takes_the_readers_approval():
    # A server's tool of that name, on an approval given to the product's reader.
    assert _resource_read(entity_id="srv-1", granted_to="mcp:resources") == "pause"
    # The product's reader, on an approval given to that server's tool.
    assert (
        _resource_read(granted_to="mcp:resources", granted_on="srv-1") == "pause"
    )


def test_a_caller_that_resolved_no_entity_ids_covers_no_mcp_tool():
    assert (
        _gate(
            "mcp:Wiki",
            entity_id="srv-1",
            granted_to="mcp:Wiki",
            granted_on="srv-1",
            ids=None,
        ).action
        == "pause"
    )
    # ...and leaves a product tool as it was.
    assert _gate("builtin", granted_to="builtin", ids=None).action == "execute"


@pytest.mark.parametrize("granted_to", ["mcp:Wiki", "custom:t9"])
def test_a_product_tool_does_not_inherit_an_approval_given_to_a_tool_that_names_itself(
    granted_to,
):
    """Consent given to a discovered or authored tool is not the built-in's."""
    decision = _gate("builtin", granted_to=granted_to, granted_on="srv-1")

    assert decision.action == "pause"
    assert decision.invalidated_preference is False


@pytest.mark.parametrize(
    ("source", "granted_to"),
    [
        ("builtin", "builtin"),
        ("integration:notion", "integration:notion"),
        # A product tool keeps matching by name, as before.
        ("integration:notion", "builtin"),
        # Recorded by a proposal that had no definition.
        ("builtin", "unknown"),
    ],
)
def test_a_product_tool_keeps_matching_by_name(source, granted_to):
    assert _gate(source, granted_to=granted_to).action == "execute"


def test_a_changed_mcp_tool_on_its_own_server_still_invalidates_the_approval():
    decision = evaluate_gate(
        "search_wiki",
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={"search_wiki": _FP},
        current_fingerprint="another-shape",
        tool_source="mcp:Wiki",
        tool_entity_id="srv-1",
        exception_sources={"search_wiki": "mcp:Wiki"},
        exception_entity_ids={"search_wiki": "srv-1"},
    )

    assert decision.action == "pause"
    assert decision.invalidated_preference is True


def test_standing_approval_covers_compares_server_ids_as_strings():
    server = uuid.uuid4()

    assert standing_approval_covers(
        "mcp:Wiki", "mcp:Wiki", tool_entity_id=str(server), granted_entity_id=server
    )


# --- the proposal carries the server ---------------------------------------


def test_the_proposal_carries_the_tools_entity_id_and_redaction_drops_it():
    proposal = build_action_proposal(
        tool_call_id="c1",
        tool_name="search_wiki",
        arguments={"query": "x"},
        input_schema=_SCHEMA,
        source="mcp:Wiki",
        entity_id="srv-1",
    )

    assert proposal["tool_entity_id"] == "srv-1"
    [redacted] = redact_proposals([proposal])
    assert "tool_entity_id" not in redacted
    assert redacted["source"] == "mcp:Wiki"


def test_a_proposal_without_an_entity_records_none():
    proposal = build_action_proposal(
        tool_call_id="c1", tool_name="list_agents", arguments={}
    )

    assert proposal["tool_entity_id"] is None


def test_screening_a_batch_puts_the_server_id_in_the_proposal():
    from app.tools.base import (
        CallableToolExecutor,
        CompositeToolExecutor,
        ToolCall,
    )

    async def _handler(**_kwargs):
        return "ran"

    callable_exec = CallableToolExecutor()
    callable_exec.register("search_wiki", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="search_wiki",
                description="",
                input_schema=_SCHEMA,
                source="mcp:Wiki",
                entity_id="srv-1",
            )
        ],
        callable_exec,
    )

    def _always_pause(call):
        raise HumanInputRequestedError(
            question="?", tool_call_id=call.id, conversation=[]
        )

    composite.set_approval_gate(_always_pause)

    [proposal] = composite.screen_for_approval(
        [ToolCall(id="c1", name="search_wiki", arguments={})]
    )

    assert proposal["tool_entity_id"] == "srv-1"
    assert proposal["source"] == "mcp:Wiki"


# --- "Yes, always" records the server --------------------------------------


class _FakeDB:
    """Records added rows; the first ``scalar`` is the existing-row lookup."""

    def __init__(self, existing=None):
        self.added = []
        self._existing = existing
        self._scalars = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def scalar(self, _stmt):
        self._scalars += 1
        if self._scalars == 1:
            return self._existing
        return SimpleNamespace(id=uuid.uuid4(), email="reviewer@example.com")


@pytest.mark.asyncio
async def test_approve_always_on_a_run_records_the_server_id():
    from app.api.v1.assignments import _grant_standing_approvals
    from app.api.v1.schemas.assignment import ApprovalDecision

    proposal = build_action_proposal(
        tool_call_id="c1",
        tool_name="search_wiki",
        arguments={},
        input_schema=_SCHEMA,
        source="mcp:Wiki",
        entity_id="srv-1",
    )
    db = _FakeDB()
    run = SimpleNamespace(pending_approvals=[proposal], user_id=uuid.uuid4())

    await _grant_standing_approvals(
        db,
        run,
        [ApprovalDecision(tool_call_id="c1", decision="approve_always")],
        SimpleNamespace(id=run.user_id),
    )

    [pref] = [o for o in db.added if isinstance(o, ToolApprovalPreference)]
    assert pref.tool_source == "mcp:Wiki"
    assert pref.tool_entity_id == "srv-1"


@pytest.mark.asyncio
async def test_regranting_the_name_on_another_server_moves_the_approval_there():
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tool_key="search_wiki",
        schema_fingerprint=_FP,
        tool_source="mcp:Wiki",
        tool_entity_id="srv-1",
        granted_by_admin_id=None,
    )
    from app.services.tool_approval_service import grant_exception

    await grant_exception(
        _FakeDB(existing=existing),
        user_id=existing.user_id,
        tool_key="search_wiki",
        tool_source="mcp:Wiki",
        schema_fingerprint=_FP,
        tool_entity_id="srv-2",
    )

    assert existing.tool_entity_id == "srv-2"


@pytest.mark.asyncio
async def test_list_active_exceptions_reports_the_server_each_was_granted_on():
    from app.services.tool_approval_service import list_active_exceptions

    class _DB:
        async def execute(self, _stmt):
            return SimpleNamespace(
                all=lambda: [
                    ("search_wiki", "fp-1", "mcp:Wiki", "srv-1"),
                    ("list_agents", "fp-2", "builtin", None),
                ]
            )

    sources: dict[str, str] = {}
    entity_ids: dict[str, str] = {}
    result = await list_active_exceptions(
        _DB(), uuid.uuid4(), sources=sources, entity_ids=entity_ids
    )

    assert result == {"search_wiki": "fp-1", "list_agents": "fp-2"}
    assert sources == {"search_wiki": "mcp:Wiki", "list_agents": "builtin"}
    assert entity_ids == {"search_wiki": "srv-1"}


# --- the chat gate reads the server ----------------------------------------


class _Composite:
    def __init__(self, definition):
        self._definition = definition

    def get_tool_definition(self, _name):
        return self._definition


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("granted_on", "outcome"), [("srv-1", "execute"), ("srv-2", "pause")]
)
async def test_the_chat_gate_reads_the_server_an_approval_was_granted_on(
    monkeypatch, granted_on, outcome
):
    async def _nothing(*_a, **_k):
        return frozenset()

    async def _whitelist(_db, *, reach=None):
        return frozenset()

    async def _exceptions(_db, _uid, *, sources=None, entity_ids=None):
        sources["search_wiki"] = "mcp:Wiki"
        entity_ids["search_wiki"] = granted_on
        return {"search_wiki": _FP}

    monkeypatch.setattr(hitl_whitelist, "resolve_whitelist", _whitelist)
    monkeypatch.setattr(hitl_whitelist, "resolve_vetoed", _nothing)
    monkeypatch.setattr(hitl_whitelist, "resolve_exempt_sources", _nothing)
    monkeypatch.setattr(hitl_whitelist, "resolve_gate_suppression", _nothing)
    monkeypatch.setattr(
        "app.services.tool_approval_service.list_active_exceptions", _exceptions
    )
    definition = ToolDefinition(
        name="search_wiki",
        description="",
        input_schema=_SCHEMA,
        source="mcp:Wiki",
        entity_id="srv-1",
    )
    gate = await hitl_whitelist.build_approval_gate(
        object(), _Composite(definition), user_id=uuid.uuid4(), hitl_enabled=True
    )

    call = SimpleNamespace(id="c1", name="search_wiki")
    if outcome == "pause":
        with pytest.raises(HumanInputRequestedError):
            gate(call)
    else:
        assert gate(call) is None


# --- a grant from the tools list records the server ------------------------


@pytest.mark.asyncio
async def test_a_grant_from_the_tools_list_records_the_mcp_server_id(monkeypatch):
    from sqlalchemy import select

    from app.api.v1 import helpers
    from app.api.v1 import tool_approvals as api
    from app.models.mcp_server import MCPServer

    server_id = uuid.uuid4()
    server = SimpleNamespace(
        id=server_id,
        name="Wiki",
        discovered_tools=[{"name": "search_wiki", "input_schema": _SCHEMA}],
    )

    async def _groups(*_a, **_k):
        return set()

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(
        helpers, "user_visible_query", lambda model, *_a, **_k: select(model)
    )
    # No built-in, custom tool or provider claims the name.
    monkeypatch.setattr(
        "app.services.builtin_tool_catalog.build_builtin_tool_catalog", dict
    )
    monkeypatch.setattr("app.services.integration_tools.PROVIDER_TOOLS", {})
    monkeypatch.setattr("app.services.integration_tools.CHANNEL_BOT_TOOLS", {})

    class _DB:
        async def execute(self, stmt):
            if stmt.column_descriptions[0].get("entity") is MCPServer:
                return SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: [server])
                )
            return SimpleNamespace(first=lambda: None)

    schema, source, entity_id = await api._resolve_schema(
        _DB(), "search_wiki", user_id=uuid.uuid4(), is_admin=False
    )

    assert schema == _SCHEMA
    assert source == "mcp:Wiki"
    assert entity_id == str(server_id)
