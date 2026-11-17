"""Hostile SVGs through the chat image path are bounded.

Every SVG a model is shown is rasterized server-side (no vision provider takes
``image/svg+xml``). The markup is untrusted, and three shapes reached resvg
unbounded: a ~100-byte SVG with a tall size (a pixmap as long as the author
likes), an XML entity declaration (expanded, the XXE / billion-laughs vector),
and a nested ``<use>`` (a ~1 KB file costing seconds per level). Each is sent
here through the real chat preparation (``prepare_image_bytes_for_llm`` /
``prepare_base64_image_for_llm`` and their async variants) and must come out
rendered inside the box, or refused, within a bounded time - while a normal
logo and a normal diagram render byte for byte as they always did.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import hashlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import resvg_py
from PIL import Image

from app.llm.base import prepare_base64_image_for_llm, prepare_image_bytes_for_llm

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_svg_worker_left():
    """A test may leave the worker busy (a render it stopped waiting for):
    each one ends with no worker, so none outlives its test. Each one also
    starts and ends with an empty render cache, so a render a test expects to
    reach the worker is never answered by an earlier test's result."""
    from app.services.svg_raster import clear_svg_raster_cache, shutdown_svg_raster_worker

    clear_svg_raster_cache()
    yield
    clear_svg_raster_cache()
    shutdown_svg_raster_worker()


#: A logo as a design tool exports it: a stylesheet naming a local gradient.
LOGO_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 80" width="240" height="80">'
    b"<defs><style>.cls-1{fill:url(#g);}</style>"
    b'<linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#0fbcff"/>'
    b'<stop offset="1" stop-color="#001bda"/></linearGradient></defs>'
    b'<circle class="cls-1" cx="40" cy="40" r="32"/>'
    b'<rect x="88" y="24" width="136" height="32" rx="6" fill="#18191b"/></svg>'
)
#: A diagram as a renderer draws it: boxes, arrows with marker heads, reused shapes.
DIAGRAM_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
    b'width="640" height="360" viewBox="0 0 640 360"><defs>'
    b'<marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">'
    b'<path d="M0,0 L10,5 L0,10 z" fill="#333"/></marker>'
    b'<g id="node"><rect width="120" height="56" rx="8" fill="#e8f4ff" stroke="#0077cc" stroke-width="2"/></g>'
    b"</defs>"
    b'<use xlink:href="#node" x="40" y="40"/><use xlink:href="#node" x="260" y="40"/>'
    b'<use xlink:href="#node" x="480" y="40"/><use xlink:href="#node" x="260" y="240"/>'
    b'<path d="M160,68 L256,68" stroke="#333" stroke-width="2" marker-end="url(#arrow)"/>'
    b'<path d="M380,68 L476,68" stroke="#333" stroke-width="2" marker-end="url(#arrow)"/>'
    b'<path d="M320,96 L320,236" stroke="#333" stroke-width="2" marker-end="url(#arrow)"/></svg>'
)
#: ~100 bytes asking for a 100 x 20000 px render.
TALL_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="20000" viewBox="0 0 10 2000">'
    b'<rect width="10" height="2000" fill="#123456"/></svg>'
)
ENTITY_SVG = (
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY side "200">]>'
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 &side; &side;">'
    b'<rect x="50" y="50" width="100" height="100"/></svg>'
)


def _use_fan(depth: int, fan: int = 10) -> bytes:
    """A ``<use>`` of a group of ``<use>``s, *depth* levels deep: ``fan ** depth`` shapes."""
    levels = ['<g id="l0"><rect width="1" height="1"/></g>']
    for level in range(1, depth + 1):
        uses = "".join(f'<use href="#l{level - 1}" x="{i}"/>' for i in range(fan))
        levels.append(f'<g id="l{level}">{uses}</g>')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs>'
        + "".join(levels)
        + f'</defs><use href="#l{depth}"/></svg>'
    ).encode()


@pytest.fixture
def resvg_calls(monkeypatch):
    """Every render resvg does in THIS process: the pixmap size it produced."""
    real = resvg_py.svg_to_bytes
    calls: list[tuple[int, int]] = []

    def spy(**kwargs):
        png = real(**kwargs)
        with Image.open(io.BytesIO(bytes(png))) as opened:
            calls.append(opened.size)
        return png

    monkeypatch.setattr(resvg_py, "svg_to_bytes", spy)
    return calls


#: The reason the structural pass gives when it refuses an SVG before resvg runs.
UNBOUNDED = "its references, shapes or filters are unbounded"


@pytest.fixture
def render_reasons(monkeypatch):
    """Why each inline render of THIS process gave no PNG (``None`` for a PNG).

    A refusal test asserts the structural reason and ``resvg_calls == []``:
    what the bound means (refused before resvg draws anything), never a
    wall-clock budget a loaded CI runner cannot keep.
    """
    from app.services import svg_raster

    real = svg_raster.render_svg
    reasons: list[str | None] = []

    def spy(*args, **kwargs):
        rendered = real(*args, **kwargs)
        reasons.append(rendered.reason)
        return rendered

    monkeypatch.setattr(svg_raster, "render_svg", spy)
    return reasons


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# -- normal SVGs render exactly as before -------------------------------------


@pytest.mark.parametrize("svg", [LOGO_SVG, DIAGRAM_SVG], ids=["logo", "diagram"])
def test_a_normal_svg_renders_byte_for_byte_as_before(svg):
    from app.llm.base import _rasterize_svg_to_png

    before = bytes(resvg_py.svg_to_bytes(svg_string=svg.decode("utf-8")))
    assert _rasterize_svg_to_png(svg, source="pin") == before


@pytest.mark.parametrize("svg", [LOGO_SVG, DIAGRAM_SVG], ids=["logo", "diagram"])
def test_a_normal_svg_renders_byte_for_byte_as_before_off_the_loop(svg):
    from app.llm.base import LLM_SVG_BOX
    from app.services.svg_raster import rasterize_svg_async

    before = bytes(resvg_py.svg_to_bytes(svg_string=svg.decode("utf-8")))
    assert asyncio.run(rasterize_svg_async(svg, box=LLM_SVG_BOX, keep_native_size=True)) == before


# -- the three hostile shapes -------------------------------------------------


#: The chat path's pixel box (``app.llm.base.LLM_SVG_BOX``).
CHAT_BOX = 4096


def test_the_chat_path_renders_into_a_4096_px_box():
    from app.llm.base import LLM_SVG_BOX

    assert LLM_SVG_BOX == CHAT_BOX


def test_a_tall_svg_is_rendered_inside_the_box(resvg_calls):
    started = time.monotonic()
    assert prepare_image_bytes_for_llm(TALL_SVG, "image/svg+xml", source="tall") is not None
    assert prepare_base64_image_for_llm(_b64(TALL_SVG), source="tall-b64") is not None
    assert time.monotonic() - started < 10
    assert len(resvg_calls) == 2
    for width, height in resvg_calls:
        # Memory: the pixmap is at most CHAT_BOX square (4 bytes a pixel).
        assert max(width, height) <= CHAT_BOX
        assert height > 50 * width  # the ratio survives


def test_a_bounded_internal_entity_is_expanded_and_rendered():
    """An internal entity (an Illustrator export declares its namespaces this
    way) is expanded and rendered - byte-for-byte as resvg renders it on its
    own, which expands it too - not refused."""
    from app.services.svg_raster import rasterize_svg

    before = bytes(resvg_py.svg_to_bytes(svg_string=ENTITY_SVG.decode("utf-8"), dpi=96, width=512, height=512))
    assert rasterize_svg(ENTITY_SVG, box=512) == before


def test_a_multiplying_svg_is_refused_before_resvg_sees_it(resvg_calls):
    started = time.monotonic()
    svg = _use_fan(5)
    assert prepare_image_bytes_for_llm(svg, "image/svg+xml", source="hostile") is None
    assert prepare_base64_image_for_llm(_b64(svg), source="hostile-b64") is None
    assert time.monotonic() - started < 5
    assert resvg_calls == []


# -- compatibility: what resvg rendered on its own keeps rendering


#: An Adobe Illustrator export: its DOCTYPE declares namespaces and a style as entities.
ILLUSTRATOR_SVG = (
    b'<?xml version="1.0" encoding="utf-8"?>\n<!-- Generator: Adobe Illustrator 27.0.0 -->\n'
    b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd" [\n'
    b'\t<!ENTITY ns_extend "http://ns.adobe.com/Extensibility/1.0/">\n'
    b'\t<!ENTITY ns_ai "http://ns.adobe.com/AdobeIllustrator/10.0/">\n'
    b'\t<!ENTITY st0 "fill:#2B3A67;">\n]>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="&ns_extend;" xmlns:i="&ns_ai;" '
    b'width="120" height="60" viewBox="0 0 120 60"><rect x="10" y="10" width="100" height="40" style="&st0;"/></svg>'
)


def _billion_laughs(levels: int = 9) -> bytes:
    decls = ['<!ENTITY a0 "haha">'] + [f'<!ENTITY a{i} "{("&a" + str(i - 1) + ";") * 10}">' for i in range(1, levels + 1)]
    return (
        f'<?xml version="1.0"?><!DOCTYPE svg [{"".join(decls)}]>'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><text>&a{levels};</text></svg>'
    ).encode()


UNSAFE_ENTITIES = {
    "billion-laughs": _billion_laughs(),
    "external-system": (
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><text>&x;</text></svg>'
    ),
    "external-public": (
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x PUBLIC "-//X//EN" "http://127.0.0.1:1/x">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><text>&x;</text></svg>'
    ),
    "parameter": (
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY % p SYSTEM "http://127.0.0.1:1/e.dtd">%p;]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"/>'
    ),
    "too-many": (
        b'<?xml version="1.0"?><!DOCTYPE svg ['
        + b"".join(f'<!ENTITY e{i} "x">'.encode() for i in range(300))
        + b']><svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"/>'
    ),
}


@pytest.mark.parametrize("svg", [ENTITY_SVG, ILLUSTRATOR_SVG], ids=["entity", "illustrator"])
def test_an_internal_entity_renders_to_the_png_resvg_draws_on_its_own(svg):
    """resvg expands a DOCTYPE's internal entities itself, so an Illustrator
    export rendered before the bounds: it renders to the same PNG, and the chat
    MIME sniffing now finds its ``<svg>`` root past the internal subset (it
    stopped at the first ``>`` inside the subset)."""
    from app.llm.base import _detect_image_mime_from_bytes, _rasterize_svg_to_png

    assert _rasterize_svg_to_png(svg, source="entity") == bytes(resvg_py.svg_to_bytes(svg_string=svg.decode()))
    assert _detect_image_mime_from_bytes(svg) == "image/svg+xml"
    assert prepare_image_bytes_for_llm(svg, "image/svg+xml", source="entity") is not None
    assert prepare_base64_image_for_llm(_b64(svg), source="entity-b64") is not None


@pytest.mark.parametrize("svg", UNSAFE_ENTITIES.values(), ids=UNSAFE_ENTITIES.keys())
def test_an_unsafe_entity_is_still_refused_before_resvg_sees_it(resvg_calls, svg):
    """An external or parameter entity (the XXE vectors), a billion-laughs
    expansion and a flood of declarations stay refused - by the rasterizer's own
    bound, whatever the libexpat in use caps - and resvg never sees them."""
    from app.llm.base import _rasterize_svg_to_png

    started = time.monotonic()
    assert _rasterize_svg_to_png(svg, source="entity") is None
    assert prepare_image_bytes_for_llm(svg, "image/svg+xml", source="entity") is None
    assert time.monotonic() - started < 5
    assert resvg_calls == []


#: A long and a short style fragment, as an Illustrator export declares them.
LONG_STYLE = "fill:#2B3A67;stroke:#1D2847;stroke-width:2;stroke-miterlimit:10;opacity:0.9;"
SHORT_STYLE = "fill:#2B3A67;"


def _style_entity_svg(*values: str) -> bytes:
    """An SVG whose DOCTYPE declares the style entity ``st0`` once per value, in order."""
    decls = "".join(f'<!ENTITY st0 "{value}">' for value in values)
    return (
        f'<?xml version="1.0"?><!DOCTYPE svg [{decls}]>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
        '<rect width="8" height="8" style="&st0;"/></svg>'
    ).encode()


def test_a_redeclared_entity_renders_its_first_value_as_resvg_does():
    """XML binds the FIRST declaration of a name (XML 1.0 section 4.2): expat
    and resvg both expand it and ignore the redeclaration."""
    from app.services.svg_raster import rasterize_svg

    svg = _style_entity_svg("fill:#ff0000;", "fill:#0000ff;")
    png = rasterize_svg(svg, box=64, keep_native_size=True)
    assert png == bytes(resvg_py.svg_to_bytes(svg_string=svg.decode()))
    with Image.open(io.BytesIO(png)) as opened:
        assert opened.convert("RGB").getpixel((4, 4)) == (255, 0, 0)


def test_the_expansion_bound_reads_the_first_declaration_of_a_name(monkeypatch, resvg_calls):
    """The bound measures the value resvg expands - the first one - never a
    shorter redeclaration after it."""
    from app.services import svg_raster

    monkeypatch.setattr(svg_raster, "MAX_SVG_ENTITY_EXPANSION", len(LONG_STYLE) - 1)
    assert svg_raster.rasterize_svg(_style_entity_svg(SHORT_STYLE), box=64) is not None
    assert svg_raster.rasterize_svg(_style_entity_svg(LONG_STYLE, SHORT_STYLE), box=64) is None
    assert len(resvg_calls) == 1


def test_every_declaration_counts_against_the_declaration_bound(monkeypatch, resvg_calls):
    """A name declared again is still a declaration: the bound counts them all,
    not the distinct names."""
    from app.services import svg_raster

    monkeypatch.setattr(svg_raster, "MAX_SVG_ENTITIES", 3)
    assert svg_raster.rasterize_svg(_style_entity_svg(*[SHORT_STYLE] * 3), box=64) is not None
    assert svg_raster.rasterize_svg(_style_entity_svg(*[SHORT_STYLE] * 4), box=64) is None
    assert len(resvg_calls) == 1


def test_a_remote_image_next_to_a_rectangle_renders_the_rectangle_with_no_network_call(monkeypatch):
    import socket

    def no_network(*_args, **_kwargs):
        raise AssertionError("the rasterizer opened a connection")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    from app.llm.base import _rasterize_svg_to_png

    with_image = _reaching_out('<image href="https://example.com/a.png" width="64" height="64"/>')
    assert _rasterize_svg_to_png(with_image, source="remote") == _rasterize_svg_to_png(BLUE_SQUARE, source="rest")


def test_strict_rendering_refuses_what_the_default_renders(monkeypatch, resvg_calls):
    """``SVG_STRICT_RENDERING=true`` refuses, whole, every shape the default
    renders with the reference dropped or the entity expanded."""
    from app.core.config import settings
    from app.llm.base import _rasterize_svg_to_png

    monkeypatch.setattr(settings, "svg_strict_rendering", True)
    for svg in (
        ENTITY_SVG,
        ILLUSTRATOR_SVG,
        _reaching_out('<image href="https://example.com/a.png" width="64" height="64"/>'),
        _reaching_out('<use href="other.svg#x"/>'),
        _reaching_out('<rect width="9" height="9" fill="url(https://x/y)"/>'),
    ):
        assert _rasterize_svg_to_png(svg, source="strict") is None, svg
    assert resvg_calls == []
    assert _rasterize_svg_to_png(BLUE_SQUARE, source="strict") is not None


def _nested(inner: bytes, *, base64_encoded: bool = True) -> str:
    if base64_encoded:
        return "data:image/svg+xml;base64," + _b64(inner)
    from urllib.parse import quote

    return "data:image/svg+xml;utf8," + quote(inner.decode(), safe="")


#: A draw.io icon: a small SVG an ``<image>`` embeds as a data URI.
DRAWIO_ICON = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle cx="20" cy="20" r="18" fill="#0a0"/></svg>'


@pytest.mark.parametrize("base64_encoded", [True, False], ids=["base64", "percent-encoded"])
def test_a_nested_svg_renders_to_the_png_resvg_draws_on_its_own(base64_encoded):
    """draw.io embeds its icons as nested SVG data URIs: checked under the same
    rules (nothing to drop here), they render byte for byte as before."""
    from app.llm.base import _rasterize_svg_to_png

    svg = _reaching_out(f'<image href="{_nested(DRAWIO_ICON, base64_encoded=base64_encoded)}" width="40" height="40"/>')
    assert _rasterize_svg_to_png(svg, source="drawio") == bytes(resvg_py.svg_to_bytes(svg_string=svg.decode()))


def test_a_nested_svg_is_charged_every_time_it_is_drawn(resvg_calls):
    """A nested SVG is drawn each time its ``<image>`` is - through every
    ``<use>`` of it too: 20,000 shapes inside, drawn by 5 uses, is 100,000
    shapes; one image of it renders."""
    from app.services.svg_raster import rasterize_svg

    inner = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        + "".join(f'<rect x="{i % 100}" y="{i // 100}" width="1" height="1"/>' for i in range(20000))
        + "</svg>"
    ).encode()
    image = f'<image id="icon" href="{_nested(inner)}" width="10" height="10"/>'
    uses = "".join(f'<use href="#icon" x="{i * 10}"/>' for i in range(5))
    assert rasterize_svg(_reaching_out(image + uses), box=512) is None
    assert resvg_calls == []
    assert rasterize_svg(_reaching_out(image), box=512) is not None


def test_nested_svgs_past_the_depth_limit_are_dropped():
    from app.services.svg_raster import MAX_NESTED_SVG_DEPTH, rasterize_svg

    svg = DRAWIO_ICON
    for _ in range(MAX_NESTED_SVG_DEPTH + 1):
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><image href="{_nested(svg)}" width="40" height="40"/></svg>'.encode()
    # The deepest level is dropped, the drawing around it still renders.
    assert rasterize_svg(_reaching_out(f'<image href="{_nested(svg)}" width="40" height="40"/>'), box=512) is not None


def test_a_deeper_nested_use_costs_no_more_to_refuse():
    """Ten times the shapes per level (a million shapes here, seconds of
    resvg): the refusal is counted, never rendered."""
    started = time.monotonic()
    assert prepare_image_bytes_for_llm(_use_fan(6), "image/svg+xml", source="deep") is None
    assert time.monotonic() - started < 5


def test_the_async_variants_bound_the_same_shapes_off_the_event_loop(resvg_calls):
    from app.llm.base import (
        LLM_SVG_BOX,
        prepare_base64_image_for_llm_async,
        prepare_image_bytes_for_llm_async,
    )

    async def run():
        tall = await prepare_image_bytes_for_llm_async(TALL_SVG, "image/svg+xml", source="tall")
        refused = [
            await prepare_image_bytes_for_llm_async(_use_fan(5), "image/svg+xml", source="nested-use"),
            await prepare_base64_image_for_llm_async(_b64(_use_fan(5)), source="nested-use-b64"),
        ]
        logo = await prepare_base64_image_for_llm_async(_b64(LOGO_SVG), source="logo")
        return tall, refused, logo

    started = time.monotonic()
    tall, refused, logo = asyncio.run(run())
    assert time.monotonic() - started < 60  # the first call starts the worker
    assert tall is not None and logo is not None
    assert refused == [None, None]
    # Rendered in the worker process: resvg never ran in this one.
    assert resvg_calls == []
    optimized, _mime = tall
    with Image.open(io.BytesIO(optimized)) as opened:
        assert max(opened.size) <= LLM_SVG_BOX


def test_a_render_past_its_time_is_killed_and_the_next_one_works(monkeypatch):
    from app.services import svg_raster

    monkeypatch.setattr(svg_raster, "RASTER_TIMEOUT_SECONDS", 0.001)
    assert asyncio.run(svg_raster.rasterize_svg_async(LOGO_SVG, box=512)) is None
    monkeypatch.setattr(svg_raster, "RASTER_TIMEOUT_SECONDS", 60.0)
    png = asyncio.run(svg_raster.rasterize_svg_async(LOGO_SVG, box=512))
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"


#: A drawing that reaches outside itself: a blue square, then the reference.
def _reaching_out(reference: str) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="64" height="64"><rect width="64" height="64" fill="#00f"/>{reference}</svg>'
    ).encode()


#: The same drawing with nothing outside it: what the rest renders as.
BLUE_SQUARE = _reaching_out("")

REACHING_OUT = {
    "image-path": '<image href="/etc/hosts" width="10" height="10"/>',
    "image-file-url": '<image href="file:///etc/passwd" width="9" height="9"/>',
    "image-xlink": '<image xlink:href="file:///C:/Windows/win.ini" width="9" height="9"/>',
    "image-http": '<image href="http://127.0.0.1:1/x.png" width="9" height="9"/>',
    "image-https": '<image href="https://example.com/a.png" width="9" height="9"/>',
    "use-other-document": '<use href="other.svg#x"/>',
}


@pytest.mark.parametrize("reference", REACHING_OUT.values(), ids=REACHING_OUT.keys())
def test_a_reference_outside_the_document_is_dropped_and_the_rest_renders(reference):
    """resvg follows only in-document references, so on its own it drew nothing
    at a file path or a URL - and read the file a path names. The reference is
    dropped before resvg sees it: the rest renders exactly as without it, and
    nothing is fetched or read."""
    from app.services.svg_raster import rasterize_svg

    assert rasterize_svg(_reaching_out(reference), box=512) == rasterize_svg(BLUE_SQUARE, box=512)


@pytest.mark.parametrize("reference", REACHING_OUT.values(), ids=REACHING_OUT.keys())
def test_strict_rendering_refuses_a_reference_outside_the_document(resvg_calls, reference):
    from app.services.svg_raster import rasterize_svg

    assert rasterize_svg(_reaching_out(reference), box=512, strict=True) is None
    assert resvg_calls == []


def test_markers_are_counted_per_vertex_not_refused():
    """Arrows are how diagrams are drawn: a marker on a few paths renders; a
    marker drawn at thousands of vertices of paths is the multiplication the
    budget refuses."""
    from app.services.svg_raster import rasterize_svg

    assert rasterize_svg(DIAGRAM_SVG, box=512) is not None
    heavy_marker = "".join(f'<rect x="{i % 10}" y="{i // 10}" width="1" height="1"/>' for i in range(100))
    path = "M0 0 " + " ".join(f"L{i % 97} {i % 89}" for i in range(1000))
    markup = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs>'
        f'<marker id="m">{heavy_marker}</marker></defs>'
        f'<path d="{path}" marker-mid="url(#m)" fill="none" stroke="black"/></svg>'
    ).encode()
    assert rasterize_svg(markup, box=512) is None


@pytest.mark.parametrize(
    ("d", "vertices"),
    [
        ("M0 0 H10 H20 H30", 4),
        ("M0 0 H10 20 30", 4),  # one H, three numbers: three vertices, not one and a half
        ("M0 0 V10 20 30 40", 5),
        ("M0 0 L1 1 2 2 3 3", 4),
        ("M0 0 C1 1 2 2 3 3 4 4 5 5 6 6", 3),  # a curve is one vertex, not three
        ("M0 0 A5 5 0 1 1 10 10", 5),  # an arc: up to four segments, one per quadrant
        ("M0 0 L10 0 L10 10 Z", 4),
    ],
)
def test_a_path_is_counted_one_vertex_per_coordinate_set(d, vertices):
    from app.services.svg_raster import _path_vertices

    assert _path_vertices(d) == vertices


def test_a_horizontal_run_under_a_heavy_marker_is_refused():
    """``H`` / ``V`` take one number per vertex: halving a path's numbers, as a
    polyline's points halve, counted 301 vertices where resvg draws a marker at
    601, and admitted a drawing of 60,100 shapes under a 50,000 budget."""
    from app.services.svg_raster import MAX_SVG_EXPANDED_ELEMENTS, rasterize_svg

    marker = "".join(f'<rect x="{i % 10}" y="{i // 10}" width="1" height="1"/>' for i in range(100))
    d = "M0 0 H " + " ".join(str(i % 97) for i in range(600))
    assert 601 * 100 > MAX_SVG_EXPANDED_ELEMENTS > (2 + 600) // 2 * 100
    markup = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs>'
        f'<marker id="m">{marker}</marker></defs>'
        f'<path d="{d}" marker-mid="url(#m)" fill="none" stroke="black"/></svg>'
    ).encode()
    assert rasterize_svg(markup, box=512) is None


def test_no_async_function_prepares_images_on_the_event_loop():
    """The async callers use the async variants, which render in the worker
    process; the sync ones would render inline and block the loop (resvg
    holds the GIL, so even a thread would) - the shared rasterizer and the
    geometry reader included."""
    offenders = []
    sync_names = {
        "prepare_image_bytes_for_llm",
        "prepare_base64_image_for_llm",
        "_rasterize_svg_to_png",
        "rasterize_svg",
        "render_svg",
        "analyze_image_geometry",
    }
    # The async variants hand the sync ones a PNG, never an SVG.
    variants = {"prepare_image_bytes_for_llm_async", "prepare_base64_image_for_llm_async"}
    for path in sorted((BACKEND_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name in variants:
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    func = call.func
                    name = (
                        func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                    )
                    if name in sync_names:
                        offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{call.lineno} {node.name} -> {name}")
    assert not offenders, "\n".join(offenders)


# -- the reference gate is case-insensitive and local-only ------


def test_an_uppercase_url_marker_reference_is_counted():
    """CSS folds the function name, so a marker referenced as ``URL(#m)`` draws
    exactly as ``url(#m)``. The shape budget must count it the same way, or an
    uppercase (or mixed-case) reference smuggles the multiplication past it."""
    from app.services.svg_raster import rasterize_svg

    heavy = "".join(f'<rect x="{i % 10}" y="{i // 10}" width="1" height="1"/>' for i in range(100))
    path = "M0 0 " + " ".join(f"L{i % 97} {i % 89}" for i in range(1000))
    markup = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs>'
        f'<marker id="m">{heavy}</marker></defs>'
        f'<path d="{path}" marker-mid="URL(#m)" fill="none" stroke="black"/></svg>'
    ).encode()
    # 100 shapes x 1000 vertices = 100000 > MAX_SVG_EXPANDED_ELEMENTS: refused.
    assert rasterize_svg(markup, box=512) is None


#: A paint / filter reaching outside the document, and the attribute value
#: resvg falls back to without it (the property's default, as on main: resvg
#: follows only ``url(#id)``, so on its own it drew exactly this).
NON_LOCAL_URLS = {
    "fill-file": ('fill="url(file:///etc/passwd)"', ""),
    "fill-uppercase-http": ('fill="URL(http://127.0.0.1:1/x)"', ""),
    "fill-https-with-fallback": ('fill="url(https://x/y) #0a0"', 'fill=" #0a0"'),
    "style-protocol-relative": ('style="fill:url(//evil.example/x)"', 'style="fill:"'),
    "filter-other-document": ('filter="url(other.svg#f)"', ""),
}


@pytest.mark.parametrize(("attribute", "without"), NON_LOCAL_URLS.values(), ids=NON_LOCAL_URLS.keys())
def test_a_non_local_url_is_stripped_and_the_rest_renders(attribute, without):
    """The local-only contract covers ``url(...)`` targets, not just ``href``,
    whatever attribute or case carries them: a paint / marker / filter reaching
    a file path or a URL is stripped, and the shape renders as it does without
    it. Data URIs and local ``#id`` are kept."""
    from app.services.svg_raster import rasterize_svg

    def markup(attrs: str) -> bytes:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" {attrs}/></svg>'.encode()

    assert rasterize_svg(markup(attribute), box=512) == rasterize_svg(markup(without), box=512)


@pytest.mark.parametrize(("attribute", "_without"), NON_LOCAL_URLS.values(), ids=NON_LOCAL_URLS.keys())
def test_strict_rendering_refuses_a_non_local_url(attribute, _without):
    from app.services.svg_raster import rasterize_svg

    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" {attribute}/></svg>'
    assert rasterize_svg(svg.encode(), box=512, strict=True) is None


def test_a_local_or_inline_url_paint_still_renders():
    from app.services.svg_raster import rasterize_svg

    # A legitimate local paint still renders.
    ok = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><defs>'
        b'<linearGradient id="g"><stop offset="0" stop-color="#000"/></linearGradient></defs>'
        b'<rect width="10" height="10" fill="url(#g)"/></svg>'
    )
    assert rasterize_svg(ok, box=512) is not None


def _red_pixels(png: bytes) -> int:
    with Image.open(io.BytesIO(png)) as opened:
        return sum(
            1 for r, g, b, a in opened.convert("RGBA").get_flattened_data() if r > 200 and g < 60 and b < 60 and a > 200
        )


def test_no_local_file_is_ever_read(tmp_path, monkeypatch):
    """resvg reads the file an ``<image>`` / ``<feImage>`` href names - an
    absolute path, a relative one from the working directory, a ``..``
    traversal, a path behind a scheme it does not know (``foo:bar/../..``),
    a Windows verbatim or device path, a UNC share - and draws it into the PNG
    a model is shown. A red file is planted at every place such an href
    reaches: raw resvg draws it (proving each vector is real here), the
    rasterizer never does, and the rest of the drawing still renders."""
    from app.llm.base import _rasterize_svg_to_png

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    buffer = io.BytesIO()
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(buffer, "PNG")
    (tmp_path / "secret.png").write_bytes(buffer.getvalue())
    (cwd / "local.png").write_bytes(buffer.getvalue())
    monkeypatch.chdir(cwd)
    secret = str(tmp_path / "secret.png")
    hrefs = {
        "absolute": secret,
        "absolute-forward-slashes": secret.replace("\\", "/"),
        "relative": "local.png",
        "traversal": "../secret.png",
        "unknown-scheme-traversal": "foo:bar/../../secret.png",
    }
    if os.name == "nt":
        hrefs["verbatim"] = "\\\\?\\" + secret
        hrefs["device"] = "\\\\.\\" + secret
        hrefs["unc-admin-share"] = "//localhost/" + secret.replace("\\", "/").replace(":", "$", 1)
    read_by_resvg: set[str] = set()
    for label, href in hrefs.items():
        nested = base64.b64encode(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><image href="{href}" width="64" height="64"/></svg>'.encode()
        ).decode()
        for form, reference in {
            "image": f'<image href="{href}" width="64" height="64"/>',
            "xlink-image": f'<image xlink:href="{href}" width="64" height="64"/>',
            "feImage": (
                f'<filter id="f" x="0" y="0" width="1" height="1"><feImage href="{href}" width="64" height="64"/>'
                '</filter><rect width="64" height="64" filter="url(#f)"/>'
            ),
            "nested-svg": f'<image href="data:image/svg+xml;base64,{nested}" width="64" height="64"/>',
        }.items():
            svg = _reaching_out(reference)
            if _red_pixels(bytes(resvg_py.svg_to_bytes(svg_string=svg.decode()))) > 0:
                read_by_resvg.add(label)
            png = _rasterize_svg_to_png(svg, source="file-read")
            assert png is not None, (label, form)
            assert _red_pixels(png) == 0, (label, form)
    # Raw resvg does read these: the test proves what it claims, on this platform.
    assert {"absolute", "absolute-forward-slashes", "relative", "traversal"} <= read_by_resvg
    if os.name == "nt":  # Windows normalises ``..`` lexically, past a scheme it does not know
        assert {"unknown-scheme-traversal", "verbatim", "device"} <= read_by_resvg


def test_a_broken_worker_skips_the_image_and_never_renders_inline(monkeypatch):
    """A worker that dies on a payload (a crash or the address-space cap)
    surfaces as BrokenProcessPool. The image is skipped, never re-run inline:
    an inline render would drop the timeout and RLIMIT_AS this isolation exists
    for, on the very payload that just broke the worker."""
    from concurrent.futures import Future
    from concurrent.futures.process import BrokenProcessPool

    from app.services import svg_raster

    inline_calls: list[bytes] = []
    real_inline = svg_raster.rasterize_svg

    def spy_inline(data, **kwargs):
        inline_calls.append(data)
        return real_inline(data, **kwargs)

    class _DeadPool:
        def submit(self, fn, *args, **kwargs):
            future: Future = Future()
            future.set_exception(BrokenProcessPool("worker died"))
            return future

        def kill_workers(self):
            pass

        def shutdown(self, *args, **kwargs):
            pass

    monkeypatch.setattr(svg_raster, "rasterize_svg", spy_inline)
    monkeypatch.setattr(svg_raster, "_get_executor", lambda: _DeadPool())

    assert asyncio.run(svg_raster.rasterize_svg_async(LOGO_SVG, box=512)) is None
    assert inline_calls == []


# -- the size resvg renders at is read the way resvg reads it -----------------

#: Mermaid's output: a width in percent of the viewBox and no height.
MERMAID_SVG = (
    b'<svg id="mermaid-1" width="100%" xmlns="http://www.w3.org/2000/svg" class="flowchart" '
    b'style="max-width: 625.5px;" viewBox="-8 -8 625.5 314" role="graphics-document document">'
    b'<defs><marker id="arrowhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
    b'orient="auto"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>'
    b'<rect width="120" height="40" rx="5" fill="#ececff" stroke="#9370db"/>'
    b'<path d="M120,20 L300,20" stroke="#333" marker-end="url(#arrowhead)"/></svg>'
)
#: A chart that fills its container: both dimensions in percent.
PERCENT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 640 480">'
    b'<circle cx="320" cy="240" r="200" fill="#0af"/></svg>'
)
#: An icon as Iconify / react-icons write it: sized in em.
EM_ICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24">'
    b'<path fill="#123" d="M12 2 2 22h20z"/></svg>'
)


@pytest.mark.parametrize(
    "svg", [MERMAID_SVG, PERCENT_SVG, EM_ICON_SVG], ids=["mermaid-width-percent", "both-percent", "em-icon"]
)
def test_a_relatively_sized_svg_renders_at_its_own_size_as_before(svg):
    """resvg reads a percentage against the viewBox and an em at 16 px: such an
    SVG fits the box at its own size and renders exactly as before - never
    scaled up to the box (a 626 px Mermaid chart shown to a model at 4096 px)."""
    from app.llm.base import _rasterize_svg_to_png

    before = bytes(resvg_py.svg_to_bytes(svg_string=svg.decode("utf-8")))
    assert _rasterize_svg_to_png(svg, source="pin") == before


@pytest.mark.parametrize(
    "svg",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="6000" height="100"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" width="300"><rect width="6000" height="100"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100px 100px"><rect width="6000" height="100"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100 " height="10" viewBox="0 0 6000 10">'
        b'<rect width="6000" height="10"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100PX" height="10" viewBox="0 0 6000 10">'
        b'<rect width="6000" height="10"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" width="&#160;100" height="10" viewBox="0 0 6000 10">'
        b'<rect width="6000" height="10"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" width="20em" height="1em" font-size="300">'
        b'<rect width="6000" height="300"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="\u0661\u0660\u0660" height="10" viewBox="0 0 6000 10">'
        '<rect width="6000" height="10"/></svg>'.encode(),
    ],
    ids=[
        "no-size",
        "width-only",
        "unit-in-viewbox",
        "trailing-space",
        "upper-case-unit",
        "nbsp",
        "em-font-size",
        "non-ascii-digits",
    ],
)
def test_a_size_resvg_reads_differently_never_escapes_the_box(resvg_calls, svg):
    """With no usable viewBox and a missing width or height, resvg sizes the
    canvas from the CONTENT, which the author controls; and it does not read
    ``"100 "`` or ``"100PX"`` as 100. A size the rasterizer cannot read the way
    resvg does is fitted into the box, never trusted."""
    from app.llm.base import LLM_SVG_BOX, _rasterize_svg_to_png

    assert _rasterize_svg_to_png(svg, source="content-sized") is not None
    assert resvg_calls
    assert max(resvg_calls[-1]) <= LLM_SVG_BOX


_NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'
#: SVGs resvg sizes by their content (no usable viewBox, no definite width AND
#: height) - an agent writing ``<svg xmlns=...>`` with no size, a D3 chart at
#: ``width="100%"`` - each well inside the box.
CONTENT_SIZED = {
    "rect": f'<svg {_NS}><rect x="10" y="10" width="300" height="120" fill="#c33"/></svg>',
    "text": f'<svg {_NS}><text x="5" y="30" font-size="24">Hello world</text></svg>',
    "negative-origin": f'<svg {_NS}><rect x="-50" y="-20" width="300" height="120"/></svg>',
    "width-only": f'<svg {_NS} width="300"><rect x="10" y="10" width="300" height="120"/></svg>',
    "percent": f'<svg {_NS} width="100%" height="100%"><rect x="10" y="10" width="300" height="120"/></svg>',
    "transforms": (
        f'<svg {_NS}><g transform="translate(100,50) rotate(30) scale(1.5)"><rect width="100" height="40"/>'
        '<circle cx="20" cy="80" r="15"/></g></svg>'
    ),
    "use": f'<svg {_NS}><defs><rect id="r" width="100" height="50"/></defs><use href="#r" x="200" y="10"/></svg>',
    "paths": (
        f'<svg {_NS}><path d="m10 10 l390 20 l-350 170 z"/><path d="M0 0 C 0 300, 300 300, 300 0"/>'
        '<path d="M100 200 A50 50 0 1 1 300 200" fill="none" stroke="#000"/></svg>'
    ),
    "arrow-markers": (
        f'<svg {_NS}><defs><marker id="m" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">'
        '<path d="M0,0 L10,5 L0,10 z"/></marker></defs><rect x="20" y="20" width="160" height="60" rx="8" '
        'fill="#e8f4ff" stroke="#07c"/><text x="100" y="55" text-anchor="middle">Node</text>'
        '<path d="M180,50 L260,50" stroke="#333" marker-end="url(#m)"/>'
        '<rect x="260" y="20" width="160" height="60" rx="8" fill="#e8f4ff" stroke="#07c"/></svg>'
    ),
    "empty": f"<svg {_NS}/>",
}


@pytest.mark.parametrize("svg", CONTENT_SIZED.values(), ids=CONTENT_SIZED.keys())
def test_a_content_sized_svg_renders_at_the_size_resvg_gives_it(svg):
    """resvg sizes such an SVG by the far corner of what it draws. When that
    provably fits the box it renders exactly as before - never scaled up to
    4096 px (a 420 x 80 diagram with no size shown to a model at 4096 x 781)."""
    from app.llm.base import _rasterize_svg_to_png

    assert _rasterize_svg_to_png(svg.encode(), source="content") == bytes(resvg_py.svg_to_bytes(svg_string=svg))


@pytest.mark.parametrize("svg", CONTENT_SIZED.values(), ids=CONTENT_SIZED.keys())
def test_the_content_bound_is_never_smaller_than_what_resvg_draws(svg):
    """The bound decides whether a render may take the content's size: it must
    never read less than the canvas resvg sizes from that content."""
    import defusedxml.ElementTree as DefusedET

    from app.services.svg_raster import _content_extent

    with Image.open(io.BytesIO(bytes(resvg_py.svg_to_bytes(svg_string=svg)))) as rendered:
        width, height = rendered.size
    extent = _content_extent(DefusedET.fromstring(svg))
    assert extent is not None
    assert extent[0] + 1 >= width and extent[1] + 1 >= height  # resvg rounds the far corner up


@pytest.mark.parametrize(
    "svg",
    [
        f'<svg {_NS}><style>rect {{ transform: scale(40) }}</style><rect width="100" height="100"/></svg>',
        f'<svg {_NS}><rect x="10%" y="0" width="100" height="100"/></svg>',
        f'<svg {_NS}><text x="0" y="20" dx="5000">spread</text></svg>',
        f'<svg {_NS}><g font-size="3em"><text x="0" y="20">em</text></g></svg>',
        f'<svg {_NS} transform="scale(40)"><rect width="100" height="100"/></svg>',
    ],
    ids=["stylesheet", "percent-length", "text-offsets", "relative-font", "root-transform"],
)
def test_content_it_cannot_bound_is_fitted_into_the_box(resvg_calls, svg):
    import defusedxml.ElementTree as DefusedET

    from app.llm.base import LLM_SVG_BOX, _rasterize_svg_to_png
    from app.services.svg_raster import _content_extent

    assert _content_extent(DefusedET.fromstring(svg)) is None
    assert _rasterize_svg_to_png(svg.encode(), source="unbounded") is not None
    assert max(resvg_calls[-1]) <= LLM_SVG_BOX


def test_content_outgrowing_its_bound_is_fitted_after_all(monkeypatch, resvg_calls):
    """Defense in depth: were a font to draw wider than the glyph allowance, the
    render past the box is replaced by the box-fitted one."""
    from app.llm.base import LLM_SVG_BOX, _rasterize_svg_to_png
    from app.services import svg_raster

    monkeypatch.setattr(svg_raster, "_content_extent", lambda _root: (10.0, 10.0))
    svg = f'<svg {_NS}><rect width="6000" height="100"/></svg>'.encode()
    png = _rasterize_svg_to_png(svg, source="outgrown")
    assert png is not None
    with Image.open(io.BytesIO(png)) as rendered:
        assert max(rendered.size) <= LLM_SVG_BOX


_SHADOW = '<filter id="s"><feDropShadow dx="2" dy="2" stdDeviation="2" flood-opacity=".3"/></filter>'
_LABELS = "<style>.t{font:14px sans-serif;fill:#333}.box{fill:#e8f4ff;stroke:#07c}</style>"
_ARROW = (
    '<marker id="a" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">'
    '<path d="M0,0 L10,5 L0,10 z"/></marker>'
)
#: What an agent draws with no size at all, with shadows and a stylesheet: resvg
#: sizes each by its content. Each was refused (its filters charged at the box's
#: scale) or scaled up to the 4096 px box (a stylesheet left it unbounded).
CONTENT_SIZED_STYLED = {
    "shadow-on-a-rect": f'<svg {_NS}><defs>{_SHADOW}</defs><rect x="10" y="10" width="100" height="100" fill="#39f" filter="url(#s)"/></svg>',
    "shadowed-cards": f'<svg {_NS}><defs>{_SHADOW}</defs>'
    + "".join(f'<rect x="{20 + 130 * i}" y="20" width="110" height="70" rx="6" fill="#fff" filter="url(#s)"/>' for i in range(4))
    + "</svg>",
    "shadow-through-a-use": f'<svg {_NS}><defs>{_SHADOW}<g id="card"><rect width="120" height="50" rx="6" fill="#fff" '
    'stroke="#999" filter="url(#s)"/></g></defs><use href="#card" x="10" y="10"/><use href="#card" x="150" y="10"/></svg>',
    "label-stylesheet": f'<svg {_NS}>{_LABELS}<rect class="box" x="10" y="10" width="200" height="60" rx="8"/>'
    '<text class="t" x="30" y="45">Hello</text></svg>',
    "stylesheet-and-shadow": f'<svg {_NS}>{_LABELS}<defs>{_SHADOW}</defs><rect class="box" x="10" y="10" width="200" '
    'height="60" rx="8" filter="url(#s)"/><text class="t" x="30" y="45">Card</text></svg>',
    "chart-at-100-percent": f'<svg {_NS} width="100%" height="300">{_LABELS}'
    + "".join(f'<rect class="box" x="{30 + 40 * i}" y="{260 - 20 * (i % 7)}" width="30" height="{20 * (i % 7) + 20}"/>' for i in range(12))
    + '<text class="t" x="30" y="295">Q1</text></svg>',
    "imported-font": f'<svg {_NS}><style>@import url(https://fonts.example.com/inter.css);text{{font-family:Inter}}</style>'
    '<rect x="5" y="5" width="240" height="50" fill="#eee"/><text x="15" y="35">Inter</text></svg>',
    "arrow-from-a-stylesheet": f'<svg {_NS}><style>.e{{stroke:#333;marker-end:url(#a)}}</style><defs>{_ARROW}</defs>'
    '<rect x="20" y="20" width="100" height="40" fill="#def"/><path class="e" d="M120,40 L200,40"/>'
    '<rect x="200" y="20" width="100" height="40" fill="#def"/></svg>',
    "icon-with-a-root-style": f'<svg {_NS} width="1em" height="1em" viewBox="0 0 24 24" '
    'style="-webkit-transform: rotate(360deg); transform: rotate(360deg);"><path fill="currentColor" d="M12 2L2 22h20z"/></svg>',
    "icon-with-a-stylesheet": f'<svg {_NS} width="1.5em" height="1.5em" viewBox="0 0 24 24"><style>.a{{fill:#0a7}}</style>'
    '<path class="a" d="M12 2L2 22h20z"/></svg>',
}


@pytest.mark.parametrize("svg", CONTENT_SIZED_STYLED.values(), ids=CONTENT_SIZED_STYLED.keys())
def test_a_styled_or_filtered_svg_renders_at_the_size_resvg_gives_it(svg):
    """The filters of a content-sized SVG are charged at the scale it is drawn at
    (1:1 once its content is bounded), CSS that only paints leaves its content
    bounded, and only a font size can change what an ``em`` reads: each renders
    byte for byte as before."""
    from app.llm.base import _rasterize_svg_to_png

    assert _rasterize_svg_to_png(svg.encode(), source="styled") == bytes(resvg_py.svg_to_bytes(svg_string=svg))


@pytest.mark.parametrize(
    "name", ["shadow-on-a-rect", "shadowed-cards", "shadow-through-a-use", "stylesheet-and-shadow"]
)
def test_the_geometry_of_a_content_sized_svg_with_filters_is_read_as_before(name):
    """The logo geometry fits every SVG into its box, by a factor only resvg's own
    content size gives: read from a render at that size, the fitted render's
    filters are charged at the scale it is drawn at - and the geometry is the one
    the bounded read gives (the sanitized markup fitted into the box), instead of none."""
    from app.services.image_geometry import ANALYSIS_LONG_EDGE, _geometry_markup, _measure, analyze_image_geometry

    svg = CONTENT_SIZED_STYLED[name].encode()
    clean = _geometry_markup(svg)
    assert clean is not None
    fitted = resvg_py.svg_to_bytes(svg_string=clean.decode("utf-8"), width=ANALYSIS_LONG_EDGE, height=ANALYSIS_LONG_EDGE)
    assert analyze_image_geometry(svg, "image/svg+xml") == _measure(bytes(fitted), ("PNG",)) is not None


#: Markers a shape inherits, and what CSS adds to what is drawn.
MARKED_OR_STYLED = {
    "marker-on-a-group": f'<svg {_NS}><defs>{_ARROW}</defs><g marker-end="url(#a)" stroke="#333">'
    '<path d="M10,40 L200,40"/><path d="M10,80 L200,80"/></g></svg>',
    "marker-on-a-use": f'<svg {_NS}><defs>{_ARROW}<path id="p" d="M10,40 L200,40" stroke="#333"/></defs>'
    '<use href="#p" marker-end="url(#a)"/></svg>',
    "marker-from-a-stylesheet": CONTENT_SIZED_STYLED["arrow-from-a-stylesheet"],
    "stroke-width-from-a-stylesheet": f'<svg {_NS}><style>.e{{stroke:#333;stroke-width:4;marker-end:url(#b)}}</style>'
    '<defs><marker id="b" markerWidth="40" markerHeight="40" refX="2" refY="20" orient="auto" viewBox="0 0 10 10">'
    '<path d="M0,0 L10,5 L0,10 z"/></marker></defs><path class="e" d="M10,40 L200,40"/></svg>',
    "font-size-from-a-stylesheet": f'<svg {_NS}><style>text{{font-size:28px;font-family:sans-serif}}</style>'
    '<text x="10" y="40">Wide label here</text></svg>',
    "font-shorthand": f'<svg {_NS}><style>.t{{font:bold 22px/1.2 Arial,sans-serif}}</style>'
    '<text class="t" x="5" y="30">Service B</text></svg>',
}


@pytest.mark.parametrize("svg", MARKED_OR_STYLED.values(), ids=MARKED_OR_STYLED.keys())
def test_the_content_bound_covers_inherited_markers_and_what_css_adds(svg):
    """A marker named on a group, a ``<use>`` or in CSS is drawn on the shapes
    below it, and CSS may enlarge a font or a stroke-width marker: the bound
    takes each everywhere it may apply, never less than resvg draws."""
    import defusedxml.ElementTree as DefusedET

    from app.services.svg_raster import _content_extent

    with Image.open(io.BytesIO(bytes(resvg_py.svg_to_bytes(svg_string=svg)))) as rendered:
        width, height = rendered.size
    extent = _content_extent(DefusedET.fromstring(svg))
    assert extent is not None
    assert extent[0] + 1 >= width and extent[1] + 1 >= height


@pytest.mark.parametrize(
    "size",
    ['width="100 " height="50"', 'width="100PX" height="50"', 'width="abc" height="50"'],
    ids=["trailing-space", "upper-case-unit", "garbage"],
)
def test_a_size_resvg_cannot_read_is_read_as_100_percent_as_resvg_does(size):
    """resvg reads a width it cannot parse as absent - 100% of the viewBox."""
    from app.llm.base import _rasterize_svg_to_png

    svg = f'<svg {_NS} {size} viewBox="0 0 200 100"><rect width="200" height="100"/></svg>'
    assert _rasterize_svg_to_png(svg.encode(), source="unread") == bytes(resvg_py.svg_to_bytes(svg_string=svg))


_RED = '<rect width="40" height="20" fill="#f00"/>'
#: Markup resvg reads and expat refuses as it stands: each renders as before.
RESVG_READS = {
    "repeated-default-xmlns": f'<svg {_NS} xmlns="http://www.w3.org/2000/svg" width="40" height="20">{_RED}</svg>'.encode(),
    "repeated-xmlns-svg-first": f'<svg {_NS} xmlns="http://example.com/x" width="40" height="20">{_RED}</svg>'.encode(),
    "whitespace-before-declaration": f'\n  <?xml version="1.0"?><svg {_NS} width="40" height="20">{_RED}</svg>'.encode(),
    "latin-1-byte-undeclared": f'<svg {_NS} width="40" height="20">{_RED}<text y="15">caf\xe9</text></svg>'.encode("latin-1"),
    "no-namespace-content-sized": b'<svg><rect x="10" y="10" width="300" height="120" fill="#f00"/></svg>',
}


@pytest.mark.parametrize("svg", RESVG_READS.values(), ids=RESVG_READS.keys())
def test_markup_resvg_reads_renders_as_it_did(svg):
    """resvg renders the UTF-8 text an invalid byte is replaced in, reads past
    whitespace before the declaration, keeps the first of a repeated default
    ``xmlns`` and draws an un-namespaced SVG: the bounds check parses exactly
    that text, so each renders byte for byte as before."""
    from app.llm.base import _rasterize_svg_to_png

    before = bytes(resvg_py.svg_to_bytes(svg_string=svg.decode("utf-8", errors="replace")))
    assert _rasterize_svg_to_png(svg, source="resvg-reads") == before


_SVG_ONLY = 'xmlns="http://www.w3.org/2000/svg"'


@pytest.mark.parametrize(
    "svg",
    [
        f'<svg {_SVG_ONLY} width="40" height="20">{_RED}<text y="15">a&nbsp;b</text></svg>',
        f'<svg {_SVG_ONLY} width="40" height="20">{_RED}<text y="15">R&D</text></svg>',
        f'<svg {_SVG_ONLY} width="40" height="20"><use xlink:href="#r"/>{_RED}</svg>',
        f'<svg {_SVG_ONLY} width="40" width="50" height="20">{_RED}</svg>',
    ],
    ids=["html-entity", "bare-ampersand", "undeclared-prefix", "repeated-attribute"],
)
def test_markup_resvg_refuses_too_stays_refused(svg):
    """Not a regression: resvg refuses these on its own, so they never rendered."""
    from app.llm.base import _rasterize_svg_to_png

    with pytest.raises(Exception):  # noqa: B017 - resvg's own refusal, whatever its type
        resvg_py.svg_to_bytes(svg_string=svg)
    assert _rasterize_svg_to_png(svg.encode(), source="resvg-refuses") is None


# -- the shape budget counts what resvg draws again ---------------------------


def _marker(shapes: int, ident: str = "m") -> str:
    body = "".join(f'<rect x="{i % 10}" y="{i // 10}" width="1" height="1"/>' for i in range(shapes))
    return f'<marker id="{ident}">{body}</marker>'


def _path_data(vertices: int) -> str:
    return "M0 0 " + " ".join(f"L{i % 97} {i % 89}" for i in range(vertices))


def _document(body: str) -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{body}</svg>'.encode()


@pytest.mark.parametrize(
    "svg",
    [
        _use_fan(5).replace(b' id="', b' xml:id="'),
        _document(
            '<defs><marker id="m"><rect width="1" height="1"/></marker>'
            f"{_marker(200)}</defs>"
            f'<path d="{_path_data(1000)}" marker-mid="url(#m)"/>'
        ),
        _document(f'<defs>{_marker(100)}</defs><g marker-mid="url(#m)"><path d="{_path_data(1000)}"/></g>'),
        _document(
            f'<defs>{_marker(100)}<path id="p" d="{_path_data(1000)}"/></defs><use href="#p" marker-mid="url(#m)"/>'
        ),
        _document(
            '<defs><pattern id="p" width="10" height="10">'
            + "".join(f'<circle cx="{i % 10}" cy="{i // 10}" r="1"/>' for i in range(100))
            + '</pattern></defs><g fill="url(#p)">'
            + "".join(f'<rect x="{i % 100}" y="{i // 100}" width="1" height="1"/>' for i in range(1000))
            + "</g>"
        ),
        _document(
            f"<defs>{_marker(50)}</defs>"
            + "".join(f'<rect x="{i % 100}" width="1" height="1" marker-mid="url(#m)"/>' for i in range(700))
        ),
    ],
    ids=["xml-id-use-fan", "duplicate-id", "marker-on-group", "marker-on-use", "pattern-on-group", "markers-on-rects"],
)
def test_the_shape_budget_counts_what_resvg_draws_again(resvg_calls, svg):
    """resvg resolves ``xml:id`` like ``id`` and draws the LAST element of a
    duplicated id for a ``url()``; markers and paint servers are inherited, so
    one named on a group or a ``<use>`` is drawn for every shape below it; and
    markers go on every shape, a rect included. Each of these draws at least
    twice the budget as resvg draws it, yet was counted under it: refused."""
    from app.services.svg_raster import rasterize_svg

    assert rasterize_svg(svg, box=512) is None
    assert resvg_calls == []


def _use_chain(links: int) -> bytes:
    """``<use>`` of a group holding a ``<use>`` of a group ... ``links`` deep."""
    groups = ['<g id="c0"><rect width="1" height="1"/></g>']
    groups += [f'<g id="c{i}"><use href="#c{i - 1}"/></g>' for i in range(1, links + 1)]
    return _document("<defs>" + "".join(groups) + f'</defs><use href="#c{links}"/>')


@pytest.mark.parametrize(
    "svg",
    [_document("<g>" * 200 + '<rect width="1" height="1"/>' + "</g>" * 200), _use_chain(80)],
    ids=["nested-groups", "use-chain"],
)
def test_nesting_past_the_depth_limit_is_refused(resvg_calls, svg):
    """resvg recurses once per level - through a ``<use>`` too - and a few dozen
    levels overflow a 2 MB thread stack: the rendering process dies. Nesting
    no real SVG reaches is refused before resvg sees it."""
    from app.services.svg_raster import rasterize_svg

    assert rasterize_svg(svg, box=512) is None
    assert resvg_calls == []


def test_a_deep_but_real_nesting_still_renders():
    from app.services.svg_raster import rasterize_svg

    assert rasterize_svg(_document("<g>" * 40 + '<rect width="9" height="9"/>' + "</g>" * 40), box=64) is not None


def test_a_large_flat_document_is_not_taken_for_a_multiplication():
    """The budget refuses what MULTIPLIES: a chart with more elements of its own
    than the budget renders as it always did - its cost is the file's, which
    ``MAX_SVG_BYTES`` bounds - while the same count reached by reuse is refused."""
    from app.services.svg_raster import MAX_SVG_EXPANDED_ELEMENTS, rasterize_svg

    count = MAX_SVG_EXPANDED_ELEMENTS + 5_000
    flat = _document("".join(f'<rect x="{i % 100}" y="{i // 1000}" width="1" height="1"/>' for i in range(count)))
    assert rasterize_svg(flat, box=64) is not None
    reused = _document(
        '<defs><g id="g">'
        + "".join(f'<rect x="{i}" width="1" height="1"/>' for i in range(100))
        + "</g></defs>"
        + '<use href="#g"/>' * (count // 100)
    )
    assert rasterize_svg(reused, box=64) is None


def test_a_group_sharing_one_arrow_head_still_renders():
    """The inherited count is a count, not a ban: a diagram whose group of
    connectors names one arrow head renders, and a local link is no draw."""
    from app.services.svg_raster import rasterize_svg

    arrow = '<marker id="a" markerWidth="6" markerHeight="6" refX="5" refY="3"><path d="M0,0 L6,3 L0,6 z"/></marker>'
    connectors = "".join(f'<path d="M{i * 10},10 L{i * 10},90 L{i * 10 + 5},95"/>' for i in range(30))
    markup = _document(
        f'<defs>{arrow}</defs><g marker-end="url(#a)" stroke="#333" fill="none">{connectors}</g>'
        '<a href="#a"><text x="10" y="10">legend</text></a>'
    )
    assert rasterize_svg(markup, box=512) is not None


# -- the worker process ends with its render, its caller and its parent -------


def _process_gone(pid: int) -> bool:
    """Whether *pid* has exited (a zombie, or one being reaped, counts as exited)."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value != 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    if Path("/proc/self").exists():
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}
        except (FileNotFoundError, ProcessLookupError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        time.sleep(0.1)
    return _process_gone(pid)


def test_a_cancelled_render_takes_its_worker_with_it(monkeypatch, tmp_path):
    """A caller that goes away mid-render (a cancelled chat turn, the geometry
    backfill's budget) must not leave the worker rendering with no timeout
    left to stop it - nor the next render queued behind it."""
    from app.services import svg_raster
    from tests import svg_raster_worker_helpers

    monkeypatch.setattr(svg_raster, "_render_in_worker", svg_raster_worker_helpers.hang)
    marker = tmp_path / "worker.pid"

    async def run() -> int:
        task = asyncio.ensure_future(svg_raster.rasterize_svg_async(str(marker).encode(), box=64, timeout=300))
        deadline = time.monotonic() + 90
        while not (marker.exists() and marker.read_text()):
            assert time.monotonic() < deadline, "the worker never started the render"
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int(marker.read_text())

    pid = asyncio.run(run())
    assert svg_raster._executor is None
    assert _gone_within(pid, 30), "the render kept its worker busy after its caller was cancelled"


def test_a_slow_worker_start_does_not_count_against_the_render(monkeypatch):
    """A fresh worker starts (spawn, imports) under its own bound: on a loaded
    host that can take seconds, and a render timed from the submit would be
    killed before it began - worker after worker."""
    from app.services import svg_raster
    from tests import svg_raster_worker_helpers

    monkeypatch.setattr(svg_raster, "_start_worker", svg_raster_worker_helpers.slow_start)
    monkeypatch.setattr(svg_raster, "RASTER_TIMEOUT_SECONDS", 1.0)
    png = asyncio.run(svg_raster.rasterize_svg_async(LOGO_SVG, box=64))
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_worker_that_died_between_renders_is_replaced():
    """A worker killed while idle (the OOM killer, an operator) leaves a broken
    pool behind: the next render starts a new worker instead of refusing every
    SVG for the life of the process."""
    from app.services import svg_raster

    async def run() -> bytes | None:
        assert await svg_raster.rasterize_svg_async(LOGO_SVG, box=64)
        executor = svg_raster._executor
        next(iter(executor._processes.values())).kill()
        deadline = time.monotonic() + 30
        while not executor._broken:
            assert time.monotonic() < deadline, "the pool never noticed its worker died"
            await asyncio.sleep(0.05)
        # Other boxes, so the cache answers neither call: both reach the pool.
        await svg_raster.rasterize_svg_async(LOGO_SVG, box=65)  # meets the broken pool
        return await svg_raster.rasterize_svg_async(LOGO_SVG, box=66)

    assert asyncio.run(run()) is not None


def test_a_render_still_queued_past_its_bound_is_skipped(monkeypatch):
    """One worker serves the whole process, so renders queue: a burst of slow
    SVGs - hostile or not - delays a caller by a bounded wait, never by the
    whole queue. Past it the image is skipped without reaching the worker."""
    from app.services import svg_raster

    monkeypatch.setattr(svg_raster, "RASTER_QUEUE_TIMEOUT_SECONDS", 0.05)
    started: list[bool] = []
    monkeypatch.setattr(svg_raster, "_get_executor", lambda: started.append(True))

    async def run() -> bytes | None:
        gate = svg_raster._gate()
        await gate.acquire()  # a render ahead of this one that does not finish
        try:
            return await svg_raster.rasterize_svg_async(LOGO_SVG, box=64)
        finally:
            gate.release()

    assert asyncio.run(run()) is None
    assert started == []


def test_a_worker_whose_parent_is_killed_exits_with_it():
    """uvicorn's supervisor kills a worker whose event loop stopped answering,
    and the OOM killer kills whatever it picks: the SVG worker such a process
    started must go with it, not wait on its task queue for good."""
    script = (
        "import asyncio, time\n"
        "from app.services import svg_raster\n"
        "async def main():\n"
        f"    assert await svg_raster.rasterize_svg_async({LOGO_SVG!r}, box=64, timeout=120)\n"
        "    print(next(iter(svg_raster._executor._processes)), flush=True)\n"
        "asyncio.run(main())\n"
        "time.sleep(300)\n"
    )
    parent = subprocess.Popen(  # noqa: S603 - this interpreter running the constant script above
        [sys.executable, "-c", script], cwd=BACKEND_ROOT, stdout=subprocess.PIPE, text=True
    )
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.append(parent.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout=120)
    try:
        assert lines and lines[0].strip().isdigit(), "the parent never started its worker"
        worker = int(lines[0])
    finally:
        parent.kill()
        parent.wait(timeout=30)
    try:
        assert _gone_within(worker, 30), "the SVG worker outlived the process that started it"
    finally:
        if not _process_gone(worker):
            with contextlib.suppress(ProcessLookupError):
                os.kill(worker, getattr(signal, "SIGKILL", signal.SIGTERM))


def test_a_resvg_panic_is_no_image(monkeypatch):
    """resvg is Rust: a panic surfaces as pyo3's PanicException, a
    BaseException. An inline render reports no image instead of raising
    through its caller; anything else that is not an Exception still raises."""
    from app.services.svg_raster import rasterize_svg

    panic = type("PanicException", (BaseException,), {})

    def boom(**_kwargs):
        raise panic("called `Option::unwrap()` on a `None` value")

    monkeypatch.setattr(resvg_py, "svg_to_bytes", boom)
    assert rasterize_svg(LOGO_SVG, box=64) is None

    def interrupt(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(resvg_py, "svg_to_bytes", interrupt)
    with pytest.raises(KeyboardInterrupt):
        rasterize_svg(LOGO_SVG, box=64)


# -- absolute units are read at 96 dpi -------------------------

#: matplotlib's default output: a 6 x 4 in figure written in points.
POINTS_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="432pt" height="288pt" viewBox="0 0 432 288">'
    b'<rect width="432" height="288" fill="#eeeeee"/><path d="M40 250 L400 40" stroke="#1f77b4" stroke-width="2"/></svg>'
)
#: Inkscape's default document: millimetres.
MILLIMETRES_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="120mm" viewBox="0 0 210 120">'
    b'<rect x="20" y="20" width="170" height="80" rx="8" fill="#0fbcff"/></svg>'
)
#: A poster in inches, 5760 x 960 px at 96 dpi: wider than the box.
INCHES_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="60in" height="10in" viewBox="0 0 60 10">'
    b'<rect width="60" height="10" fill="#123456"/></svg>'
)


@pytest.mark.parametrize(
    ("raw", "px"),
    [("96px", 96.0), ("1in", 96.0), ("2.54cm", 96.0), ("25.4mm", 96.0), ("72pt", 96.0), ("6pc", 96.0), ("10", 10.0)],
)
def test_an_absolute_unit_is_read_at_96_dpi(raw, px):
    from defusedxml import ElementTree

    from app.services.svg_raster import intrinsic_size

    root = ElementTree.fromstring(f'<svg xmlns="http://www.w3.org/2000/svg" width="{raw}" height="{raw}"/>')
    width, height = intrinsic_size(root)
    assert width == pytest.approx(px) and height == pytest.approx(px)


@pytest.mark.parametrize(
    ("svg", "size"), [(POINTS_SVG, (576, 384)), (MILLIMETRES_SVG, (794, 454))], ids=["points", "millimetres"]
)
def test_a_unit_sized_svg_renders_at_its_96_dpi_size(svg, size):
    """resvg reads pt / mm / in only with a dpi: without one - the direct call
    the chat made before - such an SVG, matplotlib's and Inkscape's default
    output, was never shown to the model. At 96 dpi it renders at the size a
    browser shows it, exactly as resvg renders it at that dpi."""
    from app.llm.base import _rasterize_svg_to_png

    with pytest.raises(ValueError, match="invalid size"):
        resvg_py.svg_to_bytes(svg_string=svg.decode("utf-8"))
    png = _rasterize_svg_to_png(svg, source="unit-sized")
    assert png == bytes(resvg_py.svg_to_bytes(svg_string=svg.decode("utf-8"), dpi=96))
    with Image.open(io.BytesIO(png)) as opened:
        assert opened.size == size


def test_a_unit_sized_svg_larger_than_the_box_is_fitted_into_it(resvg_calls):
    from app.llm.base import LLM_SVG_BOX, _rasterize_svg_to_png

    assert _rasterize_svg_to_png(INCHES_SVG, source="poster") is not None
    assert resvg_calls == [(LLM_SVG_BOX, 683)]


def test_a_unit_sized_svg_renders_off_the_loop_too():
    from app.services.svg_raster import rasterize_svg_async

    png = asyncio.run(rasterize_svg_async(POINTS_SVG, box=CHAT_BOX, keep_native_size=True))
    assert png == bytes(resvg_py.svg_to_bytes(svg_string=POINTS_SVG.decode("utf-8"), dpi=96))


_SHAPES = '<rect x="4" y="4" width="30" height="20" fill="#0fbcff"/><circle cx="30" cy="22" r="6" fill="#001bda"/>'
#: Absolute lengths INSIDE a document: resvg-py's 0 dpi reads them as 0 (the
#: stroke and the rect are not drawn, the nested viewport shrinks to 8 x 6 px),
#: 96 dpi draws them as a browser does.
_INNER_UNITS = (
    '<rect x="2" y="2" width="36pt" height="18pt" fill="none" stroke="#c33" stroke-width="1.5mm"/>'
    '<svg x="20" y="10" width="8mm" height="6mm" viewBox="0 0 8 6"><rect width="8" height="6" fill="#0c6"/></svg>'
)
#: SVGs with no absolute length anywhere: sized in px, unitless, by their
#: viewBox, in % of it, by their content, in em - at the default font size or
#: under one the document sets. No glyph is drawn, so the pin holds whatever
#: fonts the host has.
NO_ABSOLUTE_LENGTH = {
    "px": f'<svg {_NS} width="120" height="60">{_SHAPES}</svg>',
    "unitless-viewbox": f'<svg {_NS} width="120" height="60" viewBox="0 0 60 30">{_SHAPES}</svg>',
    "viewbox-only": f'<svg {_NS} viewBox="0 0 60 30">{_SHAPES}</svg>',
    "percent-of-viewbox": f'<svg {_NS} width="50%" height="100%" viewBox="0 0 60 30">{_SHAPES}</svg>',
    "content-sized": f"<svg {_NS}>{_SHAPES}</svg>",
    "em-default-font": f'<svg {_NS} width="4em" height="2em" viewBox="0 0 60 30">{_SHAPES}</svg>',
    "em-under-a-font-attribute": (
        f'<svg {_NS} width="3em" height="1.5em" font-size="20" viewBox="0 0 60 30">{_SHAPES}</svg>'
    ),
    "em-under-a-font-style": (
        f'<svg {_NS} width="1em" height="1em" style="font-size:24px" viewBox="0 0 60 30">{_SHAPES}</svg>'
    ),
    "em-under-a-stylesheet-font": (
        f'<svg {_NS} width="2em" height="1em" viewBox="0 0 60 30">'
        f"<style>svg {{ font-size: 40px }}</style>{_SHAPES}</svg>"
    ),
    "em-under-a-font-keyword": f'<svg {_NS} width="3em" height="2em" font-size="large">{_SHAPES}</svg>',
    "em-under-a-font-percentage": f'<svg {_NS} width="3em" height="2em" font-size="150%">{_SHAPES}</svg>',
    "em-under-a-font-shorthand": (
        f'<svg {_NS} width="3em" height="2em" style="font: bold 30px serif" viewBox="0 0 60 30">{_SHAPES}</svg>'
    ),
}


@pytest.mark.parametrize("svg", list(NO_ABSOLUTE_LENGTH.values()), ids=list(NO_ABSOLUTE_LENGTH))
def test_an_svg_with_no_absolute_length_renders_byte_for_byte_as_before(svg):
    """With no pt / mm / in length in it, an SVG draws the same pixels at 96
    dpi as at resvg-py's default 0 dpi: the model gets, on both paths, the very
    PNG a direct resvg-py call at its defaults gives. An em size under a
    font size the document sets is rendered at its own size, not fitted to
    4096 px."""
    from app.llm.base import _rasterize_svg_to_png
    from app.services.svg_raster import rasterize_svg_async

    before = bytes(resvg_py.svg_to_bytes(svg_string=svg))
    inline = _rasterize_svg_to_png(svg.encode(), source="pin")
    in_worker = asyncio.run(rasterize_svg_async(svg.encode(), box=CHAT_BOX, keep_native_size=True))
    expected = hashlib.sha256(before).hexdigest()
    assert inline is not None and hashlib.sha256(inline).hexdigest() == expected
    assert in_worker is not None and hashlib.sha256(in_worker).hexdigest() == expected


#: Absolute lengths inside an SVG sized in px, by its viewBox, by its content,
#: or in em under a font size in px - SVGs that rendered at 0 dpi.
INNER_ABSOLUTE_LENGTHS = {
    "px": f'<svg {_NS} width="120" height="60">{_SHAPES}{_INNER_UNITS}</svg>',
    "unitless-viewbox": f'<svg {_NS} width="120" height="60" viewBox="0 0 60 30">{_SHAPES}{_INNER_UNITS}</svg>',
    "viewbox-only": f'<svg {_NS} viewBox="0 0 60 30">{_SHAPES}{_INNER_UNITS}</svg>',
    "content-sized": f"<svg {_NS}>{_SHAPES}{_INNER_UNITS}</svg>",
    "em-under-a-font-shorthand": (
        f'<svg {_NS} width="3em" height="2em" style="font: bold 30px serif" viewBox="0 0 60 30">'
        f"{_SHAPES}{_INNER_UNITS}</svg>"
    ),
}


@pytest.mark.parametrize("svg", list(INNER_ABSOLUTE_LENGTHS.values()), ids=list(INNER_ABSOLUTE_LENGTHS))
def test_an_absolute_length_inside_an_svg_is_read_at_96_dpi(svg):
    """An absolute length inside a px-sized, unitless or viewBox-sized SVG is
    read as a browser reads it: the model gets, on both paths, byte for
    byte what resvg draws at 96 dpi - where resvg-py's default 0 dpi read it as
    0 and drew the SVG without it."""
    from app.llm.base import _rasterize_svg_to_png
    from app.services.svg_raster import rasterize_svg_async

    at_96_dpi = bytes(resvg_py.svg_to_bytes(svg_string=svg, dpi=96))
    assert at_96_dpi != bytes(resvg_py.svg_to_bytes(svg_string=svg))
    assert _rasterize_svg_to_png(svg.encode(), source="inner-units") == at_96_dpi
    assert asyncio.run(rasterize_svg_async(svg.encode(), box=CHAT_BOX, keep_native_size=True)) == at_96_dpi


def _ink_size(png: bytes) -> tuple[int, int] | None:
    """Width and height of what a PNG draws (its non-transparent pixels), or ``None``."""
    with Image.open(io.BytesIO(png)) as opened:
        box = opened.convert("RGBA").getchannel("A").getbbox()
    return None if box is None else (box[2] - box[0], box[3] - box[1])


#: One absolute length inside a 120 x 60 px SVG, and the pixels it covers at
#: 96 dpi, anti-aliased edges included.
INNER_LENGTH_DRAWN = {
    "stroke-width-1mm": ('<line x1="10" y1="30" x2="110" y2="30" stroke="#c33" stroke-width="1mm"/>', (100, 4)),
    "nested-viewport-8mm": (
        '<svg x="10" y="10" width="8mm" height="6mm" viewBox="0 0 8 6"><rect width="8" height="6" fill="#0c6"/></svg>',
        (31, 23),
    ),
    "rect-36pt": ('<rect x="10" y="10" width="36pt" height="18pt" fill="#123456"/>', (48, 24)),
}


@pytest.mark.parametrize(("shape", "size"), list(INNER_LENGTH_DRAWN.values()), ids=list(INNER_LENGTH_DRAWN))
def test_an_absolute_length_inside_a_px_sized_svg_is_drawn_at_its_css_size(shape, size):
    """1 mm is 96 / 25.4 px and 1 pt is 96 / 72 px, as in every browser: a 1 mm
    stroke is ~3.8 px thick, a nested 8 x 6 mm viewport ~30 x 23 px, a 36 x 18 pt
    rect 48 x 24 px - measured on the PNG the model gets, where resvg-py's
    default 0 dpi drew them at another size or not at all."""
    from app.llm.base import _rasterize_svg_to_png

    svg = f'<svg {_NS} width="120" height="60">{shape}</svg>'
    png = _rasterize_svg_to_png(svg.encode(), source="inner-length")
    assert png is not None and _ink_size(png) == size
    assert _ink_size(bytes(resvg_py.svg_to_bytes(svg_string=svg))) != size


def test_a_font_size_in_pt_inside_a_px_sized_svg_is_drawn_at_its_css_size():
    """``font-size="12pt"`` is 16 px at 96 dpi: the label is drawn exactly as a
    ``font-size="16px"`` one, byte for byte - where resvg-py's default 0 dpi
    drew no text at all (on a host with no font, neither label is drawn and the
    two are the same PNG still)."""
    from app.llm.base import _rasterize_svg_to_png

    label = f'<svg {_NS} width="160" height="40"><text x="4" y="28" font-size="{{}}" fill="#c33">Label</text></svg>'
    in_pt, in_px = label.format("12pt"), label.format("16px")
    blank = bytes(resvg_py.svg_to_bytes(svg_string=f'<svg {_NS} width="160" height="40"/>'))
    assert bytes(resvg_py.svg_to_bytes(svg_string=in_pt)) == blank
    assert _rasterize_svg_to_png(in_pt.encode(), source="pt-label") == bytes(resvg_py.svg_to_bytes(svg_string=in_px))


#: An em / ex size under a font size in an absolute unit: 0 at resvg-py's 0 dpi,
#: so these never rendered.
EM_UNDER_AN_ABSOLUTE_FONT = {
    "font-attribute-pt": (
        f'<svg {_NS} width="4em" height="2em" font-size="12pt" viewBox="0 0 60 30">{_SHAPES}</svg>',
        (64, 32),
    ),
    "font-style-pt": (f'<svg {_NS} width="4em" height="2em" style="font-size:9pt">{_SHAPES}</svg>', (48, 24)),
    "stylesheet-font-mm": (
        f'<svg {_NS} width="4em" height="2em"><style>svg{{font-size:5mm}}</style>{_SHAPES}</svg>',
        (76, 38),
    ),
    "ex-under-a-font-in": (
        f'<svg {_NS} width="8ex" height="4ex" font-size="0.25in" viewBox="0 0 60 30">{_SHAPES}</svg>',
        (96, 48),
    ),
    "font-shorthand-pt": (
        f'<svg {_NS} width="4em" height="2em" style="font: italic 12pt serif">{_SHAPES}</svg>',
        (64, 32),
    ),
}


@pytest.mark.parametrize(("svg", "size"), list(EM_UNDER_AN_ABSOLUTE_FONT.values()), ids=list(EM_UNDER_AN_ABSOLUTE_FONT))
def test_an_em_size_under_a_font_size_in_an_absolute_unit_renders_at_96_dpi(svg, size):
    """Its size goes through an absolute unit the way a ``width="432pt"`` does,
    so resvg-py's default dpi finds no size at all: it is read at 96 dpi, byte
    for byte what resvg draws there, on both paths."""
    from app.llm.base import _rasterize_svg_to_png
    from app.services.svg_raster import rasterize_svg_async

    with pytest.raises(ValueError, match="invalid size"):
        resvg_py.svg_to_bytes(svg_string=svg)
    at_96_dpi = bytes(resvg_py.svg_to_bytes(svg_string=svg, dpi=96))
    assert _rasterize_svg_to_png(svg.encode(), source="em-unit") == at_96_dpi
    assert asyncio.run(rasterize_svg_async(svg.encode(), box=CHAT_BOX, keep_native_size=True)) == at_96_dpi
    with Image.open(io.BytesIO(at_96_dpi)) as opened:
        assert opened.size == size


def test_a_caller_reading_at_0_dpi_is_never_moved_to_96_dpi():
    """The logo geometry reads every SVG exactly as it always did (``dpi=0``): an
    em size under a font in pt stays unreadable there, as it always was."""
    from app.services.svg_raster import rasterize_svg

    svg, _size = EM_UNDER_AN_ABSOLUTE_FONT["font-attribute-pt"]
    assert rasterize_svg(svg.encode(), box=512, dpi=0) is None


def test_the_logo_geometry_reads_an_inner_absolute_length_at_0_dpi_as_before(monkeypatch):
    """The model sees an inner 2 mm frame; the logo geometry does not:
    it renders at resvg-py's default 0 dpi, the reading every stored trim box
    was taken with, so the mark is measured without the frame - on the sync and
    the worker path - and no stored trim box moves."""
    from app.services.image_geometry import analyze_image_geometry, image_geometry

    mark = '<rect x="40" y="40" width="20" height="20" fill="#123456"/>'
    frame = '<rect x="5" y="5" width="90" height="90" fill="none" stroke="#c33" stroke-width="2mm"/>'
    framed = f'<svg {_NS} width="100" height="100">{mark}{frame}</svg>'
    unframed = f'<svg {_NS} width="100" height="100">{mark}</svg>'
    real = resvg_py.svg_to_bytes
    assert real(svg_string=framed, dpi=96) != real(svg_string=unframed, dpi=96)
    dpis: list[object] = []

    def spy(**kwargs):
        dpis.append(kwargs.get("dpi"))
        return real(**kwargs)

    monkeypatch.setattr(resvg_py, "svg_to_bytes", spy)
    geometry = analyze_image_geometry(framed.encode(), "image/svg+xml")
    assert geometry is not None and dpis and set(dpis) == {0}
    assert geometry == analyze_image_geometry(unframed.encode(), "image/svg+xml")
    assert asyncio.run(image_geometry(framed.encode(), "image/svg+xml")) == geometry


_HUGE_FONT_SHEETS = {
    "font-attribute": ('font-size="500"', ""),
    "font-style": ('style="font-size:500px"', ""),
    "stylesheet": ("", "svg { font-size: 500px }"),
    "stylesheet-after-a-quoted-comment": ("", 'svg { content: "/*"; font-size: 500px }'),
    "stylesheet-after-a-quoted-brace": ("", "svg { content: '}'; font-size: 500px }"),
    "font-shorthand": ('style="font: 500px serif"', ""),
}


@pytest.mark.parametrize("view_box", ['viewBox="0 0 10 10"', ""], ids=["viewbox", "no-viewbox"])
@pytest.mark.parametrize(("root", "sheet"), list(_HUGE_FONT_SHEETS.values()), ids=list(_HUGE_FONT_SHEETS))
def test_an_em_size_under_a_large_font_is_never_drawn_past_the_box(resvg_calls, root, sheet, view_box):
    """``width="10em"`` under a 500 px font is 5000 px: the render is fitted into
    the box, never drawn at 5000 px first - wherever the font size is set,
    including a stylesheet whose string holds a ``/*`` or a ``}`` (resvg reads
    it as text; a comment or a rule end would have hidden the font size)."""
    from app.services.svg_raster import rasterize_svg

    style = f"<style>{sheet}</style>" if sheet else ""
    svg = f'<svg {_NS} width="10em" height="10em" {root} {view_box}>{style}<rect width="10" height="10"/></svg>'
    png = rasterize_svg(svg.encode(), box=512, keep_native_size=True)
    assert png is not None
    assert resvg_calls and all(max(call) <= 512 for call in resvg_calls)


def test_an_em_size_under_a_large_font_is_never_probed_past_its_bound(resvg_calls):
    """The logo geometry fits every SVG into its box and, for one without a
    viewBox, reads the size resvg gives it from a render at that size: only
    when that size is bounded by ``MAX_CONTENT_PROBE_SIDE``, never at 8000 px."""
    from app.services.image_geometry import analyze_image_geometry
    from app.services.svg_raster import MAX_CONTENT_PROBE_SIDE

    svg = f'<svg {_NS} width="10em" height="10em" font-size="800"><rect width="10" height="10"/></svg>'
    assert analyze_image_geometry(svg.encode(), "image/svg+xml") is not None
    assert resvg_calls and all(max(call) <= MAX_CONTENT_PROBE_SIDE for call in resvg_calls)


@pytest.mark.parametrize(
    "declaration",
    [
        "font-size:calc(300px)",
        "font-size:var(--size)",
        "font-size:300 px",
        "font-size:30rem",
        "font:caption",
        "font:bold 300 serif",
    ],
    ids=["function", "variable", "spaced-unit", "rem", "system-font", "unitless-shorthand"],
)
def test_a_font_size_resvg_does_not_read_leaves_the_default_as_before(declaration):
    """resvg reads a font size as a number with its unit or a keyword and
    ignores anything else, keeping the default: so does the bound, and such an
    em-sized SVG renders at the default size exactly as before."""
    from app.llm.base import _rasterize_svg_to_png
    from app.services.svg_raster import _png_size

    svg = f'<svg {_NS} width="10em" height="5em" style="{declaration}"><rect width="10" height="10"/></svg>'
    before = bytes(resvg_py.svg_to_bytes(svg_string=svg))
    assert _png_size(before) == (160, 80)
    assert _rasterize_svg_to_png(svg.encode(), source="ignored-font") == before


@pytest.mark.parametrize(
    ("css", "structure"),
    [
        ("svg { font-size: 30px } /* rect { fill: red } */", "svg { font-size: 30px }  "),
        ('g { content: "}"; filter: url(#f) }', 'g { content: " "; filter: url(#f) }'),
        ("g { content: '/*'; filter: url('#f') }", "g { content: '/*'; filter: url('#f') }"),
        ('g { content: "a\\"}" }', 'g { content: "a\\" " }'),
        ('/* "a */ g { fill: red }', "  g { fill: red }"),
        ('g { content: "open }', None),
        ('g { content: "}\ng { filter: url(#f) }', None),
        ("g { content: '}\ng { filter: url(#f) }", None),
        ('g { content: "}\ng { filter: url(#f) } "x"', None),
    ],
    ids=[
        "comment",
        "brace-in-string",
        "comment-in-string",
        "escaped-quote",
        "quote-in-comment",
        "open-string",
        "newline-in-a-string",
        "newline-in-a-single-quoted-string",
        "newline-in-a-string-before-a-later-quote",
    ],
)
def test_css_is_read_with_its_strings_and_comments_as_a_css_parser_reads_them(css, structure):
    from app.services.svg_raster import _css_text

    assert _css_text(css) == structure


@pytest.mark.parametrize(
    "sheet",
    [
        'g { content: "}"; filter: url(#spread); }',
        "g { content: '/*'; filter: url(#spread); }",
        'g { content: "open; filter: url(#spread); }',
        'g { content: "}\ng { filter: url(#spread); }',
    ],
    ids=["after-a-quoted-brace", "after-a-quoted-comment", "after-an-open-string", "after-a-newline-in-a-string"],
)
def test_a_group_filter_bomb_behind_a_css_string_is_refused(resvg_calls, render_reasons, sheet):
    """resvg reads ``content: "}"`` as text and filters the group (measured: 33 s
    inline for this morphology spread), so the rule after the string is charged
    on the group as the plain ``g { filter }`` is - and a sheet whose string is
    left open is charged as if its filters reached every element."""
    from app.llm.base import _rasterize_svg_to_png

    corners = "".join(
        f'<rect x="{x}" y="{y}" width="60" height="60" fill="#39f"/>'
        for x, y in ((40, 40), (3990, 40), (40, 3990), (3990, 3990), (2000, 2000))
    )
    svg = (
        f'<svg {_NS} width="4096" height="4096" viewBox="0 0 4096 4096"><style>{sheet}</style>'
        '<defs><filter id="spread"><feMorphology operator="dilate" radius="10"/></filter></defs>'
        f"<g>{corners}</g></svg>"
    ).encode()
    assert _rasterize_svg_to_png(svg, source="quoted-group-spread") is None
    assert render_reasons == [UNBOUNDED]
    assert resvg_calls == []


# -- filters are costed before resvg runs them -------------------------


def _filtered(primitives: str, side: int = 4096, *, filter_attrs: str = "", wrap: str = "") -> bytes:
    """A *side* px square rect under the filter *primitives*, optionally in a ``<g {wrap}>``."""
    shape = f'<rect width="{side}" height="{side}" fill="#08f" filter="url(#f)"/>'
    if wrap:
        shape = f"<g {wrap}>{shape}</g>"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}">'
        f'<filter id="f" {filter_attrs}>{primitives}</filter>{shape}</svg>'
    ).encode()


#: Each of these runs resvg into the 10 s worker timeout (measured: 10.2 s,
#: the worker killed); each is refused by the structural pass in milliseconds.
FILTER_BOMBS = {
    "morphology-radius": _filtered('<feMorphology radius="64"/>'),
    "many-blurs": _filtered('<feGaussianBlur stdDeviation="5"/>' * 300),
    "turbulence-octaves": _filtered('<feTurbulence baseFrequency="0.9" numOctaves="10"/>'),
    "morphology-under-a-scale": _filtered(
        '<feMorphology radius="4"/>', 16, filter_attrs='x="0" y="0" width="1" height="1"', wrap='transform="scale(64)"'
    ).replace(b'width="16" height="16">', b'width="1024" height="1024">', 1),
    "region-past-the-canvas": (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        b'<filter id="f" filterUnits="userSpaceOnUse" x="-10000" y="-10000" width="20200" height="20200">'
        b'<feFlood flood-color="#08f"/></filter><rect width="200" height="200" filter="url(#f)"/></svg>'
    ),
    "filter-in-a-scaled-use": (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024"><defs>'
        b'<filter id="f"><feMorphology radius="4"/></filter>'
        b'<rect id="r" width="16" height="16" filter="url(#f)"/></defs>'
        b'<use href="#r" transform="scale(64)"/></svg>'
    ),
    "filter-from-a-stylesheet": (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="4096" height="4096">'
        b"<style>.x{filter:url(#f)}</style>"
        b'<filter id="f"><feMorphology radius="64"/></filter>'
        b'<rect class="x" width="4096" height="4096"/></svg>'
    ),
}


@pytest.mark.parametrize("svg", list(FILTER_BOMBS.values()), ids=list(FILTER_BOMBS))
def test_a_filter_bomb_is_refused_before_resvg_sees_it(resvg_calls, render_reasons, svg):
    from app.llm.base import _rasterize_svg_to_png

    assert _rasterize_svg_to_png(svg, source="filter-bomb") is None
    assert render_reasons == [UNBOUNDED]
    assert resvg_calls == []


def test_the_filter_budget_stays_under_the_worker_timeout():
    from app.services.svg_raster import MAX_FILTER_WORK_MS, RASTER_TIMEOUT_SECONDS

    assert MAX_FILTER_WORK_MS < RASTER_TIMEOUT_SECONDS * 1000


def _figma_cards(count: int = 30) -> bytes:
    """Figma's export of a grid of cards, each with its own drop shadow filter."""
    cards = "".join(
        f'<filter id="s{i}" x="{(i % 6) * 230 - 10}" y="{(i // 6) * 170 - 10}" width="240" height="180" '
        'filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB">'
        '<feFlood flood-opacity="0" result="bg"/>'
        '<feColorMatrix in="SourceAlpha" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0" result="hardAlpha"/>'
        '<feOffset dy="4"/><feGaussianBlur stdDeviation="6"/><feComposite in2="hardAlpha" operator="out"/>'
        '<feColorMatrix values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.25 0"/>'
        '<feBlend in2="bg" result="shadow"/><feBlend in="SourceGraphic" in2="shadow"/></filter>'
        f'<rect x="{(i % 6) * 230}" y="{(i // 6) * 170}" width="220" height="160" rx="8" fill="#fff" '
        f'filter="url(#s{i})"/>'
        for i in range(count)
    )
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="900" viewBox="0 0 1400 900">{cards}</svg>'.encode()


LEGITIMATE_FILTERS = {
    "drop-shadow": (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="160">'
        b'<filter id="f" x="-20%" y="-20%" width="140%" height="140%">'
        b'<feGaussianBlur in="SourceAlpha" stdDeviation="4"/><feOffset dx="3" dy="3"/>'
        b'<feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
        b'<rect x="30" y="30" width="200" height="80" rx="12" fill="#0fbcff" filter="url(#f)"/></svg>'
    ),
    "figma-cards": _figma_cards(),
    "inkscape-blur": (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
        b'<filter id="b"><feGaussianBlur stdDeviation="3"/></filter>'
        b'<path d="M 10 10 L 300 10 L 300 200 Z" filter="url(#b)"/></svg>'
    ),
    "glow-on-a-full-page": _filtered('<feGaussianBlur stdDeviation="8"/><feMerge><feMergeNode/></feMerge>', 1920),
}


@pytest.mark.parametrize("svg", list(LEGITIMATE_FILTERS.values()), ids=list(LEGITIMATE_FILTERS))
def test_a_legitimate_filter_still_renders_byte_for_byte(svg):
    from app.llm.base import _rasterize_svg_to_png

    assert _rasterize_svg_to_png(svg, source="filter") == bytes(resvg_py.svg_to_bytes(svg_string=svg.decode("utf-8")))


def test_a_filter_inherited_through_a_chain_is_charged_what_it_draws():
    """resvg follows a filter's ``href`` chain (a shadow two hops away draws
    exactly as the inline one): the budget charges the primitives and the region
    it inherits, and the drawing renders byte for byte as before."""
    import defusedxml.ElementTree as DefusedET

    from app.llm.base import _rasterize_svg_to_png
    from app.services.svg_raster import _expanded_total

    shadow = (
        'x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur in="SourceAlpha" stdDeviation="3"/>'
        '<feOffset dx="3" dy="3"/><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
    )
    card = '<rect x="30" y="30" width="200" height="80" rx="10" fill="#0fbcff" filter="url(#f0)"/>'
    flat = f'<svg {_NS} width="300" height="160"><filter id="f0" {shadow}{card}</svg>'
    chained = (
        f'<svg {_NS} width="300" height="160"><filter id="f2" {shadow}<filter id="f1" xlink:href="#f2"/>'
        f'<filter id="f0" xlink:href="#f1"/>{card}</svg>'
    )

    def work(svg: str) -> float:
        return _expanded_total(DefusedET.fromstring(svg))[1]

    assert work(chained) == work(flat) > 0
    assert _rasterize_svg_to_png(chained.encode(), source="chain") == bytes(resvg_py.svg_to_bytes(svg_string=chained))


# -- a stylesheet filter on a container is charged where resvg applies it


def _spread_shapes(count: int = 40) -> str:
    """Small ordinary shapes spread across a 4096 px canvas (a group of them
    spans it, while each child's own box stays tiny)."""
    return "".join(
        f'<circle cx="{200 + (i % 8) * 500}" cy="{200 + (i // 8) * 500}" r="20" fill="#39f"/>' for i in range(count)
    )


#: An ordinary gaussian blur declared once, applied to the group / a class below.
_BLUR_DEFS = '<defs><filter id="b" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="40"/></filter></defs>'


def _group_stylesheet_filter(selector: str, *, on_group_class: str = "") -> bytes:
    group = f'<g class="{on_group_class}">{_spread_shapes()}</g>' if on_group_class else f"<g>{_spread_shapes()}</g>"
    return (
        f'<svg {_NS} width="4096" height="4096" viewBox="0 0 4096 4096">'
        f"<style>{selector} {{ filter: url(#b); }}</style>{_BLUR_DEFS}{group}</svg>"
    ).encode()


def _leaf_stylesheet_filter() -> bytes:
    shapes = _spread_shapes().replace("<circle", '<circle class="b"')
    return (
        f'<svg {_NS} width="4096" height="4096" viewBox="0 0 4096 4096">'
        f"<style>.b {{ filter: url(#b); }}</style>{_BLUR_DEFS}<g>{shapes}</g></svg>"
    ).encode()


@pytest.mark.parametrize(
    "svg",
    [_group_stylesheet_filter("g"), _group_stylesheet_filter(".card", on_group_class="card")],
    ids=["type-selector", "class-on-group"],
)
def test_a_stylesheet_filter_on_a_group_is_charged_at_its_region_not_its_children(svg):
    """resvg applies ``g { filter }`` (or a class a GROUP carries) to the whole
    group as one layer over its region (measured: one blur ~2.6 s on a
    4096 px canvas); the per-leaf charge saw only the children's tiny boxes and
    let a blur/morphology bomb pass the budget and burn the worker timeout on
    every uncached attempt. The container is now charged at its
    region, the same accounting the inline ``filter=`` attribute already used."""
    import defusedxml.ElementTree as DefusedET

    from app.services.svg_raster import _expanded_total

    group_ms = _expanded_total(DefusedET.fromstring(svg.decode()))[1]
    leaf_ms = _expanded_total(DefusedET.fromstring(_leaf_stylesheet_filter().decode()))[1]
    # The group is charged at its (drawn-extent) region, hundreds to thousands of
    # ms for one blur; the same blur referenced on a class only the leaves carry
    # stays at their tiny boxes. On the previous head both were ~10 ms, and the
    # container application went uncharged.
    assert group_ms >= 500
    assert group_ms > 20 * leaf_ms
    assert leaf_ms < 200


def test_a_leaf_class_filter_does_not_charge_the_group_that_wraps_it():
    """A filter on a class only the LEAVES carry must not be charged on their
    wrapping group - the selector decides, so a real per-leaf shadow keeps
    rendering byte for byte instead of being refused."""
    from app.llm.base import _rasterize_svg_to_png

    svg = _leaf_stylesheet_filter()
    assert _rasterize_svg_to_png(svg, source="leaf-shadow") == bytes(resvg_py.svg_to_bytes(svg_string=svg.decode()))


@pytest.mark.parametrize(
    "count,width,height",
    [(4, 1600, 900), (6, 1920, 1080), (8, 3840, 2160), (12, 1920, 1080)],
    ids=["deck-4-cards", "poster-6-cards", "big-8-cards-4k", "12-cards"],
)
def test_a_stylesheet_shadow_on_small_group_cards_still_renders(count, width, height):
    """An ordinary drop shadow applied through ``g.card { filter }`` to small
    group-cards renders byte for byte - the container charge is taken at the
    cards' drawn extent, never the whole canvas, so a normal deck / poster is
    not refused (the first cut double-charged and refused these)."""
    from app.llm.base import _rasterize_svg_to_png

    cards = "".join(
        f'<g class="card"><rect x="{40 + (i % 4) * 300}" y="{40 + (i // 4) * 190}" width="260" height="150" '
        f'rx="10" fill="#fff"/></g>'
        for i in range(count)
    )
    svg = (
        f'<svg {_NS} width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<style>g.card { filter: url(#sh); }</style>'
        '<defs><filter id="sh" x="-20%" y="-20%" width="140%" height="140%">'
        '<feGaussianBlur in="SourceAlpha" stdDeviation="4"/><feOffset dx="2" dy="3"/>'
        '<feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>'
        f"{cards}</svg>"
    ).encode()
    assert _rasterize_svg_to_png(svg, source="cards") == bytes(resvg_py.svg_to_bytes(svg_string=svg.decode()))


def test_a_stylesheet_filter_bomb_on_a_full_canvas_group_is_refused(resvg_calls, render_reasons):
    """An ordinary morphology spread applied through ``g { filter }`` to a group
    spanning the canvas exceeds the filter budget once charged at the group's
    region, and is refused in milliseconds without resvg ever running it - where
    the per-leaf charge let it through."""
    from app.llm.base import _rasterize_svg_to_png

    corners = "".join(
        f'<rect x="{x}" y="{y}" width="60" height="60" fill="#39f"/>'
        for x, y in ((40, 40), (3990, 40), (40, 3990), (3990, 3990), (2000, 2000))
    )
    svg = (
        f'<svg {_NS} width="4096" height="4096" viewBox="0 0 4096 4096">'
        '<style>g { filter: url(#spread); }</style>'
        '<defs><filter id="spread"><feMorphology operator="dilate" radius="10"/></filter></defs>'
        f"<g>{corners}</g></svg>"
    ).encode()
    assert _rasterize_svg_to_png(svg, source="group-spread") is None
    assert render_reasons == [UNBOUNDED]
    assert resvg_calls == []


# -- rendered PNGs are cached ------------------------------------------


def _count_worker_calls(monkeypatch) -> list[bool]:
    """Every time a render reaches the worker pool."""
    from app.services import svg_raster

    calls: list[bool] = []
    real = svg_raster._get_executor

    def spy():
        calls.append(True)
        return real()

    monkeypatch.setattr(svg_raster, "_get_executor", spy)
    return calls


def test_a_cached_render_returns_the_same_png_without_the_worker(monkeypatch):
    """A conversation's history holds the same SVGs turn after turn: the second
    render of the same bytes is the first render's PNG, never a new one."""
    from app.services.svg_raster import rasterize_svg_async

    calls = _count_worker_calls(monkeypatch)

    async def run() -> tuple[bytes | None, bytes | None]:
        first = await rasterize_svg_async(DIAGRAM_SVG, box=CHAT_BOX, keep_native_size=True)
        second = await rasterize_svg_async(bytes(DIAGRAM_SVG), box=CHAT_BOX, keep_native_size=True)
        return first, second

    first, second = asyncio.run(run())
    assert first is not None and second == first
    assert calls == [True]


def test_callers_queued_behind_the_same_svg_share_its_render(monkeypatch):
    """Callers that all missed the cache before the first render finished wait
    at the gate; each finds that render cached once it gets through, instead of
    rendering the same bytes again."""
    from app.services.svg_raster import rasterize_svg_async

    calls = _count_worker_calls(monkeypatch)

    async def run() -> list[bytes | None]:
        return await asyncio.gather(
            *(rasterize_svg_async(DIAGRAM_SVG, box=CHAT_BOX, keep_native_size=True) for _ in range(4))
        )

    renders = asyncio.run(run())
    assert renders[0] is not None and all(render == renders[0] for render in renders)
    assert calls == [True]


def test_the_cache_key_carries_every_render_parameter(monkeypatch):
    from app.services.svg_raster import rasterize_svg_async

    calls = _count_worker_calls(monkeypatch)

    async def run() -> list[bytes | None]:
        return [
            await rasterize_svg_async(LOGO_SVG, box=512),
            await rasterize_svg_async(LOGO_SVG, box=256),
            await rasterize_svg_async(LOGO_SVG, box=512, keep_native_size=True),
            await rasterize_svg_async(LOGO_SVG + b" ", box=512),
            await rasterize_svg_async(LOGO_SVG, box=512),
        ]

    renders = asyncio.run(run())
    assert len(calls) == 4  # the last one is the first one again
    assert renders[4] == renders[0]
    with Image.open(io.BytesIO(renders[0])) as fitted, Image.open(io.BytesIO(renders[1])) as smaller:
        assert fitted.size[0] == 512 and smaller.size[0] == 256
    with Image.open(io.BytesIO(renders[2])) as native:
        assert native.size == (240, 80)


def test_a_refusal_is_cached_too(monkeypatch):
    from app.services.svg_raster import rasterize_svg_async

    calls = _count_worker_calls(monkeypatch)

    async def run() -> list[bytes | None]:
        return [await rasterize_svg_async(_use_fan(5), box=512) for _ in range(3)]

    assert asyncio.run(run()) == [None, None, None]
    assert calls == [True]


def test_a_timeout_is_not_cached(monkeypatch):
    """A render killed on its timeout says something about the moment (a loaded
    host), not about the bytes: the next call renders again."""
    from app.services import svg_raster

    monkeypatch.setattr(svg_raster, "RASTER_TIMEOUT_SECONDS", 0.001)
    assert asyncio.run(svg_raster.rasterize_svg_async(LOGO_SVG, box=512)) is None
    assert len(svg_raster._render_cache) == 0
    monkeypatch.setattr(svg_raster, "RASTER_TIMEOUT_SECONDS", 60.0)
    assert asyncio.run(svg_raster.rasterize_svg_async(LOGO_SVG, box=512)) is not None


def test_a_skipped_render_is_not_cached(monkeypatch):
    from app.services import svg_raster

    monkeypatch.setattr(svg_raster, "RASTER_QUEUE_TIMEOUT_SECONDS", 0.05)

    async def run() -> bytes | None:
        gate = svg_raster._gate()
        await gate.acquire()
        try:
            return await svg_raster.rasterize_svg_async(LOGO_SVG, box=64)
        finally:
            gate.release()

    assert asyncio.run(run()) is None
    assert len(svg_raster._render_cache) == 0


def test_the_cache_is_bounded_in_entries_least_recently_used_first(monkeypatch):
    from app.services import svg_raster

    monkeypatch.setattr(svg_raster, "RENDER_CACHE_MAX_ENTRIES", 2)
    svg_raster._cache_put(("a",), b"1")
    svg_raster._cache_put(("b",), b"2")
    assert svg_raster._cache_get(("a",)) == b"1"  # "a" is now the most recent
    svg_raster._cache_put(("c",), None)
    assert list(svg_raster._render_cache) == [("a",), ("c",)]
    assert svg_raster._cache_get(("b",)) is svg_raster._CACHE_MISS


def test_the_cache_is_bounded_in_bytes(monkeypatch):
    from app.services import svg_raster

    overhead = svg_raster._CACHE_ENTRY_OVERHEAD
    monkeypatch.setattr(svg_raster, "RENDER_CACHE_MAX_BYTES", 4 * (overhead + 1000))
    for name in "abcde":
        svg_raster._cache_put((name,), b"x" * 1000)
        assert svg_raster._render_cache_bytes <= svg_raster.RENDER_CACHE_MAX_BYTES
    assert list(svg_raster._render_cache) == [("b",), ("c",), ("d",), ("e",)]
    svg_raster._cache_put(("e",), b"y" * 500)  # replacing an entry re-counts it
    assert svg_raster._render_cache_bytes == 3 * (overhead + 1000) + overhead + 500
    svg_raster._cache_put(("huge",), b"z" * (overhead + 1000 + 1))  # past a quarter of the cache
    assert svg_raster._cache_get(("huge",)) is svg_raster._CACHE_MISS
    svg_raster.clear_svg_raster_cache()
    assert svg_raster._render_cache_bytes == 0 and not svg_raster._render_cache


def test_the_default_cache_bound_is_what_the_docs_say():
    from app.services.svg_raster import RENDER_CACHE_MAX_BYTES, RENDER_CACHE_MAX_ENTRIES

    assert RENDER_CACHE_MAX_ENTRIES == 256
    assert RENDER_CACHE_MAX_BYTES == 32 * 1024 * 1024
