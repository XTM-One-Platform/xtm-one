"""A screen insert that runs to its scene's end, on every frame its scene is painted on.

Preview 7b2a216d of revision 125 (an 8.0 s scene, its keyed insert from 0 for 14 s, extend_mode hold, a cut out, the
preview 8.2 s long) showed the source whole at 7.6 s and the surface flat in the bezel's colour at 8.15 s, the footage
moving: the insert's ``t1`` was the scene's length when the inserts were planned, before the music's beat lengthened
it, and the page faded it out over its last 0.2 s - while ``held`` ran to the scene's last frame, drawing the surface in
the bezel's colour there. The join gate read the layer's audits, so a bezel-coloured layer passed as "drawn on every
one".

Synthetic fixtures only - no provider, no browser, no database. Each frame is composed as the render composes it: each
scene's keyed insert drawn onto its own footage by ``screen_insert.compose`` (``timeline.insert_layers``), the scenes
blended as the page's ``applyTransition`` blends them, the frame then put through a lossy encode. The presence gate
reads those encoded frames (``insert_checks.PresenceGate``), never the layer:

* F31 a keyed insert from 0 for longer than its scene, a cut out, the scene held longer than planned: its source on the
  scene's last painted frame;
* F32 the same with a fade_black out: its source on every blended frame that still shows it;
* F33 three such scenes, the middle one previewed: its source on every frame of the preview, its trailing window
  included;
* F34 a surface drawn in the bezel's colour under a declared insert: "insert missing", never "drawn on every one".

And the pieces: the planner's ``to_end`` and its one clock, the page's ramp, the frame server's and the page's tripwires.
"""

from __future__ import annotations

import io
import math
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import compositor, compositor_page, insert_checks, render, screen_insert, timeline
from app.services.video_studio.render_checks import INSERT_MISSING
from app.services.video_studio.screen_insert import Footage, InsertSource
from app.services.video_studio.spec import INSERT_FADE_SECONDS, parse_video_spec, validate_video_spec
from tests.video_studio_insert_fixture import (
    FPS,
    GROUNDS,
    QUAD,
    SCREEN,
    H,
    W,
    booth,
    clip_document,
    key_of,
    source_of,
    text_ui,
    weights,
)


class _Render:
    """The render of a planned *document* frame by frame, as the page and the frame server draw it and an encoder keeps
    it: each scene's finished frame, the scenes stacked over a black stage at their layers' opacities, put through a
    lossy encode. Where the page places each insert and how much of it shows (its ``insertRects``) and each layer's
    audit are kept as the frame server keeps them."""

    def __init__(self, document: dict) -> None:
        self.document = document
        ids = [scene["id"] for scene in document["scenes"]]
        self.footage = {scene_id: booth(seed=7) for scene_id in ids}
        self.pictures = {key_of(scene_id): text_ui(GROUNDS[scene_id]) for scene_id in ids}
        self.inserts: dict[str, InsertSource] = {}
        self.fills: dict[str, object] = {}
        self.surfaces: dict[str, object] = {}
        self.states: dict[str, screen_insert.Matting] = {}
        for scene_id in ids:
            key, first = key_of(scene_id), self.footage[scene_id]
            self.inserts[key] = screen_insert.keyed_on(source_of(scene_id), first)
            self.fills[key] = screen_insert.static_fill(self.inserts[key], first, (W, H))
            self.surfaces[key] = screen_insert.Surface.of(first, list(map(tuple, SCREEN)))
            self.states[key] = screen_insert.Matting()
        self.audits: dict[str, dict] = {key: {"frames": {}, "evidence": {}, "placed": {}} for key in self.inserts}
        #: What the gate expects each insert to show (its source), while :attr:`pictures` is what is drawn on its quad.
        self.sources = dict(self.pictures)
        self._drawn: dict[int, dict[str, np.ndarray]] = {}

    def _scene(self, scene: dict, frame: int, shown: float) -> np.ndarray:
        rgb = self.footage[scene["id"]].astype(np.float32)
        t = max(0.0, frame / FPS - float(scene["start"]))
        for payload, opacity, visibility in timeline.insert_layers(scene, t):
            if opacity <= 0:
                continue
            key = payload["src"]
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
                visibility=1.0 if visibility is None else visibility,
            )
            self.audits[key]["frames"][frame] = layer.audit.as_dict()
            box = [min(max(float(v), 0.0), 1.0) for corner in (payload["box"][0], payload["box"][2]) for v in corner]
            self.audits[key]["placed"][frame] = [*box, round(shown * opacity, 3)]
            x0, y0, x1, y1 = layer.warped.box
            image = np.asarray(layer.warped.image, dtype=np.float32)
            alpha = image[..., 3:4] / 255.0 * opacity
            rgb[y0:y1, x0:x1] = image[..., :3] * alpha + rgb[y0:y1, x0:x1] * (1.0 - alpha)
        return rgb

    def _painted_scenes(self, frame: int) -> list[dict]:
        at = frame / FPS
        scenes = self.document["scenes"]
        return [s for i, s in enumerate(scenes) if at >= s["start"] and (at < s["end"] or i == len(scenes) - 1)]

    def _stack(self, frame: int, layers: dict[str, np.ndarray]) -> np.ndarray:
        """The scenes painted on *frame*, each drawn as *layers* holds it, stacked over a black stage at their layers'
        opacities (the page's ``applyTransition``)."""
        painted = self._painted_scenes(frame)
        if len(painted) == 1:
            return layers[painted[0]["id"]]
        before, after = painted
        left, coming = weights(after, frame / FPS)
        out = layers[before["id"]] * left
        return layers[after["id"]] * coming + out * (1.0 - coming)

    def layers(self, frame: int) -> dict[str, np.ndarray]:
        """Each scene painted on *frame*, drawn once (a keyed insert's matting carries from frame to frame)."""
        if frame not in self._drawn:
            painted = self._painted_scenes(frame)
            if len(painted) == 1:
                self._drawn[frame] = {painted[0]["id"]: self._scene(painted[0], frame, 1.0)}
            else:
                before, after = painted
                left, coming = weights(after, frame / FPS)
                self._drawn[frame] = {
                    before["id"]: self._scene(before, frame, left * (1.0 - coming)),
                    after["id"]: self._scene(after, frame, coming),
                }
        return self._drawn[frame]

    def frame(self, frame: int) -> np.ndarray:
        """Output *frame* as encoded: the scenes the page paints there (``renderFrame``), blended, then a lossy encode."""
        out = self._stack(frame, self.layers(frame))
        buffer = io.BytesIO()
        Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(buffer, format="JPEG", quality=90)
        return np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))

    def toned(self, frame: int, scene_id: str, value: float) -> Image.Image:
        """Output *frame* with the scene *scene_id* painted one flat *value* (0 black, 255 white) where its layer is,
        as the page's ``tone()`` draws it and the frame server keeps it: a lossless capture."""
        layers = {**self.layers(frame), scene_id: np.full((H, W, 3), float(value), dtype=np.float32)}
        return Image.fromarray(np.clip(self._stack(frame, layers), 0, 255).astype(np.uint8))

    def keep_blends(self, frames: range, folder) -> None:
        """What the frame server keeps of each presence frame blending an insert (``FrameServer.keep_blend``): its scene
        painted black and white, by insert and frame, in *folder*."""
        for key, audit in self.audits.items():
            scene_id = timeline.insert_scene(key)
            for n in frames:
                spot = audit["placed"].get(n)
                if spot is None or not insert_checks.PRESENCE_MIN_SHOWN <= spot[4] < insert_checks.BLEND_WHOLE:
                    continue
                kept = {}
                for tone, value in (("black", 0.0), ("white", 255.0)):
                    path = Path(folder) / f"blend-{scene_id}-{n:07d}-{tone}.png"
                    self.toned(n, scene_id, value).save(path)
                    kept[tone] = str(path)
                audit.setdefault("blend", {})[n] = kept

    def gate(self, frames: range, *, frame_range: tuple[int, int] | None = None, blends: bool = True) -> insert_checks.PresenceGate:
        """The presence gate over the encoded *frames*, as a render or a preview of *frame_range* reads its own MP4 - each
        frame blending an insert unblended with its scene painted black and white, unless *blends* is off (a segment
        drawn earlier)."""
        encoded = {n: self.frame(n) for n in frames}
        placed = insert_checks.placed_inserts(self.document, self.inserts)
        with tempfile.TemporaryDirectory() as folder:
            if blends:
                self.keep_blends(frames, folder)
            gate = insert_checks.PresenceGate(self.audits, placed, self.document, self.inserts, fps=FPS, frame_range=frame_range)
            for n, picture in encoded.items():
                gate.read(n, picture, {need: self.sources[need[0]] for need in gate.needs(n)})
        return gate


def _painted(document: dict, index: int) -> list[int]:
    return [frame for frame, _t in insert_checks._painted(document, index)]


# ── F31: a cut out, the scene held longer than planned ────────────────────

#: The insert planned on a 2.0 s scene, which plays 2.2 s (the cut on the music's beat): a cut into the next.
CUT = [("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 2.2, 4.2, 2.0, "cut", 0.0)]


def test_f31_an_insert_running_past_its_scene_is_on_the_scene_s_last_painted_frame():
    document = clip_document(CUT)
    payload = document["scenes"][0]["inserts"][0]
    # One clock: its t1 and its held end are the scene's painted length.
    assert payload["t1"] == payload["held"][1] == 2.2
    painted = _painted(document, 0)
    last = painted[-1]
    assert last == math.ceil(2.2 * FPS) - 1
    render_ = _Render(document)
    gate = render_.gate(range(painted[0], last + 1))
    result = gate.result()
    assert result.findings == []
    assert last in gate.read_frames[key_of("a")]
    # Whole to its last frame, never faded out: the layer's own visibility past the planned length is 1.
    assert all(render_.audits[key_of("a")]["frames"][n]["visibility"] is None for n in painted if n / FPS >= 1.8)
    assert result.by_key[key_of("a")]["missing"] == 0 and result.by_key[key_of("a")]["frames"] == len(painted)


def test_the_planner_shows_an_insert_running_past_its_scene_to_the_scene_s_end_after_the_beat_lengthened_it():
    """The 8.0 s scene of revision 125 played 8.2 s: its cut moved onto the music's beat after its inserts were planned."""
    clip = "00000000-0000-0000-0000-00000000c11b"
    insert = {"type": "screen_insert", "source": {"clip_id": clip}, "quad": QUAD, "start": 0, "duration": 14, "key": {"color": "green"}}
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Synthetic tail",
                "freestyle": True,
                "theme": {"motion": {"beat_sync": "cuts"}},
                "scenes": [
                    {"id": "shot", "type": "clip", "clip_id": clip, "extend_mode": "hold", "overlays": [insert]},
                    {"id": "next", "type": "clip", "clip_id": clip},
                ],
            }
        )
    )
    eight = timeline.ClipMedia(duration=8.0, width=1920, height=1080, fps=24.0, frame_count=192)
    media = timeline.PlanMedia(
        clips={"shot": eight, "next": eight},
        inserts={timeline.insert_key("shot", 0): timeline.ImageMedia(1280, 720)},
        inserts_gathered=True,
        beats=timeline.BeatGrid(period=0.5, phase=0.2),
    )
    scene = timeline.plan(spec, media, fps=24, theme={}).document["scenes"][0]
    length = round(scene["end"] - scene["start"], 4)
    assert length == 8.2
    (payload,) = scene["inserts"]
    assert payload["to_end"] and payload["t1"] == length and payload["held"] == [0.0, length]
    seen = [timeline.insert_visibility(payload, t) for t in (7.6, 7.9, 7.95, 8.05, 8.15)]
    assert seen == [1.0] * 5


# ── F32: the same with a fade_black out ───────────────────────────────────

FADE = [("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 1.7, 3.7, 2.0, "fade_black", 0.5)]


def test_f32_an_insert_running_past_its_scene_is_on_every_blended_frame_of_a_fade_black_out():
    document = clip_document(FADE)
    painted = _painted(document, 0)
    render_ = _Render(document)
    gate = render_.gate(range(painted[0], painted[-1] + 1))
    result = gate.result()
    assert result.findings == []
    key = key_of("a")
    blended = [n for n in painted if n / FPS >= 1.7]
    assert blended, "the fade's frames are painted with the scene it leaves"
    # Drawn whole through the fade: its layer never faded on its own, the transition dims the finished frame.
    assert all(render_.audits[key]["frames"][n]["visibility"] is None for n in blended)
    # Read on every blended frame that still shows it, each one present.
    shows = [n for n in blended if render_.audits[key]["placed"][n][4] >= insert_checks.PRESENCE_MIN_SHOWN]
    assert shows and set(shows) <= set(gate.read_frames[key])


# ── F33: three such scenes, the middle one previewed ──────────────────────

THREE = [
    ("a", 0.0, 2.2, 2.0, "cut", 0.0),
    ("b", 1.8, 4.4, 2.4, "crossfade", 0.4),
    ("c", 4.0, 6.2, 2.0, "crossfade", 0.4),
]


def test_f33_the_middle_scene_s_preview_shows_its_insert_on_every_frame_its_trailing_window_included():
    document = clip_document(THREE)
    begin, end, _settled_from, _settled_to = render.window_times(document, 1)
    frame_range = (math.floor(begin * FPS), min(int(document["frames"]), math.ceil(end * FPS)))
    painted = _painted(document, 1)
    # The preview's window runs to the end of the next scene's transition in: the middle scene's last frames in it.
    assert painted[-1] == frame_range[1] - 1
    render_ = _Render(document)
    gate = render_.gate(range(*frame_range), frame_range=frame_range)
    result = gate.result()
    key = key_of("b")
    assert [f.message for f, _ in result.findings] == []
    shows = [n for n in painted if render_.audits[key]["placed"][n][4] >= insert_checks.PRESENCE_MIN_SHOWN]
    assert set(shows) <= set(gate.read_frames[key])
    # Its trailing window: the frames the next scene comes in over, every one that still shows it read and present.
    trailing = [n for n in shows if n / FPS >= 4.0]
    assert trailing and set(trailing) <= set(gate.read_frames[key])


# ── F34: a surface drawn in the bezel's colour under a declared insert ────


def test_f34_a_surface_in_the_bezel_s_colour_under_a_declared_insert_is_an_insert_missing_never_drawn_on_every_one():
    # Planned as an older release planned it: the insert ends at the length planned, the scene painted 0.2 s past it, held there.
    document = clip_document(CUT, to_end=False)
    payload = document["scenes"][0]["inserts"][0]
    assert payload["t1"] == 2.0 and payload["held"] == [0.0, 2.2]
    painted = _painted(document, 0)
    render_ = _Render(document)
    encoded = {n: render_.frame(n) for n in painted}
    tail = [n for n in painted if n / FPS >= 2.0]
    # Past t1 the layer is drawn showing none of the insert: the surface in the bezel's colour, every audit passing.
    assert all(render_.audits[key_of("a")]["frames"][n]["visibility"] == 0.0 for n in tail)
    placed = insert_checks.placed_inserts(document, render_.inserts)
    assert not [f for f, _ in insert_checks.findings(render_.audits, placed, fps=FPS) if f.kind == INSERT_MISSING]
    # Read off those encoded frames: its spec says it shows whole there (14 s from 0), and nothing of it is.
    payload["to_end"] = True
    placed = insert_checks.placed_inserts(document, render_.inserts)
    gate = insert_checks.PresenceGate(render_.audits, placed, document, render_.inserts, fps=FPS)
    for n, picture in encoded.items():
        gate.read(n, picture, {need: render_.pictures[need[0]] for need in gate.needs(n)})
    result = gate.result()
    (finding, _proof), *_rest = [hit for hit in result.findings if hit[0].data["insert"] == key_of("a")]
    assert finding.kind == INSERT_MISSING and finding.data["why"] == "flat" and finding.data["gate"] == "presence"
    assert finding.data["frame"] in tail and finding.message.startswith(f"insert missing at t={insert_checks.clock(finding.data['frame'] / FPS)}: ")
    assert finding.data["colour"].startswith("#") and f"on frame {finding.data['frame']} of the encoded video" in finding.message
    # Said as a finding of its report, never "drawn on every one".
    report = next(r for r in insert_checks.reports(render_.audits, placed, fps=FPS, presence=result.by_key) if r["key"] == key_of("a"))
    lines = [m.line() for m in insert_checks.classify(report, {INSERT_MISSING})]
    assert not any(line.endswith("drawn on every one") for line in lines)
    assert any(line.startswith("finding (insert_missing): its source missing from") for line in lines)


def test_the_joins_line_says_the_frames_measured_and_the_residual_key_alone():
    """The joins line says the frames its layer was measured on there and the residual key it left: what the
    encoded frames showed is the presence line's, never said twice."""
    report = {"joins": {"frames": 4, "residual_key": 0.0}}
    joins_line = "on its scene's joins with the scenes beside it: 4 frame(s) measured, residual key 0.00% of the quad at most"

    def said(r: dict) -> list[str]:
        return [m.text for m in insert_checks.classify(r) if "joins with the scenes beside it" in m.text]

    present = {"frames": 20, "missing": 0, "unjudged": 0, "joins": 4, "joins_missing": 0}
    lost = {**present, "missing": 2, "joins_missing": 1, "first_missing": 1.9}
    read = {**report["joins"], "read": {"present": [[1.942, 2.025]], "missing": [[1.9, 1.9]]}}
    for shown in (report, {"joins": read, "presence": present}, {"joins": read, "presence": lost}, {**report, "presence": lost}):
        assert said(shown) == [joins_line]
    # Its source missing there is its presence line's.
    lines = [m.text for m in insert_checks.classify({"joins": read, "presence": lost})]
    assert "its source missing from 2 of 20 encoded frame(s) it shows whole on, the first at t=0:01.90" in lines
    # No frame of its layer measured there: no joins line.
    assert said({"joins": {"frames": 0, "residual_key": 0.0, "read": read["read"]}, "presence": present}) == []


def test_another_picture_on_the_quad_is_absent_and_a_flat_source_is_never_called_flat():
    document = clip_document(CUT)
    render_ = _Render(document)
    painted = _painted(document, 0)
    placed = insert_checks.placed_inserts(document, render_.inserts)
    frames = {n: render_.frame(n) for n in painted[:6]}
    # Compared with a picture of other lines: its source is not what the quad shows.
    other = Image.new("RGB", (600, 450), (40, 46, 60))
    for column in range(40, 560, 60):
        other.paste((220, 220, 220), (column, 30, column + 12, 420))
    gate = insert_checks.PresenceGate(render_.audits, placed, document, render_.inserts, fps=FPS)
    for n, picture in frames.items():
        gate.read(n, picture, {need: other for need in gate.needs(n)})
    (finding, _proof), *_ = gate.result().findings
    assert finding.data["why"] == "absent" and finding.data["value"] < insert_checks.PRESENCE_SSIM_MIN
    # A source that is one flat colour itself: a flat quad is what it shows, never judged flat.
    blank = Image.new("RGB", (600, 450), (30, 30, 30))
    gate = insert_checks.PresenceGate(render_.audits, placed, document, render_.inserts, fps=FPS)
    for n, picture in frames.items():
        gate.read(n, picture, {need: blank for need in gate.needs(n)})
    assert gate.result().findings == [] and gate.unjudged[key_of("a")] == len(frames)


# ── The pieces ────────────────────────────────────────────────────────────


def test_an_insert_running_to_its_end_never_fades_out_and_one_ending_inside_its_scene_does():
    running = {"t0": 0.0, "t1": 8.0, "to_end": True}
    assert [timeline.insert_visibility(running, t) for t in (0.0, 7.9, 7.95, 8.0, 8.15)] == [1.0] * 5
    late = {"t0": 1.0, "t1": 8.0, "to_end": True}
    assert timeline.insert_visibility(late, 1.1) == pytest.approx(0.5) and timeline.insert_visibility(late, 8.1) == 1.0
    inside = {"t0": 1.0, "t1": 3.0}
    assert [timeline.insert_visibility(inside, t) for t in (2.9, 3.0)] == pytest.approx([0.5, 0.0])
    # The page's ramp reads the same flag.
    script = compositor_page.page_html("")
    assert "const insertShown = (ins, t) => t >= ins.t0 && (ins.to_end || t < ins.t1)" in script
    assert "ins.to_end ? 1 : 1 - prog(t, ins.t1 - INSERT_FADE, INSERT_FADE)" in script


def test_one_clock_t1_held_and_a_video_s_last_held_frame_move_to_the_painted_length():
    video = {"t0": 0.0, "t1": 8.0, "to_end": True, "keyed": True, "quad": QUAD, "time_map": [[0.0, 0.0], [4.0, 4.0], [8.0, 4.0]]}
    inside = {"t0": 0.0, "t1": 3.0, "keyed": True, "quad": [[0.6, 0.1], [0.9, 0.1], [0.9, 0.4], [0.6, 0.4]]}
    timeline.hold_surfaces([video, inside], 8.2)
    assert video["t1"] == 8.2 and video["held"] == [0.0, 8.2] and video["time_map"][-1] == [8.2, 4.0]
    assert inside["t1"] == 3.0 and inside["held"] == [0.0, 8.2]
    assert timeline.painted_end(video) == 8.2 and timeline.painted_end({"t1": 5.0, "to_end": True}) == 5.0


def test_the_frames_an_insert_running_to_its_end_shows_whole_on():
    document = clip_document([("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 2.2, 4.2, 2.0, "cut", 0.0)])
    late = document["scenes"][1]["inserts"][0]
    late["t0"] = 0.5
    whole = timeline.whole_frames(document)
    # From its first frame to its scene's last painted one (2.2 s at 24 fps: frame 52), the frame past it the first out.
    assert whole[key_of("a")] == (0, 53)
    # Coming in 0.5 s into its scene: whole once its fade in is done (2.9 s: frame 70), to its scene's end (4.2 s).
    assert INSERT_FADE_SECONDS == 0.2 and whole[key_of("b")] == (70, 101)


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.answers: list[dict] = []

    async def fulfill(self, **answer) -> None:
        self.answers.append(answer)

    async def abort(self, reason: str) -> None:
        self.answers.append({"aborted": reason})


async def test_the_frame_server_and_the_page_keep_every_frame_that_paints_past_the_painted_end(tmp_path):
    path, wall = tmp_path / "ui.png", tmp_path / "wall.png"
    text_ui((40, 46, 60)).save(path)
    Image.fromarray(booth(seed=7)).save(wall)
    insert = InsertSource(str(path), "image", tuple(tuple(p) for p in QUAD), footage=Footage(str(wall), "image", key="backdrop/a"))
    server = compositor.FrameServer(compositor.CompositorAssets(inserts={"insert/a.0": insert}), "<html></html>", whole={"insert/a.0": (0, 53)})
    for url in (
        "insert/a.0/0.png?w=480&h=270&n=10&v=1000",  # whole, as it must be
        "insert/a.0/0.png?w=480&h=270&n=50&v=0",  # the bezel's colour inside the span it shows whole on
        "insert/a.0/0.png?w=480&h=270&n=53&v=1000",  # its scene painted past its painted end
    ):
        await server.handle(_Route(f"{compositor.ORIGIN}/{url}"))
    # What the page saw itself: its scene painted at t = 2.25 s, past 2.2.
    server.overran([{"src": "insert/a.0", "scene": "a", "frame": 54, "t": 2.25, "length": 2.2, "painted_end": 2.2}])
    taken = server.take_audits(0, 60)["insert/a.0"]["overrun"]
    assert sorted(taken) == ["50", "53", "54"]
    assert taken["50"] == {"why": "partly_shown", "visibility": 0.0} and taken["53"] == {"why": "painted_past"}
    document = clip_document([("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 2.2, 4.2, 2.0, "cut", 0.0)])
    placed = insert_checks.placed_inserts(document, {"insert/a.0": insert})
    (finding,) = insert_checks.overruns({"insert/a.0": {"overrun": {int(n): v for n, v in taken.items()}}}, placed, document, fps=FPS)
    assert finding.kind == INSERT_MISSING and finding.data["frame"] == 50 and finding.data["frames"] == 3
    assert (finding.data["length"], finding.data["painted_end"]) == (2.2, 2.2)
    assert "scene a, length 2.200 s, painted end 2.200 s" in finding.message
    # The page notes each frame it paints there, and hands them on once.
    script = compositor_page.page_html("")
    assert "if (!S.media) noteOverrun(sc, ins, t, length);" in script and "overruns() {" in script
