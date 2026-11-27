"""Platform widgets and the OpenCTI / OpenAEV / pack conversions (pure, no I/O).

* ``dashboard_platform``: what a native OpenCTI / OpenAEV / OpenCRQ widget may
  hold, the data shape it produces and how it is drawn;
* ``custom_dashboard_spec.PlatformWidget`` and the write-time gate on the
  ``DASHBOARD_STUDIO`` flag;
* ``dashboard_interop``: real OpenCTI exports (the dashboards seeded in the
  ``opencti-dashboards`` knowledge base) and a real OpenAEV export (its
  starter-pack simulation dashboard) read into widgets and written back
  unchanged; hostile files refused with a reason; what a conversion leaves out
  reported.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.services.custom_dashboard_spec import DashboardSpecError, validate_spec, validate_widget
from app.services.custom_dashboards import check_platform_widgets_allowed
from app.services.dashboard_grid import ROW_PITCH, rows_from_platform, rows_to_platform, width_of
from app.services.dashboard_interop import MAX_PACK_WIDGETS, InteropError, parse_any, read_zip_entries
from app.services.dashboard_interop.layout import reading_order
from app.services.dashboard_interop.openaev import build_export_zip
from app.services.dashboard_interop.opencti import (
    build_custom_view_export,
    build_dashboard_export,
    build_widget_export,
    config_from_period,
    decode_base64_json,
    encode_base64_json,
)
from app.services.dashboard_interop.pack_format import PACK_FORMAT, bind_widget, build_pack, validate_pack_widget
from app.services.dashboard_platform import NativeError, check_native, resolve_view
from app.services.dashboard_platform.opencrq import METRICS, procedure_input

BACKEND = Path(__file__).resolve().parents[1]
OPENCTI_DASHBOARDS = sorted((BACKEND / "app/agents/seed_data/knowledge_base/opencti-dashboards").glob("dashboard-*.json"))
OPENAEV_EXPORT = BACKEND / "tests/fixtures/dashboards/openaev_technical_simulation.json"
INTEGRATION = "11111111-1111-4111-8111-111111111111"

EMPTY = {"mode": "and", "filters": [], "filterGroups": []}


def _opencti_widget(**overrides) -> dict:
    widget = {
        "type": "donut",
        "perspective": "entities",
        "dataSelection": [
            {
                "attribute": "entity_type",
                "filters": {
                    "mode": "and",
                    "filters": [{"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}],
                    "filterGroups": [],
                },
            }
        ],
        "parameters": {"title": "Reports by type"},
    }
    widget.update(overrides)
    return widget


def _openaev_widget(**config) -> dict:
    widget_config = {
        "widget_configuration_type": "flat",
        "title": "Vulnerable endpoints",
        "series": [
            {"name": "", "filter": {"mode": "and", "filters": [{"key": "base_entity", "operator": "eq", "values": ["vulnerable-endpoint"]}]}}
        ],
    }
    widget_config.update(config)
    return {"widget_type": "number", "widget_config": widget_config}


def _opencti_export(path: Path, *, urlsafe: bool = False) -> dict:
    """A seeded dashboard as OpenCTI exports it: the manifest base64-encoded."""
    seeded = json.loads(path.read_text())
    raw = json.dumps(seeded["manifest"]).encode()
    manifest = (base64.urlsafe_b64encode(raw).rstrip(b"=") if urlsafe else base64.b64encode(raw)).decode()
    return {"openCTI_version": seeded["openCTI_version"], "type": "dashboard", "configuration": {"name": seeded["name"], "manifest": manifest}}


def _openaev_zip(document: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("custom_dashboards.json", json.dumps(document))
        archive.writestr("meta.json", json.dumps({"schema": {"kind": "jsonapi", "version": 1}}))
    return buffer.getvalue()


# ── Native checks ────────────────────────────────────────────────────────────


def test_an_opencti_widget_is_kept_in_canonical_form_with_its_shape():
    check = check_native("opencti", {**_opencti_widget(), "id": "x", "layout": {"x": 0}})
    assert check.shape == "distribution"
    assert check.native_type == "donut"
    assert check.title == "Reports by type"
    assert check.native == {
        "type": "donut",
        "perspective": "entities",
        "dataSelection": [
            {
                "label": "",
                "number": 10,
                "attribute": "entity_type",
                "date_attribute": "created_at",
                "isTo": True,
                "perspective": "entities",
                "filters": {
                    "mode": "and",
                    "filters": [{"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}],
                    "filterGroups": [],
                },
                "dynamicFrom": EMPTY,
                "dynamicTo": EMPTY,
            }
        ],
        "parameters": {"title": "Reports by type"},
    }
    assert resolve_view(check, None) == "donut"


@pytest.mark.parametrize(
    ("widget", "shape"),
    [
        (_opencti_widget(type="number"), "number"),
        (_opencti_widget(type="line"), "series"),
        (_opencti_widget(type="list"), "rows"),
        (_opencti_widget(type="map", perspective="relationships"), "geo"),
        (_opencti_widget(type="tree"), "distribution"),
        (_opencti_widget(type="wordcloud"), "distribution"),
        (_opencti_widget(type="radar"), "distribution"),
        (_opencti_widget(type="polar-area"), "distribution"),
        (_opencti_widget(type="heatmap"), "series"),
        (_opencti_widget(type="bookmark"), "rows"),
        (_opencti_widget(perspective="audits"), None),
    ],
)
def test_each_opencti_type_has_its_shape_or_is_drawn_as_a_link(widget, shape):
    assert check_native("opencti", widget).shape == shape


def _selection(**overrides) -> dict:
    return {**_opencti_widget()["dataSelection"][0], **overrides}


def _deep_group(depth: int) -> dict:
    group = copy.deepcopy(EMPTY)
    for _ in range(depth - 1):
        group = {"mode": "and", "filters": [], "filterGroups": [group]}
    return group


@pytest.mark.parametrize(
    ("widget", "fragment"),
    [
        (_opencti_widget(type="text"), "becomes a 'text' widget"),
        (_opencti_widget(type="custom-attributes"), "OpenCTI widget type must be one of"),
        (_opencti_widget(perspective="files"), "perspective must be one of"),
        (_opencti_widget(type="map"), "perspective must be one of: relationships"),
        (_opencti_widget(dataSelection=[]), "1 to 1 dataSelection"),
        # A saved filter is named by its id, never by anything else.
        (_opencti_widget(dataSelection=[_selection(filters_id="a b")]), "id of an OpenCTI saved filter"),
        (_opencti_widget(dataSelection=[_selection(dynamicTo_id=["x"])]), "id of an OpenCTI saved filter"),
        (_opencti_widget(dataSelection=[_selection(script="x")]), "unknown keys: script"),
        (
            _opencti_widget(dataSelection=[_selection(filters={"mode": "and", "filters": [{"key": "x", "operator": "script", "values": []}]})]),
            "operator 'script' is not allowed",
        ),
        (_opencti_widget(dataSelection=[_selection(filters={"mode": "xor", "filters": []})]), "mode must be 'and' or 'or'"),
        (_opencti_widget(dataSelection=[_selection(filters=_deep_group(5))]), "more than 4 levels"),
        (
            _opencti_widget(
                dataSelection=[_selection(filters={"mode": "and", "filters": [{"key": "x", "values": ["a"]}] * 31})]
            ),
            "at most 30 filters",
        ),
        (
            _opencti_widget(dataSelection=[_selection(filters={"mode": "and", "filters": [{"key": "a b", "values": []}]})]),
            "must be an attribute name",
        ),
        (
            _opencti_widget(dataSelection=[_selection(filters={"mode": "and", "filters": [{"key": "x", "values": ["a\x00"]}]})]),
            "NUL character",
        ),
        (
            _opencti_widget(dataSelection=[_selection(filters={"mode": "and", "filters": [{"key": "x", "values": ["a" * 501]}]})]),
            "longer than 500",
        ),
        (_opencti_widget(dataSelection=[_selection(number=0)]), "between 1 and 1000"),
        (_opencti_widget(parameters={"interval": "minute"}), "interval must be one of"),
        (_opencti_widget(parameters={"onclick": "x"}), "unknown keys: onclick"),
        (
            _opencti_widget(dataSelection=[_selection(label="x" * 15000)]),
            "at most 200 characters",
        ),
    ],
)
def test_an_opencti_widget_that_cannot_be_kept_says_why(widget, fragment):
    with pytest.raises(NativeError) as refused:
        check_native("opencti", widget)
    assert fragment in str(refused.value)


def test_an_opencti_widget_keeps_what_the_opencti_editor_writes():
    # Audits' "Distinct", a custom field's column and a top N left empty (saved as null).
    audits = _opencti_widget(type="number", perspective="audits", dataSelection=[_selection(perspective="audits", unique=True)])
    assert check_native("opencti", audits).native["dataSelection"][0]["unique"] is True
    column = {"attribute": "my_field", "label": "My field", "attributeType": "string"}
    listed = check_native("opencti", _opencti_widget(type="list", dataSelection=[_selection(columns=[column], number=None)]))
    assert (listed.native["dataSelection"][0]["columns"], listed.native["dataSelection"][0]["number"]) == ([column], None)


def test_an_opencti_map_keeps_its_zoom_written_as_text():
    check = check_native("opencti", _opencti_widget(type="map", perspective="relationships", dataSelection=[_selection(zoom="2")]))
    assert check.native["dataSelection"][0]["zoom"] == 2


def test_a_configuration_beyond_its_size_is_refused():
    columns = [{"attribute": f"attribute_{i}", "label": "x" * 199} for i in range(20)]
    selections = [_selection(columns=columns) for _ in range(5)]
    with pytest.raises(NativeError) as refused:
        check_native("opencti", _opencti_widget(type="line", dataSelection=selections))
    assert str(refused.value) == "The opencti configuration is too large (more than 16 KB)."


def test_an_openaev_widget_is_kept_in_canonical_form():
    check = check_native("openaev", _openaev_widget())
    assert (check.shape, check.native_type, check.title) == ("number", "number", "Vulnerable endpoints")
    assert check.native == {
        "widget_type": "number",
        "widget_config": {
            "widget_configuration_type": "flat",
            "title": "Vulnerable endpoints",
            "time_range": "DEFAULT",
            "date_attribute": "base_created_at",
            "series": [
                {
                    "name": "",
                    "filter": {
                        "mode": "and",
                        "filters": [{"key": "base_entity", "values": ["vulnerable-endpoint"], "operator": "eq", "mode": "or"}],
                    },
                }
            ],
        },
    }


@pytest.mark.parametrize(
    ("widget", "fragment"),
    [
        ({"widget_type": "gauge", "widget_config": {}}, "widget_type must be one of"),
        (_openaev_widget(widget_configuration_type="list"), "needs widget_config.widget_configuration_type 'flat'"),
        (
            _openaev_widget(series=[{"name": "", "filter": {"mode": "and", "filters": [{"key": "a", "operator": "equals", "values": []}]}}]),
            "operator 'equals' is not allowed",
        ),
        (_openaev_widget(time_range="LAST_CENTURY"), "time_range must be one of"),
        (_openaev_widget(columns=["a"]), "unknown keys for flat: columns"),
        (
            {"widget_type": "list", "widget_config": {"widget_configuration_type": "list", "series": [{"name": "", "filter": {}}]}},
            "reads its rows from widget_config.perspective",
        ),
        (
            {"widget_type": "line", "widget_config": {"widget_configuration_type": "temporal-histogram", "interval": "decade", "series": [{"name": "", "filter": {}}]}},
            "interval must be one of",
        ),
    ],
)
def test_an_openaev_widget_that_cannot_be_kept_says_why(widget, fragment):
    with pytest.raises(NativeError) as refused:
        check_native("openaev", widget)
    assert fragment in str(refused.value)


@pytest.mark.parametrize(
    ("widget_type", "config_type", "shape", "view"),
    [
        ("security-coverage", "structural-histogram", "outcomes", "outcomematrix"),
        ("attack-path", "structural-histogram", "outcomes", "pathgraph"),
        ("command-center", "structural-histogram", "outcomes", "exposureconsole"),
        ("exposure-score", "structural-histogram", "outcomes", "scoregauge"),
        ("resilience-gauge", "structural-histogram", "outcomes", "ringgauge"),
        ("average", "average", "outcomes", "domaincards"),
        ("posture-radar", "structural-histogram", "series", "radar"),
    ],
)
def test_an_openaev_security_posture_widget_is_drawn_as_openaev_draws_it(widget_type, config_type, shape, view):
    config = {"widget_configuration_type": config_type, "series": [{"name": "", "filter": {}}]}
    if config_type == "structural-histogram":
        config["field"] = "base_attack_patterns_side"
    check = check_native("openaev", {"widget_type": widget_type, "widget_config": config})
    assert (check.shape, resolve_view(check, None)) == (shape, view)


def test_an_opencrq_widget_names_a_metric_never_a_procedure():
    assert check_native("opencrq", {"metric": "kev_findings"}).shape == "number"
    assert check_native("opencrq", {"metric": "risk_by_scope"}).native == {"metric": "risk_by_scope", "window_days": 90}
    with pytest.raises(NativeError) as refused:
        check_native("opencrq", {"metric": "setTenantHomeDashboard"})
    assert "metric must be one of" in str(refused.value)
    with pytest.raises(NativeError):
        check_native("opencrq", {"metric": "kev_findings", "procedure": "dashboard.setTenantHomeDashboard"})
    with pytest.raises(NativeError) as refused:
        check_native("opencrq", {"metric": "kev_findings", "window_days": 30})
    assert str(refused.value) == "The kev_findings metric does not take window_days."
    with pytest.raises(NativeError):
        check_native("opencrq", {"metric": "findings_trend", "organization_id": "../admin"})


def test_every_opencrq_metric_calls_a_dashboard_query_with_only_its_declared_input():
    org = "22222222-2222-4222-8222-222222222222"
    assert all(m.procedure.startswith("dashboard.get") for m in METRICS.values())
    assert procedure_input({"metric": "exposure_trend_ale", "organization_id": org}) == {"organizationId": org, "windowDays": 90}
    assert procedure_input({"metric": "risk_by_scope", "organization_id": org, "window_days": 30}) == {"windowDays": 30}
    assert procedure_input({"metric": "top_controls"}) == {}


@pytest.mark.parametrize(
    ("view", "expected"),
    [(None, "donut"), ("hbar", "hbar"), ("donut", "donut")],
)
def test_a_view_follows_the_data(view, expected):
    assert resolve_view(check_native("opencti", _opencti_widget()), view) == expected


def test_a_view_that_cannot_draw_the_data_is_refused():
    with pytest.raises(NativeError) as refused:
        resolve_view(check_native("opencti", _opencti_widget()), "line")
    assert str(refused.value) == (
        "view 'line' cannot draw distribution data from a donut widget; use one of: "
        "distribution, donut, hbar, bar, treemap, radar, polar, wordcloud."
    )


# ── The platform widget in a dashboard ──────────────────────────────────────


def test_a_platform_widget_is_validated_through_its_source():
    widget = validate_widget({"kind": "platform", "source": "opencti", "integration_id": INTEGRATION, "native": _opencti_widget()})
    assert (widget["view"], widget["size"], widget["title"], widget["integration_id"]) == ("donut", "m", "Reports by type", INTEGRATION)
    with pytest.raises(DashboardSpecError) as refused:
        validate_widget({"kind": "platform", "source": "opencti", "integration_id": INTEGRATION, "native": _opencti_widget(type="text")})
    assert "becomes a 'text' widget" in str(refused.value)
    with pytest.raises(DashboardSpecError):
        validate_widget({"kind": "platform", "source": "opencti", "integration_id": "not-a-uuid", "native": _opencti_widget()})
    with pytest.raises(DashboardSpecError):
        validate_widget({"kind": "platform", "source": "splunk", "integration_id": INTEGRATION, "native": {}})


def _dashboard(*widgets) -> dict:
    return validate_spec({"title": "T", "widgets": list(widgets) or [{"kind": "text", "body": "x"}]})


def test_without_the_studio_no_platform_widget_is_added_or_changed(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    live = {"kind": "platform", "id": "live", "source": "opencrq", "integration_id": INTEGRATION, "native": {"metric": "kev_findings"}}
    before = _dashboard({"kind": "text", "id": "t", "body": "x"}, live)
    with pytest.raises(DashboardSpecError) as refused:
        check_platform_widgets_allowed(None, before)
    assert "needs the Dashboard Studio" in str(refused.value)
    # Kept as it was, or removed: the rest of the dashboard stays editable.
    assert check_platform_widgets_allowed(before, _dashboard({"kind": "text", "id": "t", "body": "y"}, live)) == []
    assert check_platform_widgets_allowed(before, _dashboard({"kind": "text", "id": "t", "body": "y"})) == []
    changed = {**live, "native": {"metric": "critical_findings"}}
    with pytest.raises(DashboardSpecError):
        check_platform_widgets_allowed(before, _dashboard(changed))


def test_with_the_studio_platform_widgets_are_written(dashboard_studio_enabled):
    live = {"kind": "platform", "source": "opencrq", "integration_id": INTEGRATION, "native": {"metric": "kev_findings"}}
    changed = check_platform_widgets_allowed(None, _dashboard(live))
    assert [(w["source"], w["integration_id"], w["native"]) for w in changed] == [
        ("opencrq", INTEGRATION, {"metric": "kev_findings"})
    ]


# ── OpenCTI exports ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", OPENCTI_DASHBOARDS, ids=lambda p: p.stem)
def test_every_seeded_opencti_dashboard_is_read_whole_and_written_back_unchanged(path):
    seeded = json.loads(path.read_text())
    parsed = parse_any(json.dumps(_opencti_export(path)).encode())
    assert parsed.origin == "opencti-dashboard"
    assert parsed.name == seeded["name"]
    assert len(parsed.widgets) == len(seeded["manifest"]["widgets"])
    assert parsed.report.notes == []
    bound = [bind_widget(w, INTEGRATION) for w in parsed.widgets]
    export, report = build_dashboard_export(parsed.name, bound, version=seeded["openCTI_version"])
    assert report.notes == []
    assert export["openCTI_version"] == seeded["openCTI_version"]
    manifest = decode_base64_json(export["configuration"]["manifest"], what="manifest")
    assert all(w["layout"]["i"] == wid for wid, w in manifest["widgets"].items())
    # Every widget comes back where it was, as large as it was.
    cells = [{k: w["layout"][k] for k in "xywh"} for w in manifest["widgets"].values()]
    seeded_cells = [{k: w["layout"][k] for k in "xywh"} for w in seeded["manifest"]["widgets"].values()]
    assert cells == sorted(seeded_cells, key=lambda c: (c["y"], c["x"]))
    again = parse_any(json.dumps(export).encode())
    assert [w.get("native", w.get("body")) for w in again.widgets] == [w.get("native", w.get("body")) for w in parsed.widgets]
    assert [w["layout"] for w in again.widgets] == [w["layout"] for w in parsed.widgets]


def test_an_opencti_manifest_in_url_safe_base64_is_read():
    path = OPENCTI_DASHBOARDS[0]
    assert parse_any(json.dumps(_opencti_export(path, urlsafe=True)).encode()).widgets == parse_any(
        json.dumps(_opencti_export(path)).encode()
    ).widgets


def _custom_view(target: object = "Country") -> dict:
    """An OpenCTI custom view: a list of what targets the page's entity, a number of reports about it, a text."""
    regarding = {"key": ["regardingOf"], "values": [{"key": "id", "values": ["SELF_ID"]}, {"key": "relationship_type", "values": ["targets"]}], "operator": "eq", "mode": "or"}
    widgets = {
        "w1": {
            "type": "list", "perspective": "entities", "layout": {"x": 0, "y": 0, "w": 6, "h": 4},
            "parameters": {"title": "What targets it"},
            "dataSelection": [{"filters": {"mode": "and", "filters": [regarding], "filterGroups": []}, "number": 10}],
        },
        "w2": {
            "type": "number", "perspective": "entities", "layout": {"x": 6, "y": 0, "w": 3, "h": 2},
            "parameters": {"title": "Reports about it"},
            "dataSelection": [{"filters": {"mode": "and", "filters": [
                {"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"},
                {"key": ["objects"], "values": ["SELF_ID"], "operator": "eq", "mode": "or"},
            ], "filterGroups": []}}],
        },
        "w3": {"type": "text", "layout": {"x": 9, "y": 0, "w": 3, "h": 2}, "parameters": {"title": "About", "content": "Read me"}},
    }  # fmt: skip
    configuration = {"name": "Country overview", "manifest": encode_base64_json({"config": {}, "widgets": widgets})}
    if target is not None:
        configuration["target_entity_type"] = target
    return {"openCTI_version": "7.260706.0", "type": "custom-view", "configuration": configuration}


def test_an_opencti_custom_view_reads_its_entity_from_an_entity_input_of_its_type():
    parsed = parse_any(json.dumps(_custom_view()).encode())
    assert (parsed.origin, parsed.name, [w["kind"] for w in parsed.widgets]) == ("opencti-custom-view", "Country overview", ["platform", "platform", "text"])
    listed, counted = parsed.widgets[0]["native"], parsed.widgets[1]["native"]
    # Where OpenCTI puts the page's entity, the widgets read the $entity$ token.
    assert listed["dataSelection"][0]["filters"]["filters"][0]["values"][0] == {"key": "id", "values": ["$entity$"]}
    assert counted["dataSelection"][0]["filters"]["filters"][1]["values"] == ["$entity$"]
    assert "SELF_ID" not in json.dumps(parsed.widgets)
    assert parsed.inputs == [
        {"type": "input.entity", "title": "Country", "token": "entity", "options": {"source": "opencti", "entityTypes": ["Country"]}, "dataSources": {}}
    ]
    assert any("custom view of Country" in note for note in parsed.report.notes)


def _with_config(document: dict, config: dict) -> dict:
    manifest = decode_base64_json(document["configuration"]["manifest"], what="manifest")
    configuration = {**document["configuration"], "manifest": encode_base64_json({**manifest, "config": config})}
    return {**document, "configuration": configuration}


@pytest.mark.parametrize(
    ("config", "period"),
    [
        ({"relativeDate": "months-3", "startDate": None, "endDate": None}, "3m"),
        ({"relativeDate": "days-1"}, "1d"),
        ({"relativeDate": "years-1"}, "1y"),
        ({"relativeDate": None, "startDate": "2026-01-01T00:00:00.000Z", "endDate": "2026-03-31T00:00:00.000Z"}, "2026-01-01/2026-03-31"),
        ({"startDate": "2026-01-01T00:00:00+02:00"}, "2026-01-01/"),
    ],
)
def test_an_opencti_dashboard_s_period_is_read_by_its_widgets_and_written_back(config, period):
    parsed = parse_any(json.dumps(_with_config(_custom_view(), config)).encode())
    assert [w.get("range") for w in parsed.widgets] == [period, period, None]  # the text reads no period
    assert f"The dashboard reads the period {period}: its widgets keep it." in parsed.report.notes
    bound = [bind_widget(w, INTEGRATION) for w in parsed.widgets]
    export, _report = build_custom_view_export(parsed.name, bound, target_type="Country", token="entity")
    written = decode_base64_json(export["configuration"]["manifest"], what="manifest")["config"]
    assert written == config_from_period(period)
    assert config_from_period("3m") == {"relativeDate": "months-3", "startDate": None, "endDate": None}
    assert config_from_period("2026-01-01/") == {"relativeDate": None, "startDate": "2026-01-01T00:00:00.000Z", "endDate": None}
    assert [w.get("range") for w in parse_any(json.dumps(export).encode()).widgets] == [period, period, None]


def test_an_opencti_period_xtm_one_cannot_read_is_said_and_left_out():
    parsed = parse_any(json.dumps(_with_config(_custom_view(), {"relativeDate": "weeks-2"})).encode())
    assert [w.get("range") for w in parsed.widgets] == [None, None, None]
    assert "The dashboard's period could not be read: its widgets read the page's period." in parsed.report.notes


def test_an_opencti_export_writes_the_period_its_widgets_read_and_says_what_it_could_not():
    parsed = parse_any(json.dumps(_custom_view()).encode())
    bound = [bind_widget(w, INTEGRATION) for w in parsed.widgets]
    # Only the entity's widgets read a token: in a custom view they are kept; give them periods.
    bound[0]["range"], bound[1]["range"] = "90d", "6m"
    export, report = build_custom_view_export(parsed.name, bound, target_type="Country", token="entity")
    assert decode_base64_json(export["configuration"]["manifest"], what="manifest")["config"] in (
        {"relativeDate": "months-3", "startDate": None, "endDate": None},
        {"relativeDate": "months-6", "startDate": None, "endDate": None},
    )
    assert any("read different periods" in note for note in report.notes)
    bound[0]["range"] = bound[1]["range"] = "$time$"
    export, report = build_custom_view_export(parsed.name, bound, target_type="Country", token="entity")
    assert decode_base64_json(export["configuration"]["manifest"], what="manifest")["config"] == {}
    assert any("period of a dashboard input" in note for note in report.notes)


def test_an_opencti_export_says_a_widget_on_the_page_s_period_reads_the_dashboard_s():
    native = {"type": "number", "perspective": "entities", "dataSelection": [{}]}
    widgets = [
        {"kind": "platform", "id": "a", "title": "A", "source": "opencti", "native": native, "range": "3m"},
        {"kind": "platform", "id": "b", "title": "B", "source": "opencti", "native": native},
    ]
    export, report = build_dashboard_export("Board", widgets)
    assert decode_base64_json(export["configuration"]["manifest"], what="manifest")["config"]["relativeDate"] == "months-3"
    assert report.notes == ["The widgets that read the page's period read the OpenCTI dashboard's, 3m."]


def test_a_widget_s_period_travels_in_a_pack_and_into_a_dashboard():
    from app.services.dashboard_definition import summary_widgets, upgrade
    from app.services.dashboard_interop.pack_format import parse_pack_document

    parsed = parse_any(json.dumps(_with_config(_custom_view(), {"relativeDate": "months-6"})).encode())
    document = build_pack(parsed.name, None, [bind_widget(w, INTEGRATION) for w in parsed.widgets])
    assert [w.get("range") for w in document["widgets"]] == ["6m", "6m", None]
    again = parse_pack_document(document)
    assert [w.get("range") for w in again.widgets] == ["6m", "6m", None]
    spec = validate_spec({"title": "T", "widgets": [bind_widget(w, INTEGRATION) for w in again.widgets]})
    sources = upgrade(spec)["dataSources"]
    assert sorted(s["options"].get("range") for s in sources.values() if s["type"] == "platform") == ["6m", "6m"]
    assert [w.get("range") for w in summary_widgets(upgrade(spec))] == ["6m", "6m", None]
    with pytest.raises(DashboardSpecError, match="range is one of"):
        validate_pack_widget({**again.widgets[0], "range": "last week"})


def test_a_custom_view_goes_back_to_opencti_reading_its_page_s_entity():
    parsed = parse_any(json.dumps(_custom_view()).encode())
    bound = [bind_widget(w, INTEGRATION) for w in parsed.widgets]
    export, report = build_custom_view_export(parsed.name, bound, target_type="Country", token="entity")
    assert (export["type"], export["configuration"]["target_entity_type"], report.notes) == ("custom-view", "Country", [])
    manifest = decode_base64_json(export["configuration"]["manifest"], what="manifest")
    assert "$entity$" not in json.dumps(manifest) and json.dumps(manifest).count("SELF_ID") == 2
    again = parse_any(json.dumps(export).encode())
    assert [w.get("native", w.get("body")) for w in again.widgets] == [w.get("native", w.get("body")) for w in parsed.widgets]
    with pytest.raises(InteropError):
        build_custom_view_export(parsed.name, bound, target_type="Stix-Core-Object", token="entity")


def test_an_opencti_dashboard_export_leaves_out_the_widgets_reading_an_input():
    # An OpenCTI dashboard has no inputs: a $token$ left in a filter would match nothing there.
    parsed = parse_any(json.dumps(_custom_view()).encode())
    bound = [bind_widget(w, INTEGRATION) for w in parsed.widgets]
    export, report = build_dashboard_export(parsed.name, bound)
    manifest = decode_base64_json(export["configuration"]["manifest"], what="manifest")
    assert [w["type"] for w in manifest["widgets"].values()] == ["text"]
    assert "$entity$" not in json.dumps(manifest)
    assert report.notes == [
        "Widget 'What targets it' reads the dashboard's input $entity$; it is not a variable of the OpenCTI dashboard, so it was left out.",
        "Widget 'Reports about it' reads the dashboard's input $entity$; it is not a variable of the OpenCTI dashboard, so it was left out.",
    ]


@pytest.mark.parametrize(("target", "types", "title"), [(None, ["Stix-Core-Object"], "Entity"), ("Intrusion-Set", ["Intrusion-Set"], "Intrusion Set"), ("<b>", ["Stix-Core-Object"], "Entity")])
def test_a_custom_view_s_input_is_of_its_target_type_or_of_any_entity(target, types, title):
    parsed = parse_any(json.dumps(_custom_view(target)).encode())
    assert (parsed.inputs[0]["options"]["entityTypes"], parsed.inputs[0]["title"]) == (types, title)


def test_a_pack_keeps_its_entity_inputs_unbound_and_entity_inputs_only():
    from app.services.dashboard_interop.pack_format import parse_pack_document, validate_pack_input

    parsed = parse_any(json.dumps(_custom_view()).encode())
    bound = {**parsed.inputs[0], "options": {**parsed.inputs[0]["options"], "integration_id": str(INTEGRATION)}}
    document = build_pack(parsed.name, None, [bind_widget(w, INTEGRATION) for w in parsed.widgets], [bound])
    assert document["inputs"] == parsed.inputs
    again = parse_pack_document(json.loads(json.dumps(document)))
    assert again.inputs == parsed.inputs
    with pytest.raises(DashboardSpecError, match="entity inputs only"):
        validate_pack_input({"type": "input.dropdown", "token": "x", "options": {}})
    with pytest.raises(DashboardSpecError, match="invalid"):
        validate_pack_input({"type": "input.entity", "token": "x", "options": {"source": "opencti", "entityTypes": []}})
    wrong = {**document, "inputs": [{"type": "input.text", "token": "x", "options": {}}]}
    assert parse_pack_document(wrong).inputs == []


def test_a_single_opencti_widget_export_is_read_and_written():
    widget = {**_opencti_widget(), "layout": {"x": 0, "y": 0, "w": 6, "h": 4}}
    parsed = parse_any(json.dumps({"openCTI_version": "6.4.0", "type": "widget", "configuration": encode_base64_json(widget)}).encode())
    assert (parsed.origin, parsed.name, len(parsed.widgets)) == ("opencti-widget", "Reports by type", 1)
    assert parsed.widgets[0]["width"] == "half"
    # The dashboard's grid is OpenCTI's: the place is kept as it is.
    assert parsed.widgets[0]["layout"] == {"x": 0, "y": 0, "w": 6, "h": 4}
    export = build_widget_export(bind_widget(parsed.widgets[0], INTEGRATION), version="6.4.0")
    written = decode_base64_json(export["configuration"], what="widget")
    assert (written["type"], written["layout"]) == ("donut", {"x": 0, "y": 0, "w": 6, "h": 4})


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"openCTI_version": "5.12.15", "type": "dashboard", "configuration": {}}, "dashboards are read from version 5.12.16 on"),
        ({"openCTI_version": "six", "type": "dashboard"}, "does not name a valid openCTI_version"),
        ({"openCTI_version": "6.0.0", "type": "investigation", "configuration": {}}, "are not dashboards, custom views or widgets"),
        ({"openCTI_version": "6.0.0", "type": "dashboard", "configuration": {"manifest": "%%%"}}, "is not valid base64"),
        (
            {"openCTI_version": "6.0.0", "type": "dashboard", "configuration": {"manifest": base64.b64encode(b"[1, 2").decode()}},
            "is not valid JSON",
        ),
        (
            {"openCTI_version": "6.0.0", "type": "dashboard", "configuration": {"manifest": encode_base64_json({"widgets": []})}},
            "has no widgets",
        ),
    ],
)
def test_an_opencti_file_that_cannot_be_read_says_why(document, message):
    with pytest.raises(InteropError) as refused:
        parse_any(json.dumps(document).encode())
    assert message in str(refused.value)


def test_what_opencti_cannot_draw_is_left_out_of_its_export_and_said():
    widgets = [
        {"kind": "text", "id": "intro", "body": "Hello"},
        {"kind": "score", "id": "score", "label": "Score", "value": 1},
        validate_widget({"kind": "platform", "source": "opencrq", "integration_id": INTEGRATION, "native": {"metric": "kev_findings"}}),
    ]
    export, report = build_dashboard_export("D", widgets)
    assert report.notes == [
        "Widget 'score' (score) has no OpenCTI equivalent and was left out.",
        "Widget 'Known exploited vulnerabilities' (opencrq) has no OpenCTI equivalent and was left out.",
    ]
    manifest = decode_base64_json(export["configuration"]["manifest"], what="manifest")
    assert [w["type"] for w in manifest["widgets"].values()] == ["text"]
    assert export["openCTI_version"] == "6.0.0"


# ── OpenAEV exports ──────────────────────────────────────────────────────────


def test_the_openaev_starter_dashboard_is_read_whole_and_written_back_unchanged():
    document = json.loads(OPENAEV_EXPORT.read_text())
    parsed = parse_any(_openaev_zip(document))
    widget_count = len(document["data"]["relationships"]["custom_dashboard_widgets"]["data"])
    assert (parsed.origin, parsed.name, len(parsed.widgets)) == ("openaev-dashboard", "Technical simulation", widget_count)
    # Its widgets follow the simulation the dashboard is opened on: they read it from a simulation input, not
    # required (as OpenAEV, every simulation while none is picked), and nothing is dropped.
    simulation = next(r["id"] for r in document["included"] if r["type"] == "custom_dashboards_parameters" and r["attributes"]["custom_dashboards_parameter_type"] == "simulation")
    assert parsed.inputs == [
        {"type": "input.entity", "title": "Simulation", "token": "simulation", "options": {"source": "openaev", "entityTypes": ["simulation"], "required": False}, "dataSources": {}}
    ]
    assert parsed.report.notes[0] == (
        "The dashboard's simulation parameter 'simulation' is its 'Simulation' input: the widgets read $simulation$, every simulation while none is picked."
    )
    read = json.dumps([w["native"] for w in parsed.widgets])
    assert (read.count("$simulation$"), simulation in read) == (json.dumps(document["included"]).count(simulation) - 2, False)
    assert not any("left out" in note or "was removed" in note for note in parsed.report.notes)
    archive, report = build_export_zip(parsed.name, [bind_widget(w, INTEGRATION) for w in parsed.widgets], inputs=parsed.inputs)
    assert report.notes == []
    again = parse_any(archive)
    assert again.inputs == parsed.inputs
    assert [w["native"] for w in again.widgets] == [w["native"] for w in parsed.widgets]
    assert [w["layout"] for w in again.widgets] == [w["layout"] for w in parsed.widgets]
    entries = read_zip_entries(archive)
    written = json.loads(entries["custom_dashboards.json"])
    cells = sorted(
        (w["attributes"]["widget_layout"] for w in written["included"] if w["type"] == "widgets"),
        key=lambda c: (c["widget_layout_y"], c["widget_layout_x"]),
    )
    seeded_cells = sorted(
        (w["attributes"]["widget_layout"] for w in document["included"] if w["type"] == "widgets"),
        key=lambda c: (c["widget_layout_y"], c["widget_layout_x"]),
    )
    # The dashboard's grid is OpenCTI's (82 px rows), coarser than OpenAEV's (70 px): a place comes back
    # within a row of where it was, the columns exactly.
    assert len(cells) == len(seeded_cells)
    for cell, seeded in zip(cells, seeded_cells, strict=True):
        assert (cell["widget_layout_x"], cell["widget_layout_w"]) == (seeded["widget_layout_x"], seeded["widget_layout_w"])
        assert abs(cell["widget_layout_y"] - seeded["widget_layout_y"]) <= 1
        assert abs(cell["widget_layout_h"] - seeded["widget_layout_h"]) <= 1
    assert sorted(entries) == ["custom_dashboards.json", "meta.json"]
    assert json.loads(entries["meta.json"]) == {"schema": {"kind": "jsonapi", "version": 1}}


def test_an_openaev_export_writes_its_entity_inputs_as_parameters_and_leaves_out_what_reads_another():
    series = lambda token: [{"name": "x", "filter": {"mode": "and", "filters": [{"key": "base_scenario_side", "values": [token], "operator": "eq", "mode": "or"}]}}]  # noqa: E731
    widget = lambda wid, token: {  # noqa: E731
        "kind": "platform", "id": wid, "source": "openaev", "integration_id": INTEGRATION,
        "native": {"widget_type": "number", "widget_config": {"widget_configuration_type": "flat", "series": series(token)}},
    }  # fmt: skip
    inputs = [
        {"type": "input.entity", "title": "Scenario", "token": "scenario", "options": {"source": "openaev", "entityTypes": ["scenario"], "required": False}},
        {"type": "input.entity", "title": "Country", "token": "country", "options": {"source": "opencti", "entityTypes": ["Country"]}},
    ]
    document, report = build_export_zip("Board", [widget("a", "$scenario$"), widget("b", "$country$")], inputs=inputs)
    written = json.loads(read_zip_entries(document)["custom_dashboards.json"])
    parameters = [r for r in written["included"] if r["type"] == "custom_dashboards_parameters"]
    assert [p["attributes"] for p in parameters] == [
        {"custom_dashboards_parameter_id": "p1", "custom_dashboards_parameter_name": "Scenario", "custom_dashboards_parameter_type": "scenario"}
    ]
    assert written["data"]["relationships"]["custom_dashboard_parameters"]["data"] == [{"id": "p1", "type": "custom_dashboards_parameters"}]
    [kept] = [r for r in written["included"] if r["type"] == "widgets"]
    assert kept["attributes"]["widget_config"]["series"][0]["filter"]["filters"][0]["values"] == ["p1"]
    assert report.notes == ["Widget 'b' reads the dashboard's input $country$, which OpenAEV cannot set, so it was left out."]


def test_two_openaev_parameters_of_one_type_are_two_inputs():
    parameters = [("p-a", "simulation", "Last drill"), ("p-b", "simulation", ""), ("p-c", "timeRange", "Time range")]
    from app.services.dashboard_interop import ConversionReport
    from app.services.dashboard_interop.openaev import _parameter_inputs

    tokens, inputs = _parameter_inputs(parameters, ConversionReport())
    assert tokens == {"p-a": "simulation", "p-b": "simulation_2"}
    assert [(i["title"], i["token"]) for i in inputs] == [("Last drill", "simulation"), ("Simulation", "simulation_2")]


def test_an_openaev_range_bound_to_the_dashboard_dates_uses_its_own_range():
    document = {
        "data": {
            "id": "d",
            "type": "custom_dashboards",
            "attributes": {"custom_dashboard_name": "D"},
            "relationships": {
                "custom_dashboard_widgets": {"data": [{"id": "w", "type": "widgets"}]},
                "custom_dashboard_parameters": {"data": [{"id": "p-start", "type": "custom_dashboards_parameters"}]},
            },
        },
        "included": [
            {"id": "p-start", "type": "custom_dashboards_parameters", "attributes": {"custom_dashboards_parameter_type": "startDate"}},
            {"id": "w", "type": "widgets", "attributes": {"widget_type": "number", "widget_config": _openaev_widget(time_range="CUSTOM", start="p-start")["widget_config"], "widget_layout": {}}},
        ],
    }
    parsed = parse_any(_openaev_zip(document))
    assert parsed.widgets[0]["native"]["widget_config"]["time_range"] == "DEFAULT"
    assert "start" not in parsed.widgets[0]["native"]["widget_config"]
    assert parsed.report.notes == ["Widget 1 (Vulnerable endpoints): its range followed the dashboard's dates; it now uses its own time range."]


def test_what_openaev_cannot_draw_is_left_out_of_its_export_and_said():
    with pytest.raises(InteropError) as refused:
        build_export_zip("D", [{"kind": "text", "id": "t", "body": "x"}])
    assert str(refused.value) == "This dashboard has no OpenAEV widget to export."


# ── Packs and hostile files ──────────────────────────────────────────────────


def test_a_pack_keeps_platform_widgets_unbound_and_binds_them_on_insert():
    live = validate_pack_widget({"kind": "platform", "source": "opencti", "native": _opencti_widget()})
    assert "integration_id" not in live
    document = build_pack("SOC pack", "Ready-made", [{"kind": "text", "id": "t", "body": "x"}, {**live, "integration_id": INTEGRATION}])
    assert document["format"] == PACK_FORMAT and "integration_id" not in document["widgets"][1]
    parsed = parse_any(json.dumps(document).encode())
    assert (parsed.origin, parsed.name, parsed.description) == ("xtm-one-pack", "SOC pack", "Ready-made")
    assert bind_widget(parsed.widgets[1], INTEGRATION)["integration_id"] == INTEGRATION
    with pytest.raises(DashboardSpecError):
        bind_widget(parsed.widgets[1], None)


def test_a_pack_leaves_out_what_it_cannot_read_and_says_so():
    document = {
        "format": PACK_FORMAT,
        "version": 1,
        "name": "P",
        "widgets": [{"kind": "text", "id": "a", "body": "x"}, {"kind": "text", "id": "a", "body": "y"}, {"kind": "html", "body": "<b>"}],
    }
    parsed = parse_any(json.dumps(document).encode())
    assert [w["id"] for w in parsed.widgets] == ["a", "text"]
    assert parsed.report.notes[0].startswith("Widget 3 was left out:")
    assert parsed.report.notes[1] == "Two widgets had the id 'a'; the second was renamed."


def test_a_pack_keeps_its_first_widgets_only():
    document = {"format": PACK_FORMAT, "version": 1, "name": "P", "widgets": [{"kind": "text", "body": "x"}] * (MAX_PACK_WIDGETS + 5)}
    parsed = parse_any(json.dumps(document).encode())
    assert len(parsed.widgets) == MAX_PACK_WIDGETS
    assert parsed.report.notes[0] == f"The pack holds {MAX_PACK_WIDGETS + 5} widgets; the first {MAX_PACK_WIDGETS} were kept."


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not json", "is not valid JSON"),
        (json.dumps({"hello": 1}).encode(), "is not a widget pack, an OpenCTI"),
        (json.dumps({"format": PACK_FORMAT, "version": 3, "name": "P", "widgets": []}).encode(), "this platform reads versions 1 and 2"),
        (b"PK\x03\x04garbage", "not a readable ZIP"),
        (_zip({"bomb.json": b"0" * (4 * 1024 * 1024 + 1)}), "expands beyond 4 MB"),
        (_zip({f"{i}.json": b"{}" for i in range(17)}), "more than 16 entries"),
        (_zip({"readme.txt": b"x"}), "neither a widget pack nor an OpenAEV dashboard export"),
        (b"{" * 6 * 1024 * 1024, "larger than 5 MB"),
        # Deep nesting parses, and is not a format this reads.
        (b"[" * 100000 + b"]" * 100000, "is not a widget pack"),
    ],
    ids=["text", "unknown-json", "pack-version", "bad-zip", "zip-bomb", "zip-entries", "zip-no-json", "too-large", "deep-json"],
)
def test_a_hostile_file_is_refused_with_a_reason(data, message):
    with pytest.raises(InteropError) as refused:
        parse_any(data)
    assert message in str(refused.value)


# ── Layout ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("columns", "width"), [(2, "third"), (4, "third"), (5, "half"), (6, "half"), (8, "full"), (12, "full")])
def test_a_reader_without_the_grid_gets_the_closest_width(columns, width):
    assert width_of(columns) == width


def test_opencti_rows_are_the_dashboard_s_and_openaev_rows_come_back_within_one():
    # The dashboard's grid is OpenCTI's (D8): its rows are kept as they are.
    assert ROW_PITCH["opencti"] == ROW_PITCH["xtm"]
    assert [rows_to_platform(rows_from_platform(rows, "opencti"), "opencti") for rows in range(1, 60)] == list(range(1, 60))
    # OpenAEV's rows are shorter: a row count comes back within one.
    assert all(abs(rows_to_platform(rows_from_platform(rows, "openaev"), "openaev") - rows) <= 1 for rows in range(1, 60))


def test_widgets_are_read_top_to_bottom_left_to_right():
    assert reading_order([(6, 0), (0, 0), (None, None), (0, 4)]) == [1, 0, 3, 2]


def test_a_pack_file_of_version_1_is_read_in_opencti_rows_and_one_written_now_is_version_2():
    from app.services.dashboard_interop.pack_format import PACK_VERSION, build_pack, parse_pack_document

    widget = {"kind": "text", "id": "note", "body": "hi", "layout": {"x": 0, "y": 8, "w": 6, "h": 8}}
    old = parse_pack_document({"format": PACK_FORMAT, "version": 1, "name": "Old", "widgets": [widget]})
    # Rows of 36 px, read in rows of 82 px: 8-16 is 4-7.
    assert old.widgets[0]["layout"] == {"x": 0, "y": 4, "w": 6, "h": 3}
    written = build_pack("New", None, old.widgets)
    assert written["version"] == PACK_VERSION == 2
    assert parse_pack_document(written).widgets[0]["layout"] == {"x": 0, "y": 4, "w": 6, "h": 3}
