"""A screen insert's colours held to its source, its light, its crops opened and the resolution its numbers come from.

On synthetic footage, no provider, no browser: a dark product UI inserted
into a blue-lit shot whose screen catches a window's glare, composed frame
by frame through ``screen_insert.compose`` - the render's own function - and
read back as the render's findings and per-insert report. What is proven:

* the colour gate (``screen_insert.colour_fidelity``): the insert as
  composited against its source drawn into the same pixels fails
  ``insert_colour_shift`` at ``light: 0.6`` (its blacks lifted) and passes at
  ``light: 0.15``, the black level within the threshold; a cast on a grey
  source is named ("tinted blue"), a coloured source's turned hue measured;
* the light is light (``ambient_light``, ``lay_light``): a screen blend of
  the footage's low frequencies above the surface's own level, the same on
  every channel - no cast of the room's blue or of a key's green, and a
  surface lit evenly lays none; ``light`` is an overlay field defaulting to
  0.15, carried to the page, the range workers and the editor's builds;
* every crop of a preview is attached and named by the conversation's file
  (the id ``read_file_content`` opens), its findings' frames linked so;
  ``save_video_frame`` cuts a region of any moment at 100 %;
* an ultra render's insert metrics are measured, and its crops cut, on its
  2160p frames, and its answer says so.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import compositor, insert_checks, render_checks, screen_insert, timeline
from app.services.video_studio.compositor_page import ORIGIN

W, H = 960, 540
#: The screen's true corners (pixels) in the shot: a monitor seen a little from the side.
TRUE = np.array([(285, 112), (675, 128), (668, 420), (292, 405)], dtype=float)
QUAD = tuple((float(x) / W, float(y) / H) for x, y in TRUE)
FPS = 30.0
FRAMES = 4
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
MODEL_GREEN = (32, 255, 30)


def _glare() -> np.ndarray:
    """A window's glare falling across the screen: a broad diagonal band, 0-1."""
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    along = (xs - 480.0) * 0.8 + (ys - 270.0) * 0.6
    return np.exp(-0.5 * (along / 110.0) ** 2)


def _blue_lit_shot(seed: int, *, keyed: bool = False, glare: bool = True) -> np.ndarray:
    """A room lit blue, a dark bezel, a dark screen (or a green one) catching the blue glare, the encoder's noise."""
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + np.array([20, 35, 80])).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    centre = TRUE.mean(axis=0)
    draw.polygon([tuple(p) for p in centre + (TRUE - centre) * 1.05], fill=(10, 12, 20))
    draw.polygon([tuple(p) for p in TRUE], fill=MODEL_GREEN if keyed else (28, 36, 70))
    frame = np.asarray(picture).astype(np.float32)
    if glare:
        mask_image = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask_image).polygon([tuple(p) for p in TRUE], fill=255)
        mask = np.asarray(mask_image) > 0
        band = _glare()[..., None]
        # A dark screen takes the glare as added blue light; a green one is lit brighter where it falls.
        lit = frame * (0.75 + 0.5 * band) if keyed else frame + band * np.array([60.0, 90.0, 150.0], dtype=np.float32)
        frame[mask] = lit[mask]
    frame += rng.normal(0.0, 3.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _dark_ui(width: int = 1310, height: int = 1000) -> Image.Image:
    """A product dashboard: light text on a dark ground, denser than the quad, of its proportions."""
    picture = Image.new("RGB", (width, height), (22, 26, 34))
    draw = ImageDraw.Draw(picture)
    rng = np.random.default_rng(5)
    for row in range(40, height - 40, 28):
        x = 60
        while x < width - 80:
            w = int(rng.integers(20, 120))
            draw.rectangle((x, row, x + w, row + 10), fill=(200, 205, 215))
            x += w + 14
    return picture


def _insert(light: float, *, keyed: bool = False) -> screen_insert.InsertSource:
    return screen_insert.InsertSource(
        "p.png",
        "image",
        QUAD,
        light=light,
        key=screen_insert.KeySpec("green") if keyed else None,
        footage=screen_insert.Footage("f", "video", FPS, "clip/s1"),
        label=LABEL,
    )


def _composed(light: float, *, keyed: bool = False) -> tuple[dict, list, dict]:
    """Every frame composed as the frame server composes it; the audits, the findings and the insert's report."""
    frames = [_blue_lit_shot(10 + n, keyed=keyed) for n in range(FRAMES)]
    insert = _insert(light, keyed=keyed)
    surface = None
    if keyed:
        insert = screen_insert.keyed_on(insert, frames[0])
    else:
        surface = screen_insert.Surface.of(frames[0], [tuple(p) for p in TRUE])
    state = screen_insert.Matting()
    audits: dict = {"frames": {}, "evidence": {}}
    picture = _dark_ui()
    for n, frame in enumerate(frames):
        ahead = [(n + k, frames[n + k]) for k in (1, 2) if n + k < len(frames)]
        layer = screen_insert.compose(insert, picture, (W, H), n, footage=frame, surface=surface, state=state, footage_ahead=ahead)
        assert layer is not None
        audits["frames"][str(n)] = layer.audit.as_dict()
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, FRAMES / FPS, light=light, keyed=keyed)}
    found = insert_checks.findings({KEY: audits}, placed, fps=FPS)
    [report] = insert_checks.reports({KEY: audits}, placed, fps=FPS)
    return audits, found, report


# ── The colour gate and the light ─────────────────────────────────────────


def test_a_dark_ui_in_a_blue_lit_shot_fails_the_colour_gate_at_light_0_6_and_passes_at_0_15():
    _audits, found, report = _composed(0.6)
    kinds = {f.kind: f for f, _proof in found}
    assert render_checks.INSERT_COLOUR_SHIFT in kinds, [f.message for f, _p in found]
    finding = kinds[render_checks.INSERT_COLOUR_SHIFT]
    assert finding.severity == render_checks.FAIL and finding.scene_id == "s1"
    assert finding.message.startswith("insert colour shift at t=0:00.") and f"{LABEL} is drawn with its blacks lifted " in finding.message
    structured = {"action", "excluded"}
    assert all(type(value) in (float, int, str, type(None)) for name, value in finding.data.items() if name not in structured), finding.data
    assert finding.data["gate"] == "colour_shift" and finding.data["action"]["kind"] == insert_checks.LOWER_LIGHT
    assert "lower its light (light: 0.6 now; 0.15 is the default, 0 draws the source as it is)" in finding.message
    assert finding.data["light"] == 0.6 and finding.data["black_lift"] > screen_insert.MAX_BLACK_LIFT
    assert finding.data["frames"] == FRAMES
    assert report["colour"]["black_lift"] > screen_insert.MAX_BLACK_LIFT and report["colour"]["frames_failed"] == FRAMES
    # The whole render fails on it, and the gate refuses the next one of the revision.
    checks = render_checks.Checks(findings=[finding])
    assert checks.failed and render_checks.open_findings(checks.as_dict(), same_quality=False)
    assert render_checks.INSERT_COLOUR_SHIFT in render_checks.PICTURE_KINDS

    _audits, found, report = _composed(0.15)
    assert render_checks.INSERT_COLOUR_SHIFT not in {f.kind for f, _p in found}, [f.message for f, _p in found]
    colour = report["colour"]
    # The light plays - the glare is there to see - and the black level stays within the threshold.
    assert 0.0 < colour["black_lift"] <= screen_insert.MAX_BLACK_LIFT
    assert colour["light"] == 0.15 and colour["frames_failed"] == 0
    # A dark UI has no hue of its own: judged by its cast, which the light never gives it.
    assert colour["hue_rotation"] is None and colour["cast"] is not None and colour["cast"] < 1.0
    assert colour["contrast"] is not None and colour["contrast"] > 0.9
    # The numbers the light is tuned on, in the answer's Insert line.
    report["crops"] = []
    [line] = render_checks.insert_lines(render_checks.Checks(inserts=[report]).as_dict())
    assert "colour at light 0.15: luminance +" in line and "blacks +" in line and "measured on 960x540 frames" in line

    _audits, found, report = _composed(0.0)
    assert abs(report["colour"]["black_lift"]) < 0.002, "at light 0 the source is drawn as it is"


def test_the_light_casts_no_colour_and_an_evenly_lit_surface_lays_none():
    # A green key under the glare at a strong light: the light is the surface's luma, never its green.
    _audits, found, report = _composed(0.6, keyed=True)
    assert render_checks.INSERT_COLOUR_SHIFT not in {f.kind for f, _p in found}
    assert report["colour"]["cast"] < 1.0 and report["colour"]["black_lift"] > 0.0
    # A surface lit evenly - whatever its colour and its brightness - lays no light over the insert.
    for colour in ((28, 36, 70), MODEL_GREEN, (200, 200, 210)):
        region = np.empty((200, 300, 3), dtype=np.float32)
        region[...] = colour
        luma = region @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        quad_px = np.array([(20, 20), (280, 20), (280, 180), (20, 180)], dtype=float)
        light = screen_insert.ambient_light(luma, np.ones((200, 300), dtype=np.float32), quad_px)
        assert light is not None and float(light.max()) < 0.5, colour
    # Laid over, light is the same on every channel and never past white.
    shown = np.array([[[22.0, 26.0, 34.0], [250.0, 250.0, 250.0]]], dtype=np.float32)
    lit = screen_insert.lay_light(shown, np.array([[100.0, 100.0]], dtype=np.float32), 0.5)
    assert np.allclose(lit[0, 0] - shown[0, 0], (255.0 - shown[0, 0]) * 50.0 / 255.0)
    assert float(lit.max()) <= 255.0


def test_the_colour_measure_names_a_tint_a_turned_hue_and_lifted_blacks():
    rng = np.random.default_rng(7)
    judged = np.ones((60, 80), dtype=bool)
    grey = np.repeat(rng.uniform(20.0, 220.0, (60, 80, 1)), 3, axis=2).astype(np.float32)
    # A grey tinted blue and its blacks lifted: a cast named, the lift measured.
    tinted = np.clip(grey * 0.8 + np.array([10.0, 30.0, 70.0], dtype=np.float32), 0, 255)
    measured = screen_insert.colour_fidelity(tinted, grey, judged)
    assert measured["hue_rotation"] is None and measured["cast"] > screen_insert.MAX_CAST
    assert screen_insert.hue_name(measured["cast_hue"]) == "blue"
    assert measured["black_lift"] > screen_insert.MAX_BLACK_LIFT and measured["contrast"] < 0.9
    assert screen_insert.colour_badness(measured["black_lift"], measured["hue_rotation"], measured["cast"]) > 1.0
    # A coloured source whose hue turned 12 degrees: measured on its hue, its saturation kept.
    angle = np.radians(12.0)
    ycc = np.stack([np.full((60, 80), 110.0), np.full((60, 80), 40.0), np.full((60, 80), -20.0)], axis=-1)
    turned = ycc.copy()
    turned[..., 1] = ycc[..., 1] * np.cos(angle) - ycc[..., 2] * np.sin(angle)
    turned[..., 2] = ycc[..., 1] * np.sin(angle) + ycc[..., 2] * np.cos(angle)

    def rgb(values: np.ndarray) -> np.ndarray:
        y, cb, cr = values[..., 0], values[..., 1], values[..., 2]
        return np.stack([y + 1.402 * cr, y - 0.344136 * cb - 0.714136 * cr, y + 1.772 * cb], axis=-1).astype(np.float32)

    measured = screen_insert.colour_fidelity(rgb(turned), rgb(ycc), judged)
    assert measured["cast"] is None and measured["hue_rotation"] == pytest.approx(12.0, abs=0.5)
    assert measured["saturation"] == pytest.approx(1.0, abs=0.02) and abs(measured["black_lift"]) < 0.005
    # Too little judged: nothing measured.
    assert screen_insert.colour_fidelity(grey, grey, np.zeros((60, 80), dtype=bool)) is None


def test_a_tinted_insert_s_finding_reads_tinted_blue_with_its_blacks_lifted():
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 1.0, 3.0, light=0.15)}
    frames = {
        str(n): {"black_lift": 0.18, "luma_shift": 0.12, "cast": 9.0, "cast_hue": 228.0, "contrast": 0.71, "hue_rotation": None}
        for n in range(40, 60)
    }
    [(finding, _proof)] = insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, placed, fps=30)
    assert finding.kind == render_checks.INSERT_COLOUR_SHIFT
    assert (
        f"{LABEL} is drawn tinted blue (a cast of 9 grey levels on a source with no hue of its own), with its blacks lifted 18% "
        "against its source (20 frames past blacks lifted 5%, a hue turned 5 degrees or a cast of 6 grey levels; "
        "luminance +12.0%, blacks +18.0%, cast 9.0 grey levels (blue), contrast 0.71x)"
    ) in finding.message
    # One action: its light, the one setting the fixtures show moving lifted blacks (0.6 fails, 0.15 passes).
    assert "lower its light (light: 0.15 now" in finding.message and finding.data["action"]["kind"] == insert_checks.LOWER_LIGHT


def test_light_is_a_screen_insert_field_defaulting_to_0_15_carried_to_the_page_the_ranges_and_the_builds():
    from app.services.video_studio import insert_builds, render_ranges
    from app.services.video_studio.spec import INSERT_DEFAULT_LIGHT, Overlay

    overlay = Overlay.model_validate({"type": "screen_insert", "source": {"asset": "logo"}, "quad": [list(p) for p in QUAD]})
    assert overlay.light is None and timeline.insert_light(overlay) == INSERT_DEFAULT_LIGHT == 0.15
    assert timeline.insert_light(overlay.model_copy(update={"light": 0.0})) == 0.0
    with pytest.raises(ValueError):
        Overlay.model_validate({"type": "screen_insert", "source": {"asset": "logo"}, "quad": [list(p) for p in QUAD], "light": 1.5})
    # A text overlay takes no light: the field is dropped with the insert's other fields.
    assert "light" not in Overlay.model_validate({"type": "callout", "text": "x", "light": 0.4}).model_dump(exclude_none=True)
    # Carried to a range's worker with the insert, and read back.
    lit = dataclasses.replace(_insert(0.4), box=tuple(screen_insert.grown_box(QUAD, W / H)))
    published = json.loads(json.dumps(render_ranges._manifest(compositor.CompositorAssets(inserts={KEY: lit}), lambda path: path), default=str))
    assert published["inserts"][KEY]["light"] == 0.4
    # The editor's build of the insert is drawn again when its light changes.
    build = insert_builds.InsertBuild(KEY, "s1", "f", "s", "image", QUAD, 0.0, 2.0, ())
    assert build.light == 0.15 and build.digest() != dataclasses.replace(build, light=0.4).digest()


# ── The crops, attached and opened ────────────────────────────────────────


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, *_args, **_kwargs):
        return SimpleNamespace(id=uuid.UUID(int=1), email="maker@example.com", role="user")

    async def commit(self):
        return None


def _conversation_files(monkeypatch) -> dict[str, list]:
    """``_file_refs`` as the conversation sees it: each file shown through a copy of its own, attached or linked."""
    from app.services.builtin_tools import video_studio as tools

    seen: dict[str, list] = {"attached": [], "linked": []}

    async def file_refs(file_ids, *, conversation_id=None, user_id="", shown=None, show=True, run_copies=None):
        pictures = tools._Pictures()
        for fid in file_ids:
            if not fid:
                continue
            copy = f"conv-{fid}"
            pictures.ids.append(copy)
            pictures.copies[str(fid)] = copy
            if show:
                pictures.refs.append(SimpleNamespace(file_id=copy))
            seen["attached" if show else "linked"].append(copy)
        return pictures

    monkeypatch.setattr(tools, "_file_refs", file_refs)
    monkeypatch.setattr(tools, "async_session", lambda: _Session())
    return seen


def _checks(size: tuple[int, int]) -> tuple[dict, list[str], str]:
    crop_ids = [str(uuid.uuid4()) for _ in range(3)]
    proof_id = str(uuid.uuid4())
    report = {
        "key": KEY,
        "label": LABEL,
        "scene_id": "s1",
        "frames": 30,
        "refined": None,
        "seed_error": 0.001,
        "alignment_max": 0.002,
        "residual_key": None,
        "flicker_frames": 0,
        "sharpness": 0.92,
        "halo": None,
        "colour": {"light": 0.15, "luma_shift": 0.01, "black_lift": 0.02, "hue_rotation": None, "cast": 0.3, "cast_hue": 30.0, "saturation": None, "contrast": 0.97, "frames_failed": 0},
        "measured_at": list(size),
        "cropped_at": list(size),
        "crops": [
            {"frame": n, "at": n / 30, "word": word, "box": [0.3, 0.2, 0.7, 0.78], "file_id": crop_id}
            for n, word, crop_id in zip((7, 15, 23), ("first", "middle", "last"), crop_ids, strict=True)
        ],
    }
    finding = render_checks.Finding(render_checks.INSERT_HALO, 0.5, 0.8, "s1", "insert halo at t=0:00.50", data={"frame_file_id": proof_id})
    return render_checks.Checks(inserts=[report], findings=[finding]).as_dict(), crop_ids, proof_id


def _tools(monkeypatch, row: SimpleNamespace) -> tuple:
    from app.services.builtin_tools.video_studio import build_video_studio_tools
    from app.services.video_studio import jobs, projects

    project = SimpleNamespace(id=uuid.uuid4(), title="Booth film", revision=3)

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def started(*_args, **_kwargs):
        return SimpleNamespace(
            render=SimpleNamespace(id=row.id),
            superseded=[],
            reused=False,
            relaunched=False,
            preflight=None,
            notes=[],
            advisories=[],
        )

    async def woken(*_args, **_kwargs):
        return None

    async def waited(*_args, **_kwargs):
        return [row]

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "start_scene_render", started)
    monkeypatch.setattr(projects, "start_render", started)
    monkeypatch.setattr(jobs, "wake", woken)
    monkeypatch.setattr(jobs, "wait_for", waited)
    return build_video_studio_tools(user_id=str(uuid.UUID(int=1)), conversation_id=str(uuid.uuid4())), project


def _row(checks: dict, size: tuple[int, int], *, scene_id: str | None) -> SimpleNamespace:
    from app.models.video_studio import JOB_READY

    return SimpleNamespace(
        id=uuid.uuid4(),
        scene_id=scene_id,
        status=JOB_READY,
        superseded_by_id=None,
        frame_file_ids=[str(uuid.uuid4()) for _ in range(3)] if scene_id else None,
        file_id=str(uuid.uuid4()),
        poster_file_id=str(uuid.uuid4()),
        checks=checks,
        width=size[0],
        height=size[1],
        fps=30,
        duration_ms=4000,
        size_bytes=12_000_000,
        notes=None,
        gate_override=None,
        quality="ultra",
    )


def test_every_crop_of_a_preview_is_attached_and_named_by_the_id_that_opens(monkeypatch):
    from app.tools.base import ToolCall

    seen = _conversation_files(monkeypatch)
    checks, crop_ids, proof_id = _checks((1280, 720))
    row = _row(checks, (1280, 720), scene_id="s1")
    (_defs, executor), project = _tools(monkeypatch, row)
    result = asyncio.run(executor.execute_tool(ToolCall(id="p1", name="render_video_scene", arguments={"video_id": str(project.id), "scene": "s1"})))
    assert not result.is_error, result.content
    attached = [ref.file_id for ref in result.files or []]
    # The scene's three frames, every crop of every insert, then the frame each finding shows: all attached, as the
    # conversation's own files (one only linked was refused by read_file_content).
    assert attached == [f"conv-{fid}" for fid in [*row.frame_file_ids, *crop_ids, proof_id]]
    assert not seen["linked"]
    outcome = json.loads(result.content.split("\n", 1)[0])
    [report] = outcome["checks"]["inserts"]
    assert [crop["file_id"] for crop in report["crops"]] == [f"conv-{fid}" for fid in crop_ids]
    assert all(crop["file_id"] in attached for crop in report["crops"]), "a crop the answer names that read_file_content refuses"
    # The frame a finding shows by the same id; no id of the render's own is ever given. Its findings are the
    # answer's own, never a second list under the checks.
    [finding] = outcome["findings"]
    assert finding["frame_file_id"] == f"conv-{proof_id}" and outcome["status"] == "blocked"
    assert "findings" not in outcome["checks"]
    assert "The frame each finding shows is attached too" in result.content
    for own in (*crop_ids, proof_id):
        assert own not in result.content.replace(f"conv-{own}", "")
    # The answer's Insert line names the same ids, its colours at its light and the draft's resolution.
    assert f"first frame file `conv-{crop_ids[0]}`" in result.content and "100% crops of the 1280x720 frame" in result.content
    assert "colour at light 0.15: luminance +1.0%, blacks +2.0%" in result.content
    assert "measured on 1280x720 frames" in result.content
    assert "a render measures and crops them again on its own frames (3840x2160 for ultra)" in result.content
    assert "its region box [0.3, 0.2, 0.7, 0.78] (save_video_frame region" in result.content
    # The stored checks keep the render's own ids: the retention deletes those.
    assert render_checks.insert_crop_ids(row.checks) == crop_ids


def test_save_video_frame_cuts_a_region_of_any_moment_at_100_percent(monkeypatch):
    from app.llm.base import FileRef
    from app.services.builtin_tools import _shared
    from app.services.video_studio import clips
    from app.tools.base import ToolCall

    _conversation_files(monkeypatch)
    frame = Image.new("RGB", (3840, 2160), (10, 20, 30))
    ImageDraw.Draw(frame).rectangle((960, 540, 2879, 1079), fill=(200, 40, 40))
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG")
    asked: dict = {}

    async def resolve(*_args, **_kwargs):
        return {"kind": "file", "name": "booth-ultra.mp4", "file_id": "f", "at_seconds": 2.0}

    async def picture(_source, _work, *, max_side):
        asked["max_side"] = max_side
        return buffer.getvalue()

    async def persist(**kwargs):
        asked.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(clips, "resolve_frame", resolve)
    monkeypatch.setattr(clips, "frame_picture", picture)
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    monkeypatch.setattr(FileRef, "from_attachment", staticmethod(lambda row: row))
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    _defs, executor = build_video_studio_tools(user_id=str(uuid.UUID(int=1)), conversation_id=str(uuid.uuid4()))
    call = ToolCall(id="f1", name="save_video_frame", arguments={"file_id": str(uuid.uuid4()), "at_seconds": 2.0, "region": {"box": [0.25, 0.25, 0.75, 0.5]}})
    result = asyncio.run(executor.execute_tool(call))
    assert not result.is_error, result.content
    # Cut from the frame at its own size - never scaled - and at 100 %: the region's own pixels.
    assert asked["max_side"] == clips.REGION_FRAME_MAX_SIDE
    cut = Image.open(io.BytesIO(asked["raw_bytes"]))
    assert cut.size == (1920, 540) and cut.getpixel((10, 10)) == (200, 40, 40)
    assert "cut at 100% (1920x540 px of the 3840x2160 frame)" in result.content and asked["filename"].endswith(" - region.png")
    # A quad is cut by its bounding box; a region that encloses nothing is refused before anything is read.
    assert clips.frame_region({"quad": [[0.3, 0.2], [0.7, 0.22], [0.69, 0.7], [0.31, 0.68]]}) == (0.3, 0.2, 0.7, 0.7)
    for wrong in ({"box": [0.5, 0.5, 0.4, 0.6]}, {"box": [0.1, 0.1]}, {"box": [0, 0, 1, 1], "quad": [[0, 0]] * 4}, "box"):
        with pytest.raises(clips.ClipError):
            clips.frame_region(wrong)
    refused = asyncio.run(executor.execute_tool(ToolCall(id="f2", name="save_video_frame", arguments={"file_id": "x", "region": {"box": [0.5, 0.5, 0.4, 0.6]}})))
    assert refused.is_error and "region encloses nothing" in refused.content


# ── The resolution the numbers come from ──────────────────────────────────


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.status: object = None
        self.body = b""

    async def fulfill(self, status: int, body: bytes = b"", content_type: str | None = None, headers: dict | None = None) -> None:
        self.status, self.body = status, body

    async def abort(self, reason: str) -> None:
        self.status = reason


def test_an_ultra_render_measures_and_crops_its_inserts_on_2160p_frames(tmp_path):
    """The frame server draws and audits an insert at the size the page asks - the host's in device pixels, 3840x2160
    for an ultra render (a 1920x1080 stage at a device scale of 2) - and cuts its crops from the 2160p frame captured."""
    from app.services.video_studio.compositor_page import PAGE_JS

    # The page asks at its host's size in device pixels: an ultra render's pages run at a device scale of 2.
    assert "Math.round(w * window.devicePixelRatio * zoom)" in PAGE_JS
    still = tmp_path / "footage.png"
    Image.fromarray(_blue_lit_shot(3)).resize((1920, 1080), Image.Resampling.BILINEAR).save(still)
    source = tmp_path / "ui.png"
    _dark_ui().save(source)
    insert = screen_insert.InsertSource(str(source), "image", QUAD, footage=screen_insert.Footage(str(still), "image"), label=LABEL)
    segments = tmp_path / compositor.SEGMENTS_DIR
    server = compositor.FrameServer(
        compositor.CompositorAssets(inserts={KEY: insert}),
        "<html></html>",
        evidence_dir=segments / compositor.EVIDENCE_DIR,
        crops={KEY: [12]},
    )
    route = _Route(f"{ORIGIN}/{KEY}/0.png?w=3840&h=2160&n=12")
    asyncio.run(server.handle(route))
    assert route.status == 200
    audit = server.audits[KEY][12]
    assert audit.size == [3840, 2160] and audit.sharpness is not None and audit.black_lift is not None
    # The crop of the captured 2160p frame: the insert's box, at the frame's own pixels.
    box = screen_insert.bbox(screen_insert.grown_box(QUAD, W / H), 1920, 1080)
    rect = [box[0], box[1], box[2] - box[0], box[3] - box[1]]
    shot = io.BytesIO()
    Image.new("RGB", (3840, 2160), (12, 14, 20)).save(shot, format="PNG")
    asyncio.run(server.crop(12, shot.getvalue(), {KEY: rect}, (1920, 1080)))
    taken = server.take_audits(0, 30)
    assert taken[KEY]["frames"]["12"]["size"] == [3840, 2160] and taken[KEY]["crop_frame"] == [3840, 2160]
    (segments / (Path(compositor.segment_name(0, 30)).stem + compositor.AUDIT_SUFFIX)).write_text(json.dumps(taken), encoding="utf-8")
    merged = compositor.segment_audits(segments, [(0, 30)])
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 1.0, light=insert.light)}
    [report] = insert_checks.reports(merged, placed, fps=30)
    assert report["measured_at"] == [3840, 2160] and report["cropped_at"] == [3840, 2160]
    [crop] = report["crops"]
    assert Image.open(crop["path"]).size == (rect[2] * 2, rect[3] * 2)
    assert crop["box"] == pytest.approx([rect[0] / 1920, rect[1] / 1080, (rect[0] + rect[2]) / 1920, (rect[1] + rect[3]) / 1080], abs=1e-4)
    crop["file_id"] = crop.pop("path")
    [line] = render_checks.insert_lines(render_checks.Checks(inserts=[report]).as_dict())
    assert "measured on 3840x2160 frames" in line and "100% crops of the 3840x2160 frame" in line


def test_an_ultra_render_s_answer_says_its_numbers_and_crops_are_its_own_2160p_frames(monkeypatch):
    from app.tools.base import ToolCall

    _conversation_files(monkeypatch)
    checks, crop_ids, proof = _checks((3840, 2160))
    row = _row(checks, (3840, 2160), scene_id=None)
    (_defs, executor), project = _tools(monkeypatch, row)
    result = asyncio.run(executor.execute_tool(ToolCall(id="r1", name="render_video", arguments={"video_id": str(project.id), "quality": "ultra"})))
    assert not result.is_error, result.content
    # The poster, the crops, then the frame the finding shows: every file the checks name is attached.
    assert [ref.file_id for ref in result.files or []] == [f"conv-{fid}" for fid in [row.poster_file_id, *crop_ids, proof]]
    assert "Its numbers (sharpness, colours) were measured on this render's own 3840x2160 frames, never on a preview's." in result.content
    assert "measured on 3840x2160 frames" in result.content and "100% crops of the 3840x2160 frame" in result.content
    assert f"first frame file `conv-{crop_ids[0]}`" in result.content
