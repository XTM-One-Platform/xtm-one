"""The chart widgets of OpenCTI and OpenAEV as their adapters hand them to the engine.

``tests/fixtures/dashboards/module_charts.json`` records each module's answer
for a chart widget of every type, the closed data its adapter makes of it and
the presentation preset it sends (the frontend draws the same records:
``charts/ChartKinds.test.tsx``). Here: the adapters still make exactly that,
every preset validates against its view's options and is the view the widget
is drawn with, and what the data carries besides values (colours, keys, links)
is bounded and as the modules colour it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services.dashboard_definition import TYPE_TO_VIEW
from app.services.dashboard_live import MAX_ITEMS, distribution_data, openaev, opencti, presentation_of
from app.services.dashboard_live.opencti_colors import item_color, string_to_colour
from app.services.dashboard_platform import check_native, resolve_view
from app.services.dashboard_visualization_options import NAMED_COLORS, validate_options

FIXTURE = json.loads((Path(__file__).parent / "fixtures/dashboards/module_charts.json").read_text())
CASES = {case["name"]: case for case in FIXTURE["cases"]}
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalized(case: dict) -> tuple[dict, str]:
    check = check_native(case["source"], case["native"])
    if case["source"] == "opencti":
        return opencti.normalize(check.native, check.shape, case["answer"], FIXTURE["base_url"]), check.shape
    return openaev.normalize(check.native, case["answer"]), check.shape


@pytest.mark.parametrize("name", list(CASES))
def test_the_adapter_makes_the_recorded_data_and_preset(name):
    case = CASES[name]
    data, shape = _normalized(case)
    assert (shape, data) == (case["shape"], case["data"])
    check = check_native(case["source"], case["native"])
    assert presentation_of(case["source"], check.native) == case["presentation"]


@pytest.mark.parametrize("name", list(CASES))
def test_every_preset_is_valid_and_is_the_view_the_widget_is_drawn_with(name):
    case = CASES[name]
    check = check_native(case["source"], case["native"])
    preset = case["presentation"]
    # A preset the frontend would lay under nothing is a dead preset: it names the widget's own view.
    assert TYPE_TO_VIEW[preset["view"]] == resolve_view(check, None) == case["view"]
    assert validate_options(preset["view"], preset["options"]) == preset["options"]


@pytest.mark.parametrize("name", list(CASES))
def test_colours_keys_and_links_are_bounded(name):
    data = CASES[name]["data"]
    records = data.get("items") or [p for s in data.get("series", []) for p in s["points"]] + data.get("series", [])
    for record in records:
        if "color" in record:
            assert record["color"] in NAMED_COLORS or _HEX.match(record["color"]), record
        if "key" in record:
            assert 0 < len(record["key"]) <= 100
        if "link" in record:
            assert record["link"].startswith(FIXTURE["base_url"] + "/dashboard/id/")


def test_opencti_colours_its_bars_by_entity_and_its_donuts_by_their_own_colours():
    bars = CASES["opencti-horizontal-bar"]["data"]["items"]
    assert [(i["label"], i["color"], "link" in i) for i in bars] == [
        ("Northwind CERT", "#BA88FF", True),
        ("Jane Analyst", "#BA88FF", True),
        # A restricted entity takes its family's grey and is not opened.
        ("Restricted", "#424242", False),
    ]
    labels = CASES["opencti-donut-labels"]["data"]["items"]
    # Each label its own colour; one without (shifted out by OpenCTI, a defect) takes the palette's.
    assert [i.get("color") for i in labels] == ["#ff0000", "#ffffff", None]
    assert [i["color"] for i in CASES["opencti-donut-boolean"]["data"]["items"]] == ["#66bb6a", "#ef5350"]
    # A radar, a polar area of types, a tree, a word cloud: no colour of their own (the palette draws them).
    for name in ("opencti-radar", "opencti-polar-area", "opencti-tree", "opencti-wordcloud"):
        assert all("color" not in i for i in CASES[name]["data"]["items"]), name


def test_opencti_presets_follow_the_widget_s_parameters():
    def preset(native: dict) -> dict:
        return presentation_of("opencti", check_native("opencti", native).native)["options"]

    line = CASES["opencti-line"]["native"]
    assert (preset(line)["xAxisType"], preset(line)["dateFormat"], preset(line)["legendDisplay"]) == ("category", "monthYear", "bottom")
    area = CASES["opencti-area"]["native"]
    assert (preset(area)["xAxisType"], preset(area)["dateFormat"], preset(area)["stackMode"], preset(area)["fill"]) == ("time", "date", "stacked", "gradient")
    tree = CASES["opencti-tree"]["native"]
    assert (preset(tree).get("colorByPoint"), preset(tree).get("valueShades")) == (True, None)
    plain_tree = {**tree, "parameters": {}}
    assert (preset(plain_tree).get("colorByPoint"), preset(plain_tree)["valueShades"], preset(plain_tree)["paletteLead"]) == (None, True, "accent")
    bars = {**CASES["opencti-horizontal-bar"]["native"], "parameters": {"distributed": True}}
    assert (preset(bars)["colorByPoint"], preset(bars)["openLinks"], preset(bars)["barSize"]) == (True, True, 30)


def test_openaev_colours_by_meaning_server_side():
    temporal = CASES["openaev-vertical-bar-temporal"]["data"]["series"]
    assert [(s["name"], s["color"]) for s in temporal] == [("Not Detected", "#f14337"), ("Prevented", "success")]
    structural = CASES["openaev-vertical-bar-structural"]["data"]["items"]
    assert {i["color"] for i in structural} == {"#f14337"}
    assert [i["key"] for i in structural] == ["sp-1", "sp-2", "sp-3"]
    status = CASES["openaev-donut-status"]["data"]["items"]
    assert [(i["label"], i["color"]) for i in status] == [("SUCCESS", "success"), ("FAILED", "#f14337"), ("PENDING", "#9e9e9e")]
    radar = CASES["openaev-posture-radar"]["data"]["series"]
    # A miss is red, NOT DETECTED included (OpenAEV draws it green: a defect drawn as intended); a status named
    # as OpenAEV translates it in the legend, another label as it comes.
    assert [(s["name"], s["color"]) for s in radar] == [("Success", "success"), ("Failed", "#f14337"), ("NOT DETECTED", "#f14337")]
    # The line uses no meaning: its series take the palette.
    assert all("color" not in s for s in CASES["openaev-line"]["data"]["series"])
    # Horizontal bars of one series: a colour per bar from the palette, none in the data.
    assert all("color" not in i for i in CASES["openaev-horizontal-bar"]["data"]["items"])


@pytest.mark.parametrize(
    ("names", "colors"),
    [
        (["Detected TTPs", "Prevented TTPs"], ["#00f0bc", "success"]),
        (["UNDETECTED", "unprevented", "Missed", "PENDING"], ["#f14337", "#ff9800", "#f14337", "#75829a"]),
        (["Findings", "Injects"], None),
        (["Findings", "SUCCESS", "Other", "x", "y"], ["accent", "success", "#ff9800", "#ff7043", "accent"]),
    ],
)
def test_openaev_semantic_series_colours(names, colors):
    assert openaev._semantic_colors(names) == colors


def test_openaev_presets_follow_the_series():
    def preset(native: dict) -> dict:
        return presentation_of("openaev", check_native("openaev", native).native)["options"]

    one = CASES["openaev-horizontal-bar"]["native"]
    assert (preset(one)["colorByPoint"], preset(one)["dataValuesDisplay"], preset(one)["legendDisplay"]) == (True, "all", "off")
    two = json.loads(json.dumps(one))
    two["widget_config"]["series"] = two["widget_config"]["series"] * 2
    assert ("colorByPoint" in preset(two), preset(two)["legendDisplay"]) == (False, "bottom")
    line = CASES["openaev-line"]["native"]
    assert preset(line)["palette"] == "material"
    single = json.loads(json.dumps(line))
    single["widget_config"]["series"] = single["widget_config"]["series"][:1]
    assert (preset(single)["paletteLead"], "palette" in preset(single)) == ("accent", False)
    structural = CASES["openaev-vertical-bar-structural"]["native"]
    assert (preset(structural)["emptySample"], "xAxisType" in preset(structural)) == ("platforms", False)
    assert preset(CASES["openaev-vertical-bar-temporal"]["native"])["emptySample"] == "temporal"


def test_types_without_a_chart_have_no_chart_preset():
    assert opencti._chart_preset({"type": "number"}) is None
    assert openaev._chart_preset({"widget_type": "list", "widget_config": {}}) is None


def test_item_keys_are_kept_and_cut():
    data = distribution_data([("a", 1, {"key": "k" * 300, "color": "url(x)", "link": "javascript:alert(1)"})] * (MAX_ITEMS + 5))
    assert len(data["items"]) == MAX_ITEMS
    assert data["items"][0] == {"label": "a", "value": 1.0, "key": "k" * 100}


@pytest.mark.parametrize(
    ("text", "colour"),
    # Checked against OpenCTI's ``stringToColour`` run by node.
    [("IPv4-Addr", "#f92e56"), ("Domain-Name", "#f43dcf"), ("APT28", "#0b97b9"), ("é😀x", "#ec89b0"), ("true", "#2e7d32"), ("", "#5d4037")],
)
def test_string_colours_are_opencti_s(text, colour):
    assert string_to_colour(text) == colour


def test_entity_colours_are_opencti_s():
    assert [item_color(t) for t in ("Malware", "Intrusion-Set", "Report", "uses", "Country", "Restricted")] == [
        "#F0B60A", "#FF9800", "#70B23B", "#616161", "#05ACC1", "#424242",
    ]
    # An observable's and an unknown string's colour is hashed.
    assert item_color("IPv4-Addr") == "#f92e56"
    assert item_color("false", reversed_=True) == "#2e7d32"
