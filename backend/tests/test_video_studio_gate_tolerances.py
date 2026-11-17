"""The compositing gate fails what the audience would see and only that, and the evidence it names opens.

Synthetic fixtures only - no provider, no browser:

* every file a check output names - an insert's crops, the crop of the side holding its residual key, the frame each
  finding shows - is attached to the answer, a preview's and a failed render's: readable in the conversation, as a
  preview's first, middle and last frames are; a crowded render attaches its frames and each insert's worst finding
  and crops first, 12 at most, and names the rest;
* the residual key is read inside the quad and :data:`screen_insert.RESIDUAL_MARGIN` of the frame's width around it: a
  flat green screen with a thin green halo 1% outside it and a cat walking in front passes, the halo taken out of the
  margin and said; the footage's own glow on the screen's rim (filled with the bezel's colour) and its spill on
  the desk fail nothing; a 1% green patch the source itself shows is the source's; under 0.3% of the quad a
  residual key is said, never failed;
* the bezel lock's temporal prior holds a locked-off insert through a bright line drawn across its surface on two
  frames: no alignment finding, the drawn quad never moves; a tracked quad is never held, and a real step is never
  hidden from the gate while the prior settles; a finding needs five frames in a row or 2% of them;
* ``insert_padded``: a logo on transparency (contain, tall quad) and a navy plate with a centred logo (cover) hold no
  letterbox, a white card letterboxed in a dark picture does;
* ``frozen``: an 8 s locked-off take whose only motion is a low-contrast blob covering 5% of the frame, then a 1 s
  crossfade into a still, holds no frozen finding; a still held past the blend does, and so does the frame a subject
  stopped on once encoded by x264;
* a scene preview within those tolerances passes the gate with no finding: the next render is not refused.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter
from scipy.ndimage import gaussian_filter

from app.llm.base import FileRef
from app.services.builtin_tools._shared import resolve_file_ref
from app.services.video_studio import encoder, frame_freeze, insert_checks, render_checks, screen_insert, surface_refine
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec
from app.tools.base import FileContext
from tests.test_video_studio_insert_colour_fidelity import _row, _Session, _tools
from tests.test_video_studio_insert_compositing import _ui

W, H = 960, 540
FPS = 24.0
GREEN = (32, 230, 40)
BEZEL = (28, 28, 32)
#: The monitor's screen in the footage, pixels.
SCREEN = (300, 130, 660, 400)
KEY = "insert/s1.0"
FRAMES = 8


def _quad(x0: float = SCREEN[0], y0: float = SCREEN[1], x1: float = SCREEN[2], y1: float = SCREEN[3]) -> tuple:
    return tuple((x / W, y / H) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def _gate(layers: list) -> tuple[list, dict]:
    """The findings and the report of an insert's composed *layers*, as the render reads its frame server's audits."""
    audits = {KEY: {"frames": {str(n): layer.audit.as_dict() for n, layer in enumerate(layers)}, "evidence": {}}}
    placed = {KEY: insert_checks.Placed(KEY, "s1", "Scene s1: its screen insert 0", 0.0, len(layers) / FPS)}
    found = [finding for finding, _proof in insert_checks.findings(audits, placed, fps=FPS)]
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    return found, report


def _at(layer, x: int, y: int) -> np.ndarray:
    x0, y0, _x1, _y1 = layer.warped.box
    return np.asarray(layer.warped.image, dtype=int)[y - y0, x - x0, :3]


def _green_excess(pixel: np.ndarray) -> int:
    return int(pixel[1]) - max(int(pixel[0]), int(pixel[2]))


# ── 1. The gate's evidence opens ──────────────────────────────────────────


def _files_as_the_conversation_holds_them(monkeypatch) -> list[str]:
    """``_file_refs`` as a conversation holds them: each through a copy of its own, a ``FileRef`` once attached; the
    copies only linked are listed in the list returned."""
    from app.services.builtin_tools import video_studio as tools

    linked: list[str] = []

    async def file_refs(file_ids, *, conversation_id=None, user_id="", shown=None, show=True, run_copies=None):
        pictures = tools._Pictures()
        for fid in dict.fromkeys(str(f) for f in file_ids if f):
            copy = f"conv-{fid}"
            pictures.ids.append(copy)
            pictures.copies[fid] = copy
            if show:
                pictures.refs.append(FileRef(copy, f"{copy}.png", "image/png", 2048, f"conversations/{copy}.png"))
            else:
                linked.append(copy)
        return pictures

    monkeypatch.setattr(tools, "_file_refs", file_refs)
    monkeypatch.setattr(tools, "async_session", lambda: _Session())
    return linked


def _named_checks() -> dict:
    """A render's checks naming six files: an insert's three crops and its residual side, two findings' frames."""
    crops = [{"frame": n, "at": n / 30, "word": word, "file_id": str(uuid.uuid4())} for n, word in ((7, "first"), (15, "middle"), (23, "last"))]
    report = {
        "key": KEY,
        "label": "Scene s1: its screen insert 0",
        "scene_id": "s1",
        "frames": 30,
        "residual_key": 0.004,
        "residual_where": {"edge": {"right": 0.004}},
        "residual_crop": {"frame": 15, "at": 0.5, "side": "right", "file_id": str(uuid.uuid4())},
        "crops": crops,
    }
    findings = [
        render_checks.Finding(render_checks.INSERT_RESIDUAL_KEY, 0.5, 0.8, "s1", "residual key at t=0:00.50", data={"frame_file_id": str(uuid.uuid4())}),
        render_checks.Finding(render_checks.INSERT_HALO, 0.6, 0.9, "s1", "insert halo at t=0:00.60", data={"frame_file_id": str(uuid.uuid4())}),
        render_checks.Finding(render_checks.FROZEN, 0.1, 0.9, "s1", "the picture is frozen for 0.8 s", render_checks.WARN),
    ]
    return render_checks.Checks(inserts=[report], findings=findings).as_dict()


@pytest.mark.parametrize("scene_id", ["s1", None], ids=["scene-preview", "failed-render"])
def test_every_file_the_checks_name_is_attached_and_opens_in_the_conversation(monkeypatch, scene_id):
    from app.models.video_studio import JOB_FAILED
    from app.tools.base import ToolCall

    _files_as_the_conversation_holds_them(monkeypatch)
    checks = _named_checks()
    named = render_checks.frame_file_ids(checks)
    assert len(named) == 6
    row = _row(checks, (1280, 720), scene_id=scene_id)
    if scene_id is None:
        row.status, row.error = JOB_FAILED, "The render failed its picture check."
    (_defs, executor), project = _tools(monkeypatch, row)
    call = (
        ToolCall(id="p1", name="render_video_scene", arguments={"video_id": str(project.id), "scene": "s1"})
        if scene_id
        else ToolCall(id="r1", name="render_video", arguments={"video_id": str(project.id)})
    )
    result = asyncio.run(executor.execute_tool(call))
    # What the turn holds: the agent loop adds every file of an answer to its context; read_file_content reads it.
    context = FileContext()
    for ref in result.files or []:
        context.add(ref)
    shown = [f"conv-{fid}" for fid in named]
    assert all(fid in result.content for fid in shown), "the answer names each by the conversation's copy"
    unreadable = [fid for fid in shown if resolve_file_ref(context, fid) is None]
    assert not unreadable, f"files the checks name that read_file_content refuses: {unreadable}"
    assert "The frame each finding shows is attached too" in result.content


def _crowded_checks(inserts: int = 5) -> dict:
    """A render's checks naming 40 files: five inserts, each with three crops, a residual side and four findings."""
    reports, findings = [], []
    for index in range(inserts):
        key = f"insert/s1.{index}"
        crops = [{"frame": n, "at": n / 30, "word": word, "file_id": f"crop-{index}-{word}"} for n, word in ((7, "first"), (15, "middle"), (23, "last"))]
        reports.append(
            {
                "key": key,
                "label": f"Scene s1: its screen insert {index}",
                "scene_id": "s1",
                "frames": 30,
                "residual_key": 0.004,
                "residual_where": {"edge": {"right": 0.004}},
                "residual_crop": {"frame": 15, "at": 0.5, "side": "right", "file_id": f"strip-{index}"},
                "crops": crops,
            }
        )
        for n, (kind, severity) in enumerate(
            (
                (render_checks.INSERT_HALO, render_checks.WARN),
                (render_checks.INSERT_RESIDUAL_KEY, render_checks.FAIL),
                (render_checks.INSERT_SOFT, render_checks.FAIL),
                (render_checks.INSERT_FLICKER, render_checks.FAIL),
            )
        ):
            data = {"insert": key, "frame_file_id": f"proof-{index}-{n}"}
            findings.append(render_checks.Finding(kind, 0.1 * n + index, None, "s1", f"{kind} at t=0:0{index}", severity, data))
    return render_checks.Checks(inserts=reports, findings=findings).as_dict()


def test_a_crowded_render_attaches_its_worst_evidence_first_and_links_the_rest(monkeypatch):
    from app.tools.base import ToolCall

    linked = _files_as_the_conversation_holds_them(monkeypatch)
    checks = _crowded_checks()
    assert len(render_checks.frame_file_ids(checks)) == 40
    row = _row(checks, (1280, 720), scene_id="s1")
    (_defs, executor), project = _tools(monkeypatch, row)
    result = asyncio.run(executor.execute_tool(ToolCall(id="p1", name="render_video_scene", arguments={"video_id": str(project.id), "scene": "s1"})))
    attached = [ref.file_id for ref in result.files or []]
    # Its three frames, then per insert the frame of its worst finding (the earliest failing one) and its crops.
    assert len(attached) == render_checks.ATTACHED_EVIDENCE == 12
    assert attached == [
        *(f"conv-{fid}" for fid in row.frame_file_ids),
        "conv-proof-0-1", "conv-crop-0-first", "conv-crop-0-middle", "conv-crop-0-last",
        "conv-proof-1-1", "conv-crop-1-first", "conv-crop-1-middle", "conv-crop-1-last",
        "conv-proof-2-1",
    ]  # fmt: skip
    # The rest are linked, each named by the conversation's copy, and the answer says so.
    assert len(linked) == 31 and all(fid in result.content for fid in linked)
    assert "31 more file(s) its checks name are linked rather than attached" in result.content


# ── 2. The residual key: where, how much, and the margin's spill ──────────


def _plate(*, cat: int | None = None, halo: bool = True, seed: int = 0) -> np.ndarray:
    """A noisy wall, a dark 6 px bezel, a flat green screen; a thin green halo 1% of the frame's width outside the
    screen (the footage's own glow, past the bezel); a cat *cat* px along, walking in front of the screen's bottom edge."""
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 95).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    x0, y0, x1, y1 = SCREEN
    if halo:
        reach = round(0.01 * W)
        draw.rectangle((x0 - reach - 1, y0 - reach - 1, x1 + reach + 1, y1 + reach + 1), outline=GREEN, width=2)
    draw.rectangle((x0 - 6, y0 - 6, x1 + 6, y1 + 6), fill=BEZEL)
    draw.rectangle((x0, y0, x1, y1), fill=GREEN)
    if cat is not None:
        fur, x = (92, 72, 58), 360 + cat
        draw.ellipse((x - 70, y1 - 40, x + 70, y1 + 60), fill=fur)
        draw.ellipse((x + 40, y1 - 95, x + 110, y1 - 30), fill=fur)
        draw.polygon([(x + 48, y1 - 80), (x + 58, y1 - 115), (x + 70, y1 - 88)], fill=fur)
        draw.polygon([(x + 82, y1 - 88), (x + 96, y1 - 117), (x + 104, y1 - 80)], fill=fur)
    return np.asarray(picture.filter(ImageFilter.GaussianBlur(1.0))).copy()


def _insert(*, track: object = None) -> InsertSource:
    quad = _quad()
    return InsertSource(
        "p.png",
        "image",
        quad,
        key=KeySpec("green"),
        footage=Footage("f", "video", FPS, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        track=track,
        label="Scene s1: its screen insert 0",
    )


def _static(frames: list[np.ndarray], picture: Image.Image) -> list:
    """A static keyed insert as the frame server draws it: filled once off its first footage frame."""
    insert = screen_insert.keyed_on(_insert(), frames[0])
    fill = screen_insert.static_fill(insert, frames[0], (W, H))
    surface = screen_insert.Surface.of(frames[0], [(x * W, y * H) for x, y in insert.quad])
    state = screen_insert.Matting()
    return [
        screen_insert.compose(insert, picture, (W, H), n, footage=frame, surface=surface, fill=fill, state=state, window_frame=n)
        for n, frame in enumerate(frames)
    ]


def _keyed(frames: list[np.ndarray], picture: Image.Image) -> list:
    """A keyed insert keyed frame by frame (a tracked one's path), its matte pooled over the frames around."""
    insert = screen_insert.keyed_on(_insert(), frames[0])
    state = screen_insert.Matting()
    return [
        screen_insert.compose(
            insert,
            picture,
            (W, H),
            n,
            footage=frame,
            state=state,
            window_frame=n,
            footage_ahead=[(n + k, frames[n + k]) for k in (1, 2) if n + k < len(frames)],
        )
        for n, frame in enumerate(frames)
    ]


def _walk() -> list[np.ndarray]:
    """The plate with nothing in front on its first frame, then a cat walking in across the screen's bottom edge."""
    return [_plate(seed=1)] + [_plate(cat=12 * n, seed=1 + n) for n in range(1, FRAMES)]


@pytest.mark.parametrize("draw", [_static, _keyed], ids=["static", "keyed-every-frame"])
def test_a_halo_past_the_bezel_and_a_cat_in_front_pass_and_the_halo_is_taken_out_and_said(draw):
    layers = draw(_walk(), _ui(400, 300))
    found, report = _gate(layers)
    assert found == [], [f.message for f in found]
    assert not report.get("residual_key")
    # The halo 1% past the screen is the footage's own: taken out of the margin, never green, never a finding.
    x0, y0, x1, y1 = SCREEN
    reach = round(0.01 * W)
    for x, y in ((x0 - reach, 265), (x1 + reach, 265), (480, y0 - reach), (520, y1 + reach)):
        assert _green_excess(_at(layers[-1], x, y)) < 30, (x, y, _at(layers[-1], x, y))
    despilled = report["despilled"]
    assert despilled["share"] > 0 and despilled["frames"] == FRAMES and set(despilled["sides"]) == set(screen_insert.SIDES)
    [line] = render_checks.insert_lines(render_checks.Checks(inserts=[report]).as_dict())
    # What its key took out of the margin is by design, said with the key it comes from - never a finding.
    assert "by design (its key): the footage's key colour taken out of the margin past the quad" in line
    # The cat stays in front: the insert is drawn over none of it.
    assert all(layer.audit.covered is not None and not layer.audit.covers_foreground for layer in layers[1:])


def _glowing(seed: int) -> np.ndarray:
    """The plate with the footage's own screen glow on the rim - a short strip of the key below the screen's bottom
    edge, joined to it - and its spill on the desk farther down."""
    frame = _plate(halo=False, seed=seed).copy()
    x0, _y0, _x1, y1 = SCREEN
    frame[y1 + 1 : y1 + 3, x0 + 140 : x0 + 220] = GREEN
    frame[y1 + 12 : y1 + 15, x0 + 60 : x0 + 300] = (70, 150, 60)
    return frame


@pytest.mark.parametrize("draw", [_static, _keyed], ids=["static", "keyed-every-frame"])
def test_a_glow_on_the_rim_and_a_spill_on_the_desk_fail_nothing(draw):
    frames = [_glowing(seed=4 + n) for n in range(FRAMES)]
    layers = draw(frames, _ui(400, 300))
    found, report = _gate(layers)
    assert found == [], [f.message for f in found]
    # The glow on the rim, within the residual margin, takes the bezel's colour: nothing of it is left, and
    # it is never a grey despill; the spill farther out is taken out.
    assert report["residual_key"] == 0.0
    assert report["despilled"]["sides"].get("bottom", 0.0) > 0
    x0, _y0, _x1, y1 = SCREEN
    assert np.abs(_at(layers[-1], x0 + 180, y1 + 2) - BEZEL).max() <= 8, _at(layers[-1], x0 + 180, y1 + 2)
    assert _green_excess(_at(layers[-1], x0 + 180, y1 + 13)) < 30


@pytest.mark.parametrize("draw", [_static, _keyed], ids=["static", "keyed-every-frame"])
def test_a_one_percent_green_patch_of_the_source_s_own_is_the_source_s(draw):
    picture = _ui(400, 300)
    # 1% of the picture - so of the quad - the key's own green, drawn by the source itself.
    ImageDraw.Draw(picture).rectangle((180, 135, 214, 169), fill=GREEN)
    layers = draw([_plate(seed=2 + n) for n in range(FRAMES)], picture)
    found, report = _gate(layers)
    assert found == [], [f.message for f in found]
    assert report["residual_key"] == 0.0 and 0.008 < report["source_key_hue"] < 0.02
    [line] = render_checks.insert_lines(render_checks.Checks(inserts=[report]).as_dict())
    # Inside the drawn source it is by design.
    assert "by design (its source's own colours): the source's own key hue on " in line
    assert "left out of the residual key and the halo" in line


def _audited(frames: int, **fields: object) -> dict:
    return {KEY: {"frames": {str(n): dict(fields) for n in range(frames)}, "evidence": {}}}


def test_a_residual_key_under_the_gate_s_share_is_said_and_never_fails():
    placed = {KEY: insert_checks.Placed(KEY, "s1", "Scene s1: its screen insert 0", 0.0, 260 / FPS)}
    under = _audited(260, residual_key=0.0025, residual_where={"edge": {"top": 0.0025}})
    assert insert_checks.findings(under, placed, fps=FPS) == []
    [report] = insert_checks.reports(under, placed, fps=FPS)
    assert report["residual_key"] == 0.0025 and report["residual_advisory"]
    [line] = render_checks.insert_lines(render_checks.Checks(inserts=[report]).as_dict())
    assert "residual key 0.25% of the quad" in line and "advisory, under the gate's share" in line
    over = _audited(260, residual_key=0.0035, residual_where={"inside": {"top": 0.0035}})
    [(finding, _proof)] = insert_checks.findings(over, placed, fps=FPS)
    assert finding.kind == render_checks.INSERT_RESIDUAL_KEY and finding.severity == render_checks.FAIL
    assert "within 0.5% of the frame's width of it" in finding.message and "past 0.3%" in finding.message


def _transient(frames: int, past: list[int], share: float) -> dict:
    """An insert drawn on *frames* frames, its residual key at *share* of the quad on the frames *past* alone."""
    audited = _audited(frames, residual_key=0.0)
    for n in past:
        audited[KEY]["frames"][str(n)] = {"residual_key": share, "residual_where": {"inside": {"top": share}}}
    return audited


def test_a_residual_key_on_a_one_or_two_frame_transient_is_an_advisory_and_on_six_in_a_row_a_failure():
    """The residual key holds to the persistence of the alignment (``gate_fails``): a 0.5% residual on 2 frames
    of 260 - fur, motion blur on the rim - passes, its worst frame kept in the report as advisory; on 6 in a row it
    fails."""
    placed = {KEY: insert_checks.Placed(KEY, "s1", "Scene s1: its screen insert 0", 0.0, 260 / FPS)}
    blip = _transient(260, [120, 121], 0.005)
    assert insert_checks.findings(blip, placed, fps=FPS) == []
    [report] = insert_checks.reports(blip, placed, fps=FPS)
    assert (report["residual_key"], report["residual_advisory"], report["residual_past_frames"]) == (0.005, True, 2)
    assert report["verdicts"]["residual_key"] is True
    [line] = render_checks.insert_lines(render_checks.Checks(inserts=[report]).as_dict())
    assert "residual key 0.50% of the quad" in line
    assert "advisory, past the gate's share on 2 frames only, under the 5 it fails on: nothing to fix" in line
    held = _transient(260, list(range(120, 126)), 0.005)
    [(finding, _proof)] = insert_checks.findings(held, placed, fps=FPS)
    assert finding.kind == render_checks.INSERT_RESIDUAL_KEY and finding.severity == render_checks.FAIL
    assert "(6 frames past 0.3%)" in finding.message
    [report] = insert_checks.reports(held, placed, fps=FPS)
    assert report["residual_advisory"] is False and "residual_past_frames" not in report


# ── 3. The bezel lock on a locked-off shot ────────────────────────────────


#: A grey panel on a darker wall, and where a bright line is drawn across it, inside its top edge.
PANEL = (300, 130, 660, 400)
LINE_ROW = PANEL[1] + 12


def _locked_off(*, line: bool) -> np.ndarray:
    picture = Image.new("RGB", (W, H), (60, 60, 64))
    draw = ImageDraw.Draw(picture)
    draw.rectangle(PANEL, fill=(100, 104, 108))
    if line:
        # A text line the model painted on the surface: far brighter than the surface's own edge.
        draw.rectangle((PANEL[0] + 14, LINE_ROW, PANEL[2] - 14, LINE_ROW + 2), fill=(250, 250, 250))
    return np.asarray(picture.filter(ImageFilter.GaussianBlur(0.8))).copy()


def _unkeyed(frames: list[np.ndarray]) -> list:
    quad = _quad(*PANEL)
    insert = InsertSource("p.png", "image", quad, footage=Footage("f", "video", FPS, "clip/s1"), box=tuple(screen_insert.grown_box(quad, W / H)))
    surface = screen_insert.Surface.of(frames[0], [(x * W, y * H) for x, y in quad])
    state = screen_insert.Matting()
    picture = _ui(400, 300)
    return [
        screen_insert.compose(insert, picture, (W, H), n, footage=frame, surface=surface, state=state, window_frame=n)
        for n, frame in enumerate(frames)
    ]


def _drawn(layer) -> np.ndarray:
    return np.array(layer.audit.refined, dtype=float) * [W, H]


def test_a_line_drawn_across_a_locked_off_surface_on_two_frames_never_moves_the_insert(monkeypatch):
    frames = [_locked_off(line=n in (12, 13)) for n in range(30)]
    # Without the prior, the refiner catches the line as the top edge on those two frames: the jump of the report.
    monkeypatch.setattr(surface_refine, "PRIOR_MAX_SHARE", 1.0)
    jumped = _unkeyed(frames)
    moved = max(float(np.abs(_drawn(jumped[n]) - _drawn(jumped[11])).max()) for n in (12, 13))
    assert moved > 0.01 * W, moved
    monkeypatch.undo()
    layers = _unkeyed(frames)
    first = _drawn(layers[0])
    assert max(float(np.abs(_drawn(layer) - first).max()) for layer in layers) < 0.1, "the drawn quad moved"
    assert [n for n, layer in enumerate(layers) if layer.audit.held] == [12, 13]
    # The bezel it rejected is kept for the gate, which reads two frames off it and fails nothing.
    assert all(layers[n].audit.alignment_found > surface_refine.ALIGNMENT_MAX_SHARE for n in (12, 13))
    found, report = _gate(layers)
    assert not [f for f in found if f.kind == render_checks.INSERT_ALIGNMENT], [f.message for f in found]
    assert report["held_frames"] == 2 and report["alignment_frames"] == 2
    assert report["alignment_max"] < surface_refine.ALIGNMENT_MAX_SHARE


def test_a_tracked_quad_is_never_held_its_tracker_s_correction_is_a_real_one():
    refiner = surface_refine.Refiner()
    field = _locked_off(line=False).astype(np.float32).mean(axis=2)
    given = np.array([(x * W, y * H) for x, y in _quad(*PANEL)])
    for _ in range(6):
        refiner.step(field, given, W, prior=False)
    shifted = refiner.step(np.roll(field, 15, axis=1), given, W, prior=False)
    assert not shifted.held and abs(float(np.asarray(shifted.quad)[:, 0].mean() - given[:, 0].mean()) - 15.0) < 1.0


def test_a_real_bezel_step_is_never_hidden_while_the_prior_settles():
    placed = {KEY: insert_checks.Placed(KEY, "s1", "Scene s1: its screen insert 0", 0.0, 260 / FPS)}

    def audited(found: float | None) -> dict:
        frames = {str(n): {"alignment": 0.001, "refined": [[0.3, 0.2], [0.7, 0.2], [0.7, 0.8], [0.3, 0.8]]} for n in range(260)}
        for n in range(100, 105):
            # The prior held the bezel of the frames before: the drawn quad's error reads small, the step's does not.
            frames[str(n)].update({"held": True, "alignment_found": found})
        return {KEY: {"frames": frames, "evidence": {}}}

    [(finding, _proof)] = insert_checks.findings(audited(0.016), placed, fps=FPS)
    assert finding.kind == render_checks.INSERT_ALIGNMENT and finding.data["frames"] == 5 and finding.data["worst"] == 0.016
    assert insert_checks.findings(audited(None), placed, fps=FPS) == []


def test_the_lock_follows_a_bezel_that_really_moved():
    refiner = surface_refine.Refiner()
    still = _locked_off(line=False)
    field = still.astype(np.float32).mean(axis=2)
    given = np.array([(x * W, y * H) for x, y in _quad(*PANEL)])
    for _ in range(6):
        assert not refiner.step(field, given, W).held
    # The panel shifts 15 px for good (a camera's step): held a few frames, then followed.
    shifted = np.roll(field, 15, axis=1)
    held = [refiner.step(shifted, given, W).held for _ in range(surface_refine.PRIOR_SETTLE_FRAMES + 2)]
    assert held[: surface_refine.PRIOR_SETTLE_FRAMES - 1] == [True] * (surface_refine.PRIOR_SETTLE_FRAMES - 1)
    assert held[-1] is False
    assert abs(float(refiner.correction[:, 0].mean()) - 15.0) < 1.0


def test_an_alignment_finding_needs_five_frames_in_a_row():
    assert not surface_refine.alignment_failing([100, 101], 260)
    assert not surface_refine.alignment_failing([10, 90, 200, 250], 260)
    assert surface_refine.alignment_failing([40, 41, 42, 43, 44], 260)
    # Scattered frames are no run: one weak frame at a time never blocks, whatever the insert's length.
    assert not surface_refine.alignment_failing([10, 50, 90, 130, 170, 210], 260)
    assert not surface_refine.alignment_failing([3, 20, 33], 40)
    # An insert drawn on fewer frames than a run fails on all of them - a save's one frame too.
    assert surface_refine.alignment_failing([0, 1, 2], 3) and not surface_refine.alignment_failing([0, 2], 3)
    assert surface_refine.alignment_failing([0], 1)


# ── 4. insert_padded on real pictures ─────────────────────────────────────


def test_a_logo_on_transparency_and_a_plate_with_a_centred_logo_hold_no_letterbox_a_letterboxed_card_does():
    logo = Image.new("RGBA", (600, 300), (0, 0, 0, 0))
    draw = ImageDraw.Draw(logo)
    draw.ellipse((220, 70, 380, 230), fill=(0, 180, 255, 255))
    draw.rectangle((100, 130, 500, 170), fill=(255, 255, 255, 255))
    # (a) A logo on transparency, contain, on a tall quad: its transparent rows are not the picture's.
    assert screen_insert.padded_sides(logo, "contain", 300, 900) is None
    # (b) An opaque navy plate with a centred logo: cover never judged, and the plate is one surface anyway.
    plate = Image.new("RGBA", (800, 450), (20, 30, 70, 255))
    ImageDraw.Draw(plate).ellipse((330, 155, 470, 295), fill=(240, 240, 240, 255))
    assert screen_insert.padded_sides(plate, "cover", 800, 450) is None
    assert screen_insert.padded_sides(plate, "contain", 800, 450) is None
    # (c) A logo on a white card letterboxed in a dark picture: dark bands that are not the card's sides.
    card = Image.new("RGBA", (800, 450), (12, 14, 18, 255))
    draw = ImageDraw.Draw(card)
    draw.rectangle((0, 90, 799, 359), fill=(250, 250, 250, 255))
    draw.ellipse((340, 165, 460, 285), fill=(200, 30, 40, 255))
    assert screen_insert.padded_sides(card, "contain", 800, 450) == "top and bottom"
    # On a transparent canvas, its alpha-zero margin cut away first: the same letterbox.
    canvas = Image.new("RGBA", (900, 600), (0, 0, 0, 0))
    canvas.paste(card, (50, 75))
    assert screen_insert.padded_sides(canvas, "contain", 300, 900) == "top and bottom"


# ── 5. frozen: a locked-off take with a moving subject ────────────────────


#: The analysis copy's size, each frame drawn at twice it and area-averaged, as ffmpeg's area scaling does.
COPY_W, COPY_H, OVER = 192, 108, 2


def _take() -> list[bytes]:
    """8 s at 30 fps of a locked-off wall and a blob covering 5% of the frame, 10 grey levels off it, moving 2 px a
    frame of a 1920 px source; then 1 s crossfading into a still of the wall; then 0.3 s of the still."""
    rng = np.random.default_rng(3)
    size = (COPY_H * OVER, COPY_W * OVER)
    wall = gaussian_filter(np.kron(rng.uniform(40, 90, (size[0] // 12, size[1] // 12)), np.ones((12, 12))), 6)
    ys, xs = np.mgrid[0 : size[0], 0 : size[1]]
    radius = np.sqrt(0.05 * size[0] * size[1] / np.pi)
    step = 2.0 * size[1] / 1920.0

    def copy(picture: np.ndarray) -> np.ndarray:
        return np.clip(np.round(picture.reshape(COPY_H, OVER, COPY_W, OVER).mean(axis=(1, 3))), 0, 255).astype(np.uint8)

    moving = [copy(wall + 10.0 * (((xs - 60 - n * step) ** 2 + (ys - size[0] / 2) ** 2) <= radius**2)) for n in range(240)]
    still = copy(wall)
    fade = [np.clip(np.round((1 - a) * moving[-1] + a * still), 0, 255).astype(np.uint8) for a in np.linspace(0, 1, 30)]
    return [frame.tobytes() for frame in [*moving, *fade, *([still] * 9)]]


DOCUMENT = {
    "duration": 9.3,
    "scenes": [
        {"id": "corridor", "type": "clip", "start": 0.0, "end": 9.0},
        {"id": "still", "type": "image", "motion": "ken_burns", "start": 8.0, "end": 9.3, "transition_in": {"type": "crossfade", "duration": 1.0}},
    ],
}


def _check(monkeypatch, frames: list[bytes]) -> frame_freeze.FrameCheck:
    async def stream(_path, *, width, height, fps, start=0.0):
        assert (width, height, fps, start) == (COPY_W, COPY_H, 30.0, 0.0)
        for frame in frames:
            yield frame

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    return asyncio.run(frame_freeze.check_frames("preview.mp4", document=DOCUMENT, width=1920, height=1080, fps=30))


def test_a_locked_off_take_whose_small_subject_moves_and_its_crossfade_out_are_never_frozen(monkeypatch):
    frames = _take()
    check = _check(monkeypatch, frames)
    assert check.findings == [], [f.message for f in check.findings]
    # Read on the whole frame and over the blend, as before: frozen windows all through the take and the crossfade.
    monkeypatch.setattr(frame_freeze, "SUBJECT_MIN_SHARE", 1.0)
    monkeypatch.setattr(frame_freeze, "BLENDED_TRANSITIONS", frozenset())
    before = _check(monkeypatch, frames)
    assert {f.scene_id for f in before.findings} == {"corridor", "still"}


def test_a_held_frame_and_a_subject_that_stops_are_still_frozen():
    frames = [np.frombuffer(raw, dtype=np.uint8).reshape(COPY_H, COPY_W) for raw in _take()[:60]]
    assert frame_freeze.frozen_runs(frames + [frames[-1]] * 30, 30.0, takes=["corridor"] * 90) == [(59, 90)]
    assert frame_freeze.frozen_runs([frames[0]] * 30, 30.0) == [(0, 30)]
    # A blend is excluded only where the plan blends: a still held past it is frozen, from the blend's end.
    assert frame_freeze.blended_windows(DOCUMENT) == [(8.0, 9.0)]
    longer = {**DOCUMENT, "duration": 10.5, "scenes": [DOCUMENT["scenes"][0], {**DOCUMENT["scenes"][1], "end": 10.5}]}
    [held] = frame_freeze.findings_of_runs([(int(8.2 * 30), int(10.5 * 30))], 30.0, longer)
    assert (held.scene_id, held.at, held.end) == ("still", 9.0, 10.5)


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_on_encoded_footage_a_moving_subject_is_never_frozen_and_the_frame_it_holds_still_is(tmp_path):
    """The subject's pixels are picked because they changed: x264's own output must not keep them moving once held."""
    size, fps = (640, 360), 24
    rng = np.random.default_rng(5)
    wall = gaussian_filter(np.kron(rng.uniform(40, 90, (size[1] // 20, size[0] // 20)), np.ones((20, 20))), 6)
    ys, xs = np.mgrid[0 : size[1], 0 : size[0]]
    radius = np.sqrt(0.05 * size[0] * size[1] / np.pi)

    def frame(n: int) -> Image.Image:
        # A blob 10 levels off a grainy wall, 1 px a frame of this 640 px frame for 2 s; then the frame it stopped on.
        blob = ((xs - 150 - min(n, 48)) ** 2 + (ys - size[1] / 2) ** 2) <= radius**2
        grain = np.random.default_rng(min(n, 48)).normal(0.0, 2.0, wall.shape)
        return Image.fromarray(np.clip(wall + 10.0 * blob + grain, 0, 255).astype(np.uint8)).convert("RGB")

    names = []
    for n in range(96):
        frame(n).save(tmp_path / f"f{n:03d}.png")
        names.append((n / fps, f"f{n:03d}.png"))
    out = str(tmp_path / "take.mp4")
    asyncio.run(
        encoder.frames_to_mezzanine(names, end_time=96 / fps, out_path=out, fps=fps, crf=23, preset="veryfast", workdir=str(tmp_path))
    )
    document = {"duration": 4.0, "scenes": [{"id": "take", "type": "clip", "start": 0.0, "end": 4.0}]}
    check = asyncio.run(frame_freeze.check_frames(out, document=document, width=size[0], height=size[1], fps=fps))
    [held] = check.findings
    assert 2.0 <= held.at <= 2.2 and held.end >= 3.9, (held.at, held.end)


# ── The acceptance: within those tolerances, nothing to fix ───────────────


def test_a_preview_within_the_tolerances_passes_the_gate_and_the_next_render_is_not_refused():
    frames = {}
    for n in range(260):
        # Under 0.3% of residual key on some frames; within 0.5% of the bezel on all but two.
        frames[str(n)] = {
            "alignment": 0.013 if n in (120, 121) else 0.003,
            "refined": [[0.3, 0.2], [0.7, 0.2], [0.7, 0.8], [0.3, 0.8]],
            "residual_key": 0.0028 if 100 <= n < 140 else 0.0,
            "residual_where": {"edge": {"bottom": 0.0028}} if 100 <= n < 140 else None,
            "coverage": 0.99,
            "sharpness": 0.9,
        }
    audits = {KEY: {"frames": frames, "evidence": {}}}
    placed = {KEY: insert_checks.Placed(KEY, "s1", "Scene s1: its screen insert 0", 0.0, 260 / FPS)}
    found = [finding for finding, _proof in insert_checks.findings(audits, placed, fps=FPS)]
    assert found == [], [f.message for f in found]
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    assert report["residual_advisory"] and report["alignment_frames"] == 2
    checks = render_checks.Checks(frames={"status": render_checks.PASSED}, inserts=[report], findings=found).as_dict()
    assert checks["status"] == render_checks.PASSED
    assert render_checks.scene_passed(checks, "s1") and render_checks.picture_findings(checks, "s1") == []
    assert render_checks.open_findings(checks, same_quality=True) == []
    drawn = render_checks.record_seen({}, {"s1": "digest"}, uuid.uuid4(), checks=checks)
    assert drawn["s1"]["passed"] and "picture" not in drawn["s1"]
    assert "advisory" in json.dumps(render_checks.insert_lines(checks))
