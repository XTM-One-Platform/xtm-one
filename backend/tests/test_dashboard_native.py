"""The stored form of a dashboard (version 3): an OpenCTI manifest, platform widgets verbatim.

A definition written as a manifest reads back as it was; OpenCTI and OpenAEV
widgets are stored in their platform's own shape, with a dashboard input named
as each platform names one; a text widget is OpenCTI's; what neither platform
has stays under the one extension key; a manifest the working model cannot
read is refused with its reason.
"""

from __future__ import annotations

import pytest

from app.services.dashboard_definition import upgrade, validate_definition
from app.services.dashboard_native import (
    EXTENSION,
    NativeFormatError,
    from_native,
    is_native,
    to_native,
    validate_native,
    variable_id,
)

INTEGRATION = "11111111-1111-4111-8111-111111111111"
AEV_INTEGRATION = "22222222-2222-4222-8222-222222222222"

OCTI_NATIVE = {
    "type": "vertical-bar",
    "perspective": "entities",
    "dataSelection": [
        {
            "label": "Reports",
            "attribute": "entity_type",
            "date_attribute": "created_at",
            "perspective": "entities",
            "number": 10,
            "filters": {
                "mode": "and",
                "filterGroups": [],
                "filters": [
                    {"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"},
                    {"key": ["objects"], "values": ["$sector$"], "operator": "eq", "mode": "or"},
                ],
            },
        }
    ],
    "parameters": {"title": "Reports per month", "interval": "month"},
}
AEV_NATIVE = {
    "widget_type": "donut",
    "widget_config": {
        "widget_configuration_type": "structural-histogram",
        "field": "base_attack_patterns_side",
        "series": [{"name": "", "filter": {"mode": "and", "filters": [
            {"key": "base_simulation_side", "operator": "eq", "mode": "or", "values": ["$simulation$"]},
        ]}}],
    },
}  # fmt: skip


def _rich() -> dict:
    """A definition with every part the manifest has to carry."""
    return {
        "version": 2,
        "title": "SOC board",
        "description": "Every **morning**",
        "theme": "dark",
        "dataSources": {
            "ds_reports": {"type": "platform", "name": "Reports", "options": {"source": "opencti", "integration_id": INTEGRATION, "native": OCTI_NATIVE, "refresh": "5m", "range": "30d"}},
            "ds_aev": {"type": "platform", "options": {"source": "openaev", "integration_id": AEV_INTEGRATION, "native": AEV_NATIVE}},
            "ds_crq": {"type": "platform", "options": {"source": "opencrq", "integration_id": INTEGRATION, "native": {"metric": "kev_findings"}}},
            "ds_static": {"type": "static", "options": {"shape": "number", "data": {"value": 42, "change": -3}}},
            "ds_top": {"type": "derived", "options": {"extends": "ds_static", "steps": [{"op": "head", "count": 1}]}},
        },
        "visualizations": {
            "reports": {"type": "xtm.column", "title": "Reports", "dataSources": {"primary": "ds_reports"}, "options": {"stackMode": "stacked"}},
            "reports_line": {"type": "xtm.line", "dataSources": {"primary": "ds_reports"}},
            "aev": {"type": "xtm.pie", "dataSources": {"primary": "ds_aev"}},
            "kev": {"type": "xtm.singlevalue", "dataSources": {"primary": "ds_crq"}},
            "answer": {"type": "xtm.singlevalue", "title": "Answer", "dataSources": {"primary": "ds_static"},
                       "eventHandlers": [{"type": "drilldown.setToken", "options": {"tokens": [{"token": "picked", "key": "value"}]}}]},
            "note": {"type": "xtm.text", "title": "Readme", "options": {"body": "## Weekly review"}},
            "box": {"type": "xtm.shape.rectangle", "options": {}},
        },
        "inputs": {
            "in_sector": {"type": "input.entity", "title": "Sector", "token": "sector",
                          "options": {"source": "opencti", "integration_id": INTEGRATION, "entityTypes": ["Sector"]}},
            "in_sim": {"type": "input.entity", "token": "simulation",
                       "options": {"source": "openaev", "integration_id": AEV_INTEGRATION, "entityTypes": ["simulation"], "required": False}},
            "in_text": {"type": "input.text", "token": "host", "defaultValue": "srv-01"},
        },
        "layout": {
            "tabs": {"items": [{"layoutId": "main", "label": "Main"}, {"layoutId": "wall", "label": "Wall"}]},
            "layoutDefinitions": {
                "main": {"structure": [
                    {"item": "reports", "position": {"x": 0, "y": 0, "w": 6, "h": 8}},
                    {"item": "reports_line", "position": {"x": 6, "y": 0, "w": 6, "h": 8}},
                    {"item": "aev", "position": {"x": 0, "y": 8, "w": 4, "h": 8}},
                    {"item": "kev", "position": {"x": 4, "y": 8, "w": 4, "h": 6}},
                    {"item": "answer", "position": {"x": 8, "y": 8, "w": 4, "h": 6}},
                    {"item": "note", "position": {"x": 0, "y": 16, "w": 12, "h": 4}},
                ]},
                "wall": {"type": "absolute", "options": {"width": 800, "height": 400},
                         "structure": [{"item": "box", "position": {"x": 0, "y": 0, "w": 10, "h": 10}}]},
            },
            "globalInputs": ["in_sector", "in_sim", "in_text"],
        },
        "expressions": {"conditions": {"has_host": {"value": "isSet($host$)"}}},
        "applicationProperties": {"hideExport": True},
    }  # fmt: skip


@pytest.fixture
def definition() -> dict:
    return validate_definition(_rich())


def test_a_definition_comes_back_from_its_manifest_as_it_was(definition):
    manifest = to_native(definition)
    assert is_native(manifest)
    assert from_native(manifest) == definition
    assert validate_native(manifest) == manifest


def test_platform_widgets_are_stored_in_their_platform_s_shape(definition):
    widgets = to_native(definition)["widgets"]
    reports = widgets["reports"]
    assert {k: v for k, v in reports.items() if k not in ("id", "layout")}.keys() == {"type", "perspective", "dataSelection", "parameters"}
    assert reports["type"] == "vertical-bar" and reports["id"] == "reports"
    assert set(reports["layout"]) == {"i", "x", "y", "w", "h", "moved", "static"}
    # Two visualizations of one OpenCTI widget: two OpenCTI widgets, one data source in the extension.
    assert widgets["reports_line"]["dataSelection"] == reports["dataSelection"]
    assert set(widgets["aev"]) == {"id", "widget_type", "widget_config", "layout"}
    # No configuration is stored twice.
    sources = to_native(definition)[EXTENSION]["sources"]
    assert "native" not in sources["ds_reports"]["options"] and sources["ds_reports"]["hostedBy"] == "reports"
    assert "native" in sources["ds_crq"]["options"]  # OpenCRQ has no dashboards of its own


def test_an_input_a_filter_reads_is_named_as_each_platform_names_one(definition):
    manifest = to_native(definition)
    sector, simulation = variable_id("in_sector"), variable_id("in_sim")
    octi_values = manifest["widgets"]["reports"]["dataSelection"][0]["filters"]["filters"][1]["values"]
    assert octi_values == [f"$var:{sector}"]
    aev_values = manifest["widgets"]["aev"]["widget_config"]["series"][0]["filter"]["filters"][0]["values"]
    assert aev_values == [simulation]
    variables = {v["name"]: v for v in manifest["variables"]}
    assert variables["sector"] == {
        "id": sector, "name": "sector", "type": "entity", "restriction": {"mode": "none"},
        "defaultValue": None, "entityTypes": ["Sector"],
    }  # fmt: skip
    assert variables["simulation"]["type"] == "x_xtm_one.openaev.entity"
    assert variables["host"]["type"] == "text" and variables["host"]["defaultValue"] == "srv-01"


def test_a_text_widget_is_opencti_s_and_xtm_one_s_own_widgets_are_prefixed(definition):
    widgets = to_native(definition)["widgets"]
    assert widgets["note"]["type"] == "text"
    assert widgets["note"]["parameters"] == {"title": "Readme", "content": "## Weekly review"}
    assert widgets["answer"]["type"] == "x_xtm_one.singlevalue"
    assert widgets["answer"]["parameters"] == {"title": "Answer"}
    assert widgets["kev"]["type"] == "x_xtm_one.singlevalue"
    assert to_native(definition)[EXTENSION]["widgets"]["kev"]["platform"] == "opencrq"


def test_a_change_to_a_platform_widget_in_the_manifest_is_read(definition):
    manifest = to_native(definition)
    manifest["widgets"]["reports"]["parameters"]["interval"] = "week"
    assert from_native(manifest)["dataSources"]["ds_reports"]["options"]["native"]["parameters"]["interval"] == "week"
    manifest["widgets"]["note"]["parameters"]["content"] = "Changed"
    assert from_native(manifest)["visualizations"]["note"]["options"]["body"] == "Changed"


def test_the_grid_is_opencti_s_a_widget_sits_where_the_dashboard_places_it(definition):
    manifest = to_native(definition)
    assert manifest[EXTENSION]["grid"] == "opencti"
    assert {k: manifest["widgets"]["reports"]["layout"][k] for k in "xywh"} == {"x": 0, "y": 0, "w": 6, "h": 8}
    # A widget on a canvas only still has a place on the grid.
    assert manifest["widgets"]["box"]["layout"]["w"] >= 1


def test_a_dashboard_stored_before_reads_its_places_in_opencti_rows():
    from app.services.dashboard_native import current_form

    legacy = validate_definition(_rich())  # its places were rows of 36 px
    read = from_native(current_form(legacy))
    structure = {e["item"]: e["position"] for e in read["layout"]["layoutDefinitions"]["main"]["structure"]}
    # Tops and bottoms are converted: what touched still touches. 0-8 -> 0-4 (3.5 rounded), 8-14 -> 4-6, 16-20 -> 7-9.
    assert structure["reports"] == {"x": 0, "y": 0, "w": 6, "h": 4}
    assert structure["kev"] == {"x": 4, "y": 4, "w": 4, "h": 2}
    assert structure["note"] == {"x": 0, "y": 7, "w": 12, "h": 2}
    # A canvas keeps its pixels.
    assert read["layout"]["layoutDefinitions"]["wall"]["structure"][0]["position"] == {"x": 0, "y": 0, "w": 10, "h": 10}
    # Read once: today's manifest is returned as it is.
    manifest = current_form(legacy)
    assert current_form(manifest) is manifest
    # Version 1: a placed widget is converted, one it never placed is placed in today's rows.
    v1 = {"title": "T", "widgets": [{"kind": "text", "id": "a", "body": "a", "layout": {"x": 0, "y": 0, "w": 6, "h": 8}},
                                    {"kind": "text", "id": "b", "body": "b", "width": "half"}]}  # fmt: skip
    places = [e["position"] for e in from_native(current_form(v1))["layout"]["layoutDefinitions"]["layout_1"]["structure"]]
    # (an unplaced widget goes below the placed ones)
    assert places == [{"x": 0, "y": 0, "w": 6, "h": 4}, {"x": 0, "y": 4, "w": 6, "h": 2}]


def test_upgrade_reads_a_manifest_as_the_working_model(definition):
    assert upgrade(to_native(definition)) == definition


def test_a_manifest_without_the_extension_is_refused_with_its_reason(definition):
    manifest = to_native(definition)
    with pytest.raises(NativeFormatError, match="integrations it reads"):
        from_native({k: v for k, v in manifest.items() if k != EXTENSION})
    manifest["widgets"]["extra"] = {"id": "extra", "type": "line", "perspective": "entities", "dataSelection": []}
    with pytest.raises(NativeFormatError, match=r"widgets\.extra"):
        from_native(manifest)
    assert not is_native({"title": "v1", "widgets": []})


def test_an_opencti_export_holds_the_inputs_its_widgets_read_as_variables(definition):
    import base64
    import json

    from app.services.dashboard_definition import widget_view
    from app.services.dashboard_interop.opencti import build_dashboard_export

    export, report = build_dashboard_export("SOC board", widget_view(definition), inputs=definition["inputs"])
    manifest = json.loads(base64.b64decode(export["configuration"]["manifest"]))
    sector = variable_id("in_sector")
    assert manifest["variables"] == [
        {"id": sector, "name": "sector", "type": "entity", "restriction": {"mode": "none"}, "defaultValue": None, "entityTypes": ["Sector"]}
    ]
    reports = [w for w in manifest["widgets"].values() if w["type"] == "vertical-bar"]
    assert reports and reports[0]["dataSelection"][0]["filters"]["filters"][1]["values"] == [f"$var:{sector}"]
    assert not any("left out" in note and "Reports" in note for note in report.notes)


def test_an_opencti_dashboard_s_variables_are_read_on_import():
    import base64
    import json

    from app.services.dashboard_interop.opencti import parse_export_document

    widget = {"id": "w1", **OCTI_NATIVE, "layout": {"i": "w1", "x": 0, "y": 0, "w": 6, "h": 4}}
    widget = json.loads(json.dumps(widget).replace("$sector$", "$var:v-1"))
    text_widget = {"id": "w2", "type": "line", "perspective": "entities", "dataSelection": [{"label": "x", "attribute": "entity_type",
                   "date_attribute": "created_at", "perspective": "entities", "filters": {"mode": "and", "filterGroups": [],
                   "filters": [{"key": ["name"], "values": ["$var:v-2"], "operator": "eq", "mode": "or"}]}}],
                   "parameters": {"title": "By name"}, "layout": {"i": "w2", "x": 6, "y": 0, "w": 6, "h": 4}}  # fmt: skip
    manifest = {
        "config": {},
        "variables": [
            {"id": "v-1", "name": "Sector", "type": "entity", "entityTypes": ["Sector"], "restriction": {"mode": "none"}, "defaultValue": None},
            {"id": "v-2", "name": "Name", "type": "text", "restriction": {"mode": "none"}, "defaultValue": None},
        ],
        "widgets": {"w1": widget, "w2": text_widget},
    }
    document = {"openCTI_version": "7.0.0", "type": "dashboard",
                "configuration": {"name": "Imported", "manifest": base64.b64encode(json.dumps(manifest).encode()).decode()}}  # fmt: skip
    parsed = parse_export_document(document)
    assert [(i["id"], i["token"], i["options"]["entityTypes"]) for i in parsed.inputs] == [("input_sector", "sector", ["Sector"])]
    [kept] = parsed.widgets
    assert kept["native"]["dataSelection"][0]["filters"]["filters"][1]["values"] == ["$sector$"]
    assert any("By name" in note and "left out" in note for note in parsed.report.notes)
