"""The template's brand mark is drawn once on a frame.

A passed ultra render showed the watermark (``theme.watermark``) and a
statement's logo (``show_logo``) drawing the template's mark on the same
frame. What is proven:

* a statement does not draw its logo while the watermark is on
  (``spec.card_draws_logo``): the planner leaves it out, a title or closing
  card keeps its own (the watermark steps aside for those);
* played frame by frame in a real Chromium, a statement with ``show_logo``
  under a watermark draws one mark on every frame, and the quality check's
  audit finds nothing;
* the audit counts the marks each frame draws - the watermark, a card's
  logo, an image overlay of a brand mark - and reports an image overlay of
  the template's mark under the watermark as "brand mark drawn twice", with
  its frame and how to keep one.

The Chromium tests are skipped where Playwright's Chromium is not installed;
the ``Browser tool (Chromium)`` CI job sets ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import os

import pytest

from app.services.video_studio import quality, timeline
from app.services.video_studio.spec import card_draws_logo, parse_video_spec
from app.services.video_studio.themes import build_theme


def _spec(*, watermark: str = "bottom_right", proof: dict | None = None, transition: str = "slide_left") -> object:
    """A title card, then three statements - the middle one showing its logo - then a closing card.

    Slow slides: the scene going out stays in view, its logo with it, for two seconds of the next one.
    """
    return parse_video_spec(
        {
            "title": "Brand once",
            "freestyle": True,
            "theme": {"watermark": watermark},
            "default_transition": {"type": transition, "duration": 2.0},
            "scenes": [
                {"id": "open", "type": "title", "title": "Know your adversaries", "show_logo": True},
                {"id": "before", "type": "statement", "lines": ["Too much *noise*."]},
                {
                    "id": "proof",
                    "type": "statement",
                    "lines": ["Six thousand teams"],
                    "align": "left",
                    "show_logo": True,
                    **(proof or {}),
                },
                {"id": "after", "type": "statement", "lines": ["One platform."]},
                {"id": "close", "type": "outro", "title": "See you there", "show_logo": True},
            ],
        }
    )


def _document(spec) -> dict:
    theme = build_theme(mode="dark", background_style="aurora", has_logo=True, has_mark=True)
    return timeline.plan(spec, timeline.PlanMedia(), fps=30, theme=theme).document


def _scenes(document: dict) -> dict[str, dict]:
    return {scene["id"]: scene for scene in document["scenes"]}


def test_a_statement_under_a_watermark_is_planned_without_its_logo():
    planned = _scenes(_document(_spec()))
    assert planned["proof"]["show_logo"] is False
    # The cards that carry the brand keep their logo: the watermark steps aside for them.
    assert planned["open"]["show_logo"] is True and planned["close"]["show_logo"] is True
    # Without a watermark, the statement draws the logo it asks for.
    assert _scenes(_document(_spec(watermark="none")))["proof"]["show_logo"] is True


def test_the_rule_is_one_function_of_the_card_its_switch_and_the_watermark():
    assert card_draws_logo("statement", True, "none") and card_draws_logo("statement", True, None)
    assert not card_draws_logo("statement", True, "top_left") and not card_draws_logo("statement", False, "none")
    assert card_draws_logo("title", True, "bottom_right") and card_draws_logo("outro", True, "bottom_right")


def test_an_image_overlay_of_a_brand_mark_is_planned_as_one():
    spec = _spec(proof={"show_logo": False, "overlays": [{"type": "image", "asset": "logo_mark", "position": "bottom_left"}]})
    (overlay,) = _scenes(_document(spec))["proof"]["overlays"]
    assert overlay["brand"] == "logo_mark" and overlay["src"] == "overlay/logo_mark"


def test_the_audit_s_brand_twice_reads_as_a_finding_naming_the_frame_and_the_switches():
    document = _document(_spec())
    observed = {
        "kind": "brand_twice",
        "marks": [{"mark": "overlay:logo_mark", "scene": "proof"}, {"mark": "watermark", "scene": None}],
        "at": 1.2,
        "video_at": 9.53,
    }
    index = [scene["id"] for scene in document["scenes"]].index("proof")
    audit = {"scenes": [{"index": index, "id": "proof", "findings": [observed]}]}
    (finding,) = [f for f in quality.findings_from_audit(audit, document, None) if f.kind == "brand_mark_twice"]
    assert (finding.scene_id, finding.severity, finding.group) == ("proof", "dense", "content")
    assert finding.message == (
        "brand mark drawn twice on the frame at 0:09.53 (1.2 s into the scene): its image overlay of the template's "
        "logo_mark and the watermark draw the brand on the same frame - keep one (drop its image overlay of the "
        "logo_mark; theme.watermark: none)"
    )
    # A card's logo on the scene before, under a transition: named with its scene and its switch.
    observed["marks"] = [{"mark": "logo", "scene": "before"}, {"mark": "watermark", "scene": None}]
    (finding,) = [f for f in quality.findings_from_audit(audit, document, None) if f.kind == "brand_mark_twice"]
    assert "scene before's logo and the watermark" in finding.message
    assert "show_logo: false on scene before" in finding.message


# ── In a real Chromium ────────────────────────────────────────────────────


def _skip_without_chromium(exc: Exception) -> None:
    text = str(exc)
    if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
        raise exc
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
    pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


async def _marks_per_frame(document: dict, step: int = 3) -> dict[int, list[dict]]:
    """The brand marks every *step*-th frame of *document* draws, played in the compositor page as the quality check plays it."""
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(viewport={"width": int(document["width"]), "height": int(document["height"])})
                await context.route("**/*", handle)
                page = await context.new_page()
                await page.goto(index, wait_until="load")
                await page.evaluate("doc => window.__xtm.load(doc)", {**document, "preview_media": {}})
                return {
                    frame: await page.evaluate("frame => window.__xtm.marks(frame)", frame)
                    for frame in range(0, int(document["frames"]), step)
                }
            finally:
                await browser.close()
    except Exception as exc:
        _skip_without_chromium(exc)
        raise


async def _audit(document: dict) -> dict:
    try:
        return await quality.measure_document(document)
    except Exception as exc:
        _skip_without_chromium(exc)
        raise


@pytest.mark.chromium
@pytest.mark.parametrize("transition", ["slide_left", "crossfade", "wipe"])
async def test_a_statement_with_its_logo_under_a_watermark_draws_one_mark_on_every_frame(transition):
    document = _document(_spec(transition=transition))
    frames = await _marks_per_frame(document)
    # The watermark stands over the statements - the mark the audience sees - and never beside a second one,
    # nor beside a card's logo while that card goes out or comes in.
    proof = _scenes(document)["proof"]
    inside = [marks for frame, marks in frames.items() if proof["start"] + 2.0 <= frame / 30 < proof["end"] - 0.5]
    assert inside and all(marks == [{"mark": "watermark", "scene": None}] for marks in inside)
    assert max(len(marks) for marks in frames.values()) == 1, {f: m for f, m in frames.items() if len(m) > 1}
    findings = quality.findings_from_audit(await _audit(document), document, None)
    assert [f.message for f in findings if f.kind == "brand_mark_twice"] == []


@pytest.mark.chromium
async def test_two_cards_showing_their_logo_through_a_slow_slide_are_reported_drawn_twice():
    """The audit counts across the scenes a transition shows: the card going out and the one coming in."""
    spec = parse_video_spec(
        {
            "title": "Brand twice",
            "freestyle": True,
            "default_transition": {"type": "slide_left", "duration": 2.0},
            "scenes": [
                {"id": "open", "type": "title", "title": "Know your adversaries", "show_logo": True},
                {"id": "proof", "type": "statement", "lines": ["Six thousand teams"], "align": "left", "show_logo": True},
            ],
        }
    )
    document = _document(spec)
    frames = await _marks_per_frame(document, step=1)
    doubled = [marks for marks in frames.values() if len(marks) > 1]
    assert doubled and all(sorted(m["scene"] for m in marks) == ["open", "proof"] for marks in doubled)
    found = [f for f in quality.findings_from_audit(await _audit(document), document, None) if f.kind == "brand_mark_twice"]
    assert [f.scene_id for f in found] == ["proof"]
    assert "scene open's logo and its logo draw the brand on the same frame" in found[0].message
    assert "keep one (show_logo: false on scene open; show_logo: false)" in found[0].message


@pytest.mark.chromium
async def test_an_overlay_of_the_mark_under_the_watermark_is_reported_drawn_twice_with_its_frame():
    overlay = {"type": "image", "asset": "logo_mark", "position": "bottom_left", "start": 0.5, "duration": 2.5}
    document = _document(_spec(proof={"show_logo": False, "overlays": [overlay]}))
    audit = await _audit(document)
    observed = {scene["id"]: f for scene in audit["scenes"] for f in scene["findings"] if f["kind"] == "brand_twice"}
    # On the scene showing it, and on the next one's opening, while the slide still shows it.
    assert all(sorted(m["mark"] for m in f["marks"]) == ["overlay:logo_mark", "watermark"] for f in observed.values())
    proof = _scenes(document)["proof"]
    assert proof["start"] + 0.5 <= observed["proof"]["video_at"] <= proof["start"] + 3.0
    found = {f.scene_id: f for f in quality.findings_from_audit(audit, document, None) if f.kind == "brand_mark_twice"}
    assert set(found) == set(observed) and found["proof"].message.startswith("brand mark drawn twice on the frame at 0:")
    assert "keep one (drop its image overlay of the logo_mark; theme.watermark: none)" in found["proof"].message
    assert all("scene proof's image overlay" in f.message for scene_id, f in found.items() if scene_id != "proof")
