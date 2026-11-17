"""Tokens: the page's values put into data sources, typed inputs, bounded requests.

A token goes only where a value goes (a filter's values, a step's value, a
range) and comes out through the same checks as a stored widget; a source
using a token with no value waits; a request's tokens are bounded; each input
type has its own options.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.dashboard_definition import DefinitionError, optional_tokens, validate_definition
from app.services.dashboard_platform import check_native
from app.services.dashboard_tokens import (
    TokensWaitingError,
    TokenValues,
    names_in,
    put_in_filters,
    range_of,
    substitute,
)

CTI = {
    "type": "number",
    "perspective": "entities",
    "dataSelection": [{"perspective": "entities", "filters": {"mode": "and", "filterGroups": [], "filters": [
        {"key": ["entity_type"], "operator": "eq", "values": ["$types$"], "mode": "or"},
        {"key": ["name"], "operator": "search", "values": ["$q$ group"], "mode": "or"},
        {"key": ["confidence"], "operator": "gte", "values": [50], "mode": "or"},
    ]}}],
    "parameters": {"title": "$q$ stays"},
}  # fmt: skip


def test_a_token_goes_into_filter_values_only():
    out = put_in_filters(CTI, {"types": ["Malware", "Intrusion-Set"], "q": "APT"})
    filters = out["dataSelection"][0]["filters"]["filters"]
    assert [f["values"] for f in filters] == [["Malware", "Intrusion-Set"], ["APT group"], [50]]
    assert out["parameters"]["title"] == "$q$ stays"
    # The result is a widget like any stored one.
    assert check_native("opencti", out).native["dataSelection"][0]["filters"]["filters"][0]["values"] == ["Malware", "Intrusion-Set"]


def test_a_source_waits_for_a_token_without_value():
    for tokens in ({}, {"types": []}, {"types": ""}, {"types": ["Malware"]}):
        with pytest.raises(TokensWaitingError):
            put_in_filters(CTI, tokens)


def test_a_derived_step_value_takes_a_token():
    steps = [{"op": "where", "field": "sev", "operator": "eq", "value": "$sev$"}, {"op": "head", "count": 5}]
    assert put_in_filters(steps, {"sev": "high"})[0]["value"] == "high"


def test_substitution_and_names():
    assert substitute("$a$", {"a": ["x", "y"]}) == ["x", "y"]
    assert substitute("pre $a|u$ post $b$", {"a": ["x", "y"], "b": "z"}) == "pre x,y post z"
    assert substitute("no tokens, $5 off", {}) == "no tokens, $5 off"
    assert names_in({"a": ["$x$", {"b": "$env:user$ and $ds:result.count$"}]}) == {"x", "env:user", "ds:result.count"}
    assert range_of("$time$", {"time": "30d"}, None) == "30d"
    assert range_of(None, {}, "7d") == "7d"
    assert range_of("90d", {}, "7d") == "90d"


@pytest.mark.parametrize(
    "values",
    [
        {f"t{i}": "x" for i in range(51)},
        {"bad name": "x"},
        {"$x$": "x"},
        {"t": "x" * 501},
        {"t": ["x"] * 51},
        {"t": ["x" * 501]},
        {"t": {"nested": "x"}},
    ],
)
def test_a_request_s_tokens_are_bounded(values):
    with pytest.raises(ValidationError):
        TokenValues(values=values)


def _with(inputs: dict, **extra) -> dict:
    return {
        "version": 2,
        "title": "T",
        "dataSources": {"s": {"type": "static", "options": {"shape": "number", "data": {"value": 1}}}},
        "visualizations": {"v": {"type": "xtm.singlevalue", "dataSources": {"primary": "s"}}},
        "inputs": inputs,
        "layout": {
            "tabs": {"items": [{"layoutId": "l", "label": "L"}]},
            "layoutDefinitions": {"l": {"structure": [{"item": "v", "position": {"x": 0, "y": 0, "w": 4, "h": 6}}]}},
            "globalInputs": list(inputs),
        },
        **extra,
    }


def test_each_input_type_validates_its_options():
    definition = validate_definition(
        _with(
            {
                "time": {"type": "input.timerange", "token": "time", "defaultValue": "30d", "options": {"items": ["7d", "30d"]}},
                "sev": {"type": "input.dropdown", "token": "sev", "defaultValue": "high", "options": {"items": [{"label": "High", "value": "high"}]}},
                "types": {"type": "input.multiselect", "token": "types", "defaultValue": ["Malware"], "dataSources": {"primary": "s"}, "options": {"valueField": "label"}},
                "q": {"type": "input.text", "token": "q", "options": {"placeholder": "Search"}},
                "n": {"type": "input.number", "token": "n", "defaultValue": 3, "options": {"min": 1, "max": 10}},
                "country": {"type": "input.entity", "token": "entity", "defaultValue": "c1", "options": {
                    "source": "opencti", "integration_id": "11111111-1111-4111-8111-111111111111", "entityTypes": ["Country"],
                }},
                "go": {"type": "input.button", "options": {"label": "Reset", "actions": [
                    {"type": "unsetToken", "token": "q"}, {"type": "link", "url": "/dashboards?tab=$q|u$"},
                    {"type": "switchTab", "layoutId": "l"}, {"type": "setToken", "token": "sev", "value": "low"},
                ]}},
            },
            expressions={"tokens": {"double": "$n$ * 2"}},
            defaults={"tokens": {"default": {"sev": {"value": "medium"}}}},
        )
    )  # fmt: skip
    assert definition["layout"]["options"] == {"submitButton": False, "submitOnDashboardLoad": True}
    assert definition["inputs"]["types"]["options"] == {"items": [], "valueField": "label"}
    # An entity input keeps its platform, integration (as text) and types.
    assert definition["inputs"]["country"]["options"] == {
        "source": "opencti", "integration_id": "11111111-1111-4111-8111-111111111111", "entityTypes": ["Country"],
    }  # fmt: skip


@pytest.mark.parametrize(
    ("inputs", "fragment"),
    [
        ({"t": {"type": "input.timerange", "token": "t", "defaultValue": "2d"}}, "a period among"),
        ({"t": {"type": "input.timerange", "token": "t", "options": {"items": ["5y"]}}}, "items are periods"),
        ({"t": {"type": "input.text"}}, "sets a token"),
        ({"t": {"type": "input.multiselect", "token": "t", "defaultValue": "x"}}, "list of values"),
        ({"t": {"type": "input.number", "token": "t", "defaultValue": "3"}}, "is a number"),
        ({"t": {"type": "input.text", "token": "t", "dataSources": {"primary": "s"}}}, "reads no data source"),
        ({"t": {"type": "input.dropdown", "token": "t", "options": {"items": [{"label": "", "value": "x"}]}}}, "label"),
        ({"t": {"type": "input.button", "token": "t", "options": {"label": "x", "actions": [{"type": "unsetToken", "token": "a"}]}}}, "no token"),
        ({"t": {"type": "input.button", "options": {"label": "x", "actions": [{"type": "link", "url": "javascript:alert(1)"}]}}}, "url is"),
        ({"t": {"type": "input.button", "options": {"label": "x", "actions": [{"type": "link", "url": "//evil.example"}]}}}, "url is"),
        ({"t": {"type": "input.button", "options": {"label": "x", "actions": [{"type": "switchTab", "layoutId": "nope"}]}}}, "names no layout 'nope'"),
        ({"t": {"type": "input.button", "options": {"label": "x", "actions": [{"type": "run", "cmd": "x"}]}}}, "actions"),
        ({"t": {"type": "input.text", "token": "t", "options": {"onChange": "x"}}}, "onChange"),
        ({"t": {"type": "input.entity", "token": "t", "options": {"source": "opencti", "entityTypes": []}}}, "entityTypes"),
        ({"t": {"type": "input.entity", "token": "t", "options": {"source": "misp", "entityTypes": ["Country"]}}}, "source"),
        ({"t": {"type": "input.entity", "token": "t", "options": {"source": "opencti", "entityTypes": ["Country; drop"]}}}, "entityTypes"),
        ({"t": {"type": "input.entity", "token": "t", "defaultValue": ["c1"], "options": {"source": "opencti", "entityTypes": ["Country"]}}}, "is text"),
        ({"t": {"type": "input.entity", "token": "t", "dataSources": {"primary": "s"}, "options": {"source": "opencti", "entityTypes": ["Country"]}}}, "reads no data source"),
    ],
)
def test_an_input_is_refused_with_its_field(inputs, fragment):
    with pytest.raises(DefinitionError, match=fragment):
        validate_definition(_with(inputs))


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        ({"expressions": {"tokens": {"env:user": "1"}}}, "not a token name"),
        ({"expressions": {"tokens": {"a": "x" * 301}}}, "1-300 characters"),
        ({"defaults": {"tokens": {"default": {"a": {"value": 3}}}}}, "text or a list"),
        ({"defaults": {"tokens": {"default": {"a": {"value": "x", "other": 1}}}}}, r"\{value\}"),
        ({"defaults": {"tokens": ["a"]}}, "defaults.tokens is"),
    ],
)
def test_token_expressions_and_defaults_are_checked(extra, fragment):
    with pytest.raises(DefinitionError, match=fragment):
        validate_definition(_with({}, **extra))


def test_a_platform_range_is_a_period_or_a_token():
    native = {"metric": "active_findings"}
    base = {"source": "opencrq", "integration_id": "11111111-1111-4111-8111-111111111111", "native": native}
    # A period key, an OpenCTI dashboard's fixed dates (either end open), or a token.
    for good in ("30d", "3m", "1d", "$time$", "2026-01-01/2026-03-31", "/2026-03-31"):
        validate_definition(_with({}, dataSources={"s": {"type": "platform", "options": {**base, "range": good}}}))
    for wrong in ("last week", "2026-03-31/2026-01-01", "2026-02-30/", "/"):
        with pytest.raises(DefinitionError, match="range is one of"):
            validate_definition(_with({}, dataSources={"s": {"type": "platform", "options": {**base, "range": wrong}}}))


def test_an_entity_input_not_required_leaves_its_filters_out_while_empty():
    # As OpenAEV reads a dashboard parameter nobody picked: the filter goes, the widget reads everything.
    native = {"widget_config": {"series": [{"filter": {"mode": "and", "filters": [
        {"key": "base_simulation_side", "values": ["$simulation$"], "operator": "eq", "mode": "or"},
        {"key": "base_entity", "values": ["inject"], "operator": "eq", "mode": "or"},
    ]}}]}}  # fmt: skip
    optional = frozenset({"simulation"})
    left = put_in_filters(native, {}, optional)
    assert left["widget_config"]["series"][0]["filter"]["filters"] == [{"key": "base_entity", "values": ["inject"], "operator": "eq", "mode": "or"}]
    picked = put_in_filters(native, {"simulation": "s-1"}, optional)
    assert picked["widget_config"]["series"][0]["filter"]["filters"][0]["values"] == ["s-1"]
    # A required input still makes its source wait.
    with pytest.raises(TokensWaitingError):
        put_in_filters(native, {}, frozenset())
    definition = _with(
        {
            "simulation": {"type": "input.entity", "token": "simulation", "options": {"source": "openaev", "entityTypes": ["simulation"], "required": False}},
            "country": {"type": "input.entity", "token": "country", "options": {"source": "opencti", "entityTypes": ["Country"]}},
        }
    )
    assert optional_tokens(validate_definition(definition)) == {"simulation"}
