"""The absolute layout of a dashboard definition: canvas options, elements, lines and their edits."""

from __future__ import annotations

import copy

import pytest

from app.services.dashboard_assets import sniff_image_type
from app.services.dashboard_canvas import file_ids, port_point
from app.services.dashboard_definition import DefinitionError, apply_definition_operations, validate_definition

_IMAGE = "3f2b8c1e-7d4a-4c55-9a8e-0b1c2d3e4f50"
_SVG = "6a1c0f2e-1111-4c55-9a8e-0b1c2d3e4f51"
_BACKGROUND = "9b9b9b9b-2222-4c55-9a8e-0b1c2d3e4f52"


def _canvas(**overrides) -> dict:
    definition = {
        "version": 2,
        "title": "Ops room",
        "visualizations": {
            "box": {"type": "xtm.shape.rectangle", "options": {"fillColor": "#1a2b3c", "rx": 12, "strokeDashStyle": "dashed"}},
            "dot": {"type": "xtm.shape.ellipse", "options": {"fillColor": "#ff0000", "fillOpacity": 0.5}},
            "arrow": {"type": "xtm.shape.line", "options": {"toArrow": True, "strokeWidth": 3}},
            "shield": {"type": "xtm.icon", "options": {"icon": "shield-check", "color": "#00ff00"}},
            "logo": {"type": "xtm.icon", "options": {"fileId": _SVG}},
            "map": {"type": "xtm.image", "options": {"fileId": _IMAGE, "alt": "The network", "fit": "cover"}},
            "notes": {"type": "xtm.markdown", "options": {"markdown": "# Ops", "fontSize": "large", "textAlign": "center"}},
            "brief": {"type": "xtm.text", "options": {"body": "Hello"}},
        },
        "layout": {
            "tabs": {"items": [{"layoutId": "canvas", "label": "Canvas"}, {"layoutId": "grid", "label": "Grid"}]},
            "layoutDefinitions": {
                "canvas": {
                    "type": "absolute",
                    "options": {
                        "width": 1600,
                        "height": 900,
                        "display": "fit-to-width",
                        "backgroundColor": "#0a0f1c",
                        "backgroundImage": {"fileId": _BACKGROUND, "sizeType": "contain", "opacity": 0.4},
                    },
                    "structure": [
                        {"item": "box", "position": {"x": 10, "y": 20, "w": 200, "h": 100}},
                        {"item": "dot", "position": {"x": 400, "y": 20, "w": 80, "h": 80}},
                        {"item": "arrow", "type": "line", "position": {"from": {"item": "box", "port": "e"}, "to": {"item": "dot", "port": "w"}}},
                        {"item": "shield", "position": {"x": 600, "y": 20, "w": 48, "h": 48}},
                        {"item": "logo", "position": {"x": 700, "y": 20, "w": 48, "h": 48}},
                        {"item": "map", "position": {"x": 10, "y": 300, "w": 640, "h": 360}},
                        {"item": "notes", "position": {"x": 700, "y": 300, "w": 400, "h": 200}},
                    ],
                },
                "grid": {"type": "grid", "structure": [{"item": "brief", "position": {"x": 0, "y": 0, "w": 12, "h": 4}}]},
            },
        },
    }
    definition.update(overrides)
    return definition


def _invalid(definition: dict) -> str:
    with pytest.raises(DefinitionError) as caught:
        validate_definition(definition)
    return str(caught.value)


def test_a_canvas_and_its_elements_validate_and_normalize() -> None:
    stored = validate_definition(_canvas(theme="light"))
    canvas = stored["layout"]["layoutDefinitions"]["canvas"]
    assert canvas["options"] == {
        "width": 1600,
        "height": 900,
        "display": "fit-to-width",
        "backgroundColor": "#0a0f1c",
        "backgroundImage": {"fileId": _BACKGROUND, "sizeType": "contain", "opacity": 0.4},
    }
    assert canvas["structure"][2] == {
        "item": "arrow",
        "type": "line",
        "position": {"from": {"item": "box", "port": "e"}, "to": {"item": "dot", "port": "w"}},
    }
    viz = stored["visualizations"]
    assert viz["box"]["options"] == {"fillColor": "#1a2b3c", "rx": 12, "strokeDashStyle": "dashed"}
    assert viz["arrow"]["options"] == {"toArrow": True, "strokeWidth": 3}
    assert viz["shield"]["options"] == {"icon": "shield-check", "color": "#00ff00"}
    assert viz["notes"]["options"] == {"markdown": "# Ops", "fontSize": "large", "textAlign": "center"}
    assert stored["theme"] == "light"
    assert file_ids(stored) == {_IMAGE, _SVG, _BACKGROUND}


def test_a_canvas_without_options_gets_its_default_size() -> None:
    definition = _canvas()
    definition["layout"]["layoutDefinitions"]["canvas"]["options"] = {}
    stored = validate_definition(definition)
    assert stored["layout"]["layoutDefinitions"]["canvas"]["options"] == {"width": 1440, "height": 960}


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("layout", "layoutDefinitions", "canvas", "options", "width"), 20_000, "width"),
        (("layout", "layoutDefinitions", "canvas", "options", "display"), "stretch", "display"),
        (("layout", "layoutDefinitions", "canvas", "options", "backgroundColor"), "red", "a colour is"),
        (("layout", "layoutDefinitions", "canvas", "options", "backgroundImage"), {"fileId": "../x"}, "fileId"),
        (("layout", "layoutDefinitions", "canvas", "options", "zoom"), 2, "zoom"),
        (("visualizations", "box", "options", "fillColor"), "url(javascript:x)", "fillColor"),
        (("visualizations", "box", "options", "strokeColor"), "#zzz", "a colour is"),
        (("visualizations", "box", "options", "rx"), -1, "rx"),
        (("visualizations", "box", "options", "onclick"), "x", "onclick"),
        (("visualizations", "arrow", "options", "strokeWidth"), 99, "strokeWidth"),
        (("visualizations", "shield", "options", "icon"), "shield-chek", "close: shield-check"),
        (("visualizations", "map", "options", "alt"), "x" * 301, "alt"),
        (("visualizations", "map", "options", "fit"), "tile", "fit"),
        (("visualizations", "notes", "options", "markdown"), "x" * 5_001, "markdown"),
        (("visualizations", "notes", "options", "fontFamily"), "comic", "fontFamily"),
    ],
)
def test_every_option_is_bounded_and_unknown_keys_are_refused(path, value, message) -> None:
    definition = _canvas()
    node = definition
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert message in _invalid(definition)


def test_an_icon_names_a_lucide_icon_or_an_uploaded_svg_never_both_nor_none() -> None:
    both = _canvas()
    both["visualizations"]["shield"]["options"]["fileId"] = _SVG
    assert "either 'icon'" in _invalid(both)
    none = _canvas()
    none["visualizations"]["shield"]["options"] = {}
    assert "either 'icon'" in _invalid(none)


def test_an_icon_alias_is_stored_under_its_canonical_name() -> None:
    definition = _canvas()
    definition["visualizations"]["shield"]["options"]["icon"] = "ShieldCheck"
    assert validate_definition(definition)["visualizations"]["shield"]["options"]["icon"] == "shield-check"


def test_canvas_elements_sit_on_an_absolute_layout_only() -> None:
    definition = _canvas()
    definition["layout"]["layoutDefinitions"]["grid"]["structure"].append(
        {"item": "box", "position": {"x": 0, "y": 4, "w": 6, "h": 4}}
    )
    definition["layout"]["layoutDefinitions"]["canvas"]["structure"].pop(0)
    definition["layout"]["layoutDefinitions"]["canvas"]["structure"].pop(1)
    assert "sits on an absolute layout, not a grid" in _invalid(definition)


def test_a_line_is_placed_by_a_line_entry_and_only_a_line_is() -> None:
    as_block = _canvas()
    as_block["layout"]["layoutDefinitions"]["canvas"]["structure"][2] = {
        "item": "arrow", "position": {"x": 0, "y": 0, "w": 10, "h": 10},
    }  # fmt: skip
    assert "placed by a 'line' entry" in _invalid(as_block)
    box_as_line = _canvas()
    box_as_line["layout"]["layoutDefinitions"]["canvas"]["structure"][0] = {
        "item": "box", "type": "line", "position": {"from": {"x": 0, "y": 0}, "to": {"x": 5, "y": 5}},
    }  # fmt: skip
    message = _invalid(box_as_line)
    assert "placed by a 'line' entry" in message
    mixed = _canvas()
    mixed["layout"]["layoutDefinitions"]["canvas"]["structure"][2]["position"] = {"x": 0, "y": 0, "w": 1, "h": 1}
    assert "a line is placed by {from, to}" in _invalid(mixed)


def test_a_line_end_holds_to_a_block_of_its_own_layout() -> None:
    definition = _canvas()
    definition["layout"]["layoutDefinitions"]["canvas"]["structure"][2]["position"]["to"] = {"item": "brief", "port": "n"}
    assert "'brief' is no block of this layout" in _invalid(definition)
    bad_port = _canvas()
    bad_port["layout"]["layoutDefinitions"]["canvas"]["structure"][2]["position"]["to"] = {"item": "dot", "port": "ne"}
    assert "port" in _invalid(bad_port)


def test_a_line_on_a_grid_is_refused() -> None:
    definition = _canvas()
    definition["layout"]["layoutDefinitions"]["grid"]["structure"].append(
        {"item": "arrow", "type": "line", "position": {"from": {"x": 0, "y": 0}, "to": {"x": 1, "y": 1}}}
    )
    definition["layout"]["layoutDefinitions"]["canvas"]["structure"].pop(2)
    assert "a line sits on an absolute layout, not a grid" in _invalid(definition)


def test_a_canvas_element_reads_no_data_source() -> None:
    definition = _canvas(dataSources={"ds": {"type": "static", "options": {"shape": "number", "data": {"value": 3}}}})
    definition["visualizations"]["box"]["dataSources"] = {"primary": "ds"}
    assert "reads no data source" in _invalid(definition)


def test_removing_a_block_lets_go_of_the_lines_held_to_it() -> None:
    stored = validate_definition(_canvas())
    edited = apply_definition_operations(stored, [{"op": "delete", "path": "visualizations.dot"}])
    line = edited["layout"]["layoutDefinitions"]["canvas"]["structure"][1]
    assert line == {
        "item": "arrow",
        "type": "line",
        "position": {"from": {"item": "box", "port": "e"}, "to": {"x": 400, "y": 60}},
    }
    removed = apply_definition_operations(stored, [{"op": "remove", "id": "box"}])
    assert removed["layout"]["layoutDefinitions"]["canvas"]["structure"][1]["position"]["from"] == {"x": 210, "y": 70}


def test_the_layout_operation_places_blocks_and_lines_of_a_canvas() -> None:
    stored = validate_definition(_canvas())
    edited = apply_definition_operations(
        stored,
        [
            {
                "op": "layout",
                "layoutId": "canvas",
                "items": [
                    {"id": "box", "x": 30, "y": 40, "w": 220, "h": 110},
                    {"id": "arrow", "from": {"x": 1, "y": 2}, "to": {"item": "box", "port": "s"}},
                ],
            }
        ],
    )
    structure = edited["layout"]["layoutDefinitions"]["canvas"]["structure"]
    assert structure[0]["position"] == {"x": 30, "y": 40, "w": 220, "h": 110}
    assert structure[2]["position"] == {"from": {"x": 1, "y": 2}, "to": {"item": "box", "port": "s"}}


def test_the_theme_is_set_and_deleted_by_path() -> None:
    stored = validate_definition(_canvas())
    dark = apply_definition_operations(stored, [{"op": "set", "path": "theme", "value": "dark"}])
    assert dark["theme"] == "dark"
    assert "theme" not in apply_definition_operations(dark, [{"op": "delete", "path": "theme"}])
    with pytest.raises(DefinitionError, match="theme"):
        apply_definition_operations(stored, [{"op": "set", "path": "theme", "value": "sepia"}])


def test_a_widget_added_to_a_canvas_is_placed_in_pixels_below_its_blocks() -> None:
    stored = validate_definition(_canvas())
    edited = apply_definition_operations(
        stored, [{"op": "upsert", "layoutId": "canvas", "widget": {"kind": "text", "id": "added", "body": "Hi"}}]
    )
    entry = edited["layout"]["layoutDefinitions"]["canvas"]["structure"][-1]
    assert entry == {"item": "added", "type": "block", "position": {"x": 24, "y": 684, "w": 480, "h": 320}}


def test_tabs_are_edited_through_the_layout_path() -> None:
    stored = validate_definition(_canvas())
    layout = copy.deepcopy(stored["layout"])
    layout["tabs"]["items"].reverse()
    layout["tabs"]["items"][0]["label"] = "Notes"
    layout["tabs"]["options"] = {"showTabBar": True, "barPosition": "left"}
    edited = apply_definition_operations(stored, [{"op": "set", "path": "layout", "value": layout}])
    assert edited["layout"]["tabs"] == {
        "items": [{"layoutId": "grid", "label": "Notes"}, {"layoutId": "canvas", "label": "Canvas"}],
        "options": {"showTabBar": True, "barPosition": "left"},
    }


def test_port_points_are_the_middles_of_the_sides() -> None:
    box = {"x": 10, "y": 20, "w": 200, "h": 100}
    assert [port_point(box, port) for port in ("n", "e", "s", "w")] == [
        {"x": 110, "y": 20}, {"x": 210, "y": 70}, {"x": 110, "y": 120}, {"x": 10, "y": 70},
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n....", "image/png"),
        (b"\xff\xd8\xff\xe0....", "image/jpeg"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None),
        (b"<html><script>x</script>", None),
        (b"", None),
    ],
    ids=["png", "jpeg", "gif", "webp", "svg", "html", "empty"],
)
def test_an_image_type_is_read_from_its_bytes(data, expected) -> None:
    assert sniff_image_type(data) == expected


def test_the_widget_count_leaves_out_the_canvas_elements() -> None:
    from app.services.dashboard_definition import visualization_count

    assert visualization_count(validate_definition(_canvas())) == 1


def test_the_studio_outline_says_where_a_canvas_places_its_lines() -> None:
    from app.services.dashboard_studio import outline

    said = outline(validate_definition(_canvas())).splitlines()
    assert "  - arrow [xtm.shape.line, box.e -> dot.w]" in said
    assert "  - box [xtm.shape.rectangle, 10,20 200x100]" in said


def test_deleting_the_last_visualization_is_refused_like_removing_it() -> None:
    lone = _canvas()
    lone["visualizations"] = {"box": lone["visualizations"]["box"]}
    lone["layout"]["tabs"]["items"].pop()
    lone["layout"]["layoutDefinitions"].pop("grid")
    lone["layout"]["layoutDefinitions"]["canvas"]["structure"] = [{"item": "box", "position": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    stored = validate_definition(lone)
    with pytest.raises(DefinitionError, match="keeps at least one widget"):
        apply_definition_operations(stored, [{"op": "delete", "path": "visualizations.box"}])
