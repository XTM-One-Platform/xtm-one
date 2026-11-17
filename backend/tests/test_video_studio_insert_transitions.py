"""A keyed screen insert at its scene's joins with the scenes beside it: never the key, through a transition or a cut.

Synthetic fixtures only - no provider, no browser, no database. Each frame of a join is composed as the render composes
it: each scene's keyed insert drawn onto that scene's own footage frame by ``screen_insert.compose`` - its layer whole
wherever the page draws it (``timeline.insert_layers``), how much of the insert shows composed over the colour of the
surface with nothing on it -, then the two finished frames blended by the transition. The frames are read by the
render's gates: the frames' audits (``insert_checks.findings``) and the guard on the frames as composited
(``insert_checks.judge_dropped``):

* F23 two insert scenes joined by a crossfade on a green panel: no key pixel in any frame of the join;
* F24 the same with a cut;
* F25 an insert starting 0.3 s after its scene: the panel flat in the bezel's colour before it, never green;
* F26 F23 with each insert layer blended by the transition's alpha on its own: the guard fails, naming the frame.

And the pieces: a keyed surface held over its scene and handed over between successive inserts on it, the frames the
page draws a layer on and a frame missing among them, a residual key on one frame of a join blocking, the frame server's
layer cache keyed by everything a layer is drawn by, and a neighbour's blocking finding kept in a scene preview.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import (
    compositor,
    compositor_page,
    insert_checks,
    insert_frames,
    render_answer,
    render_checks,
    screen_insert,
    timeline,
)
from app.services.video_studio.render_checks import (
    FAIL,
    INSERT_DROPPED,
    INSERT_FLICKER,
    INSERT_RESIDUAL_KEY,
    WARN,
    Checks,
    Finding,
)
from app.services.video_studio.screen_insert import Footage, InsertSource
from app.services.video_studio.spec import INSERT_FADE_SECONDS
from tests.video_studio_insert_fixture import FPS, QUAD, SCREEN, H, W, booth, source_of, text_ui

KEYS = {"a": "insert/a.0", "b": "insert/b.0"}


def _payload(scene_id: str, t0: float, t1: float) -> dict:
    source = source_of(scene_id)
    return {
        "src": KEYS[scene_id],
        "quad": QUAD,
        "box": [list(p) for p in source.box],
        "t0": t0,
        "t1": t1,
        "keyed": True,
        "matte": {"fps": FPS, "frames": 200},
    }


def _document(scenes: list[tuple]) -> dict:
    """A planned document of clip scenes, each ``(id, start, end, transition in, its seconds, its insert's t0[, t1])``:
    its insert to the scene's end unless *t1* says, its keyed surfaces held as the planner holds them
    (``timeline.hold_surfaces``)."""
    planned = []
    for scene_id, start, end, kind, seconds, t0, *until in scenes:
        inserts = [_payload(scene_id, t0, until[0] if until else round(end - start, 4))]
        timeline.hold_surfaces(inserts, end - start)
        planned.append(
            {
                "id": scene_id,
                "type": "clip",
                "start": start,
                "end": end,
                "transition_in": {"type": kind, "duration": seconds},
                "inserts": inserts,
            }
        )
    duration = planned[-1]["end"]
    return {"fps": FPS, "frames": math.ceil(duration * FPS), "duration": duration, "fade_in": 0.0, "fade_out": 0.0, "scenes": planned}


class _Render:
    """The render's compositing of a planned *document*, frame by frame, as the page and the frame server draw it - each
    scene's frame drawn alone kept in *keep* for the guard, as the page's ``alone()`` draws it."""

    def __init__(self, document: dict, grounds: dict[str, tuple[int, int, int]], keep: Path | None = None) -> None:
        self.document = document
        self.keep = keep
        self.footage = {scene["id"]: booth(seed=7) for scene in document["scenes"]}
        self.pictures = {KEYS[scene_id]: text_ui(ground) for scene_id, ground in grounds.items()}
        self.inserts: dict[str, InsertSource] = {}
        self.fills: dict[str, object] = {}
        self.surfaces: dict[str, object] = {}
        self.states: dict[str, screen_insert.Matting] = {}
        for scene in document["scenes"]:
            key, first = KEYS[scene["id"]], self.footage[scene["id"]]
            keyed = screen_insert.keyed_on(source_of(scene["id"]), first)
            self.inserts[key] = keyed
            self.fills[key] = screen_insert.static_fill(keyed, first, (W, H))
            self.surfaces[key] = screen_insert.Surface.of(first, list(map(tuple, SCREEN)))
            self.states[key] = screen_insert.Matting()
        self.audits: dict[str, dict] = {key: {"frames": {}, "evidence": {}, "placed": {}, "alone": {}} for key in self.inserts}

    def keys(self) -> dict[str, object]:
        return {key: insert.key.key() for key, insert in self.inserts.items()}

    def scene_frame(self, scene: dict, frame: int, *, shown_at: float | None = None, weight: float = 1.0) -> np.ndarray:
        """*scene*'s finished frame at output *frame*: its footage, each insert's layer over it - drawn at the opacity
        the page gives it, or at *shown_at* on its own (the order F26 breaks) -, the frame blending it at *weight*."""
        rgb = self.footage[scene["id"]].astype(np.float32)
        t = max(0.0, frame / FPS - float(scene["start"]))
        drawn = []
        for payload, opacity, shown in timeline.insert_layers(scene, t):
            if opacity <= 0:
                continue
            key = payload["src"]
            drawn.append(key)
            layer = screen_insert.compose(
                self.inserts[key],
                self.pictures[key],
                (W, H),
                frame,
                footage=self.footage[scene["id"]],
                surface=self.surfaces[key],
                fill=self.fills[key],
                state=self.states[key],
                window_frame=frame,
                visibility=1.0 if shown is None else shown,
            )
            self.audits[key]["frames"][frame] = layer.audit.as_dict()
            # Where the page places the layer's box on the frame (0-1, the footage filling it here) and how much shows.
            box = [min(max(float(v), 0.0), 1.0) for corner in (payload["box"][0], payload["box"][2]) for v in corner]
            self.audits[key]["placed"][frame] = [*box, round(weight * (opacity if shown_at is None else shown_at), 3)]
            x0, y0, x1, y1 = layer.warped.box
            image = np.asarray(layer.warped.image, dtype=np.float32)
            alpha = image[..., 3:4] / 255.0 * (opacity if shown_at is None else shown_at)
            rgb[y0:y1, x0:x1] = image[..., :3] * alpha + rgb[y0:y1, x0:x1] * (1.0 - alpha)
        if self.keep is not None and drawn:
            path = self.keep / f"alone-{scene['id']}-{frame:07d}.png"
            Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(path)
            for key in drawn:
                self.audits[key]["alone"][frame] = {"file": path.name, "box": self.audits[key]["placed"][frame][:4], "path": str(path)}
        return rgb

    def frame(self, frame: int, *, alone: bool = False) -> np.ndarray:
        """Output *frame*: each scene on screen finished on its own, then the two blended by the transition - or, *alone*,
        each insert blended by the transition's alpha on its own over its footage."""
        at = frame / FPS
        scenes = self.document["scenes"]
        shown = [s for i, s in enumerate(scenes) if at >= s["start"] and (at < s["end"] or i == len(scenes) - 1)]
        if len(shown) == 1:
            return np.clip(self.scene_frame(shown[0], frame), 0, 255).astype(np.uint8)
        before, after = shown
        tr = after["transition_in"]
        e = min(max((at - after["start"]) / tr["duration"], 0.0), 1.0)
        out = self.scene_frame(before, frame, shown_at=(1.0 - e) if alone else None, weight=1.0 - e) * (1.0 - e)
        out += self.scene_frame(after, frame, shown_at=e if alone else None, weight=e) * e
        return np.clip(out, 0, 255).astype(np.uint8)

    def key_left(self, picture: np.ndarray) -> float:
        return max(screen_insert.key_left(picture, SCREEN, key) for key in self.keys().values())


def _join_frames(document: dict) -> list[int]:
    first, end, _coming = timeline.transition_windows(document)[0]
    return list(range(math.ceil(first * FPS), math.ceil(end * FPS)))


def _gates(render: _Render, frames: dict[int, np.ndarray]) -> tuple[list, list]:
    """The frames' audits read by the render's gates, and the guard on the frames as composited - over the frames drawn,
    as a scene preview reads its window."""
    drawn = (min(frames), max(frames) + 1)
    placed = insert_checks.placed_inserts(render.document, render.inserts)
    found = insert_checks.findings(render.audits, placed, fps=FPS, frame_range=drawn)
    guard = insert_checks.guard_frames(placed, frame_range=drawn)
    assert guard, "every join of an insert scene is read by the guard"
    judged = insert_checks.judge_dropped(
        {n: frames[n] for n in guard if n in frames}, render.audits, placed, guard, render.document, render.keys(), fps=FPS
    )
    return found, judged


# ── F23: two insert scenes joined by a crossfade ──────────────────────────

#: The outgoing insert ends where the join begins - its scene still on screen at full strength through it, as at 96.7 s
#: of render dad61b44 -, the incoming one comes in with its scene.
CROSSFADE = [("a", 0.0, 2.0, "cut", 0.0, 0.0, 1.5), ("b", 1.5, 3.5, "crossfade", 0.5, 0.0)]


def test_f23_a_crossfade_between_two_insert_scenes_shows_no_key_pixel_on_any_frame_of_the_join(tmp_path):
    document = _document(CROSSFADE)
    render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)}, keep=tmp_path)
    join = _join_frames(document)
    frames = {n: render.frame(n) for n in range(join[0] - round(INSERT_FADE_SECONDS * FPS) - 2, join[-1] + 4)}
    # Gone before the join, the outgoing insert's surface stays covered through it: the bezel's colour, never the key.
    assert all(render.audits[KEYS["a"]]["frames"][n]["visibility"] == 0.0 for n in join)
    assert [n for n, picture in frames.items() if render.key_left(picture) > 0.0] == []
    found, judged = _gates(render, frames)
    assert [f.kind for f, _ in found if f.kind in (INSERT_RESIDUAL_KEY, INSERT_FLICKER, INSERT_DROPPED)] == []
    assert judged == []


# ── F24: the same with a cut ──────────────────────────────────────────────

CUT = [("a", 0.0, 2.0, "cut", 0.0, 0.0), ("b", 2.0, 4.0, "cut", 0.0, 0.0)]


def test_f24_a_cut_between_two_insert_scenes_shows_no_key_pixel_either_side(tmp_path):
    document = _document(CUT)
    render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)}, keep=tmp_path)
    join = _join_frames(document)
    assert len(join) == 2 * timeline.CUT_FRAMES
    # The last frames of the outgoing scene - its insert fading out - and the first of the next.
    frames = {n: render.frame(n) for n in range(join[0] - round(INSERT_FADE_SECONDS * FPS), join[-1] + 4)}
    assert [n for n, picture in frames.items() if render.key_left(picture) > 0.0] == []
    found, judged = _gates(render, frames)
    assert [f.kind for f, _ in found if f.kind in (INSERT_RESIDUAL_KEY, INSERT_FLICKER, INSERT_DROPPED)] == []
    assert judged == []


# ── F25: an insert coming in after its scene's start ──────────────────────


def test_f25_the_panel_is_flat_in_the_bezel_s_colour_before_its_insert_comes_in_never_green():
    document = _document([("a", 0.0, 2.0, "cut", 0.0, 0.3)])
    render = _Render(document, {"a": (40, 46, 60)})
    payload = document["scenes"][0]["inserts"][0]
    assert payload["held"] == [0.0, 2.0]
    inside = screen_insert._coverage(screen_insert._grown_px(SCREEN, -6.0), (0, 0, W, H)) > 0.5
    _fill, off = screen_insert.screen_off(render.keys()[KEYS["a"]], render.fills[KEYS["a"]].bezel)
    before = [n for n in range(0, math.floor(0.3 * FPS))]
    for n in range(0, math.ceil((0.3 + INSERT_FADE_SECONDS) * FPS) + 3):
        picture = render.frame(n)
        assert render.key_left(picture) == 0.0, n
        if n in before:
            seen = picture[inside].astype(np.float32)
            # Flat, in the colour of the surface with nothing on it (the footage's light laid over it, a little).
            assert np.abs(seen.mean(axis=0) - np.asarray(off)).max() < 14.0, (n, seen.mean(axis=0), off)
            assert float(seen.std(axis=0).max()) < 8.0, n
            assert render.audits[KEYS["a"]]["frames"][n]["visibility"] == 0.0
    # Once in, it is the insert's own ground, judged as any frame drawn whole.
    picture = render.frame(round(0.8 * FPS))
    assert np.abs(picture[inside].astype(np.float32).mean(axis=0) - np.asarray(off)).max() > 14.0
    whole = render.audits[KEYS["a"]]["frames"][round(0.8 * FPS)]
    assert whole["visibility"] is None and whole["sharpness"] is not None


# ── F26: each insert blended by the transition's alpha on its own ─────────


def test_f26_an_insert_layer_blended_by_the_transition_alone_fails_the_guard_naming_the_frame(tmp_path):
    document = _document(CROSSFADE)
    render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)}, keep=tmp_path)
    join = _join_frames(document)
    frames = {n: render.frame(n, alone=True) for n in join}
    assert max(render.key_left(picture) for picture in frames.values()) > screen_insert.RESIDUAL_FAIL_SHARE
    _found, judged = _gates(render, frames)
    assert judged, "the key shown through the join fails the render"
    finding, proof = judged[0]
    assert finding.kind == INSERT_DROPPED and finding.severity == FAIL and finding.data["transition"]
    assert finding.data["frame"] in join and f"on frame {finding.data['frame']}," in finding.message
    assert finding.message.startswith(f"insert dropped at t={render_checks.clock(finding.data['frame'] / FPS)}: Scene ")
    assert "of its quad on its scene drawn alone" in finding.message
    # The scene's frame drawn alone, as the page drew it, is the frame the finding shows.
    assert proof == render.audits[finding.data["insert"]]["alone"][finding.data["frame"]]["path"]


def test_a_key_hued_picture_of_the_scene_beside_an_insert_never_reads_as_its_key(tmp_path):
    """The guard reads each scene drawn alone: on a frame the two scenes blend, a green of the scene beside it showing
    through the insert's quad is that scene's picture (a test pattern's green bar, render dad61b44's booth), never the
    insert's key. The encoded frame itself is read where its scene shows alone."""
    document = _document(CROSSFADE)
    render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)}, keep=tmp_path)
    join = _join_frames(document)
    frames = {n: render.frame(n).copy() for n in join}
    placed = insert_checks.placed_inserts(document, render.inserts)
    guard = insert_checks.guard_frames(placed)

    def shows_alone(frame: int) -> bool:
        return any(render.audits[key]["placed"][frame][4] >= insert_checks.GUARD_ALONE for key in guard[frame])

    def green_bar(frame: int) -> None:
        x0, y0, _x1, y1 = (int(v) for v in (*SCREEN[0], *SCREEN[2]))
        frames[frame][y0 + 10 : y1 - 10, x0 + 20 : x0 + 60] = (0, 255, 0)

    blended = [n for n in guard if not shows_alone(n)]
    assert blended, "frames the two scenes blend on are read"
    for n in blended:
        green_bar(n)
    assert insert_checks.judge_dropped(frames, render.audits, placed, guard, document, render.keys(), fps=FPS) == []
    # Where a scene shows alone, the encoded frame itself is read: the same green there is a key the insert left.
    alone = next(n for n in guard if shows_alone(n))
    green_bar(alone)
    (finding, _proof), *_rest = insert_checks.judge_dropped(frames, render.audits, placed, guard, document, render.keys(), fps=FPS)
    assert finding.data["frame"] == alone and "of its quad on the encoded frame" in finding.message


def test_the_same_joins_drawn_with_each_insert_faded_on_its_own_show_the_key(monkeypatch):
    """What F23-F25 hold the render to: drawn as the page drew them earlier - each insert faded on its own over its
    footage, left out outside its own span -, the same frames show the key on the join, after the cut and before the
    insert comes in."""
    monkeypatch.setattr(timeline, "insert_layers", lambda scene, at: [(p, timeline.insert_visibility(p, at), None) for p in scene["inserts"]])
    for scenes in (CROSSFADE, CUT):
        document = _document(scenes)
        render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)})
        join = _join_frames(document)
        assert max(render.key_left(render.frame(n)) for n in join) > screen_insert.RESIDUAL_FAIL_SHARE
    render = _Render(_document([("a", 0.0, 2.0, "cut", 0.0, 0.3)]), {"a": (40, 46, 60)})
    assert render.key_left(render.frame(3)) > screen_insert.RESIDUAL_FAIL_SHARE


# ── The pieces ────────────────────────────────────────────────────────────


def _keyed(src: str, quad: list, t0: float, t1: float) -> dict:
    return {"src": src, "quad": quad, "t0": t0, "t1": t1, "keyed": True}


def test_a_keyed_surface_is_held_over_its_scene_and_handed_over_between_the_inserts_on_it():
    left = [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]]
    right = [[0.6, 0.1], [0.9, 0.1], [0.9, 0.4], [0.6, 0.4]]
    alone = [_keyed("insert/s.0", left, 2.0, 5.0)]
    timeline.hold_surfaces(alone, 8.0)
    assert alone[0]["held"] == [0.0, 8.0] and alone[0]["surface"] == 0
    # Two on one surface, one after the other: the first holds it until the next comes in, the last to the end.
    successive = [_keyed("insert/s.0", left, 0.0, 3.0), _keyed("insert/s.1", left, 4.0, 6.0), _keyed("insert/s.2", right, 1.0, 2.0)]
    timeline.hold_surfaces(successive, 8.0)
    assert [p["held"] for p in successive] == [[0.0, 4.0], [4.0, 8.0], [0.0, 8.0]]
    assert [p["surface"] for p in successive] == [0, 0, 2]
    # Overlapping in time: the later one comes in over the earlier, which covers the key beneath it.
    over = [_keyed("insert/s.0", left, 0.0, 5.0), _keyed("insert/s.1", left, 3.0, 8.0)]
    timeline.hold_surfaces(over, 8.0)
    assert [p["held"] for p in over] == [[0.0, 5.0], [3.0, 8.0]]
    scene = {"start": 10.0, "end": 18.0, "inserts": over}
    (_first, opacity_a, shown_a), (second, opacity_b, shown_b) = timeline.insert_layers(scene, 3.1)
    assert (opacity_a, shown_a) == (1.0, 1.0)
    assert shown_b is None and opacity_b == timeline.insert_visibility(second, 3.1)
    assert 0.0 < opacity_b < 1.0
    # Before it comes in, a lone keyed insert is drawn whole, showing nothing of itself; an unkeyed one is not drawn.
    unkeyed = {"src": "insert/u.0", "quad": right, "t0": 2.0, "t1": 5.0}
    scene = {"start": 0.0, "end": 8.0, "inserts": [_keyed("insert/k.0", left, 2.0, 5.0), unkeyed]}
    timeline.hold_surfaces(scene["inserts"], 8.0)
    assert "held" not in unkeyed
    assert [(o, s) for _p, o, s in timeline.insert_layers(scene, 1.0)] == [(1.0, 0.0), (0.0, None)]
    # Held to the scene's end on its last frame, however the length was rounded.
    assert timeline.insert_held(scene["inserts"][0], 8.0 - timeline.HELD_END_SLACK / 2, 8.0)


def test_an_insert_shows_as_the_page_fades_it():
    payload = {"t0": 1.0, "t1": 3.0}
    seen = [timeline.insert_visibility(payload, t) for t in (0.99, 1.0, 1.1, 2.0, 2.9, 3.0)]
    assert seen == pytest.approx([0.0, 0.0, 0.5, 1.0, 0.5, 0.0])
    assert timeline.insert_visibility({"t0": 0.0, "t1": 3.0}, 0.0) == 1.0
    # The page reads the same numbers, injected from the one source.
    script = compositor_page.page_html("")
    assert "__INSERT_FADE__" not in script and "__HELD_END_SLACK__" not in script
    assert f"const INSERT_FADE = {float(INSERT_FADE_SECONDS)!r}, HELD_END_SLACK = {timeline.HELD_END_SLACK!r};" in script


def test_every_join_is_a_window_of_frames_a_transition_s_and_a_cut_s_each_side():
    document = {"fps": FPS, "scenes": [{"start": 0.0}, {"start": 1.5, "transition_in": {"duration": 0.5}}, {"start": 3.0}]}
    assert timeline.transition_windows(document) == [(1.5, 2.0, 1), (3.0 - 2 / FPS, 3.0 + 2 / FPS, 2)]


def test_a_frame_the_page_draws_an_insert_on_that_it_was_not_drawn_on_is_no_finding_off_its_audits_alone():
    document = _document(CUT)
    render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)})
    for n in range(0, 2 * FPS):
        render.frame(n)
    placed = insert_checks.placed_inserts(document, render.inserts)
    key = KEYS["a"]
    assert placed[key].expected_frames() == list(range(0, 2 * FPS))
    gone = 2 * FPS - 1
    render.audits[key]["frames"].pop(gone)
    assert insert_checks.missing_frames(placed[key], render.audits[key]["frames"]) == [gone]
    # The page log's miss is the presence gate's to say, off the encoded frame: never a finding of the audits.
    passed = {key: [{"stage": "preview", "id": "p1", "gates": {"flicker": True}}]}
    found = [f for f, _ in insert_checks.findings(render.audits, placed, fps=FPS, passed=passed) if f.kind == INSERT_FLICKER]
    assert found == []
    report = next(r for r in insert_checks.reports(render.audits, placed, fps=FPS) if r["key"] == key)
    # Nor counted in its flicker fact until the encoded frame reads it missing; the joins' audits count
    # no join of their own.
    assert "missing_frames" not in report and report["flicker_frames"] == 0 and "missing" not in report["joins"]
    lines = [m.line() for m in insert_checks.classify(report)]
    assert any(line.endswith("flicker 0 frame(s)") for line in lines) and not any("not drawn" in line for line in lines)
    (joins,) = [line for line in lines if "on its scene's joins with the scenes beside it: " in line]
    assert joins.endswith("frame(s) measured, residual key 0.00% of the quad at most")


def test_a_residual_key_on_one_frame_of_a_join_blocks_and_on_one_frame_elsewhere_does_not():
    document = _document(CUT)
    render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)})
    for n in range(0, 2 * FPS):
        render.frame(n)
    key = KEYS["a"]
    placed = insert_checks.placed_inserts(document, render.inserts)
    frames = render.audits[key]["frames"]
    inside, join = 10, 2 * FPS - 1
    assert not placed[key].at_join(inside) and placed[key].at_join(join)
    frames[inside] = {**frames[inside], "residual_key": 0.02}
    found = [f for f, _ in insert_checks.findings(render.audits, placed, fps=FPS) if f.kind == INSERT_RESIDUAL_KEY]
    assert found == []
    frames[join] = {**frames[join], "residual_key": 0.02}
    passed = {key: [{"stage": "save", "id": "r7", "gates": {"residual_key": True}}]}
    (found,) = [f for f, _ in insert_checks.findings(render.audits, placed, fps=FPS, passed=passed) if f.kind == INSERT_RESIDUAL_KEY]
    assert found.severity == FAIL and found.data["transition"] == 1 and found.data["frame"] == join
    assert "1 on its scene's join with another (a transition or a cut), where one frame blocks" in found.message
    assert insert_checks.verdicts(frames, placed[key])["residual_key"] is False


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.answers: list[dict] = []

    async def fulfill(self, **answer) -> None:
        self.answers.append(answer)

    async def abort(self, reason: str) -> None:
        self.answers.append({"aborted": reason})


async def test_a_layer_is_reused_only_for_the_same_scene_time_settings_and_visibility(monkeypatch, tmp_path):
    path, wall = tmp_path / "ui.png", tmp_path / "wall.png"
    text_ui((40, 46, 60)).save(path)
    Image.fromarray(booth(seed=7)).save(wall)

    def on(scene_id: str) -> InsertSource:
        # The same picture on the same still backdrop, in two scenes: what it is drawn from names its scene's footage.
        return InsertSource(str(path), "image", tuple(tuple(p) for p in QUAD), footage=Footage(str(wall), "image", key=f"backdrop/{scene_id}"))

    assets = compositor.CompositorAssets(inserts={"insert/a.0": on("a"), "insert/b.0": on("b")})
    server = compositor.FrameServer(assets, "<html></html>", guard={5: ["insert/a.0"]})
    drawn: list[float] = []
    compose = screen_insert.compose

    def counted(*args, **kwargs):
        drawn.append(kwargs.get("visibility", 1.0))
        return compose(*args, **kwargs)

    monkeypatch.setattr(screen_insert, "compose", counted)
    for url in (
        "insert/a.0/0.png?w=480&h=270&n=0",
        "insert/a.0/0.glow.png?w=480&h=270&n=0",  # the same layer, asked for its glow
        "insert/a.0/0.png?w=480&h=270&n=1&v=500",  # another visibility
        "insert/b.0/0.png?w=480&h=270&n=1&v=500",  # the same file and frame, another scene
    ):
        await server.handle(_Route(f"{compositor.ORIGIN}/{url}"))
    assert drawn == [1.0, 0.5, 0.5]
    # Kept on the worker's disk under how much of it shows: another page reads the frame back, nothing composed.
    assert insert_frames.frame_name(0, None, (480, 270), 1.0, 0.5).endswith("-v0500")
    again = compositor.FrameServer(assets, "<html></html>")
    await again.handle(_Route(f"{compositor.ORIGIN}/insert/a.0/0.png?w=480&h=270&n=2&v=500"))
    assert drawn == [1.0, 0.5, 0.5] and again.audits["insert/a.0"][2].visibility == 0.5
    assert server._frames_of("insert/a.0", assets.inserts["insert/a.0"]).folder != server._frames_of(
        "insert/b.0", assets.inserts["insert/b.0"]
    ).folder
    # Where the page placed each insert on a guard's frame, 0-1 of the frame, and how much of it showed, handed on with
    # the segment's audits; its scene drawn alone there kept beside them.
    server.place(5, {"insert/a.0": [48.0, 27.0, 240.0, 135.0, 0.4]}, (480, 270))
    server.place(6, {"insert/a.0": [0.0, 0.0, 10.0, 10.0, 1.0]}, (480, 270))
    assert server.guard_scenes(5) == ["a"] and server.guard_scenes(6) == []
    await server.keep_alone(5, "a", b"png", {"insert/a.0": [48.0, 27.0, 240.0, 135.0, 1.0]}, (480, 270))
    assert server.alone == {}  # no evidence directory: nothing kept
    server.evidence_dir = tmp_path / "evidence"
    await server.keep_alone(5, "a", b"png", {}, (480, 270))
    taken = server.take_audits(0, 10)
    assert taken["insert/a.0"]["placed"] == {"5": [0.1, 0.1, 0.6, 0.6, 0.4]}
    # Placed nowhere on the scene drawn alone: kept with no box, which the guard reads as an insert dropped.
    assert taken["insert/a.0"]["alone"] == {"5": {"file": "alone-a-0000005.png", "box": None}}
    assert (tmp_path / "evidence" / "alone-a-0000005.png").read_bytes() == b"png"


def test_a_neighbour_s_finding_on_the_join_blocks_its_preview_and_is_said_as_a_finding():
    checks = Checks()
    joined = Finding(INSERT_RESIDUAL_KEY, 1.0, 1.2, "s1", "residual key at t=0:01.00: ...", FAIL, {"insert": "insert/s1.0", "transition": 1})
    elsewhere = Finding(INSERT_RESIDUAL_KEY, 1.0, 1.2, "s1", "residual key at t=0:01.00: ...", FAIL, {"insert": "insert/s1.0"})
    checks.findings = [joined, elsewhere]
    checks.inserts = [{"key": "insert/s1.0", "label": "Scene s1: its screen insert 0", "scene_id": "s1", "frames": 12}]
    render_checks.settle_neighbours(checks, "s2", {})
    kept, warned = checks.findings
    assert kept.severity == FAIL and kept.message.startswith("on scene s1, measured on this preview's join with it: ")
    assert warned.severity == WARN and "its own preview judges it" in warned.message
    heading = render_checks.insert_heading(checks.inserts[0])
    assert heading.endswith(", a neighbour's, measured on this preview's frames of the join")
    answer = render_answer.of_checks(checks.as_dict())
    assert [item["severity"] for item in answer.findings] == [render_answer.BLOCKS]
    assert any(fact.get("what", "").startswith("seen in the transition from this scene") for fact in answer.facts)


def test_the_page_draws_a_held_keyed_insert_whole_and_names_its_visibility():
    script = compositor_page.page_html("")
    assert "const base = held[k] && !items.some((other, j) => j < k && held[j] && other.ins.surface === ins.surface);" in script
    assert "+ (base ? `&v=${Math.round(a * 1000)}` : '')" in script
    # Never the old ramp of the insert on its own over the key.
    assert "prog(t, ins.t0, 0.2)" not in script


def test_a_guard_frame_with_no_reading_is_a_warning_saying_what_it_read_never_a_failure(tmp_path):
    """A guard frame with neither its scene drawn alone nor where the page placed the insert (a segment an attempt
    before drew) is no measure of an absence: a warning naming it, the frames asked and read - while a frame the page
    drew alone with no layer of the insert stays a failure."""
    document = _document(CROSSFADE)
    render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)}, keep=tmp_path)
    join = _join_frames(document)
    frames = {n: render.frame(n) for n in join}
    placed = insert_checks.placed_inserts(document, render.inserts)
    guard = insert_checks.guard_frames(placed)
    key = next(key for key in render.inserts if any(key in keys for keys in guard.values()))
    asked = sorted(frame for frame, keys in guard.items() if key in keys)
    lost = asked[0]
    render.audits[key]["alone"].pop(lost, None)
    render.audits[key]["placed"].pop(lost, None)
    judged = insert_checks.judge_dropped(frames, render.audits, placed, guard, document, render.keys(), fps=FPS)
    [(finding, proof)] = judged
    assert (finding.kind, finding.severity, proof) == (INSERT_DROPPED, WARN, None)
    assert finding.message.startswith(f"insert guard could not read frame {lost} of ")
    assert f"requested {len(asked)} guard frames, read {len(asked) - 1}" in finding.message
    assert (finding.data["requested"], finding.data["read"], finding.data["unread"]) == (len(asked), len(asked) - 1, True)
    # The page drew that scene alone there and placed no layer of it: measured, a failure.
    render.audits[key]["alone"][lost] = {"file": "alone.png", "box": None}
    [(finding, _proof)] = insert_checks.judge_dropped(frames, render.audits, placed, guard, document, render.keys(), fps=FPS)
    assert (finding.severity, finding.data["placed"]) == (FAIL, False)
    assert "has no layer where the page should have placed it on its scene drawn alone" in finding.message


@pytest.mark.asyncio
async def test_the_guard_never_reads_a_frame_an_earlier_attempt_left_in_its_folder(tmp_path, monkeypatch):
    """``encoder.extract_frames`` clears the PNGs of a pass before it runs: a frame ffmpeg did not yield this time
    is missing, never the one an earlier attempt wrote."""
    from app.services.video_studio import encoder

    stale = tmp_path / "frame-0000010-002.png"
    stale.write_bytes(b"from an attempt before")

    async def yields_the_first_only(args, **_kwargs):
        Path(str(args[-1]).replace("%03d", "001")).write_bytes(b"png")

    monkeypatch.setattr(encoder, "ffmpeg", yields_the_first_only)
    read = await encoder.extract_frames("video.mp4", [10, 11], fps=FPS, out_dir=str(tmp_path))
    assert read == {10: str(tmp_path / "frame-0000010-001.png")}
    assert not stale.exists()


def test_a_partly_shown_frame_is_judged_for_its_surface_alone():
    render = _Render(_document([("a", 0.0, 2.0, "cut", 0.0, 0.5)]), {"a": (40, 46, 60)})
    picture = render.frame(round(0.55 * FPS))
    audit = render.audits[KEYS["a"]]["frames"][round(0.55 * FPS)]
    assert 0.0 < audit["visibility"] < 1.0
    assert audit["residual_key"] is not None and audit["residual_key"] <= screen_insert.RESIDUAL_FAIL_SHARE
    assert audit["sharpness"] is None and audit["black_lift"] is None and audit["luma"] is None
    assert render.key_left(picture) == 0.0
    buffer = io.BytesIO()
    Image.fromarray(picture).save(buffer, format="PNG")
    assert buffer.getvalue()
