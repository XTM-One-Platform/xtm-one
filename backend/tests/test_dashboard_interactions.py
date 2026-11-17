"""Interactions and visibility: typed click handlers, conditions, references checked.

A handler is one of a closed set (set or unset tokens from what the click
gives, a link - a web-app path or an http(s) address -, another dashboard
with tokens, the platform's page, a tab, a time range from a brush); a
visualization shows under conditions the definition names.
"""

from __future__ import annotations

import pytest

from app.services.dashboard_definition import DefinitionError, validate_definition

DASHBOARD = "22222222-2222-4222-8222-222222222222"


def _with(handlers: list | None = None, visibility: dict | None = None, conditions: dict | None = None) -> dict:
    viz: dict = {"type": "xtm.table", "dataSources": {"primary": "s"}}
    if handlers is not None:
        viz["eventHandlers"] = handlers
    if visibility is not None:
        viz["containerOptions"] = {"visibility": visibility}
    return {
        "version": 2,
        "title": "T",
        "dataSources": {"s": {"type": "static", "options": {"shape": "rows", "data": {"columns": [{"key": "name"}], "rows": []}}}},
        "visualizations": {"v": viz},
        "expressions": {"conditions": conditions} if conditions is not None else {},
        "layout": {
            "tabs": {"items": [{"layoutId": "l", "label": "L"}, {"layoutId": "m", "label": "M"}]},
            "layoutDefinitions": {"l": {"structure": [{"item": "v", "position": {"x": 0, "y": 0, "w": 6, "h": 6}}]}, "m": {}},
        },
    }


def test_every_handler_type_validates_in_order():
    handlers = [
        {"type": "drilldown.setToken", "options": {"tokens": [{"token": "host", "key": "row.name.value"}, {"token": "mode", "value": "detail"}]}},
        {"type": "drilldown.unsetToken", "options": {"tokens": [{"token": "q"}]}},
        {"type": "drilldown.linkToUrl", "options": {"url": "https://cti.example/search?q=$click.value|u$", "newTab": True}},
        {"type": "drilldown.linkToDashboard", "options": {"dashboardId": DASHBOARD, "tokens": [{"token": "host", "value": "$click.value$"}]}},
        {"type": "drilldown.openInPlatform", "options": {}},
        {"type": "drilldown.switchTab", "options": {"layoutId": "m"}},
        {"type": "drilldown.setTimeRange", "options": {"token": "zoom"}},
    ]  # fmt: skip
    definition = validate_definition(_with(handlers, {"showConditions": ["picked"], "hideWhenNoData": True}, {"picked": {"name": "Host picked", "value": "isSet($host$)"}}))
    viz = definition["visualizations"]["v"]
    assert [h["type"] for h in viz["eventHandlers"]] == [h["type"] for h in handlers]
    assert viz["eventHandlers"][3]["options"] == {"dashboardId": DASHBOARD, "tokens": [{"token": "host", "value": "$click.value$"}], "newTab": False}
    assert viz["containerOptions"] == {"visibility": {"showConditions": ["picked"], "hideConditions": [], "hideWhenNoData": True}}


@pytest.mark.parametrize(
    ("handlers", "fragment"),
    [
        ([{"type": "drilldown.runScript", "options": {}}], "type"),
        ([{"type": "drilldown.setToken", "options": {"tokens": [{"token": "a", "key": "row.x.value", "value": "y"}]}}], "one of the two"),
        ([{"type": "drilldown.setToken", "options": {"tokens": [{"token": "a", "key": "row.x"}]}}], "key is name"),
        ([{"type": "drilldown.setToken", "options": {"tokens": [{"token": "1a", "value": "x"}]}}], "a token is"),
        ([{"type": "drilldown.linkToUrl", "options": {"url": "javascript:alert(1)"}}], "url is"),
        ([{"type": "drilldown.linkToUrl", "options": {"url": "//evil.example/x"}}], "url is"),
        ([{"type": "drilldown.linkToDashboard", "options": {"dashboardId": "nope"}}], "dashboardId"),
        ([{"type": "drilldown.switchTab", "options": {"layoutId": "zz"}}], "names no layout 'zz'"),
        ([{"type": "drilldown.openInPlatform", "options": {"url": "x"}}], "url"),
        ([{"type": "drilldown.setTimeRange", "options": {"token": "a.b"}}], "token"),
        ([{"type": "drilldown.setToken", "options": {"tokens": []}}] * 11, "at most 10"),
    ],
)
def test_a_handler_is_refused_with_its_field(handlers, fragment):
    with pytest.raises(DefinitionError, match=fragment):
        validate_definition(_with(handlers))


@pytest.mark.parametrize(
    ("visibility", "conditions", "fragment"),
    [
        ({"showConditions": ["ghost"]}, {}, "names no condition 'ghost'"),
        ({"hideConditions": ["c"]}, {"c": {"value": ""}}, "1-300 characters"),
        ({}, {"c": {"value": "x", "eval": "y"}}, r"\{name, value\}"),
        ({}, {"bad id!": {"value": "x"}}, "an id is"),
        ({"hideWhenNoData": "yes"}, {}, "hideWhenNoData"),
        ({"showWhen": "x"}, {}, "showWhen"),
    ],
)
def test_visibility_names_only_conditions_the_definition_holds(visibility, conditions, fragment):
    with pytest.raises(DefinitionError, match=fragment):
        validate_definition(_with(None, visibility, conditions))
