"""``to_workset`` in ``CompositeToolExecutor``: which tools carry it, and what a call with it does.

While the workset executor is on the composite (``on_composite``), every tool
that is not a write by its name gains the argument. A call that carries it
runs the tool without it, hands the full result (before the loop's size cap)
to the ingestor, and returns the ingestor's receipt instead of the result.
"""

from __future__ import annotations

import pytest

from app.tools.base import (
    WORKSET_INGEST_ARGUMENT,
    WORKSPACE_PATH_ARGUMENT,
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolResult,
    offers_workset_ingest,
)


def _definition(name: str, **properties) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "properties": properties or {"page": {"type": "integer"}}},
        source="test",
    )


class _Ingestor:
    def __init__(self) -> None:
        self.calls: list[tuple[ToolCall, ToolResult, object]] = []

    async def ingest(
        self, *, call: ToolCall, result: ToolResult, spec: object, personal_records: bool = False
    ) -> ToolResult:
        self.calls.append((call, result, spec))
        self.personal_records = personal_records
        return ToolResult(tool_call_id=result.tool_call_id, content="RECEIPT")


class _IngestingExecutor(CallableToolExecutor):
    """What the workset executor does on the composite."""

    def __init__(self, ingestor: _Ingestor) -> None:
        super().__init__()
        self.ingestor = ingestor

    def on_composite(self, composite) -> None:
        composite.workset_ingestor = self.ingestor


def _composite(*definitions: ToolDefinition, result: str = "FULL RESULT", is_error: bool = False):
    received: list[dict] = []
    source = CallableToolExecutor()

    for definition in definitions:

        async def _handler(_name=definition.name, **arguments):
            received.append({"name": _name, **arguments})
            return ToolResult(tool_call_id="", content=result, is_error=is_error)

        source.register(definition.name, _handler)
    composite = CompositeToolExecutor()
    composite.add_tools(list(definitions), source)
    ingestor = _Ingestor()
    composite.add_tools([_definition("workset_list_items")], _IngestingExecutor(ingestor))
    return composite, ingestor, received


@pytest.mark.parametrize(
    ("name", "offered"),
    [
        ("search_cases", True),
        ("get_opencti_entity", True),
        ("narratives", True),  # an unknown verb is a write only to the fail-safe fallback
        ("cases", True),
        ("create_report", False),
        ("send_email", False),
        ("get_or_create_entity", False),
        ("github_graphql_query", False),  # a write override
        ("render_chart", True),  # a read override
        ("workset_search", False),  # the workset's own tools
    ],
)
def test_offered_to_every_tool_that_is_not_a_write_by_its_name(name, offered):
    assert offers_workset_ingest(_definition(name)) is offered


def test_the_argument_appears_only_while_a_workset_is_on_the_composite():
    composite = CompositeToolExecutor()
    composite.add_tools([_definition("search_cases"), _definition("create_report")], CallableToolExecutor())
    assert [WORKSET_INGEST_ARGUMENT in (d.input_schema["properties"]) for d in composite.all_tools] == [False, False]

    composite.add_tools([_definition("workset_list_items")], _IngestingExecutor(_Ingestor()))
    schemas = {d.name: d.input_schema["properties"] for d in composite.all_tools}
    assert set(schemas["search_cases"]) == {"page", WORKSET_INGEST_ARGUMENT}
    assert set(schemas["create_report"]) == {"page"}
    assert set(schemas["workset_list_items"]) == {"page"}
    # The registered definition is left as it was.
    assert set(composite.get_tool_definition("search_cases").input_schema["properties"]) == {"page"}


def test_a_replaced_tool_is_offered_with_its_new_schema():
    composite, _, _ = _composite(_definition("search_cases"))
    assert WORKSET_INGEST_ARGUMENT in composite.all_tools[0].input_schema["properties"]
    composite.add_tools([_definition("search_cases", query={"type": "string"})], CallableToolExecutor())
    offered = next(d for d in composite.all_tools if d.name == "search_cases")
    assert set(offered.input_schema["properties"]) == {"query", WORKSET_INGEST_ARGUMENT}


async def test_the_tool_runs_without_the_argument_and_the_model_gets_the_receipt():
    big = "x" * 400_000  # over the loop's cap: the ingestor must see all of it
    composite, ingestor, received = _composite(_definition("search_cases"), result=big)
    spec = {"item_start": "^## "}
    result = await composite.execute_tool(
        ToolCall(id="c1", name="search_cases", arguments={"page": 2, WORKSET_INGEST_ARGUMENT: spec})
    )
    assert received == [{"name": "search_cases", "page": 2}]
    assert len(ingestor.calls) == 1
    call, full, given = ingestor.calls[0]
    assert call.arguments == {"page": 2}
    assert full.content == big
    assert given == spec
    assert result.content == "RECEIPT"


async def test_a_failed_call_is_returned_as_it_is_and_nothing_is_ingested():
    composite, ingestor, _ = _composite(_definition("search_cases"), result="Error: upstream 502", is_error=True)
    result = await composite.execute_tool(
        ToolCall(id="c1", name="search_cases", arguments={WORKSET_INGEST_ARGUMENT: {}})
    )
    assert (result.content, result.is_error) == ("Error: upstream 502", True)
    assert ingestor.calls == []


@pytest.mark.parametrize("spec", [None, False])
async def test_an_empty_spec_is_a_plain_call(spec):
    composite, ingestor, received = _composite(_definition("search_cases"))
    result = await composite.execute_tool(
        ToolCall(id="c1", name="search_cases", arguments={"page": 1, WORKSET_INGEST_ARGUMENT: spec})
    )
    assert received == [{"name": "search_cases", "page": 1}]
    assert ingestor.calls == []
    assert result.content == "FULL RESULT"


async def test_a_write_given_the_argument_runs_as_a_plain_call():
    composite, ingestor, received = _composite(_definition("create_report"))
    result = await composite.execute_tool(
        ToolCall(id="c1", name="create_report", arguments={"page": 1, WORKSET_INGEST_ARGUMENT: {}})
    )
    assert received == [{"name": "create_report", "page": 1}]
    assert ingestor.calls == []
    assert result.content == "FULL RESULT"


async def test_with_a_workspace_path_too_nothing_runs():
    composite, ingestor, received = _composite(_definition("search_cases"))
    result = await composite.execute_tool(
        ToolCall(
            id="c1",
            name="search_cases",
            arguments={WORKSET_INGEST_ARGUMENT: {}, WORKSPACE_PATH_ARGUMENT: "out.json"},
        )
    )
    assert result.is_error is True
    assert result.content == (
        f"Error: give either {WORKSET_INGEST_ARGUMENT} or {WORKSPACE_PATH_ARGUMENT}, not both. Nothing was run."
    )
    assert received == []
    assert ingestor.calls == []


async def test_without_a_workset_the_argument_is_left_to_the_tool():
    """No ingestor, no special meaning: the composite does not strip what it never offered."""
    received: list[dict] = []
    source = CallableToolExecutor()

    async def _handler(**arguments):
        received.append(arguments)
        return ToolResult(tool_call_id="", content="ok")

    source.register("search_cases", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([_definition("search_cases")], source)
    await composite.execute_tool(ToolCall(id="c1", name="search_cases", arguments={WORKSET_INGEST_ARGUMENT: {}}))
    assert received == [{WORKSET_INGEST_ARGUMENT: {}}]


def test_each_read_tool_carries_a_short_schema_and_the_guide_is_said_once():
    """Every read tool of the agent carries the argument on every turn: its
    schema stays short, and how to fill it is on ``workset_add_item`` alone."""
    import json

    from app.services.builtin_tools import build_workset_tools
    from app.tools.base import WORKSET_INGEST_GUIDE, WORKSET_INGEST_SCHEMA

    assert len(json.dumps(WORKSET_INGEST_SCHEMA)) <= 600
    definitions, _ = build_workset_tools(None, "u", None)
    carrying = [d.name for d in definitions if WORKSET_INGEST_GUIDE in d.description]
    assert carrying == ["workset_add_item"]
    from app.services.workset_split import SplitSpec

    # The schema offers exactly the keys the splitter accepts.
    assert set(WORKSET_INGEST_SCHEMA["properties"]) == set(SplitSpec.__dataclass_fields__)


# ── One access rule: to_workset is the workset's write ──────────────────────


def _gate_pausing(name: str):
    from app.tools.base import HumanInputRequestedError

    seen: list[str] = []

    def _gate(call: ToolCall) -> None:
        seen.append(call.name)
        if call.name == name:
            raise HumanInputRequestedError(
                question=f"Approval required to run {call.name}", tool_call_id=call.id, conversation=[]
            )

    return _gate, seen


async def test_a_read_with_to_workset_pauses_where_the_workset_write_would():
    from app.tools.base import HumanInputRequestedError

    composite, ingestor, received = _composite(_definition("search_cases"))
    gate, seen = _gate_pausing("workset_add_item")
    composite.set_approval_gate(gate)

    plain = await composite.execute_tool(ToolCall(id="c0", name="search_cases", arguments={"page": 1}))
    assert plain.content == "FULL RESULT"

    with pytest.raises(HumanInputRequestedError):
        await composite.execute_tool(
            ToolCall(id="c1", name="search_cases", arguments={"page": 1, WORKSET_INGEST_ARGUMENT: {}})
        )
    assert seen == ["search_cases", "search_cases", "workset_add_item"]
    assert received == [{"name": "search_cases", "page": 1}]  # only the plain call ran
    assert ingestor.calls == []

    proposals = composite.screen_for_approval(
        [ToolCall(id="c2", name="search_cases", arguments={WORKSET_INGEST_ARGUMENT: {}})]
    )
    assert [(p["tool_call_id"], p["tool_name"]) for p in proposals] == [("c2", "search_cases")]

    # Approved by the person: it runs and ingests.
    composite.bypass_approval_for({"c1"})
    result = await composite.execute_tool(
        ToolCall(id="c1", name="search_cases", arguments={"page": 1, WORKSET_INGEST_ARGUMENT: {}})
    )
    assert result.content == "RECEIPT"
    assert len(ingestor.calls) == 1


async def test_without_the_workset_write_on_the_surface_to_workset_is_refused():
    """A deny list or a scope removed ``workset_add_item``: no writing through a read either."""
    from app.tools.base import offering_tools

    composite, ingestor, received = _composite(_definition("search_cases"))
    surface = [d for d in composite.all_tools if d.name != "workset_add_item"]
    with offering_tools(surface):
        result = await composite.execute_tool(
            ToolCall(id="c1", name="search_cases", arguments={WORKSET_INGEST_ARGUMENT: {}})
        )
    assert result.is_error is True
    assert result.content == (
        "Error: to_workset is not available to you here: storing into the workset is not allowed. "
        "Call search_cases without it. Nothing was run."
    )
    assert received == []
    assert ingestor.calls == []


def test_the_argument_is_withdrawn_from_a_surface_without_the_workset_write():
    from app.services.builtin_tools import apply_tool_deny_list, build_workset_tools
    from app.tools.base import fit_descriptions_to_surface

    composite = CompositeToolExecutor()
    composite.add_tools([_definition("search_cases")], CallableToolExecutor())
    definitions, executor = build_workset_tools(None, "u", None)
    composite.add_tools(definitions, executor)

    kept = fit_descriptions_to_surface(composite.all_tools)
    assert WORKSET_INGEST_ARGUMENT in next(d for d in kept if d.name == "search_cases").input_schema["properties"]

    denied = fit_descriptions_to_surface(apply_tool_deny_list(composite.all_tools, ["workset_add_item"]))
    assert {d.name for d in denied if WORKSET_INGEST_ARGUMENT in (d.input_schema.get("properties") or {})} == set()
    assert set(next(d for d in denied if d.name == "search_cases").input_schema["properties"]) == {"page"}
