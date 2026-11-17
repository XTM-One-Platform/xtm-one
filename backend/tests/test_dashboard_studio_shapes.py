"""The closed shapes of maps, graphs and flows, and the visualizations drawing them.

A ``geo``, ``graph``, ``events`` or ``tree`` source holds only what its shape
says: cut, coerced, bounded, and the same when closed again. The definition
takes the new visualization types; an SVG choropleth's drawing goes through
the template assets' sanitizer; a platform widget may be drawn with a view
that suits its shape only.
"""

from __future__ import annotations

import pytest

from app.services.custom_dashboard_spec import validate_widget
from app.services.dashboard_definition import (
    MAX_CHOROPLETH_SVG_BYTES,
    DefinitionError,
    validate_definition,
)
from app.services.dashboard_live import (
    MAX_EVENTS,
    MAX_GEO_POINTS,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_REGIONS,
    MAX_TREE_DEPTH,
    MAX_TREE_ITEMS,
    closed_static_data,
)

INTEGRATION = "11111111-1111-4111-8111-111111111111"


def _close_twice(shape: str, data: dict) -> dict:
    once = closed_static_data(shape, data)
    assert closed_static_data(shape, once) == once
    return once


# ── geo ──────────────────────────────────────────────────────────────────────


def test_geo_keeps_points_on_the_globe_and_regions_with_a_value():
    data = _close_twice(
        "geo",
        {
            "points": [
                {"label": "Paris", "lat": "48.85", "lon": 2.35, "value": 12, "group": "City"},
                {"label": "Nowhere", "lat": 91, "lon": 0, "value": 1},
                {"label": "No longitude", "lat": 10},
                {"label": "Unsized", "lat": -33.9, "lon": 151.2},
                "not a point",
            ],
            "regions": [
                {"code": "FR", "label": "France", "value": 5},
                {"code": "FR", "value": 99},
                {"code": "DE"},
                {"code": "", "value": 3},
                {"code": "room-101", "value": "7"},
            ],
        },
    )
    assert data == {
        "points": [
            {"label": "Paris", "lat": 48.85, "lon": 2.35, "value": 12.0, "group": "City"},
            {"label": "Unsized", "lat": -33.9, "lon": 151.2, "value": None, "group": None},
        ],
        "regions": [
            {"code": "FR", "label": "France", "value": 5.0},
            {"code": "room-101", "label": "room-101", "value": 7.0},
        ],
    }


def test_geo_is_bounded():
    data = closed_static_data(
        "geo",
        {
            "points": [{"lat": 0, "lon": 0, "label": "x" * 500}] * (MAX_GEO_POINTS + 5),
            "regions": [{"code": f"r{i}", "value": i} for i in range(MAX_REGIONS + 5)],
        },
    )
    assert len(data["points"]) == MAX_GEO_POINTS
    assert len(data["points"][0]["label"]) == 120
    assert len(data["regions"]) == MAX_REGIONS


# ── graph ────────────────────────────────────────────────────────────────────


def test_graph_names_each_node_once_and_adds_the_ends_an_edge_names():
    data = _close_twice(
        "graph",
        {
            "nodes": [{"id": "apt28", "label": "APT28", "group": "Intrusion set"}, {"id": "apt28", "label": "again"}, {"label": "no id"}],
            "edges": [
                {"source": "apt28", "target": "x-agent", "label": "uses", "value": "3"},
                {"source": "", "target": "apt28"},
                {"source": "apt28", "target": "fr", "value": "many"},
            ],
        },
    )
    assert data == {
        "nodes": [
            {"id": "apt28", "label": "APT28", "group": "Intrusion set", "value": None},
            {"id": "x-agent", "label": "x-agent", "group": None, "value": None},
            {"id": "fr", "label": "fr", "group": None, "value": None},
        ],
        "edges": [
            {"source": "apt28", "target": "x-agent", "label": "uses", "value": 3.0},
            {"source": "apt28", "target": "fr", "label": None, "value": None},
        ],
    }


def test_graph_is_bounded_and_drops_an_edge_whose_end_has_no_room():
    nodes = [{"id": f"n{i}"} for i in range(MAX_GRAPH_NODES)]
    edges = [{"source": "n0", "target": "beyond"}] + [{"source": "n0", "target": "n1"}] * (MAX_GRAPH_EDGES + 5)
    data = closed_static_data("graph", {"nodes": nodes, "edges": edges})
    assert len(data["nodes"]) == MAX_GRAPH_NODES
    assert len(data["edges"]) == MAX_GRAPH_EDGES
    assert all(edge["target"] == "n1" for edge in data["edges"])


# ── events ───────────────────────────────────────────────────────────────────


def test_events_keep_a_readable_start_sorted_and_an_end_after_it():
    data = _close_twice(
        "events",
        {
            "events": [
                {"label": "Campaign", "start": "2026-03-01T10:00:00+02:00", "end": "2026-03-04", "lane": "APT28", "value": 4},
                {"label": "Report", "start": "2026-01-15"},
                {"label": "Backwards", "start": "2026-05-01", "end": "2026-04-01"},
                {"label": "Garbage", "start": "yesterday"},
                {"label": "Bad day", "start": "2026-02-30"},
                {"label": "Number", "start": 1700000000},
            ]
        },
    )
    assert data == {
        "events": [
            {"label": "Report", "start": "2026-01-15", "end": None, "lane": None, "value": None},
            {"label": "Campaign", "start": "2026-03-01T08:00:00Z", "end": "2026-03-04", "lane": "APT28", "value": 4.0},
            {"label": "Backwards", "start": "2026-05-01", "end": None, "lane": None, "value": None},
        ]
    }


def test_events_are_bounded():
    data = closed_static_data("events", {"events": [{"label": "e", "start": "2026-01-01"}] * (MAX_EVENTS + 1)})
    assert len(data["events"]) == MAX_EVENTS


# ── tree ─────────────────────────────────────────────────────────────────────


def test_tree_sums_a_path_named_twice_and_cuts_deep_ones():
    data = _close_twice(
        "tree",
        {
            "items": [
                {"path": ["Malware", "Ransomware"], "value": 4},
                {"path": ["Malware", "Ransomware"], "value": "2"},
                {"path": ["Tool", "Scanner", "Port", "Too deep"], "value": 1},
                {"label": "Flat", "value": 3},
                {"path": ["Negative"], "value": -1},
                {"path": [], "value": 1},
                {"path": ["No value"]},
            ]
        },
    )
    assert data == {
        "items": [
            {"path": ["Malware", "Ransomware"], "value": 6.0},
            {"path": ["Tool", "Scanner", "Port"], "value": 1.0},
            {"path": ["Flat"], "value": 3.0},
        ]
    }
    assert MAX_TREE_DEPTH == 3


def test_tree_is_bounded():
    data = closed_static_data("tree", {"items": [{"path": [f"p{i}"], "value": 1} for i in range(MAX_TREE_ITEMS + 5)]})
    assert len(data["items"]) == MAX_TREE_ITEMS


@pytest.mark.parametrize("shape", ["geo", "graph", "events", "tree"])
def test_a_shape_reads_nothing_from_values_of_the_wrong_type(shape):
    assert closed_static_data(shape, {"points": "x", "regions": 3, "nodes": None, "edges": {}, "events": "e", "items": 1}) == {
        "geo": {"points": [], "regions": []},
        "graph": {"nodes": [], "edges": []},
        "events": {"events": []},
        "tree": {"items": []},
    }[shape]


# ── The definition ───────────────────────────────────────────────────────────

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50" onload="alert(1)">'
    '<script>alert(1)</script><rect id="room-101" x="0" y="0" width="50" height="50"/>'
    '<rect id="room-102" x="50" y="0" width="50" height="50" onclick="steal()"/></svg>'
)


def _definition(viz_type: str, shape: str, data: dict, **options) -> dict:
    return {
        "version": 2,
        "title": "Maps",
        "dataSources": {"src": {"type": "static", "options": {"shape": shape, "data": data}}},
        "visualizations": {"viz": {"type": viz_type, "dataSources": {"primary": "src"}, "options": options}},
        "layout": {
            "tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
            "layoutDefinitions": {"main": {"structure": [{"item": "viz", "position": {"x": 0, "y": 0, "w": 12, "h": 8}}]}},
        },
    }


@pytest.mark.parametrize(
    ("viz_type", "shape", "data"),
    [
        ("xtm.map", "geo", {"regions": [{"code": "FR", "value": 3}]}),
        ("xtm.linkgraph", "graph", {"edges": [{"source": "a", "target": "b"}]}),
        ("xtm.networkgraph", "graph", {"edges": [{"source": "a", "target": "b"}]}),
        ("xtm.sankey", "graph", {"edges": [{"source": "a", "target": "b", "value": 2}]}),
        ("xtm.parallelcoordinates", "rows", {"columns": [{"key": "a"}], "rows": [{"a": "1"}]}),
        ("xtm.timeline", "events", {"events": [{"label": "e", "start": "2026-01-01"}]}),
        ("xtm.treemap", "tree", {"items": [{"path": ["a"], "value": 1}]}),
    ],
)
def test_the_new_visualizations_draw_their_shapes(viz_type, shape, data):
    definition = validate_definition(_definition(viz_type, shape, data))
    assert definition["visualizations"]["viz"]["type"] == viz_type
    assert definition["dataSources"]["src"]["options"]["data"] == closed_static_data(shape, data)


def test_an_svg_choropleth_keeps_its_regions_and_loses_what_runs():
    definition = validate_definition(
        _definition("xtm.choropleth.svg", "geo", {"regions": [{"code": "room-101", "value": 1}]}, svg=SVG, legend=True)
    )
    options = definition["visualizations"]["viz"]["options"]
    assert options["legend"] is True
    assert 'id="room-101"' in options["svg"] and 'id="room-102"' in options["svg"]
    for gone in ("<script", "onload", "onclick", "alert(", "steal("):
        assert gone not in options["svg"]
    # Read again, the drawing is the same.
    assert validate_definition(definition)["visualizations"]["viz"]["options"]["svg"] == options["svg"]


@pytest.mark.parametrize(
    ("svg", "fragment"),
    [
        (None, "needs options.svg"),
        ("   ", "needs options.svg"),
        ("<svg><rect></svg>", "not a usable SVG"),
        ("<html><body/></html>", "not a usable SVG"),
        ('<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>', "not a usable SVG"),
        ('<svg xmlns="http://www.w3.org/2000/svg">' + " " * MAX_CHOROPLETH_SVG_BYTES + "</svg>", f"more than {MAX_CHOROPLETH_SVG_BYTES} bytes"),
    ],
)
def test_an_svg_choropleth_refuses_what_is_no_safe_svg(svg, fragment):
    options = {} if svg is None else {"svg": svg}
    with pytest.raises(DefinitionError) as refused:
        validate_definition(_definition("xtm.choropleth.svg", "geo", {}, **options))
    assert fragment in str(refused.value)


def test_an_svg_choropleths_other_options_keep_the_common_bound():
    with pytest.raises(DefinitionError) as refused:
        validate_definition(_definition("xtm.choropleth.svg", "geo", {}, svg=SVG, note="x" * 20_000))
    assert "options holds more than" in str(refused.value)


def test_a_new_static_shape_that_does_not_fit_is_refused():
    with pytest.raises(DefinitionError) as refused:
        validate_definition(_definition("xtm.map", "places", {}))
    assert "dataSources.src.static.options.shape: Input should be 'number', 'series', 'distribution', 'rows', 'text', 'geo', 'graph', 'events', 'tree' or 'outcomes'" in str(refused.value)


# ── Platform widgets ─────────────────────────────────────────────────────────


def _map_widget(**extra) -> dict:
    return {
        "kind": "platform",
        "id": "targets",
        "source": "opencti",
        "integration_id": INTEGRATION,
        "native": {"type": "map", "perspective": "relationships", "dataSelection": [{"attribute": "internal_id"}]},
        **extra,
    }


def test_an_opencti_map_is_drawn_as_a_map():
    assert validate_widget(_map_widget())["view"] == "map"


def test_an_opencti_tree_and_timeline_are_drawn_as_opencti_draws_them():
    tree = {**_map_widget(), "native": {"type": "tree", "perspective": "entities", "dataSelection": [{}]}}
    timeline = {**_map_widget(), "native": {"type": "timeline", "perspective": "entities", "dataSelection": [{}]}}
    assert validate_widget(tree)["view"] == "treemap"
    assert validate_widget(timeline)["view"] == "timeline"


def test_a_view_that_does_not_suit_the_map_is_refused():
    with pytest.raises(Exception) as refused:
        validate_widget(_map_widget(view="sankey"))
    assert "use one of: map, choroplethsvg" in str(refused.value)
