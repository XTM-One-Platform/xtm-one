"""A figure is judged as it is drawn in motion, and its drift is capped to the frame.

A figure scene with ``motion: drift`` and ``camera: follow``, stood ``perspective: tilt``, built correctly, then the
drift and the tilt added up on its last seconds and its left-most node went past the frame's edge, while the save
measured the figure flat and still and said every text fit. What is proven, in plain geometry (``figure_fit``) and,
where it is installed, against Chromium's own projection of the compositor's transform:

* the stand the fit judges is the one the compositor draws: one table (``figure_fit.STANDS``), read by both;
* a 12-node architecture at ``motion: drift``, ``perspective: tilt``, 24 s keeps every node and both legend lines
  inside the frame - 3% clear on every side - at every pose of its drift and at its last frame, and the save says
  the drift it plays;
* a figure the drift would push within 3% of an edge has its drift capped, the save saying by how much; one whose
  cap is too small to see holds still once built (``motion: none`` for its drawing), said too;
* the page is planned with the drift fitted, its key moving with what the drift is fitted to;
* a figure whose drawn box still crosses the frame is a quality finding.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import compositor_page, figure_fit, figures, quality
from app.services.video_studio.compositor_page import caption_reserve

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"
FLOW = {"type": "process_flow", "steps": [{"title": "Collect"}, {"title": "Enrich"}, {"title": "Act"}]}
FRAME = (1920, 1080)
SAFE = caption_reserve(1080, style="pill", position="bottom")
EASE = figure_fit.move_ease(None)


def _rect(x: float, y: float, w: float, h: float) -> dict:
    return {"x": x, "y": y, "w": w, "h": h}


def _union(rects: list[dict]) -> dict:
    x0, y0 = min(r["x"] for r in rects), min(r["y"] for r in rects)
    x1, y1 = max(r["x"] + r["w"] for r in rects), max(r["y"] + r["h"] for r in rects)
    return _rect(x0, y0, x1 - x0, y1 - y0)


#: Twelve nodes in a row - OpenAEV the left-most - in one zone, a two-line legend under them, as the page describes.
NODES = [_rect(70 + 150 * i, 420, 130, 110) for i in range(12)]
LEGEND = [_rect(720, 640, 480, 22), _rect(720, 676, 480, 22)]
ZONE = _rect(50, 380, 1820, 180)
NAMES = ["OpenAEV", "OpenCTI", "XTM One", "SIEM", "EDR", "SOAR", "Ticketing", "Email", "Cloud", "Identity", "Vault", "Lake"]


def _architecture() -> dict:
    steps = [{"labels": ["Platform"], "box": ZONE, "settle": 0.6}]
    steps += [{"labels": [name], "box": node, "settle": 0.6} for name, node in zip(NAMES, NODES, strict=True)]
    steps[-1]["box"] = _union([NODES[-1], *LEGEND])
    return {"steps": steps, "figure": _union([ZONE, *NODES, *LEGEND])}


def _scene(**fields) -> SimpleNamespace:
    base = {
        "id": "arch",
        "build": "sequence",
        "camera": "follow",
        "reveal": "even",
        "dwell": 1.0,
        "motion": "drift",
        "perspective": "tilt",
        "narration": None,
    }
    return SimpleNamespace(**{**base, **fields})


def _times(described: dict, length: float) -> list[float]:
    labels = [step["labels"] for step in described["steps"]]
    return figures.reveal_times(labels, length=length, settle=[0.6] * len(labels))


def _fitted(described: dict, scene: SimpleNamespace, length: float = 24.0, safe: dict = SAFE) -> figure_fit.FigureFit:
    fit = figures.plan_fit(
        described, _times(described, length), scene=scene, kind="architecture", length=length, safe=safe, viewport=FRAME, ease=EASE
    )
    assert fit is not None
    return fit


def _rect_of(value: dict) -> figure_fit.Rect:
    return (value["x"], value["y"], value["w"], value["h"])


def _poses(fit: figure_fit.FigureFit, start: float, length: float):
    """Every pose of the drift (and its last frame): ``(t, zoom, stand)``."""
    for k in range(97):
        t = start + (length - start) * k / 96
        yield t, figure_fit.drift_zoom(t, start, length, fit.drift), figure_fit.stand_at(fit.perspective, EASE, t, length)


def test_a_12_node_tilted_figure_keeps_every_node_and_its_legend_inside_the_frame():
    described = _architecture()
    scene = _scene()
    fit = _fitted(described, scene)
    assert fit.motion == "drift" and not fit.still and fit.margin >= figure_fit.FIT_MARGIN
    steps = [_rect_of(step["box"]) for step in described["steps"]]
    follows = figure_fit.camera_follows("follow", steps, 24.0, figures.WHOLE_HOLD)
    start = figure_fit.drift_start(_times(described, 24.0), [0.6] * len(steps), 24.0, follows=follows, hold=figures.WHOLE_HOLD)
    # The camera follows the steps, then drifts on the whole figure over the last seconds.
    assert follows and start == pytest.approx(22.0)
    figure = _rect_of(described["figure"])
    for t, zoom, stand in _poses(fit, start, 24.0):
        for name, part in [*zip(NAMES, NODES, strict=True), ("legend line 1", LEGEND[0]), ("legend line 2", LEGEND[1])]:
            box = figure_fit.drawn_box(figure, FRAME, SAFE, zoom, stand, part=_rect_of(part))
            clear, side = figure_fit.clearance(box, FRAME)
            assert clear >= figure_fit.FIT_MARGIN, f"{name} {clear:.2%} from the {side} edge at {t:.2f} s"
    # The page draws it with that drift.
    plan = figures._plan_with(_times(described, 24.0), scene=scene, length=24.0, safe=SAFE, fit=fit)
    assert (plan["motion"], plan["drift"]) == ("drift", fit.drift)


def test_the_save_reports_the_drift_each_figure_plays(monkeypatch):
    described = _architecture()
    monkeypatch.setattr(figures, "read_steps", lambda _digest: asyncio.sleep(0, result=described))
    source = figures.FigureSource("architecture", {}, None, frozenset())
    still = figures.FigurePreview("arch", source, {}, FRAME, "digest", SAFE, _scene(), EASE)
    [line] = asyncio.run(figures.fit_lines([still], {"arch": figures.FigureTiming(24.0)}))
    fit = _fitted(described, _scene())
    assert line == fit.line("arch")
    assert line.startswith(f"Scene arch: its figure drifts in {fit.drift:.1%}, stood tilt, inside the frame at every pose")
    assert f"{fit.margin:.1%} inside the frame" in line


def _tall() -> dict:
    """A figure taller than wide, framed on the room the subtitles leave: the drift pushes its top edge out first."""
    box = _rect(560, 40, 800, 1000)
    return {"steps": [{"labels": ["Whole"], "box": box, "settle": 0.6}], "figure": box}


def test_a_drift_that_would_push_a_figure_to_the_edge_is_capped_and_said():
    # Facing the camera over a deep subtitle band, the room is short: a full drift leaves its top edge under 3%.
    band = {"top": 0.0, "bottom": 420.0}
    described = _tall()
    scene = _scene(perspective="flat", camera="still")
    figure = _rect_of(described["figure"])
    full = figure_fit._least(figure, FRAME, band, perspective="flat", eased=EASE, length=24.0, start=0.95, amount=0.06)
    assert full[0] < figure_fit.FIT_MARGIN and full[1] == "top"
    fit = _fitted(described, scene, safe=band)
    assert fit.capped and figure_fit.MIN_DRIFT <= fit.drift < figure_fit.DRIFT_ZOOM
    assert fit.margin >= figure_fit.FIT_MARGIN
    assert fit.line("arch") == (
        f"Scene arch: its figure's drift is capped at {fit.drift:.1%} (of 6%) so it stays 3% inside the frame on every "
        f"side, facing the camera: its top edge {fit.margin:.1%} inside the frame at 24.0 s, as it is drawn. "
        f"Its figure spans {fit.spans[0]:.0%} to {fit.spans[1]:.0%} of the frame's height, centred between 0% and 61%."
    )
    plan = figures._plan_with([0.35], scene=scene, length=24.0, safe=band, fit=fit)
    assert (plan["motion"], plan["drift"]) == ("drift", fit.drift)


def test_a_cap_too_small_to_see_holds_the_figure_still_and_says_so():
    # Shorter still: the drift that keeps 3% is under 1.5%, too little to see.
    band = {"top": 0.0, "bottom": 590.0}
    described = _tall()
    scene = _scene(perspective="flat", camera="still")
    fit = _fitted(described, scene, safe=band)
    assert fit.still and fit.drift == 0.0 and fit.motion == "none"
    assert "holds still once built (drawn with motion none)" in fit.line("arch")
    plan = figures._plan_with([0.35], scene=scene, length=24.0, safe=band, fit=fit)
    assert (plan["motion"], plan["drift"]) == ("none", 0.0)


def test_a_figure_whose_drawn_box_leaves_the_frame_is_a_quality_finding():
    from app.services.video_studio import timeline
    from app.services.video_studio.spec import parse_video_spec, validate_video_spec

    authored = validate_video_spec(
        {
            "title": "Platform",
            "freestyle": True,
            "scenes": [{"id": "arch", "type": "figure", "block_spec": FLOW, "perspective": "orbit", "duration": 24}],
        }
    )
    spec = parse_video_spec(authored)
    document = timeline.estimate_plan(spec, {}, {}).document
    fit = figure_fit.FigureFit(0.0, 0.06, -0.012, "left", 23.5, perspective="orbit", still=True)
    assert fit.leaves
    found = quality.content_findings(spec, document, quality.ContentFacts(figures={"arch": fit}), authored)
    [finding] = [f for f in found if f.kind == "outside"]
    assert finding.severity == "overloaded" and finding.scene_id == "arch"
    assert "runs 1.2% of the frame past its left edge at 23.5 s - set perspective: flat" in finding.message
    # Inside the frame, nothing is said.
    inside = figure_fit.FigureFit(0.06, 0.06, 0.041, "top", 24.0, perspective="orbit")
    assert not [f for f in quality.content_findings(spec, document, quality.ContentFacts(figures={"arch": inside}), authored) if f.kind == "outside"]


def test_an_unchanged_figure_is_fitted_once_and_what_its_fit_reads_changing_fits_it_again(monkeypatch):
    """The quality check runs on every save and every get_video - an unchanged figure reads its steps from the
    object store once; its perspective, its drift or its narration's length changing judges it again."""
    import dataclasses

    described = _architecture()
    reads: list[str] = []

    async def read_steps(digest):
        reads.append(digest)
        return described

    monkeypatch.setattr(figures, "read_steps", read_steps)
    source = figures.FigureSource("architecture", {}, None, frozenset())
    still = figures.FigurePreview("arch", source, {}, FRAME, "digest", SAFE, _scene(), EASE)
    timing = figures.FigureTiming(24.0)
    first = asyncio.run(figures.figure_fits([still], {"arch": timing}))
    assert asyncio.run(figures.figure_fits([still], {"arch": timing})) == first and reads == ["digest"]
    changes = (
        (dataclasses.replace(still, scene=_scene(perspective="flat")), timing),
        (dataclasses.replace(still, scene=_scene(motion="none")), timing),
        (still, figures.FigureTiming(24.0, 2.0, 18.0)),
    )
    for changed, timed in changes:
        asyncio.run(figures.figure_fits([changed], {"arch": timed}))
    assert len(reads) == 4


def test_what_a_figure_is_drawn_from_moves_with_what_its_drift_is_fitted_to():
    source = figures.FigureSource("architecture", {"blocks": []}, None, frozenset())
    document = {"presentation": {}}
    options = {"length": 24.0, "words": None, "safe": SAFE, "viewport": FRAME, "scale": 1, "fps": 30}
    tilted = figures.render_key(source, document, scene=_scene(), **options)
    assert figures.render_key(source, document, scene=_scene(perspective="orbit"), **options) != tilted
    assert figures.render_key(source, document, scene=_scene(), ease="in_out_expo", **options) != tilted
    still = figures.FigurePreview("arch", source, {}, FRAME, "digest", SAFE, _scene(), EASE)
    timing = figures.FigureTiming(24.0)
    assert figures.build_digest(still, timing) != figures.build_digest(
        figures.FigurePreview("arch", source, {}, FRAME, "digest", SAFE, _scene(perspective="flat"), EASE), timing
    )


# ── One stand for the compositor and the fit ─────────────────────────────


def _constant(source: str, name: str) -> float:
    found = re.search(rf"(?:export )?const {name} = ([0-9.]+);", source)
    assert found, name
    return float(found.group(1))


def test_the_fit_reads_the_figure_page_s_own_numbers_and_the_compositor_reads_the_fit_s_stands():
    build = (FRONTEND / "figure-build.ts").read_text(encoding="utf-8")
    assert _constant(build, "FRAME_MARGIN") == figure_fit.FRAME_MARGIN
    assert _constant(build, "DRIFT_ZOOM") == figure_fit.DRIFT_ZOOM
    assert _constant(build, "CAMERA_MOVE") == figure_fit.CAMERA_MOVE
    assert _constant(build, "STACKED_STEPS") == figure_fit.STACKED_STEPS
    assert _constant(build, "WHOLE_HOLD") == figures.WHOLE_HOLD
    # The page drifts by what the plan says.
    assert "amount: plan.drift ?? DRIFT_ZOOM" in build
    js = compositor_page.PAGE_JS
    assert "__FIGURE_STANDS__" not in js and "const FIGURE_STANDS = {" in js
    assert "rotateX(${lerp(stood.from[0], stood.to[0], e)}deg)" in js
    # No pose of its own: the numbers of a stand live in ``figure_fit.STANDS`` alone.
    assert "rotateY(${lerp(-18" not in js and "scale(0.93)" not in js


@pytest.mark.chromium
def test_the_fit_projects_a_stood_figure_where_chromium_draws_it():
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:
                if os.environ.get("REQUIRE_CHROMIUM") == "1":
                    raise
                pytest.skip(f"Playwright's Chromium is missing: {str(exc)[:160]}")
            try:
                page = await browser.new_page(viewport={"width": FRAME[0], "height": FRAME[1]})
                distance = round(max(FRAME) * figure_fit.PERSPECTIVE)
                await page.set_content(
                    "<html><body style='margin:0'><div style='position:absolute;inset:0;perspective:"
                    f"{distance}px'><div id='box' style='position:absolute;left:0;top:0;width:{FRAME[0]}px;height:"
                    f"{FRAME[1]}px;transform-origin:50% 50%'><div id='part' style='position:absolute;left:100px;top:300px;"
                    "width:1700px;height:400px'></div></div></div></body></html>"
                )
                drawn = {}
                for name, stand in figure_fit.STANDS.items():
                    for e in (0.0, 1.0):
                        rx = stand["from"][0] + (stand["to"][0] - stand["from"][0]) * e
                        ry = stand["from"][1] + (stand["to"][1] - stand["from"][1]) * e
                        await page.evaluate(
                            "([s, x, y]) => { document.getElementById('box').style.transform = "
                            "`scale(${s}) rotateX(${x}deg) rotateY(${y}deg)`; }",
                            [stand["scale"], rx, ry],
                        )
                        r = await page.evaluate("() => { const r = document.getElementById('part').getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; }")
                        drawn[(name, e)] = (r, (rx, ry, stand["scale"]))
                return drawn
            finally:
                await browser.close()

    for (_name, _e), (chromium, pose) in asyncio.run(run()).items():
        corners = [figure_fit.project((x, y), pose, FRAME) for x in (100, 1800) for y in (300, 700)]
        mine = (min(c[0] for c in corners), min(c[1] for c in corners), max(c[0] for c in corners), max(c[1] for c in corners))
        assert mine == pytest.approx(tuple(chromium), abs=0.6)
