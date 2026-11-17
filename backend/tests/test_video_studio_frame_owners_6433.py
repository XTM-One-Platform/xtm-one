"""Each encoded frame judged for the scene that owns it.

A clip scene with a screen insert running to its end (6.127 s long, painted end 6.127 s) cut into the next scene, at
60 fps: the render warned "insert missing at t=2:14.65 ... frame 8079 has its scene's footage painted at t=6.128 s of
the scene, at or past its painted end - nothing in the spec causes it and rendering again draws the same frame", and
said the next render of the same scene would block. The cut lands at frame 8078.94: frame 8079 is the next scene's, its
full-screen recording with no quad. Under motion blur its first sub-frame (8078.83) still paints the scene the cut
leaves, stamped 8079, and the frame server judged that request by its output frame against a span worked out from
seconds.

Synthetic fixtures only - no provider, no browser, no ffmpeg:

* two scenes joined by a cut, the first with an insert to its end, at 30 and 60 fps, 6.127 s long: no finding, the
  motion blur's sub-frames drawn through the real frame server;
* the same with the insert gone from its scene's last 2 frames: the presence gate fails on exactly those 2;
* the ownership table (``frame_owners.FrameOwners``) equal to the page's rule and to every check's frames, on 20 random
  scene lengths;
* a finding nothing in the spec causes: the harness's, said once, never a finding of the video.
"""

from __future__ import annotations

import io
import math
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import (
    compositor,
    compositor_page,
    frame_freeze,
    insert_checks,
    motion_design,
    render_answer,
    render_checks,
    timeline,
)
from app.services.video_studio.frame_owners import CUT_FRAMES, FrameOwners, first_at
from app.services.video_studio.render_checks import FAIL, INSERT_MISSING
from app.services.video_studio.screen_insert import Footage, InsertSource

W, H = 320, 180
QUAD = [[0.3, 0.25], [0.7, 0.25], [0.7, 0.75], [0.3, 0.75]]
LENGTH = 6.127
#: Where the scene with the insert starts at each frame rate: its cut lands 0.94 of a frame past a frame (8078.94 at
#: 60 fps, the render of the report; 4039.94 at 30), so the first sub-frame of the next scene's first frame still
#: paints it under motion blur.
STARTS = {60: 128.522, 30: 128.5377}
KEY = "insert/a.0"


def _document(fps: int, start: float, *, transition: tuple[str, float] = ("cut", 0.0)) -> dict:
    """Scene ``a`` (a clip of :data:`LENGTH` s from *start*, its insert planned to run to its end) and scene ``b`` (a
    full-screen recording, no insert) joined by *transition*, planned as ``timeline.plan`` does."""
    end = round(start + LENGTH, 4)
    kind, seconds = transition
    insert = {"src": KEY, "quad": QUAD, "t0": 0.0, "t1": LENGTH - 0.3, "to_end": True}
    timeline.hold_surfaces([insert], round(end - start, 4))
    b_start = round(end - seconds, 4)
    scenes = [
        {"id": "a", "type": "clip", "start": start, "end": end, "transition_in": {"type": "cut", "duration": 0.0}, "inserts": [insert]},
        {"id": "b", "type": "recording", "start": b_start, "end": round(b_start + 3.0, 4), "transition_in": {"type": kind, "duration": seconds}},
    ]
    duration = scenes[-1]["end"]
    return {"fps": fps, "frames": math.ceil(duration * fps), "duration": duration, "fade_in": 0.0, "fade_out": 0.0, "scenes": scenes}


def _ui(seed: int, size: tuple[int, int]) -> Image.Image:
    """A page of a product: a header, panels and lines of text, its detail everywhere."""
    rng = np.random.default_rng(seed)
    picture = Image.new("RGB", size, (18, 24, 36))
    draw = ImageDraw.Draw(picture)
    w, h = size
    draw.rectangle([0, 0, w, h // 10], fill=(40, 180, 200))
    for _ in range(14):
        x, y = int(rng.integers(0, w - 60)), int(rng.integers(h // 8, h - 40))
        fill = tuple(int(v) for v in rng.integers(40, 230, 3))
        draw.rectangle([x, y, x + int(rng.integers(40, 160)), y + int(rng.integers(20, 90))], fill=fill)
    for row in range(h // 8, h, 14):
        draw.line([(w // 20, row), (w // 20 + int(rng.integers(w // 4, w - w // 10)), row)], fill=(220, 220, 220), width=2)
    return picture


def _footage(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(30, 140, (H, W, 3), dtype=np.uint8)


def _encoded(rgb: np.ndarray) -> np.ndarray:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="JPEG", quality=90)
    return np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))


SOURCE = _ui(1, (512, 360))
RECORDING = np.asarray(_ui(2, (W, H)))
BEZEL = (30, 30, 34)


def _box() -> tuple[int, int, int, int]:
    return round(QUAD[0][0] * W), round(QUAD[0][1] * H), round(QUAD[2][0] * W), round(QUAD[2][1] * H)


def _frame(scene: str, *, shown: bool = True) -> np.ndarray:
    """An encoded frame: scene ``a``'s footage with its insert on its quad - or its surface in the bezel's colour when
    not *shown* -, or scene ``b``'s recording, full screen."""
    if scene == "b":
        return _encoded(RECORDING.copy())
    rgb = _footage(7).copy()
    x0, y0, x1, y1 = _box()
    rgb[y0:y1, x0:x1] = np.asarray(SOURCE.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)) if shown else BEZEL
    return _encoded(rgb)


def _inserts() -> dict[str, InsertSource]:
    return {KEY: InsertSource("a.png", "image", tuple(tuple(p) for p in QUAD), label="Scene a: its screen insert 0")}


def _gate(document: dict, frames: range, *, gone: set[int] = frozenset()) -> insert_checks.PresenceGate:
    """The presence gate over the encoded *frames*, the page having placed scene ``a``'s layer on each of its own."""
    owners = FrameOwners.of(document)
    mine = set(owners.frames_of(0))
    audits = {KEY: {"frames": {}, "placed": {str(n): [*QUAD[0], *QUAD[2], 1.0] for n in frames if n in mine}}}
    placed = insert_checks.placed_inserts(document, _inserts(), owners=owners)
    gate = insert_checks.PresenceGate(audits, placed, document, _inserts(), fps=document["fps"])
    for n in frames:
        scene = owners.ids[owners.owner(n)]
        gate.read(n, _frame(scene, shown=n not in gone), {need: SOURCE for need in gate.needs(n)})
    return gate


# ── The cut of the report ─────────────────────────────────────────────────


@pytest.mark.parametrize("fps", [60, 30])
def test_an_insert_to_its_scene_s_end_is_judged_on_its_scene_s_encoded_frames_and_never_the_next_one(fps):
    document = _document(fps, STARTS[fps])
    a = document["scenes"][0]
    owners = FrameOwners.of(document)
    cut = math.ceil(a["end"] * fps)
    assert a["end"] * fps % 1 == pytest.approx(0.94, abs=0.002), "the cut lands inside a frame"
    # The scene's first encoded frame to its last, inclusive, as the page places the cut.
    assert owners.frames_of(0) == range(math.ceil(a["start"] * fps), cut) and owners.frames_of(1).start == cut
    assert owners.table()[cut - 1] == ("a",) and owners.table()[cut] == ("b",)
    if fps == 60:
        assert cut == 8079 and render_checks.clock(cut / fps) == "2:14.65"
    placed = insert_checks.placed_inserts(document, _inserts(), owners=owners)
    whole = placed[KEY].whole_frames()
    assert whole[-1] == cut - 1 and cut not in insert_checks.presence_frames(placed)
    assert timeline.whole_frames(document) == {KEY: (whole[0], cut)}
    # Its last 12 frames and the next scene's first 6, read off the encoded video: present on every one of its own.
    gate = _gate(document, range(cut - 12, cut + 6))
    result = gate.result()
    assert [finding.message for finding, _ in result.findings] == []
    assert gate.read_frames[KEY] == list(range(cut - 12, cut))
    assert result.by_key[KEY]["frames"] == 12 and result.by_key[KEY]["missing"] == 0


@pytest.mark.parametrize("fps", [60, 30])
def test_the_same_insert_gone_from_its_scene_s_last_two_frames_fails_on_exactly_those(fps):
    document = _document(fps, STARTS[fps])
    cut = math.ceil(document["scenes"][0]["end"] * fps)
    gone = {cut - 2, cut - 1}
    result = _gate(document, range(cut - 12, cut + 6), gone=gone).result()
    ((finding, _proof),) = result.findings
    assert finding.kind == INSERT_MISSING and finding.severity == FAIL and finding.data["why"] == "flat"
    assert finding.data["frame"] == cut - 2 and finding.data["frames"] == 2 and finding.data["measured"] == 12
    assert finding.data["span"] == [round((cut - 2) / fps, 3), round((cut - 1) / fps, 3)]
    assert result.by_key[KEY]["missing"] == 2
    # Nothing in the spec causes it, but on two frames it is the video's: a block, never the harness's.
    assert render_checks.harness_defects([finding], {}) == ([finding], [])


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.answers: list[dict] = []

    async def fulfill(self, **answer) -> None:
        self.answers.append(answer)

    async def abort(self, reason: str) -> None:
        self.answers.append({"aborted": reason})


async def _drawn_under_motion_blur(server: compositor.FrameServer, document: dict, frames: range, *, driven: bool) -> None:
    """*frames* drawn as the driver draws them under motion blur (``compositor.render_frames``): each sub-frame at its own
    time, the page asking scene ``a``'s matted layer wherever it paints the scene (``renderFrame``), stamped with the
    frame the sub-frame is part of (``S.frame``). *driven*: the driver says which sub-frame is off its frame's centre."""
    fps = float(document["fps"])
    a = document["scenes"][0]
    for frame in frames:
        for offset in motion_design.blur_offsets(motion_design.BLUR_SAMPLES):
            at = frame + offset
            if not a["start"] <= at / fps < a["end"]:
                continue
            server.shutter = driven and bool(offset)
            await server.handle(_Route(f"{compositor.ORIGIN}/{KEY}/0.png?w=480&h=270&n={round(at)}&v=1000"))
        server.shutter = False


@pytest.mark.parametrize("fps", [60, 30])
async def test_the_shutter_of_the_next_scene_s_first_frame_is_drawn_and_never_judged(tmp_path, fps):
    document = _document(fps, STARTS[fps])
    cut = math.ceil(document["scenes"][0]["end"] * fps)
    picture, wall = tmp_path / "ui.png", tmp_path / "wall.png"
    SOURCE.save(picture)
    Image.fromarray(_footage(7)).save(wall)
    insert = InsertSource(
        str(picture), "image", tuple(tuple(p) for p in QUAD), footage=Footage(str(wall), "image", key="clip/a"), label="Scene a: its screen insert 0"
    )
    assets = compositor.CompositorAssets(inserts={KEY: insert})
    placed = insert_checks.placed_inserts(document, {KEY: insert})

    def served() -> compositor.FrameServer:
        return compositor.FrameServer(assets, "<html></html>", whole=timeline.whole_frames(document))

    server = served()
    await _drawn_under_motion_blur(server, document, range(cut - 4, cut + 3), driven=True)
    taken = server.take_audits(0, document["frames"])[KEY]
    # Every frame of its own audited on its centre, never the next scene's; nothing painted past its end.
    assert sorted(int(n) for n in taken["frames"]) == list(range(cut - 4, cut)) and "overrun" not in taken
    assert insert_checks.overruns({KEY: taken}, placed, document, fps=fps) == []
    # The frame server judging the output frame each sub-frame is stamped with: the report's finding, on the cut.
    server = served()
    await _drawn_under_motion_blur(server, document, range(cut - 4, cut + 3), driven=False)
    taken = server.take_audits(0, document["frames"])[KEY]
    assert taken["overrun"] == {str(cut): {"why": "painted_past"}}
    (finding,) = insert_checks.overruns({KEY: {"overrun": {cut: {"why": "painted_past"}}}}, placed, document, fps=fps)
    if fps == 60:
        assert finding.message.startswith("insert missing at t=2:14.65: Scene a: its screen insert 0 runs to its scene's end")
        assert "frame 8079 has its scene's footage painted at t=6.128 s of the scene" in finding.message
    # A finding nothing in the spec causes: the harness's, said once with its frame - never the video's.
    finding.data["frame_file_id"] = "frame-8079"
    kept, harness = render_checks.harness_defects([finding], {})
    answer = render_answer.of_checks(render_checks.Checks(findings=kept, harness=harness).as_dict())
    assert answer.status == render_answer.GREEN and len(answer.infra_once) == 1
    assert "(frame: file `frame-8079`)" in answer.infra_once[0]


def test_the_page_paints_each_frame_by_the_rule_the_table_reads():
    script = compositor_page.page_html("")
    assert "const T = index / S.fps;" in script and "S.frame = Math.round(index);" in script
    assert "S.scenes.forEach((sc, i) => { if (T >= sc.start && (T < sc.end || i === S.scenes.length - 1)) active.push(i); });" in script
    # A transition blends only while the scene it leaves is painted.
    assert "if (!prev || p >= 1) return;" in script


def test_a_frame_s_time_reaching_a_second_is_read_as_the_page_compares_them():
    # 0.28 s at 25 fps: 0.28 * 25 is 7.000000000000001, whose ceiling is 8 - frame 7's time is 0.28, the page paints it.
    assert math.ceil(0.28 * 25) == 8 and 7 / 25 >= 0.28 and first_at(0.28, 25) == 7
    assert math.ceil(8.3 * 30) == 250 and first_at(8.3, 30) == 249
    assert first_at(0.0, 60) == 0 and first_at(134.649, 60) == 8079 and first_at(2.2, 24) == 53


def test_the_watch_reads_one_encoded_frame_in_a_whole_number():
    assert [frame_freeze.analysis_rate(fps) for fps in (24, 25, 30, 50, 60)] == [24, 25, 30, 25, 30]
    document = _document(50, 10.0)
    shown = frame_freeze.Shown.of(document, 50, frame_freeze.analysis_rate(50), offset=4.0)
    assert (shown.first, shown.step) == (200, 2) and shown.frame(7) == 214


async def test_the_watch_asks_the_decoder_a_whole_fraction_of_the_frame_rate(monkeypatch):
    from app.services.video_studio import encoder

    asked = []

    async def stream(_path, *, width, height, fps, start=0.0):
        asked.append(fps)
        for _ in range(10):
            yield np.zeros((height, width), dtype=np.uint8).tobytes()

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    check = await frame_freeze.check_frames("final.mp4", document=_document(50, 0.0), width=W, height=H, fps=50)
    assert asked == [25.0] and check.summary["fps"] == 25.0


# ── The table and every check, on 20 random scene lengths ─────────────────


def _random_document(seed: int) -> dict:
    """Two to four scenes of random lengths - never a whole number of frames -, joined by cuts and transitions as the
    planner joins them, each with an insert running to its end that comes in at 0 or later."""
    rng = np.random.default_rng(seed)
    fps = int(rng.choice([24, 25, 30, 50, 60]))
    scenes, cursor = [], 0.0
    for index in range(int(rng.integers(2, 5))):
        length = round(float(rng.uniform(1.5, 7.5)), 4)
        kind = "cut" if index == 0 else str(rng.choice(["cut", "crossfade", "blur", "fade_black"]))
        previous = scenes[-1]["end"] - scenes[-1]["start"] if scenes else 0.0
        overlap = 0.0 if kind == "cut" else round(min(float(rng.uniform(0.2, 0.8)), previous * 0.45), 4)
        start = round(max(0.0, cursor - overlap), 4)
        end = round(start + length, 4)
        t0 = 0.0 if rng.random() < 0.5 else round(float(rng.uniform(0.1, 1.0)), 4)
        insert = {"src": f"insert/s{index}.0", "quad": QUAD, "t0": t0, "t1": length, "to_end": True}
        timeline.hold_surfaces([insert], round(end - start, 4))
        scenes.append(
            {"id": f"s{index}", "type": "clip", "start": start, "end": end, "transition_in": {"type": kind, "duration": overlap}, "inserts": [insert]}
        )
        cursor = end
    duration = scenes[-1]["end"]
    return {"fps": fps, "frames": math.ceil(duration * fps), "duration": duration, "fade_in": 0.0, "fade_out": 0.0, "scenes": scenes}


def _page_paints(document: dict) -> list[tuple[str, ...]]:
    """The scenes the page's ``renderFrame`` paints on each frame's centre: ``T = n / fps``, every scene with
    ``T >= start`` and ``T < end``, the last one to the video's end."""
    fps, scenes = float(document["fps"]), document["scenes"]
    last = len(scenes) - 1
    return [
        tuple(scene["id"] for i, scene in enumerate(scenes) if n / fps >= scene["start"] and (n / fps < scene["end"] or i == last))
        for n in range(document["frames"])
    ]


@pytest.mark.parametrize("case", range(20))
def test_the_ownership_table_is_every_check_s_frames(case):
    document = _random_document(6433 + case)
    fps, scenes = float(document["fps"]), document["scenes"]
    owners = FrameOwners.of(document)
    table = owners.table()
    assert table == _page_paints(document)
    inserts = {scene["inserts"][0]["src"]: InsertSource("x.png", "image", tuple(tuple(p) for p in QUAD)) for scene in scenes}
    placed = insert_checks.placed_inserts(document, inserts)
    whole_spans = timeline.whole_frames(document)
    joins = owners.joins()
    for index, scene in enumerate(scenes):
        frames = owners.frames_of(index)
        payload = scene["inserts"][0]
        key = payload["src"]
        # The presence gate's frames: its scene's encoded frames, the last one included, nothing another scene owns alone.
        assert [n for n, _t in insert_checks._painted(document, index)] == list(frames)
        # The frames it owns: those no later scene is painted on, but the first of a transition in - the
        # scene before's -, and those it owns alone, no earlier scene painted on them.
        owned = owners.owned(index)
        assert list(owned) == [n for n in frames if owners.owner(n) == index]
        assert placed[key].owns == (owned.start, owned.stop)
        alone = owners.alone(index)
        assert list(alone) == [n for n in owned if table[n][0] == scene["id"]]
        assert placed[key].alone == (alone.start, alone.stop)
        if index and owners.blended[index] and len(table[frames.start]) > 1:
            assert owners.owner(frames.start) == index - 1 and owned.start == frames.start + 1
        whole = placed[key].whole_frames()
        assert whole == [n for n in frames if timeline.insert_visibility(payload, n / fps - scene["start"]) >= 1.0]
        assert whole[-1] == frames[-1] and all(scene["id"] in table[n] for n in whole)
        # The frame server's span is the presence gate's.
        assert whole_spans[key] == (whole[0], frames.stop)
        # Its joins: a cut's frames each side of it, a transition's those both its scenes are painted on.
        mine = [(first, end) for first, end, coming in joins if coming in (index, index + 1)]
        assert placed[key].joins == tuple(mine)
        blends = [(first, end) for first, end, coming in joins if coming in (index, index + 1) and owners.blended[coming]]
        assert [(first, end) for first, end, what in placed[key].blends if not what.startswith("video")] == blends
    for first, end, coming in joins:
        before, after = scenes[coming - 1]["id"], scenes[coming]["id"]
        if owners.blended[coming]:
            assert all({before, after} <= set(table[n]) for n in range(first, end))
        else:
            cut = owners.frames_of(coming).start
            assert (first, end) == (max(0, cut - CUT_FRAMES), min(owners.total, cut + CUT_FRAMES))
            assert all(table[n][-1] == before for n in range(first, cut)) and all(table[n] == (after,) for n in range(cut, end))
    # The frozen-picture watch: the scene each frame it reads shows is its encoded frame's owner - the later one painted,
    # but on a transition's first frame the scene before, whole there.
    rate = frame_freeze.analysis_rate(fps)
    shown = frame_freeze.Shown.of(document, fps, rate)
    assert fps / rate == shown.step
    firsts = {owners.frames_of(i).start for i in range(1, len(scenes)) if owners.blended[i]}
    for count in range(owners.total // shown.step):
        n = count * shown.step
        assert shown.scene(count)["id"] == table[n][-2 if n in firsts and len(table[n]) > 1 else -1]
    # The audio check's scene of a second (render_checks.scene_at) agrees on every frame's own time.
    assert all((render_checks.scene_at(document, n / fps) or {}).get("id") == table[n][-1] for n in range(owners.total))


@pytest.mark.parametrize("case", range(20))
def test_the_frame_server_s_spans_never_fire_on_what_the_page_asks_on_a_frame_s_centre(case):
    """The frame server's rule (``FrameServer._insert``: a layer asked at or past its span's end, or partly shown inside
    it) over every layer the page asks on a frame's centre, on the same 20 documents: the page asks an insert's layer
    only on frames its scene owns, and whole wherever the span says - off the centre it is drawn, never judged
    (``FrameServer.shutter``)."""
    document = _random_document(6433 + case)
    fps, scenes = float(document["fps"]), document["scenes"]
    spans = timeline.whole_frames(document)
    for frame in range(document["frames"]):
        t = frame / fps
        for index, scene in enumerate(scenes):
            if not (t >= scene["start"] and (t < scene["end"] or index == len(scenes) - 1)):
                continue
            payload = scene["inserts"][0]
            first, end = spans[payload["src"]]
            assert frame < end
            if frame >= first:
                assert timeline.insert_visibility(payload, t - scene["start"]) >= 1.0


def test_the_table_s_runs_are_contiguous_and_cover_every_frame():
    document = _random_document(6433)
    owners = FrameOwners.of(document)
    assert owners.frames_of(0).start == 0 and owners.frames_of(len(owners.ids) - 1).stop == owners.total
    assert all(owners.scenes_on(n) for n in range(owners.total)) and owners.owner(owners.total) is None
