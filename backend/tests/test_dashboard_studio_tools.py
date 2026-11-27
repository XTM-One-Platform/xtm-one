"""The Dashboard Studio's discovery tools: what an agent reads before writing a widget.

Every platform widget type is described with an example configuration the
platform's own checks accept; the visualization catalog is read from the
options models, so it never drifts from what a save accepts; a source is read
before it is saved, with what to do when it reads nothing.
"""

from __future__ import annotations

import json

import pytest

from app.services.dashboard_definition import DATA_TYPES, visualization_catalog
from app.services.dashboard_platform import PLATFORM_SOURCES, NativeError, check_native, describe_widget
from app.services.dashboard_visualization_options import OPTIONS_BY_TYPE, option_names


def _all_types() -> list[tuple[str, str]]:
    return [(s, t["type"]) for s in PLATFORM_SOURCES for t in describe_widget(s)["types"]]


@pytest.mark.parametrize(("source", "widget_type"), _all_types(), ids=[f"{s}-{t}" for s, t in _all_types()])
def test_every_widget_type_has_an_example_its_platform_accepts(source, widget_type):
    described = describe_widget(source, widget_type)
    assert described["source"] == source and described["type"] == widget_type
    check_native(source, described["example"])
    assert described["rules"]


def test_an_unknown_platform_or_type_is_refused_with_what_exists():
    with pytest.raises(NativeError, match="source must be one of"):
        describe_widget("elsewhere")
    with pytest.raises(NativeError, match="vertical-bar"):
        describe_widget(PLATFORM_SOURCES[0], "no-such-type")


def test_the_catalog_is_every_data_visualization_with_the_shapes_it_draws():
    catalog = {v["type"]: v for v in visualization_catalog()}
    assert set(catalog) == set(DATA_TYPES) - {"xtm.platformlink"}
    assert all(v["shapes"] for v in catalog.values())
    assert "xtm.singlevalue" in {v["type"] for v in visualization_catalog("number")}
    assert "xtm.singlevalue" not in {v["type"] for v in visualization_catalog("series")}
    # The options are the model's, as stored (camelCase), with the values of a closed list.
    names = {o["name"]: o for o in option_names("xtm.column")}
    assert "stackMode" in names and names["stackMode"]["values"] == ["auto", "stacked", "stacked100"]
    assert {o["name"] for o in option_names("xtm.column")} == {
        f.alias or n for n, f in OPTIONS_BY_TYPE["xtm.column"].model_fields.items()
    }


@pytest.fixture
def studio_tools(dashboard_studio_enabled):
    from app.services.builtin_tools import build_custom_dashboard_tools
    from app.tools.base import ToolCall

    definitions, executor = build_custom_dashboard_tools(None, "11111111-1111-4111-8111-111111111111")

    async def call(name: str, arguments: dict):
        return await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))

    return {d.name for d in definitions}, call


def test_the_studio_offers_the_discovery_tools(studio_tools):
    names, _call = studio_tools
    assert {"dashboard_catalog", "describe_platform_widget", "lookup_platform_values", "preview_source", "add_widget", "check_dashboard"} <= names


async def test_the_catalog_and_the_description_answer_the_agent(studio_tools):
    _names, call = studio_tools
    listing = await call("dashboard_catalog", {"shape": "distribution"})
    assert not listing.is_error and "- xtm.pie: distribution" in listing.content
    options = await call("dashboard_catalog", {"type": "xtm.pie"})
    assert "showDonutHole" in options.content
    described = await call("describe_platform_widget", {"source": PLATFORM_SOURCES[0], "widget_type": "line"})
    payload = json.loads(described.content.split("\n", 1)[0])
    assert payload["example"]["type"] == "line" and "lookup_platform_values" in described.content
    refused = await call("lookup_platform_values", {"integration_id": "x", "kind": "secrets"})
    assert refused.is_error and "kind must be one of" in refused.content


async def test_a_source_is_read_before_it_is_saved(studio_tools):
    _names, call = studio_tools
    number = await call("preview_source", {"source": {"type": "static", "options": {"shape": "number", "data": {"value": 42}}}})
    assert not number.is_error and number.content.startswith("The source reads ok (number)")
    assert "42" in number.content
    empty = await call("preview_source", {"source": {"type": "static", "options": {"shape": "rows", "data": {"columns": ["a"], "rows": []}}}})
    assert "empty (rows)" in empty.content
    invalid = await call("preview_source", {"source": {"type": "static", "options": {"shape": "nothing"}}})
    assert invalid.is_error and "invalid at" in invalid.content


async def test_a_step_written_wrong_is_answered_with_every_step_s_form(studio_tools):
    _names, call = studio_tools
    wrong = await call(
        "preview_source",
        {"source": {"type": "derived", "options": {"extends": "parent", "steps": [{"op": "where", "eq": "critical"}]}}},
    )
    assert wrong.is_error and "steps" in wrong.content
    assert "{op: where, field, operator?: eq|ne|gt|gte|lt|lte|contains|in, value?}" in wrong.content
    assert "{op: stats, agg: count|sum|avg|min|max|last, field?, by?}" in wrong.content


def test_the_source_and_alert_forms_are_in_the_preview_description(dashboard_studio_enabled):
    from app.services.builtin_tools import build_custom_dashboard_tools

    definitions, _executor = build_custom_dashboard_tools(None, "11111111-1111-4111-8111-111111111111")
    preview = next(d for d in definitions if d.name == "preview_source").description
    assert "A source is {type, name?, options}" in preview
    assert "{op: gt|gte|lt|lte|eq|ne, id, value, label?, severity?: info|warning|critical}" in preview


async def test_the_data_the_user_reads_is_found_with_its_ids(studio_tools, monkeypatch):
    import uuid
    from types import SimpleNamespace

    from app.services import dashboard_sources

    kb = SimpleNamespace(id=uuid.UUID("33333333-3333-4333-8333-333333333333"), name="Findings")
    doc = SimpleNamespace(id=uuid.UUID("44444444-4444-4444-8444-444444444444"), filename="findings.csv", source_type="csv", status="ready")
    integration = SimpleNamespace(id=uuid.UUID("55555555-5555-4555-8555-555555555555"), name="Ransomware.live", provider="ransomware_live")
    tool = SimpleNamespace(name="list_recent_victims", description="Recent victims.", input_schema={"properties": {"limit": {"type": "integer"}}})

    async def _documents(_db, _user, knowledge_base_id=None):
        return [(kb, doc)]

    async def _integrations(_db, _user):
        return [integration]

    async def _tools(_db, _user, integration_id):
        assert str(integration_id) == str(integration.id)
        return [tool]

    monkeypatch.setattr(dashboard_sources, "readable_documents", _documents)
    monkeypatch.setattr(dashboard_sources, "readable_integrations", _integrations)
    monkeypatch.setattr(dashboard_sources, "read_tools", _tools)
    _names, call = studio_tools
    knowledge = await call("find_dashboard_data", {"kind": "knowledge", "search": "finding"})
    assert f"knowledge_base_id {kb.id} (Findings), document_id {doc.id}" in knowledge.content
    integrations = await call("find_dashboard_data", {"kind": "integrations"})
    assert f"Ransomware.live (ransomware_live): integration_id {integration.id}" in integrations.content
    tools = await call("find_dashboard_data", {"kind": "integrations", "integration_id": str(integration.id)})
    assert "- list_recent_victims: Recent victims." in tools.content and '"limit"' in tools.content
    nothing = await call("find_dashboard_data", {"kind": "knowledge", "search": "nope"})
    assert "Nothing matches" in nothing.content


async def test_a_step_that_does_not_apply_says_why_to_the_agent():
    """The pipeline's own reason reaches the agent writing the step, not only the log."""
    from app.services.builtin_tools.custom_dashboard_studio import describe_read

    line = describe_read({"status": "error", "code": "invalid_result", "detail": "eval: '>' is not allowed"})
    assert line.startswith("error invalid_result (") and "eval: '>' is not allowed" in line
    # What an integration said is data in a quarantine block, never instructions.
    assert "UNTRUSTED" in line
    source = {
        "type": "static",
        "options": {"shape": "rows", "data": {"columns": [{"key": "n"}], "rows": [{"n": 1}]}},
    }
    from app.services import dashboard_sources

    definition = {"dataSources": {"base": source}, "inputs": {}}
    read = await dashboard_sources.read_source(
        None, None, {**definition, "dataSources": {**definition["dataSources"], "d": {"type": "derived", "options": {
            "extends": "base", "steps": [{"op": "eval", "field": "big", "expression": "n > 0"}]}}}}, "d", None,
    )  # fmt: skip
    assert read["code"] == "invalid_result" and "'>' is not allowed" in read["detail"]


async def test_a_derived_source_is_previewed_on_a_parent_not_saved_yet(studio_tools):
    _names, call = studio_tools
    parent = {"type": "static", "options": {"shape": "rows", "data": {"columns": [{"key": "n"}], "rows": [{"n": 2}, {"n": 3}]}}}
    derived = {"type": "derived", "options": {"extends": "base", "steps": [{"op": "stats", "agg": "sum", "field": "n"}]}}
    read = await call("preview_source", {"source": derived, "sources": {"base": parent}})
    assert not read.is_error and read.content.startswith("The source reads ok (number)") and "5" in read.content
    refused = await call("preview_source", {"source": derived, "sources": {"base": {"type": "static"}}})
    assert refused.is_error and "sources.base is invalid" in refused.content


async def test_previews_without_a_widget_are_paced(studio_tools):
    from app.services.builtin_tools.custom_dashboard_studio import PREVIEW_LIMIT, PREVIEWS_BEFORE_A_WIDGET

    _names, call = studio_tools
    source = {"type": "static", "options": {"shape": "number", "data": {"value": 1}}}
    answers = [await call("preview_source", {"source": source}) for _ in range(PREVIEWS_BEFORE_A_WIDGET)]
    assert all("no widget added" not in a.content for a in answers[:-1])
    assert f"{PREVIEWS_BEFORE_A_WIDGET} sources previewed and no widget added since" in answers[-1].content
    more = [await call("preview_source", {"source": source}) for _ in range(PREVIEW_LIMIT - PREVIEWS_BEFORE_A_WIDGET + 1)]
    assert not any(a.is_error for a in more[:-1])
    assert more[-1].is_error and "no more previews until one is" in more[-1].content


def test_a_platform_part_the_platform_does_not_read_is_told():
    """An entities widget given a relationship filter (dynamicFrom) read every technique of the platform, not the
    94 of one intrusion set: the read looked right. It is kept as written, and told."""
    from app.services.builtin_tools.custom_dashboard_studio import unread_parts

    uses = {"mode": "and", "filters": [{"key": ["fromId"], "values": ["x"], "operator": "eq", "mode": "or"}], "filterGroups": []}

    def source(perspective: str) -> dict:
        selection = {"perspective": perspective, "attribute": "kill-chain-phase.internal_id", "dynamicFrom": uses}
        native = {"type": "horizontal-bar", "perspective": perspective, "dataSelection": [selection]}
        return {"type": "platform", "options": {"source": "opencti", "integration_id": "i", "native": native}}

    told = unread_parts(source("entities"))
    assert "dataSelection[0].dynamicFrom: only relationships read it" in told and "regardingOf" in told
    assert unread_parts(source("relationships")) == ""
    assert unread_parts({"type": "static", "options": {}}) == ""


async def test_a_table_in_a_tile_narrower_than_its_columns_is_told():
    """A nine-column relationship list placed in 4 of the 12 grid columns read fine and drew nothing readable."""
    import uuid
    from types import SimpleNamespace

    from app.services.builtin_tools import custom_dashboard_studio
    from app.tools.base import ToolResult

    async def _edit(**_kwargs):
        return ToolResult(tool_call_id="x", content="ok")

    _definitions, handlers = custom_dashboard_studio.build_dashboard_studio_tools(
        user_id=str(uuid.uuid4()), conversation_id=None, assignment_run_id=None, edit=_edit, source="test"
    )
    check = handlers["check_dashboard"]
    rows = {"columns": [{"key": f"c{i}", "label": f"C{i}"} for i in range(9)], "rows": [{"c0": 1}]}
    definition = {
        "title": "T",
        "dataSources": {"wide": {"type": "static"}, "narrow": {"type": "static"}},
        "visualizations": {
            "cramped": {"type": "xtm.list", "dataSources": {"primary": "wide"}},
            "roomy": {"type": "xtm.table", "dataSources": {"primary": "narrow"}},
        },
        "layout": {"layoutDefinitions": {"m": {"structure": [
            {"item": "cramped", "position": {"x": 0, "y": 0, "w": 4, "h": 4}},
            {"item": "roomy", "position": {"x": 0, "y": 4, "w": 12, "h": 4}},
        ]}}},
    }  # fmt: skip

    async def _working(_dashboard_id, *, need="read"):
        return SimpleNamespace(revision=3), definition

    async def _read(_definition, _source_id, _range=None):
        return {"status": "ok", "shape": "rows", "data": rows}

    closure = dict(zip(check.__code__.co_freevars, check.__closure__ or (), strict=True))
    closure["_working"].cell_contents = _working
    closure["_read"].cell_contents = _read
    answer = await check(dashboard_id=str(uuid.uuid4()))
    assert "2 visualizations draw data, 1 need attention" in answer.content
    assert "- cramped [xtm.list <- wide]: ok (rows, 9 columns, 1 rows); cramped: 9 columns in 4 grid columns" in answer.content
    assert "roomy [xtm.table <- narrow]: ok (rows, 9 columns, 1 rows)\n" in answer.content + "\n"

    # A platform source its platform reads only in part, checked at the end: told there too, not only on a preview.
    uses = {"mode": "and", "filters": [{"key": ["fromId"], "values": ["x"], "operator": "eq", "mode": "or"}], "filterGroups": []}
    native = {"type": "horizontal-bar", "perspective": "entities", "dataSelection": [{"perspective": "entities", "dynamicFrom": uses}]}
    definition["dataSources"]["narrow"] = {"type": "platform", "options": {"source": "opencti", "integration_id": "i", "native": native}}
    answer = await check(dashboard_id=str(uuid.uuid4()))
    assert "2 need attention" in answer.content
    assert "roomy [xtm.table <- narrow]: ok (rows, 9 columns, 1 rows); misread: The platform does not read: dataSelection[0].dynamicFrom" in answer.content


def test_a_dropdown_whose_rows_give_one_value_is_told():
    """A dropdown of 189 intrusion sets read from a list offered one choice: the list's first column, its type."""
    from app.services.builtin_tools.custom_dashboard_studio import describe_choices

    rows = {
        "columns": [{"key": "entity_type"}, {"key": "name"}],
        "rows": [{"entity_type": "Intrusion-Set", "name": f"APT{i}", "id": f"id-{i}"} for i in range(189)],
    }
    told = describe_choices({}, rows)
    assert told.startswith("1 choices from 189 rows (value: entity_type); collapsed:")
    assert "entity_type, name, id" in told and "input.entity" in told
    assert "collapsed" not in describe_choices({"valueField": "id"}, rows)
    assert describe_choices({}, {"items": [{"label": "a"}, {"label": "b"}]}) == "2 choices from 2 rows (value: label)"


async def test_the_studio_reads_a_source_with_the_inputs_at_their_defaults(monkeypatch):
    """A source filtered on an intrusion set picker answered "waiting" to every check: the tools read it with
    no input set, where a viewer opening the page has the picker's default."""
    import uuid
    from contextlib import asynccontextmanager

    import app.core.database as database
    from app.services import dashboard_sources
    from app.services.builtin_tools import custom_dashboard_studio
    from app.services.dashboard_tokens import default_tokens
    from app.tools.base import ToolResult

    definition = {
        "dataSources": {"s": {"type": "static", "options": {"shape": "number", "data": {"value": 1}}}},
        "inputs": {
            "pick": {"type": "input.entity", "token": "actor", "defaultValue": "intrusion-set--1"},
            "range": {"type": "input.timerange", "token": "time", "defaultValue": "30d"},
            "free": {"type": "input.text", "token": "q", "defaultValue": ""},
            "button": {"type": "input.button"},
        },
    }
    assert default_tokens(definition) == {"actor": "intrusion-set--1", "time": "30d", "q": ""}

    seen: list[dict] = []

    async def _read_source(_db, _user, _definition, _sid, _range=None, **kwargs):
        seen.append(kwargs.get("tokens"))
        return {"status": "ok", "shape": "number", "data": {"value": 1}}

    @asynccontextmanager
    async def _session():
        yield None

    async def _edit(**_kwargs):
        return ToolResult(tool_call_id="x", content="ok")

    monkeypatch.setattr(dashboard_sources, "read_source", _read_source)
    monkeypatch.setattr(database, "async_session", _session)
    _definitions, handlers = custom_dashboard_studio.build_dashboard_studio_tools(
        user_id=str(uuid.uuid4()), conversation_id=None, assignment_run_id=None, edit=_edit, source="test"
    )
    preview = handlers["preview_source"]
    closure = dict(zip(preview.__code__.co_freevars, preview.__closure__ or (), strict=True))
    await closure["_read"].cell_contents(definition, "s")
    assert seen == [{"actor": "intrusion-set--1", "time": "30d", "q": ""}]


async def test_a_capture_that_fails_says_why():
    """"The headless browser is unavailable" was said of every failure: the agent waited for a browser that was up,
    while the page had not finished rendering."""
    import uuid
    from types import SimpleNamespace

    from app.services.builtin_tools import custom_dashboard_studio
    from app.tools.base import ToolResult

    async def _edit(**_kwargs):
        return ToolResult(tool_call_id="x", content="ok")

    _definitions, handlers = custom_dashboard_studio.build_dashboard_studio_tools(
        user_id=str(uuid.uuid4()), conversation_id=None, assignment_run_id=None, edit=_edit, source="test"
    )
    check = handlers["check_dashboard"]
    closure = dict(zip(check.__code__.co_freevars, check.__closure__ or (), strict=True))

    async def _working(_dashboard_id, *, need="read"):
        return SimpleNamespace(revision=1), {"title": "T", "dataSources": {}, "visualizations": {}, "layout": {"layoutDefinitions": {}}}

    async def _capture(*_a):
        raise RuntimeError("The dashboard did not finish rendering in time.")

    closure["_working"].cell_contents = _working
    closure["_capture"].cell_contents = _capture
    answer = await check(dashboard_id=str(uuid.uuid4()), capture=True)
    assert "could not be rendered (The dashboard did not finish rendering in time.)" in answer.content
    assert "headless browser is unavailable" not in answer.content
