"""Slide links INSIDE a deck: ``[text](#/<slide id>)`` in any markup field.

The grammar (``presentation_markup.py``) and its frontend mirror
(``markup.ts``), what a save tells the agent about links that land nowhere
(``slide_links_line``), and the exports: the PDF's anchors and the click
targets both PPTX writers lay over every link
(``pptx_writer.add_slide_link_hotspots`` fed by ``SLIDE_LINK_BOXES_JS``).
The frontend half is pinned by
``frontend/src/components/presentation/slide-links-in-deck.test.ts`` and
``slide-link-render.test.tsx``.
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path

import pytest
from pptx import Presentation as PptxPresentation
from pptx.enum.action import PP_ACTION
from pptx.oxml.ns import qn

from app.services import pptx_writer
from app.services.builtin_tools.presentation import _render_edit_account, slide_links_line
from app.services.presentation_export import SLIDE_LINK_BOXES_JS
from app.services.presentation_markup import (
    LINK_URL_PATTERN,
    MARKUP_SUMMARY,
    SLIDE_LINK_REF_PATTERN,
    InlineMarkupError,
    Segment,
    check_markup,
    fit_markup,
    literal_markdown_links,
    markup_to_plain_text,
    markup_to_plain_text_lenient,
    parse_inline_markup,
    slide_link_targets,
)
from app.services.presentation_patch import apply_operations
from app.services.presentation_spec import SLIDE_ID_PATTERN, PresentationSpecError, validate_spec

PRESENTATION_DIR = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"
PAGES_DIR = PRESENTATION_DIR.parent.parent / "pages"

#: A one-pixel PNG, the stand-in for a slide screenshot.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _linked_deck() -> dict:
    return validate_spec(
        {
            "title": "Roadmap",
            "slides": [
                {"layout": "title", "id": "cover", "title": "Roadmap", "subtitle": "2026"},
                {
                    "layout": "content",
                    "id": "q4",
                    "title": "Q4 in flight",
                    "blocks": [
                        {"type": "list", "items": ["[Gateway (Big Move)](#/gateway) - on track", "[Agents](#/agents)"]},
                    ],
                },
                {"layout": "content", "id": "gateway", "title": "Gateway", "blocks": [{"type": "text", "content": "x"}]},
                {
                    "layout": "content",
                    "id": "agents",
                    "title": "Agents",
                    "blocks": [{"type": "text", "content": "Back to [Q4](#/q4)."}],
                },
            ],
        }
    )


# ── Grammar ──────────────────────────────────────────────────────────────────


def test_a_slide_link_parses_as_one_segment_with_its_target():
    assert parse_inline_markup("See [Gateway](#/q4-gw) **now**") == [
        Segment("text", "See "),
        Segment("link", "Gateway", target="q4-gw"),
        Segment("text", " "),
        Segment("bold", "now"),
    ]
    assert markup_to_plain_text("See [Gateway](#/q4-gw)") == "See Gateway"
    assert markup_to_plain_text_lenient("See [Gateway](#/q4-gw)") == "See Gateway"


@pytest.mark.parametrize(
    "value",
    [
        "[site](http://filigran.io)",
        "[x](javascript:alert(1))",
        "[x](data:text/html,hi)",
        "[x](#top)",
        "[x](/presentations/abc)",
        "[x](mailto:nobody)",
        "a [bracket] b",
    ],
    ids=["http", "javascript", "data", "fragment", "path", "mailto-no-domain", "brackets"],
)
def test_any_other_markdown_link_stays_literal_text(value):
    assert parse_inline_markup(value) == [Segment("text", value)]
    assert slide_link_targets(value) == []


def test_a_web_or_mail_link_parses_with_its_address_even_between_parentheses():
    value = (
        "Open: deeper PDF ([#321](https://github.com/XTM-One-Platform/xtm-one/issues/321)), "
        "mail [us](mailto:team@filigran.io)."
    )
    assert parse_inline_markup(value) == [
        Segment("text", "Open: deeper PDF ("),
        Segment("link", "#321", target="https://github.com/XTM-One-Platform/xtm-one/issues/321"),
        Segment("text", "), mail "),
        Segment("link", "us", target="mailto:team@filigran.io"),
        Segment("text", "."),
    ]
    # A web link is no slide link: it is never reported as one landing nowhere.
    assert slide_link_targets(value) == []
    assert literal_markdown_links(value) == []


def test_the_link_address_grammar_is_the_banner_action_s_minus_parentheses():
    from app.services.presentation_spec import check_action_url

    for address in ("https://github.com/a/b/issues/1?x=1#y", "mailto:team@filigran.io"):
        assert re.fullmatch(LINK_URL_PATTERN, address)
        assert check_action_url(address) == address
    for address in ("http://x.io", "javascript:alert(1)", "https://x.io/a(b)", 'https://x.io/"', "https://x .io"):
        assert not re.fullmatch(LINK_URL_PATTERN, address)


def test_literal_markdown_links_names_what_the_slide_shows_raw():
    assert literal_markdown_links("a [x](http://x.io) b [y](javascript:alert(1)) [z](https://ok.io) [s](#/q4)") == [
        "[x](http://x.io)",
        "[y](javascript:alert(1)",
    ]


@pytest.mark.parametrize(
    ("value", "words"),
    [
        ("[](#/abc)", "unbalanced inline marker ']("),
        ("[x](#/has space)", "unbalanced inline marker ']("),
        ("[x](#/" + "a" * 65 + ")", "unbalanced inline marker ']("),
        ("[**x**](#/abc)", "the [text](...) link contains another marker"),
        ("[**x**](https://x.io)", "the [text](...) link contains another marker"),
        ("**see [x](#/abc)**", "the **bold** span contains another marker"),
        ("[[accent:[x](#/abc)]]", "unbalanced inline marker"),
    ],
    ids=["no-label", "space-in-ref", "ref-too-long", "markup-in-label", "markup-in-web-label", "link-in-bold", "link-in-span"],
)
def test_a_malformed_slide_link_is_refused_with_the_shape_to_write(value, words):
    with pytest.raises(InlineMarkupError) as excinfo:
        parse_inline_markup(value)
    assert words in str(excinfo.value)
    if "unbalanced" in words:
        assert "[text](#/SLIDE_ID)" in str(excinfo.value)


def test_the_ref_grammar_is_the_slide_id_grammar():
    assert f"^{SLIDE_LINK_REF_PATTERN}$" == SLIDE_ID_PATTERN


def test_the_grammar_summary_names_the_slide_link():
    assert "[text](#/SLIDE_ID)" in MARKUP_SUMMARY


def test_fit_markup_keeps_a_link_that_fits_and_flattens_one_that_does_not():
    assert fit_markup("Go to [Gateway](#/gw)", 80) == "Go to [Gateway](#/gw)"
    assert fit_markup("Go to [Gateway](#/gw)", 12) == "Go to Gat..."


def test_a_deck_with_slide_links_validates_and_keeps_them():
    spec = _linked_deck()
    assert spec["slides"][1]["blocks"][0]["items"][0]["text"].startswith("[Gateway (Big Move)](#/gateway)")
    with pytest.raises(PresentationSpecError):
        validate_spec({"title": "x", "slides": [{"layout": "section", "title": "[](#/a)"}]})


def test_check_markup_accepts_a_slide_link():
    assert check_markup("[Gateway](#/gateway)") == "[Gateway](#/gateway)"


def test_slide_link_targets_reads_like_the_renderer():
    assert slide_link_targets("a [b](#/c) d [e](#/f_g) [h](https://x)") == ["c", "f_g"]
    # Notes are not validated: a link in a string the strict parser refuses is still read.
    assert slide_link_targets("**open [b](#/c)") == ["c"]
    assert slide_link_targets(None) == []


# ── What a save tells the agent ──────────────────────────────────────────────


def test_a_markdown_link_the_slide_shows_raw_is_named_with_its_slide():
    spec = _linked_deck()
    spec["slides"][2]["blocks"][0]["content"] = "See [the RFC](http://example.org/rfc) and [#321](https://github.com/x/y/issues/321)."
    lines = slide_links_line(spec).splitlines()
    assert lines == [
        "Slide links: 3 links, every one lands on a slide of this deck.",
        "Not links: 1 Markdown link shown as raw text (slide 3: [the RFC](http://example.org/rfc)). "
        "A link points at a slide (#/SLIDE_ID), an https:// page or a mailto: address.",
    ]


def test_a_deck_without_slide_links_says_nothing():
    spec = validate_spec({"title": "x", "slides": [{"layout": "section", "title": "Hello"}]})
    assert slide_links_line(spec) == ""


def test_every_link_landing_on_a_slide_is_counted():
    assert slide_links_line(_linked_deck()) == "Slide links: 3 links, every one lands on a slide of this deck."


def test_a_link_to_no_slide_is_named_with_its_slide_and_how_to_fix_it():
    spec = _linked_deck()
    spec["slides"][3]["blocks"][0]["content"] = "Back to [Q4](#/4) and [Q3](#/q3)."
    line = slide_links_line(spec)
    assert line.startswith("Slide links: 4 links, 2 pointing at no slide of this deck (slide 4 -> #/4, slide 4 -> #/q3)")
    assert "never by its number" in line


def test_deleting_a_linked_slide_is_reported_in_the_edit_account():
    spec = _linked_deck()
    outcome = apply_operations(spec, [{"op": "delete_slide", "slide": "gateway"}])
    account = _render_edit_account(
        outcome.spec,
        deck_id="00000000-0000-0000-0000-000000000001",
        revision=3,
        previous_revision=2,
        outcome=outcome,
        summary="Removed the Gateway slide",
    )
    lines = [line for line in account.splitlines() if line.startswith("Slide links:")]
    assert lines == [
        "Slide links: 3 links, 1 pointing at no slide of this deck (slide 2 -> #/gateway) - drawn as plain "
        "text. A link names its target by the slide's id (the ids get_presentation lists), never by its number."
    ]


def test_more_broken_links_than_listed_are_counted_in_full():
    slides = [{"layout": "section", "id": "s0", "title": "Index"}] + [
        {"layout": "section", "id": f"s{i}", "title": f"[go](#/missing{i})"} for i in range(1, 16)
    ]
    line = slide_links_line(validate_spec({"title": "x", "slides": slides}))
    assert "15 links, 15 pointing at no slide" in line
    assert line.count(" -> #/") == 12
    assert "and 3 more" in line


# ── Exports ──────────────────────────────────────────────────────────────────


def _hotspots(prs) -> list[tuple[int, str, int | None]]:
    """(slide position, shape name, target slide position) of every link target."""
    slides = list(prs.slides)
    found = []
    for position, slide in enumerate(slides):
        for shape in slide.shapes:
            if shape.click_action.action != PP_ACTION.NAMED_SLIDE:
                continue
            target = shape.click_action.target_slide
            found.append((position, shape.name, slides.index(target) if target is not None else None))
    return found


_BOXES = [
    [{"x": 100, "y": 200, "w": 300, "h": 40, "target": 2}, {"x": 0, "y": 0, "w": 10, "h": 10, "target": 0}],
    [{"x": 10, "y": 20, "w": 30, "h": 40, "target": 9}, {"x": 10, "y": 20, "w": 0, "h": 40, "target": 0}],
    [{"x": 50, "y": 60, "w": 70, "h": 80, "target": 0}, {"x": 1, "y": 1, "w": 1, "h": 1, "target": True}],
]
_WEB_BOXES = [
    [
        {"x": 10, "y": 20, "w": 30, "h": 40, "href": "https://github.com/x/y/issues/321"},
        {"x": 10, "y": 20, "w": 30, "h": 40, "href": "mailto:team@filigran.io"},
        # A measured value is not trusted: anything but the link grammar writes nothing.
        {"x": 10, "y": 20, "w": 30, "h": 40, "href": "javascript:alert(1)"},
        {"x": 10, "y": 20, "w": 30, "h": 40, "href": "http://x.io"},
        {"x": 10, "y": 20, "w": 30, "h": 40, "href": 7},
    ]
]


def test_both_pptx_variants_lay_a_hyperlink_over_each_web_link():
    measured = [{"background": {"r": 0, "g": 0, "b": 0, "a": 1}, "items": []}]
    for data in (
        pptx_writer.build_image_pptx([_PNG], aspect_ratio="16:9", slide_links=_WEB_BOXES),
        pptx_writer.build_native_pptx(measured, captures={}, aspect_ratio="16:9", slide_links=_WEB_BOXES),
    ):
        slide = PptxPresentation(io.BytesIO(data)).slides[0]
        links = [
            (shape.name, shape.click_action.hyperlink.address)
            for shape in slide.shapes
            if shape.click_action.action == PP_ACTION.HYPERLINK
        ]
        assert links == [
            ("Link to https://github.com/x/y/issues/321", "https://github.com/x/y/issues/321"),
            ("Link to mailto:team@filigran.io", "mailto:team@filigran.io"),
        ]



def test_the_picture_pptx_lays_a_jump_target_over_each_link():
    prs = PptxPresentation(io.BytesIO(pptx_writer.build_image_pptx([_PNG] * 3, aspect_ratio="16:9", slide_links=_BOXES)))
    # A link to its own slide, past the deck, zero-sized or with no integer target writes nothing.
    assert _hotspots(prs) == [(0, "Link to slide 3", 2), (2, "Link to slide 1", 0)]
    shape = next(s for s in prs.slides[0].shapes if s.name == "Link to slide 3")
    emu_per_px = prs.slide_width / 1920
    assert (shape.left, shape.top, shape.width, shape.height) == tuple(int(v * emu_per_px) for v in (100, 200, 300, 40))
    # Hit everywhere in a slide show, painted nowhere: a fill at 0% opacity, no outline.
    fill = shape._element.spPr.find(qn("a:solidFill"))
    assert fill.find(qn("a:srgbClr")).find(qn("a:alpha")).get("val") == "0"
    assert shape._element.spPr.find(qn("a:ln")).find(qn("a:noFill")) is not None


def test_the_editable_pptx_lays_the_jump_targets_above_the_slide():
    measured = [
        {"background": {"r": 0, "g": 0, "b": 0, "a": 1}, "items": [{"kind": "rect", "x": 0, "y": 0, "w": 50, "h": 50}]}
        for _ in range(3)
    ]
    data = pptx_writer.build_native_pptx(measured, captures={}, aspect_ratio="16:9", slide_links=_BOXES)
    prs = PptxPresentation(io.BytesIO(data))
    assert _hotspots(prs) == [(0, "Link to slide 3", 2), (2, "Link to slide 1", 0)]
    assert list(prs.slides[0].shapes)[-1].name == "Link to slide 3"


def test_a_deck_with_no_link_exports_no_jump_target():
    prs = PptxPresentation(io.BytesIO(pptx_writer.build_image_pptx([_PNG, _PNG], aspect_ratio="16:9")))
    assert _hotspots(prs) == []


def test_the_exporter_measures_the_anchors_the_renderer_draws():
    """One attribute, three readers: the renderer writes it, the box probe and
    the export page's anchors read it."""
    renderer = (PRESENTATION_DIR / "inline-markup.tsx").read_text(encoding="utf-8")
    assert "data-slide-link-index={index}" in renderer
    assert 'a[data-slide-link-index], a[data-link-href]' in SLIDE_LINK_BOXES_JS
    assert "data-link-href={target}" in renderer
    assert 'rel="noopener noreferrer"' in renderer
    assert ".deck-static-slide" in SLIDE_LINK_BOXES_JS
    context = (PRESENTATION_DIR / "slide-link-context.ts").read_text(encoding="utf-8")
    assert "return `deck-slide-${index}`;" in context
    assert "id={anchored ? slideAnchorId(index) : undefined}" in (PRESENTATION_DIR / "StaticSlide.tsx").read_text(
        encoding="utf-8"
    )
    static_page = (PAGES_DIR / "PresentationStaticRender.tsx").read_text(encoding="utf-8")
    assert "useSlideLinksValue(slides, undefined, slideAnchorHref)" in static_page


def test_render_deck_measures_the_links_for_both_pptx_variants():
    import inspect

    from app.services import presentation_export

    # render_deck retries the single attempt, which is where the page is measured.
    source = inspect.getsource(presentation_export._render_deck_once)
    assert "if want_slide_images or want_primitives:\n            result.slide_links = await page.evaluate(SLIDE_LINK_BOXES_JS)" in source
    exporter = inspect.getsource(presentation_export.export_presentation)
    assert exporter.count("slide_links=rendered.slide_links") == 2


# ── The frontend mirror ──────────────────────────────────────────────────────


def test_the_frontend_tokenizer_reads_the_same_slide_link():
    markup = (PRESENTATION_DIR / "markup.ts").read_text(encoding="utf-8")
    token = re.search(r"const TOKEN_RE =\s*/(.+)/g;", markup)
    assert token is not None
    # The link alternative, in the backend's order: after the span, before emphasis.
    alternatives = token.group(1)
    url = LINK_URL_PATTERN.replace("/", r"\/")
    link = r"\[([^[\]\n]+)\]\((?:#\/(" + SLIDE_LINK_REF_PATTERN + r")|(" + url + r"))\)"
    assert link in alternatives
    assert alternatives.index(r"\[\[") < alternatives.index(link) < alternatives.index("(?<![A-Za-z0-9_])_")
    # The strict reading knows the link's marker.
    assert r"const MARKER_RE = /\*\*|`|\[\[|\]\]|\]\(#\//;" in markup
    # The editor's DOM half builds the link element, never parses HTML.
    dom = (PRESENTATION_DIR / "inline-runs-dom.ts").read_text(encoding="utf-8")
    assert 'doc.createElement("a")' in dom
    assert "innerHTML" not in dom
