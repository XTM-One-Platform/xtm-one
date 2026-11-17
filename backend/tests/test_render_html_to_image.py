"""``render_html_to_image`` - a browser-rendered figure in one call.

The browser itself is mocked here (the backend suite runs without Chromium);
the wrapping, the probe formatting, the script stripping, the font inlining
and the tool's file handling are what these tests pin.
"""

from __future__ import annotations

import base64
import io
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.services.builtin_tools import build_generate_file_tool, html_render
from app.services.builtin_tools.html_render import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    MAX_CANVAS_PX,
    MAX_DEVICE_PIXELS,
    MAX_PROBE_FINDINGS,
    MAX_SCALE,
    HtmlRenderError,
    _bounded,
    _safe_name,
    _strip_scripts,
    check_canvas_area,
    font_face_css,
    format_coverage,
    format_probe,
    format_svg_stats,
    wrap_html,
)
from app.tools.base import FileContext, ToolCall


def _png(width: int = 8, height: int = 6) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (0, 0, 145)).save(buffer, "PNG")
    return buffer.getvalue()


# ── Pure helpers ──────────────────────────────────────────────────────────


def test_bounds_and_defaults():
    assert _bounded(None, default=DEFAULT_WIDTH, lo=64, hi=MAX_CANVAS_PX, what="width") == DEFAULT_WIDTH
    assert _bounded("", default=DEFAULT_HEIGHT, lo=64, hi=MAX_CANVAS_PX, what="height") == DEFAULT_HEIGHT
    assert _bounded("1200", default=1, lo=64, hi=MAX_CANVAS_PX, what="width") == 1200
    assert _bounded(2.0, default=1, lo=1, hi=3, what="scale") == 2
    with pytest.raises(HtmlRenderError, match="between 64 and 4096"):
        _bounded(9000, default=1, lo=64, hi=MAX_CANVAS_PX, what="width")
    with pytest.raises(HtmlRenderError, match="whole number"):
        _bounded("wide", default=1, lo=64, hi=MAX_CANVAS_PX, what="width")


def test_canvas_area_is_bounded_beyond_the_per_dimension_caps():
    """Each bound alone admits 4096 x 4096 at 3x: 151 million device pixels.

    The area cap is the default canvas at the highest density; the message
    names the area asked for and the ceiling.
    """
    assert MAX_DEVICE_PIXELS == DEFAULT_WIDTH * DEFAULT_HEIGHT * MAX_SCALE * MAX_SCALE
    check_canvas_area(DEFAULT_WIDTH, DEFAULT_HEIGHT, MAX_SCALE)
    check_canvas_area(MAX_CANVAS_PX, MAX_CANVAS_PX, 1)
    check_canvas_area(2560, 1440, 2)
    with pytest.raises(HtmlRenderError, match=r"4096x4096 at 3x is 150,994,944 device pixels; the ceiling is"):
        check_canvas_area(MAX_CANVAS_PX, MAX_CANVAS_PX, MAX_SCALE)
    with pytest.raises(HtmlRenderError, match="Lower the scale or the canvas"):
        check_canvas_area(MAX_CANVAS_PX, MAX_CANVAS_PX, 2)


def test_safe_name_strips_extensions_and_odd_characters():
    assert _safe_name("founders card.png") == "founders-card"
    assert _safe_name("Founders/Card.html") == "Founders-Card"
    assert _safe_name("") .startswith("figure-")
    assert _safe_name("../x") == "x"


def test_wrap_html_sizes_a_fragment_and_keeps_a_document():
    fragment = wrap_html("<div>hi</div>", width=800, height=450, font_css="@font-face{}")
    assert fragment.startswith("<!doctype html><html><head>")
    assert "<style>@font-face{}</style>" in fragment
    assert "body{width:800px;height:450px;overflow:hidden;}" in fragment
    assert fragment.endswith("<body><div>hi</div></body></html>")

    document = wrap_html(
        "<!DOCTYPE html><html><head><title>t</title></head><body>x</body></html>",
        width=100,
        height=50,
        font_css="",
    )
    assert document.count("<head>") == 1
    assert "<title>t</title>" in document
    assert "body{width:100px;height:50px" in document
    assert "@font-face" not in document

    headless = wrap_html("<html><body>y</body></html>", width=10, height=10, font_css="F")
    assert "<html><head><style>F</style>" in headless


def test_scripts_and_inline_handlers_are_stripped():
    document = '<div onclick="alert(1)" data-x="1"><script>alert(2)</script>ok</div><SCRIPT src="a.js"></SCRIPT>'
    assert _strip_scripts(document) == '<div data-x="1">ok</div>'


def test_format_probe_names_every_kind_worst_first_and_bounds_the_list():
    probe = {
        "out": [{"el": 'div "out"', "right": 2000, "bottom": 100, "left": 0, "top": 0, "canvas": [1920, 1080]}],
        "ovf": [
            {"el": "p.line", "scroll": [497, 20], "box": [200, 20], "nowrap": True},
            {"el": "div.card", "scroll": [300, 900], "box": [300, 600], "nowrap": False},
        ],
        "overlap": [{"a": "h1", "b": "span", "area": 51}],
    }
    text, total = format_probe(probe, width=1920, height=1080)
    assert total == 4
    lines = text.splitlines()
    assert lines[0] == 'OUT div "out": right=2000 > 1920'
    assert lines[1] == "OVF p.line: nowrap text runs past its box (497x20 in 200x20)"
    assert lines[2] == "OVF div.card: content clipped (300x900 in 300x600)"
    assert lines[3] == "OVERLAP h1 x span: 51 px2"
    assert format_probe({}, width=1, height=1) == ("", 0)

    many = {"overlap": [{"a": f"a{i}", "b": "b", "area": i} for i in range(MAX_PROBE_FINDINGS + 5)]}
    text, total = format_probe(many, width=1, height=1)
    assert total == MAX_PROBE_FINDINGS + 5
    assert len(text.splitlines()) == MAX_PROBE_FINDINGS


def test_format_probe_lists_the_svg_findings_and_counts_the_crossings_apart():
    probe = {
        "overlap": [{"a": "h1", "b": "span", "area": 51}],
        "svg": {
            "svgs": 1,
            "labels": 4,
            "shapes": 3,
            "edges": 2,
            "crossings": 7,
            "truncated": False,
            "ovf": [{"text": 'text "Long"', "shape": "rect.box", "outside": 40}],
            "overlap": [{"text": 'text "Node"', "shape": "circle", "share": 25}],
            "edge": [{"text": 'text "Crossed"', "edges": 2, "first": "path.link"}],
        },
    }
    text, total = format_probe(probe, width=100, height=100)
    assert total == 4
    assert text.splitlines() == [
        'OVF text "Long": runs past the border of rect.box (40% of the label outside)',
        "OVERLAP h1 x span: 51 px2",
        'OVERLAP text "Node" x circle: label drawn over a shape it is not inside (25% of the label covered)',
        'EDGE-ON-TEXT text "Crossed": crossed by 2 edges (first path.link)',
    ]
    assert format_svg_stats(probe) == "SVG: 4 label(s), 3 shape(s), 2 edge(s) measured; 7 edge-edge crossing(s)."
    probe["svg"]["truncated"] = True
    assert "only part of it was measured" in format_svg_stats(probe)
    assert format_svg_stats({}) == ""
    assert format_svg_stats({"svg": {"svgs": 0}}) == ""


def test_format_probe_reports_a_wrapped_cell_and_the_whole_height_measured():
    probe = {
        "out": [{"el": "div.tail", "right": 100, "bottom": 7900, "left": 0, "top": 7800, "canvas": [2560, 8000]}],
        "wrap": [{"el": 'td.name "A long name"', "lines": 2, "column": 1, "top": 6012}],
        "measured": {"width": 2560, "height": 8000},
    }
    text, total = format_probe(probe, width=2560, height=MAX_CANVAS_PX)
    # Bounds are the measured document, not the PNG's cap: bottom=7900 is not
    # reported as past a 4096 px canvas.
    assert text.splitlines() == [
        "OUT div.tail: past the canvas",
        'WRAP td.name "A long name": text runs to 2 lines where its column holds 1 (top=6012)',
    ]
    assert total == 2
    assert format_coverage(probe, height=MAX_CANVAS_PX) == (
        f"Coverage: the figure is 8000 px tall; the PNG shows its first {MAX_CANVAS_PX} px (the canvas cap) "
        "and the probe measured all 8000 px."
    )
    assert format_coverage({}, height=1080) == ""


def test_the_probe_holds_the_svg_pass_in_the_one_function_playwright_evaluates():
    assert html_render.PROBE_JS.startswith("(bounds) => {")
    assert "const probeWrap = (name, CAP) =>" in html_render.PROBE_JS
    assert "wrap: probeWrap(name, CAP)" in html_render.PROBE_JS
    assert "const probeSvg = (name) =>" in html_render.PROBE_JS
    assert "svg: probeSvg(name)" in html_render.PROBE_JS
    assert "isPointInFill" in html_render.PROBE_JS
    assert "getPointAtLength" in html_render.PROBE_JS


def test_font_face_css_inlines_the_bundled_fonts_and_is_empty_without_a_bundle(tmp_path):
    assert font_face_css(tmp_path / "missing") == ""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "geologica-latin-400-normal-abc123.woff2").write_bytes(b"wOF2geo")
    (assets / "elms-sans-latin-600-normal-elm789.woff2").write_bytes(b"wOF2elms")
    (assets / "ibm-plex-sans-latin-700-normal-def456.woff2").write_bytes(b"wOF2plex")
    (assets / "ibm-plex-sans-latin-400-italic-ghi.woff2").write_bytes(b"wOF2it")
    (assets / "ibm-plex-sans-latin-700-italic-zzz.woff2").write_bytes(b"skipped")
    css = font_face_css(tmp_path)
    assert css.count("@font-face") == 4
    assert "font-family:'Geologica';font-style:normal;font-weight:400" in css
    # The display face a template may name (the THREAD deck).
    assert "font-family:'Elms Sans';font-style:normal;font-weight:600" in css
    assert "font-family:'IBM Plex Sans';font-style:normal;font-weight:700" in css
    assert "font-family:'IBM Plex Sans';font-style:italic;font-weight:400" in css
    assert base64.b64encode(b"wOF2geo").decode() in css
    assert "skipped" not in css


def test_font_face_css_falls_back_to_the_worker_font_directory(tmp_path):
    """The worker image has no frontend bundle: the fonts its Dockerfile copies
    to /app/fonts (the @fontsource names, no hash) are read instead."""
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "geologica-latin-700-normal.woff2").write_bytes(b"wOF2geo700")
    (fonts / "ibm-plex-sans-latin-400-normal.woff2").write_bytes(b"wOF2plex")
    (fonts / "geologica-latin-ext-400-normal.woff2").write_bytes(b"ext")
    css = font_face_css(tmp_path / "no-bundle", fonts)
    assert css.count("@font-face") == 2
    assert "font-family:'Geologica';font-style:normal;font-weight:700" in css
    assert base64.b64encode(b"ext").decode() not in css, "latin-ext is not the latin subset"
    assert font_face_css(tmp_path / "no-bundle", tmp_path / "no-fonts") == ""
    # A bundle wins over the font directory.
    (tmp_path / "dist" / "assets").mkdir(parents=True)
    assert font_face_css(tmp_path / "dist", fonts) == ""


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statements: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows)


def _asset(filename: str, content_type: str, size: int = 10):
    return SimpleNamespace(
        id=uuid.uuid4(), filename=filename, content_type=content_type, size=size, s3_object_name=f"tpl/{filename}"
    )


def _patch_assets(monkeypatch, rows, blobs: dict[str, bytes]):
    session = _Session(rows)
    downloads: list[str] = []

    async def _groups(_db, _uid):
        return []

    async def _download(name):
        downloads.append(name)
        return blobs[name]

    monkeypatch.setattr("app.services.builtin_tools._shared._own_session", lambda: session)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)
    return session, downloads


@pytest.mark.asyncio
async def test_template_asset_handles_are_inlined_svg_included(monkeypatch):
    logo = _asset("filigran.svg", "image/svg+xml")
    mark = _asset("opencti.png", "image/png")
    other = str(uuid.uuid4())
    session, downloads = _patch_assets(
        monkeypatch, [logo, mark], {"tpl/filigran.svg": b"<svg/>", "tpl/opencti.png": b"PNG"}
    )
    html = f'<img src="file:{logo.id}"><img src="[[FILE:{str(mark.id).upper()}]]"><img src="file:{other}">'
    out, too_big = await html_render.inline_template_asset_refs(html, str(uuid.uuid4()))
    assert too_big == []
    assert f"data:image/svg+xml;base64,{base64.b64encode(b'<svg/>').decode()}" in out
    assert f"data:image/png;base64,{base64.b64encode(b'PNG').decode()}" in out
    assert f"file:{other}" in out, "an id that is no visible template asset is left as written"
    assert sorted(downloads) == ["tpl/filigran.svg", "tpl/opencti.png"]
    # The lookup is scoped to template assets under the container's visibility.
    sql = str(session.statements[0])
    assert "template_containers" in sql and "file_attachments.source" in sql


@pytest.mark.asyncio
async def test_template_assets_are_bounded_and_need_a_handle(monkeypatch):
    poster = _asset("poster.png", "image/png", size=html_render.MAX_INLINE_FILE_BYTES + 1)
    font = _asset("brand.woff2", "font/woff2")
    session, downloads = _patch_assets(monkeypatch, [poster, font], {})
    html = f'<img src="file:{poster.id}"><img src="file:{font.id}">'
    out, too_big = await html_render.inline_template_asset_refs(html, None)
    assert out == html
    assert too_big == ["poster.png (8.0 MB)"]
    assert downloads == [], "refused off the row, never downloaded"
    session.statements.clear()
    assert await html_render.inline_template_asset_refs("<b>no handles</b>", None) == ("<b>no handles</b>", [])
    assert session.statements == [], "no handle, no query"


@pytest.mark.asyncio
async def test_render_inlines_a_template_asset_and_refuses_an_oversized_one(monkeypatch):
    persisted: list[dict] = []
    _defs, executor, rendered = _build(monkeypatch, persisted)
    executor.file_context = FileContext()
    logo = _asset("filigran.svg", "image/svg+xml")
    _patch_assets(monkeypatch, [logo], {"tpl/filigran.svg": b"<svg/>"})
    result = await _call(executor, html=f'<img src="file:{logo.id}">')
    assert result.is_error is False, result.content
    assert "data:image/svg+xml;base64," in rendered[0]["html"]
    assert persisted[0]["raw_bytes"] == f'<img src="file:{logo.id}">'.encode(), "the source keeps the handle"

    big = _asset("poster.png", "image/png", size=html_render.MAX_INLINE_FILE_BYTES + 1)
    _patch_assets(monkeypatch, [big], {})
    refused = await _call(executor, html=f'<img src="file:{big.id}">')
    assert refused.is_error and "template assets over the 8.0 MB per-image ceiling" in refused.content
    assert len(rendered) == 1, "the browser was not launched for the refused call"


# ── The tool ──────────────────────────────────────────────────────────────


def _build(monkeypatch, persisted: list[dict], probe: dict | None = None):
    async def _fake_persist(**kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{kwargs['filename']}",
            source=kwargs["source"],
        )

    rendered: list[dict] = []

    async def _fake_render(html, *, width, height, scale, font_css):
        rendered.append({"html": html, "width": width, "height": height, "scale": scale, "font_css": font_css})
        return _png(), probe or {"out": [], "ovf": [], "overlap": [], "leaves": 3}

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _fake_persist)
    monkeypatch.setattr(html_render, "render_html", _fake_render)
    monkeypatch.setattr(html_render, "font_face_css", lambda: "")
    defs, executor = build_generate_file_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    return defs, executor, rendered


async def _call(executor, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name="render_html_to_image", arguments=arguments))


def test_render_html_to_image_rides_the_file_generation_suite(monkeypatch):
    defs, _executor, _rendered = _build(monkeypatch, [])
    names = [d.name for d in defs]
    assert names[-1] == "render_html_to_image"
    assert {d.source for d in defs} == {"builtin:files"}
    render = defs[-1]
    assert set(render.input_schema["properties"]) == {"html", "source_file_id", "width", "height", "scale", "name"}
    assert "profiles block" in render.description


@pytest.mark.asyncio
async def test_render_stores_the_png_and_its_html_source_and_reports_a_clean_probe(monkeypatch):
    persisted: list[dict] = []
    _defs, executor, rendered = _build(monkeypatch, persisted)
    ctx = FileContext()
    executor.file_context = ctx

    result = await _call(executor, html="<div>Founders</div>", name="founders-card", width=1600, height=900, scale=2)
    assert result.is_error is False, result.content
    assert rendered[0]["width"] == 1600 and rendered[0]["height"] == 900 and rendered[0]["scale"] == 2
    assert [p["filename"] for p in persisted] == ["founders-card.html", "founders-card.png"]
    assert persisted[0]["raw_bytes"] == b"<div>Founders</div>"
    assert persisted[0]["content_type"] == "text/html"
    assert persisted[1]["content_type"] == "image/png"
    assert all(p["source"] == "agent_working" for p in persisted)
    html_ref, png_ref = ctx.list_files()
    assert result.files == [png_ref], "the PNG is attached once, as a vision block"
    assert f"as file_id {png_ref.file_id}" in result.content
    assert f'render_html_to_image(source_file_id="{html_ref.file_id}")' in result.content
    assert "Probe: clean" in result.content
    assert "1600x900 px at 2x" in result.content
    assert "system fonts were used" in result.content
    assert "Visual summary:" in result.content
    assert "annotate_file" in result.content


@pytest.mark.asyncio
async def test_render_reports_the_probe_findings(monkeypatch):
    probe = {
        "out": [{"el": "div.badge", "right": 1950, "bottom": 10, "left": 0, "top": 0, "canvas": [1920, 1080]}],
        "ovf": [{"el": "p.line", "scroll": [497, 20], "box": [200, 20], "nowrap": True}],
        "overlap": [],
    }
    persisted: list[dict] = []
    _defs, executor, _rendered = _build(monkeypatch, persisted, probe=probe)
    executor.file_context = FileContext()
    result = await _call(executor, html="<div>x</div>")
    assert result.is_error is False
    assert "Probe: 2 finding(s) - fix them in the source and re-render:" in result.content
    assert "OUT div.badge: right=1950 > 1920" in result.content
    assert "OVF p.line: nowrap text runs past its box" in result.content


@pytest.mark.asyncio
async def test_re_rendering_a_stored_source_saves_no_second_html(monkeypatch):
    persisted: list[dict] = []
    _defs, executor, rendered = _build(monkeypatch, persisted)
    ctx = FileContext()
    source = FileRef.from_bytes(b"<div>v2</div>", "founders-card.html", "text/html")
    source.source = "agent_working"
    ctx.add(source)
    executor.file_context = ctx

    result = await _call(executor, source_file_id=source.file_id)
    assert result.is_error is False, result.content
    assert rendered[0]["html"] == "<div>v2</div>"
    assert [p["filename"] for p in persisted] == ["founders-card.png"], "the source already exists"
    assert f"HTML source: founders-card.html (file_id {source.file_id})" in result.content


@pytest.mark.asyncio
async def test_render_inlines_conversation_image_handles(monkeypatch):
    persisted: list[dict] = []
    _defs, executor, rendered = _build(monkeypatch, persisted)
    ctx = FileContext()
    logo = FileRef.from_bytes(_png(2, 2), "anssi.png", "image/png")
    ctx.add(logo)
    executor.file_context = ctx
    result = await _call(executor, html=f'<img src="file:{logo.file_id}">' if len(logo.file_id) == 36 else f"<img src=\"[[FILE:{logo.file_id}]]\">")
    assert result.is_error is False, result.content
    assert "data:image/png;base64," in rendered[0]["html"]
    assert persisted[0]["raw_bytes"].startswith(b"<img src=\"file:") or persisted[0]["raw_bytes"].startswith(b"<img src=\"[[FILE:"), (
        "the SOURCE keeps the handle, the render gets the bytes"
    )


@pytest.mark.asyncio
async def test_render_refuses_bad_input_and_relays_a_browser_failure(monkeypatch):
    persisted: list[dict] = []
    _defs, executor, _rendered = _build(monkeypatch, persisted)
    executor.file_context = FileContext()

    empty = await _call(executor, html="   ")
    assert empty.is_error and "pass the HTML to render" in empty.content
    huge = await _call(executor, html="x" * (html_render.MAX_HTML_CHARS + 1))
    assert huge.is_error and "ceiling is 200,000" in huge.content
    wide = await _call(executor, html="<b/>", width=10_000)
    assert wide.is_error and "between 64 and 4096" in wide.content
    # Every dimension within its own bound, the product far over the area cap:
    # refused before the browser is launched, nothing rendered or stored.
    poster = await _call(executor, html="<b/>", width=MAX_CANVAS_PX, height=MAX_CANVAS_PX, scale=MAX_SCALE)
    assert poster.is_error and "device pixels; the ceiling is" in poster.content
    assert _rendered == []
    missing = await _call(executor, source_file_id="nope")
    assert missing.is_error and "no file 'nope'" in missing.content
    assert persisted == []


@pytest.mark.asyncio
async def test_render_bounds_the_document_after_conversation_images_are_inlined(monkeypatch):
    """The source cap is checked on what the model wrote; a ``file:<uuid>`` handle
    then expands to the stored bytes in base64, so the inlined document has a
    ceiling of its own - or one short source line hands Chromium an unbounded page."""
    persisted: list[dict] = []
    _defs, executor, rendered = _build(monkeypatch, persisted)
    ctx = FileContext()
    logo = FileRef.from_bytes(_png(2, 2), "anssi.png", "image/png")
    ctx.add(logo)
    executor.file_context = ctx
    handle = f"file:{logo.file_id}" if len(logo.file_id) == 36 else f"[[FILE:{logo.file_id}]]"
    html = f'<img src="{handle}">'
    assert len(html) < 80, "the SOURCE is tiny; only the inlined document is over the ceiling"

    monkeypatch.setattr(html_render, "MAX_INLINED_HTML_CHARS", 80)
    result = await _call(executor, html=html)
    assert result.is_error is True
    assert "with its conversation images inlined the document is" in result.content
    assert "the ceiling is 80" in result.content
    assert rendered == [], "the browser was never launched"
    assert persisted == [], "nothing was stored"

    async def _boom(html, **kwargs):
        raise RuntimeError("chromium is not installed")

    monkeypatch.setattr(html_render, "render_html", _boom)
    failed = await _call(executor, html="<b>x</b>")
    assert failed.is_error is True
    assert "could not render the HTML (chromium is not installed)" in failed.content
    assert persisted == []

    _defs, bare = build_generate_file_tool(db=None, user_id="", conversation_id=None)
    bare.file_context = FileContext()
    result = await bare.execute_tool(ToolCall(id="c2", name="render_html_to_image", arguments={"html": "<b/>"}))
    assert result.is_error and "needs a conversation or run" in result.content


@pytest.mark.asyncio
async def test_an_oversized_conversation_image_is_refused_before_it_is_downloaded(monkeypatch):
    """The inliner base64-encodes every referenced image whole; the size is
    read off the FileRef and refused BEFORE a byte is fetched, so a 40 MB
    upload named by handle costs nothing and hands Chromium nothing."""
    persisted: list[dict] = []
    _defs, executor, rendered = _build(monkeypatch, persisted)
    ctx = FileContext()
    huge = FileRef(
        file_id=str(uuid.uuid4()),
        filename="poster.png",
        content_type="image/png",
        size=html_render.MAX_INLINE_FILE_BYTES + 1,
        s3_object_name="chat/conv/poster.png",
    )
    ctx.add(huge)
    executor.file_context = ctx
    downloads: list[str] = []

    async def _never(file_id):
        downloads.append(file_id)
        return b""

    monkeypatch.setattr(ctx, "read_bytes", _never)
    result = await _call(executor, html=f'<img src="file:{huge.file_id}">')
    assert result.is_error is True
    assert "per-image ceiling for inlining: poster.png (8.0 MB)" in result.content
    assert downloads == [], "refused off the metadata, never downloaded"
    assert rendered == [] and persisted == []
    # A non-image file of the same size is not inlined at all, so it is not counted.
    ctx2 = FileContext()
    big_pdf = FileRef(
        file_id=str(uuid.uuid4()),
        filename="report.pdf",
        content_type="application/pdf",
        size=html_render.MAX_INLINE_FILE_BYTES + 1,
        s3_object_name="chat/conv/report.pdf",
    )
    ctx2.add(big_pdf)
    assert html_render.oversized_inline_refs(f'<a href="file:{big_pdf.file_id}">x</a>', ctx2) == []
    assert html_render.oversized_inline_refs("<b>no handles</b>", ctx2) == []
    assert html_render.oversized_inline_refs(f'<img src="file:{big_pdf.file_id}">', None) == []


def test_the_renderer_is_locked_down_like_the_pdf_renderer():
    import inspect

    source = inspect.getsource(html_render.render_html)
    assert "java_script_enabled=False" in source
    assert 'startswith(("data:", "about:", "blob:"))' in source
    assert "route.abort()" in source
    assert "_render_slot()" in source, "shares the export's Chromium slots"
    assert "args=list(RENDER_CHROMIUM_ARGS)" in source, "a dns-prefetch hint is not a request the route sees"
    assert "const CAP = 200;" in html_render.PROBE_JS, "the out / ovf lists are bounded in the page, not only in Python"
