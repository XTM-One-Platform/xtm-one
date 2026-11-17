"""The presence gate reads a crossfade's frames unblended, never against the raw source.

An insert that runs to its scene's end is painted through its scene's transition out, and its presence is
read on the encoded frames. On a crossfade those frames blend it with the incoming scene, and the gate compared them
with the raw source: at 8.13 s of scene s03-stat it read SSIM 0.30 under 0.3 and blocked the brand film as "insert
missing", on every preview, and again from the s04-filigran side (11 of 11 join frames) - with "render again" as the
fix, which changed nothing. What is proven, on the synthetic joins fixture (``test_video_studio_insert_tail``): each
scene composed as the render composes it, the scenes blended as the page blends them, a lossy encode:

* read raw, as before, the crossfade out into a figure scene fails the insert on its join (the bug);
* read unblended - the frame less its scene painted black, over how much of its scene the frame shows (the white
  painting less the black one), as the frame server keeps them - every frame of the join that shows 65% of the insert
  or more is present, and the frames under it are said as a fact (a crossfade into a statement whose bright
  text crosses the quad warned on its last frames, alpha 0.53 to 0.60, and promised to block the next render);
* the insert really removed on the join (a black screen) still fails there, the finding naming its alpha and the edit
  that keeps the insert off the crossfade;
* the frame server keeps the black and white paintings of exactly the presence frames that blend an insert, and the
  page draws them.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import compositor, compositor_page, insert_checks, render_checks
from app.services.video_studio.render_checks import FAIL, INSERT_MISSING
from app.services.video_studio.screen_insert import Footage, InsertSource
from tests.test_video_studio_insert_tail import _painted, _Render
from tests.video_studio_insert_fixture import FPS, QUAD, H, W, clip_document, key_of, weights

#: The insert's scene, its insert from 0 to its end, then a 0.7 s crossfade into a figure scene with no insert.
JOIN = [("a", 0.0, 2.2, 2.2, "cut", 0.0), ("b", 1.5, 3.7, 2.2, "crossfade", 0.7)]


def _figure() -> np.ndarray:
    """A figure scene's frame: a dense diagram of thin bright strokes and labels on a dark ground - finer and stronger
    structure than the insert's lines of text, across them."""
    picture = Image.new("RGB", (W, H), (10, 12, 22))
    draw = ImageDraw.Draw(picture)
    for x in range(0, W, 5):
        draw.line((x, 0, x, H), fill=(250, 250, 255) if (x // 5) % 2 else (0, 210, 240), width=2)
    for k, y in enumerate(range(8, H, 23)):
        for x in range(6 + (k % 3) * 7, W - 20, 41):
            draw.rectangle((x, y, x + 18, y + 3), fill=(10, 12, 22))
    return np.asarray(picture)


def _stat_screen() -> Image.Image:
    """The insert: a stat screen - soft grey lines of text on a dark ground, less contrast than the figure after it."""
    picture = Image.new("RGB", (600, 450), (40, 46, 60))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((40, 40, 330, 150), fill=(104, 110, 124))
    for row in range(190, 410, 30):
        draw.rectangle((40, row, 360, row + 10), fill=(96, 100, 112))
    return picture


def _render(render_class: type[_Render] = _Render) -> tuple[dict, _Render]:
    document = clip_document(JOIN)
    document["scenes"][1]["inserts"] = []
    render_ = render_class(document)
    render_.footage["b"] = _figure()
    render_.pictures[key_of("a")] = render_.sources[key_of("a")] = _stat_screen()
    return document, render_


def _join(document: dict) -> list[int]:
    return [n for n in _painted(document, 0) if n / FPS >= document["scenes"][1]["start"]]


def test_read_raw_the_crossfade_into_a_figure_fails_the_insert_on_its_join(monkeypatch):
    document, render_ = _render()
    painted = _painted(document, 0)
    # No unblending kept: every frame showing half the insert or more read against its raw source.
    monkeypatch.setattr(insert_checks, "BLEND_RAW_SHOWN", 0.0)
    result = render_.gate(range(painted[0], painted[-1] + 1), blends=False).result()
    hits = [finding for finding, _proof in result.findings if finding.data["insert"] == key_of("a")]
    assert hits and hits[0].kind == INSERT_MISSING and hits[0].data["transition"]
    assert hits[0].data["frame"] in _join(document)


def test_read_unblended_every_frame_of_the_crossfade_out_that_shows_the_insert_is_present():
    document, render_ = _render()
    painted = _painted(document, 0)
    join = _join(document)
    assert len(join) >= 15, "a 0.7 s crossfade at 24 fps"
    gate = render_.gate(range(painted[0], painted[-1] + 1))
    result = gate.result()
    assert [finding.message for finding, _proof in result.findings] == []
    key = key_of("a")
    shows = [n for n in join if render_.audits[key]["placed"][n][4] >= insert_checks.PRESENCE_MIN_SHOWN]
    faint = [n for n in join if n not in shows]
    assert shows and faint
    # Every frame of the join that shows half of it or more read, unblended, and present; the others said as a fact.
    assert set(shows) <= set(gate.read_frames[key])
    assert set(gate.alphas[key]) == {n for n in shows if render_.audits[key]["placed"][n][4] < insert_checks.BLEND_WHOLE}
    summary = result.by_key[key]
    assert summary["missing"] == 0 and summary["joins_missing"] == 0 and summary["faint"] == len(faint)
    assert insert_checks.PRESENCE_MIN_SHOWN <= summary["least_alpha"] < 0.75
    report = next(r for r in insert_checks.reports(render_.audits, gate.placed, fps=FPS, presence=result.by_key) if r["key"] == key)
    lines = [m.text for m in insert_checks.classify(report)]
    # Its source seen on the join is its presence line's alone: the joins line says what its layer measured.
    joins = [line for line in lines if line.startswith("on its scene's joins with the scenes beside it: ")]
    assert joins and not any("its source" in line for line in joins)
    said = next(line for line in lines if line.startswith("its source present on all"))
    assert "blended with the scene beside it, read unblended (the insert at alpha" in said
    assert f"{len(faint)} frame(s) of a transition show under 65% of it, not judged" in said


class _Removed(_Render):
    """The black-screen variant: the insert really removed from its quad once its scene's join begins."""

    removed_from = 0

    def _scene(self, scene: dict, frame: int, shown: float) -> np.ndarray:
        kept = self.pictures
        if frame >= self.removed_from:
            self.pictures = {**kept, key_of("a"): Image.new("RGB", (600, 450), (0, 0, 0))}
        try:
            return super()._scene(scene, frame, shown)
        finally:
            self.pictures = kept


def test_an_insert_really_removed_on_the_join_still_fails_and_its_finding_names_its_alpha():
    document = clip_document(JOIN)
    # Removed from the join's third frame: the crossfade has begun, the insert at about 0.88 there.
    _Removed.removed_from = _join(document)[2]
    document, render_ = _render(_Removed)
    painted = _painted(document, 0)
    result = render_.gate(range(painted[0], painted[-1] + 1)).result()
    (finding, _proof), *_rest = [hit for hit in result.findings if hit[0].data["insert"] == key_of("a")]
    assert finding.kind == INSERT_MISSING and finding.data["gate"] == "presence" and finding.data["transition"]
    assert finding.data["frame"] == _join(document)[2] and finding.data["why"] in ("flat", "absent")
    alpha = finding.data["alpha"]
    assert insert_checks.PRESENCE_MIN_SHOWN <= alpha <= 1.0
    assert f"(the insert at alpha {alpha:.2f} there, blended with the scene beside it)" in finding.message
    # Its frames all lie in the crossfade out: the fix names the edit that keeps the insert off them, never
    # "render again", and it blocks as any finding the spec causes - never the harness's.
    assert finding.data["action"]["kind"] == insert_checks.AVOID_TRANSITION
    assert finding.message.endswith(
        "its frames are the crossfade out of scene a into scene b (0:01.50 to 0:02.21): make scene b's transition a "
        "cut, or end the insert before it (a duration under 1.50 s)"
    )
    assert "render again" not in finding.message
    assert render_checks.harness_defects([finding], {}) == ([finding], []) and finding.severity == FAIL


def _statement() -> np.ndarray:
    """A statement scene's frame: a dark ground and two lines of large bright letters across where the insert's quad
    sits - what outweighs the insert on a crossfade's last frames (s03-stat into s04)."""
    picture = Image.new("RGB", (W, H), (12, 14, 24))
    draw = ImageDraw.Draw(picture)
    for top in (84, 140):
        for x in range(70, W - 70, 22):
            draw.rectangle((x, top, x + 15, top + 36), fill=(244, 246, 252))
            draw.rectangle((x + 4, top + 8, x + 11, top + 28), fill=(12, 14, 24))
    return np.asarray(picture)


class _Ringing(_Render):
    """The encode of a crossfade into the statement as a codec draws it: ringing along the statement's bright edges, as
    strong as the statement shows - what the black and white paintings (lossless) do not carry, so unblending leaves it
    over the quad, divided by the insert's alpha there."""

    def frame(self, frame: int) -> np.ndarray:
        from scipy import ndimage

        layers = self.layers(frame)
        out = self._stack(frame, layers)
        painted = self._painted_scenes(frame)
        if len(painted) == 2:
            coming = weights(painted[1], frame / FPS)[1]
            statement = layers[painted[1]["id"]]
            out = out + 3.0 * (statement - ndimage.gaussian_filter(statement, (1.2, 1.2, 0))) * coming
        buffer = io.BytesIO()
        Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(buffer, format="JPEG", quality=90)
        return np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))


def _statement_gate() -> tuple[dict, _Render, insert_checks.PresenceGate]:
    document, render_ = _render(_Ringing)
    render_.footage["b"] = _statement()
    painted = _painted(document, 0)
    return document, render_, render_.gate(range(painted[0], painted[-1] + 1))


def test_a_crossfade_into_a_statement_whose_text_crosses_the_quad_judges_no_faint_frame_and_warns_of_none():
    document, render_, gate = _statement_gate()
    join = _join(document)
    key = key_of("a")
    result = gate.result()
    # No finding: nothing to warn of, nothing for the harness to say.
    assert [finding.message for finding, _proof in result.findings] == []
    summary = result.by_key[key]
    assert summary["missing"] == 0 and summary["joins_missing"] == 0
    # The frames under alpha 0.65 are counted faint, the others read unblended and present.
    faint = [n for n in join if render_.audits[key]["placed"][n][4] < insert_checks.PRESENCE_MIN_SHOWN]
    assert faint and summary["faint"] == len(faint)
    assert set(join) - set(faint) <= set(gate.read_frames[key])
    assert min(gate.alphas[key].values()) >= insert_checks.PRESENCE_MIN_SHOWN


def test_judged_from_half_shown_the_statement_s_last_frames_read_as_missing(monkeypatch):
    """The report: judged from 50% shown, the last frames of the crossfade - alpha 0.52 to 0.58, the statement's
    edges over the quad - read "absent" and warned, though nothing in the spec draws them otherwise."""
    monkeypatch.setattr(insert_checks, "PRESENCE_MIN_SHOWN", 0.5)
    document, _, gate = _statement_gate()
    missed = sorted(hit[0] for hit in gate.missing[key_of("a")])
    assert missed and set(missed) <= set(_join(document))
    assert all(0.5 <= gate.alphas[key_of("a")][n] < 0.65 for n in missed)


def test_on_the_statement_the_insert_really_removed_on_the_join_still_fails():
    document = clip_document(JOIN)
    _Removed.removed_from = _join(document)[2]
    document, render_ = _render(_Removed)
    render_.footage["b"] = _statement()
    painted = _painted(document, 0)
    result = render_.gate(range(painted[0], painted[-1] + 1)).result()
    (finding, _proof), *_rest = [hit for hit in result.findings if hit[0].data["insert"] == key_of("a")]
    assert finding.kind == INSERT_MISSING and finding.data["transition"] and finding.severity == FAIL
    assert finding.data["action"]["kind"] == insert_checks.AVOID_TRANSITION
    assert render_checks.harness_defects([finding], {}) == ([finding], [])


async def test_the_frame_server_keeps_the_scene_painted_black_and_white_on_the_frames_that_blend_an_insert(tmp_path):
    path = tmp_path / "ui.png"
    Image.new("RGB", (60, 40), (40, 46, 60)).save(path)
    insert = InsertSource(str(path), "image", tuple(tuple(p) for p in QUAD), footage=Footage(str(path), "image", key="backdrop/a"))
    server = compositor.FrameServer(
        compositor.CompositorAssets(inserts={"insert/a.0": insert}),
        "<html></html>",
        evidence_dir=tmp_path / "evidence",
        presence={10: ["insert/a.0"], 11: ["insert/a.0"], 12: ["insert/a.0"]},
    )
    rect = lambda shown: {"insert/a.0": [10, 10, 100, 60, shown]}  # noqa: E731
    assert server.blend_scenes(10, rect(0.7)) == ["a"], "a crossfade's frame showing most of it"
    assert server.blend_scenes(11, rect(1.0)) == [], "its scene alone: nothing to unblend"
    assert server.blend_scenes(12, rect(0.3)) == [], "too little of it shown to judge"
    assert server.blend_scenes(13, rect(0.7)) == [], "not a frame the gate reads"
    shot = Image.new("RGB", (3840, 2160), (255, 255, 255))
    for tone in ("black", "white"):
        import io

        buffer = io.BytesIO()
        shot.save(buffer, format="PNG")
        await server.keep_blend(10, "a", tone, buffer.getvalue())
    taken = server.take_audits(0, 20)["insert/a.0"]["blend"]
    assert set(taken) == {"10"} and set(taken["10"]) == {"black", "white"}
    with Image.open(tmp_path / "evidence" / taken["10"]["black"]) as kept:
        assert kept.width == insert_checks.PRESENCE_WIDTH, "kept at the width the gate reads the encoded frame at"
    # The page paints a scene black or white where its layer is, and the next frame drawn puts it back.
    script = compositor_page.page_html("")
    assert "async tone(index, id, white) {" in script and "brightness(0)${white ? ' invert(1)' : ''}" in script


def _crossfade() -> dict:
    from app.services.video_studio import timeline
    from app.services.video_studio.spec import parse_video_spec
    from app.services.video_studio.themes import build_theme

    spec = parse_video_spec(
        {
            "title": "Unblend",
            "freestyle": True,
            "theme": {"watermark": "none"},
            "default_transition": {"type": "crossfade", "duration": 1.0},
            "scenes": [
                {"id": "stat", "type": "statement", "lines": ["Six thousand *teams*."]},
                {"id": "figure", "type": "statement", "lines": ["One platform, every signal."], "align": "left"},
            ],
        }
    )
    theme = build_theme(mode="dark", background_style="grid", has_logo=False, has_mark=False)
    return timeline.plan(spec, timeline.PlanMedia(), fps=30, theme=theme).document


@pytest.mark.chromium
async def test_in_a_real_chromium_the_black_and_white_paintings_unblend_a_crossfade_to_the_scene_it_leaves():
    """The page's ``tone()`` on a crossfade's frame: the frame less the black painting, over the white less the black,
    is the scene it leaves drawn alone - what the presence gate reads an insert on."""
    import io
    import os

    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    document = _crossfade()
    second = document["scenes"][1]
    frame = round((second["start"] + 0.4) * document["fps"])
    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    def shot(data: bytes) -> np.ndarray:
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(viewport={"width": int(document["width"]), "height": int(document["height"])})
                await context.route("**/*", handle)
                page = await context.new_page()
                await page.goto(index, wait_until="load")
                await page.evaluate("doc => window.__xtm.load(doc)", {**document, "preview_media": {}})
                await page.evaluate("i => window.__xtm.render(i)", frame)
                seen = shot(await page.screenshot(type="png"))
                toned = {}
                for white in (False, True):
                    assert await page.evaluate("([i, id, w]) => window.__xtm.tone(i, id, w)", [frame, "stat", white])
                    toned[white] = shot(await page.screenshot(type="png"))
                await page.evaluate("([i, id]) => window.__xtm.alone(i, id)", [frame, "stat"])
                alone = shot(await page.screenshot(type="png"))
                # The next frame drawn puts the scene back as the transition draws it.
                await page.evaluate("i => window.__xtm.render(i)", frame)
                again = shot(await page.screenshot(type="png"))
            finally:
                await browser.close()
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    assert np.abs(again - seen).mean() < 0.5
    black, white = toned[False], toned[True]
    weight = np.clip((white - black).mean(axis=2) / 255.0, 0.0, 1.0)
    # A crossfade's frame: the scene it leaves shows at one alpha everywhere, between the two ends.
    assert 0.2 < float(weight.mean()) < 0.98 and float(weight.std()) < 0.02
    unblended = (seen - black) / np.maximum(weight, 1 / 255)[..., None]
    shows = weight >= insert_checks.PRESENCE_MIN_SHOWN
    assert np.abs(unblended - alone)[shows].mean() < 3.0, "the frame unblended is the scene it leaves, drawn alone"
    assert np.abs(seen - alone)[shows].mean() > 3 * np.abs(unblended - alone)[shows].mean(), "the blend is not the scene"
