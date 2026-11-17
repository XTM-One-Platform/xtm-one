"""The presence gate inside every window that dims or blurs an insert's scene.

Synthetic fixtures only (``tests.video_studio_insert_fixture``): each scene's keyed insert drawn onto its own footage,
the scenes' layers set as the page's ``applyTransition`` sets them, its flash and the video's fades over them, each
frame encoded; where the page places each insert and how much of it shows as its ``insertRects`` says. Each case passes
with the insert drawn and fails as missing with its source removed on the frames the gate judges it on there:

* an insert entering on a crossfade, a zoom, a flash and a wipe, the scene it leaves showing another UI - a wipe's
  mask hides the scene coming in where its edge has not passed, which ``insertRects`` counts;
* an insert under the video's fade in and fade out;
* a scene its transitions cover whole, no un-transitioned frame of it to compare with: judged by the frame around it;
* a high-contrast source under a blur, over the floor there and read by its structure - another picture drawn instead
  is its source absent inside the window;
* a zoom blurring a noisy wall 8 CSS pixels and more on the frames it is judged on.
"""

from __future__ import annotations

import functools
import os

import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import compositor_page, insert_checks
from app.services.video_studio.render_checks import FAIL, INSERT_MISSING
from tests.video_studio_insert_fixture import (
    FPS,
    GROUNDS,
    PresenceRender,
    booth,
    clip_document,
    key_of,
    text_ui,
    tiled_booth,
    transition_layers,
)

COMING, LEAVING = key_of("b"), key_of("a")
#: Scene ``a`` showing a UI of long lines, unlike the short labels of scene ``b``'s: a quad read on the wrong scene is
#: never its source.
OTHER = {"a": text_ui(GROUNDS["a"])}


def _two(kind: str, seconds: float = 0.5) -> tuple:
    return (("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 1.7, 3.2, 1.5, kind, seconds))


#: Scene ``b`` between two blurs that cover it whole: from 0.9 s to 1.9 s, scene ``a`` painted to 1.4 s, ``c`` from it.
COVERED = (("a", 0.0, 1.4, 1.4, "cut", 0.0), ("b", 0.9, 1.9, 1.0, "blur", 0.55), ("c", 1.4, 3.0, 1.6, "blur", 0.55))
#: One scene with its insert, the video fading in and out over half a second.
FADED = (("b", 0.0, 2.5, 2.5, "cut", 0.0),)


def _document(scenes: tuple, *, fades: float = 0.0) -> dict:
    document = clip_document(list(scenes))
    document["fade_in"] = document["fade_out"] = fades
    return document


def _judged(render_: PresenceRender, gate: insert_checks.PresenceGate, window: str, key: str = COMING) -> list[int]:
    """The frames of *window* the gate judges *key* on: shown whole there, its scene's, and not hidden by the video's
    fade past the floor."""
    where = insert_checks.placed_inserts(render_.document, render_.inserts)[key]
    return [
        n
        for n in where.whole_frames()
        if (hit := where.blend_at(n)) is not None
        and hit[2] == window
        and where.owns_frame(n, gate._placed[key])
        and gate._placed[key][n][4] >= insert_checks.PRESENCE_MIN_SHOWN
        and insert_checks.video_fade_left(render_.document, n / FPS) >= insert_checks.PRESENCE_MIN_SHOWN
    ]


@functools.cache
def _present(scenes: tuple, fades: float = 0.0, other: bool = True) -> tuple[PresenceRender, insert_checks.PresenceGate, insert_checks.Presence]:
    render_ = PresenceRender(_document(scenes, fades=fades), pictures=OTHER if other else None)
    gate = render_.gate()
    return render_, gate, gate.result()


def _missing(result: insert_checks.Presence, key: str = COMING):
    hits = [finding for finding, _proof in result.findings if finding.data["insert"] == key]
    assert hits, f"{key} read present with its source removed"
    (finding,) = hits
    assert finding.kind == INSERT_MISSING and finding.severity == FAIL
    return finding


def _bars() -> Image.Image:
    """Another high-contrast picture than the source: light bars across a dark ground."""
    picture = Image.new("RGB", (600, 450), (20, 20, 24))
    draw = ImageDraw.Draw(picture)
    for column in range(0, 600, 60):
        draw.rectangle((column, 0, column + 24, 450), fill=(220, 220, 230))
    return picture


def _unrecognised(monkeypatch) -> None:
    """A quad under the floor whose source's structure the gate does not read: the frame around it judges it."""
    original = insert_checks.structural_similarity

    def blind(seen, expected, mask):
        if insert_checks.detail_variance(seen, mask) < insert_checks.FLAT_VAR_MIN:
            return 0.0
        return original(seen, expected, mask)

    monkeypatch.setattr(insert_checks, "structural_similarity", blind)


# ── Entering and leaving on each transition ───────────────────────────────

TRANSITIONS = ("crossfade", "zoom", "flash", "wipe")


@pytest.mark.parametrize("kind", TRANSITIONS)
def test_an_insert_entering_and_one_leaving_on_the_transition_are_present(kind):
    render_, gate, result = _present(_two(kind))
    assert [finding.message for finding, _ in result.findings] == []
    # Each read inside the window - the one coming in as it enters, the one leaving as it goes.
    assert _judged(render_, gate, f"{kind} in")
    assert _judged(render_, gate, f"{kind} out", LEAVING)
    for key in (COMING, LEAVING):
        assert result.by_key[key]["missing"] == 0 and result.by_key[key]["joins"] >= 1


@pytest.mark.parametrize("kind", TRANSITIONS)
def test_the_insert_entering_with_its_source_removed_inside_the_transition_is_missing(kind):
    render_, gate, _result = _present(_two(kind))
    removed = _judged(render_, gate, f"{kind} in")
    result = PresenceRender(_document(_two(kind)), pictures=OTHER, removed=set(removed)).gate().result()
    finding = _missing(result)
    assert finding.data["why"] == "flat" and finding.data["frame"] == removed[0] and finding.data["frames"] == len(removed)
    assert f"inside the {kind} in: flat against the frame around it" in finding.message
    assert result.by_key[LEAVING]["missing"] == 0


def test_a_wipe_shows_the_scene_coming_in_where_its_edge_has_passed():
    render_, _gate, _result = _present(_two("wipe"))
    shown = [render_.audits[COMING]["placed"][n][4] for n in range(41, 53)]
    # Nothing of it over the quad until the edge reaches it, then more of it each frame - never whole from the first.
    assert shown[0] == 0.0 and shown[-1] == 1.0 and shown == sorted(shown)
    assert any(0.0 < share < insert_checks.PRESENCE_MIN_SHOWN for share in shown)
    script = compositor_page.page_html("")
    assert "let shown = shownAlpha(node) * (built.wipe != null ? spanned(built, r) : 1);" in script
    assert "return spanned(over, rect) * parseFloat(getComputedStyle(over.layer).opacity);" in script


def _wiped_spec() -> tuple[dict, str]:
    """A statement wiped in over another, a screen insert on the middle half of its frame."""
    from app.services.video_studio import themes, timeline
    from app.services.video_studio.spec import parse_video_spec, validate_video_spec

    insert = {
        "type": "screen_insert",
        "source": {"file_id": "00000000-0000-0000-0000-000000006426"},
        "quad": [[0.25, 0.3], [0.75, 0.3], [0.75, 0.75], [0.25, 0.75]],
        "duration": 3.0,
    }
    raw = {
        "title": "Wiped",
        "freestyle": True,
        "format": {"aspect_ratio": "16:9"},
        "captions": {"enabled": False},
        "theme": {"watermark": "none"},
        "scenes": [
            {"id": "one", "type": "statement", "lines": ["Every alert"], "duration": 3.0},
            {
                "id": "two",
                "type": "statement",
                "lines": ["In one place"],
                "duration": 3.0,
                "backdrop": {"file_id": "00000000-0000-0000-0000-000000006427"},
                "transition": {"type": "wipe", "duration": 0.8},
                "overlays": [insert],
            },
        ],
    }
    spec = parse_video_spec(validate_video_spec(raw))
    media = timeline.PlanMedia()
    media.backdrops["two"] = timeline.ImageMedia(1600, 900)
    media.inserts[timeline.insert_key("two", 0)] = timeline.ImageMedia(1280, 720)
    media.inserts_gathered = True
    theme = themes.build_theme(mode=spec.theme.mode, background_style=spec.theme.background)
    return timeline.plan(spec, media, fps=FPS, theme=theme).document, timeline.insert_key("two", 0)


@pytest.mark.chromium
async def test_in_a_real_chromium_insert_rects_shows_a_wiped_in_insert_as_far_as_the_edge_has_passed():
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    document, key = _wiped_spec()
    scene = document["scenes"][1]
    start, seconds = float(scene["start"]), float(scene["transition_in"]["duration"])
    fps = float(document["fps"])
    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    def at(p: float) -> int:
        """The frame at progress *p* of the wipe."""
        return round((start + p * seconds) * fps)

    shown: dict[float, float] = {}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(viewport={"width": int(document["width"]), "height": int(document["height"])})
                await context.route("**/*", handle)
                page = await context.new_page()
                await page.goto(index, wait_until="load")
                await page.evaluate("doc => window.__xtm.load(doc)", document)
                for p in (0.35, 0.5, 1.2):
                    await page.evaluate("i => window.__xtm.render(i)", at(p))
                    rects = await page.evaluate("() => window.__xtm.insertRects()")
                    shown[p] = rects[key][4] if key in rects else 0.0
            finally:
                await browser.close()
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    # The edge (``e * 116 - 8`` percent of the stage: 19% at 0.35, 50% at 0.5) short of the quad's left side at a
    # quarter of the stage, then across half of it, then past it: shown as far as it has passed, never whole while the
    # scene left shows there.
    assert transition_layers("wipe", 0.35)[1].wipe < 25.0
    assert shown[0.35] == pytest.approx(0.0, abs=0.02)
    assert 0.3 < shown[0.5] < 0.7
    assert shown[1.2] == pytest.approx(1.0, abs=0.01)


# ── The video's fades ─────────────────────────────────────────────────────


def test_an_insert_under_the_video_s_fade_in_and_fade_out_is_present():
    render_, gate, result = _present(FADED, 0.5, False)
    assert [finding.message for finding, _ in result.findings] == []
    # Judged on the fades' frames that leave the floor of the frame or more; the darker ones not judged, a fact.
    for window in ("video fade in", "video fade out"):
        assert _judged(render_, gate, window)
        assert set(_judged(render_, gate, window)) <= set(gate.read_frames[COMING])
    assert result.by_key[COMING]["faded"] >= 2
    assert result.by_key[COMING]["missing"] == 0


@pytest.mark.parametrize("window", ["video fade in", "video fade out"])
def test_an_insert_with_its_source_removed_under_the_video_s_fade_is_missing(window):
    render_, gate, _result = _present(FADED, 0.5, False)
    # The fade's first frame shows black alone: nothing of anything to judge there.
    removed = [n for n in _judged(render_, gate, window) if n > 0]
    result = PresenceRender(_document(FADED, fades=0.5), removed=set(removed)).gate().result()
    finding = _missing(result)
    assert finding.data["frame"] == removed[0] and finding.data["frames"] == len(removed)


# ── A scene its transitions cover ─────────────────────────────────────────


def test_a_scene_its_transitions_cover_is_judged_by_the_whole_frame_around_it(monkeypatch):
    _unrecognised(monkeypatch)
    render_ = PresenceRender(_document(COVERED))
    gate = render_.gate()
    result = gate.result()
    # No frame of scene b's shows it alone: nothing beside its windows to compare its quad with.
    assert gate.references[COMING] == set()
    assert _judged(render_, gate, "blur in") or _judged(render_, gate, "blur out")
    assert [finding.message for finding, _ in result.findings] == []
    (dimmed,) = result.by_key[COMING]["dimmed"]
    assert dimmed["frames"] >= 1 and dimmed["whole_frame"] == dimmed["frames"] and dimmed["recognised"] == 0


def test_a_scene_its_transitions_cover_with_its_source_removed_on_a_textured_wall_is_missing():
    probe = PresenceRender(_document(COVERED), wall=tiled_booth)
    gate = probe.gate()
    removed = sorted({*_judged(probe, gate, "blur in"), *_judged(probe, gate, "blur out")})
    assert removed
    result = PresenceRender(_document(COVERED), wall=tiled_booth, removed=set(removed)).gate().result()
    finding = _missing(result)
    assert finding.data["why"] == "flat" and finding.data["frames"] == len(removed)
    assert "flat against the frame around it" in finding.message


# ── A high-contrast source under a blur ───────────────────────────────────

#: Scene b's insert: light text on a near-black ground, which a transition's blur leaves over the floor.
CONTRAST = {"b": text_ui((12, 14, 18))}


def test_a_high_contrast_source_under_a_blur_is_read_by_its_structure():
    render_ = PresenceRender(_document(_two("blur", 0.55)), pictures=CONTRAST)
    gate = render_.gate()
    result = gate.result()
    assert [finding.message for finding, _ in result.findings] == []
    assert _judged(render_, gate, "blur in")
    # Over the floor on every frame of the blur: never dimmed, its source's structure compared there.
    assert result.by_key[COMING]["missing"] == 0 and "dimmed" not in result.by_key[COMING]


def test_another_picture_on_a_high_contrast_insert_inside_a_blur_is_its_source_absent():
    probe = PresenceRender(_document(_two("blur", 0.55)), pictures=CONTRAST)
    gate = probe.gate()
    removed = _judged(probe, gate, "blur in")
    result = PresenceRender(_document(_two("blur", 0.55)), pictures=CONTRAST, removed=set(removed), instead=_bars()).gate().result()
    finding = _missing(result)
    assert finding.data["why"] == "absent" and finding.data["frame"] == removed[0] and finding.data["frames"] == len(removed)
    assert "its source is not on its quad" in finding.message


# ── A noisy wall a zoom blurs strongly ────────────────────────────────────

STRONG = _two("zoom", 0.8)


def test_a_zoom_that_blurs_a_noisy_wall_strongly_with_the_source_removed_is_missing():
    render_, gate, result = _present(STRONG, 0.0, False)
    assert [finding.message for finding, _ in result.findings] == []
    removed = _judged(render_, gate, "zoom in")
    # The first frame judged is blurred by 8 CSS px of the stage and more: the wall's grain gone with it.
    p = (removed[0] / FPS - 1.7) / 0.8
    assert transition_layers("zoom", p)[1].blur >= 8.0
    result = PresenceRender(_document(STRONG), wall=booth, removed=set(removed)).gate().result()
    finding = _missing(result)
    assert finding.data["why"] == "flat" and finding.data["frame"] == removed[0] and finding.data["frames"] == len(removed)
    assert "inside the zoom in: flat against the frame around it" in finding.message
