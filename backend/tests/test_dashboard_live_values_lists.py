"""The values, lists, cards, timelines, maps and texts of the modules' dashboards, as their adapters send them.

The engine draws what the adapters send (``dev-docs/dashboard-rendering-reference.md``):
OpenCTI's and OpenAEV's numbers, lists, distribution list, timeline, bookmarks
and map become generic data - typed cells, entity colours, icons and types,
links built here from the integration's base URL and an id - and a
presentation preset of generic options that a person could set.
"""

from __future__ import annotations

import pytest

from app.services import dashboard_live
from app.services.custom_dashboard_spec import DashboardSpecError, validate_spec
from app.services.dashboard_live import MAX_COLUMNS, MAX_ROWS, MAX_SECONDARY, presentation_of
from app.services.dashboard_live import openaev as live_openaev
from app.services.dashboard_live import opencti as live_opencti
from app.services.dashboard_live.opencti_entities import (
    entity_chip,
    entity_color,
    entity_icon,
    entity_label,
    marking_chip,
)
from app.services.dashboard_platform import check_native
from app.services.dashboard_visualization_options import validate_options

BASE = "https://cti.example"
SERIES = [{"name": "x", "filter": {"mode": "and", "filters": [{"key": "base_entity", "operator": "eq", "values": ["finding"], "mode": "or"}]}}]


def _cti(widget_type: str, perspective: str = "entities", **selection) -> dict:
    return check_native(
        "opencti", {"type": widget_type, "perspective": perspective, "dataSelection": [{"attribute": "entity_type", **selection}]}
    ).native


def _aev(widget_type: str, config: dict) -> dict:
    return check_native("openaev", {"widget_type": widget_type, "widget_config": config}).native


# ── Presets ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("native", "view", "options"),
    [
        # Headed by its type, as OpenCTI's card; its own title in the body, beside the difference.
        (_cti("number"), "xtm.singlevalue", {"valueStyle": "difference", "headerTitle": "Entities number", "topLabel": "Entities number"}),
        (_cti("list"), "xtm.list", {"listStyle": "table", "rowHeight": 48, "arrowColor": "accent"}),
        (_cti("bookmark"), "xtm.list", {"listStyle": "cards", "cardsPerRow": 3}),
        (_cti("distribution-list"), "xtm.distribution", {"listStyle": "rows"}),
        (_cti("timeline"), "xtm.timeline", {"timelineStyle": "alternate"}),
    ],
    ids=["number", "list", "bookmark", "distribution-list", "timeline"],
)
def test_an_opencti_widget_is_presented_as_opencti_draws_it(native, view, options):
    # Every OpenCTI widget's title capitalized (CardTitle), and OpenCTI's sentence when it has nothing to show.
    assert presentation_of("opencti", native) == {"view": view, "options": {**options, "titleStyle": "capitalize", "emptyMessage": "found", "accentColor": "brand"}}


def test_an_opencti_number_names_what_it_counts_in_its_body():
    native = _cti("number")
    native["parameters"] = {"title": "Number of Vulnerabilities"}
    relationships = _cti("number", "relationships")
    assert presentation_of("opencti", native)["options"]["topLabel"] == "Number of Vulnerabilities"
    assert presentation_of("opencti", relationships)["options"]["headerTitle"] == "Relationships number"


def test_an_opencti_map_opens_one_zoom_level_below_its_own_on_its_centre():
    centred = presentation_of("opencti", _cti("map", "relationships", zoom=4, centerLat=50.5, centerLng=4.4))
    assert centred is not None and centred["view"] == "xtm.map"
    assert centred["options"] == {
        "titleStyle": "capitalize",
        "emptyMessage": "found",
        "accentColor": "brand",
        "layers": ["choropleth", "marker"],
        "regionColors": "level",
        "basemap": "contrast",
        "markerIcon": "building-2",
        "markerColor": "#d95f00",
        "markerSize": 25,
        "zoom": 3.0,
        "centerLat": 50.5,
        "centerLng": 4.4,
    }
    # No centre: the first city, else Paris, at OpenCTI's default zoom (2, opened at 1).
    plain = presentation_of("opencti", _cti("map", "relationships"))["options"]
    assert (plain["zoom"], plain["centerLat"], plain["centerLng"], plain["centerOnFirstMarker"]) == (1.0, 48.8566969, 2.3514616, True)
    # A centre of 0 is no centre, as OpenCTI reads it (``centerLat && centerLng``).
    assert presentation_of("opencti", _cti("map", "relationships", centerLat=0, centerLng=10))["options"]["centerOnFirstMarker"] is True


@pytest.mark.parametrize(
    ("native", "options"),
    [
        (_aev("number", {"widget_configuration_type": "flat", "series": SERIES}), {"valueStyle": "tile", "valueInfo": "header"}),
        (_aev("list", {"widget_configuration_type": "list", "perspective": {"name": "x", "filter": {}}}), {"listStyle": "rows", "rowHeight": 50}),
    ],
    ids=["number", "list"],
)
def test_an_openaev_widget_is_presented_as_openaev_draws_it(native, options):
    view = "xtm.singlevalue" if options.get("valueStyle") else "xtm.list"
    assert presentation_of("openaev", native) == {"view": view, "options": {**options, "titleStyle": "caps", "emptyMessage": "display", "accentColor": "brand"}}


def test_every_preset_is_what_a_person_could_set():
    natives = [_cti(t) for t in ("number", "list", "bookmark", "distribution-list", "timeline")]
    natives += [_cti("map", "relationships", zoom=6, centerLat=1.5, centerLng=2.5)]
    for native in natives:
        preset = live_opencti.presentation(native)
        assert validate_options(preset["view"], preset["options"]) == preset["options"]
    for native in (_aev("number", {"widget_configuration_type": "flat", "series": SERIES}),):
        preset = live_openaev.presentation(native)
        assert validate_options(preset["view"], preset["options"]) == preset["options"]


@pytest.mark.parametrize(
    ("view", "options"),
    [
        ("xtm.singlevalue", {"valueStyle": "huge"}),
        ("xtm.list", {"listStyle": "grid"}),
        ("xtm.list", {"rowHeight": 200}),
        ("xtm.list", {"cardsPerRow": 9}),
        ("xtm.distribution", {"listStyle": "cards"}),
        ("xtm.timeline", {"timelineStyle": "zigzag"}),
        ("xtm.map", {"regionColors": "rainbow"}),
        ("xtm.map", {"markerColor": "url(#x)"}),
        ("xtm.map", {"markerIcon": "not-an-icon"}),
        ("xtm.map", {"zoom": 30}),
        ("xtm.map", {"centerLat": 91}),
    ],
    ids=["value", "list", "row-height", "cards", "distribution", "timeline", "regions", "marker-color", "marker-icon", "zoom", "lat"],
)
def test_an_option_outside_its_bounds_is_refused(view, options):
    with pytest.raises(ValueError, match="options"):
        validate_options(view, options)


def test_a_preset_that_does_not_validate_is_dropped(monkeypatch):
    monkeypatch.setattr(live_opencti, "presentation", lambda _native: {"view": "xtm.list", "options": {"listStyle": "javascript"}})
    assert presentation_of("opencti", _cti("list")) is None


# ── OpenCTI's entity vocabulary ──────────────────────────────────────────────


def test_an_entity_is_coloured_as_opencti_s_item_color():
    # A family's colour; a cyber observable's and an unknown type's from the hash of its name
    # (the values OpenCTI's ``stringToColour`` gives in a browser).
    assert [entity_color(t) for t in ("Malware", "Intrusion-Set", "Report", "Country", "uses")] == [
        "#F0B60A", "#FF9800", "#70B23B", "#05ACC1", "#616161",
    ]  # fmt: skip
    assert (entity_color("IPv4-Addr"), entity_color("Url"), entity_color("default")) == ("#f92e56", "#4f4d01", "#41d613")
    assert entity_color(None) == "#5d4037"


def test_an_entity_type_is_named_drawn_and_chipped_as_opencti_does():
    assert [entity_label(t) for t in ("Intrusion-Set", "Attack-Pattern", "Case-Incident", "StixFile", "Url", None)] == [
        "Intrusion Set", "Attack Pattern", "Incident response", "File", "URL", "Unknown",
    ]  # fmt: skip
    assert (entity_icon("Malware"), entity_icon("intrusion-set"), entity_icon("Something-New")) == ("biohazard", "gem", "sparkles")
    assert entity_chip("Vulnerability") == {"label": "Vulnerability", "color": "#F0B60A", "icon": "bug"}
    assert entity_chip(None) is None


@pytest.mark.parametrize(
    ("marking", "chip"),
    [
        ({"definition": "TLP:RED"}, {"label": "TLP:RED", "color": "danger"}),
        ({"definition": "TLP:AMBER+STRICT"}, {"label": "TLP:AMBER+STRICT", "color": "alert"}),
        ({"definition": "PAP:GREEN"}, {"label": "PAP:GREEN", "color": "success"}),
        ({"definition": "SF"}, {"label": "SF", "color": "info"}),
        ({"definition": "NONE"}, {"label": "NONE"}),
        ({"definition": "CUSTOM"}, {"label": "CUSTOM", "color": "neutral"}),
        ({"definition": "TLP:RED", "x_opencti_color": "#123456"}, {"label": "TLP:RED", "color": "#123456"}),
        ({"definition": "TLP:RED", "x_opencti_color": "red; background:url(x)"}, {"label": "TLP:RED", "color": "danger"}),
    ],
    ids=["red", "amber", "green", "sf", "none", "unknown", "own", "own-invalid"],
)
def test_a_marking_is_coloured_as_item_markings(marking, chip):
    assert marking_chip(marking) == chip


# ── OpenCTI: numbers, lists, timelines, bookmarks ────────────────────────────


NODE = {
    "id": "r-1",
    "entity_type": "Report",
    "created_at": "2026-10-01T10:00:00Z",
    "created": "2026-09-30T08:00:00Z",
    "modified": "2026-10-02T08:00:00Z",
    "representative": {"main": "Q3 threat landscape", "secondary": "**Summary** " + "x" * 400},
    "createdBy": {"name": "ACME CERT"},
    "creators": [{"name": "alice"}, {"name": "bob"}],
    "status": {"template": {"name": "Analyzed", "color": "#4caf50"}},
    "objectLabel": [{"id": "l1", "value": "apt", "color": "#ff0000"}, {"id": "l2", "value": "ransomware", "color": "javascript:x"}],
    "objectMarking": [{"id": "m1", "definition": "TLP:AMBER", "x_opencti_color": None}],
    # What an answer says is never a link: rows link to the entity by its id, on the integration.
    "url": "javascript:alert(1)",
    "_url": "https://evil.example",
}


def test_an_opencti_entity_list_types_every_default_column():
    rows = live_opencti._rows(BASE, _cti("list"), {"edges": [{"node": NODE}], "pageInfo": {"globalCount": 1}})
    assert rows["rows"] == [
        {
            "entity_type": {"label": "Report", "color": "#70B23B", "icon": "file-text"},
            "name": "Q3 threat landscape",
            "created": "2026-09-30T08:00:00Z",
            "created_at": "2026-10-01T10:00:00Z",
            "modified": "2026-10-02T08:00:00Z",
            "createdBy": "ACME CERT",
            "creators": "alice, bob",
            "x_opencti_workflow_id": {"label": "Analyzed", "color": "#4caf50"},
            # A label's colour that is not a colour is left out; the chip keeps its name.
            "objectLabel": [{"label": "apt", "color": "#ff0000"}, {"label": "ransomware"}],
            "objectMarking": [{"label": "TLP:AMBER", "color": "alert"}],
            "_url": f"{BASE}/dashboard/id/r-1",
            "_entityType": "Report",
        }
    ]


def test_an_opencti_list_keeps_at_most_its_columns_and_rows():
    native = _cti("list")
    native["dataSelection"][0]["columns"] = [{"attribute": f"field_{i}"} for i in range(20)]
    edges = [{"node": {**NODE, "id": f"r-{i}"}} for i in range(MAX_ROWS + 10)]
    rows = live_opencti._rows(BASE, native, {"edges": edges})
    assert len(rows["columns"]) == MAX_COLUMNS == 12
    assert len(rows["rows"]) == MAX_ROWS


def test_an_opencti_entity_timeline_is_dated_and_carries_its_second_line():
    native = _cti("timeline", date_attribute="modified")
    rows = live_opencti._rows(BASE, native, {"edges": [{"node": NODE}], "pageInfo": {"globalCount": 4}})
    assert rows["columns"] == [
        {"key": "date", "label": "Date", "kind": "datetime"},
        {"key": "name", "label": "Name"},
        {"key": "secondary", "label": "Description"},
    ]
    (row,) = rows["rows"]
    assert row["date"] == "2026-10-02T08:00:00Z"
    assert row["name"] == "Q3 threat landscape"
    assert row["secondary"].startswith("**Summary** x") and len(row["secondary"]) <= dashboard_live.MAX_STRING
    assert (row["_color"], row["_icon"], row["_entityType"], row["_url"]) == ("#70B23B", "file-text", "Report", f"{BASE}/dashboard/id/r-1")
    assert rows["total"] == 4


def test_a_relationship_timeline_shows_each_relationship_s_remote_end():
    filters = {"mode": "and", "filters": [{"key": "fromId", "values": ["apt28"], "operator": "eq", "mode": "or"}], "filterGroups": []}
    native = _cti("timeline", "relationships", filters=filters)
    relationship = {
        "id": "rel", "entity_type": "uses", "created_at": "2026-10-01T10:00:00Z",
        "from": {"id": "apt28", "entity_type": "Intrusion-Set", "representative": {"main": "APT28"}},
        "to": {"id": "xa", "entity_type": "Malware", "representative": {"main": "X-Agent", "secondary": "An implant"}},
    }  # fmt: skip
    (row,) = live_opencti._rows(BASE, native, {"edges": [{"node": relationship}]})["rows"]
    assert (row["name"], row["secondary"], row["_entityType"], row["_url"]) == ("X-Agent", "An implant", "Malware", f"{BASE}/dashboard/id/xa")
    # Without a fromId naming the source, the source is the remote end.
    (other,) = live_opencti._rows(BASE, _cti("timeline", "relationships"), {"edges": [{"node": relationship}]})["rows"]
    assert other["name"] == "APT28"


def test_the_events_a_timeline_draws_keep_a_second_line_of_at_most_150_characters():
    data = dashboard_live.events_data([("A", "2026-10-01", None, None, None, {"secondary": "y" * 400, "icon": "Bug", "color": "#70B23B"})])
    (event,) = data["events"]
    assert len(event["secondary"]) == MAX_SECONDARY == 150
    assert (event["icon"], event["color"]) == ("bug", "#70B23B")


def test_a_row_s_colour_and_icon_are_validated():
    data = dashboard_live.rows_data([("name", "Name")], [{"name": "x", "_color": "red;x", "_icon": "nope-nope"}, {"name": "y", "_color": "#abc", "_icon": "Bug"}])
    assert data["rows"] == [{"name": "x"}, {"name": "y", "_color": "#abc", "_icon": "bug"}]


# ── OpenCTI: distribution list ───────────────────────────────────────────────


def _distribution(native: dict, items: list[dict]) -> list[dict]:
    answer = {"data": {"stixCoreObjectsDistribution": items}}
    return live_opencti.normalize(native, "distribution", answer, BASE)["items"]


def test_a_distribution_list_by_type_names_each_type_and_draws_the_default_icon():
    items = _distribution(_cti("distribution-list"), [{"label": "Intrusion-Set", "value": 12, "entity": None}])
    # The type's translation (OpenCTI shows its raw ``entity_<type>`` key: drawn as intended), the default
    # circle in that name's hash colour, and no link: the item is not an entity.
    assert items == [{"label": "Intrusion Set", "value": 12.0, "color": "#41d613", "entityType": "Intrusion-Set", "icon": "circle"}]


def test_a_distribution_list_of_entities_links_each_to_its_page():
    native = _cti("distribution-list", attribute="created-by.internal_id")
    native["dataSelection"][0]["attribute"] = "createdBy_id"
    items = _distribution(
        native,
        [
            {"label": "o1", "value": 5, "entity": {"id": "o 1", "entity_type": "Organization", "representative": {"main": "ACME"}}},
            {"label": "l1", "value": 3, "entity": {"id": "l1", "entity_type": "Label", "value": "apt", "color": "#ff0000"}},
            {"label": "u1", "value": 2, "entity": {"id": "u1", "entity_type": "User", "name": "Jane"}},
            {"label": "x", "value": 1, "entity": {"id": "x", "entity_type": "Malware", "representative": {"main": "Restricted"}}},
        ],
    )
    assert items[0] == {"label": "ACME", "value": 5.0, "color": "#BA88FF", "entityType": "Organization", "icon": "building-2", "link": f"{BASE}/dashboard/id/o%201"}
    # An entity's own colour wins.
    assert items[1]["color"] == "#ff0000"
    # A user, and a restricted entity, are not linked.
    assert "link" not in items[2] and "link" not in items[3]


def test_another_opencti_distribution_keeps_its_plain_items():
    items = _distribution(_cti("donut"), [{"label": "Malware", "value": 2, "entity": None}])
    assert items == [{"label": "Malware", "value": 2.0}]


# ── OpenCTI: map levels ──────────────────────────────────────────────────────


def test_a_map_ranks_its_countries_from_level_0_to_9():
    countries = [
        {"label": code, "value": value, "entity": {"entity_type": "Country", "name": code, "x_opencti_aliases": [code]}}
        for code, value in (("US", 100), ("FR", 50), ("DE", 1))
    ]
    regions = live_opencti._geo(countries)["regions"]
    # Over [1, 101): 9 * 99 / 100 = 8.91 -> 8; 9 * 49 / 100 -> 4; the least is 0 (drawn as level 5 by the map).
    assert [(r["code"], r["level"]) for r in regions] == [("US", 8), ("FR", 4), ("DE", 0)]
    assert all(0 <= r["level"] <= 9 for r in regions)


def test_a_map_finds_a_country_by_its_alpha_3_alias_as_opencti_s_map_does():
    countries = [
        {"label": n, "value": v, "entity": {"entity_type": "Country", "name": n, "x_opencti_aliases": aliases}}
        for n, v, aliases in (
            ("Germany", 9, ["DEU"]),
            ("United Kingdom", 8, ["Great Britain", "gbr"]),
            ("Réunion", 7, ["REU"]),
            ("Atlantis", 6, ["ATL", "Lost city"]),
        )
    ]
    regions = live_opencti._geo(countries)["regions"]
    assert [(r["code"], r["label"]) for r in regions] == [("DE", "Germany"), ("GB", "United Kingdom"), ("RE", "Réunion")]


def test_every_alpha_3_code_names_one_of_the_alpha_2_codes():
    from app.services.country_codes import ISO_3166_ALPHA2, ISO_3166_ALPHA3_TO_ALPHA2

    assert set(ISO_3166_ALPHA3_TO_ALPHA2.values()) == set(ISO_3166_ALPHA2)
    assert len(ISO_3166_ALPHA3_TO_ALPHA2) == len(ISO_3166_ALPHA2) == 249
    assert all(len(code) == 3 and code.isalpha() and code.isupper() for code in ISO_3166_ALPHA3_TO_ALPHA2)


# ── OpenAEV ──────────────────────────────────────────────────────────────────



@pytest.mark.parametrize(
    ("entity", "icon"),
    [("scenario", {"name": "clapperboard", "color": "#00f0bc"}), ("finding", {"name": "binoculars", "color": "danger"}), ("team", {"name": "hash", "color": "accent"})],
    ids=["scenario", "finding", "other"],
)
def test_an_openaev_number_has_its_entity_s_tile(entity, icon):
    series = [{"name": "x", "filter": {"mode": "and", "filters": [{"key": "base_entity", "operator": "eq", "values": [entity], "mode": "or"}]}}]
    native = _aev("number", {"widget_configuration_type": "flat", "series": series})
    data = live_openaev.normalize(native, {"interval_count": 1500, "previous_interval_count": 1400, "difference_count": 100})
    # What it counts is said beside its change only when the engine has words for it.
    subject = {"subject": entity} if entity != "team" else {}
    assert data == {"value": 1500.0, "change": 100.0, "previous": 1400.0, "period": "interval", "icon": icon, **subject}


@pytest.mark.parametrize(
    ("own", "asked", "span"),
    [("LAST_QUARTER", None, "3m"), ("DEFAULT", "LAST_DAY", "1d"), ("LAST_YEAR", "CUSTOM", "custom"), ("ALL_TIME", None, "all"), ("DEFAULT", None, None)],
    ids=["own", "dashboard", "fixed", "all-time", "unknown"],
)
def test_an_openaev_number_says_over_which_span_it_changed(own, asked, span):
    series = [{"name": "x", "filter": {"mode": "and", "filters": [{"key": "base_entity", "operator": "eq", "values": ["finding"], "mode": "or"}]}}]
    native = _aev("number", {"widget_configuration_type": "flat", "series": series, "time_range": own})
    data = live_openaev.normalize(native, {"interval_count": 4, "previous_interval_count": 1, "difference_count": 3}, time_range=asked)
    assert data.get("span") == span


def test_an_openaev_list_types_its_columns_as_it_renders_them():
    columns = ["base_representative", "finding_type", "inject_expectation_type", "inject_expectation_status", "base_created_at", "status"]
    native = _aev("list", {"widget_configuration_type": "list", "perspective": {"name": "x", "filter": {}}, "columns": columns})
    answer = {
        "total": 3,
        "es_datas": [
            {"base_id": "f/1", "base_entity": "finding", "base_representative": "CVE-2026-1", "finding_type": "CVE",
             "base_created_at": "2026-10-02T09:00:00Z", "_url": "https://evil.example"},
            {"base_id": "e1", "base_entity": "expectation-inject", "base_representative": "Detect beacon",
             "inject_expectation_type": "DETECTION", "inject_expectation_status": "FAILED", "base_inject_side": "i1",
             "base_simulation_side": "s1"},
            {"base_id": "s1", "base_entity": "simulation", "base_representative": "Q4 drill", "status": "RUNNING"},
            {"base_id": "z", "base_entity": "payload", "base_representative": "Payload", "finding_type": "Other"},
        ],
    }  # fmt: skip
    data = live_openaev.normalize(native, answer, "https://aev.example")
    assert [(c["key"], c.get("kind", "text")) for c in data["columns"]] == [
        ("base_representative", "text"), ("finding_type", "chip"), ("inject_expectation_type", "chip"),
        ("inject_expectation_status", "chip"), ("base_created_at", "datetime"), ("status", "chip"),
    ]  # fmt: skip
    finding, expectation, simulation, payload = data["rows"]
    assert finding["finding_type"] == {"label": "CVE", "color": "#f44336"}
    assert (finding["_url"], finding["_icon"]) == ("https://aev.example/admin/findings/f%2F1", "binoculars")
    assert expectation["inject_expectation_type"] == {"label": "Detection", "color": "#0fbcff", "icon": "radio"}
    assert expectation["inject_expectation_status"] == {"label": "Not Detected", "color": "danger", "icon": "radio"}
    assert expectation["_url"] == "https://aev.example/admin/simulations/s1/injects/i1?expectation_id=e1"
    assert simulation["status"] == {"label": "Running", "color": "success"} and "_icon" not in simulation
    # An entity OpenAEV does not open has no link; an unknown finding type takes the default chip colour.
    assert "_url" not in payload and payload["finding_type"] == {"label": "Other", "color": "#607d8b"}
    assert data["total"] == 3


def test_an_openaev_row_link_is_never_built_on_a_base_that_is_not_http():
    native = _aev("list", {"widget_configuration_type": "list", "perspective": {"name": "x", "filter": {}}, "columns": []})
    data = live_openaev.normalize(native, {"es_datas": [{"base_id": "a", "base_entity": "asset"}]}, "javascript:")
    assert "_url" not in data["rows"][0]


# ── Text widgets ─────────────────────────────────────────────────────────────


def test_a_text_widget_may_be_written_in_markdown():
    spec = validate_spec({"title": "T", "widgets": [{"kind": "text", "body": "# Title\n\n- a", "format": "markdown"}]})
    assert spec["widgets"][0]["format"] == "markdown"
    plain = validate_spec({"title": "T", "widgets": [{"kind": "text", "body": "x"}]})
    assert "format" not in plain["widgets"][0]
    with pytest.raises(DashboardSpecError):
        validate_spec({"title": "T", "widgets": [{"kind": "text", "body": "x", "format": "html"}]})
