"""The version 2 dashboard definition: upgrade from widgets, references, bounds, edits.

A stored widget dashboard reads as a definition; a definition only refers to
elements it holds; every free-form object is bounded; a widget edit keeps
what the widget form cannot carry (interactions, visibility, description).
"""

from __future__ import annotations

import pytest

from app.services.custom_dashboard_spec import validate_spec
from app.services.dashboard_definition import (
    MAX_OBJECT_BYTES,
    DefinitionError,
    apply_definition_operations,
    from_widgets,
    upgrade,
    validate_definition,
    widget_view,
)

INTEGRATION = "11111111-1111-4111-8111-111111111111"
CRQ = {"kind": "platform", "id": "kev", "source": "opencrq", "integration_id": INTEGRATION, "native": {"metric": "kev_findings"}}


def _base(**extra) -> dict:
    definition = {
        "version": 2,
        "title": "Board",
        "dataSources": {"answer": {"type": "static", "options": {"shape": "number", "data": {"value": 42, "change": -3}}}},
        "visualizations": {"big": {"type": "xtm.singlevalue", "title": "Answer", "dataSources": {"primary": "answer"}}},
        "layout": {
            "tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
            "layoutDefinitions": {"main": {"structure": [{"item": "big", "position": {"x": 0, "y": 0, "w": 4, "h": 6}}]}},
        },
    }
    definition.update(extra)
    return definition


def test_a_widget_dashboard_reads_as_a_definition_with_its_places():
    spec = validate_spec(
        {"title": "Today", "subtitle": "Friday", "widgets": [{"kind": "text", "id": "note", "body": "hi", "width": "half"}, CRQ]}
    )
    definition = validate_definition(upgrade(spec))
    assert (definition["title"], definition["description"]) == ("Today", "Friday")
    assert definition["visualizations"]["note"]["type"] == "xtm.text"
    assert definition["visualizations"]["note"]["options"] == {"body": "hi"}
    assert definition["visualizations"]["kev"]["dataSources"] == {"primary": "ds_kev"}
    assert definition["dataSources"]["ds_kev"]["options"]["source"] == "opencrq"
    structure = definition["layout"]["layoutDefinitions"]["layout_1"]["structure"]
    assert [(e["item"], e["position"]) for e in structure] == [
        ("note", {"x": 0, "y": 0, "w": 6, "h": 2}),
        ("kev", {"x": 0, "y": 2, "w": 12, "h": 3}),
    ]


def test_the_widget_view_gives_back_the_widgets():
    spec = from_widgets({"title": "T", "widgets": [{"kind": "text", "id": "note", "body": "hi"}, CRQ]})
    widgets = widget_view(spec)
    assert [(w["id"], w["kind"]) for w in widgets] == [("note", "text"), ("kev", "platform")]
    assert widgets[1]["native"] == {"metric": "kev_findings"} and widgets[1]["integration_id"] == INTEGRATION


def test_a_static_source_is_kept_in_its_closed_shape():
    definition = validate_definition(_base())
    assert definition["dataSources"]["answer"]["options"]["data"] == {"value": 42.0, "change": -3.0}


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"visualizations": {"big": {"type": "xtm.singlevalue", "dataSources": {"primary": "ghost"}}}}, "names no data source 'ghost'"),
        ({"visualizations": {"big": {"type": "xtm.line"}}}, "needs dataSources.primary"),
        ({"visualizations": {"big": {"type": "xtm.text", "options": {"body": "x"}, "dataSources": {"primary": "answer"}}}}, "reads no data source"),
        ({"visualizations": {"big": {"type": "xtm.gauge9000", "dataSources": {"primary": "answer"}}}}, "type"),
        ({"dataSources": {"answer": {"type": "static", "options": {"shape": "number", "data": {"value": "many"}}}}}, "does not fit the 'number' shape"),
        ({"dataSources": {"answer": {"type": "sql", "options": {}}}}, "dataSources.answer"),
        ({"inputs": {"big": {"type": "input.text", "token": "q"}}}, r"share the id\(s\) big"),
        ({"inputs": {"q": {"type": "input.text", "token": "1bad"}}}, "a token is"),
    ],
)
def test_a_definition_refers_only_to_what_it_holds(change, fragment):
    with pytest.raises(DefinitionError, match=fragment):
        validate_definition(_base(**change))


@pytest.mark.parametrize(
    ("layout", "fragment"),
    [
        ({"tabs": {"items": [{"layoutId": "other", "label": "O"}]}, "layoutDefinitions": {"main": {}}}, "names no layout 'other'"),
        ({"tabs": {"items": [{"layoutId": "main", "label": "M"}]}, "layoutDefinitions": {"main": {}, "lost": {}}}, "lost is in no tab"),
        (
            {"tabs": {"items": [{"layoutId": "main", "label": "M"}]}, "layoutDefinitions": {"main": {"structure": [{"item": "ghost", "position": {"x": 0, "y": 0, "w": 1, "h": 1}}]}}},
            "names no block 'ghost'",
        ),
        (
            {"tabs": {"items": [{"layoutId": "main", "label": "M"}]}, "layoutDefinitions": {"main": {"structure": [{"item": "big", "position": {"x": 8, "y": 0, "w": 6, "h": 1}}]}}},
            "x \\+ w must be at most 12",
        ),
        (
            {"tabs": {"items": [{"layoutId": "main", "label": "M"}]}, "layoutDefinitions": {"main": {"structure": [
                {"item": "big", "position": {"x": 0, "y": 0, "w": 1, "h": 1}},
                {"item": "big", "position": {"x": 1, "y": 0, "w": 1, "h": 1}},
            ]}}},
            "places 'big' twice",
        ),
    ],
)
def test_a_layout_places_only_what_exists_once_and_within_its_grid(layout, fragment):
    with pytest.raises(DefinitionError, match=fragment):
        validate_definition(_base(layout=layout))


def test_an_absolute_layout_places_in_pixels():
    layout = {
        "tabs": {"items": [{"layoutId": "main", "label": "M"}]},
        "layoutDefinitions": {"main": {"type": "absolute", "options": {"width": 1440, "height": 960}, "structure": [
            {"item": "big", "position": {"x": 700, "y": 40, "w": 300, "h": 200}},
        ]}},
    }
    assert validate_definition(_base(layout=layout))["layout"]["layoutDefinitions"]["main"]["type"] == "absolute"


def test_free_objects_and_the_whole_definition_are_bounded():
    with pytest.raises(DefinitionError, match="more than"):
        validate_definition(_base(defaults={"blob": "x" * MAX_OBJECT_BYTES}))
    with pytest.raises(DefinitionError, match="at most"):
        validate_definition(_base(expressions={f"k{i}": "x" * 2000 for i in range(300)}))


def test_a_static_row_link_must_stay_in_the_platform():
    rows = {"shape": "rows", "data": {"columns": [{"key": "name"}], "rows": [{"name": "x", "_url": "https://evil.example"}]}}
    with pytest.raises(DefinitionError, match="web app"):
        validate_definition(_base(dataSources={"answer": {"type": "static", "options": rows}}))


def test_a_widget_edit_keeps_what_the_widget_form_cannot_carry():
    spec = from_widgets({"title": "T", "widgets": [{"kind": "text", "id": "note", "body": "hi"}]})
    spec["visualizations"]["note"]["eventHandlers"] = [{"type": "drilldown.setToken", "options": {"tokens": []}}]
    spec["visualizations"]["note"]["description"] = "Why it matters"
    edited = apply_definition_operations(spec, [{"op": "upsert", "widget": {"kind": "text", "id": "note", "body": "new"}}])
    viz = edited["visualizations"]["note"]
    assert (viz["options"], viz["description"], viz["eventHandlers"][0]["type"]) == ({"body": "new"}, "Why it matters", "drilldown.setToken")


def test_removing_a_platform_widget_drops_its_own_source_only():
    spec = from_widgets({"title": "T", "widgets": [{"kind": "text", "id": "note", "body": "hi"}, CRQ]})
    spec["dataSources"]["shared"] = {"type": "static", "options": {"shape": "number", "data": {"value": 1}}}
    edited = apply_definition_operations(spec, [{"op": "remove", "id": "kev"}])
    assert sorted(edited["dataSources"]) == ["shared"]


def test_element_edits_set_and_delete_by_path():
    spec = validate_definition(_base())
    edited = apply_definition_operations(
        spec,
        [
            {"op": "set", "path": "inputs.q", "value": {"type": "input.text", "token": "q", "defaultValue": "*"}},
            {"op": "set", "path": "layout", "value": {**spec["layout"], "globalInputs": ["q"]}},
            {"op": "set", "path": "title", "value": "Renamed"},
        ],
    )
    assert (edited["title"], edited["inputs"]["q"]["token"], edited["layout"]["globalInputs"]) == ("Renamed", "q", ["q"])
    dropped = apply_definition_operations(edited, [{"op": "delete", "path": "inputs.q"}])
    assert (dropped["inputs"], dropped["layout"]["globalInputs"]) == ({}, [])
    with pytest.raises(DefinitionError, match="title cannot be deleted"):
        apply_definition_operations(spec, [{"op": "delete", "path": "title"}])
    with pytest.raises(DefinitionError, match="path must be"):
        apply_definition_operations(spec, [{"op": "set", "path": "visualizations", "value": {}}])
    with pytest.raises(DefinitionError, match="no visualizations element 'ghost'"):
        apply_definition_operations(spec, [{"op": "delete", "path": "visualizations.ghost"}])


def test_a_layout_edit_lands_in_the_layout_it_names():
    spec = validate_definition(
        _base(
            layout={
                "tabs": {"items": [{"layoutId": "main", "label": "Main"}, {"layoutId": "two", "label": "Two"}]},
                "layoutDefinitions": {
                    "main": {"structure": [{"item": "big", "position": {"x": 0, "y": 0, "w": 4, "h": 6}}]},
                    "two": {"type": "absolute", "structure": []},
                },
            }
        )
    )
    moved = apply_definition_operations(spec, [{"op": "layout", "layoutId": "main", "items": [{"id": "big", "x": 4, "y": 2, "w": 8, "h": 5}]}])
    assert moved["layout"]["layoutDefinitions"]["main"]["structure"][0]["position"] == {"x": 4, "y": 2, "w": 8, "h": 5}
    with pytest.raises(DefinitionError, match="no widget has the id 'big'"):
        apply_definition_operations(spec, [{"op": "layout", "layoutId": "two", "items": [{"id": "big", "x": 0, "y": 0, "w": 1, "h": 1}]}])
    with pytest.raises(DefinitionError, match="no layout 'three'"):
        apply_definition_operations(spec, [{"op": "layout", "layoutId": "three", "items": [{"id": "big", "x": 0, "y": 0, "w": 1, "h": 1}]}])


def test_a_cross_check_error_reads_as_the_field_it_is_about():
    broken = _base(visualizations={"big": {"type": "xtm.singlevalue", "dataSources": {"primary": "ghost"}}})
    with pytest.raises(DefinitionError) as caught:
        validate_definition(broken)
    lines = [line for line in str(caught.value).splitlines() if line.startswith("  - ")]
    assert lines == ["  - visualizations.big.dataSources.primary names no data source 'ghost'"]


def test_a_layout_step_places_a_visualization_placed_nowhere_yet():
    spec = _base()
    spec["visualizations"]["gauge"] = {"type": "xtm.fillergauge", "dataSources": {"primary": "answer"}, "options": {"maxValue": 60}}
    spec["layout"]["tabs"]["items"].append({"layoutId": "two", "label": "Two"})
    spec["layout"]["layoutDefinitions"]["two"] = {"structure": []}
    placed = apply_definition_operations(
        validate_definition(spec), [{"op": "layout", "layoutId": "main", "items": [{"id": "gauge", "x": 0, "y": 6, "w": 6, "h": 5}]}]
    )
    assert placed["layout"]["layoutDefinitions"]["main"]["structure"][-1] == {
        "item": "gauge", "type": "block", "position": {"x": 0, "y": 6, "w": 6, "h": 5},
    }  # fmt: skip
    # Placed already: moved where it is, never added to a second tab.
    with pytest.raises(DefinitionError, match="no widget has the id 'gauge'"):
        apply_definition_operations(placed, [{"op": "layout", "layoutId": "two", "items": [{"id": "gauge", "x": 0, "y": 0, "w": 1, "h": 1}]}])
    # An id that names nothing is still refused.
    with pytest.raises(DefinitionError, match="no widget has the id 'ghost'"):
        apply_definition_operations(placed, [{"op": "layout", "layoutId": "main", "items": [{"id": "ghost", "x": 0, "y": 0, "w": 1, "h": 1}]}])


# ── The validation views XTM One invented, upgraded on read ──────────────────

OUTCOMES = {"type": "static", "options": {"shape": "outcomes", "data": {"items": [{"label": "Prevention", "success": 3, "failed": 1}]}}}


def _aev(widget_type: str) -> dict:
    config = {"widget_configuration_type": "structural-histogram", "field": "base_attack_patterns_side", "series": [{"name": "", "filter": {}}]}
    return {"type": "platform", "options": {"source": "openaev", "integration_id": INTEGRATION, "native": {"widget_type": widget_type, "widget_config": config}}}


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [
        ("xtm.validationgates", "xtm.exposureconsole"),
        ("xtm.exposuregauge", "xtm.scoregauge"),
        ("xtm.resiliencerings", "xtm.ringgauge"),
        ("xtm.outcomecards", "xtm.domaincards"),
        ("xtm.outcomegrid", "xtm.outcomematrix"),
    ],
)
def test_a_stored_invented_validation_view_is_read_as_its_replacement(legacy, replacement):
    stored = _base(
        dataSources={"out": OUTCOMES},
        visualizations={"big": {"type": legacy, "title": "Posture", "dataSources": {"primary": "out"}, "options": {"link": {"label": "Open", "href": "/dashboards"}}}},
        defaults={"visualizations": {legacy: {"options": {}}, "global": {"options": {}}}},
    )
    read = upgrade(stored)
    assert read["visualizations"]["big"]["type"] == replacement
    assert read["visualizations"]["big"]["options"] == stored["visualizations"]["big"]["options"]
    assert set(read["defaults"]["visualizations"]) == {replacement, "global"}
    # What is stored is left as it was; what is read validates.
    assert stored["visualizations"]["big"]["type"] == legacy
    assert validate_definition(read)["visualizations"]["big"]["type"] == replacement


def test_a_platform_widget_drawn_with_an_invented_view_takes_its_module_s_own():
    stored = _base(
        dataSources={"path": _aev("attack-path"), "matrix": _aev("security-coverage")},
        visualizations={
            "path": {"type": "xtm.outcomegrid", "dataSources": {"primary": "path"}},
            "matrix": {"type": "xtm.outcomegrid", "dataSources": {"primary": "matrix"}},
            # Each takes its module's view, whatever the invented one was.
            "gates": {"type": "xtm.validationgates", "dataSources": {"primary": "matrix"}},
        },
    )
    read = upgrade(stored)
    assert {vid: viz["type"] for vid, viz in read["visualizations"].items()} == {
        "path": "xtm.pathgraph",
        "matrix": "xtm.outcomematrix",
        "gates": "xtm.outcomematrix",
    }


def test_a_version_1_platform_widget_with_an_invented_view_is_read_with_its_replacement():
    widget = {"kind": "platform", "id": "path", "source": "openaev", "integration_id": INTEGRATION, "native": _aev("attack-path")["options"]["native"], "view": "outcomegrid"}
    gauge = {**widget, "id": "gauge", "view": "exposure", "native": _aev("exposure-score")["options"]["native"]}
    read = upgrade({"title": "Old", "widgets": [widget, gauge]})
    assert {vid: viz["type"] for vid, viz in read["visualizations"].items()} == {"path": "xtm.pathgraph", "gauge": "xtm.scoregauge"}
