"""Presentation export: render token, asset capability, PPTX writers,
export packaging and the render-token-only API routes.

The headless render itself needs Chromium and a running frontend, so it is
exercised end to end in the deploy pipeline, not here. What IS pinned here is
everything around it that can regress silently:

* the render token is a confined credential — disjoint from a session JWT in
  BOTH directions, bound to one deck, short-lived;
* the render-asset capability only ever covers files the deck references;
* the PPTX writers place what the browser measured, at the browser's size;
* the export endpoint and the render routes refuse the wrong credential kind.
"""

from __future__ import annotations

import io
import struct
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pptx import Presentation as PptxPresentation
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu

from app.core.config import settings
from app.core.security import JWT_CLOCK_SKEW_LEEWAY, create_access_token, is_confined_token
from app.services import pptx_writer
from app.services.presentation_export import (
    CANVAS_SIZES,
    EXPORT_CONTENT_TYPES,
    EXPORT_EXTENSIONS,
    EXPORT_FORMATS,
    PDF_PAGE_SIZES_IN,
    PDF_PRINT_SCALE,
    PDF_PRINT_SCALES,
    RENDER_TOKEN_SCOPE,
    RENDER_TOKEN_TTL,
    RenderTokenError,
    decode_render_token,
    export_filename,
    mint_render_token,
    referenced_file_ids,
    render_base_url,
    static_render_url,
)
from tests.jwt_clock import freeze_pyjwt_clock

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _png(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    """A minimal valid RGB PNG (no Pillow needed)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    def in_transaction(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _export_budget_admits(monkeypatch):
    """The export routes' per-user budget is counted in Redis, which unit tests have none of."""
    from app.api.v1 import presentations as routes

    async def _admit(user_id):
        return None

    monkeypatch.setattr(routes, "enforce_export_rate_limit", _admit)


def _deck_lookup(deck) -> list[_Result]:
    """The two results the visibility-aware deck load consumes: the
    caller's group memberships, then the deck row."""
    return [_Result(rows=[]), _Result(deck)]


def _deck_row(user_id: uuid.UUID, **overrides):
    columns = {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "title": "Quarterly review",
        "theme": "filigran",
        "slide_count": 1,
        "revision": 1,
        "conversation_id": None,
        "assignment_run_id": None,
        "template_container_id": None,
        "is_company_managed": False,
        "shared_group_ids": None,
        "shared_group_permissions": None,
        "shared_user_ids": None,
        "shared_user_permissions": None,
        "access_restricted": False,
        "updated_by_user_id": None,
        "spec": {
            "title": "Quarterly review",
            "theme": "filigran",
            "aspect_ratio": "16:9",
            "transition": "fade",
            "slides": [{"layout": "title", "title": "Q3"}],
        },
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    columns.update(overrides)
    return SimpleNamespace(**columns)


def _user(user_id: uuid.UUID, role: str = "user"):
    return SimpleNamespace(id=user_id, role=role, is_admin=role == "admin", is_active=True, email="u@example.com")


# ── Render token ──────────────────────────────────────────────────────────────


def test_render_token_round_trips_its_bindings():
    deck_id, user_id = uuid.uuid4(), uuid.uuid4()
    claims = decode_render_token(mint_render_token(deck_id, user_id))
    assert claims.presentation_id == deck_id
    assert claims.user_id == user_id


def test_render_token_is_confined_and_short_lived():
    """The session surface refuses it on sight (``is_confined_token``), and it
    expires in minutes — a leaked one buys a short read of one deck's images."""
    token = mint_render_token(uuid.uuid4(), uuid.uuid4())
    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    assert payload["scope"] == RENDER_TOKEN_SCOPE
    assert is_confined_token(payload) is True
    lifetime = payload["exp"] - payload["iat"]
    assert lifetime <= RENDER_TOKEN_TTL.total_seconds()
    assert timedelta(minutes=5) >= RENDER_TOKEN_TTL


def test_a_session_jwt_is_not_a_render_token():
    """Disjoint in the other direction: a real session cannot drive the render
    routes, so a stolen session token gains nothing new from them."""
    session_token = create_access_token(str(uuid.uuid4()))
    with pytest.raises(RenderTokenError):
        decode_render_token(session_token)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: {**p, "scope": "something_else"},
        lambda p: {k: v for k, v in p.items() if k != "presentation_id"},
        lambda p: {**p, "presentation_id": "not-a-uuid"},
        lambda p: {**p, "exp": datetime.now(UTC) - JWT_CLOCK_SKEW_LEEWAY - timedelta(seconds=1)},
    ],
    ids=["wrong-scope", "unbound", "malformed-binding", "expired"],
)
def test_render_token_variants_are_refused(mutate):
    payload = {
        "sub": str(uuid.uuid4()),
        "scope": RENDER_TOKEN_SCOPE,
        "presentation_id": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": datetime.now(UTC) + timedelta(minutes=1),
    }
    token = jwt.encode(mutate(payload), settings.secret_key, algorithm=settings.jwt_algorithm)
    with pytest.raises(RenderTokenError):
        decode_render_token(token)


def test_render_token_expiry_is_checked_with_the_clock_skew_leeway(monkeypatch):
    """Minted by one process, fetched back by Chromium through whichever replica."""
    expiry = 2_000_000_000
    leeway = int(JWT_CLOCK_SKEW_LEEWAY.total_seconds())
    payload = {
        "sub": str(uuid.uuid4()),
        "scope": RENDER_TOKEN_SCOPE,
        "presentation_id": str(uuid.uuid4()),
        "iat": expiry - 180,
        "exp": expiry,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    freeze_pyjwt_clock(monkeypatch, expiry + leeway - 1)
    assert decode_render_token(token)
    freeze_pyjwt_clock(monkeypatch, expiry + leeway + 1)
    with pytest.raises(RenderTokenError):
        decode_render_token(token)


def test_render_token_signed_with_another_key_is_refused():
    payload = {
        "sub": str(uuid.uuid4()),
        "scope": RENDER_TOKEN_SCOPE,
        "presentation_id": str(uuid.uuid4()),
        "exp": datetime.now(UTC) + timedelta(minutes=1),
    }
    forged = jwt.encode(payload, "not-the-platform-key", algorithm=settings.jwt_algorithm)
    with pytest.raises(RenderTokenError):
        decode_render_token(forged)


# ── What a deck may fetch ─────────────────────────────────────────────────────


def test_referenced_file_ids_walks_the_whole_spec_and_the_theme():
    bg, col, speaker, logo, logo_light = (uuid.uuid4() for _ in range(5))
    spec = {
        "slides": [
            {"layout": "content", "background_image_file_id": str(bg)},
            {
                "layout": "content",
                "columns": [[{"type": "image", "file_id": str(col)}], []],
            },
            {"layout": "closing", "speakers": [{"photo_file_id": str(speaker).upper()}]},
        ]
    }
    theme = {"logo_file_id": str(logo), "logo_light_file_id": str(logo_light), "accent": "#0ff"}
    ids = referenced_file_ids(spec, theme)
    assert ids == frozenset(str(x) for x in (bg, col, speaker, logo, logo_light))


def test_referenced_file_ids_ignores_non_file_keys_and_non_uuids():
    spec = {
        "slides": [
            {
                "layout": "content",
                "id": str(uuid.uuid4()),  # a slide id, not a file
                "background_image_file_id": "../../etc/passwd",
                "blocks": [{"block": "image", "file_id": "", "caption": str(uuid.uuid4())}],
            }
        ]
    }
    assert referenced_file_ids(spec, {"logo_file_id": "not-a-uuid"}) == frozenset()
    assert referenced_file_ids(None, None) == frozenset()


# ── Where the page lives ──────────────────────────────────────────────────────


def test_render_base_url_prefers_the_explicit_setting(monkeypatch):
    monkeypatch.setattr(settings, "presentation_render_base_url", "https://render.internal/ ")
    assert render_base_url() == "https://render.internal"


def test_render_base_url_uses_loopback_in_the_unified_image(monkeypatch, tmp_path):
    from app.services import presentation_export as mod

    monkeypatch.setattr(settings, "presentation_render_base_url", "")
    monkeypatch.setattr(mod, "_FRONTEND_DIST", tmp_path)  # exists -> unified image
    assert render_base_url() == f"http://127.0.0.1:{mod.UNIFIED_IMAGE_PORT}"


def test_render_base_url_falls_back_to_the_frontend_url(monkeypatch, tmp_path):
    from app.services import presentation_export as mod

    monkeypatch.setattr(settings, "presentation_render_base_url", "")
    monkeypatch.setattr(mod, "_FRONTEND_DIST", tmp_path / "missing")
    monkeypatch.setattr(settings, "frontend_url", "http://localhost:3000/")
    assert render_base_url() == "http://localhost:3000"


def test_static_render_url_carries_the_token_in_the_query(monkeypatch):
    monkeypatch.setattr(settings, "presentation_render_base_url", "http://127.0.0.1:4000")
    deck_id = uuid.uuid4()
    url = static_render_url(deck_id, "tok.en")
    assert url == f"http://127.0.0.1:4000/presentations/{deck_id}/static?render_token=tok.en"


# ── Packaging ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("export_format", EXPORT_FORMATS)
def test_export_filename_is_a_slug_with_the_format_extension(export_format):
    name = export_filename("Q3 Review: Résumé / plan", export_format)
    assert name.endswith("." + EXPORT_EXTENSIONS[export_format])
    stem = name.rsplit(".", 1)[0]
    assert stem and "/" not in stem and ":" not in stem and " " not in stem


def test_export_filename_never_comes_out_empty():
    assert export_filename("", "pdf") == "presentation.pdf"
    assert export_filename("///", "pptx") == "presentation.pptx"
    assert len(export_filename("x" * 500, "pdf")) <= 90


def test_every_format_has_a_content_type_and_extension():
    for export_format in EXPORT_FORMATS:
        assert export_format in EXPORT_CONTENT_TYPES
        assert export_format in EXPORT_EXTENSIONS
    assert EXPORT_CONTENT_TYPES["pdf"] == "application/pdf"
    assert EXPORT_CONTENT_TYPES["pptx"] == EXPORT_CONTENT_TYPES["pptx_native"]


def test_pdf_paper_matches_the_pptx_slide_and_the_canvas():
    """A PDF page and a PPTX slide of the same deck are the same physical size
    (within 400 EMU, a hundredth of a millimetre — the metric A4 paper has no
    exact 4-decimal inch spelling), and each format's print scale maps its
    canvas height onto its page height exactly."""
    assert set(PDF_PAGE_SIZES_IN) == set(CANVAS_SIZES) == set(pptx_writer.SLIDE_SIZES_EMU) == set(PDF_PRINT_SCALES)
    for ratio, (w_in, h_in) in PDF_PAGE_SIZES_IN.items():
        slide_w, slide_h = pptx_writer.SLIDE_SIZES_EMU[ratio]
        assert round(float(w_in.rstrip("in")) * 914_400) == pytest.approx(slide_w, abs=400)
        assert round(float(h_in.rstrip("in")) * 914_400) == pytest.approx(slide_h, abs=400)
        _cw, ch = CANVAS_SIZES[ratio]
        assert ch * PDF_PRINT_SCALES[ratio] == pytest.approx(float(h_in.rstrip("in")) * 96)
    # The slide formats keep the historical 1 canvas px = 0.5pt scale.
    assert PDF_PRINT_SCALE == PDF_PRINT_SCALES["16:9"] == PDF_PRINT_SCALES["4:3"] == pytest.approx(720 / 1080)


def test_portrait_pages_are_real_paper_in_portrait():
    """A4 / letter are documents: taller than wide on the canvas, on the page
    and on the PPTX slide, at ISO / US paper sizes."""
    for ratio in ("A4", "letter"):
        cw, ch = CANVAS_SIZES[ratio]
        assert cw == 1920 and ch > cw
        slide_w, slide_h = pptx_writer.SLIDE_SIZES_EMU[ratio]
        assert slide_h > slide_w
    assert pptx_writer.SLIDE_SIZES_EMU["A4"] == (7_560_000, 10_692_000)  # 210 x 297 mm
    assert pptx_writer.SLIDE_SIZES_EMU["letter"] == (7_772_400, 10_058_400)  # 8.5 x 11 in
    assert PDF_PAGE_SIZES_IN["letter"] == ("8.5in", "11in")


# ── PPTX writers ──────────────────────────────────────────────────────────────


def test_image_pptx_is_one_full_bleed_picture_per_slide_with_notes():
    data = pptx_writer.build_image_pptx(
        [_png(), _png(), _png()],
        aspect_ratio="16:9",
        notes=["first", None, "  third  "],
        title="Deck",
    )
    prs = PptxPresentation(io.BytesIO(data))
    assert prs.slide_width == Emu(12_192_000)
    assert prs.slide_height == Emu(6_858_000)
    assert len(prs.slides) == 3
    for slide in prs.slides:
        pictures = [s for s in slide.shapes if s.shape_type is not None and s.shape_type == 13]
        assert len(pictures) == 1
        picture = pictures[0]
        assert (picture.left, picture.top) == (0, 0)
        assert (picture.width, picture.height) == (prs.slide_width, prs.slide_height)
    notes = [s.notes_slide.notes_text_frame.text if s.has_notes_slide else "" for s in prs.slides]
    assert notes == ["first", "", "third"]
    assert prs.core_properties.title == "Deck"


@pytest.mark.parametrize(
    ("transition", "effect"),
    [("none", None), ("fade", "fade"), ("slide", "push"), ("convex", "push"), ("concave", "cover"), ("zoom", "zoom")],
)
def test_both_powerpoint_variants_play_the_deck_transition_with_the_notes(transition, effect):
    """The deck's transition plays in PowerPoint too, pixel-perfect and
    editable alike - the closest base PresentationML transition, at reveal's
    pace, on every slide, placed where the schema wants it (after
    ``clrMapOvr``) - and the speaker notes ride along, markup stripped."""
    from pptx.oxml.ns import qn

    from app.services.presentation_spec import PRESENTATION_TRANSITIONS

    assert set(pptx_writer._PPTX_TRANSITIONS) == set(PRESENTATION_TRANSITIONS)
    notes = ["Land the **42%**.", None]
    measured = [{"index": n, "w": 1920, "h": 1080, "background": {"r": 10, "g": 10, "b": 10, "a": 1}, "items": []} for n in (0, 1)]
    variants = {
        "image": pptx_writer.build_image_pptx([_png(), _png()], aspect_ratio="16:9", notes=notes, transition=transition),
        "native": pptx_writer.build_native_pptx(measured, captures={}, aspect_ratio="16:9", notes=notes, transition=transition),
    }
    for variant, data in variants.items():
        prs = PptxPresentation(io.BytesIO(data))
        for slide in prs.slides:
            node = slide._element.find(qn("p:transition"))
            if effect is None:
                assert node is None, variant
                continue
            assert node is not None, variant
            assert node.get("spd") == "med"
            assert [child.tag for child in node] == [qn(f"p:{effect}")]
            assert node.getprevious().tag == qn("p:clrMapOvr"), "the schema order: cSld, clrMapOvr, transition"
        assert prs.slides[0].notes_slide.notes_text_frame.text == "Land the 42%.", variant
        assert not prs.slides[1].has_notes_slide, variant
    # The export hands the deck's own transition to both variants.
    from pathlib import Path

    from app.services import presentation_export

    source = Path(presentation_export.__file__).read_text(encoding="utf-8")
    assert 'transition = str(spec.get("transition") or "slide")' in source
    assert source.count("transition=transition") == 2


def test_image_pptx_uses_the_4_3_slide_size():
    data = pptx_writer.build_image_pptx([_png()], aspect_ratio="4:3")
    prs = PptxPresentation(io.BytesIO(data))
    assert (prs.slide_width, prs.slide_height) == (Emu(9_144_000), Emu(6_858_000))


def test_image_pptx_refuses_an_empty_deck():
    with pytest.raises(ValueError):
        pptx_writer.build_image_pptx([], aspect_ratio="16:9")


def _measured_slide(capture_id: str) -> dict:
    return {
        "index": 0,
        "w": 1920,
        "h": 1080,
        "background": {"r": 10, "g": 20, "b": 30, "a": 1},
        "items": [
            # A rect painting the whole slide is the background, not a shape.
            {
                "kind": "rect",
                "x": 0,
                "y": 0,
                "w": 1920,
                "h": 1080,
                "opacity": 1,
                "fill": {"r": 0, "g": 0, "b": 0, "a": 0.5},
                "line": None,
                "edges": None,
                "radius": 0,
            },
            # A card: rounded, semi-transparent fill, 2px border.
            {
                "kind": "rect",
                "x": 96,
                "y": 200,
                "w": 800,
                "h": 400,
                "opacity": 1,
                "fill": {"r": 255, "g": 255, "b": 255, "a": 0.1},
                "line": {"r": 0, "g": 200, "b": 255, "a": 1, "width": 2},
                "edges": None,
                "radius": 24,
            },
            # Text: two runs and a hard line break, centred, 48px heading.
            {
                "kind": "text",
                "x": 96,
                "y": 80,
                "w": 1728,
                "h": 120,
                "opacity": 1,
                "align": "center",
                "lineHeight": 57.6,
                "valign": "top",
                "letterSpacing": 0,
                "padding": {"top": 0, "right": 0, "bottom": 0, "left": 0},
                "runs": [
                    {
                        "text": "Quarterly ",
                        "font": "Geologica, system-ui, sans-serif",
                        "size": 48,
                        "weight": 700,
                        "italic": False,
                        "color": "rgb(255, 255, 255)",
                        "underline": False,
                        "mono": False,
                    },
                    {
                        "text": "review\nQ3",
                        "font": '"IBM Plex Sans", sans-serif',
                        "size": 48,
                        "weight": 400,
                        "italic": True,
                        "color": "rgba(0, 200, 255, 0.5)",
                        "underline": False,
                        "mono": False,
                    },
                ],
            },
            # An icon captured from the page.
            {"kind": "image", "id": capture_id, "x": 1700, "y": 900, "w": 96, "h": 96, "opacity": 1},
            # An image whose capture failed: skipped, never a broken picture.
            {"kind": "image", "id": "missing", "x": 0, "y": 0, "w": 10, "h": 10, "opacity": 1},
        ],
    }


def test_native_pptx_places_measured_primitives_as_editable_shapes():
    capture_id = "x0-3"
    data = pptx_writer.build_native_pptx(
        [_measured_slide(capture_id)],
        captures={capture_id: _png()},
        aspect_ratio="16:9",
        notes=["speak"],
        title="Deck",
    )
    prs = PptxPresentation(io.BytesIO(data))
    assert len(prs.slides) == 1
    slide = prs.slides[0]

    # The slide-covering rect was folded into the background: 50% black over
    # (10, 20, 30) -> (5, 10, 15), and it produced no shape of its own.
    assert str(slide.background.fill.fore_color.rgb) == "050A0F"
    shapes = list(slide.shapes)
    assert len(shapes) == 3  # card, text box, icon picture

    card, textbox, picture = shapes
    emu_per_px = 12_192_000 / 1920
    assert card.left == int(96 * emu_per_px) and card.top == int(200 * emu_per_px)
    assert card.width == int(800 * emu_per_px)
    assert card.line.color.rgb == pptx_writer._rgb((0, 200, 255))
    # 10% white over the (5, 10, 15) background -> (30, 34, 39).
    assert str(card.fill.fore_color.rgb) == "1E2227"

    frame = textbox.text_frame
    assert [p.text for p in frame.paragraphs] == ["Quarterly review", "Q3"]
    runs = frame.paragraphs[0].runs
    assert runs[0].font.name == "Geologica"
    assert runs[0].font.bold is True
    assert runs[0].font.size.pt == 24  # 48px * 0.5pt/px
    assert runs[1].font.name == "IBM Plex Sans"
    assert runs[1].font.italic is True
    assert runs[1].font.bold is False
    # 50% cyan (0, 200, 255) over the (5, 10, 15) background -> (2, 105, 135).
    assert str(runs[1].font.color.rgb) == "026987"

    assert picture.shape_type == 13
    assert picture.left == int(1700 * emu_per_px)
    assert picture.width == int(96 * emu_per_px)
    assert slide.notes_slide.notes_text_frame.text == "speak"


def test_native_pptx_refuses_an_empty_deck():
    with pytest.raises(ValueError):
        pptx_writer.build_native_pptx([], captures={}, aspect_ratio="16:9")


def test_native_pptx_draws_one_sided_borders_as_bars():
    slide = {
        "index": 0,
        "w": 1920,
        "h": 1080,
        "background": {"r": 0, "g": 0, "b": 0, "a": 1},
        "items": [
            {
                "kind": "rect",
                "x": 100,
                "y": 100,
                "w": 600,
                "h": 200,
                "opacity": 1,
                "fill": None,
                "line": None,
                "radius": 0,
                "edges": {"top": 0, "right": 0, "bottom": 0, "left": 4, "color": {"r": 0, "g": 200, "b": 255, "a": 1}},
            },
        ],
    }
    prs = PptxPresentation(io.BytesIO(pptx_writer.build_native_pptx([slide], captures={}, aspect_ratio="16:9")))
    shapes = list(prs.slides[0].shapes)
    assert len(shapes) == 1  # only the painted (left) edge
    bar = shapes[0]
    emu_per_px = 12_192_000 / 1920
    assert bar.width == max(1, int(4 * emu_per_px))
    assert bar.height == int(200 * emu_per_px)


def test_native_pptx_keeps_each_edge_its_own_colour():
    """A persona / tier card is a thin grey frame with a thick toned top rule.
    The extractor reports the colour per side; the writer must paint each bar
    in ITS colour, not the whole frame in the first drawn side's tone."""
    grey = {"r": 200, "g": 200, "b": 200, "a": 1}
    tone = {"r": 0, "g": 21, "b": 168, "a": 1}
    slide = _slide_with(
        [
            {
                "kind": "rect",
                "x": 100,
                "y": 100,
                "w": 600,
                "h": 300,
                "opacity": 1,
                "fill": None,
                "line": None,
                "radius": 12,
                "edges": {
                    "top": 6,
                    "right": 1,
                    "bottom": 1,
                    "left": 1,
                    "color": tone,
                    "colors": {"top": tone, "right": grey, "bottom": grey, "left": grey},
                },
            }
        ]
    )
    prs = PptxPresentation(io.BytesIO(pptx_writer.build_native_pptx([slide], captures={}, aspect_ratio="16:9")))
    bars = list(prs.slides[0].shapes)
    assert len(bars) == 4
    emu_per_px = 12_192_000 / 1920
    thick = [bar for bar in bars if bar.height == max(1, int(6 * emu_per_px))]
    assert len(thick) == 1 and str(thick[0].fill.fore_color.rgb) == "0015A8"
    thin = [bar for bar in bars if bar is not thick[0]]
    assert {str(bar.fill.fore_color.rgb) for bar in thin} == {"C8C8C8"}


def test_native_pptx_edge_colour_falls_back_to_the_single_colour():
    """A payload without per-side colours (the older shape) still paints."""
    tone = {"r": 0, "g": 200, "b": 255, "a": 1}
    slide = _slide_with(
        [
            {
                "kind": "rect",
                "x": 0,
                "y": 0,
                "w": 400,
                "h": 100,
                "opacity": 1,
                "fill": None,
                "line": None,
                "radius": 0,
                "edges": {"top": 2, "right": 0, "bottom": 2, "left": 0, "color": tone},
            }
        ]
    )
    prs = PptxPresentation(io.BytesIO(pptx_writer.build_native_pptx([slide], captures={}, aspect_ratio="16:9")))
    bars = list(prs.slides[0].shapes)
    assert len(bars) == 2
    assert {str(bar.fill.fore_color.rgb) for bar in bars} == {"00C8FF"}


def _text_item(runs: list[dict], **overrides) -> dict:
    item = {
        "kind": "text",
        "x": 100,
        "y": 100,
        "w": 600,
        "h": 60,
        "opacity": 1,
        "runs": runs,
        "align": "left",
        "lineHeight": 60,
        "padding": {"top": 0, "right": 0, "bottom": 0, "left": 0},
        "valign": "top",
        "letterSpacing": 0,
    }
    item.update(overrides)
    return item


def _slide_with(items: list[dict]) -> dict:
    return {"index": 0, "w": 1920, "h": 1080, "background": {"r": 255, "g": 255, "b": 255, "a": 1}, "items": items}


def test_native_pptx_outlines_stroked_text_instead_of_screenshotting_it():
    """``-webkit-text-stroke`` with a transparent fill (the ghost section number)
    stays editable text: a run outline (``a:ln``) and NO fill — not a picture,
    which would also bake in whatever overlaps the element (the slide title)."""
    from pptx.oxml.ns import qn

    run = {
        "text": "02",
        "font": "Geologica",
        "size": 560,
        "weight": 600,
        "italic": False,
        "color": None,
        "stroke": {"r": 0, "g": 21, "b": 168, "a": 0.22, "width": 3},
    }
    data = pptx_writer.build_native_pptx(
        [_slide_with([_text_item([run], w=640, h=560, lineHeight=560)])], captures={}, aspect_ratio="16:9"
    )
    shape = next(iter(PptxPresentation(io.BytesIO(data)).slides[0].shapes))
    rpr = shape.text_frame.paragraphs[0].runs[0].font._element
    children = [child.tag for child in rpr]
    assert children[0] == qn("a:ln"), "the outline must be the first run property (schema order)"
    assert qn("a:noFill") in children, "a hollow glyph has no fill, not the inherited text colour"
    line_colour = rpr.find(qn("a:ln")).find(qn("a:solidFill")).find(qn("a:srgbClr")).get("val")
    # 22% of the accent blended onto the white background.
    assert line_colour == str(pptx_writer._rgb(pptx_writer.blend(run["stroke"], (255, 255, 255))))
    # 3 canvas px at 0.5pt/px = 1.5pt = 19050 EMU.
    assert rpr.find(qn("a:ln")).get("w") == "19050"


def test_native_pptx_keeps_a_single_line_box_on_one_line():
    """Chromium laid the box out as one line; PowerPoint's wider glyph metrics
    must not break it into two — wrapping is off for single-line boxes and on
    for multi-line ones."""
    run = {
        "text": "What shipped this quarter",
        "font": "Geologica",
        "size": 112,
        "weight": 600,
        "italic": False,
        "color": {"r": 0, "g": 0, "b": 0, "a": 1},
    }
    data = pptx_writer.build_native_pptx(
        [_slide_with([_text_item([run], h=118, lineHeight=118), _text_item([run], y=400, h=300, lineHeight=118)])],
        captures={},
        aspect_ratio="16:9",
    )
    single, multi = list(PptxPresentation(io.BytesIO(data)).slides[0].shapes)
    assert single.text_frame.word_wrap is False
    assert multi.text_frame.word_wrap is True


def test_native_pptx_widens_wrapped_boxes_without_moving_their_anchor():
    """A wrapped box gets a few percent of slack for PowerPoint's wider glyph
    advances (or it breaks one more line than Chromium and loses the last one),
    and the slack is added on the side the text is NOT anchored to: a
    left-aligned box grows right, a right-aligned one grows left, a centred one
    grows both ways - so nothing visibly drifts."""
    run = {"text": "Community forum; 24x7 with Enterprise", "font": "IBM Plex Sans", "size": 32, "weight": 400, "italic": False, "color": {"r": 0, "g": 0, "b": 0, "a": 1}}
    x, w, lh = 500, 400, 40
    items = [_text_item([run], x=x, w=w, h=90, lineHeight=lh, align=align) for align in ("left", "right", "center")]
    items.append(_text_item([run], x=x, w=w, h=44, lineHeight=lh, align="right"))  # single line
    data = pptx_writer.build_native_pptx([_slide_with(items)], captures={}, aspect_ratio="16:9")
    emu = pptx_writer.SLIDE_SIZES_EMU["16:9"][0] / 1920
    left, right, center, single = list(PptxPresentation(io.BytesIO(data)).slides[0].shapes)
    slack = w * pptx_writer._WRAP_SLACK_RATIO
    assert left.left == pytest.approx(x * emu, abs=emu) and left.width == pytest.approx((w + slack) * emu, abs=emu)
    assert right.left == pytest.approx((x - slack) * emu, abs=emu) and right.left + right.width == pytest.approx((x + w) * emu, abs=2 * emu)
    assert center.left + center.width / 2 == pytest.approx((x + w / 2) * emu, abs=2 * emu)
    # A single line only needs a hair, anchored the same way.
    assert single.width == pytest.approx((w + 6) * emu, abs=emu)
    assert single.left + single.width == pytest.approx((x + w) * emu, abs=2 * emu)


def test_native_pptx_keeps_a_fill_blocks_rows_where_the_render_spread_them():
    """A grid block with ``fill`` spreads its rows over the height the
    slide leaves it, and the exported slide must show the same spread. The
    export has no layout of its own to keep in step: the extractor emits every
    row at the box Chromium measured for it and the writer places it there -
    so the rows land spread when the CSS spread them, and stacked from the
    top, at the very same coordinates as before, when it did not."""
    import re

    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    run = {"text": "Detect", "font": "IBM Plex Sans", "size": 32, "weight": 500, "italic": False, "color": {"r": 0, "g": 0, "b": 0, "a": 1}}
    emu = pptx_writer.SLIDE_SIZES_EMU["16:9"][0] / 1920
    # Six rows of 60px in a block that runs from y=240 to y=1020 (780px).
    stacked_tops = [240 + index * 76 for index in range(6)]  # 60px rows, the 16px gap
    spread_tops = [240 + round(index * (780 - 60) / 5) for index in range(6)]  # `space-between`
    for tops in (stacked_tops, spread_tops):
        data = pptx_writer.build_native_pptx(
            [_slide_with([_text_item([run], y=top, h=60) for top in tops])], captures={}, aspect_ratio="16:9"
        )
        shapes = list(PptxPresentation(io.BytesIO(data)).slides[0].shapes)
        assert [shape.top for shape in shapes] == pytest.approx([top * emu for top in tops], abs=emu)
    # The spread rows end where the block ends; the stacked ones stop short.
    assert spread_tops[-1] + 60 == 1020 and stacked_tops[-1] + 60 < 1020

    # The contract the test above relies on, pinned in the extractor: a row's
    # position is its own measured box relative to the slide, and `fill` (the
    # layout switch) is not a name the extractor knows - only the paint colour is.
    assert "const box = (rect) => ({ x: rect.left - base.left, y: rect.top - base.top" in EXTRACT_PRIMITIVES_JS
    assert not re.search(r"data-fill|dataset\.fill|distribute", EXTRACT_PRIMITIVES_JS)


@pytest.mark.parametrize(
    ("color", "background", "opacity", "expected"),
    [
        ({"r": 255, "g": 255, "b": 255, "a": 1}, (0, 0, 0), 1.0, (255, 255, 255)),
        ({"r": 255, "g": 255, "b": 255, "a": 0.5}, (0, 0, 0), 1.0, (128, 128, 128)),
        ({"r": 255, "g": 255, "b": 255, "a": 1}, (0, 0, 0), 0.5, (128, 128, 128)),
        ({"r": 255, "g": 0, "b": 0, "a": 0}, (9, 9, 9), 1.0, None),
        (None, (9, 9, 9), 1.0, None),
    ],
)
def test_blend_flattens_alpha_onto_the_background(color, background, opacity, expected):
    assert pptx_writer.blend(color, background, opacity) == expected


@pytest.mark.parametrize(
    ("css", "expected"),
    [
        ("rgb(1, 2, 3)", {"r": 1, "g": 2, "b": 3, "a": 1.0}),
        ("rgba(1, 2, 3, 0.25)", {"r": 1, "g": 2, "b": 3, "a": 0.25}),
        ("rgb(1 2 3 / 50%)", {"r": 1, "g": 2, "b": 3, "a": 0.5}),
        ("rgba(0, 0, 0, 0)", {"r": 0, "g": 0, "b": 0, "a": 0.0}),
        # What Chromium computes a ``color-mix()`` tint to: 0..1 channels.
        ("color(srgb 0 0.0823529 0.658824 / 0.09)", {"r": 0, "g": 21.0, "b": 168.0, "a": 0.09}),
        ("color(srgb 1 1 1)", {"r": 255.0, "g": 255.0, "b": 255.0, "a": 1.0}),
        ("color(srgb 50% 0% 100% / 50%)", {"r": 127.5, "g": 0.0, "b": 255.0, "a": 0.5}),
        ("transparent", None),
        ("", None),
        (None, None),
    ],
)
def test_css_colours_parse_as_the_browser_reports_them(css, expected):
    parsed = pptx_writer._parse_css_color(css)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert {k: pytest.approx(v, abs=0.01) for k, v in parsed.items()} == expected


def test_the_extractor_captures_inline_svg_tints_and_icons_in_the_text_flow():
    """The measured-DOM contract the collateral blocks depend on.

    Three DOM facts, each of which silently emptied a native export before:
    an inline ``<svg>`` has a lowercase ``tagName`` (it lives in the SVG
    namespace) so it must be recognised by namespace, not by an uppercase
    tag; a ``color-mix()`` tint computes to ``color(srgb ...)``, not to
    ``rgb()``; and an icon sharing a line with bare text
    (``<li><svg/>Unlimited assets</li>``) must yield BOTH a picture and a
    text frame, framed on the text nodes so the two do not overlap.
    """
    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    js = EXTRACT_PRIMITIVES_JS
    # SVG recognised by namespace — never by an uppercase tag comparison.
    assert "namespaceURI === SVG_NS" in js
    assert 'tagName === "SVG"' not in js
    # Tints: the ``color(srgb ...)`` form plus a canvas fallback for the rest.
    assert "color\\(srgb" in js
    assert 'getContext("2d"' in js
    # Atoms in the text flow: captured as pictures, text framed on the text
    # nodes only, and the run colour handed over already parsed.
    assert "collectAtoms" in js
    assert "rectUnion(textNodesOf(el))" in js
    assert "color: textColor(cs)" in js
    # Bare text next to block children is framed rather than dropped.
    assert "node.nodeType === 3 && node.textContent.trim()" in js
    # A border colour is read per side, and only off a side that is drawn
    # (``border: 0`` resets the undrawn sides to currentColor, which is the
    # TEXT colour): a card is a grey frame with a toned top rule, and the
    # writer needs each side's own colour to paint it that way.
    assert "color: width > 0 ? parseColor(cs[`border${cap}Color`]) : null" in js
    assert "colors: { top: sides.top.color" in js
    # Uniform means ONE outline: every side drawn, same width AND same colour.
    assert "sameColor(side.color, line)" in js
    # CSS text-transform is applied to the run text, both for text-like boxes
    # and for bare text nodes next to block children.
    assert 'if (transform === "uppercase") return text.toUpperCase();' in js
    assert js.count("transformText(node.textContent.replace(") == 2


@pytest.mark.parametrize(
    ("family", "mono", "expected"),
    [
        ("Geologica, system-ui, sans-serif", False, "Geologica"),
        ('"IBM Plex Mono", ui-monospace, monospace', True, "IBM Plex Mono"),
        ("system-ui, sans-serif", False, "Calibri"),
        ("monospace", True, "Consolas"),
        ("", True, "Consolas"),
        (None, False, "Calibri"),
    ],
)
def test_fonts_keep_the_deck_family_and_map_generics(family, mono, expected):
    assert pptx_writer.pick_font(family, mono=mono) == expected


# ── API routes ────────────────────────────────────────────────────────────────


# ── Charts ────────────────────────────────────────────────────────────


def _rgba(r: int, g: int, b: int, a: float = 1.0) -> dict:
    return {"r": r, "g": g, "b": b, "a": a}


_CHART_SERIES = [
    {"name": "Bookings", "values": [11_200_000, 22_000_000, 26_500_000, 16_500_000], "color": "rgb(0, 21, 168)"},
    {"name": "Pipeline", "values": [30_000_000, 28_000_000, 31_000_000, 35_000_000], "color": "rgb(64, 196, 255)"},
]
_CHART_COLORS = [_rgba(0, 21, 168), _rgba(64, 196, 255)]
_SLICE_COLORS = [_rgba(0, 21, 168), _rgba(64, 196, 255), _rgba(255, 171, 64), _rgba(102, 187, 106)]
_CHART_STYLE = {
    "font": '"IBM Plex Sans", sans-serif',
    "size": 22,
    "fit": 1,
    "tick": _rgba(255, 255, 255, 0.72),
    "label": _rgba(255, 255, 255),
    "grid": _rgba(255, 255, 255, 0.12),
    "axis": _rgba(255, 255, 255, 0.28),
    "target": _rgba(255, 171, 64),
    "legend": _rgba(255, 255, 255, 0.8),
    "background": _rgba(8, 17, 33),
}


def _chart_item(kind: str, *, chart: dict | None = None, **overrides) -> dict:
    """A ``chart`` primitive as ``EXTRACT_PRIMITIVES_JS`` emits one: the figure
    as data (the ``data-deck-chart`` payload) plus the boxes Chromium gave its
    legend, canvas and inner plot, the colours resolved in the page's theme
    and the axis the drawing settled on (``.deck-chart-facts``)."""
    single = kind in ("pie", "donut")
    series = _CHART_SERIES[:1] if single else _CHART_SERIES
    payload = {
        "kind": kind,
        "categories": ["Q1", "Q2", "Q3", "Q4"],
        "series": series,
        "slice_colors": ["rgb(0, 21, 168)", "rgb(64, 196, 255)", "rgb(255, 171, 64)", "rgb(102, 187, 106)"]
        if single
        else [],
        "format": "currency",
        "unit": "EUR",
        "stacked": kind == "stacked_bar",
        "horizontal": False,
        "legend": True,
        "values": True,
        "target": None,
        "target_name": "",
        "locale": "en",
    }
    payload.update(chart or {})
    item = {
        "kind": "chart",
        "x": 120,
        "y": 240,
        "w": 1680,
        "h": 720,
        "opacity": 1,
        "chart": payload,
        "plot": {"x": 120, "y": 300, "w": 1680, "h": 660},
        "legend": {"x": 700, "y": 240, "w": 520, "h": 40},
        "inner": {"x": 760, "y": 320, "w": 560, "h": 560} if single else {"x": 220, "y": 320, "w": 1560, "h": 560},
        "target_line": None,
        "labels": [],
        "colors": _CHART_COLORS[: len(series)],
        "slice_colors": _SLICE_COLORS if single else [],
        "axis": None if single else {"min": 0, "max": 40_000_000, "step": 10_000_000},
        "values": True,
        "style": dict(_CHART_STYLE),
    }
    item.update(overrides)
    return item


def _chart_slide(items: list[dict]) -> dict:
    return {"index": 0, "w": 1920, "h": 1080, "background": {"r": 8, "g": 17, "b": 33, "a": 1}, "items": items}


def _build(items: list[dict], *, captures: dict | None = None, log: list[str] | None = None):
    data = pptx_writer.build_native_pptx(
        [_chart_slide(items)], captures=captures or {}, aspect_ratio="16:9", export_log=log
    )
    return PptxPresentation(io.BytesIO(data)).slides[0]


def _the_chart(slide):
    frames = [shape for shape in slide.shapes if getattr(shape, "has_chart", False)]
    assert len(frames) == 1, [shape.shape_type for shape in slide.shapes]
    return frames[0]


@pytest.mark.parametrize(
    ("kind", "chart_type"),
    [
        ("bar", "COLUMN_CLUSTERED"),
        ("stacked_bar", "COLUMN_STACKED"),
        ("line", "LINE_MARKERS"),
        ("area", "AREA"),
        ("pie", "PIE"),
        ("donut", "DOUGHNUT"),
    ],
)
def test_native_pptx_writes_every_native_chart_kind_as_an_editable_chart(kind, chart_type):
    """The acceptance test of native chart export: the PPTX opens with a
    native chart of the right type carrying the DECK'S series and categories,
    not a picture of them - and the log has nothing to say about it."""
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    item = _chart_item(kind)
    log: list[str] = []
    slide = _build([item], log=log)
    chart = _the_chart(slide).chart
    assert chart.chart_type == getattr(XL_CHART_TYPE, chart_type)
    plot = chart.plots[0]
    assert [str(c) for c in plot.categories] == ["Q1", "Q2", "Q3", "Q4"]
    expected = item["chart"]["series"]
    assert [s.name for s in plot.series] == [s["name"] for s in expected]
    assert [list(s.values) for s in plot.series] == [[float(v) for v in s["values"]] for s in expected]
    assert log == []
    assert not any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for shape in slide.shapes)
    assert not chart.has_title


def test_a_pie_or_donut_plots_one_series_in_the_viewers_slice_colours():
    """A single-series kind: the extra series the payload may carry are not
    plotted, every slice takes the viewer's palette colour, the legend lists
    the categories (``varyColors``), and a donut keeps the viewer's hole."""
    from pptx.oxml.ns import qn

    extra = dict(_CHART_SERIES[1])
    for kind in ("pie", "donut"):
        item = _chart_item(kind, chart={"series": [_CHART_SERIES[0], extra]})
        chart = _the_chart(_build([item])).chart
        plot = chart.plots[0]
        assert len(plot.series) == 1
        assert plot.vary_by_categories is True
        points = plot.series[0].points
        assert [str(points[i].format.fill.fore_color.rgb) for i in range(4)] == ["0015A8", "40C4FF", "FFAB40", "66BB6A"]
        hole = plot._element.find(qn("c:holeSize"))
        # python-pptx's pie templates ship a `c:dLbls` showing nothing; what
        # matters is whether the VALUE is shown, and where.
        labels = plot._element.find(qn("c:dLbls"))
        if kind == "donut":
            assert hole is not None and hole.get("val") == str(pptx_writer._DONUT_HOLE_PERCENT)
            # The ring is labelled by the text the viewer wrote outside it, never
            # by a PowerPoint data label (which could only sit on the ring).
            assert labels is None or labels.find(qn("c:showVal")).get("val") in ("0", "false")
            assert labels is None or labels.find(qn("c:dLblPos")) is None
        else:
            assert hole is None
            assert plot.has_data_labels is True
            assert plot.data_labels.show_value is True
            assert labels.find(qn("c:dLblPos")) is not None


def _slice_label(category: int, text: str, *, role: str = "value") -> dict:
    """A pie's or a donut's value as the extractor measures it: ``value``
    outside its slice, ``segment`` inside it."""
    return {
        "text": text,
        "x": 700 + 150 * category,
        "y": 400,
        "w": 80,
        "h": 28,
        "role": role,
        "series": 0,
        "category": category,
        "rotate": False,
        "placement": None,
        "anchor": "middle" if role == "segment" else "start",
        "lift": None,
    }


_QUARTER_TEXTS = ["\u20ac11.2M", "\u20ac22M", "\u20ac26.5M", "\u20ac16.5M"]


def _point_labels(series) -> dict[int, object]:
    """The series' own ``c:dLbl`` children, by point."""
    from pptx.oxml.ns import qn

    d_lbls = series._element.find(qn("c:dLbls"))
    found = d_lbls.findall(qn("c:dLbl")) if d_lbls is not None else []
    return {int(d_lbl.find(qn("c:idx")).get("val")): d_lbl for d_lbl in found}


def test_a_pies_values_outside_are_powerpoints_own_with_leader_lines_at_the_viewers_size():
    """The viewer writes a pie's values outside its slices at the end of
    a leader, set at the size its layout settled on (``data-label-size``); the
    PowerPoint labels sit outside the slices, leader lines on, at that size.
    Every value written: the plot's labels stand, the series adds none."""
    from pptx.enum.chart import XL_LABEL_POSITION
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    labels = [_slice_label(index, text) for index, text in enumerate(_QUARTER_TEXTS)]
    item = _chart_item("pie", labels=labels, values_placement="outside", label_size=16)
    plot = _the_chart(_build([item])).chart.plots[0]
    _slide_w, _slide_h, _emu, pt_per_px = pptx_writer._slide_geometry("16:9")
    assert plot.data_labels.position == XL_LABEL_POSITION.OUTSIDE_END
    assert plot._element.find(qn("c:dLbls")).find(qn("c:showLeaderLines")).get("val") == "1"
    assert plot.data_labels.font.size == Pt(round(16 * pt_per_px, 1))
    assert plot.series[0]._element.find(qn("c:dLbls")) is None


def test_a_pie_value_the_viewer_left_out_has_no_powerpoint_label():
    from pptx.enum.chart import XL_LABEL_POSITION
    from pptx.oxml.ns import qn

    labels = [_slice_label(index, text) for index, text in enumerate(_QUARTER_TEXTS[:3])]
    item = _chart_item("pie", labels=labels, values_placement="outside")
    series = _the_chart(_build([item])).chart.plots[0].series[0]
    assert series.data_labels.position == XL_LABEL_POSITION.OUTSIDE_END
    assert series._element.find(qn("c:dLbls")).find(qn("c:showLeaderLines")).get("val") == "1"
    points = _point_labels(series)
    assert list(points) == [3]
    assert points[3].find(qn("c:delete")).get("val") == "1"


def test_a_pies_values_inside_are_centred_on_their_slices_in_the_viewers_ink():
    """With no room outside, the viewer writes a value inside each slice
    large enough to hold it, inked from the slice's colour; PowerPoint's labels
    are centred on those slices in the same ink, and the others carry none."""
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_LABEL_POSITION
    from pptx.oxml.ns import qn

    labels = [_slice_label(0, _QUARTER_TEXTS[0], role="segment"), _slice_label(2, _QUARTER_TEXTS[2], role="segment")]
    item = _chart_item("pie", labels=labels, values_placement="inside", label_size=12)
    plot = _the_chart(_build([item])).chart.plots[0]
    series = plot.series[0]
    assert plot.data_labels.position == XL_LABEL_POSITION.CENTER
    assert series.data_labels.position == XL_LABEL_POSITION.CENTER
    points = _point_labels(series)
    assert sorted(points) == [0, 1, 2, 3]
    assert [index for index, d_lbl in points.items() if d_lbl.find(qn("c:delete")) is not None] == [1, 3]
    for index, fill in ((0, (0, 21, 168)), (2, (255, 171, 64))):
        assert points[index].find(qn("c:dLblPos")).get("val") == "ctr"
        assert points[index].find(qn("c:showVal")).get("val") == "1"
        assert series.points[index].data_label.font.color.rgb == RGBColor(*pptx_writer._segment_ink(fill))
    # The dark slice is inked light and the light one dark, as on screen.
    assert pptx_writer._segment_ink((0, 21, 168)) != pptx_writer._segment_ink((255, 171, 64))


def test_a_donuts_values_keep_their_leaders_outside_and_their_ink_inside():
    """A doughnut's labels can only sit on its ring, so the viewer's
    values are its text, where it wrote them: outside with each leader drawn
    from the rim at its slice's angle, inside inked from the slice."""
    from pptx.dml.color import RGBColor
    from pptx.shapes.connector import Connector

    leaders = [{"x1": 900, "y1": 600, "x2": 914, "y2": 614, "w": 2}, {"x1": 700, "y1": 500, "x2": 686, "y2": 486, "w": 2}]
    labels = [_slice_label(index, text) for index, text in enumerate(_QUARTER_TEXTS[:2])]
    outside = _build([_chart_item("donut", labels=labels, slice_leaders=leaders, values_placement="outside")])
    connectors = [shape for shape in outside.shapes if isinstance(shape, Connector)]
    assert len(connectors) == 2
    _slide_w, _slide_h, emu_per_px, _pt = pptx_writer._slide_geometry("16:9")
    ends = sorted((c.begin_x, c.begin_y, c.end_x, c.end_y) for c in connectors)
    assert ends == sorted(
        tuple(Emu(int(leader[key] * emu_per_px)) for key in ("x1", "y1", "x2", "y2")) for leader in leaders
    )
    assert [shape.text_frame.text for shape in outside.shapes if shape.has_text_frame] == _QUARTER_TEXTS[:2]

    inside = _build([_chart_item("donut", labels=[_slice_label(1, "\u20ac22M", role="segment")], values_placement="inside")])
    (text,) = [shape for shape in inside.shapes if shape.has_text_frame]
    assert text.text_frame.text == "\u20ac22M"
    run = text.text_frame.paragraphs[0].runs[0]
    assert run.font.color.rgb == RGBColor(*pptx_writer._segment_ink((64, 196, 255)))
    assert not any(isinstance(shape, Connector) for shape in inside.shapes)


def test_the_extractor_reads_where_a_pies_values_went_and_its_leaders():
    """The page publishes where a pie's values are written and draws
    each leader as a line the extractor reads end to end; a pie's chart text
    is its own size, never a value's (each carries its own) nor an inked one."""
    from pathlib import Path

    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    for needle in (
        "values_placement: facts && facts.dataset.valuesPlacement ? facts.dataset.valuesPlacement : null,",
        'slice_leaders: Array.from(canvas.querySelectorAll(".deck-chart-slice-leader")).map(endsOf),',
        "const tick = axisTick || canvas.querySelector('text:not([data-role=\"segment\"])');",
        'const chartTextPx = axisTick ? 0 : px(ccs.getPropertyValue("--deck-chart-text"));',
        "size: (chartTextPx || px(tcs.fontSize)) * k",
        'leader: paintOf(canvas, ".deck-chart-slice-leader", "stroke"),',
    ):
        assert needle in EXTRACT_PRIMITIVES_JS, needle
    blocks = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation" / "blocks"
    assert 'className="deck-chart-slice-leader"' in (blocks / "ChartCanvas.tsx").read_text(encoding="utf-8")


def test_an_axis_chart_carries_the_viewers_axis_legend_and_number_formats():
    """The value axis is the one the viewer settled on (its min / max / step,
    dashed gridlines, no tick marks), the legend is PowerPoint's own pinned by
    manual layout on the box the viewer's legend took, and the labels and
    ticks read as the viewer wrote them (``€16.5M``, ``€30M``)."""
    from pptx.enum.chart import XL_LABEL_POSITION, XL_LEGEND_POSITION
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    from pptx.oxml.ns import qn

    item = _chart_item("bar")
    chart = _the_chart(_build([item])).chart
    plot = chart.plots[0]
    # Said explicitly: with `c:varyColors` absent PowerPoint colours a lone
    # series by category and lists the categories in the legend.
    assert plot.vary_by_categories is False
    assert plot._element.find(qn("c:varyColors")).get("val") in ("0", "false")
    assert plot.gap_width == pptx_writer._BAR_GAP_WIDTH
    assert plot.overlap == pptx_writer._BAR_OVERLAP_CLUSTERED
    assert [str(s.format.fill.fore_color.rgb) for s in plot.series] == ["0015A8", "40C4FF"]

    axis = chart.value_axis
    assert (axis.minimum_scale, axis.maximum_scale, axis.major_unit) == (0.0, 40_000_000.0, 10_000_000.0)
    assert axis.has_major_gridlines is True
    assert axis.major_gridlines.format.line.dash_style == MSO_LINE_DASH_STYLE.DASH
    # Whole-million ticks: no decimal (``€30M``, not ``€30.0M``).
    assert axis.tick_labels.number_format == '[>=1000000]"\u20ac"#,##0,,"M";[>=1000]"\u20ac"#,##0,"k";"\u20ac"#,##0'
    assert axis.tick_labels.number_format_is_linked is False

    assert chart.has_legend is True
    assert chart.legend.position == XL_LEGEND_POSITION.TOP
    assert chart.legend.include_in_layout is False
    manual = chart.legend._element.find(qn("c:layout")).find(qn("c:manualLayout"))
    assert manual is not None
    assert manual.find(qn("c:xMode")).get("val") == "edge"
    # The legend box in fractions of the frame (which runs past the canvas by
    # the axis slack, see the frame test), a hair wider than measured.
    frame_h = item["h"] + pptx_writer._AXIS_FRAME_SLACK_PX
    assert float(manual.find(qn("c:y")).get("val")) == pytest.approx((240 - 240) / frame_h, abs=1e-4)
    assert float(manual.find(qn("c:h")).get("val")) == pytest.approx(40 / frame_h, abs=1e-4)
    slack = 520 * pptx_writer._LEGEND_SLACK_RATIO
    assert float(manual.find(qn("c:x")).get("val")) == pytest.approx((700 - slack / 2 - 120) / 1680, abs=1e-4)
    assert float(manual.find(qn("c:w")).get("val")) == pytest.approx((520 + slack) / 1680, abs=1e-4)
    # The plot area on the viewer's grid box.
    plot_layout = chart._chartSpace.chart.plotArea.find(qn("c:layout")).find(qn("c:manualLayout"))
    assert plot_layout.find(qn("c:layoutTarget")).get("val") == "inner"
    assert float(plot_layout.find(qn("c:x")).get("val")) == pytest.approx((220 - 120) / 1680, abs=1e-4)

    assert plot.has_data_labels is True
    labels = plot.data_labels
    assert labels.number_format == '[>=1000000]"\u20ac"#,##0.0,,"M";[>=1000]"\u20ac"#,##0,"k";"\u20ac"#,##0'
    assert labels.number_format_is_linked is False
    assert labels.position == XL_LABEL_POSITION.OUTSIDE_END
    assert labels.font.bold is True
    assert labels.show_value is True and labels.show_series_name is False


def test_line_labels_sit_above_and_area_and_donut_take_no_position():
    """PowerPoint repairs a file whose area chart declares a label position,
    and a doughnut has none: those two write no ``c:dLblPos``."""
    from pptx.enum.chart import XL_LABEL_POSITION
    from pptx.oxml.ns import qn

    line = _the_chart(_build([_chart_item("line")])).chart.plots[0]
    assert line.data_labels.position == XL_LABEL_POSITION.ABOVE
    for series in line.series:
        assert series.smooth is True
        assert series.marker.style is not None
    area = _the_chart(_build([_chart_item("area")])).chart.plots[0]
    assert area.has_data_labels is True
    assert area._element.find(qn("c:dLbls")).find(qn("c:dLblPos")) is None


def test_a_stacked_chart_writes_its_totals_as_the_text_the_viewer_wrote():
    """A stack's TOTAL has no PowerPoint data label: the viewer's totals are
    placed as text boxes where it measured them, and no native label is
    turned on (it would label every segment instead)."""
    from pptx.enum.chart import XL_CHART_TYPE

    labels = [
        {"text": "\u20ac48.2M", "x": 300, "y": 330, "w": 90, "h": 28},
        {"text": "\u20ac50M", "x": 700, "y": 320, "w": 80, "h": 28},
    ]
    item = _chart_item("stacked_bar", labels=labels)
    slide = _build([item])
    chart = _the_chart(slide).chart
    plot = chart.plots[0]
    assert chart.chart_type == XL_CHART_TYPE.COLUMN_STACKED
    assert plot.overlap == pptx_writer._BAR_OVERLAP_STACKED
    assert plot.has_data_labels is False
    texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
    assert texts == ["\u20ac48.2M", "\u20ac50M"]
    # A stacked area takes the stacked chart type as well.
    area = _the_chart(_build([_chart_item("area", chart={"stacked": True})])).chart
    assert area.chart_type == XL_CHART_TYPE.AREA_STACKED


def test_a_horizontal_stacks_totals_are_pinned_at_the_bar_end_inside_the_frame():
    """The viewer keeps room past a horizontal stack's longest bar for
    its total and writes it there, anchored at the bar's end (``text-anchor:
    start``); the writer pins the text box the same way - left-aligned on the
    measured run, no slack - so a `100%` total measured inside the canvas ends
    inside the chart frame, and a run PowerPoint sets wider grows into the room
    the viewer kept, never back over the bar. A value anchored at its right
    edge keeps that edge; a centred one keeps the symmetric slack it had."""
    from pptx.enum.chart import XL_CHART_TYPE

    share = {
        "format": "percent",
        "unit": None,
        "horizontal": True,
        "categories": ["EMEA", "Americas", "APAC", "LATAM"],
        "series": [
            {"name": name, "values": values, "color": color}
            for (name, values), color in zip(
                [("Finance", [40, 35, 30, 25]), ("Public", [25, 25, 25, 25]), ("Energy", [35, 40, 45, 50])],
                ["rgb(0, 21, 168)", "rgb(64, 196, 255)", "rgb(255, 171, 64)"],
                strict=True,
            )
        ],
    }
    # The canvas (the frame) spans x 120..1800; the plot ends at 1733 and each
    # total starts LABEL_OFFSET past it, its 60.7px run ending 2.7px inside.
    totals = [
        {"text": "100%", "x": 1739, "y": 330 + row * 140, "w": 60.7, "h": 20, "role": "total", "series": 2, "category": row, "anchor": "start"}
        for row in range(4)
    ]
    # One value written back toward the axis (a negative bar's), and one centred.
    others = [
        {"text": "-5%", "x": 300, "y": 900, "w": 40, "h": 20, "role": "value", "series": 0, "category": 0, "anchor": "end"},
        {"text": "50%", "x": 900, "y": 900, "w": 50, "h": 20, "role": "value", "series": 0, "category": 1, "anchor": "middle"},
    ]
    item = _chart_item("stacked_bar", chart=share, labels=totals + others, inner={"x": 300, "y": 320, "w": 1433, "h": 560})
    item["axis"] = {"min": 0, "max": 100, "step": 25}
    slide = _build([item])
    frame = _the_chart(slide)
    assert frame.chart.chart_type == XL_CHART_TYPE.BAR_STACKED
    _slide_w, _slide_h, emu_per_px, _pt = pptx_writer._slide_geometry("16:9")
    frame_right = frame.left + frame.width
    boxes = {shape.text_frame.text: shape for shape in slide.shapes if shape.has_text_frame}
    pinned = [shape for shape in slide.shapes if shape.has_text_frame and shape.text_frame.text == "100%"]
    assert len(pinned) == 4
    for shape in pinned:
        assert shape.text_frame.paragraphs[0].alignment == PP_ALIGN.LEFT
        assert shape.text_frame.word_wrap is False
        assert shape.left == Emu(int(1739 * emu_per_px))
        assert shape.width == Emu(int(60.7 * emu_per_px))
        assert shape.left + shape.width <= frame_right
    # Anchored at its right edge: the box ends where the text did.
    end = boxes["-5%"]
    assert end.text_frame.paragraphs[0].alignment == PP_ALIGN.RIGHT
    assert end.left + end.width == Emu(int(300 * emu_per_px)) + Emu(int(40 * emu_per_px))
    # Centred: a hair wider than measured on both sides, as before.
    middle = boxes["50%"]
    assert middle.text_frame.paragraphs[0].alignment == PP_ALIGN.CENTER
    assert middle.left == Emu(int((900 - 3) * emu_per_px))
    assert middle.width == Emu(int((50 + 6) * emu_per_px))


def test_grouped_bar_labels_take_the_size_and_the_turn_the_viewer_gave_them():
    """A value wider than its bar is set smaller or turned on its side by
    the viewer, never dropped; PowerPoint's own data labels follow - the size
    the drawing published (``data-label-size``, canvas px times the body's fit)
    and, when turned, a label reading bottom to top (``a:bodyPr/@rot``)."""
    from pptx.oxml.ns import qn

    _slide_w, _slide_h, _emu, pt_per_px = pptx_writer._slide_geometry("16:9")
    item = _chart_item("bar", label_size=20, label_rotate=True)
    plot = _the_chart(_build([item])).chart.plots[0]
    labels = plot.data_labels
    assert labels.font.size.pt == pytest.approx(round(20 * pt_per_px, 1))
    body = labels._element.find(qn("c:txPr")).find(qn("a:bodyPr"))
    assert body.get("rot") == pptx_writer._LABEL_ROTATION_UPRIGHT
    # Upright labels keep the tick size and no turn.
    upright = _the_chart(_build([_chart_item("bar")])).chart.plots[0].data_labels
    assert upright.font.size.pt == pytest.approx(round(_CHART_STYLE["size"] * pt_per_px, 1))
    assert upright._element.find(qn("c:txPr")).find(qn("a:bodyPr")).get("rot") is None


def test_the_chart_text_takes_the_sizes_the_viewer_drew_its_label_size_in():
    """``label_size`` sets the viewer's ticks and values; the writer
    reads the sizes the page measured - the tick font, the values' published
    size, the legend's own size - and falls back on the chart's text scale
    (``chart_tick_px``, the fit included) only where the page measured none."""
    _slide_w, _slide_h, _emu, pt_per_px = pptx_writer._slide_geometry("16:9")
    style = {**_CHART_STYLE, "size": 18, "legend_size": 20}
    chart = _the_chart(_build([_chart_item("bar", style=style, label_size=12)])).chart
    assert chart.font.size.pt == pytest.approx(round(18 * pt_per_px, 1))
    assert chart.category_axis.tick_labels.font.size.pt == pytest.approx(round(18 * pt_per_px, 1))
    assert chart.plots[0].data_labels.font.size.pt == pytest.approx(round(12 * pt_per_px, 1))
    assert chart.legend.font.size.pt == pytest.approx(round(20 * pt_per_px, 1))

    unmeasured = {key: value for key, value in _CHART_STYLE.items() if key not in ("size", "legend_size")}
    small = _the_chart(_build([_chart_item("bar", style={**unmeasured, "fit": 0.8}, chart={"label_size": "sm"})])).chart
    assert small.font.size.pt == pytest.approx(round(18 * 0.8 * pt_per_px, 1))
    # The legend without a measured size reads like the ticks, as before.
    assert small.legend.font.size.pt == pytest.approx(round(18 * 0.8 * pt_per_px, 1))


def test_values_with_a_size_of_their_own_keep_it_and_the_ticks_keep_label_size():
    """``value_label_size`` sizes the values apart from the axis text.
    The reported combo at ``label_size`` md and ``value_label_size`` 20: the
    page measured its ticks at 22px and its values at 20px, and PowerPoint's
    data labels and tick labels take those two sizes."""
    _slide_w, _slide_h, _emu, pt_per_px = pptx_writer._slide_geometry("16:9")
    combo = {
        "series": [
            {"name": "A", "kind": "bar", "values": [2.6, 3.6, 4.2, 4.3], "color": "rgb(0, 21, 168)"},
            {"name": "B", "kind": "bar", "values": [2.6, 3.7, 2.5, 6.3], "color": "rgb(64, 196, 255)"},
        ],
        "label_size": "md",
        "value_label_size": 20,
    }
    style = {**_CHART_STYLE, "size": 22}
    chart = _the_chart(_build([_chart_item("combo", chart=combo, style=style, label_size=20)])).chart
    assert chart.category_axis.tick_labels.font.size.pt == pytest.approx(round(22 * pt_per_px, 1))
    assert chart.value_axis.tick_labels.font.size.pt == pytest.approx(round(22 * pt_per_px, 1))
    assert chart.plots[0].data_labels.font.size.pt == pytest.approx(round(20 * pt_per_px, 1))


def test_values_the_page_did_not_measure_take_the_size_their_chart_names_converted_like_the_ticks():
    """Where the page published no size for the values, the writer
    falls back on the chart's ``value_label_size`` - its px, or a text scale's
    first value size - through the conversion the ticks' fallback uses (the
    fit included); a chart that names none labels its values at the ticks'
    size, as before."""
    _slide_w, _slide_h, _emu, pt_per_px = pptx_writer._slide_geometry("16:9")
    unmeasured = {key: value for key, value in _CHART_STYLE.items() if key not in ("size", "legend_size")}
    style = {**unmeasured, "fit": 0.8}
    for named, value_px in ((20, 20), ("sm", 18), ("lg", 26)):
        item = _chart_item("bar", style=style, chart={"label_size": "md", "value_label_size": named})
        chart = _the_chart(_build([item])).chart
        assert chart.font.size.pt == pytest.approx(round(22 * 0.8 * pt_per_px, 1)), named
        assert chart.category_axis.tick_labels.font.size.pt == pytest.approx(round(22 * 0.8 * pt_per_px, 1)), named
        assert chart.plots[0].data_labels.font.size.pt == pytest.approx(round(value_px * 0.8 * pt_per_px, 1)), named
    plain = _the_chart(_build([_chart_item("bar", style=style, chart={"label_size": "md"})])).chart
    assert plain.plots[0].data_labels.font.size.pt == pytest.approx(round(22 * 0.8 * pt_per_px, 1))
    # A stack's segments without a measured size follow the values' size too.
    stack = _chart_item("stacked_bar", style=style, chart={"label_size": "md", "value_label_size": 16})
    stack["labels"] = [{"text": "\u20ac11.2M", "x": 300, "y": 700, "w": 70, "h": 20, "role": "segment", "series": 0, "category": 0}]
    first = _the_chart(_build([stack])).chart.plots[0].series[0]
    assert first.data_labels.font.size.pt == pytest.approx(round(16 * 0.8 * pt_per_px, 1))


def test_a_stack_labels_the_segments_the_viewer_wrote_inside_and_places_its_totals():
    """The values the viewer wrote INSIDE a stack's segments become
    PowerPoint's own centred data labels on exactly those segments - every
    other point of the series has its label deleted - in an ink that reads on
    the segment; the totals stay the viewer's text, turned when it turned them.
    The stack is as wide as the viewer's (``_BAR_GAP_WIDTH_STACKED``)."""
    from pptx.enum.chart import XL_LABEL_POSITION
    from pptx.oxml.ns import qn

    labels = [
        {"text": "\u20ac11.2M", "x": 300, "y": 700, "w": 70, "h": 20, "role": "segment", "series": 0, "category": 0},
        {"text": "\u20ac22.4M", "x": 700, "y": 690, "w": 70, "h": 20, "role": "segment", "series": 0, "category": 1},
        {"text": "\u20ac9.4M", "x": 700, "y": 520, "w": 60, "h": 20, "role": "segment", "series": 1, "category": 1},
        {"text": "\u20ac26.3M", "x": 310, "y": 330, "w": 28, "h": 80, "role": "total", "series": 1, "category": 0, "rotate": True},
    ]
    item = _chart_item("stacked_bar", labels=labels, label_size=22, segment_size=16)
    slide = _build([item])
    plot = _the_chart(slide).chart.plots[0]
    assert plot.gap_width == pptx_writer._BAR_GAP_WIDTH_STACKED
    # No plot-wide label: it would label every segment.
    assert plot.has_data_labels is False

    first, second = plot.series
    for series, kept in ((first, {0, 1}), (second, {1})):
        d_lbls = series._element.find(qn("c:dLbls"))
        assert d_lbls is not None
        assert series.data_labels.position == XL_LABEL_POSITION.CENTER
        assert series.data_labels.show_value is True
        deleted = {
            int(d_lbl.find(qn("c:idx")).get("val"))
            for d_lbl in d_lbls.findall(qn("c:dLbl"))
            if d_lbl.find(qn("c:delete")) is not None
        }
        assert deleted == set(range(4)) - kept
    _slide_w, _slide_h, _emu, pt_per_px = pptx_writer._slide_geometry("16:9")
    assert first.data_labels.font.size.pt == pytest.approx(round(16 * pt_per_px, 1))
    # The ink, as the viewer's: near-white on the dark blue first series,
    # near-black on the light blue second one.
    assert first.data_labels.font.color.rgb == pptx_writer._rgb(pptx_writer._achromatic(pptx_writer._SEGMENT_INK_LIGHT))
    assert second.data_labels.font.color.rgb == pptx_writer._rgb(pptx_writer._achromatic(pptx_writer._SEGMENT_INK_DARK))
    assert str(first.data_labels.font.color.rgb) == "FCFCFC"

    # The total: one text box, turned to read bottom to top about the centre it was drawn on.
    boxes = [shape for shape in slide.shapes if shape.has_text_frame]
    assert [shape.text_frame.text for shape in boxes] == ["\u20ac26.3M"]
    (total,) = boxes
    assert total.rotation == pytest.approx(270.0)
    _slide_w, _slide_h, emu_per_px, _pt = pptx_writer._slide_geometry("16:9")
    assert total.left + total.width / 2 == pytest.approx((310 + 14) * emu_per_px, abs=emu_per_px)
    assert total.top + total.height / 2 == pytest.approx((330 + 40) * emu_per_px, abs=emu_per_px)


def test_a_segment_value_is_inked_from_its_segment_as_the_viewer_inks_it():
    """The viewer inks a value inside a segment white or near-black from the
    segment's OKLCH lightness (a relative colour in presentation.css); the
    writer reads the same threshold off the resolved colour."""
    from pathlib import Path

    css = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation" / "presentation.css"
    ).read_text(encoding="utf-8")
    light, threshold, dark = (
        pptx_writer._SEGMENT_INK_LIGHT,
        pptx_writer._SEGMENT_INK_THRESHOLD,
        pptx_writer._SEGMENT_INK_DARK,
    )
    assert f"clamp({dark:g}, ({threshold:g} - l) * 1000, {light:g}) 0 0)" in css
    assert pptx_writer._segment_ink((0, 21, 168)) == pptx_writer._achromatic(light)
    assert pptx_writer._segment_ink((242, 190, 58)) == pptx_writer._achromatic(dark)
    # The OKLCH lightness of the extremes, and a mid grey.
    assert pptx_writer._oklab_lightness((255, 255, 255)) == pytest.approx(1.0, abs=1e-3)
    assert pptx_writer._oklab_lightness((0, 0, 0)) == pytest.approx(0.0, abs=1e-6)
    assert pptx_writer._oklab_lightness((119, 119, 119)) == pytest.approx(0.5693, abs=1e-3)


#: A stack whose second series is too thin to hold its value: the
#: value the viewer wrote past the stack, over the total, and its leader's legs.
_PAST_THE_STACK_LABELS = [
    {"text": "\u20ac11.2M", "x": 300, "y": 700, "w": 70, "h": 20, "role": "segment", "series": 0, "category": 0},
    {"text": "\u20ac48.2M", "x": 300, "y": 330, "w": 90, "h": 28, "role": "total", "series": 1, "category": 0, "anchor": "middle"},
    {"text": "\u20ac612k", "x": 319, "y": 296, "w": 52, "h": 18, "role": "annotation", "series": 1, "category": 0, "anchor": "middle"},
]
_PAST_THE_STACK_LEADER = [
    {"x": 380, "y": 359, "w": 8, "h": 2, "series": 1},
    {"x": 387, "y": 304, "w": 2, "h": 57, "series": 1},
    {"x": 374, "y": 304, "w": 15, "h": 2, "series": 1},
]


def test_a_thin_segment_value_past_its_stack_is_the_viewers_text_in_its_series_colour_with_its_leader():
    """The value the viewer wrote past a stack, its segment too thin to
    hold it, is placed as its text where the viewer measured it - in its
    series' colour, at the size the page published - and its leader is drawn
    leg by leg, solid, in the same colour, at the stroke the viewer measured.
    The total keeps the values' colour; the segment inside keeps PowerPoint's
    own label."""
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    from pptx.shapes.connector import Connector

    item = _chart_item(
        "stacked_bar",
        labels=_PAST_THE_STACK_LABELS,
        leaders=_PAST_THE_STACK_LEADER,
        label_size=22,
        segment_size=16,
        annotation_size=14,
    )
    slide = _build([item])
    _slide_w, _slide_h, emu_per_px, pt_per_px = pptx_writer._slide_geometry("16:9")
    boxes = {shape.text_frame.text: shape for shape in slide.shapes if shape.has_text_frame}
    assert set(boxes) == {"\u20ac48.2M", "\u20ac612k"}
    value = boxes["\u20ac612k"].text_frame.paragraphs[0].runs[0]
    assert value.font.size.pt == pytest.approx(round(14 * pt_per_px, 1))
    assert value.font.color.rgb == pptx_writer._rgb((64, 196, 255))
    total = boxes["\u20ac48.2M"].text_frame.paragraphs[0].runs[0]
    assert total.font.size.pt == pytest.approx(round(22 * pt_per_px, 1))
    assert total.font.color.rgb == pptx_writer._rgb((255, 255, 255))
    # Where the viewer wrote it: centred on the measured box.
    assert boxes["\u20ac612k"].left + boxes["\u20ac612k"].width / 2 == pytest.approx((319 + 26) * emu_per_px, abs=emu_per_px)
    legs = [shape for shape in slide.shapes if isinstance(shape, Connector)]
    assert len(legs) == 3
    for leg in legs:
        assert leg.line.color.rgb == pptx_writer._rgb((64, 196, 255))
        assert leg.line.dash_style == MSO_LINE_DASH_STYLE.SOLID
        assert leg.line.width.pt == pytest.approx(max(0.75, 2 * pt_per_px), abs=0.01)
    # The lane runs up beside the stack along its measured centre.
    lane = next(leg for leg in legs if leg.begin_x == leg.end_x)
    assert lane.begin_x == Emu(int(388 * emu_per_px))
    # The segment inside the stack is still PowerPoint's own label.
    first = _the_chart(slide).chart.plots[0].series[0]
    assert first.data_labels.show_value is True


def test_values_past_a_stack_are_written_when_the_chart_writes_no_totals():
    """A series' own show_values on a stacked bar: the chart writes no
    totals, the series its segments' values - the one too thin is still the
    viewer's text with its leader in the PPTX."""
    from pptx.shapes.connector import Connector

    labels = [label for label in _PAST_THE_STACK_LABELS if label["role"] != "total"]
    item = _chart_item("stacked_bar", labels=labels, leaders=_PAST_THE_STACK_LEADER, values=False, annotation_size=14)
    slide = _build([item])
    assert [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame] == ["\u20ac612k"]
    assert len([shape for shape in slide.shapes if isinstance(shape, Connector)]) == 3


def test_large_number_labels_are_compact_like_the_viewers():
    """An axis chart whose axis writes compact ticks writes compact
    labels too (``2.2M users``, never ``3,800,000 users``); a pie keeps every digit."""
    users = [{"name": "Users", "values": [1_200_000, 3_800_000], "color": "rgb(0, 21, 168)"}]
    chart = {"format": "number", "unit": "users", "series": users, "categories": ["Q1", "Q2"]}
    plot = _the_chart(_build([_chart_item("bar", chart=chart)])).chart.plots[0]
    assert plot.data_labels.number_format == '[>=1000000]#,##0.0,,"M"" users";[>=1000]#,##0,"k"" users";#,##0" users"'
    pie = _the_chart(_build([_chart_item("pie", chart=chart)])).chart.plots[0]
    assert pie.data_labels.number_format == '#,##0" users"'


def test_a_horizontal_chart_lists_its_categories_top_down_with_the_value_axis_below():
    """The viewer lists categories top-down; PowerPoint's bar chart puts the
    first at the bottom. The category axis is reversed and the VALUE axis is
    told to cross at the category maximum (``c:valAx/c:crosses`` - the element
    python-pptx's ``value_axis.crosses`` would NOT write), so the ticks stay
    at the bottom. The frame also grows to the LEFT so the right-aligned
    category labels keep their room instead of wrapping."""
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.oxml.ns import qn

    hours = [{"name": "Time to first response", "values": [12, 26, 31, 48], "color": "rgb(0, 21, 168)"}]
    item = _chart_item("bar", chart={"horizontal": True, "unit": "h", "format": "number", "series": hours})
    item["axis"] = {"min": 0, "max": 60, "step": 20}
    frame = _the_chart(_build([item]))
    chart = frame.chart
    assert chart.chart_type == XL_CHART_TYPE.BAR_CLUSTERED
    assert chart.category_axis.reverse_order is True
    assert chart.value_axis._element.find(qn("c:crosses")).get("val") == "max"
    assert chart.value_axis.tick_labels.number_format == "#,##0"
    assert chart.plots[0].data_labels.number_format == '#,##0"h"'
    _slide_w, _slide_h, emu_per_px, _pt = pptx_writer._slide_geometry("16:9")
    slack = pptx_writer._AXIS_FRAME_SLACK_PX
    assert frame.left == Emu(int((item["x"] - slack) * emu_per_px))
    assert frame.width == Emu(int((item["w"] + slack) * emu_per_px))


def test_an_axis_charts_frame_runs_past_the_canvas_and_a_pies_does_not():
    """PowerPoint sets its bottom axis labels lower than the viewer's ticks and
    clips them to the frame: the transparent frame runs a little past the
    canvas (never past the slide), so the last row of labels is never cut. A
    pie has no axis and keeps the box it was measured in."""
    _slide_w, slide_h, emu_per_px, _pt = pptx_writer._slide_geometry("16:9")
    bar = _the_chart(_build([_chart_item("bar")]))
    assert bar.top == Emu(int(240 * emu_per_px))
    assert bar.height == Emu(int((720 + pptx_writer._AXIS_FRAME_SLACK_PX) * emu_per_px))
    pie = _the_chart(_build([_chart_item("pie")]))
    assert pie.height == Emu(int(720 * emu_per_px))
    # Clamped: a chart ending near the slide's bottom edge cannot run off it.
    low = _the_chart(_build([_chart_item("bar", y=340, h=730)]))
    assert low.top + low.height <= slide_h


def test_a_chart_title_is_an_editable_text_box_the_chart_never_covers():
    """A chart's own ``title`` reaches the slide as the text primitive
    the extractor visits before the chart - an editable text box in the head's
    place, top-left of the frame - and the chart is framed off its legend and
    canvas alone, the legend box on the title's right and the plot under it:
    nothing PowerPoint draws covers the title."""
    from pptx.oxml.ns import qn

    run = {
        "text": "Monthly signups",
        "font": "Geologica",
        "size": 24,
        "weight": 500,
        "italic": False,
        "color": {"r": 255, "g": 255, "b": 255, "a": 1},
    }
    # The head's row: the title at the left, the legend on the right; the
    # canvas under both, the plot's grid inside it.
    title = _text_item([run], x=136, y=248, w=400, h=32, lineHeight=32)
    item = _chart_item(
        "line",
        x=120,
        y=248,
        w=1680,
        h=712,
        plot={"x": 120, "y": 296, "w": 1680, "h": 664},
        legend={"x": 1200, "y": 248, "w": 520, "h": 32},
        inner={"x": 220, "y": 336, "w": 1560, "h": 560},
    )
    slide = _build([title, item])
    shapes = list(slide.shapes)
    text = next(shape for shape in shapes if shape.has_text_frame and shape.text_frame.text == "Monthly signups")
    frame = _the_chart(slide)
    # Visited first, so stacked under the (transparent) chart frame.
    assert shapes.index(text) < shapes.index(frame)
    # The plot area starts under the title's box...
    chart = frame.chart
    plot_layout = chart._chartSpace.chart.plotArea.find(qn("c:layout")).find(qn("c:manualLayout"))
    plot_top = frame.top + float(plot_layout.find(qn("c:y")).get("val")) * frame.height
    assert plot_top > text.top + text.height
    # ...and the legend sits on its right, never over it.
    legend_layout = chart.legend._element.find(qn("c:layout")).find(qn("c:manualLayout"))
    legend_left = frame.left + float(legend_layout.find(qn("c:x")).get("val")) * frame.width
    assert legend_left > text.left + text.width
    assert chart.has_title is False
    """The viewer names the target in its legend; a PowerPoint legend lists
    series only, so the name sits by the line. The extractor hands the line
    over with its stroke width (an SVG line's own box has none), centred on
    the line - the connector runs along that centre."""
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    from pptx.shapes.connector import Connector

    item = _chart_item(
        "bar",
        chart={"target": 30_000_000, "target_name": "Plan"},
        target_line={"x": 220, "y": 458.5, "w": 1560, "h": 3},
    )
    slide = _build([item])
    connectors = [shape for shape in slide.shapes if isinstance(shape, Connector)]
    assert len(connectors) == 1
    line = connectors[0]
    _slide_w, _slide_h, emu_per_px, _pt = pptx_writer._slide_geometry("16:9")
    assert line.begin_y == line.end_y == Emu(int(460 * emu_per_px))
    assert line.begin_x == Emu(int(220 * emu_per_px)) and line.end_x == Emu(int(1780 * emu_per_px))
    assert line.line.dash_style == MSO_LINE_DASH_STYLE.DASH
    assert str(line.line.color.rgb) == "FFAB40"
    names = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
    assert names == ["Plan"]
    # No target in the payload: a stray box draws nothing.
    quiet = _build([_chart_item("bar", target_line={"x": 220, "y": 458.5, "w": 1560, "h": 3})])
    assert not any(isinstance(shape, Connector) for shape in quiet.shapes)


def test_the_series_colours_are_the_viewers_blended_on_the_charts_surface():
    """Colours arrive resolved (``{r,g,b,a}``) in the theme the page was
    rendered in; a translucent one is flattened on the panel the chart sits
    on, never on black. The chart's text takes the viewer's tick colour."""
    item = _chart_item("bar", colors=[_rgba(0, 21, 168, 0.5), _rgba(64, 196, 255)])
    item["style"]["background"] = _rgba(255, 255, 255)
    chart = _the_chart(_build([item])).chart
    fills = [str(s.format.fill.fore_color.rgb) for s in chart.plots[0].series]
    assert fills[0] == str(pptx_writer._rgb(pptx_writer.blend(_rgba(0, 21, 168, 0.5), (255, 255, 255))))
    assert fills[1] == "40C4FF"
    assert chart.font.name == "IBM Plex Sans"
    # 22px at 0.5pt/px, rounded to a tenth.
    assert chart.font.size.pt == pytest.approx(11.0)


def test_a_chart_that_cannot_be_read_is_logged_and_left_out():
    """A payload with no categories or no series (the page did not publish
    what the writer needs) is skipped with a line in the log, never a chart
    of nothing and never an exception that loses the whole export."""
    log: list[str] = []
    slide = _build([_chart_item("bar", chart={"categories": []})], log=log)
    assert not any(getattr(shape, "has_chart", False) for shape in slide.shapes)
    assert log == ["Slide 1: a chart could not be read from the rendered page and was left out."]
    log.clear()
    slide = _build([_chart_item("line", chart={"kind": "sunburst"})], log=log)
    assert not any(getattr(shape, "has_chart", False) for shape in slide.shapes)
    assert len(log) == 1


def test_a_gauge_is_pasted_as_a_picture_and_said_so_in_the_log():
    """What PowerPoint cannot draw arrives as an ``image`` item tagged with
    the kind (and the extractor's reason): the capture is placed as a picture
    and the export log tells the person which slide and why. An untagged
    picture (an icon) says nothing."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from app.services.chart_export import TWO_AXIS_BARS_REASON, picture_note

    log: list[str] = []
    slide = _build(
        [
            {"kind": "image", "id": "x0-1", "x": 400, "y": 300, "w": 800, "h": 500, "opacity": 1, "chart": "gauge"},
            {"kind": "image", "id": "x0-2", "x": 1700, "y": 900, "w": 96, "h": 96, "opacity": 1},
        ],
        captures={"x0-1": _png(), "x0-2": _png()},
        log=log,
    )
    pictures = [shape for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert len(pictures) == 2
    assert log == [picture_note(1, "gauge")]
    assert "PowerPoint has no gauge chart" in log[0]
    # Bars read against two value axes stay a picture too, and the
    # note gives the extractor's reason, not the kind's.
    log.clear()
    _build(
        [
            {
                "kind": "image",
                "id": "x0-1",
                "x": 400,
                "y": 300,
                "w": 800,
                "h": 500,
                "opacity": 1,
                "chart": "bar",
                "chart_reason": TWO_AXIS_BARS_REASON,
            }
        ],
        captures={"x0-1": _png()},
        log=log,
    )
    assert log == [
        "Slide 1: the bar chart is a picture of the rendered figure - "
        "PowerPoint draws the bars of two value axes over each other."
    ]


def test_a_map_is_pasted_as_a_picture_its_title_legend_and_caption_written_as_text():
    """A map is not a kind PowerPoint can draw, so the extractor pastes
    its canvas - captured at the slide's 2x - as a picture tagged with the kind,
    and walks its title, its ramp legend and its caption as it walks any text:
    the writer places the picture, writes the text as editable runs, and the
    log says which slide holds a map picture."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from app.services.chart_export import NATIVE_CHART_KINDS, picture_note
    from app.services.presentation_export import NATIVE_PPTX_IMAGE_SCALE

    assert "map" not in NATIVE_CHART_KINDS and NATIVE_PPTX_IMAGE_SCALE == 2
    ink = {"r": 236, "g": 238, "b": 242, "a": 1}

    def run(text: str, size: int) -> dict:
        return {
            "text": text, "font": "IBM Plex Sans", "size": size, "weight": 400, "italic": False,
            "color": ink, "stroke": None, "underline": False, "mono": False,
        }

    log: list[str] = []
    slide = _build(
        [
            _text_item([run("Customers by country", 30)], x=120, y=200, w=600, h=40),
            _text_item([run("Customers", 20)], x=1300, y=206, w=140, h=28),
            _text_item([run("9", 20)], x=1450, y=206, w=20, h=28),
            # The ramp's bar is a gradient: a picture of its own.
            {"kind": "image", "id": "x0-2", "x": 1480, "y": 214, "w": 120, "h": 12, "opacity": 1},
            _text_item([run("84", 20)], x=1610, y=206, w=30, h=28),
            {"kind": "image", "id": "x0-1", "x": 120, "y": 260, "w": 1680, "h": 460, "opacity": 1, "chart": "map", "chart_reason": "map"},
            _text_item([run("End of quarter", 20)], x=120, y=740, w=400, h=28),
        ],
        captures={"x0-1": _png(), "x0-2": _png()},
        log=log,
    )
    pictures = [shape for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert len(pictures) == 2
    texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame and shape.text_frame.text]
    assert texts == ["Customers by country", "Customers", "9", "84", "End of quarter"]
    assert not any(getattr(shape, "has_chart", False) for shape in slide.shapes)
    assert log == [picture_note(1, "map", "map")]
    assert "PowerPoint has no map chart that opens outside Office" in log[0]


# ── Two units on one chart ────────────────────────────────────────────


def _board_chart_item(**chart) -> dict:
    """"New logos and ASP" as the extractor emits it: bars of a count against
    the left-hand axis, a price line against the right-hand one, both axes
    the viewer settled on, each tick ink and the dotted right-hand target."""
    payload = {
        "kind": "combo",
        "series": [
            {"name": "New logos", "values": [24, 34, 42, 56], "color": "rgb(0, 21, 168)", "kind": "bar"},
            {
                "name": "ASP",
                "values": [240_000, 330_000, 420_000, 540_000],
                "color": "rgb(64, 196, 255)",
                "kind": "line",
                "axis": "secondary",
            },
        ],
        "format": "number",
        "unit": None,
        "secondary_format": "currency",
        "secondary_unit": "EUR",
        "secondary_target": 500_000,
        "secondary_target_name": "\u20ac500k",
    }
    payload.update(chart)
    style = {**_CHART_STYLE, "primary_tick": _rgba(0, 21, 168), "secondary_tick": _rgba(64, 196, 255)}
    style["secondary_target"] = _rgba(255, 171, 64)
    return _chart_item(
        "combo",
        chart=payload,
        axis={"min": 0, "max": 60, "step": 20},
        secondary_axis={"min": 0, "max": 600_000, "step": 200_000},
        secondary_target_line={"x": 220, "y": 412.5, "w": 1460, "h": 4},
        style=style,
    )


def _axis_ids(element) -> list[str]:
    from pptx.oxml.ns import qn

    return [axis_id.get("val") for axis_id in element.findall(qn("c:axId"))]


def test_a_combo_is_one_editable_chart_of_a_bar_plot_and_a_line_plot():
    """A combo on one axis is native now: its bars and its line are two plots
    of ONE chart, on the same pair of axes, the line painted over the bars,
    each series keeping its column of the embedded workbook. The bars carry
    labels; the line carries its own where the viewer wrote them."""
    from pptx.chart.plot import BarPlot, LinePlot
    from pptx.oxml.ns import qn

    series = [
        {**_CHART_SERIES[0], "kind": "bar"},
        {**_CHART_SERIES[1], "kind": "line"},
    ]
    log: list[str] = []
    chart = _the_chart(_build([_chart_item("combo", chart={"series": series})], log=log)).chart
    assert log == []
    bars, line = chart.plots
    assert isinstance(bars, BarPlot) and isinstance(line, LinePlot)
    assert [(s.name, s.index) for s in bars.series] == [("Bookings", 0)]
    assert [(s.name, s.index) for s in line.series] == [("Pipeline", 1)]
    assert list(line.series[0].values) == [float(v) for v in _CHART_SERIES[1]["values"]]
    assert _axis_ids(bars._element) == _axis_ids(line._element)
    assert len(chart._chartSpace.chart.plotArea.findall(qn("c:valAx"))) == 1
    assert str(line.series[0].format.line.color.rgb) == "40C4FF"
    assert bars.has_data_labels is True and line.has_data_labels is False


def test_a_combo_line_carries_its_values_where_the_viewer_wrote_them():
    """Over a point, PowerPoint's label ABOVE it; under one the viewer
    moved clear of a bar's value, a point label BELOW it carrying the series'
    number format (a point's ``c:dLbl`` inherits nothing); raised over the
    values in its way, the text the viewer wrote where it wrote it; not
    written, no label at all."""
    from pptx.oxml.ns import qn

    series = [{**_CHART_SERIES[0], "kind": "bar"}, {**_CHART_SERIES[1], "kind": "line"}]
    written = [
        {"text": "\u20ac9M", "x": 300, "y": 400, "w": 60, "h": 24, "role": "value", "series": 1, "category": 0},
        {"text": "\u20ac12M", "x": 700, "y": 520, "w": 70, "h": 24, "role": "value", "series": 1, "category": 1, "placement": "below"},
        {"text": "\u20ac15M", "x": 1100, "y": 300, "w": 70, "h": 24, "role": "value", "series": 1, "category": 2, "placement": "lifted"},
    ]
    slide = _build([_chart_item("combo", chart={"series": series}, labels=written, point_size=18)])
    _bars, line = _the_chart(slide).chart.plots
    labels = line.series[0]._element.find(qn("c:dLbls"))
    assert labels.find(qn("c:dLblPos")).get("val") == "t"
    number_format = labels.find(qn("c:numFmt")).get("formatCode")
    assert number_format.startswith('[>=1000000]"\u20ac"')
    points = {int(d.find(qn("c:idx")).get("val")): d for d in labels.findall(qn("c:dLbl"))}
    # Over its point: the series' own label, no override.
    assert 0 not in points
    under = points[1]
    assert under.find(qn("c:dLblPos")).get("val") == "b"
    assert under.find(qn("c:numFmt")).get("formatCode") == number_format
    assert under.find(qn("c:showVal")).get("val") == "1"
    # Raised, or not written: no PowerPoint label on the point.
    for category in (2, 3):
        assert points[category].find(qn("c:delete")).get("val") == "1"
    texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
    assert texts == ["\u20ac15M"]


def _label(text: str, series: int, category: int, placement: str | None = None, x: float = 300) -> dict:
    """A value as the extractor measures it (``.deck-chart-value``)."""
    label = {"text": text, "x": x, "y": 400, "w": 70, "h": 24, "role": "value", "series": series, "category": category}
    return {**label, "placement": placement} if placement else label


def test_a_line_chart_carries_its_values_where_the_viewer_moved_them():
    """A line's value the viewer moved clear of something is placed point
    by point - under its point PowerPoint's own label BELOW it, slid clear of the
    axis' ticks the text the viewer wrote, not written no label - while a series
    whose values all sit over their points keeps the plot's labels untouched."""
    from pptx.oxml.ns import qn

    written = [
        _label("\u20ac18M", 0, 0, "shifted", x=140),
        _label("\u20ac22M", 0, 1),
        _label("\u20ac27M", 0, 2, "below"),
        *(_label(text, 1, index) for index, text in enumerate(["\u20ac30M", "\u20ac28M", "\u20ac31M", "\u20ac35M"])),
    ]
    slide = _build([_chart_item("line", labels=written, label_size=22)])
    plot = _the_chart(slide).chart.plots[0]
    assert plot.has_data_labels is True
    moved, kept = plot.series
    assert kept._element.find(qn("c:dLbls")) is None
    labels = moved._element.find(qn("c:dLbls"))
    assert labels.find(qn("c:dLblPos")).get("val") == "t"
    points = {int(d.find(qn("c:idx")).get("val")): d for d in labels.findall(qn("c:dLbl"))}
    assert points[0].find(qn("c:delete")).get("val") == "1"
    assert 1 not in points
    assert points[2].find(qn("c:dLblPos")).get("val") == "b"
    # The viewer wrote no value over the fourth point: PowerPoint writes none either.
    assert points[3].find(qn("c:delete")).get("val") == "1"
    texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
    assert texts == ["\u20ac18M"]


def test_a_line_whose_values_alternate_over_and_under_the_points_is_labelled_point_by_point():
    """A line in a narrow panel writes its values alternating over and
    under their points, and its facts say so (``values_alternate``). The PPTX
    labels every series point by point - PowerPoint's own label ABOVE the even
    points, BELOW the odd ones, in the series' number format - and writes no
    text box: the positions are PowerPoint's, where the viewer put them."""
    from pptx.oxml.ns import qn

    categories = [f"Q{index}" for index in range(1, 9)]
    values = [246_800, 277_800, 305_400, 342_600, 379_600, 429_200, 475_000, 536_400]
    written = [
        _label(f"\u20ac{value / 1000:.1f}k", 0, category, "below" if category % 2 else None, x=100 + 48 * category)
        for category, value in enumerate(values)
    ]
    chart = {"categories": categories, "series": [{"name": "ASP", "values": values, "color": "rgb(0, 21, 168)"}]}
    slide = _build([_chart_item("line", chart=chart, labels=written, values_alternate=True, label_size=18)])
    plot = _the_chart(slide).chart.plots[0]
    (series,) = plot.series
    labels = series._element.find(qn("c:dLbls"))
    assert labels.find(qn("c:dLblPos")).get("val") == "t"
    number_format = labels.find(qn("c:numFmt")).get("formatCode")
    points = {int(d.find(qn("c:idx")).get("val")): d for d in labels.findall(qn("c:dLbl"))}
    assert sorted(points) == [1, 3, 5, 7]
    for point in points.values():
        assert point.find(qn("c:dLblPos")).get("val") == "b"
        assert point.find(qn("c:numFmt")).get("formatCode") == number_format
        assert point.find(qn("c:delete")) is None
    assert [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame] == []


def _bar_value(text: str, series: int, category: int, lift: float | None = None) -> dict:
    """A value past a bar as the extractor measures it, with the lift a fitted plan gave it."""
    return {**_label(text, series, category), "lift": lift}


def test_bars_whose_values_the_viewer_staggered_carry_powerpoint_labels_a_row_apart():
    """Under ``value_label_fit`` stagger every value is written upright
    at the size asked, every other one a row out. The PPTX keeps PowerPoint's
    own labels, at that size, outside the end of each bar: a value the viewer
    wrote a row out is a point label of its own moved up by that lift (a share
    of the chart's height, ``c:manualLayout``), in the series' number format;
    a value in its place keeps the series' label. No text box is written. The
    bars are spaced as the viewer drew them once they gave way."""
    from pptx.oxml.ns import qn

    written = [_bar_value("v", 0, category) for category in range(4)]
    written += [_bar_value("v", 1, category, 27.0) for category in range(4)]
    spacing = {"width": 28, "gap": 15.4, "category_gap": 31.17}
    item = _chart_item("bar", labels=written, label_size=22, values_fit="stagger", bar_spacing=spacing)
    slide = _build([item])
    frame = _the_chart(slide)
    plot = frame.chart.plots[0]
    assert plot.gap_width == round(100 * 31.17 / 28)
    assert plot.overlap == -round(100 * 15.4 / 28)
    near, far = plot.series
    for series in (near, far):
        labels = series._element.find(qn("c:dLbls"))
        assert labels is not None
        assert labels.find(qn("c:dLblPos")).get("val") == "outEnd"
        assert labels.find(qn("c:txPr")) is not None
    assert near._element.find(qn("c:dLbls")).findall(qn("c:dLbl")) == []
    labels = far._element.find(qn("c:dLbls"))
    number_format = labels.find(qn("c:numFmt")).get("formatCode")
    points = {int(d.find(qn("c:idx")).get("val")): d for d in labels.findall(qn("c:dLbl"))}
    assert sorted(points) == [0, 1, 2, 3]
    # The chart's frame, in slide px (the 16:9 slide is 1920px across).
    height = frame.height * 1920 / 12_192_000
    for point in points.values():
        assert point.find(qn("c:delete")) is None
        tags = [child.tag for child in point]
        assert tags[:3] == [qn("c:idx"), qn("c:layout"), qn("c:numFmt")]
        manual = point.find(qn("c:layout")).find(qn("c:manualLayout"))
        assert float(manual.find(qn("c:x")).get("val")) == 0
        assert float(manual.find(qn("c:y")).get("val")) == pytest.approx(-27.0 / height, abs=1e-6)
        assert point.find(qn("c:numFmt")).get("formatCode") == number_format
        assert point.find(qn("c:dLblPos")).get("val") == "outEnd"
    assert [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame] == []


def test_bars_whose_values_the_viewer_kept_upright_drop_the_ones_it_left_out():
    """Under ``value_label_fit`` upright a value that would touch
    another is not written; PowerPoint writes no label for it either, and
    every other value keeps the series' label in its place."""
    from pptx.oxml.ns import qn

    written = [_bar_value("v", 0, category) for category in range(4)] + [_bar_value("v", 1, 2)]
    slide = _build([_chart_item("bar", labels=written, label_size=22, values_fit="upright")])
    plot = _the_chart(slide).chart.plots[0]
    # Bars that did not have to give way keep the writer's spacing.
    assert plot.gap_width == pptx_writer._BAR_GAP_WIDTH
    assert plot.overlap == pptx_writer._BAR_OVERLAP_CLUSTERED
    near, far = plot.series
    assert near._element.find(qn("c:dLbls")).findall(qn("c:dLbl")) == []
    points = {int(d.find(qn("c:idx")).get("val")): d for d in far._element.find(qn("c:dLbls")).findall(qn("c:dLbl"))}
    assert sorted(points) == [0, 1, 3]
    for point in points.values():
        assert point.find(qn("c:delete")).get("val") == "1"


def test_horizontal_bars_whose_values_the_viewer_staggered_move_their_labels_along_the_row():
    """On a horizontal chart a staggered value moves along its row,
    past the value in its way. Its point label is moved right of its bar's
    end by that lift - a share of the chart's WIDTH, ``c:manualLayout/c:x`` -
    and stays on its row (``c:y`` 0); a fall's label moves left. A value in
    its place keeps the series' label."""
    from pptx.oxml.ns import qn

    chart = {
        "horizontal": True,
        "series": [
            {"name": "A", "values": [12, 18, 9, 14], "color": "rgb(0, 21, 168)"},
            {"name": "B", "values": [13, -4, 10, 15], "color": "rgb(64, 196, 255)"},
        ],
    }
    written = [_bar_value("v", 0, category) for category in range(4)]
    written += [_bar_value("v", 1, category, 54.0) for category in range(4)]
    slide = _build([_chart_item("bar", chart=chart, labels=written, label_size=22, values_fit="stagger")])
    frame = _the_chart(slide)
    near, far = frame.chart.plots[0].series
    assert near._element.find(qn("c:dLbls")).findall(qn("c:dLbl")) == []
    points = {int(d.find(qn("c:idx")).get("val")): d for d in far._element.find(qn("c:dLbls")).findall(qn("c:dLbl"))}
    assert sorted(points) == [0, 1, 2, 3]
    # The chart's frame, in slide px (the 16:9 slide is 1920px across).
    width = frame.width * 1920 / 12_192_000
    for category, point in points.items():
        manual = point.find(qn("c:layout")).find(qn("c:manualLayout"))
        along = -54.0 if category == 1 else 54.0
        assert float(manual.find(qn("c:x")).get("val")) == pytest.approx(along / width, abs=1e-6)
        assert float(manual.find(qn("c:y")).get("val")) == 0
        assert point.find(qn("c:dLblPos")).get("val") == "outEnd"
    assert [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame] == []


def test_a_horizontal_stack_fitted_by_stagger_writes_every_value_at_the_size_asked():
    """A horizontal stack under ``value_label_fit`` stagger writes its
    totals, the values inside its segments and a too-short segment's value
    past the stack all at the size ``label_size`` asks for - the page
    publishes one size for the three (``label_size``, ``segment_size``,
    ``annotation_size``) and the PPTX sets each at it."""
    labels = [
        {"text": "41%", "x": 700, "y": 300, "w": 40, "h": 18, "role": "segment", "series": 0, "category": 0},
        {"text": "100%", "x": 1500, "y": 300, "w": 60, "h": 18, "role": "total", "series": 1, "category": 0, "anchor": "start"},
        {"text": "3%", "x": 1580, "y": 300, "w": 28, "h": 18, "role": "annotation", "series": 1, "category": 0, "anchor": "start"},
    ]
    leaders = [
        {"x": 820, "y": 270, "w": 2, "h": 12, "series": 1},
        {"x": 820, "y": 270, "w": 774, "h": 2, "series": 1},
        {"x": 1593, "y": 270, "w": 2, "h": 20, "series": 1},
    ]
    item = _chart_item(
        "stacked_bar",
        chart={"horizontal": True},
        labels=labels,
        leaders=leaders,
        label_size=22,
        segment_size=22,
        annotation_size=22,
        values_fit="stagger",
    )
    slide = _build([item])
    _slide_w, _slide_h, _emu, pt_per_px = pptx_writer._slide_geometry("16:9")
    size = round(22 * pt_per_px, 1)
    boxes = {shape.text_frame.text: shape.text_frame.paragraphs[0].runs[0] for shape in slide.shapes if shape.has_text_frame}
    assert set(boxes) == {"100%", "3%"}
    assert boxes["100%"].font.size.pt == pytest.approx(size)
    assert boxes["3%"].font.size.pt == pytest.approx(size)
    first = _the_chart(slide).chart.plots[0].series[0]
    assert first.data_labels.font.size.pt == pytest.approx(size)


def test_a_chart_whose_values_were_not_fitted_writes_its_bar_labels_as_before():
    """``auto``: no fit published, no spacing published - the plot's
    labels alone, the writer's spacing, exactly the XML written before."""
    from pptx.oxml.ns import qn

    written = [_bar_value("v", series, category) for series in (0, 1) for category in range(4)]
    plot = _the_chart(_build([_chart_item("bar", labels=written, label_size=22)])).chart.plots[0]
    assert all(series._element.find(qn("c:dLbls")) is None for series in plot.series)
    assert plot._element.find(qn("c:dLbls")).find(qn("c:dLblPos")).get("val") == "outEnd"
    assert (plot.gap_width, plot.overlap) == (pptx_writer._BAR_GAP_WIDTH, pptx_writer._BAR_OVERLAP_CLUSTERED)


#: Three bars a quarter, the churn below zero.
_GROUPED_SERIES = [
    {"name": "New logos", "values": [2_400_000, 1_800_000, 2_200_000, 1_400_000], "color": "rgb(15, 188, 255)"},
    {"name": "Expansion", "values": [1_400_000, 1_200_000, 1_000_000, 800_000], "color": "rgb(0, 240, 188)"},
    {"name": "Churn", "values": [-600_000, -800_000, -400_000, -1_000_000], "color": "rgb(164, 122, 240)"},
]
#: How the viewer publishes them under a fit in a span 2 panel at 16px
#: (``chart-value-label-fit.test.tsx``): where ``auto`` puts them, 26px wide,
#: 6px apart in a quarter, the quarters 32.13px apart.
_GROUPED_SPACING = {"width": 26, "gap": 6, "category_gap": 32.13}


@pytest.mark.parametrize("fit", [None, "stagger", "upright"])
def test_a_group_of_upright_bars_keeps_its_tight_groups_in_powerpoint_under_every_fit(fit):
    """Under ``value_label_fit`` stagger or upright a group of upright
    bars keeps the room ``auto`` gives it, and the viewer publishes it. The
    PPTX spaces PowerPoint's bars from it - the gap between two quarters and
    between two bars of one over a bar's width - and labels them as the
    viewer wrote them: the expansion of Q2, which would touch the new logos'
    value, a row out under stagger and left out under upright; the churn's
    values under their bars. Unfitted, nothing is published and the writer
    keeps its constants, the XML written before."""
    from pptx.oxml.ns import qn

    lift = 22.0 if fit == "stagger" else 0.0
    written = [
        _bar_value("v", series, category, lift if (series, category) == (1, 1) else 0.0 if fit else None)
        for series in range(3)
        for category in range(4)
        if not (fit == "upright" and (series, category) == (1, 1))
    ]
    facts = {"values_fit": fit, "bar_spacing": _GROUPED_SPACING} if fit else {}
    item = _chart_item(
        "bar",
        chart={"series": _GROUPED_SERIES},
        colors=[_rgba(15, 188, 255), _rgba(0, 240, 188), _rgba(164, 122, 240)],
        axis={"min": -1_000_000, "max": 3_000_000, "step": 1_000_000},
        labels=written,
        label_size=16,
        **facts,
    )
    slide = _build([item])
    frame = _the_chart(slide)
    plot = frame.chart.plots[0]
    assert len(plot.series) == 3
    if fit is None:
        assert (plot.gap_width, plot.overlap) == (pptx_writer._BAR_GAP_WIDTH, pptx_writer._BAR_OVERLAP_CLUSTERED)
        assert all(series._element.find(qn("c:dLbls")) is None for series in plot.series)
        return
    assert plot.gap_width == round(100 * 32.13 / 26) == 124
    assert plot.overlap == -round(100 * 6 / 26) == -23
    new_logos, expansion, churn = plot.series
    for series in (new_logos, churn):
        assert series._element.find(qn("c:dLbls")).findall(qn("c:dLbl")) == []
    points = {int(d.find(qn("c:idx")).get("val")): d for d in expansion._element.find(qn("c:dLbls")).findall(qn("c:dLbl"))}
    assert sorted(points) == [1]
    if fit == "upright":
        assert points[1].find(qn("c:delete")).get("val") == "1"
        return
    height = frame.height * 1920 / 12_192_000
    manual = points[1].find(qn("c:layout")).find(qn("c:manualLayout"))
    assert float(manual.find(qn("c:y")).get("val")) == pytest.approx(-22.0 / height, abs=1e-6)


def test_the_spacing_of_bars_that_gave_way_stays_within_powerpoints_bounds():
    """A stack overlaps fully whatever its gap; a gap past a bar's width is the
    most PowerPoint writes; a spacing with no width is no spacing."""
    assert pptx_writer._bar_spacing({"width": 20, "gap": 0, "category_gap": 30}, True) == (150, 100)
    assert pptx_writer._bar_spacing({"width": 10, "gap": 40, "category_gap": 90}, False) == (500, -100)
    assert pptx_writer._bar_spacing({"width": 0, "gap": 4, "category_gap": 9}, False) is None
    assert pptx_writer._bar_spacing(None, False) is None


def test_the_alternating_fact_alone_labels_a_line_point_by_point():
    """The fact drives the writer: a series the page planned alternating is
    labelled point by point even when every value it measured sits over its
    point (a steep line's odd values found their room over it after all), so
    the export follows the page's plan rather than a reading of its labels."""
    from pptx.oxml.ns import qn

    written = [_label("v", 0, index) for index in range(4)] + [_label("v", 1, index) for index in range(4)]
    plot = _the_chart(_build([_chart_item("line", labels=written, values_alternate=True)])).chart.plots[0]
    for series in plot.series:
        labels = series._element.find(qn("c:dLbls"))
        assert labels is not None and labels.find(qn("c:dLblPos")).get("val") == "t"
        assert labels.findall(qn("c:dLbl")) == []
    # Without the fact, the same labels leave the plot's labels alone (the test below).
    untouched = _the_chart(_build([_chart_item("line", labels=written)])).chart.plots[0]
    assert all(series._element.find(qn("c:dLbls")) is None for series in untouched.series)


def test_a_line_chart_whose_values_sit_over_their_points_keeps_the_plot_labels_alone():
    """No collision, no change: every value over its point means no series of
    its own labels, exactly the plain chart XML."""
    from pptx.oxml.ns import qn

    written = [_label("v", series, category) for series in (0, 1) for category in range(4)]
    plot = _the_chart(_build([_chart_item("line", labels=written)])).chart.plots[0]
    assert all(series._element.find(qn("c:dLbls")) is None for series in plot.series)
    assert plot._element.find(qn("c:dLbls")).find(qn("c:dLblPos")).get("val") == "t"


def test_an_area_writes_a_value_moved_under_its_point_as_the_text_the_viewer_wrote():
    """An area's labels accept no position (PowerPoint repairs the file), so a
    value the viewer set under its point is its text, its PowerPoint label deleted."""
    from pptx.oxml.ns import qn

    written = [_label("\u20ac9M", 0, 0, "below"), *(_label("v", 0, index) for index in (1, 2, 3))]
    written += [_label("v", 1, index) for index in range(4)]
    slide = _build([_chart_item("area", labels=written)])
    moved = _the_chart(slide).chart.plots[0].series[0]
    labels = moved._element.find(qn("c:dLbls"))
    assert labels.find(qn("c:dLblPos")) is None
    (point,) = labels.findall(qn("c:dLbl"))
    assert point.find(qn("c:delete")).get("val") == "1"
    assert [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame] == ["\u20ac9M"]


def test_a_series_whose_own_show_values_is_off_keeps_no_label():
    """The payload marks a series that leaves its values to the tooltip;
    its bars carry no PowerPoint label while the others keep theirs."""
    from pptx.oxml.ns import qn

    series = [{**_CHART_SERIES[0], "show_values": False}, _CHART_SERIES[1]]
    plot = _the_chart(_build([_chart_item("bar", chart={"series": series})])).chart.plots[0]
    assert plot.has_data_labels is True
    quiet, kept = plot.series
    assert quiet._element.find(qn("c:dLbls")).find(qn("c:delete")).get("val") == "1"
    assert kept._element.find(qn("c:dLbls")) is None


def test_a_combo_line_writes_its_values_when_its_bars_write_none():
    """The bars' own ``show_values`` off: the page writes no value past a
    bar (``values`` false) but publishes the line's size, and PowerPoint labels
    the line where the viewer wrote it, the bars not at all."""
    from pptx.oxml.ns import qn

    series = [{**_CHART_SERIES[0], "kind": "bar", "show_values": False}, {**_CHART_SERIES[1], "kind": "line"}]
    written = [_label("v", 1, index, "lifted" if index == 3 else None) for index in range(4)]
    slide = _build([_chart_item("combo", chart={"series": series}, labels=written, values=False, point_size=18)])
    bars, line = _the_chart(slide).chart.plots
    assert bars.has_data_labels is False
    labels = line.series[0]._element.find(qn("c:dLbls"))
    assert labels.find(qn("c:dLblPos")).get("val") == "t"
    (lifted,) = labels.findall(qn("c:dLbl"))
    assert lifted.find(qn("c:delete")).get("val") == "1"
    assert [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame] == ["v"]


def test_a_second_unit_reads_against_a_right_hand_value_axis():
    """The acceptance chart in the editable PPTX: the price line is
    a plot of its own on a second value axis - on the right, crossing the
    categories at their far end, on the ticks the viewer drew, written as the
    viewer wrote them (``€300k``), in the line's ink - while the count keeps
    the left-hand axis, its gridlines and its own format. The right-hand
    target is a dotted connector named by its value."""
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    from pptx.oxml.ns import qn
    from pptx.shapes.connector import Connector

    log: list[str] = []
    slide = _build([_board_chart_item()], log=log)
    assert log == []
    chart = _the_chart(slide).chart
    bars, line = chart.plots
    plot_area = chart._chartSpace.chart.plotArea
    primary, secondary = plot_area.findall(qn("c:valAx"))
    category, hidden = plot_area.findall(qn("c:catAx"))
    assert _axis_ids(bars._element) == [_axis_ids(category)[0], _axis_ids(primary)[0]]
    assert _axis_ids(line._element) == [_axis_ids(hidden)[0], _axis_ids(secondary)[0]]
    assert hidden.find(qn("c:delete")).get("val") == "1"
    assert secondary.find(qn("c:axPos")).get("val") == "r"
    assert secondary.find(qn("c:crosses")).get("val") == "max"
    assert secondary.find(qn("c:crossAx")).get("val") == _axis_ids(hidden)[0]

    left, right = pptx_writer._value_axes(chart)
    assert (left.minimum_scale, left.maximum_scale, left.major_unit) == (0.0, 60.0, 20.0)
    assert (right.minimum_scale, right.maximum_scale, right.major_unit) == (0.0, 600_000.0, 200_000.0)
    assert left.tick_labels.number_format == "#,##0"
    assert right.tick_labels.number_format == '[>=1000]"\u20ac"#,##0,"k";"\u20ac"#,##0'
    assert left.has_major_gridlines is True and right.has_major_gridlines is False
    assert str(left.tick_labels.font.color.rgb) == "0015A8"
    assert str(right.tick_labels.font.color.rgb) == "40C4FF"
    # The bars write the count's format; the price line its own axis'.
    assert bars.data_labels.number_format == "#,##0"
    line_labels = line.series[0]._element.find(qn("c:dLbls"))
    assert line_labels.find(qn("c:numFmt")).get("formatCode") == '[>=1000]"\u20ac"#,##0,"k";"\u20ac"#,##0'

    connectors = [shape for shape in slide.shapes if isinstance(shape, Connector)]
    assert len(connectors) == 1
    assert connectors[0].line.dash_style == MSO_LINE_DASH_STYLE.ROUND_DOT
    assert [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame] == ["\u20ac500k"]


def test_a_ratio_axis_writes_its_unit_and_two_lines_label_in_their_own_formats():
    """"Burn and net new ARR": millions on the left, a ratio on the
    right whose ticks keep their unit (``0.5x``) so the axis says what it
    reads; on a line chart with two axes each plot labels its values in the
    format of its own axis."""
    burn = _board_chart_item(
        kind="combo",
        format="currency",
        unit="EUR",
        series=[
            {"name": "Burn", "values": [3_400_000, 3_900_000, 4_600_000, 3_100_000], "color": "x", "kind": "bar"},
            {"name": "Net new ARR", "values": [2_100_000, 3_400_000, 4_900_000, 6_200_000], "color": "y", "kind": "bar"},
            {"name": "Burn multiple", "values": [1.62, 1.15, 0.94, 0.5], "color": "z", "kind": "line", "axis": "secondary"},
        ],
        secondary_format="number",
        secondary_unit="x",
        secondary_target=1,
        secondary_target_name="1x",
    )
    burn["axis"] = {"min": 0, "max": 8_000_000, "step": 2_000_000}
    burn["secondary_axis"] = {"min": 0, "max": 2, "step": 0.5}
    burn["colors"] = [_rgba(229, 57, 53), _rgba(67, 160, 71), _rgba(64, 196, 255)]
    chart = _the_chart(_build([burn])).chart
    bars, line = chart.plots
    assert [s.name for s in bars.series] == ["Burn", "Net new ARR"]
    assert [s.name for s in line.series] == ["Burn multiple"]
    left, right = pptx_writer._value_axes(chart)
    assert right.tick_labels.number_format == '#,##0.0"x"'
    assert left.tick_labels.number_format.startswith('[>=1000000]"\u20ac"#,##0,,"M"')

    lines = _board_chart_item(
        kind="line",
        format="percent",
        series=[
            {"name": "NRR", "values": [104, 108, 112, 117], "color": "x"},
            {"name": "Customers", "values": [610, 640, 690, 744], "color": "y", "axis": "secondary"},
        ],
        secondary_format="number",
        secondary_unit="logos",
        secondary_target=None,
    )
    lines["axis"] = {"min": 0, "max": 150, "step": 50}
    lines["secondary_axis"] = {"min": 0, "max": 750, "step": 250}
    chart = _the_chart(_build([lines])).chart
    nrr, customers = chart.plots
    assert nrr.data_labels.number_format == '#,##0"%"'
    assert customers.data_labels.number_format == '#,##0" logos"'
    assert pptx_writer._value_axes(chart)[1].tick_labels.number_format == '#,##0" logos"'


def test_the_decimals_a_chart_sets_reach_the_editable_chart():
    """``secondary_decimals: 2`` writes the burn multiple 1.27x on the
    right-hand axis of the combo, and on the labels of a line chart that
    plots it against that axis - the left-hand axis keeps its own reading."""
    multiple = [0.96, 1.04, 0.47, 1.57, 1.18, 1.56, 1.27]
    quarters = ["Q1 25", "Q2 25", "Q3 25", "Q4 25", "Q1 26", "Q2 26", "Q3 26"]
    combo = _board_chart_item(
        kind="combo",
        categories=quarters,
        format="currency",
        unit="EUR",
        series=[
            {"name": "Net new ARR", "values": [1.56e6, 1.95e6, 4.1e6, 1.6e6, 2.8e6, 1.5e6, 2.2e6], "color": "x", "kind": "bar"},
            {"name": "Burn multiple", "values": multiple, "color": "z", "kind": "line", "axis": "secondary"},
        ],
        secondary_format="number",
        secondary_unit="x",
        secondary_decimals=2,
        secondary_target=None,
    )
    combo["axis"] = {"min": 0, "max": 6_000_000, "step": 2_000_000}
    combo["secondary_axis"] = {"min": 0, "max": 2, "step": 0.5}
    left, right = pptx_writer._value_axes(_the_chart(_build([combo])).chart)
    assert right.tick_labels.number_format == '#,##0.00"x"'
    assert left.tick_labels.number_format.startswith('[>=1000000]"\u20ac"#,##0,,"M"')

    line = {**combo["chart"], "kind": "line"}
    line["series"] = [{**entry, "kind": None} for entry in line["series"]]
    chart = _the_chart(_build([{**combo, "chart": line}])).chart
    arr, burn = chart.plots
    assert burn.data_labels.number_format == '#,##0.00"x"'
    # Unset on the left, the run decides: 1.56M and 1.6M would both read €1.6M.
    assert arr.data_labels.number_format.startswith('[>=1000000]"\u20ac"#,##0.00,,"M"')


#: What the viewer writes on the bridge with ``unit_scale: "M"`` and two
#: decimals (pinned in ``chart-unit-scale.test.tsx``): the PPTX places it as is.
_PINNED_BRIDGE_TEXT = ["+\u20ac2.50M", "+\u20ac1.25M", "-\u20ac0.25M", "\u20ac3.50M"]


def test_a_pinned_unit_scale_reaches_the_editable_chart():
    """A chart that pins ``unit_scale`` labels and ticks every amount in
    that one letter - one format section, its decimals - so PowerPoint writes
    the slide's strings; a waterfall's signed labels are the viewer's text."""
    bars = _chart_item(
        "bar",
        chart={
            "categories": ["Q3", "Q4"],
            "series": [{"name": "ARR", "values": [480_000, 1_200_000], "color": "x"}],
            "decimals": 1,
            "unit_scale": "M",
        },
        colors=_CHART_COLORS[:1],
        axis={"min": 0, "max": 1_500_000, "step": 500_000},
    )
    chart = _the_chart(_build([bars])).chart
    assert chart.plots[0].data_labels.number_format == '"\u20ac"#,##0.0,,"M"'
    assert pptx_writer._value_axes(chart)[0].tick_labels.number_format == '"\u20ac"#,##0.0,,"M"'

    steps = [
        {"start": 0, "end": 2_500_000, "value": 2_500_000, "role": "increase", "color": "var(--deck-success)"},
        {"start": 2_500_000, "end": 3_750_000, "value": 1_250_000, "role": "increase", "color": "var(--deck-success)"},
        {"start": 3_750_000, "end": 3_500_000, "value": -250_000, "role": "decrease", "color": "var(--deck-error)"},
        {"start": 0, "end": 3_500_000, "value": 3_500_000, "role": "total", "color": "var(--deck-accent)"},
    ]
    written = [{"text": text, "x": 260 + 300 * i, "y": 300, "w": 110, "h": 28} for i, text in enumerate(_PINNED_BRIDGE_TEXT)]
    bridge = _waterfall_item(
        steps,
        ["Licences", "Services", "Churn", "Ending"],
        labels=written,
        step_colors=[_rgba(102, 187, 106), _rgba(102, 187, 106), _rgba(239, 83, 80), _rgba(0, 21, 168)],
        connectors=[],
    )
    bridge["chart"].update({"decimals": 2, "unit_scale": "M"})
    bridge["axis"] = {"min": 0, "max": 5_000_000, "step": 1_000_000}
    slide = _build([bridge])
    chart = _the_chart(slide).chart
    assert pptx_writer._value_axes(chart)[0].tick_labels.number_format == '"\u20ac"#,##0.00,,"M"'
    assert [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame] == _PINNED_BRIDGE_TEXT


def test_the_legend_keeps_every_entry_in_the_room_powerpoint_needs():
    """PowerPoint drops a legend entry that does not fit its box, and lays a
    top legend it cannot fit out anew - over the panel title the frame
    covers. A line's key is wider than the viewer's swatch, so the box grows
    by it, into the room the frame has on the right first; a legend the
    viewer wrapped (three series and a target in a narrow panel) is stacked,
    one row per entry."""
    from pptx.enum.chart import XL_LEGEND_POSITION
    from pptx.oxml.ns import qn

    def manual(chart) -> dict[str, float]:
        layout = chart.legend._element.find(qn("c:layout")).find(qn("c:manualLayout"))
        return {tag: float(layout.find(qn(f"c:{tag}")).get("val")) for tag in ("x", "y", "w", "h")}

    # A one-row legend ending at the frame's right edge grows to the left only.
    item = _board_chart_item()
    item["legend"] = {"x": 1578, "y": 240, "w": 222, "h": 30}
    chart = _the_chart(_build([item])).chart
    assert chart.legend.position == XL_LEGEND_POSITION.TOP
    slack = 222 * pptx_writer._LEGEND_SLACK_RATIO + pptx_writer._LEGEND_LINE_KEY_PX
    box = manual(chart)
    assert box["x"] == pytest.approx((1578 - slack - 120) / 1680, abs=1e-4)
    assert box["x"] + box["w"] == pytest.approx(1.0, abs=1e-4)

    # The viewer wrapped it on three rows: stacked, a row per series.
    wrapped = _board_chart_item(
        series=[
            {"name": "Burn", "values": [1, 2, 3, 4], "color": "x", "kind": "bar"},
            {"name": "Net new ARR", "values": [2, 3, 4, 5], "color": "y", "kind": "bar"},
            {"name": "Burn multiple", "values": [1.6, 1.2, 0.9, 0.5], "color": "z", "kind": "line", "axis": "secondary"},
        ],
    )
    wrapped["legend"] = {"x": 1640, "y": 240, "w": 160, "h": 70}
    wrapped["legend_rows"] = 2
    chart = _the_chart(_build([wrapped])).chart
    assert chart.legend.position == XL_LEGEND_POSITION.RIGHT
    frame_h = wrapped["h"] + pptx_writer._AXIS_FRAME_SLACK_PX
    assert manual(chart)["h"] == pytest.approx(70 / 2 * 3 / frame_h, abs=1e-4)


def test_the_legend_sits_on_the_side_the_slide_put_it():
    """``legend_position``: PowerPoint's legend goes to the side the
    viewer drew it on - ``c:legendPos`` ``b`` under the plot, ``r`` beside it
    - pinned by manual layout to the box the viewer gave it; a bottom legend,
    centred under the plot, grows its slack both ways alike. An explicit
    ``top`` is the top legend; the stacked fallback of a wrapped legend
    belongs to the unset default alone. A payload without the field, or with
    a side the viewer cannot draw, exports as before."""
    from pptx.enum.chart import XL_LEGEND_POSITION
    from pptx.oxml.ns import qn

    def manual(chart) -> dict[str, float]:
        layout = chart.legend._element.find(qn("c:layout")).find(qn("c:manualLayout"))
        return {tag: float(layout.find(qn(f"c:{tag}")).get("val")) for tag in ("x", "y", "w", "h")}

    def legend_pos(chart) -> str:
        # `c:legendPos/@val` defaults to `r` in the schema, and python-pptx
        # writes the default by leaving the attribute (or the element) out.
        element = chart.legend._element.find(qn("c:legendPos"))
        return element.get("val", "r") if element is not None else "r"

    # Under the plot, centred: the measured box, a hair wider on both sides.
    item = _chart_item("stacked_bar", chart={"legend_position": "bottom"})
    item["legend"] = {"x": 760, "y": 920, "w": 400, "h": 30}
    item["plot"] = {"x": 120, "y": 240, "w": 1680, "h": 660}
    chart = _the_chart(_build([item])).chart
    assert chart.legend.position == XL_LEGEND_POSITION.BOTTOM
    assert legend_pos(chart) == "b"
    assert chart.legend.include_in_layout is False
    frame_h = item["h"] + pptx_writer._AXIS_FRAME_SLACK_PX
    slack = 400 * pptx_writer._LEGEND_SLACK_RATIO
    box = manual(chart)
    assert box["x"] == pytest.approx((760 - slack / 2 - 120) / 1680, abs=1e-4)
    assert box["w"] == pytest.approx((400 + slack) / 1680, abs=1e-4)
    assert box["y"] == pytest.approx((920 - 240) / frame_h, abs=1e-4)
    assert box["h"] == pytest.approx(30 / frame_h, abs=1e-4)
    # A bottom legend the viewer wrapped stays at the bottom, on its two rows.
    item["legend_rows"] = 2
    item["legend"] = {"x": 760, "y": 920, "w": 400, "h": 60}
    chart = _the_chart(_build([item])).chart
    assert legend_pos(chart) == "b"
    assert manual(chart)["h"] == pytest.approx(60 / frame_h, abs=1e-4)

    # Beside the plot: a column of entries, one row each, on the right.
    donut = _chart_item("donut", chart={"legend_position": "right"})
    donut["legend"] = {"x": 1560, "y": 480, "w": 200, "h": 160}
    donut["legend_rows"] = 4
    chart = _the_chart(_build([donut])).chart
    assert chart.legend.position == XL_LEGEND_POSITION.RIGHT
    assert legend_pos(chart) == "r"
    box = manual(chart)
    assert box["y"] == pytest.approx((480 - 240) / 720, abs=1e-4)
    assert box["h"] == pytest.approx(160 / 720, abs=1e-4)
    assert box["x"] + box["w"] <= 1.0 + 1e-6

    # An explicit top is the top legend, wrapped or not.
    top = _board_chart_item(legend_position="top")
    top["legend"] = {"x": 1640, "y": 240, "w": 160, "h": 70}
    top["legend_rows"] = 2
    assert legend_pos(_the_chart(_build([top])).chart) == "t"
    # Unset, and a side the viewer cannot draw: the export as it always was.
    for payload in ({}, {"legend_position": None}, {"legend_position": "left"}):
        before = _chart_item("bar", chart=payload)
        assert legend_pos(_the_chart(_build([before])).chart) == "t"
        stacked = _board_chart_item(**payload)
        stacked["legend"] = {"x": 1640, "y": 240, "w": 160, "h": 70}
        stacked["legend_rows"] = 2
        assert legend_pos(_the_chart(_build([stacked])).chart) == "r"


#: The ARR bridge as the viewer drew it with `allow_gap`: the ending is
#: 0.06M short of its steps, so an "Unexplained" bar stands before it.
_BRIDGE_CATEGORIES = ["Opening ARR", "New logos", "Expansion", "Churn", "Unexplained", "Closing ARR"]
_BRIDGE_STEPS = [
    {"start": 0, "end": 16_500_000, "value": 16_500_000, "role": "total", "color": "var(--deck-accent)"},
    {"start": 16_500_000, "end": 18_300_000, "value": 1_800_000, "role": "increase", "color": "var(--deck-success)"},
    {"start": 18_300_000, "end": 19_600_000, "value": 1_300_000, "role": "increase", "color": "var(--deck-success)"},
    {"start": 19_600_000, "end": 19_200_000, "value": -400_000, "role": "decrease", "color": "var(--deck-error)"},
    {"start": 19_200_000, "end": 19_140_000, "value": -60_000, "role": "gap", "color": "var(--deck-warning)"},
    {"start": 0, "end": 19_140_000, "value": 19_140_000, "role": "total", "color": "var(--deck-accent)"},
]
_BRIDGE_COLORS = [
    _rgba(0, 21, 168),
    _rgba(102, 187, 106),
    _rgba(102, 187, 106),
    _rgba(239, 83, 80),
    _rgba(255, 171, 64),
    _rgba(0, 21, 168),
]


def _waterfall_item(steps: list[dict] | None = None, categories: list[str] | None = None, **overrides) -> dict:
    steps = _BRIDGE_STEPS if steps is None else steps
    categories = _BRIDGE_CATEGORIES if categories is None else categories
    chart = {
        "categories": categories,
        "series": [{"name": "ARR", "values": [s["value"] for s in steps], "color": "var(--deck-accent)"}],
        "steps": steps,
        "stacked": False,
        "legend": False,
    }
    measured = {
        "legend": None,
        "step_colors": _BRIDGE_COLORS[: len(steps)],
        "labels": [
            {"text": "\u20ac16.5M", "x": 260, "y": 300, "w": 90, "h": 28},
            {"text": "-\u20ac60k", "x": 1300, "y": 420, "w": 90, "h": 28},
        ],
        "connectors": [{"x": 380, "y": 336, "w": 120, "h": 2}, {"x": 640, "y": 320, "w": 120, "h": 2}],
    }
    return _chart_item("waterfall", chart=chart, **{**measured, **overrides})


def test_a_waterfall_is_an_editable_stacked_column_chart_with_an_invisible_base():
    """The bridge exports as PowerPoint draws one by hand: each bar stands on
    an invisible base series, wears its step's colour (an increase, a
    decrease, a total, the gap), carries the signed change the viewer wrote as
    text (a data label would write the bar's length), and the connectors run
    where the viewer drew them. The log says it is an emulation."""
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.enum.dml import MSO_FILL, MSO_LINE_DASH_STYLE
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.shapes.connector import Connector

    from app.services.chart_export import emulation_note

    log: list[str] = []
    slide = _build([_waterfall_item()], log=log)
    chart = _the_chart(slide).chart
    assert chart.chart_type == XL_CHART_TYPE.COLUMN_STACKED
    plot = chart.plots[0]
    assert plot.overlap == pptx_writer._BAR_OVERLAP_STACKED
    assert [str(c) for c in plot.categories] == _BRIDGE_CATEGORIES
    # No step crosses zero: a base and the bars, nothing below the axis.
    assert [s.name for s in plot.series] == [pptx_writer._WATERFALL_BASE_NAME, "ARR"]
    base, bars = plot.series
    assert list(base.values) == pytest.approx([0, 16_500_000, 18_300_000, 19_200_000, 19_140_000, 0])
    assert list(bars.values) == pytest.approx([16_500_000, 1_800_000, 1_300_000, 400_000, 60_000, 19_140_000])
    assert base.format.fill.type == MSO_FILL.BACKGROUND
    fills = [str(bars.points[i].format.fill.fore_color.rgb) for i in range(len(_BRIDGE_STEPS))]
    assert fills == ["0015A8", "66BB6A", "66BB6A", "EF5350", "FFAB40", "0015A8"]
    # The viewer's signed labels, as text; no native label on a bar's length.
    assert plot.has_data_labels is False
    texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
    assert texts == ["\u20ac16.5M", "-\u20ac60k"]
    connectors = [shape for shape in slide.shapes if isinstance(shape, Connector)]
    assert len(connectors) == 2
    assert all(line.line.dash_style == MSO_LINE_DASH_STYLE.DASH for line in connectors)
    _slide_w, _slide_h, emu_per_px, _pt = pptx_writer._slide_geometry("16:9")
    assert connectors[0].begin_y == connectors[0].end_y == Emu(int(337 * emu_per_px))
    assert connectors[0].begin_x == Emu(int(380 * emu_per_px)) and connectors[0].end_x == Emu(int(500 * emu_per_px))
    assert log == [emulation_note(1, "waterfall")]
    assert not any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for shape in slide.shapes)


def test_a_waterfall_step_across_zero_is_two_segments_and_the_legend_names_the_series_only():
    """PowerPoint stacks positive values up from zero and negative ones down
    from it: a fall from +5 to -3 is a bar above the axis and one below it,
    one colour. A legend shown lists the series, never the base or the
    below-zero part."""
    from pptx.oxml.ns import qn

    steps = [
        {"start": 0, "end": 5, "value": 5, "role": "total", "color": "var(--deck-accent)"},
        {"start": 5, "end": -3, "value": -8, "role": "decrease", "color": "var(--deck-error)"},
        {"start": 0, "end": -3, "value": -3, "role": "total", "color": "var(--deck-accent)"},
    ]
    item = _waterfall_item(
        steps,
        ["Opening", "Loss", "Closing"],
        legend={"x": 700, "y": 240, "w": 120, "h": 40},
        step_colors=[_rgba(0, 21, 168), _rgba(239, 83, 80), _rgba(0, 21, 168)],
    )
    item["chart"]["legend"] = True
    item["axis"] = {"min": -5, "max": 10, "step": 5}
    chart = _the_chart(_build([item])).chart
    plot = chart.plots[0]
    assert [s.name for s in plot.series] == ["Base", "ARR", "ARR (below zero)"]
    base, bars, below = (list(s.values) for s in plot.series)
    assert base == [0, 0, 0]
    assert bars == [5, 5, -3]
    assert below == [0, -3, 0]
    entries = chart.legend._element.findall(qn("c:legendEntry"))
    assert [(e.find(qn("c:idx")).get("val"), e.find(qn("c:delete")).get("val")) for e in entries] == [("0", "1"), ("2", "1")]
    # Schema order: the entries sit between the position and the manual layout.
    tags = [child.tag.split("}")[1] for child in chart.legend._element]
    assert tags.index("legendPos") < tags.index("legendEntry") < tags.index("layout")
    # The part below zero wears its step's colour too.
    assert str(plot.series[2].points[1].format.fill.fore_color.rgb) == "EF5350"


def test_hidden_legend_entries_keep_their_schema_place_without_a_legend_position():
    """``c:legend`` is ``legendPos? legendEntry* layout? overlay? spPr? txPr?``:
    with no ``c:legendPos`` the entries open the legend, and each one after
    the entries already there - never after the layout, which PowerPoint would
    repair."""
    from lxml import etree

    legend = etree.Element(pptx_writer._c("legend"))
    etree.SubElement(legend, pptx_writer._c("layout"))
    etree.SubElement(legend, pptx_writer._c("overlay"), val="0")
    pptx_writer._hide_legend_entries(legend, [0, 2])
    tags = [etree.QName(child).localname for child in legend]
    assert tags == ["legendEntry", "legendEntry", "layout", "overlay"]
    idx = [entry.find(pptx_writer._c("idx")).get("val") for entry in legend.findall(pptx_writer._c("legendEntry"))]
    assert idx == ["0", "2"]


def test_a_waterfall_whose_bars_did_not_reach_the_payload_is_left_out_and_logged():
    log: list[str] = []
    slide = _build([_waterfall_item(steps=[], categories=["a", "b"])], log=log)
    assert not any(getattr(shape, "has_chart", False) for shape in slide.shapes)
    assert log == ["Slide 1: a chart could not be read from the rendered page and was left out."]


#: The pyramid, as the figure publishes it: the sizes as given.
_PYRAMID_CATEGORIES = ["Executive", "Director", "Manager", "Lead", "Senior", "Mid", "Entry"]
_PYRAMID_SERIES = [
    {"name": "Contributors", "values": [0, 2, 10, 40, 80, 30, 15], "color": "var(--deck-chart-1)"},
    {"name": "Managers", "values": [5, 20, 18, 6, 0, 0, 0], "color": "var(--deck-chart-2)"},
]


def _butterfly_item(spine: str = "center", **overrides) -> dict:
    """A butterfly's ``chart`` primitive: the payload ``chartExportPayload``
    publishes (a horizontal chart with its spine), the axis and bars its
    ``.deck-chart-facts`` publish, and no legend - the extractor writes a
    butterfly's legend as the viewer's text."""
    chart = {
        "kind": "butterfly",
        "categories": _PYRAMID_CATEGORIES,
        "series": _PYRAMID_SERIES,
        "format": "number",
        "unit": None,
        "stacked": False,
        "horizontal": True,
        "spine": spine,
        "legend": True,
    }
    measured = {
        "legend": None,
        "axis": {"min": -80, "max": 80, "step": 20},
        "bar_spacing": {"width": 55, "gap": 0, "category_gap": 18},
        "inner": {"x": 160, "y": 320, "w": 1600, "h": 560},
    }
    return _chart_item("butterfly", chart=chart, **{**measured, **overrides})


def test_a_butterfly_is_an_editable_clustered_bar_chart_its_left_side_negative_and_written_unsigned():
    """One clustered bar chart, the two sides on one row (overlap 100,
    the gap the viewer drew), the first series stored negative so it runs
    leftwards, and every label and tick written with the chart's own code as
    both its sections - PowerPoint shows 80, never -80; the labels' empty
    zero section leaves a zero unlabelled. The categories read
    top-down, on the left axis; the log says they left the spine."""
    from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_TICK_LABEL_POSITION
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from app.services.chart_export import spine_note

    log: list[str] = []
    slide = _build([_butterfly_item()], log=log)
    chart = _the_chart(slide).chart
    assert chart.chart_type == XL_CHART_TYPE.BAR_CLUSTERED
    plot = chart.plots[0]
    assert [str(c) for c in plot.categories] == _PYRAMID_CATEGORIES
    left, right = plot.series
    assert (left.name, right.name) == ("Contributors", "Managers")
    assert list(left.values) == [0, -2, -10, -40, -80, -30, -15]
    assert list(right.values) == [5, 20, 18, 6, 0, 0, 0]
    assert plot.overlap == 100
    assert plot.gap_width == round(100 * 18 / 55)
    assert plot.vary_by_categories is False
    # Labels and ticks: the sign is never written, nor a zero's label.
    assert plot.has_data_labels
    assert plot.data_labels.number_format == "#,##0;#,##0;"
    assert plot.data_labels.number_format_is_linked is False
    assert plot.data_labels.position == XL_LABEL_POSITION.OUTSIDE_END
    value_axis = chart.value_axis
    assert (value_axis.minimum_scale, value_axis.maximum_scale, value_axis.major_unit) == (-80, 80, 20)
    assert value_axis.tick_labels.number_format == "#,##0;#,##0"
    # The rungs top-down, written on the left axis, the value axis at the bottom.
    category_axis = chart.category_axis
    assert category_axis.reverse_order is True
    assert category_axis.tick_label_position == XL_TICK_LABEL_POSITION.LOW
    assert value_axis._element.find(pptx_writer._c("crosses")).get("val") == "max"
    # The series' colours, never PowerPoint's white fill of a negative bar;
    # no legend of PowerPoint's own (the viewer's is text).
    assert [str(series.format.fill.fore_color.rgb) for series in plot.series] == ["0015A8", "40C4FF"]
    assert [series.invert_if_negative for series in plot.series] == [False, False]
    assert chart.has_legend is False
    assert log == [spine_note(1, "butterfly")]
    assert not any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for shape in slide.shapes)


def test_a_butterfly_s_categories_take_the_plot_s_left_when_the_viewer_wrote_them_on_its_spine():
    """PowerPoint writes a bar chart's categories left of its plot: a
    butterfly whose viewer wrote them on the spine gives them a column of the
    plot's left, as wide as the viewer's estimate of its longest one. With
    spine left the viewer already kept that column: the plot is its grid, and
    the log has nothing to say."""
    from pptx.oxml.ns import qn

    def plot_box(item: dict, log: list[str]) -> tuple[float, float]:
        chart = _the_chart(_build([item], log=log)).chart
        manual = chart._chartSpace.chart.plotArea.find(qn("c:layout")).find(qn("c:manualLayout"))
        return float(manual.find(qn("c:x")).get("val")), float(manual.find(qn("c:w")).get("val"))

    centre_log: list[str] = []
    left_log: list[str] = []
    centre = plot_box(_butterfly_item(), centre_log)
    left = plot_box(_butterfly_item("left"), left_log)
    assert centre[0] > left[0] and centre[1] < left[1]
    tick_px = _CHART_STYLE.get("size") or 22
    column = len("Executive") * tick_px * pptx_writer._TICK_GLYPH_EM + pptx_writer._CATEGORY_AXIS_PADDING_PX
    frame_w = _the_chart(_build([_butterfly_item("left")])).width / pptx_writer._slide_geometry("16:9")[2]
    assert (left[1] - centre[1]) * frame_w == pytest.approx(column, abs=1)
    assert len(centre_log) == 1 and left_log == []


def test_a_butterfly_s_left_labels_stay_off_the_categories_and_a_zero_keeps_none():
    """PowerPoint writes the categories hard against the plot's left edge. A
    left value whose bar ends too near that edge for its label (Senior's 80,
    at the axis' end) is written inside the end of its bar, inked from the
    bar; every other left value keeps the series' label outside its bar - and
    the axis stays the viewer's, both sides to one extent. A zero (Executive's
    on the left) gets no point label of its own: the viewer writes no
    value for it, and even a page that measured one gets none - the series'
    number format writes a zero as nothing, on either side."""
    from pptx.oxml.ns import qn

    def measured_labels(written: bool) -> list[dict]:
        return [
            {"role": "value", "series": 0, "category": category, "text": str(value), "x": 0, "y": 0, "w": 13 * len(str(value)), "h": 22}
            for category, value in enumerate(_PYRAMID_SERIES[0]["values"])
            if value or written
        ]

    for zero_measured in (False, True):
        chart = _the_chart(_build([_butterfly_item(labels=measured_labels(zero_measured))])).chart
        assert (chart.value_axis.minimum_scale, chart.value_axis.maximum_scale) == (-80, 80)
        left = chart.plots[0].series[0]
        points = {int(d_lbl.find(qn("c:idx")).get("val")): d_lbl for d_lbl in left._element.find(qn("c:dLbls")).findall(qn("c:dLbl"))}
        assert sorted(points) == [4], zero_measured
        senior = points[4]
        assert senior.find(qn("c:dLblPos")).get("val") == "inEnd"
        ink = senior.find(f"{qn('c:txPr')}//{qn('a:defRPr')}/{qn('a:solidFill')}/{qn('a:srgbClr')}").get("val")
        assert ink == str(pptx_writer._rgb(pptx_writer._segment_ink((0, 21, 168))))
        assert senior.find(qn("c:layout")) is None
        # It carries the series' number format: the size, never the sign, nothing for a zero.
        assert senior.find(qn("c:numFmt")).get("formatCode") == "#,##0;#,##0;"
        # The series keeps its own labels, outside its bars; the right side the plot's.
        assert left.data_labels.position is not None and left.data_labels.number_format == "#,##0;#,##0;"
        assert chart.plots[0].series[1]._element.find(qn("c:dLbls")) is None
        assert chart.plots[0].data_labels.number_format == "#,##0;#,##0;"


def test_a_butterfly_s_side_that_writes_no_values_keeps_no_label():
    """A series' own show_values off leaves its labels out, as on any bar chart."""
    quiet = [_PYRAMID_SERIES[0], {**_PYRAMID_SERIES[1], "show_values": False}]
    item = _butterfly_item("left")
    item["chart"]["series"] = quiet
    plot = _the_chart(_build([item])).chart.plots[0]
    deleted = plot.series[1]._element.find(pptx_writer._c("dLbls")).find(pptx_writer._c("delete"))
    assert deleted is not None and deleted.get("val") == "1"
    assert plot.series[0]._element.find(pptx_writer._c("dLbls")) is None


def test_the_export_log_rides_the_exported_file_and_the_captures_are_2x(monkeypatch):
    """``export_presentation`` asks the render for primitives at 2x (the only
    pictures left in an editable deck are the visuals with no shape
    equivalent, soft at 1x) and hands the writer's log over as the file's
    ``notes`` - what the endpoint header and the tool result then carry."""
    import asyncio

    from app.services import presentation_export

    seen: dict = {}

    async def _fake_render(presentation_id, user_id, **kwargs):
        seen.update(kwargs)
        return presentation_export.RenderedDeck(
            slide_count=1,
            primitives=[
                _chart_slide(
                    [
                        _chart_item("bar"),
                        {
                            "kind": "image",
                            "id": "x0-1",
                            "x": 400,
                            "y": 300,
                            "w": 300,
                            "h": 200,
                            "opacity": 1,
                            "chart": "gauge",
                        },
                    ]
                )
            ],
            captures={"x0-1": _png()},
            revision=3,
        )

    monkeypatch.setattr(presentation_export, "render_deck", _fake_render)
    exported = asyncio.run(
        presentation_export.export_presentation(
            presentation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            title="Board deck",
            spec={"aspect_ratio": "16:9", "slides": [{"notes": "Say the number."}]},
            export_format="pptx_native",
        )
    )
    assert seen["want_primitives"] is True
    assert seen["image_scale"] == presentation_export.NATIVE_PPTX_IMAGE_SCALE == 2
    assert exported.filename == "board-deck.pptx"
    assert exported.notes == (
        "Slide 1: the gauge chart is a picture of the rendered figure - PowerPoint has no gauge chart.",
    )
    prs = PptxPresentation(io.BytesIO(exported.data))
    assert any(getattr(shape, "has_chart", False) for shape in prs.slides[0].shapes)
    assert prs.slides[0].notes_slide.notes_text_frame.text == "Say the number."


def test_hero_backdrops_are_captured_alone_and_the_writer_keeps_the_text_native():
    """A hero backdrop is a picture the PPTX pastes UNDER editable
    runs, so its screenshot must not carry the text drawn over it. The
    extractor tags the element (`data-export-backdrop` -> `backdrop: true`),
    the capture loop puts the slide in its isolation state for that one shot
    (`EXPORT_ISOLATE_ATTR`, answered by the stylesheet) and restores it, and
    the writer treats the item as any other picture."""
    import asyncio
    from pathlib import Path

    from app.services import presentation_export
    from app.services.presentation_export import (
        _ISOLATE_BACKDROP_JS,
        EXPORT_ISOLATE_ATTR,
        EXTRACT_PRIMITIVES_JS,
        _capture_backdrop,
    )

    # A picture INSIDE a backdrop (the mosaic's SVG) is the backdrop.
    assert 'if (el.closest("[data-export-backdrop]")) item.backdrop = true;' in EXTRACT_PRIMITIVES_JS
    assert EXPORT_ISOLATE_ATTR == "data-export-isolate"
    assert f'slide.setAttribute("{EXPORT_ISOLATE_ATTR}", captureId)' in _ISOLATE_BACKDROP_JS
    assert f'slide.removeAttribute("{EXPORT_ISOLATE_ATTR}")' in _ISOLATE_BACKDROP_JS

    # The stylesheet answers the state: content and chrome hide, the backdrop
    # (and everything inside it - the watermark, the arcs, the panel picture)
    # shows alone; the frontend publishes the attribute on every backdrop.
    presentation_dir = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"
    css = (presentation_dir / "presentation.css").read_text(encoding="utf-8")
    assert f".deck-root .deck-static-slide[{EXPORT_ISOLATE_ATTR}] > .deck-slide > *" in css
    assert f".deck-root .deck-static-slide[{EXPORT_ISOLATE_ATTR}] [data-export-backdrop] *" in css
    backdrop = (presentation_dir / "HeroBackdrop.tsx").read_text(encoding="utf-8")
    assert backdrop.count('data-export-backdrop="true"') == 4  # gradient, the split panel, spotlight, mosaic

    # The capture helper isolates, shoots, restores - even when the shot fails.
    calls: list = []

    class _Locator:
        def __init__(self, fail: bool):
            self.fail = fail

        async def screenshot(self, **kwargs):
            calls.append(("shot", kwargs))
            if self.fail:
                raise RuntimeError("detached")
            return b"png"

    class _Page:
        def __init__(self, fail: bool = False):
            self.fail = fail

        async def evaluate(self, expression, arg=None):
            calls.append(("evaluate", arg))
            return True

        def locator(self, selector):
            calls.append(("locator", selector))
            return _Locator(self.fail)

    from app.services.presentation_export import CAPTURE_TIMEOUT_MS

    assert asyncio.run(_capture_backdrop(_Page(), "x0-3")) == b"png"
    assert calls == [
        ("evaluate", ["x0-3", True, False]),
        ("locator", '[data-export-id="x0-3"]'),
        # Bounded like every capture of the loop: a stalled shot is skipped,
        # never the default 30 s per picture.
        ("shot", {"type": "png", "timeout": CAPTURE_TIMEOUT_MS}),
        ("evaluate", ["x0-3", False, False]),
    ]
    calls.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(_capture_backdrop(_Page(fail=True), "x0-3"))
    assert calls[-1] == ("evaluate", ["x0-3", False, False])
    # A pseudo-element's stand-in is shot alone over a transparent page.
    calls.clear()
    assert asyncio.run(_capture_backdrop(_Page(), "x0-4", alpha=True)) == b"png"
    assert calls[0] == ("evaluate", ["x0-4", True, True])
    assert calls[2] == ("shot", {"type": "png", "timeout": CAPTURE_TIMEOUT_MS, "omit_background": True})
    assert calls[3] == ("evaluate", ["x0-4", False, True])

    # The capture loop routes a tagged item through the helper and every
    # other picture through the plain element screenshot.
    source = Path(presentation_export.__file__).read_text(encoding="utf-8")
    assert 'if item.get("backdrop"):' in source
    assert 'page, capture_id, alpha=bool(item.get("alpha"))' in source

    # A full-slide backdrop picture lands as a picture, the text as runs.
    slide = {
        "index": 0,
        "w": 1920,
        "h": 1080,
        "background": {"r": 255, "g": 255, "b": 255, "a": 1},
        "items": [
            {"kind": "image", "id": "x0-0", "x": 0, "y": 0, "w": 1920, "h": 1080, "opacity": 1, "backdrop": True},
            {
                "kind": "text",
                "x": 100,
                "y": 400,
                "w": 1200,
                "h": 120,
                "opacity": 1,
                "runs": [{"text": "Uncover Threats.", "font": "Geologica", "size": 112, "weight": 500, "italic": False, "color": {"r": 24, "g": 25, "b": 27, "a": 1}, "stroke": None, "underline": False, "mono": False}],
                "align": "left",
                "lineHeight": 118,
                "padding": {"top": 0, "right": 0, "bottom": 0, "left": 0},
                "valign": "top",
                "letterSpacing": 0,
            },
        ],
    }
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from app.services import pptx_writer

    data = pptx_writer.build_native_pptx([slide], captures={"x0-0": _png()}, aspect_ratio="16:9")
    prs = PptxPresentation(io.BytesIO(data))
    shapes = list(prs.slides[0].shapes)
    assert any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for shape in shapes)
    assert any(getattr(shape, "has_text_frame", False) and "Uncover Threats." in shape.text_frame.text for shape in shapes)


_DECORATED_SLIDE = """
<div class="deck-root">
  <div class="deck-static-slide" data-aspect="16:9" style="width:1920px;height:1080px;background:#0a0a0a">
    <div class="deck-slide">
      <div class="deck-hero-backdrop" data-hero="mosaic" data-export-backdrop="true" aria-hidden="true">
        <svg class="deck-hero-mosaic" viewBox="0 0 100 100" style="position:absolute;inset:0;width:100%;height:100%">
          <rect x="80" y="0" width="20" height="20" fill="#e184ff" />
        </svg>
      </div>
      <div class="deck-slide-header"><h2>The attacker already runs agents</h2></div>
      <div class="deck-metrics" data-columns="2">
        <div class="deck-metric" data-tone="error">
          <div class="deck-metric-value">~30</div>
          <div class="deck-metric-label">Organizations targeted</div>
        </div>
      </div>
      <ul class="deck-list" data-style="bullet" data-size="md">
        <li class="deck-icon-text"><span class="deck-icon-text-label deck-list-text">One bullet</span></li>
      </ul>
    </div>
  </div>
</div>
"""


def _deck_stylesheet() -> str:
    """What a bare page needs to lay a deck out as the app does.

    `presentation.css` names design-system primitives (`--deck-accent:
    var(--blue-500)`), which the app's compiled stylesheet defines on `:root`
    before any deck renders - the export captures the real app page. A bare
    page without them paints every deck colour, card fills included, as
    nothing. The library declares them inside Tailwind's `@theme`, a block a
    browser ignores, so the declarations are lifted out of whatever block holds
    them, the dark theme's winning as it does in the app by default - the
    backend twin of `frontend/src/testing/design-tokens.ts`. Skips when the
    frontend is not installed.
    """
    import re
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend"
    tokens = frontend / "node_modules" / "@filigran" / "design-system" / "dist" / "tokens" / "theme.css"
    if not tokens.is_file():
        pytest.skip("the design-system tokens are not installed (frontend/node_modules)")
    # The one token the app adds on top: the accent seam (see index.css).
    app = "--color-accent: var(--color-filigran-brand-primary);"
    declarations = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+);", f"{tokens.read_text(encoding='utf-8')}\n{app}"))
    body = "\n".join(f"  {name}: {value.strip()};" for name, value in declarations.items())
    deck = (frontend / "src" / "components" / "presentation" / "presentation.css").read_text(encoding="utf-8")
    return f":root {{\n{body}\n}}\n{deck}"


def test_pseudo_element_decorations_reach_the_editable_pptx_in_a_real_browser():
    """In Chromium on the real stylesheet: a slide title's gradient rule,
    a card's beam and bloom and a list's bullet are pseudo-elements, so each
    gets a stand-in the extractor writes from its host - the gradients as
    pictures captured alone on a transparent page (the card's light ahead of
    the card's text, the paint order), the bullet as a native shape. The
    stand-ins are hidden: the page looks exactly the same after the walk. A
    picture inside a hero backdrop (the mosaic's SVG) is captured as that
    backdrop, isolated, so the headline is never baked into it."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - the export dependency is always installed
        pytest.skip("playwright is not installed")

    from PIL import Image

    from app.services.presentation_export import _ISOLATE_BACKDROP_JS, EXTRACT_PRIMITIVES_JS

    css = _deck_stylesheet()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"no Chromium for Playwright here: {exc}")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.set_content(_DECORATED_SLIDE)
        page.add_style_tag(content=css)
        before = page.screenshot()
        (slide,) = page.evaluate(EXTRACT_PRIMITIVES_JS)
        assert page.screenshot() == before, "the stand-ins changed what the page shows"
        items = slide["items"]
        texts = [item for item in items if item["kind"] == "text"]
        title = next(item for item in texts if item["runs"][0]["text"].startswith("The attacker"))
        value = next(item for item in texts if item["runs"][0]["text"] == "~30")
        stand_ins = [item for item in items if item.get("alpha")]
        # The title's rule: 96 x 4 along the bottom of the heading (its picture's
        # box is its capture's, rounded out to whole pixels).
        rule = next(item for item in stand_ins if abs(item["w"] - 96) <= 1 and abs(item["h"] - 4) <= 1)
        assert abs(rule["y"] + rule["h"] - (title["y"] + title["h"])) < 1.5
        # The card's light covers the card and comes before its value.
        light = next(item for item in stand_ins if item["w"] > 300)
        assert items.index(light) < items.index(value)
        assert light["backdrop"] is True
        # The bullet is a shape (a round accent dot), before its text.
        bullet_text = next(item for item in texts if item["runs"][0]["text"] == "One bullet")
        bullet = next(
            item for item in items if item["kind"] == "rect" and round(item["w"]) == round(item["h"]) and item["w"] < 20
        )
        assert items.index(bullet) < items.index(bullet_text) and bullet["fill"] is not None
        # The mosaic's SVG is the backdrop's picture, isolated.
        mosaic = next(item for item in items if item["kind"] == "image" and round(item["w"]) == 1920)
        assert mosaic["backdrop"] is True and not mosaic.get("alpha")
        # One text box per text: nothing is written twice.
        assert sorted(item["runs"][0]["text"] for item in texts) == [
            "One bullet",
            "Organizations targeted",
            "The attacker already runs agents",
            "~30",
        ]
        # The alpha capture is the rule alone: transparent around it, painted in it.
        page.evaluate(_ISOLATE_BACKDROP_JS, [rule["id"], True, True])
        png = page.locator(f'[data-export-id="{rule["id"]}"]').screenshot(omit_background=True)
        page.evaluate(_ISOLATE_BACKDROP_JS, [rule["id"], False, True])
        assert page.screenshot() == before, "the capture state was not restored"
        picture = Image.open(io.BytesIO(png)).convert("RGBA")
        alphas = [picture.getpixel((x, picture.height // 2))[3] for x in range(picture.width)]
        assert max(alphas) == 255, "the rule is painted"
        # Its rounded ends let the page through: no paper is baked in.
        assert picture.getpixel((0, 0))[3] < 255
        browser.close()


# The `challenge_answer` block as `blocks/challenge.tsx` renders it, pushed down
# by a fractional spacer so the cards' edges fall between two pixels.
_CONNECTOR_SLIDE = """
<div class="deck-root">
  <div class="deck-static-slide" data-aspect="16:9" style="width:1920px;height:1080px;background:#0a0a0a">
    <div class="deck-slide">
      <div style="height:100.4px"></div>
      <div class="deck-challenge">
        <div class="deck-challenge-card" data-role="challenge" data-tone="error">
          <div class="deck-challenge-label"><span>Challenge</span></div>
          <p class="deck-challenge-text">Every incident is correlated by hand.</p>
        </div>
        <svg class="deck-challenge-arrow" aria-hidden="true" data-export-alone="true" width="32" height="66"
             viewBox="0 0 24 49.5">
          <g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="0" x2="12" y2="48.625" stroke-linecap="butt" />
            <polyline points="5,41.625 12,48.625 19,41.625" />
          </g>
        </svg>
        <div class="deck-challenge-card" data-role="answer" data-tone="success">
          <div class="deck-challenge-label"><span>Answer</span></div>
          <p class="deck-challenge-text">One case model for every source.</p>
        </div>
      </div>
    </div>
  </div>
</div>
"""


def test_a_challenge_connector_reaches_the_editable_pptx_alone_and_where_the_page_drew_it():
    """In Chromium on the real stylesheet: the `challenge_answer`
    connector runs from one card's edge to the next, so its picture is captured
    alone on a transparent page (`data-export-alone`) - a crop of the page
    would carry the cards' borders - and, like every picture, takes the box its
    screenshot covers (the element's rounded out to whole pixels), so the
    writer lays it where the page painted it."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - the export dependency is always installed
        pytest.skip("playwright is not installed")
    import math

    from PIL import Image

    from app.services.presentation_export import _ISOLATE_BACKDROP_JS, EXTRACT_PRIMITIVES_JS

    css = _deck_stylesheet()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"no Chromium for Playwright here: {exc}")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.set_content(_CONNECTOR_SLIDE)
        page.add_style_tag(content=css)
        before = page.screenshot()
        boxes = page.evaluate(
            """() => {
              const base = document.querySelector(".deck-static-slide").getBoundingClientRect();
              const rel = (el) => {
                const r = el.getBoundingClientRect();
                return { top: r.top - base.top, bottom: r.bottom - base.top, left: r.left - base.left, right: r.right - base.left };
              };
              const [challenge, answer] = document.querySelectorAll(".deck-challenge-card");
              return { challenge: rel(challenge), arrow: rel(document.querySelector(".deck-challenge-arrow")), answer: rel(answer) };
            }"""
        )
        (slide,) = page.evaluate(EXTRACT_PRIMITIVES_JS)
        challenge, arrow, answer = boxes["challenge"], boxes["arrow"], boxes["answer"]
        # The connector fills the gap, and the gap's edges are fractional.
        assert arrow["top"] == challenge["bottom"] and arrow["bottom"] == answer["top"]
        assert arrow["top"] % 1 and arrow["bottom"] - arrow["top"] == 66

        # Between the cards: the connector's one picture, never a line shape.
        between = [
            item
            for item in slide["items"]
            if item["y"] > challenge["bottom"] - 2 and item["y"] + item["h"] < answer["top"] + 2 and item["w"] < 100
        ]
        assert [item["kind"] for item in between] == ["image"]
        (picture_item,) = between
        assert picture_item.get("backdrop") is True and picture_item.get("alpha") is True, picture_item
        # Its box is its capture's: the element's rounded out to whole pixels.
        assert (picture_item["y"], picture_item["y"] + picture_item["h"]) == (
            math.floor(arrow["top"]),
            math.ceil(arrow["bottom"]),
        )
        assert (picture_item["x"], picture_item["x"] + picture_item["w"]) == (
            math.floor(arrow["left"]),
            math.ceil(arrow["right"]),
        )
        assert all(
            float(item[key]).is_integer() for item in slide["items"] if item["kind"] == "image" for key in "xywh"
        )

        # The alpha capture is the connector alone: no card border in its edge
        # rows, the page through around the stroke, one stroke width all along.
        page.evaluate(_ISOLATE_BACKDROP_JS, [picture_item["id"], True, True])
        # Hidden by the isolation (no rule showing it), the shot never comes: fail fast.
        png = page.locator(f'[data-export-id="{picture_item["id"]}"]').screenshot(omit_background=True, timeout=5_000)
        page.evaluate(_ISOLATE_BACKDROP_JS, [picture_item["id"], False, True])
        assert page.screenshot() == before, "the capture state was not restored"
        picture = Image.open(io.BytesIO(png)).convert("RGBA")
        assert picture.size == (picture_item["w"], picture_item["h"])
        width, height = picture.size
        for y in (0, height - 1):
            painted = [x for x in range(width) if picture.getpixel((x, y))[3] > 0]
            assert all(abs(x + 0.5 - width / 2) <= 3 for x in painted), "a card border is baked in"
        corners = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
        assert all(picture.getpixel(corner)[3] == 0 for corner in corners)
        # 1.75 on the icon's 24-unit grid at 32 px: 2.33 canvas px, every row.
        shaft = [sum(picture.getpixel((x, y))[3] for x in range(width)) / 255 for y in range(2, height - 16)]
        assert all(abs(row - 1.75 * 32 / 24) < 0.05 for row in shaft), "the shaft is one stroke width from the top down"
        browser.close()


_FLOW_ARROW = (
    '<span class="deck-flow-arrow" aria-hidden="true"><svg class="deck-icon" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="1.75"><path d="M5 12h14M13 6l6 6-6 6"/></svg></span>'
)


def _flow_under_columns_slide(titles: tuple[str, ...], *, loop: bool) -> str:
    """A `columns` block of as many columns as the flow has steps, over a
    horizontal `process_flow` - the DOM `ColumnsBlockView` and
    `ProcessFlowBlockView` render, on the static canvas."""
    columns = "".join(
        f'<div class="deck-column"><div class="deck-blocks"><div class="deck-card">'
        f'<div class="deck-card-title">Column {index + 1}</div></div></div></div>'
        for index in range(len(titles))
    )
    steps = "".join(
        '<li class="deck-flow-step" data-tone="accent">'
        f'<div class="deck-flow-card"><div class="deck-flow-title">{title}</div></div>'
        + (_FLOW_ARROW if index < len(titles) - 1 or loop else "")
        + "</li>"
        for index, title in enumerate(titles)
    )
    return f"""
<div class="deck-root" data-deck-theme="dark">
  <div class="deck-canvas deck-static-slide" data-aspect="16:9" data-scheme="dark" style="width:1920px;height:1080px;background:#0a0a0a">
    <div class="deck-slide">
      <div class="deck-slide-body"><div class="deck-blocks deck-blocks-slide">
        <div class="deck-columns" data-count="{len(titles)}" data-ratio="equal" data-valign="start" data-gap="md">{columns}</div>
        <ol class="deck-flow" data-direction="horizontal"{' data-loop="true"' if loop else ""}>{steps}</ol>
      </div></div>
    </div>
  </div>
</div>
"""


@pytest.mark.parametrize(
    ("titles", "loop"),
    [
        (("Detect", "Prioritize", "Validate"), False),
        (("Detect", "Prioritize", "Validate", "Mobilize"), False),
        (("Detect", "Triage", "Respond"), True),
    ],
    ids=["three", "four", "loop"],
)
def test_a_horizontal_flow_exports_equal_cards_on_the_columns_grid_with_its_arrows_in_the_gutters(
    titles: tuple[str, ...], loop: bool
):
    """In Chromium on the real stylesheet: the extractor reads the
    rendered boxes, so what it writes is what the stylesheet lays out - every
    step card of a horizontal flow one width, the last included, each on the
    edges of the column above it (the `columns` block's `md` gap is the flow's
    gutter), and every arrow a picture centred in the gutter between two
    cards. A loop's return arrow sits in one trailing gutter and its cards,
    still equal, are each a gutter's share narrower than the columns."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - the export dependency is always installed
        pytest.skip("playwright is not installed")

    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    css = _deck_stylesheet()
    gap = 48  # --deck-columns-gap = --deck-space-5
    count = len(titles)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"no Chromium for Playwright here: {exc}")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.set_content(_flow_under_columns_slide(titles, loop=loop))
        page.add_style_tag(content=css)
        (slide,) = page.evaluate(EXTRACT_PRIMITIVES_JS)
        browser.close()
    items = slide["items"]
    texts = [item for item in items if item["kind"] == "text"]
    rects = [item for item in items if item["kind"] == "rect" and item["w"] > 100]
    # The painted boxes, by row: the column cards above, the step cards below.
    column_cards = sorted(rects[:count], key=lambda item: item["x"])
    step_cards = sorted(rects[count:], key=lambda item: item["x"])
    assert len(column_cards) == count and len(step_cards) == count, [round(item["w"]) for item in rects]
    assert all(card["y"] > column_cards[0]["y"] + column_cards[0]["h"] for card in step_cards)
    # Every step the same width, the last one included.
    widths = [card["w"] for card in step_cards]
    assert max(widths) - min(widths) < 1, widths
    # The flow spans the block edge to edge: first card on the left edge, last card (or the
    # return arrow) on the right edge of the columns block.
    assert abs(step_cards[0]["x"] - column_cards[0]["x"]) < 1
    right_edge = column_cards[-1]["x"] + column_cards[-1]["w"]
    # The pictures that are the arrows' icons - not the cards' light, a pseudo-element stand-in
    # captured as an alpha picture.
    arrows = [item for item in items if item["kind"] == "image" and not item.get("alpha")]
    assert len(arrows) == (count if loop else count - 1), arrows
    if loop:
        trailing = arrows[-1]
        assert abs((trailing["x"] + trailing["w"] / 2) - (right_edge - gap / 2)) < 1
        assert abs((step_cards[-1]["x"] + step_cards[-1]["w"]) - (right_edge - gap)) < 1
        # Each card is one gutter's share narrower than its column.
        assert abs(step_cards[0]["w"] - (column_cards[0]["w"] - gap / count)) < 1
    else:
        assert abs((step_cards[-1]["x"] + step_cards[-1]["w"]) - right_edge) < 1
        # Each card on the edges of the column above it: the pitch is the columns grid's.
        for card, column in zip(step_cards, column_cards, strict=True):
            assert abs(card["x"] - column["x"]) < 1 and abs(card["w"] - column["w"]) < 1, (card, column)
    # Every arrow is a picture centred in the gutter after its card, inside the card's height.
    for index, arrow in enumerate(arrows):
        card = step_cards[index]
        gutter_centre = card["x"] + card["w"] + gap / 2
        assert abs((arrow["x"] + arrow["w"] / 2) - gutter_centre) < 1, (arrow, gutter_centre)
        assert arrow["w"] <= gap
        assert card["y"] <= arrow["y"] and arrow["y"] + arrow["h"] <= card["y"] + card["h"]
    # Each title is written once, inside its own card.
    for card, title in zip(step_cards, titles, strict=True):
        (text,) = [item for item in texts if item["runs"][0]["text"] == title]
        assert card["x"] <= text["x"] and text["x"] + text["w"] <= card["x"] + card["w"] + 1


def test_the_extractor_and_the_figure_agree_on_the_chart_contract():
    """One contract, two sides: the figure publishes the chart as data on
    ``data-deck-chart`` and its laid-out facts on ``.deck-chart-facts``; the
    extractor reads exactly those, knows the native kinds from
    ``chart_export`` (injected, never a second list), measures the legend
    without the target's entry, the inner plot off the grid or the pie
    sectors, the target line with its stroke, and the values the viewer wrote."""
    import json
    from pathlib import Path

    from app.services.chart_export import (
        MIRRORED_CHART_KINDS,
        NATIVE_CHART_KINDS,
        TWO_AXIS_BARS_REASON,
        TWO_AXIS_PICTURE_SERIES_KINDS,
    )
    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    assert f"new Set({json.dumps(sorted(NATIVE_CHART_KINDS))})" in EXTRACT_PRIMITIVES_JS
    assert f"const MIRRORED_CHARTS = new Set({json.dumps(sorted(MIRRORED_CHART_KINDS))});" in EXTRACT_PRIMITIVES_JS
    assert f"new Set({json.dumps(sorted(TWO_AXIS_PICTURE_SERIES_KINDS))})" in EXTRACT_PRIMITIVES_JS
    assert f"return pictured ? {json.dumps(TWO_AXIS_BARS_REASON)} : null;" in EXTRACT_PRIMITIVES_JS
    for placeholder in (
        "__NATIVE_CHART_KINDS__",
        "__MIRRORED_CHART_KINDS__",
        "__TWO_AXIS_PICTURE_SERIES_KINDS__",
        "__TWO_AXIS_BARS_REASON__",
    ):
        assert placeholder not in EXTRACT_PRIMITIVES_JS
    # A butterfly's legend - each entry over its side, the side totals
    # under them - is the viewer's text, and its chart gets no legend box.
    assert "const ownLegend = MIRRORED_CHARTS.has(chart.kind) ? null : legend;" in EXTRACT_PRIMITIVES_JS
    assert "if (legend && !ownLegend) visit(legend);" in EXTRACT_PRIMITIVES_JS
    for needle in (
        "figure.dataset.deckChart",
        'querySelector(".deck-chart-facts")',
        "facts.dataset.min",
        "facts.dataset.secondaryMin",
        'facts.dataset.values === "true"',
        'querySelector(".deck-chart-canvas")',
        '".deck-chart-legend-item:not(.deck-chart-legend-target)"',
        "legend_rows: legendRows",
        'querySelector(".recharts-cartesian-grid")',
        'querySelectorAll(".recharts-pie-sector")',
        """querySelector('.deck-chart-target:not([data-value-axis="secondary"])')""",
        """querySelector('.deck-chart-target[data-value-axis="secondary"]')""",
        "target_line: target ? strokeBox(target) : null",
        "secondary_target_line: secondaryTarget ? strokeBox(secondaryTarget) : null",
        'querySelector(".deck-chart-tick-category")',
        '".deck-chart-tick-primary"',
        '".deck-chart-tick-secondary"',
        'querySelectorAll(".deck-chart-value")',
        'entry.axis === "secondary" && TWO_AXIS_PICTURE_SERIES.has(entry.kind || chart.kind)',
        "picture.chart = chart.kind",
        # A waterfall's bars: one colour per step, and the connectors between them.
        "step_colors: (chart.steps || []).map((step) => resolveColor(canvas, step.color))",
        'canvas.querySelectorAll(".deck-chart-connector")).map(strokeBox)',
        'connector: paintOf(canvas, ".deck-chart-connector", "stroke")',
        "picture.chart_reason = reason",
        # A thin segment's value past its stack: its leader's legs, its
        # size, and a values' paint that is neither a segment's nor its.
        'canvas.querySelectorAll(".deck-chart-leader"))',
        "({ ...strokeBox(el), series: Number(el.dataset.series) })",
        'annotation_size: factPx("annotationSize")',
        """'.deck-chart-value:not([data-role="segment"], [data-role="annotation"])'""",
    ):
        assert needle in EXTRACT_PRIMITIVES_JS, needle

    blocks = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation" / "blocks"
    figure = (blocks / "chart.tsx").read_text(encoding="utf-8")
    canvas = (blocks / "ChartCanvas.tsx").read_text(encoding="utf-8")
    fmt = (blocks / "chart-format.ts").read_text(encoding="utf-8")
    assert "data-deck-chart={exportPayload}" in figure
    assert 'className="deck-chart-canvas"' in figure
    assert 'className="deck-chart-legend-item deck-chart-legend-target"' in figure
    assert 'className="deck-chart-legend-name"' in figure
    assert 'className="deck-chart-facts"' in canvas
    assert 'data-values={values.outer ? "true" : "false"}' in canvas
    assert '"data-min": axis.min, "data-max": axis.max, "data-step": axis.step' in canvas
    # The right-hand axis of a two-unit chart: its facts, its target
    # line and the class every axis' tick labels carry.
    assert '"data-secondary-min": secondaryAxis.min' in canvas
    assert 'data-value-axis="secondary"' in canvas
    for tick in ("deck-chart-tick-category", "deck-chart-tick-primary", "deck-chart-tick-secondary"):
        assert f'className: "{tick}"' in canvas, tick
    assert 'className="deck-chart-value"' in canvas
    assert 'className="deck-chart-center"' in canvas
    assert 'className="deck-chart-connector"' in canvas
    # A thin segment's value past its stack and its leader.
    assert 'className="deck-chart-leader"' in canvas
    assert 'role="annotation"' in canvas
    assert '"data-annotation-size": values.annotation.size' in canvas
    # How the values are set: the writer sizes and turns PowerPoint's
    # labels from these, and puts segment labels where the viewer wrote them.
    for needle in (
        '{ "data-label-size": values.outer.size }',
        '{ "data-label-rotate": "true" }',
        # A line's values alternating over and under their points: the writer labels them point by point.
        '{ "data-values-alternate": "true" }',
        # Values past the bars fitted: the plan, each value's lift, the
        # bars they were fitted around - that gave way, or a tight group.
        '{ "data-values-fit": values.fit }',
        "data-lift={lift ? round2(lift) : undefined}",
        '"data-bar-width": round2(barSpacing.width)',
        '"data-bar-gap": round2(barSpacing.gap)',
        '"data-category-gap": round2(barSpacing.categoryGap)',
        "barSpacing={layout.barSpacing}",
        '{ "data-segment-size": values.inner.size }',
        "data-role={role}",
        "data-series={series}",
        "data-category={category}",
        'data-rotate={rotate ? "true" : undefined}',
    ):
        assert needle in canvas, needle
    for needle in (
        'factPx("labelSize")',
        'facts.dataset.labelRotate === "true"',
        'values_alternate: !!facts && facts.dataset.valuesAlternate === "true"',
        "values_fit: facts && facts.dataset.valuesFit ? facts.dataset.valuesFit : null",
        "width: parseFloat(facts.dataset.barWidth), gap: parseFloat(facts.dataset.barGap), "
        "category_gap: parseFloat(facts.dataset.categoryGap)",
        "lift: el.dataset.lift !== undefined ? Number(el.dataset.lift) * k : null",
        'factPx("segmentSize")',
        "role: el.dataset.role || null",
        "series: el.dataset.series !== undefined ? Number(el.dataset.series) : null",
        "category: el.dataset.category !== undefined ? Number(el.dataset.category) : null",
        'rotate: el.dataset.rotate === "true"',
        # The end of the text the viewer pinned: the writer pins its box the same way.
        'anchor: el.getAttribute("text-anchor") || null',
    ):
        assert needle in EXTRACT_PRIMITIVES_JS, needle
    assert "textAnchor={anchor}" in canvas
    # The payload keys the writer reads.
    for key in (
        "kind:",
        "categories:",
        "series:",
        "steps:",
        "start: step.start",
        "end: step.end",
        "color: waterfallColor(step.role, tone)",
        "slice_colors:",
        "format:",
        "unit:",
        "stacked:",
        "horizontal:",
        "legend:",
        "values:",
        "target:",
        "target_name:",
        "secondary_format:",
        "secondary_unit:",
        "secondary_target:",
        "secondary_target_name:",
        'label_size: block.label_size ?? "md"',
        # The values' own size, only where the chart names one it acts on.
        "...(valueSize !== undefined ? { value_label_size: valueSize } : {})",
    ):
        assert key in fmt, key
    assert '{ axis: "secondary" }' in fmt
    # The legend's own size, which the writer gives PowerPoint's legend.
    assert "legend_size: legendName ? px(getComputedStyle(legendName).fontSize) * k : null" in EXTRACT_PRIMITIVES_JS


def _app(session: _FakeSession) -> TestClient:
    from app.api.v1 import presentations as routes
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/presentations")

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_render_routes_require_a_render_token():
    client = _app(_FakeSession())
    deck_id = uuid.uuid4()
    assert client.get(f"/api/v1/presentations/{deck_id}/render-spec").status_code == 401
    assert client.get(f"/api/v1/presentations/{deck_id}/render-asset/{uuid.uuid4()}").status_code == 401
    # The poll snapshot the export draws (#10157) is token-gated the same way.
    assert client.get(f"/api/v1/presentations/{deck_id}/render-polls").status_code == 401


def test_render_polls_refuses_a_token_for_another_deck_before_any_lookup():
    session = _FakeSession()
    client = _app(session)
    token = mint_render_token(uuid.uuid4(), uuid.uuid4())
    resp = client.get(f"/api/v1/presentations/{uuid.uuid4()}/render-polls", params={"render_token": token})
    assert resp.status_code == 403
    assert session.statements == []


def test_render_routes_refuse_a_session_jwt():
    """A session must not drive the render routes: the two credential kinds are
    disjoint in both directions."""
    client = _app(_FakeSession())
    deck_id = uuid.uuid4()
    session_token = create_access_token(str(uuid.uuid4()))
    resp = client.get(
        f"/api/v1/presentations/{deck_id}/render-spec",
        params={"render_token": session_token},
    )
    assert resp.status_code == 401
    resp = client.get(
        f"/api/v1/presentations/{deck_id}/render-spec",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 401


def test_render_token_for_another_deck_is_refused_before_any_lookup():
    session = _FakeSession()
    client = _app(session)
    token = mint_render_token(uuid.uuid4(), uuid.uuid4())
    resp = client.get(
        f"/api/v1/presentations/{uuid.uuid4()}/render-spec",
        params={"render_token": token},
    )
    assert resp.status_code == 403
    assert session.statements == []


def test_render_spec_serves_the_deck_to_a_valid_token():
    user_id = uuid.uuid4()
    deck = _deck_row(user_id)
    session = _FakeSession([_Result(_user(user_id)), *_deck_lookup(deck)])
    client = _app(session)
    resp = client.get(
        f"/api/v1/presentations/{deck.id}/render-spec",
        params={"render_token": mint_render_token(deck.id, user_id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(deck.id)
    assert body["spec"]["slides"][0]["title"] == "Q3"
    assert body["theme_config"] == {}
    assert body["permission"] == "owner"
    assert body["can_manage"] is True


def test_render_spec_still_applies_the_visibility_gate():
    """The token names a user; that user must be allowed to read the deck. In
    production the visibility predicate filters the row out in SQL (404); the
    fake ignores WHERE, so this exercises the fail-closed belt behind it."""
    deck = _deck_row(uuid.uuid4())  # owned by someone else
    stranger = uuid.uuid4()
    session = _FakeSession([_Result(_user(stranger)), *_deck_lookup(deck)])
    client = _app(session)
    resp = client.get(
        f"/api/v1/presentations/{deck.id}/render-spec",
        params={"render_token": mint_render_token(deck.id, stranger)},
    )
    assert resp.status_code == 403
    # ...and the row simply not being visible is a 404.
    session = _FakeSession([_Result(_user(stranger)), *_deck_lookup(None)])
    resp = _app(session).get(
        f"/api/v1/presentations/{deck.id}/render-spec",
        params={"render_token": mint_render_token(deck.id, stranger)},
    )
    assert resp.status_code == 404


def test_render_spec_serves_a_deck_shared_read_only_with_the_token_user():
    owner, reader = uuid.uuid4(), uuid.uuid4()
    deck = _deck_row(owner, shared_user_ids=[str(reader)], shared_user_permissions={str(reader): "read_only"})
    session = _FakeSession([_Result(_user(reader)), *_deck_lookup(deck)])
    resp = _app(session).get(
        f"/api/v1/presentations/{deck.id}/render-spec",
        params={"render_token": mint_render_token(deck.id, reader)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["permission"] == "read_only"
    assert resp.json()["can_manage"] is False


def test_render_spec_carries_the_template_footer_pattern_the_export_draws():
    """The static render page draws the footer from ``template_chrome``,
    so a PDF / PPTX export of a branded deck shows the template's footer only
    if the render-token route serves the pattern exactly like the session
    route does - both build their payload through ``_response``, which
    resolves the template as the TOKEN user may see it. A template that user
    can no longer see contributes nothing, and the export then draws the free
    footer, consistently with what the same user reads in the app."""
    owner = uuid.uuid4()
    template_id = uuid.uuid4()
    deck = _deck_row(owner, template_container_id=template_id)
    deck.spec["chrome"] = {"event_label": "FIC 2026", "fields": {"classification": "Confidential"}}
    template = SimpleNamespace(
        id=template_id,
        name="Filigran Brand Deck",
        kind="presentation",
        theme_config={"accent": "#42caff"},
        extra_metadata={
            "chrome": {
                "footer": "{{event}} - {{title}} - {{classification}}",
                "variables": [{"key": "classification", "label": "Classification"}],
            }
        },
    )

    def _session(visible_template):
        # The token user, the deck (group ids + row), the author names, then
        # the template as that user may see it (group ids + row); the logo
        # roles and the open-comment count fall through to empty results.
        return _FakeSession(
            [_Result(_user(owner)), *_deck_lookup(deck), _Result(), _Result(rows=[]), _Result(visible_template)]
        )

    resp = _app(_session(template)).get(
        f"/api/v1/presentations/{deck.id}/render-spec",
        params={"render_token": mint_render_token(deck.id, owner)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_chrome"]["footer"] == "{{event}} - {{title}} - {{classification}}"
    assert [v["key"] for v in body["template_chrome"]["variables"]] == ["classification"]
    assert body["template_name"] == "Filigran Brand Deck"
    assert body["spec"]["chrome"]["fields"] == {"classification": "Confidential"}

    body = (
        _app(_session(None))
        .get(
            f"/api/v1/presentations/{deck.id}/render-spec",
            params={"render_token": mint_render_token(deck.id, owner)},
        )
        .json()
    )
    assert "template_chrome" in body and body["template_chrome"] is None
    assert body["template_name"] is None


def test_render_asset_only_serves_files_the_deck_references():
    user_id = uuid.uuid4()
    deck = _deck_row(user_id)
    session = _FakeSession([_Result(_user(user_id)), *_deck_lookup(deck)])
    client = _app(session)
    resp = client.get(
        f"/api/v1/presentations/{deck.id}/render-asset/{uuid.uuid4()}",
        params={"render_token": mint_render_token(deck.id, user_id)},
    )
    assert resp.status_code == 404
    # Refused on the deck's own reference set: no FileAttachment lookup happened
    # (user + group memberships + deck = 3 statements).
    assert len(session.statements) == 3


def test_render_asset_serves_a_readers_picture_through_the_deck_it_renders(monkeypatch):
    """The export of a SHARED deck runs as the exporting reader, who does
    not own the pictures the author's agent generated. The route hands the deck
    to ``authorize_file_access`` as the hint, so the grant is the hinted deck's
    own visibility looked up by primary key - no text scan per picture."""
    from sqlalchemy.dialects import postgresql

    from app.services import file_storage as storage

    owner, reader = uuid.uuid4(), uuid.uuid4()
    picture = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner,
        conversation_id=uuid.uuid4(),
        assignment_run_id=None,
        template_container_id=None,
        source="chat",
        s3_object_name="generated/picture.png",
        content_type="image/png",
    )
    deck = _deck_row(
        owner,
        shared_user_ids=[str(reader)],
        shared_user_permissions={str(reader): "read_only"},
        spec={
            "title": "Quarterly review",
            "theme": "filigran",
            "aspect_ratio": "16:9",
            "transition": "fade",
            "slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": str(picture.id)}]}],
        },
    )

    async def _download(object_name):
        assert object_name == picture.s3_object_name
        return _png()

    monkeypatch.setattr(storage.file_storage, "download", _download)
    session = _FakeSession(
        [
            _Result(_user(reader)),
            *_deck_lookup(deck),
            _Result(picture),  # the FileAttachment row
            _Result(rows=[]),  # the reader's group memberships, for the contract's own predicate
            _Result(deck.spec),  # the hinted deck, by primary key
        ]
    )
    resp = _app(session).get(
        f"/api/v1/presentations/{deck.id}/render-asset/{picture.id}",
        params={"render_token": mint_render_token(deck.id, reader)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    grant = str(session.statements[-1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert f"presentations.id = '{deck.id}'" in grant
    assert f"'{reader}'" in grant and "shared_user_ids" in grant, "the deck's own visibility predicate"
    assert "ILIKE" not in grant, "the hinted deck answers without the scan"


@pytest.mark.parametrize(
    ("content_type", "filename", "inline"),
    [("image/png", "figure.png", True), ("image/svg+xml", "figure.svg", False)],
    ids=["raster", "svg"],
)
def test_render_asset_answers_through_the_file_contract_and_is_never_stored(
    monkeypatch, content_type, filename, inline
):
    """The render-token fetch answers through ``file_responses`` like every
    FileAttachment byte route - ``nosniff``, raster images inline, anything else
    (an SVG is a document when navigated to) a download - and nothing may keep
    it: the token rides in the query string and an export reads the bytes as they
    are now, so it is ``private, no-store`` with no validator to revalidate."""
    from app.services import file_storage as storage

    owner = uuid.uuid4()
    picture = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner,
        conversation_id=uuid.uuid4(),
        assignment_run_id=None,
        template_container_id=None,
        source="chat",
        filename=filename,
        s3_object_name=f"generated/{filename}",
        content_type=content_type,
    )
    deck = _deck_row(
        owner,
        spec={
            "title": "Quarterly review",
            "theme": "dark",
            "aspect_ratio": "16:9",
            "transition": "fade",
            "slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": str(picture.id)}]}],
        },
    )

    async def _download(object_name):
        assert object_name == picture.s3_object_name
        return _png()

    monkeypatch.setattr(storage.file_storage, "download", _download)
    session = _FakeSession([_Result(_user(owner)), *_deck_lookup(deck), _Result(picture)])
    resp = _app(session).get(
        f"/api/v1/presentations/{deck.id}/render-asset/{picture.id}",
        params={"render_token": mint_render_token(deck.id, owner)},
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "etag" not in resp.headers
    disposition = resp.headers["content-disposition"]
    if inline:
        assert disposition == "inline"
    else:
        assert disposition.startswith("attachment;") and filename in disposition


def test_the_session_surface_refuses_a_render_token():
    """``get_current_user`` must never accept the confined token (it would turn
    a leaked render token into a full API session)."""
    from fastapi import Depends

    from app.api.deps import get_current_user
    from app.core.database import get_db

    deps_app = FastAPI()

    @deps_app.get("/whoami")
    async def whoami(user=Depends(get_current_user)):
        return {"id": str(user.id)}

    async def _db():
        yield _FakeSession()

    deps_app.dependency_overrides[get_db] = _db
    token = mint_render_token(uuid.uuid4(), uuid.uuid4())
    resp = TestClient(deps_app).get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_export_endpoint_validates_the_format_before_rendering():
    user_id = uuid.uuid4()
    from app.api.deps import get_current_user
    from app.api.v1 import presentations as routes
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/presentations")

    async def _db():
        yield _FakeSession()

    async def _user_dep():
        return _user(user_id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user_dep
    resp = TestClient(app).post(f"/api/v1/presentations/{uuid.uuid4()}/export", json={"format": "docx"})
    assert resp.status_code == 400
    assert "pdf" in resp.json()["detail"]


def test_export_endpoint_streams_the_rendered_file(monkeypatch):
    user_id = uuid.uuid4()
    deck = _deck_row(user_id)
    from app.api.deps import get_current_user
    from app.api.v1 import presentations as routes
    from app.core.database import get_db
    from app.services.presentation_export import ExportedFile

    seen: dict = {}

    async def _fake_export(**kwargs):
        seen.update(kwargs)
        return ExportedFile("quarterly-review.pdf", "application/pdf", b"%PDF-1.7 fake")

    monkeypatch.setattr(routes, "export_presentation", _fake_export)

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/presentations")

    async def _db():
        yield _FakeSession(_deck_lookup(deck))

    async def _user_dep():
        return _user(user_id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user_dep
    resp = TestClient(app).post(f"/api/v1/presentations/{deck.id}/export", json={"format": "PDF"})
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.7 fake"
    assert resp.headers["content-type"].startswith("application/pdf")
    assert 'filename="quarterly-review.pdf"' in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "private, no-store"
    assert seen["export_format"] == "pdf"
    assert seen["user_id"] == user_id
    assert seen["presentation_id"] == deck.id


def test_export_endpoint_maps_render_failures_to_gateway_errors(monkeypatch):
    user_id = uuid.uuid4()
    deck = _deck_row(user_id)
    from app.api.deps import get_current_user
    from app.api.v1 import presentations as routes
    from app.core.database import get_db
    from app.services.presentation_export import PresentationRenderError

    async def _fails(**kwargs):
        raise PresentationRenderError("The deck did not finish rendering in time.")

    monkeypatch.setattr(routes, "export_presentation", _fails)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/presentations")

    async def _db():
        yield _FakeSession(_deck_lookup(deck))

    async def _user_dep():
        return _user(user_id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user_dep
    resp = TestClient(app).post(f"/api/v1/presentations/{deck.id}/export", json={})
    assert resp.status_code == 502
    assert "did not finish" in resp.json()["detail"]


def test_export_endpoint_carries_the_export_log_in_a_header(monkeypatch):
    """The file is the body, so the log (a gauge pasted as a picture)
    rides ``X-Export-Notes`` as a JSON list the export menu reads into a
    toast; a faithful export sends no header at all, so the client has one
    presence check, not an empty-list one. The header is ASCII-safe JSON:
    the notes are plain sentences and ``json.dumps`` escapes anything else."""
    import json

    user_id = uuid.uuid4()
    deck = _deck_row(user_id)
    from app.api.deps import get_current_user
    from app.api.v1 import presentations as routes
    from app.core.database import get_db
    from app.services.presentation_export import ExportedFile

    notes = (
        "Slide 8: the gauge chart is a picture of the rendered figure - PowerPoint has no gauge chart.",
        "Slide 9: the bar chart is a picture of the rendered figure - PowerPoint draws the bars of two value axes over each other.",
    )
    outcome: dict = {"notes": notes}

    async def _fake_export(**kwargs):
        return ExportedFile(
            "quarterly-review.pptx",
            EXPORT_CONTENT_TYPES["pptx_native"],
            b"PK\x03\x04 fake",
            notes=outcome["notes"],
        )

    monkeypatch.setattr(routes, "export_presentation", _fake_export)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/presentations")

    async def _db():
        yield _FakeSession(_deck_lookup(deck))

    async def _user_dep():
        return _user(user_id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user_dep
    client = TestClient(app)
    resp = client.post(f"/api/v1/presentations/{deck.id}/export", json={"format": "pptx_native"})
    assert resp.status_code == 200
    assert routes.EXPORT_NOTES_HEADER == "X-Export-Notes"
    assert json.loads(resp.headers["x-export-notes"]) == list(notes)
    assert resp.headers["x-export-notes"].isascii()
    assert 'filename="quarterly-review.pptx"' in resp.headers["content-disposition"]

    outcome["notes"] = ()
    resp = client.post(f"/api/v1/presentations/{deck.id}/export", json={"format": "pptx_native"})
    assert resp.status_code == 200
    assert "x-export-notes" not in resp.headers


def test_the_export_menu_reads_the_notes_header_into_a_toast():
    """The frontend half of the export log: one reader of the header name,
    surfaced as a toast so the person knows before opening the file."""
    from pathlib import Path

    from app.api.v1 import presentations as routes

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    notes_ts = (frontend / "components" / "presentation" / "export-notes.ts").read_text(encoding="utf-8")
    menu = (frontend / "components" / "presentation" / "PresentationExportMenu.tsx").read_text(encoding="utf-8")
    assert f'export const EXPORT_NOTES_HEADER = "{routes.EXPORT_NOTES_HEADER}";' in notes_ts
    assert "export function exportNotes(" in notes_ts
    assert "exportNotes(" in menu and "toast(" in menu
    assert '"Exported with notes"' in menu
    for catalog in ("en", "fr", "es", "ja"):
        strings = (frontend / "i18n" / "lang" / f"{catalog}.json").read_text(encoding="utf-8")
        assert '"Exported with notes"' in strings, catalog


# ── Tool ──────────────────────────────────────────────────────────────────────


def test_export_tool_passes_the_export_log_on_to_the_agent(monkeypatch):
    """The agent is the one who hands the file over: the log's lines ride the
    tool result under ``Export notes:`` so the user is told what stayed a
    picture before opening the file - and nothing is appended when the export
    was faithful (no empty heading for the model to narrate)."""
    import asyncio
    from contextlib import asynccontextmanager

    from app.services import presentation_export, presentations
    from app.services.builtin_tools import _shared, build_presentation_tools
    from app.tools.base import ToolCall

    user_id = uuid.uuid4()
    deck = _deck_row(user_id, title="Board deck")
    outcome: dict = {
        "notes": ("Slide 8: the gauge chart is a picture of the rendered figure - PowerPoint has no gauge chart.",)
    }

    @asynccontextmanager
    async def _session():
        yield _FakeSession([])

    async def _load(db, deck_id, *, user_id):
        return (deck, [])

    async def _fake_export(**kwargs):
        return presentation_export.ExportedFile(
            "board-deck.pptx", EXPORT_CONTENT_TYPES["pptx_native"], b"PK\x03\x04" + b"0" * 4096, notes=outcome["notes"]
        )

    async def _persist(**kw):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kw["filename"],
            content_type=kw["content_type"],
            size=len(kw["raw_bytes"]),
            s3_object_name=f"chat/conv/{kw['filename']}",
        )

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(presentations, "load_visible_presentation", _load)
    monkeypatch.setattr(presentation_export, "export_presentation", _fake_export)
    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    call = ToolCall(
        id="c1", name="export_presentation", arguments={"presentation_id": str(deck.id), "format": "pptx_native"}
    )
    result = asyncio.run(executor.execute_tool(call))
    assert not result.is_error, result.content
    assert "Exported **Board deck** as PowerPoint (editable shapes): `board-deck.pptx`" in result.content
    assert "[[FILE:" in result.content
    assert "\nExport notes:\n- Slide 8: the gauge chart is a picture of the rendered figure" in result.content

    outcome["notes"] = ()
    deck.revision += 1
    result = asyncio.run(executor.execute_tool(call))
    assert not result.is_error, result.content
    assert "Export notes" not in result.content


def test_exporting_a_deck_already_exported_hands_back_its_file(monkeypatch):
    """The export is offered on every turn: a second export of a deck
    nobody changed since is the same file, not a second render and a second
    attachment. Another format, the deck saved again, or its template edited
    (which restyles the deck without a new revision: its palette, or only its
    chrome and heroes, which bump the container's updated_at) renders anew."""
    import asyncio
    import json
    from contextlib import asynccontextmanager

    from app.services import presentation_export, presentations
    from app.services.builtin_tools import _shared, build_presentation_tools
    from app.tools.base import ToolCall

    user_id = uuid.uuid4()
    deck = _deck_row(user_id, title="Board deck")
    renders: list[str] = []
    template = {"font_heading": "Geologica"}
    container_updated_at = ["2026-11-01T10:00:00+00:00"]

    @asynccontextmanager
    async def _session():
        yield _FakeSession([])

    async def _load(db, deck_id, *, user_id):
        return (deck, [])

    async def _resolve_stamp(db, container_id, *, user_id, deck_theme=None, **_kw):
        return json.dumps({"palette": dict(template), "updated_at": container_updated_at[0]}, sort_keys=True)

    async def _fake_export(**kwargs):
        renders.append(kwargs["export_format"])
        return presentation_export.ExportedFile(
            "board-deck.pdf", EXPORT_CONTENT_TYPES[kwargs["export_format"]], b"%PDF" + b"0" * 4096
        )

    async def _persist(**kw):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kw["filename"],
            content_type=kw["content_type"],
            size=len(kw["raw_bytes"]),
            s3_object_name=f"chat/conv/{kw['filename']}",
        )

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(presentations, "load_visible_presentation", _load)
    monkeypatch.setattr(presentations, "resolve_template_export_stamp", _resolve_stamp)
    monkeypatch.setattr(presentation_export, "export_presentation", _fake_export)
    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]

    def _export(export_format: str):
        call = ToolCall(
            id="c1",
            name="export_presentation",
            arguments={"presentation_id": str(deck.id), "format": export_format},
        )
        result = asyncio.run(executor.execute_tool(call))
        assert not result.is_error, result.content
        return result

    first = _export("pdf")
    again = _export("pdf")
    assert renders == ["pdf"]
    assert again.files == first.files
    assert f"[[FILE:{first.files[0].file_id}]]" in again.content
    assert "**Board deck** has not changed since you exported it as PDF" in again.content

    _export("pptx")
    deck.revision += 1
    saved_since = _export("pdf")
    assert renders == ["pdf", "pptx", "pdf"]
    assert saved_since.files != first.files
    assert saved_since.content.startswith("Exported **Board deck** as PDF")

    template["font_heading"] = "IBM Plex Sans"
    restyled = _export("pdf")
    assert renders == ["pdf", "pptx", "pdf", "pdf"]
    assert restyled.files != saved_since.files

    # A template edit the palette does not carry (its chrome pattern, a hero)
    # still bumps the container: the deck is drawn again.
    container_updated_at[0] = "2026-11-01T10:05:00+00:00"
    rechromed = _export("pdf")
    assert renders == ["pdf", "pptx", "pdf", "pdf", "pdf"]
    assert rechromed.files != restyled.files
    assert _export("pdf").files == rechromed.files
    assert renders == ["pdf", "pptx", "pdf", "pdf", "pdf"]


def test_export_tool_refuses_bad_arguments_without_rendering():
    import asyncio

    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import ToolCall

    _defs, executor = build_presentation_tools(
        db=None,  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
        conversation_id=None,
    )
    bad_format = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="c1",
                name="export_presentation",
                arguments={"presentation_id": str(uuid.uuid4()), "format": "docx"},
            )
        )
    )
    assert bad_format.is_error and "format must be one of" in bad_format.content
    bad_id = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="c2",
                name="export_presentation",
                arguments={"presentation_id": "nope", "format": "pdf"},
            )
        )
    )
    assert bad_id.is_error and "valid UUID" in bad_id.content


def test_frontend_offers_every_export_format():
    """The export menu and the tool must agree on the formats."""
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    menu = (frontend / "components" / "presentation" / "PresentationExportMenu.tsx").read_text(encoding="utf-8")
    types = (frontend / "types" / "index.ts").read_text(encoding="utf-8")
    for export_format in EXPORT_FORMATS:
        assert f'"{export_format}"' in menu, f"export menu lacks {export_format}"
        assert f'"{export_format}"' in types, f"PresentationExportFormat lacks {export_format}"
    static_page = (frontend / "pages" / "PresentationStaticRender.tsx").read_text(encoding="utf-8")
    # The protocol the exporter waits on.
    assert "deckReady" in static_page and "deckError" in static_page
    assert "deck-static-slide" in static_page
    assert "render_token" in static_page


def test_static_render_fails_on_a_figure_that_could_not_be_drawn():
    """The export page never ships a figure that could not be drawn.

    A figure whose lazy code failed to load, or whose drawing threw, drops
    ``data-deck-pending`` and carries ``data-deck-unfinished`` so the browser
    print can start. Nothing gives up on the export page, which waits with no
    bound, so an unfinished figure there is a failed one: the page turns it
    into ``deckError``, which the exporter raises at once, instead of declaring
    itself ready over the figure's error state.
    """
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    static_page = (frontend / "pages" / "PresentationStaticRender.tsx").read_text(encoding="utf-8")
    figure_wait = (frontend / "components" / "presentation" / "figure-wait.ts").read_text(encoding="utf-8")
    assert "deckSettled(root).then(({ complete })" in static_page
    assert "dataset.deckError = UNDRAWN_FIGURE_ERROR" in static_page
    assert '[data-deck-static="true"]' in figure_wait


def test_static_render_declares_the_pdf_paper_as_its_page_rule():
    """The PDF paper is declared on BOTH sides, and the two must agree.

    Chromium keeps the orientation of the document's own ``@page`` rule even
    when ``page.pdf()`` is handed an explicit landscape paper, and the report
    stylesheet ships a global portrait ``@page { size: A4 }`` -- without the
    static render page's own ``@page`` every slide printed on a portrait page,
    shrunk into its upper half. The rule must be emitted from the page (so it
    sorts after every bundled stylesheet) and its sizes must be the exporter's.
    """
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    static_page = (frontend / "pages" / "PresentationStaticRender.tsx").read_text(encoding="utf-8")
    geometry = (frontend / "components" / "presentation" / "geometry.ts").read_text(encoding="utf-8")
    assert "@page { size: ${pdfPageSize(" in static_page
    for aspect, (width, height) in PDF_PAGE_SIZES_IN.items():
        assert f'"{aspect}": "{width} {height}"' in geometry, f"geometry.ts PDF paper differs for {aspect}"


def test_section_ghost_number_is_placed_by_its_layout_box_not_a_transform():
    """The section slide's ghost number is centred by its LAYOUT box.

    ``page.pdf()`` paginates on layout boxes: a CSS transform moves what is
    painted, never what the paginator sees, and a single line box has no break
    opportunity. Centred with ``top: 50%`` + ``translateY(-50%)``, the 560px
    line box sat at 526..1086px of the 1080px page -- 6px over the edge -- so
    Chromium pushed the whole number to the NEXT page, the transform lifted it
    280px back into the slide and the slide's overflow clip cut it to its top:
    a truncated number at the bottom of every section slide that had a slide
    after it (the last page has nowhere to push to, which is how a deck ending
    on a section hid it). The screen never paginates, so the viewer was fine.

    The rule must therefore put the layout box where the number is drawn:
    ``top: calc(50% - 0.5em)`` -- half the box, because the line is
    ``line-height: 1`` -- and no transform at all.
    """
    import re
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    css = (frontend / "components" / "presentation" / "presentation.css").read_text(encoding="utf-8")
    rule = re.search(r"\.deck-root \.deck-section-number \{(?P<body>[^}]*)\}", css)
    assert rule, "presentation.css lost the .deck-root .deck-section-number rule"
    body = rule.group("body")
    assert re.search(r"(?<![\w-])transform\s*:", body) is None, "the ghost number must not be placed by a transform"
    assert "top: calc(50% - 0.5em);" in body, "the ghost number must be centred by its layout box"
    assert "line-height: 1;" in body, "0.5em is half the box only while the line is line-height: 1"


def _capture_setup(monkeypatch, deck, *, slide_images: list[bytes]):
    """Wire the capture tool onto fakes: a visible deck and a rendered deck."""
    from contextlib import asynccontextmanager

    from app.services import presentation_export, presentations
    from app.services.builtin_tools import _shared

    @asynccontextmanager
    async def _session():
        yield _FakeSession([])

    async def _load(db, deck_id, *, user_id):
        return (deck, [])

    async def _render(presentation_id, user_id, **kwargs):
        assert kwargs.get("want_slide_images") is True
        # The real renderer screenshots ONLY the requested slides, in the order
        # asked for. The fake must do the same, or these tests would pass with
        # the selection ignored and the agent shown the wrong slides.
        picked = kwargs.get("slide_indices")
        assert picked is not None, "the capture must narrow the render"
        return presentation_export.RenderedDeck(
            slide_count=len(slide_images),
            slide_images=[slide_images[i] for i in picked if i < len(slide_images)],
        )

    async def _persist(**kw):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kw["filename"],
            content_type=kw["content_type"],
            size=len(kw["raw_bytes"]),
            s3_object_name=f"chat/conv/{kw['filename']}",
        )

    monkeypatch.setattr(_shared, "_own_session", _session)
    monkeypatch.setattr(presentations, "load_visible_presentation", _load)
    monkeypatch.setattr(presentation_export, "render_deck", _render)
    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)


def _capture_deck(user_id: uuid.UUID, slides: int):
    deck = _deck_row(user_id, title="Board deck")
    deck.spec = {
        **deck.spec,
        "slides": [{"layout": "content", "title": f"S{index}"} for index in range(slides)],
    }
    return deck


def test_capture_tool_attaches_the_pixels_of_the_slides_it_was_asked_for(monkeypatch):
    """The agent's own eyes: the PNGs ride the tool result as vision blocks, so
    it judges the render instead of asking the user what the deck looks like."""
    import asyncio

    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import ToolCall

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=6)
    _capture_setup(monkeypatch, deck, slide_images=[f"png-{i}".encode() for i in range(6)])

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="c1",
                name="capture_presentation_slides",
                arguments={"presentation_id": str(deck.id), "slides": "1,4"},
            )
        )
    )

    assert not result.is_error, result.content
    assert len(result.files) == 2
    assert [ref.content_type for ref in result.files] == ["image/png", "image/png"]
    assert "Captured slide(s) 1, 4" in result.content
    assert "(6 in the deck" in result.content
    # Each capture carries the pixels of the slide it claims, not of the nth
    # image the renderer happened to return.
    assert [ref.filename.split("-")[1] for ref in result.files] == ["01", "04"]
    # The instruction that keeps the agent from outsourcing its own review.
    assert "never ask the user" in result.content.lower()


def test_capture_tool_defaults_to_the_opening_the_middle_and_the_closing_slide(monkeypatch):
    import asyncio

    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import ToolCall

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=9)
    _capture_setup(monkeypatch, deck, slide_images=[f"png-{i}".encode() for i in range(9)])

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="c1",
                name="capture_presentation_slides",
                arguments={"presentation_id": str(deck.id)},
            )
        )
    )

    assert not result.is_error, result.content
    assert "Captured slide(s) 1, 5, 9" in result.content


def test_capture_tool_refuses_more_slides_than_it_will_put_in_the_context(monkeypatch):
    """Each capture is a full-canvas image; the cap is what keeps a "show me the
    deck" reflex from spending the turn's budget on pictures."""
    import asyncio

    from app.services.builtin_tools import build_presentation_tools
    from app.services.builtin_tools.presentation import MAX_SLIDE_CAPTURES
    from app.tools.base import ToolCall

    user_id = uuid.uuid4()
    count = MAX_SLIDE_CAPTURES + 2
    deck = _capture_deck(user_id, slides=count)
    _capture_setup(monkeypatch, deck, slide_images=[b"png"] * count)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="c1",
                name="capture_presentation_slides",
                arguments={"presentation_id": str(deck.id), "slides": f"1-{count}"},
            )
        )
    )

    assert result.is_error
    assert str(MAX_SLIDE_CAPTURES) in result.content


def test_capture_tool_says_it_could_not_look_rather_than_describing_a_render(monkeypatch):
    """A render that fails must not leave the agent free to narrate the deck it
    expected: the failure is stated, and the tool returns nothing to look at."""
    import asyncio
    from contextlib import asynccontextmanager

    from app.services import presentation_export, presentations
    from app.services.builtin_tools import _shared, build_presentation_tools
    from app.tools.base import ToolCall

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=3)

    @asynccontextmanager
    async def _session():
        yield _FakeSession([])

    async def _load(db, deck_id, *, user_id):
        return (deck, [])

    async def _render(*a, **k):
        raise presentation_export.PresentationRenderError("an asset never loaded")

    monkeypatch.setattr(_shared, "_own_session", _session)
    monkeypatch.setattr(presentations, "load_visible_presentation", _load)
    monkeypatch.setattr(presentation_export, "render_deck", _render)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="c1",
                name="capture_presentation_slides",
                arguments={"presentation_id": str(deck.id)},
            )
        )
    )

    assert result.is_error
    assert "an asset never loaded" in result.content
    assert not result.files


def test_capture_tool_does_not_hand_the_user_files_it_never_asked_for(monkeypatch):
    """A review capture is for the model's eyes. A ref registered on the
    conversation's FileContext is attached to the assistant message as a
    download (``_turn_generated_ids`` in api/v1/chat.py), so three review rounds
    would leave nine file chips nobody asked for."""
    import asyncio

    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import FileContext, ToolCall

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=3)
    _capture_setup(monkeypatch, deck, slide_images=[b"png-0", b"png-1", b"png-2"])

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    executor.file_context = FileContext.from_file_refs([])
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="c1",
                name="capture_presentation_slides",
                arguments={"presentation_id": str(deck.id)},
            )
        )
    )

    assert not result.is_error, result.content
    # The pixels ride the tool result (that is the point) ...
    assert len(result.files) == 3
    # ... and nothing was added to the conversation's files.
    assert executor.file_context.is_empty


def _count_persisted(monkeypatch) -> list[str]:
    """Record the filename of every capture the tool STORES (after ``_capture_setup``)."""
    from app.services.builtin_tools import _shared

    stored: list[str] = []
    persist = _shared._persist_generated_file

    async def _persist(**kw):
        stored.append(kw["filename"])
        return await persist(**kw)

    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)
    return stored


def _capture(executor, deck, slides: str | None = None):
    import asyncio

    from app.tools.base import ToolCall

    arguments = {"presentation_id": str(deck.id)}
    if slides is not None:
        arguments["slides"] = slides
    return asyncio.run(
        executor.execute_tool(ToolCall(id="c", name="capture_presentation_slides", arguments=arguments))
    )


def test_a_recapture_of_unchanged_slides_hands_back_the_same_files(monkeypatch):
    """Every capture used to store new PNGs under new file ids, so the loop's
    once-per-file vision ledger never recognised a slide it had already
    shown, and an unchanged slide spent the turn's image budget again. The same
    pixels now come back as the same file, stored once, and the result says
    nothing changed on them."""
    from app.services.builtin_tools import build_presentation_tools

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=9)
    _capture_setup(monkeypatch, deck, slide_images=[f"png-{i}".encode() for i in range(9)])
    stored = _count_persisted(monkeypatch)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    first = _capture(executor, deck)
    again = _capture(executor, deck)

    assert not first.is_error and not again.is_error, again.content
    assert [ref.file_id for ref in again.files] == [ref.file_id for ref in first.files]
    assert len(stored) == 3, "the second capture stored nothing"
    assert "changed since your earlier capture" not in first.content
    assert "Nothing on slide(s) 1, 5, 9 changed since your earlier capture this turn" in again.content
    assert "If you edited them since, the edit did not change what they show" in again.content


def test_a_recapture_after_an_edit_stores_only_the_slide_that_changed(monkeypatch):
    from app.services.builtin_tools import build_presentation_tools

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=9)
    images = [f"png-{i}".encode() for i in range(9)]
    _capture_setup(monkeypatch, deck, slide_images=images)
    stored = _count_persisted(monkeypatch)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    first = _capture(executor, deck)
    images[4] = b"png-4-edited"
    after_edit = _capture(executor, deck)

    first_ids = [ref.file_id for ref in first.files]
    edit_ids = [ref.file_id for ref in after_edit.files]
    assert edit_ids[0] == first_ids[0] and edit_ids[2] == first_ids[2]
    assert edit_ids[1] != first_ids[1], "the edited slide is a new image"
    assert stored == [
        f"slide-01-{deck.id.hex[:8]}.png",
        f"slide-05-{deck.id.hex[:8]}.png",
        f"slide-09-{deck.id.hex[:8]}.png",
        f"slide-05-{deck.id.hex[:8]}.png",
    ]
    assert "Slide(s) 1, 9 render the identical PNG of your earlier capture this turn" in after_edit.content
    assert "Nothing on slide(s)" not in after_edit.content


def test_a_reused_capture_carries_the_notes_the_agent_saved_on_it(monkeypatch):
    """``annotate_file`` writes on the ref the turn's context resolved, and the
    loop quotes the notes of the ref a result carries when it withholds pixels
    it already showed: a reused capture must carry them, or the note would say
    "no notes saved" to an agent that saved some."""
    import dataclasses

    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import FileContext

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=3)
    _capture_setup(monkeypatch, deck, slide_images=[b"png-0", b"png-1", b"png-2"])

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    executor.file_context = FileContext.from_file_refs([])
    first = _capture(executor, deck, slides="2")
    (shown,) = first.files
    executor.file_context.add(dataclasses.replace(shown, notes="slide 2: the table overflows the footer"))

    (reused,) = _capture(executor, deck, slides="2").files

    assert reused.file_id == shown.file_id
    assert reused.notes == "slide 2: the table overflows the footer"


def test_each_turn_captures_its_own_files(monkeypatch):
    """The memory is the turn's, like the vision budget it protects: the tools
    are built per turn, and the next turn's first capture stores its own files."""
    from app.services.builtin_tools import build_presentation_tools

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=3)
    _capture_setup(monkeypatch, deck, slide_images=[b"png-0", b"png-1", b"png-2"])
    stored = _count_persisted(monkeypatch)

    _defs, this_turn = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    _defs, next_turn = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    first = _capture(this_turn, deck)
    later = _capture(next_turn, deck)

    assert {ref.file_id for ref in first.files}.isdisjoint(ref.file_id for ref in later.files)
    assert len(stored) == 6
    assert "earlier capture" not in later.content


def test_a_recapture_of_slides_that_render_alike_reuses_each_of_them(monkeypatch):
    """Two slides that render alike (no numbered footer), captured outside a
    conversation, are two files, and each gets ITS file back on a re-capture.
    Keyed on the pixels alone, the second one was stored again on every capture
    and spent the turn's vision budget each time."""
    from app.services.builtin_tools import build_presentation_tools

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=3)
    _capture_setup(monkeypatch, deck, slide_images=[b"same", b"same", b"png-2"])
    stored = _count_persisted(monkeypatch)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    first = _capture(executor, deck, slides="1,2")
    again = _capture(executor, deck, slides="1,2")

    first_ids = [ref.file_id for ref in first.files]
    assert len(set(first_ids)) == 2, first.content
    assert [ref.file_id for ref in again.files] == first_ids
    assert len(stored) == 2, "the re-capture stored nothing"
    assert "Nothing on slide(s) 1, 2 changed since your earlier capture this turn" in again.content


def test_a_slide_captured_for_the_first_time_is_never_called_unchanged(monkeypatch):
    """A slide's first capture is never reported unchanged because another
    slide already rendered the same pixels this turn: "unchanged" is said of a
    slide."""
    from app.services.builtin_tools import build_presentation_tools

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=3)
    _capture_setup(monkeypatch, deck, slide_images=[b"same", b"same", b"png-2"])

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    (slide_one,) = _capture(executor, deck, slides="1").files
    second = _capture(executor, deck, slides="2")

    assert second.files[0].file_id != slide_one.file_id
    assert "earlier capture" not in second.content


def test_the_same_pixels_on_another_deck_are_another_image(monkeypatch):
    from app.services.builtin_tools import build_presentation_tools

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=3)
    other = _capture_deck(user_id, slides=3)
    _capture_setup(monkeypatch, deck, slide_images=[b"png-0", b"png-1", b"png-2"])
    stored = _count_persisted(monkeypatch)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    first = _capture(executor, deck)
    elsewhere = _capture(executor, other)

    assert {ref.file_id for ref in first.files}.isdisjoint(ref.file_id for ref in elsewhere.files)
    assert len(stored) == 6
    assert "earlier capture" not in elsewhere.content


def test_a_capture_that_could_not_be_stored_is_stored_by_the_next_capture(monkeypatch):
    from app.services.builtin_tools import _shared, build_presentation_tools

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=3)
    _capture_setup(monkeypatch, deck, slide_images=[b"png-0", b"png-1", b"png-2"])
    stored = _count_persisted(monkeypatch)
    persist = _shared._persist_generated_file
    failing = {f"slide-02-{deck.id.hex[:8]}.png"}

    async def _persist(**kw):
        if kw["filename"] in failing:
            failing.discard(kw["filename"])
            raise RuntimeError("storage unavailable")
        return await persist(**kw)

    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    first = _capture(executor, deck)
    again = _capture(executor, deck)

    assert "Captured slide(s) 1, 3 of" in first.content
    assert "Captured slide(s) 1, 2, 3 of" in again.content
    assert [ref.file_id for ref in again.files][::2] == [ref.file_id for ref in first.files]
    assert stored == [f"slide-{n}-{deck.id.hex[:8]}.png" for n in ("01", "03", "02")]
    assert "Slide(s) 1, 3 render the identical PNG of your earlier capture this turn" in again.content


def test_a_capture_of_a_deck_that_shrank_before_the_render_labels_each_slide_by_its_pixels(monkeypatch):
    """The page renders the CURRENT row: when it holds fewer slides than the
    spec read before the render, the renderer skips the picks past its end
    wherever they sit in the selection, and every PNG kept is still named after
    the slide it shows."""
    from app.services.builtin_tools import build_presentation_tools

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=9)
    _capture_setup(monkeypatch, deck, slide_images=[b"png-0", b"png-1", b"png-2"])

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    result = _capture(executor, deck, slides="9,1")

    assert not result.is_error, result.content
    assert "Captured slide(s) 1 of" in result.content
    assert [ref.filename for ref in result.files] == [f"slide-01-{deck.id.hex[:8]}.png"]


@pytest.mark.asyncio
async def test_the_loop_spends_the_vision_budget_only_on_slides_that_changed(monkeypatch):
    """The review loop the tool's description asks for, run by the real loop:
    capture, capture again with nothing changed, edit one slide, capture. The
    unchanged re-capture is answered with the dated "already viewed" note
    instead of the pixels a second time, and the three captures spend four
    images of the turn's budget (the three slides, then the edited one) where
    they used to spend nine - two default captures already exhausted a budget
    of six and left the agent blind for the rest of its edit-check rounds."""
    from app.llm.base import ChatMessage, ChatResponse
    from app.services.builtin_tools import build_presentation_tools
    from app.tools import agent_loop as al
    from app.tools.base import CompositeToolExecutor, FileContext, ToolCall
    from tests.test_tool_call_dedup import _ScriptedLLM, _tool_call_response

    for name in ("_lf_start_llm_gen", "_lf_end_generation", "_lf_end_span", "_lf_end_tool_span"):
        monkeypatch.setattr(al, name, lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    monkeypatch.setattr(settings, "agent_loop_vision_views_per_turn", 6)

    user_id = uuid.uuid4()
    deck = _capture_deck(user_id, slides=9)
    images = [f"png-{i}".encode() for i in range(9)]
    _capture_setup(monkeypatch, deck, slide_images=images)
    defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)

    class _EditsSlideFive(_ScriptedLLM):
        async def chat(self, messages, model=None, tools=None, **kwargs):
            if self._idx == 2:
                images[4] = b"png-4-edited"
            return await super().chat(messages, model=model, tools=tools, **kwargs)

    capture = {"presentation_id": str(deck.id)}
    llm = _EditsSlideFive(
        [
            _tool_call_response(ToolCall(id="c1", name="capture_presentation_slides", arguments=capture)),
            _tool_call_response(ToolCall(id="c2", name="capture_presentation_slides", arguments=capture)),
            _tool_call_response(ToolCall(id="c3", name="capture_presentation_slides", arguments={**capture, "slides": "1,5,9"})),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    ctx = FileContext()
    messages: list[ChatMessage] = [ChatMessage(role="user", content="review the deck")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10, file_context=ctx
    )

    _c1, c2, c3 = (m for m in messages if m.role == "tool")
    assert c2.files is None
    assert "Already viewed at step 1" in c2.content
    assert "Nothing on slide(s) 1, 5, 9 changed since your earlier capture this turn" in c2.content
    assert c3.files is not None and len(c3.files) == 3, c3.content
    assert "vision budget exhausted" not in c3.content
    assert "Slide(s) 1, 9 render the identical PNG" in c3.content
    # The three slides once, then the edited slide five.
    assert len(ctx.vision_views) == 4
    assert {ref.file_id for ref in c3.files} <= set(ctx.vision_views)
