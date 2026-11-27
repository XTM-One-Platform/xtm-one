"""The options of a data visualization: closed per type, bounded, and the dynamic options grammar.

A definition's data visualizations carry their options
under their camelCase names; each type's options are a closed model, so an unknown key,
an enum outside its list, a number out of bounds, a colour that is not a token
or a strict hex, or an expression outside the subset is a 422 naming the field.
"""

from __future__ import annotations

import pytest

from app.services.dashboard_definition import (
    DATA_TYPES,
    TYPE_TO_VIEW,
    VIEW_TO_TYPE,
    DefinitionError,
    from_widgets,
    validate_definition,
    widget_view,
)
from app.services.dashboard_platform import PLATFORM_VIEWS, SHAPE_VIEWS, NativeCheck, NativeError, resolve_view
from app.services.dashboard_visualization_options import OPTIONS_BY_TYPE, check_expression, validate_options

NEW_TYPES = (
    "xtm.scatter",
    "xtm.bubble",
    "xtm.punchcard",
    "xtm.singlevalueicon",
    "xtm.singlevalueradial",
    "xtm.fillergauge",
    "xtm.markergauge",
)


def _definition(viz: dict, shape: str = "series", data: dict | None = None) -> dict:
    data = data or {"series": [{"name": "count", "points": [{"x": "2026-01-01", "y": 3}]}]}
    return {
        "version": 2,
        "title": "Board",
        "dataSources": {"main": {"type": "static", "options": {"shape": shape, "data": data}}},
        "visualizations": {"viz": {"dataSources": {"primary": "main"}, **viz}},
        "layout": {
            "tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
            "layoutDefinitions": {"main": {"structure": [{"item": "viz", "position": {"x": 0, "y": 0, "w": 6, "h": 8}}]}},
        },
    }


@pytest.mark.parametrize(
    "text",
    [
        "> primary",
        "> primary | seriesByName('count') | lastPoint() | rangeValue(ranges)",
        '> table | seriesByIndex(2) | formatByType(fmt) | prefix("$")',
        "> sparklineValues | delta(-2)",
        "> annotation | seriesByName('label') | matchValue(matches)",
        "  > primary | seriesByName('a') | min() | gradient(g)",
    ],
)
def test_the_subset_s_expressions_are_accepted(text):
    assert check_expression(text) == text


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("primary | lastPoint()", "an expression starts with '>'"),
        ("> primary | eval('1+1')", "unknown function 'eval'"),
        ("> primary | constructor()", "unknown function 'constructor'"),
        ("> primary | __proto__()", "unknown function '__proto__'"),
        ("> primary | seriesByName(1)", "seriesByName() takes (string)"),
        ("> primary | seriesByIndex('a')", "seriesByIndex() takes (number)"),
        ("> primary | lastPoint(", "lastPoint() needs ')'"),
        ("> primary lastPoint()", "steps are separated by '|'"),
        ("> primary | rangeValue(a, b)", "rangeValue() takes (ref)"),
        ("> primary; alert(1)", "unexpected text at 8"),
        ("> 'primary'", "an expression starts with a source name"),
        ("> primary" + " | min()" * 11, "an expression has at most 10 steps"),
        ("> primary | prefix('" + "a" * 600 + "')", "an expression holds at most 500 characters"),
    ],
)
def test_what_the_grammar_does_not_hold_is_refused(text, error):
    with pytest.raises(ValueError) as caught:
        check_expression(text)
    assert str(caught.value).startswith(error)


def test_every_data_type_has_an_options_model_and_the_new_types_are_data_types():
    assert set(OPTIONS_BY_TYPE) == set(DATA_TYPES)
    assert set(NEW_TYPES) <= set(DATA_TYPES)
    assert {TYPE_TO_VIEW[t] for t in NEW_TYPES} == {"scatter", "bubble", "punchcard", "icon", "radial", "filler", "marker"}
    assert set(VIEW_TO_TYPE) - {None} == set(PLATFORM_VIEWS)


def test_options_are_stored_under_their_camel_case_names():
    stored = validate_options(
        "xtm.column",
        {
            "stackMode": "stacked100",
            "overlayFields": ["errors"],
            "y2Fields": "latency",
            "showOverlayY2Axis": True,
            "yAxisScale": "log",
            "yAxisMin": 1,
            "dataValuesDisplay": "minmax",
            "legendDisplay": "top",
            "nullValueDisplay": "zero",
            "seriesColors": ["danger", "#00ff00", "chart3"],
            "seriesColorsByField": {"errors": "#f00"},
            "pointColors": "> primary | seriesByName('value') | rangeValue(ranges)",
            "annotationColor": "alert",
            "showBrush": True,
            "resultLimit": 200,
        },
    )
    assert stored == {
        "stackMode": "stacked100",
        "overlayFields": ["errors"],
        "y2Fields": "latency",
        "showOverlayY2Axis": True,
        "yAxisScale": "log",
        "yAxisMin": 1.0,
        "dataValuesDisplay": "minmax",
        "legendDisplay": "top",
        "nullValueDisplay": "zero",
        "seriesColors": ["danger", "#00ff00", "chart3"],
        "seriesColorsByField": {"errors": "#f00"},
        "pointColors": "> primary | seriesByName('value') | rangeValue(ranges)",
        "annotationColor": "alert",
        "showBrush": True,
        "resultLimit": 200,
    }


@pytest.mark.parametrize(
    ("viz_type", "options", "error"),
    [
        ("xtm.line", {"stackmode": "stacked"}, "options.stackmode: Extra inputs are not permitted"),
        ("xtm.line", {"legendDisplay": "sideways"}, "options.legendDisplay: Input should be 'right', 'left', 'top', 'bottom' or 'off'"),
        ("xtm.area", {"resultLimit": 5}, "options.resultLimit: Input should be greater than or equal to 10"),
        ("xtm.area", {"lineWidth": 60}, "options.lineWidth: Input should be less than or equal to 6"),
        ("xtm.line", {"seriesColors": ["red"]}, "options.seriesColors.0: a colour is #rgb, #rrggbb or one of"),
        ("xtm.line", {"seriesColors": ["url(javascript:x)"]}, "options.seriesColors.0: String should have at most 7 characters"),
        ("xtm.line", {"annotationColor": "#abcd"}, "options.annotationColor: a colour is #rgb, #rrggbb or one of"),
        ("xtm.pie", {"collapseThreshold": 0.9}, "options.collapseThreshold: Input should be less than or equal to 0.5"),
        ("xtm.pie", {"stackMode": "stacked"}, "options.stackMode: Extra inputs are not permitted"),
        ("xtm.singlevalue", {"majorColor": "> primary | eval()"}, "options.majorColor: unknown function 'eval'"),
        ("xtm.singlevalue", {"majorColor": "var(--x)"}, "options.majorColor: a colour is #rgb, #rrggbb or one of"),
        ("xtm.singlevalue", {"numberPrecision": 9}, "options.numberPrecision: Input should be less than or equal to 6"),
        ("xtm.singlevalue", {"icon": "shield"}, "options.icon: Extra inputs are not permitted"),
        ("xtm.singlevalueicon", {"icon": "shieldd-check"}, "options.icon: unknown icon 'shieldd-check'; did you mean shield-check"),
        ("xtm.markergauge", {"gaugeRanges": [{"from": 0, "to": 5, "value": "danger", "label": "x"}]}, "options.gaugeRanges.0.label: Extra inputs are not permitted"),
        ("xtm.table", {"columnFormat": {"a": {"width": 5}}}, "options.columnFormat.a.width: Input should be greater than or equal to 40"),
        ("xtm.table", {"columnFormat": {"a": {"cellType": "html"}}}, "options.columnFormat.a.cellType: Input should be 'text', 'array' or 'sparkline'"),
        ("xtm.table", {"tableFormat": {"rowColors": ["#fff", "nope"]}}, "options.tableFormat.rowColors."),
        ("xtm.table", {"count": 0}, "options.count: Input should be greater than or equal to 1"),
        ("xtm.list", {"count": 3}, "options.count: Extra inputs are not permitted"),
        ("xtm.scatter", {"bubbleSizeMax": 100}, "options.bubbleSizeMax: Input should be less than or equal to 60"),
    ],
)
def test_options_outside_their_type_s_model_are_refused_with_their_path(viz_type, options, error):
    with pytest.raises(ValueError) as caught:
        validate_options(viz_type, options)
    assert error in str(caught.value)


def test_the_singlevalue_family_and_the_gauges_read_their_options():
    assert validate_options(
        "xtm.singlevalueicon",
        {
            "majorValue": "> primary | seriesByName('value') | lastPoint()",
            "trendValue": 3,
            "sparklineValues": [1, 2, None, "4"],
            "trendDisplay": "percent",
            "unit": "%",
            "numberPrecision": 1,
            "majorColor": "> majorValue | rangeValue(ranges)",
            "icon": "ShieldCheck",
            "iconPosition": "above",
        },
    ) == {
        "majorValue": "> primary | seriesByName('value') | lastPoint()",
        "trendValue": 3.0,
        "sparklineValues": [1.0, 2.0, None, "4"],
        "trendDisplay": "percent",
        "unit": "%",
        "numberPrecision": 1,
        "majorColor": "> majorValue | rangeValue(ranges)",
        "icon": "shield-check",
        "iconPosition": "above",
    }
    assert validate_options("xtm.markergauge", {"gaugeRanges": [{"from": 0, "to": 50, "value": "success"}], "orientation": "vertical"}) == {
        "gaugeRanges": [{"from": 0.0, "to": 50.0, "value": "success"}],
        "orientation": "vertical",
    }
    assert validate_options("xtm.singlevalueradial", {"maxValue": 10, "radialStrokeColor": "chart2"}) == {
        "maxValue": 10.0,
        "radialStrokeColor": "chart2",
    }


def test_a_definition_validates_its_visualizations_options_and_keeps_the_panel_chrome():
    stored = validate_definition(
        _definition(
            {
                "type": "xtm.line",
                "description": "Alerts per day",
                "showProgressBar": True,
                "showLastUpdated": True,
                "options": {"legendDisplay": "off", "link": {"label": "Open", "href": "/agents"}},
            }
        )
    )
    viz = stored["visualizations"]["viz"]
    assert (viz["description"], viz["showProgressBar"], viz["showLastUpdated"]) == ("Alerts per day", True, True)
    assert viz["options"] == {"legendDisplay": "off", "link": {"label": "Open", "href": "/agents"}}
    with pytest.raises(DefinitionError) as caught:
        validate_definition(_definition({"type": "xtm.line", "options": {"legendDisplay": "sideways"}}))
    assert "  - visualizations.viz: options.legendDisplay: Input should be" in str(caught.value)
    with pytest.raises(DefinitionError) as caught:
        validate_definition(_definition({"type": "xtm.line", "options": {"link": {"label": "x", "href": "https://evil.example"}}}))
    assert "  - visualizations.viz: options.link.href: href must be a path" in str(caught.value)


@pytest.mark.parametrize("viz_type", NEW_TYPES)
def test_each_new_type_is_a_valid_visualization(viz_type):
    stored = validate_definition(_definition({"type": viz_type}))
    assert stored["visualizations"]["viz"]["type"] == viz_type


def test_a_platform_widget_may_take_a_new_view_that_suits_its_data():
    number = NativeCheck(native={}, shape="number", native_type="kev_findings", title=None, preferred_view="number")
    assert [resolve_view(number, view) for view in ("icon", "radial", "filler", "marker")] == ["icon", "radial", "filler", "marker"]
    rows = NativeCheck(native={}, shape="rows", native_type="list", title=None, preferred_view="table")
    assert resolve_view(rows, "punchcard") == "punchcard"
    with pytest.raises(NativeError, match="view 'radial' cannot draw rows data"):
        resolve_view(rows, "radial")
    assert SHAPE_VIEWS["series"] == ("line", "area", "bar", "scatter", "heatmap", "radar")
    assert SHAPE_VIEWS["distribution"] == ("distribution", "donut", "hbar", "bar", "treemap", "radar", "polar", "wordcloud")
    assert SHAPE_VIEWS["outcomes"] == ("outcomematrix", "pathgraph", "exposureconsole", "scoregauge", "ringgauge", "domaincards")


def test_a_platform_widget_with_a_new_view_round_trips_through_the_definition():
    widget = {
        "kind": "platform",
        "id": "kev",
        "source": "opencrq",
        "integration_id": "11111111-1111-4111-8111-111111111111",
        "native": {"metric": "kev_findings"},
        "view": "radial",
    }
    definition = from_widgets({"title": "Board", "widgets": [widget]})
    assert definition["visualizations"]["kev"]["type"] == "xtm.singlevalueradial"
    assert widget_view(definition)[0]["view"] == "radial"
