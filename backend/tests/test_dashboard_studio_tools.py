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
