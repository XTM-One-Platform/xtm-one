"""What the Dashboard Studio's configuration panel and toolbar write, as the definition keeps it.

The panel (``frontend/src/components/custom-dashboard/viz-config.ts``) writes a
colour by ranges as the dynamic option ``> majorValue | rangeValue(<key>EditorConfig)``
with the ranges in the visualization's ``context``, series colours, gauge
ranges, a trellis, the panel chrome, a type switched with the options the new
type takes; the toolbar adds a chart on sample data written in the dashboard
and inputs above the tabs. Each must be stored as written; one the server
would refuse is refused with the field named.
"""

from __future__ import annotations

import pytest

from app.services.dashboard_definition import DefinitionError, validate_definition


def _definition(viz: dict, *, sources: dict | None = None, inputs: dict | None = None) -> dict:
    return {
        "version": 2,
        "title": "Ops",
        "dataSources": sources
        or {
            "ds_sample": {
                "type": "static",
                "name": "Sample data",
                "options": {"shape": "number", "data": {"value": 42, "change": 6}},
            }
        },
        "visualizations": {"viz_ds_sample": viz},
        "inputs": inputs or {},
        "layout": {
            "tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
            "layoutDefinitions": {
                "main": {
                    "type": "grid",
                    "structure": [
                        {"item": "viz_ds_sample", "type": "block", "position": {"x": 0, "y": 0, "w": 6, "h": 6}}
                    ],
                }
            },
            "globalInputs": list((inputs or {}).keys()),
        },
    }


def test_a_colour_by_ranges_is_kept_with_its_ranges() -> None:
    ranges = [
        {"to": 50, "value": "#4caf50"},
        {"from": 50, "to": 80, "value": "#ff9800"},
        {"from": 80, "value": "#f44336"},
    ]
    stored = validate_definition(
        _definition(
            {
                "type": "xtm.singlevalue",
                "dataSources": {"primary": "ds_sample"},
                "options": {
                    "majorColor": "> majorValue | rangeValue(majorColorEditorConfig)",
                    "shouldAbbreviateValue": False,
                },
                "context": {"majorColorEditorConfig": ranges},
                "showProgressBar": True,
                "showLastUpdated": True,
            }
        )
    )
    viz = stored["visualizations"]["viz_ds_sample"]
    assert viz["options"] == {
        "majorColor": "> majorValue | rangeValue(majorColorEditorConfig)",
        "shouldAbbreviateValue": False,
    }
    assert viz["context"] == {"majorColorEditorConfig": ranges}
    assert (viz["showProgressBar"], viz["showLastUpdated"]) == (True, True)


def test_series_colours_a_trellis_and_gauge_ranges_are_kept() -> None:
    series = {
        "type": "static",
        "options": {"shape": "distribution", "data": {"items": [{"label": "High", "value": 14}]}},
    }
    chart = validate_definition(
        _definition(
            {
                "type": "xtm.pie",
                "dataSources": {"primary": "ds_sample"},
                "options": {
                    "seriesColors": ["#112233", "#445566"],
                    "showDonutHole": False,
                    "trellis": {"splitBy": "label", "columns": 3, "sharedScale": False, "sort": "-total"},
                },
            },
            sources={"ds_sample": series},
        )
    )
    assert chart["visualizations"]["viz_ds_sample"]["options"] == {
        "seriesColors": ["#112233", "#445566"],
        "showDonutHole": False,
        "trellis": {"enabled": True, "splitBy": "label", "columns": 3, "sharedScale": False, "sort": "-total"},
    }
    gauge = validate_definition(
        _definition(
            {
                "type": "xtm.fillergauge",
                "dataSources": {"primary": "ds_sample"},
                "options": {"gaugeRanges": [{"from": 0, "to": 50, "value": "#4caf50"}], "minValue": 0, "maxValue": 100},
            }
        )
    )
    assert gauge["visualizations"]["viz_ds_sample"]["options"]["gaugeRanges"] == [
        {"from": 0, "to": 50, "value": "#4caf50"}
    ]


@pytest.mark.parametrize(
    ("options", "field"),
    [
        # A type switched keeps only what the new type takes: an option of another type is refused.
        ({"stackMode": "stacked"}, "options.stackMode"),
        # A colour picked with transparency: a visualization's colours take none.
        ({"majorColor": "#11223344"}, "options.majorColor"),
        # A range expression naming no function the grammar knows.
        ({"majorColor": "> majorValue | ranges(majorColorEditorConfig)"}, "options.majorColor"),
    ],
)
def test_what_the_panel_never_writes_is_refused_with_its_field(options: dict, field: str) -> None:
    with pytest.raises(DefinitionError) as refused:
        validate_definition(
            _definition({"type": "xtm.singlevalue", "dataSources": {"primary": "ds_sample"}, "options": options})
        )
    assert field in str(refused.value)


def test_the_inputs_the_toolbar_adds_are_valid() -> None:
    inputs = {
        "input_dropdown": {
            "type": "input.dropdown",
            "title": "Dropdown",
            "token": "input_dropdown",
            "options": {"items": []},
        },
        "input_timerange": {
            "type": "input.timerange",
            "title": "Time range",
            "token": "input_timerange",
            "defaultValue": "30d",
        },
        "input_button": {
            "type": "input.button",
            "title": "Button",
            "options": {
                "label": "Button",
                "actions": [{"type": "setToken", "token": "input_button", "value": "clicked"}],
            },
        },
    }
    stored = validate_definition(
        _definition({"type": "xtm.singlevalue", "dataSources": {"primary": "ds_sample"}}, inputs=inputs)
    )
    assert sorted(stored["inputs"]) == ["input_button", "input_dropdown", "input_timerange"]
    assert stored["layout"]["globalInputs"] == ["input_dropdown", "input_timerange", "input_button"]
