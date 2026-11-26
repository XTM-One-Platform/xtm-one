"""Custom dashboards on the 12-column grid: placement, the ``layout`` edit, older dashboards.

A widget without a layout (an older dashboard, an agent giving only a width) is
placed below the others; a drag or a resize is one ``layout`` operation whose
bounds are checked like any write; a move re-flows the grid in the new order.
"""

from __future__ import annotations

import copy

import pytest

from app.services.custom_dashboard_spec import DashboardSpecError, apply_operations, validate_spec, with_layouts
from app.services.dashboard_grid import default_rows, fill_layouts, reflow


def _text(wid: str, **extra) -> dict:
    return {"kind": "text", "id": wid, "body": wid, **extra}


def _cells(spec: dict) -> list[tuple[str, dict]]:
    return [(w["id"], w["layout"]) for w in spec["widgets"]]


def test_widgets_without_a_layout_are_placed_in_rows_by_their_width():
    spec = validate_spec(
        {"title": "T", "widgets": [_text("a", width="half"), _text("b", width="third"), _text("c", width="third"), _text("d")]}
    )
    assert _cells(spec) == [
        ("a", {"x": 0, "y": 0, "w": 6, "h": 2}),
        ("b", {"x": 6, "y": 0, "w": 4, "h": 2}),
        ("c", {"x": 0, "y": 2, "w": 4, "h": 2}),
        ("d", {"x": 0, "y": 4, "w": 12, "h": 2}),
    ]


def test_a_new_widget_without_a_layout_goes_below_the_placed_ones():
    widgets = [_text("a", layout={"x": 6, "y": 2, "w": 6, "h": 5}), _text("b", width="half")]
    assert [w["layout"] for w in fill_layouts(widgets)] == [{"x": 6, "y": 2, "w": 6, "h": 5}, {"x": 0, "y": 7, "w": 6, "h": 2}]


def test_a_platform_widget_is_as_tall_as_its_view():
    # Rows of OpenCTI's grid (82 px).
    assert default_rows({"kind": "platform", "view": "number"}) == 3
    assert default_rows({"kind": "platform", "view": "table"}) == 4
    assert default_rows({"kind": "platform", "view": "line", "size": "l"}) == 5
    assert default_rows({"kind": "platform", "view": None}) == 2


def test_widgets_are_stored_in_reading_order():
    spec = validate_spec(
        {"title": "T", "widgets": [_text("low", layout={"x": 0, "y": 9, "w": 12, "h": 2}), _text("top", layout={"x": 3, "y": 0, "w": 3, "h": 2})]}
    )
    assert [w["id"] for w in spec["widgets"]] == ["top", "low"]


def test_a_layout_edit_places_the_widgets_it_names():
    spec = validate_spec({"title": "T", "widgets": [_text("a", width="half"), _text("b", width="half")]})
    edited = apply_operations(
        spec, [{"op": "layout", "items": [{"id": "a", "x": 6, "y": 0, "w": 6, "h": 6}, {"id": "b", "x": 0, "y": 0, "w": 6, "h": 3}]}]
    )
    assert _cells(edited) == [("b", {"x": 0, "y": 0, "w": 6, "h": 3}), ("a", {"x": 6, "y": 0, "w": 6, "h": 6})]


@pytest.mark.parametrize(
    "item",
    [
        {"id": "a", "x": 8, "y": 0, "w": 6, "h": 4},
        {"id": "a", "x": -1, "y": 0, "w": 6, "h": 4},
        {"id": "a", "x": 0, "y": 0, "w": 0, "h": 4},
        {"id": "a", "x": 0, "y": 0, "w": 13, "h": 4},
        {"id": "a", "x": 0, "y": 0, "w": 6, "h": 41},
        {"id": "a", "x": 0, "y": -2, "w": 6, "h": 4},
        {"id": "a", "x": 0, "y": 0, "w": True, "h": 4},
        {"id": "a", "x": "0", "y": 0, "w": 6, "h": 4},
        {"id": "a", "x": 0, "y": 0, "w": 6},
        {"id": "a", "x": 0, "y": 0, "w": 6, "h": 4, "z": 1},
    ],
)
def test_a_layout_out_of_the_grid_is_refused(item):
    spec = validate_spec({"title": "T", "widgets": [_text("a")]})
    with pytest.raises(DashboardSpecError):
        apply_operations(spec, [{"op": "layout", "items": [item]}])


def test_a_layout_naming_no_widget_or_naming_none_is_refused():
    spec = validate_spec({"title": "T", "widgets": [_text("a")]})
    with pytest.raises(DashboardSpecError, match="no widget has the id 'z'"):
        apply_operations(spec, [{"op": "layout", "items": [{"id": "z", "x": 0, "y": 0, "w": 6, "h": 4}]}])
    with pytest.raises(DashboardSpecError, match="a layout needs 'items'"):
        apply_operations(spec, [{"op": "layout", "items": []}])


def test_a_widget_written_with_its_layout_out_of_the_grid_is_refused():
    with pytest.raises(DashboardSpecError, match="x \\+ w must be at most 12"):
        validate_spec({"title": "T", "widgets": [_text("a", layout={"x": 10, "y": 0, "w": 4, "h": 4})]})


def test_a_move_re_flows_the_grid_in_the_new_order_keeping_sizes():
    spec = validate_spec({"title": "T", "widgets": [_text("a", width="half"), _text("b", width="half"), _text("c")]})
    moved = apply_operations(spec, [{"op": "move", "id": "c", "after": "start"}])
    assert _cells(moved) == [
        ("c", {"x": 0, "y": 0, "w": 12, "h": 2}),
        ("a", {"x": 0, "y": 2, "w": 6, "h": 2}),
        ("b", {"x": 6, "y": 2, "w": 6, "h": 2}),
    ]


def test_an_upsert_keeps_the_widgets_place_and_follows_a_new_width():
    spec = validate_spec({"title": "T", "widgets": [_text("a", layout={"x": 6, "y": 0, "w": 6, "h": 7})]})
    same = apply_operations(spec, [{"op": "upsert", "widget": _text("a", body="new")}])
    assert same["widgets"][0]["layout"] == {"x": 6, "y": 0, "w": 6, "h": 7}
    wider = apply_operations(spec, [{"op": "upsert", "widget": _text("a", width="full")}])
    assert wider["widgets"][0]["layout"] == {"x": 0, "y": 0, "w": 12, "h": 7}
    placed = apply_operations(spec, [{"op": "upsert", "widget": _text("a", layout={"x": 0, "y": 3, "w": 4, "h": 2})}])
    assert placed["widgets"][0]["layout"] == {"x": 0, "y": 3, "w": 4, "h": 2}


def test_an_older_dashboard_is_shown_placed_without_changing_what_is_stored():
    stored = {"title": "T", "widgets": [{"kind": "text", "id": "a", "body": "a", "width": "half"}]}
    before = copy.deepcopy(stored)
    shown = with_layouts(stored)
    assert shown["widgets"][0]["layout"] == {"x": 0, "y": 0, "w": 6, "h": 2}
    assert stored == before


def test_reflow_places_every_widget_in_list_order():
    widgets = [_text("a", layout={"x": 6, "y": 9, "w": 6, "h": 2}), _text("b", layout={"x": 0, "y": 0, "w": 12, "h": 3})]
    assert [w["layout"] for w in reflow(widgets)] == [{"x": 0, "y": 0, "w": 6, "h": 2}, {"x": 0, "y": 2, "w": 12, "h": 3}]
