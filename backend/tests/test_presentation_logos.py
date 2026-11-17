"""Logo walls and callouts that look designed, not boxed-in.

A logo is sized by its MARK: ``logo_height`` (``size`` stays its alias) is the
height the renderer draws, the file's transparent margin is cropped away by a
trim box read once from the bytes (``image_geometry``, stored on the file and
served with the deck as ``asset_meta``), and ``optical_balance`` lets a seal
stand a little taller than a wordmark. A ``logo_wall`` groups logos in cards.
A callout's ``variant`` is its box and ``header`` frees its children from the
icon column. What is pinned here is everything but the pixels - those are the
e2e spec's (``tests_e2e/presentation-logos.spec.ts``):

* the spec models: fields, defaults, bounds, the reference and the example;
* stored decks keep what they drew (a logo grid's labels) on the read path only;
* the geometry: what is read from PNG, SVG and opaque files, and when nothing is;
* the backfill a deck save runs, and the geometry a deck read serves;
* the layout audit's logo findings and the extractor's picture and group tags;
* the editable PPTX: a wall card is one group shape, its pictures in place.
"""

from __future__ import annotations

import asyncio
import copy
import io
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pptx import Presentation as PptxPresentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from sqlalchemy.sql.dml import Update

from app.models.presentation import SpecJSON
from app.services import pptx_writer
from app.services.image_geometry import (
    ANALYSIS_LONG_EDGE,
    IMAGE_META_VERSION,
    MAX_ANALYSIS_BYTES,
    analyze_image_geometry,
    backfill_image_geometry,
    image_geometry,
    render_geometry,
)
from app.services.presentation_export import EXTRACT_PRIMITIVES_JS, LAYOUT_AUDIT_JS
from app.services.presentation_guidance import BLOCK_EXAMPLES
from app.services.presentation_layout import LOGO_MIN_COVERAGE, LOGO_MIN_HEIGHT_PX, findings_from_observations
from app.services.presentation_legacy import keep_stored_logo_labels
from app.services.presentation_spec import (
    LOGO_HEIGHT_PX,
    LOGO_TILE_HEIGHT_MAX_PX,
    LOGO_TILE_HEIGHT_MIN_PX,
    MAX_LOGO_WALL_GROUP_LOGOS,
    MAX_LOGO_WALL_GROUPS,
    PresentationSpecError,
    logo_file_ids,
    spec_reference,
    validate_spec,
)
from app.services.presentations import load_logo_asset_meta
from app.services.template_containers import serialize_asset
from tests.rendezvous import Rendezvous

FRONTEND_PRESENTATION = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"


def _ids(count: int) -> list[str]:
    return [str(uuid.UUID(int=index + 1)) for index in range(count)]


def _deck(*blocks: dict) -> dict:
    return {"title": "Logos", "slides": [{"layout": "content", "title": "Trusted by", "blocks": list(blocks)}]}


def _grid(count: int = 8, **fields) -> dict:
    return {"type": "logo_grid", "items": [{"file_id": file_id} for file_id in _ids(count)], **fields}


def _wall(groups: int = 4, per_group: int = 8, **fields) -> dict:
    ids = _ids(groups * per_group)
    return {
        "type": "logo_wall",
        "groups": [
            {"title": f"Sector {g + 1}", "logos": [{"file_id": i} for i in ids[g * per_group : (g + 1) * per_group]]}
            for g in range(groups)
        ],
        **fields,
    }


def _stored_block(spec: dict) -> dict:
    return validate_spec(spec)["slides"][0]["blocks"][0]


# ── The spec ─────────────────────────────────────────────────────────────────


def test_logo_heights_are_the_four_documented_sizes():
    assert LOGO_HEIGHT_PX == {"sm": 28, "md": 40, "lg": 56, "xl": 72}


def test_a_logo_grid_stores_its_new_defaults_and_keeps_size_as_the_alias():
    block = _stored_block(_deck(_grid(size="lg")))
    # `size` is kept as written: the renderer reads `logo_height ?? size`.
    assert block["size"] == "lg"
    assert "logo_height" not in block
    assert block["optical_balance"] is True
    assert block["gap"] == "md"
    # Unset: the renderer reads it off `columns` (`logoGridAlign`).
    assert "align" not in block
    assert block["fill"] is False
    assert block["tone"] == "original"
    assert block["show_labels"] is False
    assert "rows" not in block
    # Unset: the cell hugs its logo, as every stored deck draws.
    assert "tile_height" not in block


def test_a_logo_grid_takes_every_new_field():
    block = _stored_block(
        _deck(_grid(logo_height="xl", rows=2, columns=4, gap="lg", align="center", fill=True, tone="mono", show_labels=True))
    )
    assert (block["logo_height"], block["rows"], block["columns"]) == ("xl", 2, 4)
    assert (block["gap"], block["align"], block["fill"], block["tone"], block["show_labels"]) == (
        "lg",
        "center",
        True,
        "mono",
        True,
    )


def test_ink_tone_optical_weight_and_a_logo_scale_are_stored_as_written():
    grid = _grid(2, tone="ink", optical_weight=True)
    grid["items"][0]["scale"] = 1.2
    block = _stored_block(_deck(grid))
    assert (block["tone"], block["optical_weight"], block["items"][0]["scale"]) == ("ink", True, 1.2)
    wall = _stored_block(_deck(_wall(1, 2, tone="ink", optical_weight=True)))
    assert (wall["tone"], wall["optical_weight"]) == ("ink", True)


def test_a_deck_without_the_new_fields_stores_none_of_them():
    # Unset stays unset: an existing deck renders exactly as before.
    grid = _stored_block(_deck(_grid(2)))
    wall = _stored_block(_deck(_wall(1, 2)))
    assert "optical_weight" not in grid and "optical_weight" not in wall
    assert all("scale" not in item for item in grid["items"])


@pytest.mark.parametrize("scale", [0.4, 1.6, 0])
def test_a_logo_scale_out_of_bounds_is_refused(scale):
    grid = _grid(1)
    grid["items"][0]["scale"] = scale
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(grid))


# ── One tile height for sibling grids ────────────────────────────────


def test_a_tile_height_is_stored_as_written_on_a_grid_and_a_wall():
    """Four sibling grids with different logo mixes share one tile size: the
    value is the cell's height in canvas px, independent of `logo_height`."""
    grid = _stored_block(_deck(_grid(4, columns=4, align="start", logo_height="md", tile_height=96)))
    assert grid["tile_height"] == 96
    assert (grid["logo_height"], grid["align"], grid["columns"]) == ("md", "start", 4)
    wall = _stored_block(_deck(_wall(columns=2, per_row=4, tile_height=LOGO_TILE_HEIGHT_MIN_PX)))
    assert wall["tile_height"] == LOGO_TILE_HEIGHT_MIN_PX
    assert _stored_block(_deck(_grid(4, tile_height=LOGO_TILE_HEIGHT_MAX_PX)))["tile_height"] == LOGO_TILE_HEIGHT_MAX_PX


@pytest.mark.parametrize(
    "value",
    [LOGO_TILE_HEIGHT_MIN_PX - 1, LOGO_TILE_HEIGHT_MAX_PX + 1, 0, -40, "lg", 96.5],
    ids=["under", "over", "zero", "negative", "a-step-name", "a-fraction"],
)
def test_a_tile_height_out_of_bounds_or_not_a_whole_number_is_refused(value):
    for block in (_grid(4, tile_height=value), _wall(columns=2, tile_height=value)):
        with pytest.raises(PresentationSpecError) as caught:
            validate_spec(_deck(block))
        assert "tile_height" in str(caught.value)


def test_the_tile_height_bounds_are_the_ones_a_slide_holds():
    """A tile shorter than the smallest mark (sm, 28 px, plus a hairline of
    padding) or taller than a third of the 1080 canvas is a mistake."""
    assert LOGO_TILE_HEIGHT_MIN_PX == 32
    assert LOGO_TILE_HEIGHT_MAX_PX == 400
    assert LOGO_HEIGHT_PX["sm"] < LOGO_TILE_HEIGHT_MIN_PX
    assert LOGO_TILE_HEIGHT_MAX_PX < 1080 / 2


def test_the_reference_teaches_the_tile_height_with_its_bounds_and_the_equal_start_cells():
    reference = spec_reference()
    for line in reference.splitlines():
        if line.startswith(("- logo_grid:", "- logo_wall:")):
            assert f"tile_height?: int (canvas px of every cell ({LOGO_TILE_HEIGHT_MIN_PX}-{LOGO_TILE_HEIGHT_MAX_PX})" in line
            assert "sibling grids share one tile size" in line
            assert "a seal up to the inner height" in line
    grid_line = next(line for line in reference.splitlines() if line.startswith("- logo_grid:"))
    assert "start = equal cells packed from the start with columns, packed logos without" in grid_line
    assert "in a tile_height cell a seal grows to its height" in grid_line


@pytest.mark.parametrize(
    ("count", "fields"),
    [
        (2, {"rows": 3}),  # more rows than logos
        (7, {"rows": 2, "columns": 3}),  # 2 x 3 cells cannot hold 7 logos
        (8, {"rows": 3, "columns": 4}),  # the third row would be empty
    ],
)
def test_rows_that_cannot_hold_the_logos_exactly_are_refused(count, fields):
    with pytest.raises(PresentationSpecError) as caught:
        validate_spec(_deck(_grid(count, **fields)))
    assert "rows" in str(caught.value)


@pytest.mark.parametrize(("count", "fields"), [(8, {"rows": 2, "columns": 4}), (5, {"rows": 2}), (6, {"rows": 1})])
def test_rows_that_hold_the_logos_are_accepted(count, fields):
    assert _stored_block(_deck(_grid(count, **fields)))["rows"] == fields["rows"]


def test_a_logo_wall_stores_its_defaults():
    block = _stored_block(_deck(_wall(columns=2, per_row=4)))
    assert block["type"] == "logo_wall"
    assert len(block["groups"]) == 4 and all(len(group["logos"]) == 8 for group in block["groups"])
    assert block["logo_height"] == "md"
    assert block["optical_balance"] is True
    assert block["tone"] == "original"
    assert block["framed"] is False
    assert block["show_labels"] is False
    assert "tile_height" not in block
    # The cards share the height the slide leaves unless told otherwise.
    assert block["fill"] is True
    assert block["groups"][0]["tone"] == "accent"


@pytest.mark.parametrize(
    "wall",
    [
        _wall(groups=MAX_LOGO_WALL_GROUPS + 1, per_group=1),
        _wall(groups=1, per_group=MAX_LOGO_WALL_GROUP_LOGOS + 1),
        _wall(groups=0),
        _wall(groups=2, per_group=0),
        _wall(columns=4),
        _wall(per_row=13),
    ],
    ids=["too-many-groups", "too-many-logos", "no-group", "empty-group", "four-columns", "per-row-13"],
)
def test_a_logo_wall_out_of_bounds_is_refused(wall):
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(wall))


def test_a_callout_without_the_new_fields_keeps_the_historical_look():
    block = _stored_block(_deck({"type": "callout", "icon": "info", "title": "Heads up", "content": "Copy."}))
    assert block["variant"] == "tinted"
    for field in ("header", "accent_bar", "size", "icon_size", "title_size", "padding", "children_gap"):
        assert field not in block


def test_a_callout_takes_its_box_header_and_sizes():
    block = _stored_block(
        _deck(
            {
                "type": "callout",
                "variant": "card",
                "header": "inline",
                "accent_bar": True,
                "size": "lg",
                "icon_size": "sm",
                "title_size": "md",
                "padding": "lg",
                "children_gap": "sm",
                "title": "A card",
                "children": [_grid(4, show_labels=False)],
            }
        )
    )
    assert (block["variant"], block["header"], block["accent_bar"]) == ("card", "inline", True)
    assert (block["size"], block["icon_size"], block["title_size"]) == ("lg", "sm", "md")
    assert (block["padding"], block["children_gap"]) == ("lg", "sm")


@pytest.mark.parametrize("field", [{"variant": "glass"}, {"header": "left"}, {"icon_size": "xl"}])
def test_a_callout_refuses_an_unknown_look(field):
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck({"type": "callout", "title": "A", **field}))


def test_the_reference_teaches_the_wall_and_the_callout_box():
    reference = spec_reference()
    for needle in ("logo_wall", "per_row", "logo_height", "optical_balance", "show_labels", "variant", "header"):
        assert needle in reference, needle


def test_the_logo_wall_example_is_the_sector_wall_and_validates():
    example = copy.deepcopy(BLOCK_EXAMPLES["logo_wall"])
    assert (example["columns"], example["per_row"], example["tone"], example["logo_height"]) == (2, 4, "original", "md")
    assert [len(group["logos"]) for group in example["groups"]] == [8, 8, 8, 8]
    ids = iter(_ids(32))
    for group in example["groups"]:
        group["logos"] = [next(ids) for _ in group["logos"]]
    assert _stored_block(_deck(example))["type"] == "logo_wall"


def test_logo_file_ids_walks_grids_and_walls_in_deck_order_once():
    a, b, c, d = _ids(4)
    spec = {
        "slides": [
            {
                "layout": "content",
                "blocks": [
                    {"type": "callout", "children": [{"type": "logo_grid", "items": [{"file_id": a}, {"file_id": b}]}]},
                    {"type": "logo_wall", "groups": [{"logos": [{"file_id": c}, {"file_id": a}]}, {"logos": [{"file_id": d}]}]},
                ],
            },
            {"layout": "content", "blocks": [{"type": "image", "file_id": str(uuid.uuid4())}]},
        ]
    }
    assert logo_file_ids(spec) == [a, b, c, d]
    assert logo_file_ids(None) == []


# ── Stored decks keep what they drew ─────────────────────────────────────────


def _labelled_grid(**fields) -> dict:
    return {"type": "logo_grid", "items": [{"file_id": _ids(1)[0], "label": "Acme"}], **fields}


def test_a_stored_grid_with_labels_keeps_drawing_them_and_nothing_else_moves():
    stored = {
        "slides": [
            {
                "layout": "content",
                "blocks": [
                    _labelled_grid(),
                    _labelled_grid(show_labels=False),
                    {"type": "logo_grid", "items": [{"file_id": _ids(1)[0]}]},
                    {"type": "callout", "children": [_labelled_grid()]},
                    {"type": "columns", "columns": [[_labelled_grid()]]},
                ],
            }
        ]
    }
    original = copy.deepcopy(stored)
    blocks = keep_stored_logo_labels(stored)["slides"][0]["blocks"]
    assert blocks[0]["show_labels"] is True
    assert blocks[1]["show_labels"] is False  # a choice written down is kept
    assert "show_labels" not in blocks[2]  # nothing to draw, nothing added
    assert blocks[3]["children"][0]["show_labels"] is True
    assert blocks[4]["columns"][0][0]["show_labels"] is True
    assert stored == original  # copy-on-write


def test_the_read_path_keeps_stored_labels_the_write_path_does_not():
    spec = _deck(_labelled_grid())
    read = SpecJSON().process_result_value(copy.deepcopy(spec), dialect=None)
    assert read["slides"][0]["blocks"][0]["show_labels"] is True
    # A grid an agent writes today: labels are alt text unless it asks.
    assert _stored_block(spec)["show_labels"] is False


# ── The geometry ─────────────────────────────────────────────────────────────


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _padded_bar() -> bytes:
    """A 200x100 transparent canvas holding an opaque 100x26 bar at (50, 37)."""
    image = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    image.paste((20, 40, 200, 255), (50, 37, 150, 63))
    return _png(image)


SEAL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="200" height="200">'
    '<circle cx="100" cy="100" r="56" fill="none" stroke="#2563EB" stroke-width="8"/>'
    '<circle cx="100" cy="100" r="34" fill="#2563EB"/></svg>'
)


def test_the_trim_box_of_a_padded_png_is_its_mark():
    meta = analyze_image_geometry(_padded_bar(), "image/png")
    assert meta is not None and meta["v"] == IMAGE_META_VERSION
    assert (meta["w"], meta["h"]) == (200, 100)
    left, top, right, bottom = meta["trim"]
    assert abs(left - 0.25) <= 0.01 and abs(right - 0.75) <= 0.01
    assert abs(top - 0.37) <= 0.015 and abs(bottom - 0.63) <= 0.015
    # An opaque bar fills its box, less the one-pixel slack around it.
    assert meta["ink"] >= 0.9


def test_an_svg_is_measured_through_its_rendering():
    meta = analyze_image_geometry(SEAL_SVG.encode(), "image/svg+xml")
    assert meta is not None
    left, top, right, bottom = meta["trim"]
    # The ring spans 40..160 of 200: a symmetric box around the seal.
    assert abs(left - 0.2) <= 0.01 and abs(right - 0.8) <= 0.01
    assert abs(top - left) <= 0.002 and abs(bottom - right) <= 0.002
    assert 0.3 <= meta["ink"] <= 0.6


def test_an_opaque_image_is_its_own_mark():
    out = io.BytesIO()
    Image.new("RGB", (120, 60), (200, 30, 30)).save(out, format="JPEG")
    meta = analyze_image_geometry(out.getvalue(), "image/jpeg")
    assert meta is not None
    assert meta["trim"] == [0.0, 0.0, 1.0, 1.0]
    assert "ink" not in meta


def test_nothing_readable_means_no_geometry_and_never_an_error():
    blank = _png(Image.new("RGBA", (40, 40), (0, 0, 0, 0)))
    assert analyze_image_geometry(blank, "image/png") is None
    assert analyze_image_geometry(b"not an image", "image/png") is None
    assert asyncio.run(image_geometry(b"\x00\x01garbage", "image/png")) is None


def test_an_svg_is_rasterized_inside_the_analysis_box_whatever_its_shape():
    """Rasterized at a fixed WIDTH, a tall SVG of a hundred bytes allocated a
    512 x 51200 pixmap before any pixel bound was looked at. It is fitted INTO
    the analysis box instead: the long edge is the bound, the ratio survives."""
    tall = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 1000"><rect width="10" height="1000"/></svg>'
    meta = analyze_image_geometry(tall, "image/svg+xml")
    assert meta is not None
    assert max(meta["w"], meta["h"]) <= ANALYSIS_LONG_EDGE
    assert meta["h"] > 50 * meta["w"]


def _use_fan(depth: int, fan: int = 10) -> bytes:
    """A `<use>` of a group of `<use>`s, `depth` levels deep: `fan ** depth` shapes."""
    levels = ['<g id="l0"><rect width="1" height="1"/></g>']
    for level in range(1, depth + 1):
        uses = "".join(f'<use href="#l{level - 1}" x="{i}"/>' for i in range(fan))
        levels.append(f'<g id="l{level}">{uses}</g>')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs>'
        + "".join(levels)
        + f'</defs><use href="#l{depth}"/></svg>'
    ).encode()


def test_an_svg_that_multiplies_its_shapes_never_reaches_the_rasterizer(monkeypatch):
    """A few hundred bytes of nested `<use>` expand to a hundred thousand shapes
    in resvg (seconds of a parse thread, tenfold per level); a marker is drawn
    once per vertex of every path that names it (counted per vertex since
    A diagram's arrow heads are fine, a marker on a thousand vertices
    is not); an embedded image that is not a readable raster, and a reference
    that reaches itself, are refused. Such a file gets no geometry - drawn as
    its file - and is refused before resvg ever sees it."""
    import resvg_py

    seen: list[str] = []
    real = resvg_py.svg_to_bytes

    def spy(**kwargs):
        seen.append(kwargs.get("svg_string") or "")
        return real(**kwargs)

    monkeypatch.setattr(resvg_py, "svg_to_bytes", spy)
    marker_body = "".join(f'<rect x="{i % 10}" y="{i // 10}" width="1" height="1"/>' for i in range(100))
    marker_path = "M0 0 " + " ".join(f"L{i % 97} {i % 89}" for i in range(1000))
    marker = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<defs><marker id="m">{marker_body}</marker></defs>'
        f'<path d="{marker_path}" marker-mid="url(#m)"/></svg>'
    ).encode()
    embedded = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<image href="data:image/png;base64,iVBORw0KGgo=" width="10" height="10"/></svg>'
    )
    cycle = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<g id="a"><rect width="1" height="1"/><use href="#a"/></g></svg>'
    )
    for hostile in (_use_fan(5), marker, embedded, cycle):
        assert analyze_image_geometry(hostile, "image/svg+xml") is None
    assert seen == []
    # A logo that reuses a shape a few times is read as before.
    assert analyze_image_geometry(_use_fan(2), "image/svg+xml") is not None
    assert len(seen) == 1


def test_an_svg_is_parsed_defused_before_it_is_read():
    """The bytes are parsed by `defusedxml` first (`services/svg_raster.py`),
    never handed raw to another XML parser: an entity declaration -
    the XXE and billion-laughs vector, never legitimate in a logo - is
    refused outright."""
    svg = (
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY side "200">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 &side; &side;">'
        b'<rect x="50" y="50" width="100" height="100"/></svg>'
    )
    assert analyze_image_geometry(svg, "image/svg+xml") is None


def test_a_raster_is_decoded_only_as_a_type_its_bytes_spell_and_a_slide_carries():
    """Typed by the bytes first, like `fetch_image_asset`: a TIFF, a PSD or an
    EPS never reaches Pillow's decoder set, whatever it is declared as - a
    slide cannot carry it either. The four slide types are read."""
    image = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    image.paste((20, 40, 200, 255), (50, 37, 150, 63))
    for fmt, declared in (("TIFF", "image/tiff"), ("TIFF", "image/png"), ("BMP", "image/bmp")):
        out = io.BytesIO()
        image.save(out, format=fmt)
        assert analyze_image_geometry(out.getvalue(), declared) is None, (fmt, declared)
    for fmt in ("PNG", "WEBP", "GIF"):
        out = io.BytesIO()
        image.save(out, format=fmt)
        meta = analyze_image_geometry(out.getvalue(), "application/octet-stream")
        assert meta is not None and meta["trim"][0] == pytest.approx(0.25, abs=0.02), fmt


def test_the_renderer_is_served_the_box_never_the_record():
    meta = {"v": 1, "w": 512, "h": 128, "trim": [0.1, 0.2, 0.9, 0.8], "ink": 0.42}
    assert render_geometry(meta) == {"w": 512, "h": 128, "trim": [0.1, 0.2, 0.9, 0.8], "ink": 0.42}
    assert render_geometry({"v": 1}) is None  # read, unreadable
    assert render_geometry(None) is None
    assert render_geometry("junk") is None


def test_the_revision_is_served_even_for_bytes_with_no_box():
    """A file with no box (too big to read, unreadable, or
    not read yet) still has bytes an in-place replacement changes, so its URL
    is versioned all the same - its size then comes from the image itself."""
    key = "template-asset/c/a1b2-huge.png"
    rev = render_geometry({"v": 1, "w": 1, "h": 1, "trim": [0, 0, 1, 1]}, storage_key=key)["rev"]
    assert render_geometry({"v": 1}, storage_key=key) == {"rev": rev}
    assert render_geometry(None, storage_key=key) == {"rev": rev}
    assert render_geometry({"v": 1}, storage_key=None) is None


def test_the_revision_names_the_bytes_and_moves_with_an_in_place_replacement():
    meta = {"v": 1, "w": 512, "h": 128, "trim": [0.1, 0.2, 0.9, 0.8]}
    before = render_geometry(meta, storage_key="template-asset/c/a1b2-logo.svg")["rev"]
    assert len(before) == 12 and all(char in "0123456789abcdef" for char in before)
    assert render_geometry(meta, storage_key="template-asset/c/a1b2-logo.svg")["rev"] == before
    # A replacement keeps the file id and gets a new storage key: a new revision.
    assert render_geometry(meta, storage_key="template-asset/c/c3d4-logo.svg")["rev"] != before
    # The digest is served, never the key it is read from.
    assert "template-asset" not in str(render_geometry(meta, storage_key="template-asset/c/a1b2-logo.svg"))


def test_a_template_asset_serializes_its_render_geometry():
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        filename="acme.svg",
        content_type="image/svg+xml",
        size=10,
        asset_role="customer_logo",
        description=None,
        image_meta={"v": 1, "w": 512, "h": 128, "trim": [0.1, 0.2, 0.9, 0.8]},
        s3_object_name="template-asset/c/a1b2-acme.svg",
        created_at=None,
    )
    served = serialize_asset(asset)
    assert served["image_meta"] == {
        "w": 512,
        "h": 128,
        "trim": [0.1, 0.2, 0.9, 0.8],
        "rev": render_geometry(asset.image_meta, storage_key=asset.s3_object_name)["rev"],
    }
    assert "s3_object_name" not in served
    # An image read as unreadable is versioned; a file never read as an image
    # (a font) carries no image_meta at all.
    asset.image_meta = {"v": 1}
    assert serialize_asset(asset)["image_meta"] == {"rev": served["image_meta"]["rev"]}
    asset.image_meta = None
    assert serialize_asset(asset)["image_meta"] is None


# ── The backfill a save runs, the geometry a read serves ─────────────────────


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Savepoint:
    def __init__(self, log: list[str]):
        self.log = log

    async def __aenter__(self):
        self.log.append("savepoint")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.log.append("rolled back" if exc_type else "released")
        return False


class _Db:
    """A session over in-memory rows: the lookup returns the unread rows, and a
    geometry write is applied only where its WHERE clause still matches."""

    def __init__(self, rows=None, fail: bool = False, fail_write: bool = False):
        self.rows = rows or []
        self.fail = fail
        self.fail_write = fail_write
        self.log: list[str] = []

    def begin_nested(self):
        return _Savepoint(self.log)

    async def execute(self, statement):
        if isinstance(statement, Update):
            if self.fail_write:
                raise RuntimeError("write refused")
            self._apply(statement)
            return None
        if self.fail:
            raise RuntimeError("database down")
        return _Rows([row for row in self.rows if row.image_meta is None])

    def _apply(self, statement) -> None:
        compiled = statement.compile()
        sql = str(compiled)
        # Conditional on the row being still unread AND still naming the object read.
        assert "image_meta IS NULL" in sql and "s3_object_name =" in sql, sql
        params = compiled.params
        file_id = next(value for name, value in params.items() if name.startswith("id"))
        key = next(value for name, value in params.items() if name.startswith("s3_object_name"))
        for row in self.rows:
            if row.id == file_id and row.image_meta is None and row.s3_object_name == key:
                row.image_meta = params["image_meta"]

    async def flush(self):
        if self.fail_write:
            raise RuntimeError("write refused")


def _row(content_type: str, filename: str, key: str, size: int = 4096) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), content_type=content_type, filename=filename, s3_object_name=key, size=size, image_meta=None
    )


def test_a_save_reads_the_geometry_of_its_logos_by_their_bytes(monkeypatch):
    from app.services import file_storage as storage_module

    logo = _row("image/png", "a.png", "k-logo")
    untyped = _row("application/octet-stream", "b.png", "k-untyped")
    pdf = _row("application/pdf", "c.pdf", "k-pdf")
    lost = _row("image/png", "d.png", "k-lost")
    huge = _row("image/png", "e.png", "k-huge", size=MAX_ANALYSIS_BYTES + 1)
    downloaded: list[str] = []

    async def download(key: str) -> bytes:
        downloaded.append(key)
        if key == "k-lost":
            raise FileNotFoundError(key)
        return b"%PDF-1.7\n%%EOF" if key == "k-pdf" else _padded_bar()

    monkeypatch.setattr(storage_module.file_storage, "download", download)
    rows = [logo, untyped, pdf, lost, huge]
    read = asyncio.run(backfill_image_geometry(_Db(rows), [str(row.id) for row in rows] + ["not-a-uuid"]))
    assert read == 3
    assert logo.image_meta["trim"][0] == pytest.approx(0.25, abs=0.01)
    # Judged by its bytes, not its declared type: an untyped PNG is a logo.
    assert untyped.image_meta["trim"][0] == pytest.approx(0.25, abs=0.01)
    # Not an image: marked read so it is never downloaded again.
    assert pdf.image_meta == {"v": IMAGE_META_VERSION}
    # A download that failed is retried by a later save.
    assert lost.image_meta is None
    # Too big to be read at all: marked read without a download.
    assert huge.image_meta == {"v": IMAGE_META_VERSION} and "k-huge" not in downloaded


def test_an_uploaded_asset_is_read_by_its_bytes_not_its_declared_type():
    from app.api.v1.template_containers import _asset_geometry

    untyped = asyncio.run(_asset_geometry(_padded_bar(), "application/octet-stream"))
    assert untyped is not None and untyped["trim"][0] == pytest.approx(0.25, abs=0.01)
    assert asyncio.run(_asset_geometry(b"wOF2\x00\x01\x00\x00font", "font/woff2")) is None


def test_a_save_whose_logos_all_carry_their_geometry_reads_nothing_and_never_raises(monkeypatch):
    """The common save - every logo already read - has nothing to download: the
    backfill returns without reading, never with an error (an empty wait once
    raised here and turned every such deck save into a 500)."""
    from app.services import file_storage as storage_module

    async def download(key: str) -> bytes:
        raise AssertionError(f"nothing is downloaded, {key} was read")

    monkeypatch.setattr(storage_module.file_storage, "download", download)
    row = _row("image/png", "a.png", "k-logo")
    row.image_meta = {"v": 1, "w": 10, "h": 10, "trim": [0, 0, 1, 1]}
    db = _Db([row])
    assert asyncio.run(backfill_image_geometry(db, [str(row.id)])) == 0
    assert db.log == ["savepoint", "released"]


def test_the_backfill_is_bounded_and_never_raises(monkeypatch):
    from app.services import file_storage as storage_module

    async def download(_key: str) -> bytes:
        return _padded_bar()

    monkeypatch.setattr(storage_module.file_storage, "download", download)
    rows = [_row("image/png", f"{i}.png", f"k{i}") for i in range(5)]
    assert asyncio.run(backfill_image_geometry(_Db(rows), [str(r.id) for r in rows], limit=2)) == 2
    assert [row.image_meta is not None for row in rows] == [True, True, False, False, False]
    assert asyncio.run(backfill_image_geometry(_Db(), ["nope"])) == 0


def test_a_geometry_failure_rolls_back_its_savepoint_never_the_save(monkeypatch):
    """On PostgreSQL a failed statement aborts the whole transaction: the
    lookup and the writes run in SAVEPOINTs of the caller's transaction, so a
    failure costs the geometry and the deck save still commits."""
    from app.services import file_storage as storage_module

    async def download(_key: str) -> bytes:
        return _padded_bar()

    monkeypatch.setattr(storage_module.file_storage, "download", download)
    lookup = _Db(fail=True)
    assert asyncio.run(backfill_image_geometry(lookup, [str(uuid.uuid4())])) == 0
    assert lookup.log == ["savepoint", "rolled back"]

    row = _row("image/png", "a.png", "k-logo")
    write = _Db([row], fail_write=True)
    assert asyncio.run(backfill_image_geometry(write, [str(row.id)])) == 0
    assert write.log == ["savepoint", "released", "savepoint", "rolled back"]


def test_a_reading_never_overwrites_a_replacement_that_landed_meanwhile(monkeypatch):
    """The backfill read an unread row, downloaded its OLD
    object, and wrote that box over an in-place replacement committed in the
    meantime - the served `rev` then named the new bytes and the trim the old
    ones. The write only lands on a row still unread AND still naming the
    object that was read."""
    from app.services import file_storage as storage_module

    row = _row("image/png", "a.png", "k-old")
    fresh = {"v": 1, "w": 64, "h": 64, "trim": [0.0, 0.0, 1.0, 1.0]}

    async def download(key: str) -> bytes:
        # The replace route commits while the old bytes are on their way.
        row.s3_object_name, row.image_meta = "k-new", fresh
        return _padded_bar()

    monkeypatch.setattr(storage_module.file_storage, "download", download)
    asyncio.run(backfill_image_geometry(_Db([row]), [str(row.id)]))
    assert row.image_meta is fresh and row.s3_object_name == "k-new"


def test_the_backfill_reads_a_few_files_at_a_time_within_a_wall_clock_budget(monkeypatch):
    """A save holds its deck's row lock, and the reads ran one
    after the other. They run a few at a time under a wall-clock budget: a
    store that hangs costs its files' geometry for this save - left unread for
    a later one - never the lock for longer than the budget."""
    from app.services import file_storage as storage_module

    rows = [_row("image/png", f"{i}.png", f"k{i}") for i in range(4)]
    stuck = _row("image/png", "stuck.png", "k-stuck")
    png = _padded_bar()
    # One after the other, the first read is still waiting for a second one
    # here when the budget runs out.
    two_at_once = Rendezvous(2)

    async def download(key: str) -> bytes:
        if key == "k-stuck":
            await asyncio.sleep(60)
        await two_at_once.arrive(key)
        return png

    monkeypatch.setattr(storage_module.file_storage, "download", download)
    started = time.monotonic()
    read = asyncio.run(
        backfill_image_geometry(_Db([*rows, stuck]), [str(r.id) for r in [*rows, stuck]], budget_seconds=1.0)
    )
    assert time.monotonic() - started < 10
    assert read == 4
    assert all(row.image_meta and row.image_meta["trim"][0] == pytest.approx(0.25, abs=0.01) for row in rows)
    assert stuck.image_meta is None


class _MetaRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def test_a_deck_read_serves_each_logos_box_keyed_as_the_spec_spells_it():
    measured, unreadable = uuid.uuid4(), uuid.uuid4()
    geometry = {"v": 1, "w": 100, "h": 50, "trim": [0, 0, 1, 1]}

    class Db:
        async def execute(self, _statement):
            return _MetaRows([(measured, geometry, "chat/c/logo.png"), (unreadable, {"v": 1}, "chat/c/x.png")])

    spec = _deck({"type": "logo_grid", "items": [{"file_id": str(measured).upper()}, {"file_id": str(unreadable)}]})
    served = asyncio.run(load_logo_asset_meta(Db(), spec))
    rev = render_geometry(geometry, storage_key="chat/c/logo.png")["rev"]
    # A logo with no box is still versioned by its bytes (drawn at its own size).
    unreadable_rev = render_geometry(None, storage_key="chat/c/x.png")["rev"]
    assert served == {
        str(measured).upper(): {"w": 100, "h": 50, "trim": [0, 0, 1, 1], "rev": rev},
        str(unreadable): {"rev": unreadable_rev},
    }
    assert asyncio.run(load_logo_asset_meta(Db(), {"slides": []})) == {}


# ── The layout audit ─────────────────────────────────────────────────────────


def _spec(titles: list[str]) -> dict:
    return {"title": "Deck", "slides": [{"layout": "content", "title": t} for t in titles]}


def _slide(observations: list[dict]) -> dict:
    return {"index": 0, "slide_id": "s0", "layout": "content", "findings": observations}


def _logo(block: str = "logo-grid", height: float = 40, coverage: float = 0.8, text: str = "Acme") -> dict:
    return {"kind": "logo", "block": block, "height": height, "coverage": coverage, "text": text}


def test_a_logo_under_the_readable_height_is_a_dense_finding():
    (finding,) = findings_from_observations([_slide([_logo("logo-wall", height=20)])], _spec(["A"]))
    assert finding.kind == "logo" and finding.severity == "dense"
    assert f"20px high (under {LOGO_MIN_HEIGHT_PX}px)" in finding.message
    assert "per_row" in finding.message


def test_logos_lost_in_their_box_are_advisory_and_never_read_as_the_bar():
    (finding,) = findings_from_observations([_slide([_logo(coverage=0.349)])], _spec(["A"]))
    assert finding.severity == "advisory"
    # Floored: 34.9% is under the 35% bar and says so.
    assert f"cover 34% of their box (under {round(LOGO_MIN_COVERAGE * 100)}%)" in finding.message
    assert "logos too small for their space" in finding.message
    assert "set rows" in finding.message


def test_an_architecture_logo_under_its_logo_height_step_is_named():
    observation = {"kind": "logo", "block": "architecture", "height": 61.4, "step": 72, "text": "Stage 1"}
    (finding,) = findings_from_observations([_slide([observation])], _spec(["A"]))
    assert finding.kind == "logo" and finding.severity == "advisory"
    assert '"Stage 1" 61px high, under its logo_height step (72px)' in finding.message
    assert "Lower logo_height" in finding.message
    # At its step (within a pixel), nothing is said.
    at_step = {**observation, "height": 71.5}
    assert findings_from_observations([_slide([at_step])], _spec(["A"])) == []


def test_the_architecture_logo_audit_reads_the_drawn_height_not_the_file_box():
    """A sized node logo is cropped to its ink by a nested svg viewBox; its
    bounding box is the whole file's, so a capped wordmark on a tall margin
    measured that way never read as short (retest of #10143: 53 px at lg, no
    finding). The audit reads ``data-logo-drawn`` times the figure's scale."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    audit = LAYOUT_AUDIT_JS[LAYOUT_AUDIT_JS.index('querySelectorAll("[data-logo-step]")') :]
    audit = audit[: audit.index('kind: "logo", block: "architecture"')]
    assert "Number(mark.dataset.logoDrawn)" in audit
    assert "drawn * scale" in audit


def test_the_architecture_logo_audit_scales_by_the_marks_screen_ctm():
    """Retest of #10186: a 256x124 wordmark at lg in a five-node 16:9 row drew
    about 53 px under its 56 px step and was not named. The figure's width over
    its viewBox width overstates the meet scale when the svg box is wider than
    the drawing; the mark's screen CTM is the scale it is painted at, and the
    fallback takes the smaller axis ratio. The threshold is half a pixel."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    audit = LAYOUT_AUDIT_JS[LAYOUT_AUDIT_JS.index('querySelectorAll("[data-logo-step]")') :]
    audit = audit[: audit.index('kind: "logo", block: "architecture"')]
    assert "mark.getScreenCTM()" in audit
    assert "Math.hypot(ctm.b, ctm.d)" in audit
    assert "Math.min(sr.width / box.width, sr.height / box.height)" in audit
    assert "height >= step - 0.5" in audit
    assert "step - 1" not in audit


def test_a_capped_wordmark_at_lg_is_named():
    observation = {"kind": "logo", "block": "architecture", "height": 53.2, "step": 56, "text": "QRadar"}
    (finding,) = findings_from_observations([_slide([observation])], _spec(["A"]))
    assert finding.severity == "advisory"
    assert "53px high, under its logo_height step (56px)" in finding.message


@pytest.mark.parametrize("height", [53.0, 54.4, 55.4])
def test_a_capped_wordmark_a_few_px_under_lg_is_named(height):
    observation = {"kind": "logo", "block": "architecture", "height": height, "step": 56, "text": "QRadar"}
    (finding,) = findings_from_observations([_slide([observation])], _spec(["A"]))
    assert finding.severity == "advisory"
    assert "under its logo_height step (56px)" in finding.message


@pytest.mark.parametrize("height", [55.5, 56.0])
def test_an_architecture_logo_at_its_step_is_not_named(height):
    observation = {"kind": "logo", "block": "architecture", "height": height, "step": 56, "text": "Square"}
    assert findings_from_observations([_slide([observation])], _spec(["A"])) == []


def test_readable_logos_that_fill_their_box_are_not_reported():
    assert findings_from_observations([_slide([_logo(height=40, coverage=0.6)])], _spec(["A"])) == []


def test_a_shrunk_slide_is_told_to_cut_not_to_raise_its_logos():
    findings = findings_from_observations([_slide([{"kind": "fit", "scale": 0.7}, _logo(height=18)])], _spec(["A"]))
    assert [finding.kind for finding in findings] == ["fit"]


def test_two_logo_blocks_drawn_alike_are_reported_once():
    alike = findings_from_observations([_slide([_logo(height=20), _logo(height=20)])], _spec(["A"]))
    assert len(alike) == 1
    different = findings_from_observations([_slide([_logo(height=20), _logo(height=22)])], _spec(["A"]))
    assert len(different) == 2


def test_the_audit_measures_logo_blocks_by_their_marks_and_packed_rows():
    assert '".deck-logo-grid, .deck-logo-wall"' in LAYOUT_AUDIT_JS
    # Each packed row counts its tallest cell and one gap.
    assert "packed += rows * (parseFloat(getComputedStyle(field).rowGap) || 0);" in LAYOUT_AUDIT_JS
    # A lone logo is placed, not lost: a row of one says nothing about the
    # width, and a field of one is not measured (a one-logo wall card is legit).
    assert "if (cells > 1) span = Math.max(span, right - left);" in LAYOUT_AUDIT_JS
    assert "if (rows && logos > 1) {" in LAYOUT_AUDIT_JS
    assert "const width = span > 0 ? Math.min(1, span / fr.width) : 1;" in LAYOUT_AUDIT_JS
    # A mark is one picture: the image cropped inside it is never measured alone.
    assert 'el.closest("[data-export-picture]")' in LAYOUT_AUDIT_JS
    assert "logo-(field|row|cell|mark|ink|wall-groups|group|group-head|group-icon)" in LAYOUT_AUDIT_JS
    assert "callout-(head|body)" in LAYOUT_AUDIT_JS


def test_the_extractor_captures_a_mark_as_one_picture_and_tags_wall_cards():
    assert 'el.hasAttribute("data-export-picture")' in EXTRACT_PRIMITIVES_JS
    assert 'el.closest("[data-export-group]")' in EXTRACT_PRIMITIVES_JS
    assert "group: groupOf(el)" in EXTRACT_PRIMITIVES_JS
    logos = (FRONTEND_PRESENTATION / "blocks" / "logos.tsx").read_text(encoding="utf-8")
    assert 'data-export-picture="true"' in logos
    assert 'data-export-group="true"' in logos
    # Reveal reads a nested <section> as a vertical slide: a card is never one.
    markup = re.sub(r"/\*.*?\*/|//[^\n]*", "", logos, flags=re.DOTALL)
    assert "<section" not in markup


# ── The editable PPTX ────────────────────────────────────────────────────────


def _picture(capture: str, x: float, y: float, w: float, h: float, group: str | None) -> dict:
    return {"kind": "image", "id": capture, "x": x, "y": y, "w": w, "h": h, "opacity": 1, "group": group}


def _wall_slide() -> dict:
    card = {
        "kind": "rect",
        "x": 100,
        "y": 200,
        "w": 840,
        "h": 380,
        "opacity": 1,
        "fill": {"r": 255, "g": 255, "b": 255, "a": 1},
        "line": {"r": 200, "g": 200, "b": 200, "a": 1, "width": 1},
        "edges": None,
        "radius": 16,
        "group": "g0-1",
    }
    return {
        "index": 0,
        "w": 1920,
        "h": 1080,
        "background": {"r": 250, "g": 250, "b": 250, "a": 1},
        "items": [
            card,
            _picture("p1", 132.5, 300.25, 160.5, 40, "g0-1"),
            _picture("p2", 420, 300, 60, 60, "g0-1"),
            _picture("p3", 760.75, 452.5, 147.25, 40, "g0-1"),
            _picture("chrome", 1700, 980, 120, 40, None),
        ],
    }


def _absolute_pictures(shapes, origin=(0.0, 0.0, 1.0, 1.0, 0.0, 0.0)):
    """(x, y, w, h) in EMU for every picture, group transforms applied."""
    ox, oy, sx, sy, cx, cy = origin
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            xfrm = shape._element.grpSpPr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm")
            off, ext = xfrm.find("{*}off"), xfrm.find("{*}ext")
            ch_off, ch_ext = xfrm.find("{*}chOff"), xfrm.find("{*}chExt")
            yield from _absolute_pictures(
                shape.shapes,
                (
                    ox + (int(off.get("x")) - cx) * sx,
                    oy + (int(off.get("y")) - cy) * sy,
                    sx * int(ext.get("cx")) / max(1, int(ch_ext.get("cx"))),
                    sy * int(ext.get("cy")) / max(1, int(ch_ext.get("cy"))),
                    int(ch_off.get("x")),
                    int(ch_off.get("y")),
                ),
            )
        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            yield (ox + (shape.left - cx) * sx, oy + (shape.top - cy) * sy, shape.width * sx, shape.height * sy)


def test_a_wall_card_exports_as_one_group_with_its_logos_in_place():
    captures = {name: _padded_bar() for name in ("p1", "p2", "p3", "chrome")}
    data = pptx_writer.build_native_pptx([_wall_slide()], captures=captures, aspect_ratio="16:9")
    slide = PptxPresentation(io.BytesIO(data)).slides[0]
    kinds = [shape.shape_type for shape in slide.shapes]
    assert kinds == [MSO_SHAPE_TYPE.GROUP, MSO_SHAPE_TYPE.PICTURE]
    group = slide.shapes[0]
    assert [shape.shape_type for shape in group.shapes] == [
        MSO_SHAPE_TYPE.AUTO_SHAPE,
        MSO_SHAPE_TYPE.PICTURE,
        MSO_SHAPE_TYPE.PICTURE,
        MSO_SHAPE_TYPE.PICTURE,
    ]
    emu_per_px = 12_192_000 / 1920
    measured = [item for item in _wall_slide()["items"] if item["kind"] == "image"]
    placed = list(_absolute_pictures(slide.shapes))
    assert len(placed) == len(measured)
    for item, (x, y, w, h) in zip(measured, placed, strict=True):
        for got, want in ((x, item["x"]), (y, item["y"]), (w, item["w"]), (h, item["h"])):
            assert abs(got / emu_per_px - want) <= 2  # within 2 px, as measured


def test_a_group_left_empty_leaves_no_shape():
    lone = {
        "index": 0,
        "w": 1920,
        "h": 1080,
        "background": {"r": 0, "g": 0, "b": 0, "a": 1},
        "items": [_picture("missing", 10, 10, 50, 50, "g0-1")],
    }
    slide = PptxPresentation(io.BytesIO(pptx_writer.build_native_pptx([lone], captures={}, aspect_ratio="16:9"))).slides[0]
    assert len(slide.shapes) == 0
