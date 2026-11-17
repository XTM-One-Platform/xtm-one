"""A figure draws a template asset as its twin for the video's scheme.

* the tone: how light a picture's ink draws, read off its pixels - the seeded
  brand logos and their declared twins told apart, an opaque picture read as
  its own surface, two shapes told apart;
* the twin: an asset of the same template the picture's description names by
  file, or that names it, drawing the same shape in a tone that suits the
  scheme better - never a picture merely mentioned, never one the person may
  not read;
* the figure: a scene repointed at a light deck's slide with one edit draws
  the slide's light-theme logos as their dark twins on a dark video - its
  still, its pictures and the save say so -, no ``block_spec`` in the spec.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from app.services import image_geometry
from app.services.video_studio import figure_twins, figures, projects
from app.services.video_studio.figure_twins import Asset, Tone
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

BRAND = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "brand_assets" / "filigran"
CONTAINER = "00000000-0000-0000-0000-00000000c373"
DECK_ID = "00000000-0000-0000-0000-00000000d373"


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _mark(ink: tuple[int, int, int, int], *, size=(400, 100), background=(0, 0, 0, 0)) -> bytes:
    image = Image.new("RGBA", size, background)
    ImageDraw.Draw(image).rectangle((20, 20, size[0] - 20, size[1] - 20), fill=ink)
    return _png(image)


async def _read_tone(data: bytes, content_type: str) -> Tone:
    read = await image_geometry.image_tone(data, content_type)
    assert read is not None and read["v"] == image_geometry.IMAGE_TONE_VERSION
    return Tone(read["tone"], read["opaque"], read["ratio"])


def _tone(data: bytes, content_type: str = "image/png") -> Tone:
    return asyncio.run(_read_tone(data, content_type))


# ── The tone ──────────────────────────────────────────────────────────────


def test_a_pictures_tone_is_its_inks_luminance_and_its_box_its_proportions():
    white, ink = _tone(_mark((255, 255, 255, 255))), _tone(_mark((10, 10, 10, 255)))
    assert white.tone == pytest.approx(1.0) and ink.tone < 0.01 and not white.opaque
    # The ink's box, not the file's: 360 x 60.
    assert white.ratio == pytest.approx(6.0, rel=0.03) and white.same_shape(ink)
    # On a dark figure the light ink suits, on a light one the dark ink.
    assert white.suits("dark") > ink.suits("dark") and ink.suits("light") > white.suits("light")
    # An opaque picture is its own surface: a dark one suits a dark figure.
    night = _tone(_mark((10, 10, 10, 255), background=(20, 20, 30, 255)))
    assert night.opaque and night.suits("dark") > 0.9 and not night.same_shape(ink)
    # Another shape is not a twin.
    assert not white.same_shape(_tone(_mark((10, 10, 10, 255), size=(100, 100))))
    assert asyncio.run(image_geometry.image_tone(b"not an image", "image/png")) is None
    assert asyncio.run(image_geometry.image_tone(_png(Image.new("RGBA", (40, 40))), "image/png")) is None


@pytest.mark.parametrize("name", ["opencti-logo", "openaev-logo", "xtm-one-logo", "xtm-hub-logo", "opencrq-mark"])
def test_each_seeded_light_theme_logo_and_its_declared_twin_draw_one_shape_in_two_tones(name):
    dark = _tone((BRAND / f"{name}.svg").read_bytes(), "image/svg+xml")
    light = _tone((BRAND / f"{name}-light.svg").read_bytes(), "image/svg+xml")
    assert dark.same_shape(light)
    assert dark.suits("dark") - light.suits("dark") >= figure_twins.MIN_TONE_GAP
    assert light.suits("light") - dark.suits("light") >= figure_twins.MIN_TONE_GAP


def test_a_pictures_tone_is_read_once_per_stored_object(monkeypatch):
    from app.services.file_storage import file_storage
    from app.services.video_studio import render_cache

    kept: dict[tuple[str, str], object] = {}
    downloads: list[str] = []

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    async def download(key):
        downloads.append(key)
        return _mark((255, 255, 255, 255))

    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(file_storage, "download", download)
    logo = Asset(str(uuid.uuid4()), "logo.png", "", CONTAINER, "templates/logo.png", "image/png", 1000)
    first = asyncio.run(figure_twins._tone_of(logo))
    assert first is not None and first.tone == pytest.approx(1.0)
    assert asyncio.run(figure_twins._tone_of(logo)) == first and downloads == ["templates/logo.png"]
    # Replaced in place, the file is a new object: read again.
    replaced = Asset(logo.id, logo.filename, "", CONTAINER, "templates/logo-2.png", "image/png", 1000)
    assert asyncio.run(figure_twins._tone_of(replaced)) == first and len(downloads) == 2
    # Too large to be read: never downloaded, remembered as unread.
    huge = Asset(str(uuid.uuid4()), "poster.png", "", CONTAINER, "templates/poster.png", "image/png", 10**9)
    assert asyncio.run(figure_twins._tone_of(huge)) is None and asyncio.run(figure_twins._tone_of(huge)) is None
    assert len(downloads) == 2


# ── The twin ──────────────────────────────────────────────────────────────


def _manifest() -> dict[str, Asset]:
    """The seeded brand template's assets, as their rows read: by file name."""
    entries = json.loads((BRAND / "manifest.json").read_text(encoding="utf-8"))["assets"]
    return {
        entry["file"]: Asset(
            str(uuid.uuid5(uuid.NAMESPACE_URL, entry["file"])),
            entry["file"],
            entry.get("description") or "",
            CONTAINER,
            f"templates/{entry['file']}",
            "image/svg+xml" if entry["file"].endswith(".svg") else "image/webp",
            1000,
        )
        for entry in entries
    }


def test_a_twin_is_an_asset_its_description_names_by_file_or_that_names_it():
    assets = _manifest()
    siblings = list(assets.values())
    # The light-theme logo names its light-on-dark twin; the twin, named by it, finds it back.
    assert [a.filename for a in figure_twins.named(assets["opencti-logo-light.svg"], siblings)] == ["opencti-logo.svg"]
    assert [a.filename for a in figure_twins.named(assets["opencti-logo.svg"], siblings)] == ["opencti-logo-light.svg"]
    # A name inside a longer one is no name: "logo.svg" is in no description as a file of its own.
    assert not figure_twins._names("the opencti-logo.svg twin", "logo.svg")
    assert figure_twins._names("the light-theme twin of OpenCTI-Mark.svg.", "opencti-mark.svg")
    # The mark names its twin AND the logo of its own tone: only the one drawing its shape is its twin.
    mark = assets["opencrq-mark-light.svg"]
    named = figure_twins.named(mark, siblings)
    assert {a.filename for a in named} == {"opencrq-mark.svg", "opencrq-logo-light.svg"}
    tones = {
        "opencrq-mark-light.svg": Tone(0.02, False, 1.0),
        "opencrq-mark.svg": Tone(0.55, False, 1.0),
        "opencrq-logo-light.svg": Tone(0.03, False, 4.2),
    }
    candidates = [(a, tones[a.filename]) for a in named]
    assert figure_twins.pick(tones[mark.filename], candidates, "dark").filename == "opencrq-mark.svg"
    # Drawn for its scheme already, or too close in tone: kept.
    assert figure_twins.pick(tones[mark.filename], candidates, "light") is None
    assert figure_twins.pick(Tone(0.5, False, 1.0), [(named[0], Tone(0.55, False, 1.0))], "dark") is None


def _deck_slide(ids: dict[str, str]) -> dict:
    """A light deck's architecture: four product logos placed for the light theme."""
    return {
        "layout": "content",
        "blocks": [
            {
                "type": "architecture",
                "zones": [
                    {"label": "Platform", "logo_file_id": ids["xtm-one-logo-light.svg"], "row": 1, "col": 1, "col_span": 4}
                ],
                "nodes": [
                    {"id": "intel", "label": "Intel", "logo_file_id": ids["opencti-logo-light.svg"], "row": 1, "col": 1},
                    {"id": "aev", "label": "Validation", "logo_file_id": ids["openaev-logo-light.svg"].upper(), "row": 1, "col": 2},
                    {"id": "hub", "label": "Hub", "logo_file_id": ids["xtm-hub-logo-light.svg"], "row": 1, "col": 3},
                    {"id": "partner", "label": "Partner", "logo_file_id": ids["not-a-template-asset"], "row": 1, "col": 4},
                ],
            }
        ],
    }


@pytest.fixture
def brand(monkeypatch):
    """The seeded brand template as the twins read it, each logo's tone off its own file."""
    assets = _manifest()
    ids = {name: asset.id for name, asset in assets.items()}
    ids["not-a-template-asset"] = str(uuid.uuid4())
    by_id = {asset.id: asset for asset in assets.values()}
    unreadable: set[str] = set()
    reads: list[str] = []

    async def template_assets(_db, wanted):
        return [by_id[str(uid)] for uid in wanted if str(uid) in by_id]

    async def siblings(_db, containers):
        assert containers == {CONTAINER}
        return list(assets.values())

    async def tone_of(asset):
        reads.append(asset.filename)
        return await _read_tone((BRAND / asset.filename).read_bytes(), asset.content_type)

    async def readable(_db, file_id, _user):
        return file_id not in unreadable

    monkeypatch.setattr(figure_twins, "_template_assets", template_assets)
    monkeypatch.setattr(figure_twins, "_siblings", siblings)
    monkeypatch.setattr(figure_twins, "_tone_of", tone_of)
    monkeypatch.setattr(figure_twins, "_readable", readable)
    return SimpleNamespace(ids=ids, unreadable=unreadable, reads=reads)


def test_a_light_decks_logos_are_drawn_as_their_dark_twins_on_a_dark_figure(brand):
    slide = _deck_slide(brand.ids)
    source = figures.FigureSource("architecture", slide, uuid.UUID(DECK_ID), frozenset(figures.figure_file_ids(slide)))
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    themed = asyncio.run(figure_twins.themed(None, source, scheme="dark", user=user))
    # Only the pictures with a declared twin, and their twins, are read.
    assert sorted(brand.reads) == sorted(
        f"{name}{tone}.svg" for name in ("xtm-one-logo", "opencti-logo", "openaev-logo", "xtm-hub-logo") for tone in ("", "-light")
    )
    block = themed.slide["blocks"][0]
    drawn = [part["logo_file_id"] for part in (*block["zones"], *block["nodes"])]
    assert drawn == [
        brand.ids["xtm-one-logo.svg"],
        brand.ids["opencti-logo.svg"],
        brand.ids["openaev-logo.svg"],
        brand.ids["xtm-hub-logo.svg"],
        brand.ids["not-a-template-asset"],
    ]
    # What the figure page may fetch follows the slide; the deck it reads through stays.
    assert themed.files == frozenset(drawn) and themed.presentation_id == source.presentation_id
    assert {(twin.filename, twin.twin_filename) for twin in themed.twins} == {
        (f"{name}-light.svg", f"{name}.svg") for name in ("xtm-one-logo", "opencti-logo", "openaev-logo", "xtm-hub-logo")
    }
    # The deck's own slide is never written to.
    assert slide["blocks"][0]["nodes"][0]["logo_file_id"] == brand.ids["opencti-logo-light.svg"]
    # On a light figure, the same slide is drawn as placed.
    assert asyncio.run(figure_twins.themed(None, source, scheme="light", user=user)) is source
    # A twin the person may not read is never drawn: the picture stays.
    brand.unreadable.add(brand.ids["opencti-logo.svg"])
    kept = asyncio.run(figure_twins.themed(None, source, scheme="dark", user=user))
    assert kept.slide["blocks"][0]["nodes"][0]["logo_file_id"] == brand.ids["opencti-logo-light.svg"]
    assert kept.slide["blocks"][0]["nodes"][1]["logo_file_id"] == brand.ids["openaev-logo.svg"]


# ── A retired raster ──────────────────────────────────────────────


def _raster_slide(file_id: str) -> dict:
    """A light deck's slide 7: its architecture places the OpenCRQ logo the template keeps as a raster."""
    return {
        "layout": "content",
        "blocks": [{"type": "architecture", "nodes": [{"id": "crq", "label": "Risk", "logo_file_id": file_id, "row": 1, "col": 1}]}],
    }


def _themed(slide: dict, scheme: str):
    source = figures.FigureSource("architecture", slide, uuid.UUID(DECK_ID), frozenset(figures.figure_file_ids(slide)))
    return asyncio.run(figure_twins.themed(None, source, scheme=scheme, user=SimpleNamespace(id=uuid.uuid4(), role="user")))


def test_a_raster_is_drawn_as_its_vector_successors_twin_on_a_dark_video(brand):
    slide = _raster_slide(brand.ids["opencrq-logo-light.png"])
    themed = _themed(slide, "dark")
    # Its successor is the vector it names, drawn in its tone and shape; its twin for the dark video is the light-on-dark SVG.
    assert themed.slide["blocks"][0]["nodes"][0]["logo_file_id"] == brand.ids["opencrq-logo.svg"]
    assert themed.files == frozenset({brand.ids["opencrq-logo.svg"]})
    [twin] = themed.twins
    assert (twin.filename, twin.twin_filename, twin.sharpened, twin.through) == (
        "opencrq-logo-light.png",
        "opencrq-logo.svg",
        True,
        "opencrq-logo-light.svg",
    )
    # The deck is not changed.
    assert slide["blocks"][0]["nodes"][0]["logo_file_id"] == brand.ids["opencrq-logo-light.png"]
    # The save names the substitution.
    preview = SimpleNamespace(scene_id="s05", document={"presentation": {"theme": "dark"}}, source=themed)
    assert figures.twin_lines([preview]) == [
        "Scene s05: its figure draws 1 raster as the vector it names for the dark video, sharp at any size, the deck "
        "left as it is: opencrq-logo-light.png as opencrq-logo.svg (through opencrq-logo-light.svg)."
    ]


def test_a_raster_on_a_video_of_its_own_scheme_is_drawn_as_its_vector(brand):
    themed = _themed(_raster_slide(brand.ids["opencrq-logo-light.png"]), "light")
    [twin] = themed.twins
    assert (twin.twin_filename, twin.sharpened, twin.through) == ("opencrq-logo-light.svg", True, "")
    assert themed.slide["blocks"][0]["nodes"][0]["logo_file_id"] == brand.ids["opencrq-logo-light.svg"]


def test_a_raster_whose_successor_may_not_be_read_is_drawn_as_placed(brand):
    brand.unreadable.add(brand.ids["opencrq-logo-light.svg"])
    slide = _raster_slide(brand.ids["opencrq-logo-light.png"])
    source = figures.FigureSource("architecture", slide, None, frozenset(figures.figure_file_ids(slide)))
    assert asyncio.run(figure_twins.themed(None, source, scheme="dark", user=None)) is source


def test_a_raster_twin_is_drawn_as_its_own_vector(brand, monkeypatch):
    """Rows seeded before the vector logo name the raster twins of each other; the dark raster names its SVG."""
    assets = _manifest()
    light, dark = assets["opencrq-logo-light.png"], assets["opencrq-logo.png"]
    renamed = {
        light.id: Asset(light.id, light.filename, "OpenCRQ logo, dark-on-light; opencrq-logo.png is the light-on-dark twin.", CONTAINER, light.storage_key, light.content_type, 1000),
        dark.id: Asset(dark.id, dark.filename, "OpenCRQ logo, light-on-dark; place opencrq-logo.svg instead.", CONTAINER, dark.storage_key, dark.content_type, 1000),
    }
    rows = [renamed.get(asset.id, asset) for asset in assets.values()]

    async def siblings(_db, _containers):
        return rows

    async def template_assets(_db, wanted):
        return [asset for asset in rows if uuid.UUID(asset.id) in set(wanted)]

    monkeypatch.setattr(figure_twins, "_siblings", siblings)
    monkeypatch.setattr(figure_twins, "_template_assets", template_assets)
    themed = _themed(_raster_slide(light.id), "dark")
    [twin] = themed.twins
    assert (twin.twin_filename, twin.sharpened, twin.through) == ("opencrq-logo.svg", True, "opencrq-logo.png")


def test_a_successor_is_a_vector_of_the_rasters_shape_and_tone():
    def asset(name: str, kind: str) -> Asset:
        return Asset(str(uuid.uuid4()), name, "", CONTAINER, f"templates/{name}", kind, 1000)

    raster = asset("logo.png", "image/png")
    mine = Tone(0.02, False, 4.8)
    same = asset("logo.svg", "image/svg+xml; charset=utf-8")
    other_tone, other_shape, raster_twin = asset("logo-dark.svg", "image/svg+xml"), asset("mark.svg", "image/svg+xml"), asset("logo-2.png", "image/png")
    candidates = [
        (other_tone, Tone(0.85, False, 4.8)),
        (other_shape, Tone(0.02, False, 1.0)),
        (raster_twin, Tone(0.02, False, 4.8)),
        (same, Tone(0.03, False, 4.6)),
    ]
    assert figure_twins.successor(raster, mine, candidates) == same
    # A vector is already sharp; a raster naming no vector of its shape and tone has none.
    assert figure_twins.successor(same, mine, candidates) is None
    assert figure_twins.successor(raster, mine, candidates[:3]) is None


def test_a_figure_whose_twins_cannot_be_read_is_drawn_as_its_slide_places_it(brand, monkeypatch):
    slide = _deck_slide(brand.ids)
    source = figures.FigureSource("architecture", slide, None, frozenset(figures.figure_file_ids(slide)))

    async def broken(_db, _wanted):
        raise RuntimeError("database gone")

    monkeypatch.setattr(figure_twins, "_template_assets", broken)
    assert asyncio.run(figure_twins.themed(None, source, scheme="dark", user=None)) is source


# ── The figure scene ──────────────────────────────────────────────────────


def test_a_scene_repointed_at_a_light_decks_slide_draws_its_logos_dark_on_a_dark_video(brand, monkeypatch):
    from app.api.v1 import helpers
    from app.services import presentations

    # The scene had copied the block into itself; one edit points it at the deck's slide again.
    written = {"type": "architecture", "nodes": [{"label": "Intel"}, {"label": "Hub"}]}
    authored = {
        "title": "Brand film",
        "theme": {"mode": "dark"},
        "scenes": [{"id": "s05", "type": "figure", "block_spec": written, "duration": 6}],
    }
    edited, _ = projects.apply_operations(
        authored,
        [{"op": "update_scene", "scene": "s05", "fields": {"presentation_id": DECK_ID, "slide": 7, "block": 0}}],
    )
    scene = edited["scenes"][0]
    assert "block_spec" not in scene and (scene["presentation_id"], scene["slide"], scene["block"]) == (DECK_ID, 7, 0)
    spec = parse_video_spec(validate_video_spec(edited))
    deck = {"slides": [{"layout": "title", "title": "x"}] * 6 + [_deck_slide(brand.ids)]}

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: deck)

    async def no_groups(_db, _user_id):
        return []

    async def no_geometry(_db, _spec):
        return {}

    monkeypatch.setattr(helpers, "get_user_group_ids", no_groups)
    monkeypatch.setattr(presentations, "load_logo_asset_meta", no_geometry)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    [preview] = asyncio.run(figures.figure_previews(_Db(), user, spec, {})).values()
    # The still is drawn from the dark twins: its document, its pictures and its digest follow them.
    shown = preview.document["presentation"]["spec"]["slides"][0]["blocks"][0]
    assert shown["nodes"][0]["logo_file_id"] == brand.ids["opencti-logo.svg"]
    assert brand.ids["opencti-logo-light.svg"] not in preview.source.files
    as_placed = figures.preview_digest(
        figures.source_from_deck(spec.scenes[0], deck, uuid.UUID(DECK_ID)),
        scheme="dark",
        palette={},
        viewport=preview.viewport,
        safe=preview.safe,
    )
    assert preview.digest != as_placed
    # The save says which pictures it draws as their twins.
    [line] = figures.twin_lines([preview])
    assert line.startswith("Scene s05: its figure draws 4 pictures as their twins for the dark video: ")
    assert "opencti-logo-light.svg as opencti-logo.svg" in line
