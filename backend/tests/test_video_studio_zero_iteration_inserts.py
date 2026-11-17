"""A correctly built scene passes its first preview: what a preview used to discover, known before a frame is drawn.

Synthetic fixtures only - a still with a flat screen, one with a curved screen, one seen at an angle, a 6 s
recording under a 9 s narration, a 12 s insert on an 8 s looped clip. No browser, no database, no storage:

* the bezel a static screen insert lands on is measured on its first footage frame at save (``insert_bezels``):
  a quad left out takes it, a quad within 2% of it is snapped, one further off or a frame with no bezel is refused;
* a curved surface is measured (``surface_refine.Flatness``) and refused at generation; one seen at an angle is flat
  (the perspective warp covers it);
* a recording or a clip shorter than its narration says the speed or the extend_mode that closes it, and an insert
  past its clip's loop seam says where the seam falls (``quality.content_findings``);
* the pre-render lint lists every static fault in one answer (``render_checks.static_reasons``);
* no finding asks the author to type a measured number back into the spec.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.models.video_studio import JOB_READY
from app.services.video_studio import (
    clips,
    insert_bezels,
    insert_checks,
    quality,
    render_checks,
    surface_refine,
    timeline,
)
from app.services.video_studio.spec import VideoSpecError, parse_video_spec, validate_video_spec

CLIP = "00000000-0000-0000-0000-00000000c263"
LOOPED = "00000000-0000-0000-0000-00000000a263"
OFF_CLIP = "00000000-0000-0000-0000-00000000b263"
TAKE = "00000000-0000-0000-0000-00000000d263"
BACKDROP = "00000000-0000-0000-0000-00000000e263"
SHOWN = "00000000-0000-0000-0000-00000000f263"
#: The flat screen of :func:`_still`: (320, 170) to (960, 550) of a 1280x720 frame, 0-1.
SCREEN = ((0.25, 170 / 720), (0.75, 170 / 720), (0.75, 550 / 720), (0.25, 550 / 720))
#: A quad given with a few corners off: what an author reads off a frame.
NUMBERS = re.compile(r"\[\[-?\d")


def _still(shape: str = "flat") -> np.ndarray:
    """A 1280x720 room with a green screen: flat and square-on, curved (its top and bottom edges bowing 7.5%), or seen
    at an angle (its left side 50% taller than its right)."""
    image = Image.new("RGB", (1280, 720), (46, 46, 46))
    draw = ImageDraw.Draw(image)
    if shape == "flat":
        draw.rectangle((300, 150, 980, 570), fill=(18, 18, 20))
        draw.rectangle((320, 170, 959, 549), fill=(0, 177, 64))
    elif shape == "curved":
        top = [(320 + t * 640, 170 + 48 * (1 - (2 * t - 1) ** 2)) for t in np.linspace(0, 1, 60)]
        bottom = [(320 + t * 640, 550 - 48 * (1 - (2 * t - 1) ** 2)) for t in np.linspace(1, 0, 60)]
        draw.polygon(top + bottom, fill=(0, 177, 64))
    else:
        draw.polygon([(320, 150), (960, 220), (960, 500), (320, 570)], fill=(0, 177, 64))
    return np.asarray(image)


def _close(quad, expected, tolerance: float = 0.004) -> bool:
    return all(abs(x - ex) < tolerance and abs(y - ey) < tolerance for (x, y), (ex, ey) in zip(quad, expected, strict=True))


# ── The surface, measured ─────────────────────────────────────────────────


def test_a_flat_screen_is_flat_seen_at_an_angle_or_not_and_a_curved_one_says_how_it_is_not():
    flat = surface_refine.measure_bezel(_still("flat"))
    assert flat is not None and flat.flatness.flat and _close(flat.quad, SCREEN)
    curved = surface_refine.measure_bezel(_still("curved"))
    assert curved is not None and not curved.flatness.flat
    # Its top edge bows 48 px over 640: 7.5% of its length, measured off the points along it - and confirmed by them:
    # a quarter of them on one line AND standing off it by more than 0.5% of the frame's width.
    assert curved.flatness.words().startswith("its top edge bows 7") and 0.06 < curved.flatness.error < 0.09
    assert curved.flatness.inliers[0] < surface_refine.BOW_MAX_INLIERS
    assert curved.flatness.residuals[0] > surface_refine.ALIGNMENT_MAX_SHARE
    # Seen at an angle, a flat screen is flat: the perspective warp covers it, said as a fact, in degrees.
    oblique = surface_refine.measure_bezel(_still("oblique"))
    assert oblique is not None and oblique.flatness.flat and oblique.flatness.error == 0.0
    assert oblique.flatness.angle == pytest.approx(0.5, abs=0.01) and oblique.flatness.angle_words() == "left side 50% taller than right"
    assert oblique.flatness.words() == "seen at 42 degrees" and oblique.flatness.fault() is None
    # The record a shot and a save keep reads back as it was measured.
    again = surface_refine.Bezel.from_dict(oblique.as_dict())
    assert again.flatness.words() == oblique.flatness.words() and again.flatness.flat and again.quad == oblique.quad
    assert again.flatness.inliers == oblique.flatness.inliers and again.flatness.angle == pytest.approx(oblique.flatness.angle, abs=1e-3)
    # An older record - its sides seen at an angle, no bow - reads back flat: by design.
    legacy = {"sides": [640.0, 280.0, 640.0, 420.0], "bows": [0.0002, 0.0, 0.0002, 0.0], "error": 0.5, "flat": False}
    assert surface_refine.Flatness.from_dict(legacy).flat


def test_a_quad_given_is_measured_against_the_bezel_near_it():
    near = surface_refine.measure_bezel(_still(), given=((0.252, 0.238), (0.752, 0.238), (0.752, 0.766), (0.252, 0.766)))
    assert near is not None and near.found_by == "seed" and near.error < insert_bezels.SNAP_MAX_SHARE
    assert _close(near.quad, SCREEN)
    off = surface_refine.measure_bezel(_still(), given=((0.27, 0.25), (0.77, 0.25), (0.77, 0.78), (0.27, 0.78)))
    assert off is not None and off.error > insert_bezels.SNAP_MAX_SHARE


# ── The spec: a static insert's quad is the footage's ─────────────────────


def _insert(**fields) -> dict:
    return {"type": "screen_insert", "source": {"file_id": SHOWN}, "start": 0.0, "duration": 4.0, **fields}


def _clip_spec(*overlays: dict) -> dict:
    return {
        "title": "Booth",
        "freestyle": True,
        "scenes": [{"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": list(overlays)}],
    }


def test_a_static_insert_without_a_quad_is_left_to_its_footage_and_a_tracked_one_needs_its_own():
    spec = parse_video_spec(validate_video_spec(_clip_spec(_insert())))
    overlay = spec.scenes[0].overlays[0]
    assert overlay.quad is None and overlay.auto_quad and timeline.auto_quads(spec) == [("s1", 0)]
    with pytest.raises(VideoSpecError, match="a tracked screen insert needs its quad"):
        validate_video_spec(_clip_spec(_insert(track=True)))
    with pytest.raises(VideoSpecError, match="needs its source"):
        validate_video_spec(_clip_spec({"type": "screen_insert", "start": 0.0, "duration": 4.0}))


def _measured(given=None, bezel=None, *, scene_id: str = "s1", footage=("clip", CLIP)) -> insert_bezels.Measured:
    return insert_bezels.Measured(scene_id, 0, footage, "file-1", 12, 0.5, given, bezel, "digest")


def _bezel(shape: str = "flat", given=None) -> surface_refine.Bezel:
    found = surface_refine.measure_bezel(_still(shape), given=given)
    assert found is not None
    return found


def test_the_save_writes_the_bezel_of_a_quad_left_out_and_snaps_one_given_close_to_it():
    authored = validate_video_spec(_clip_spec(_insert()))
    resolved = insert_bezels.resolve(authored, [_measured(None, _bezel())])
    assert not resolved.problems and resolved.changed
    written = resolved.authored["scenes"][0]["overlays"][0]["quad"]
    assert _close(written, SCREEN) and "takes the bezel measured on frame 12" in resolved.notes[0]
    # Stored, it validates and is drawn as a quad given: the render's bezel lock starts on the bezel.
    assert not parse_video_spec(resolved.authored).scenes[0].overlays[0].auto_quad
    # A quad 0.3% off is snapped to the bezel, said; saved again, it is the bezel and nothing is written.
    close = ((0.252, 0.238), (0.752, 0.238), (0.752, 0.766), (0.252, 0.766))
    snapped = insert_bezels.resolve(validate_video_spec(_clip_spec(_insert(quad=close))), [_measured(close, _bezel(given=close))])
    assert snapped.changed and "snapped to it" in snapped.notes[0]
    stored = snapped.authored["scenes"][0]["overlays"][0]["quad"]
    again = _bezel(given=tuple(map(tuple, stored)))
    assert not insert_bezels.resolve(snapped.authored, [_measured(tuple(map(tuple, stored)), again)]).changed


def test_the_save_refuses_a_frame_with_no_bezel_and_a_quad_far_off_without_a_number_to_type_back():
    authored = validate_video_spec(_clip_spec(_insert()))
    empty = insert_bezels.resolve(authored, [_measured(None, None)])
    [problem] = empty.problems
    assert problem.path == "scenes.0.overlays.0.quad" and "no bezel on frame 12" in problem.message
    assert "the frame is attached" in problem.message and empty.refused == [_measured(None, None)]
    far = ((0.27, 0.25), (0.77, 0.25), (0.77, 0.78), (0.27, 0.78))
    refused = insert_bezels.resolve(validate_video_spec(_clip_spec(_insert(quad=far))), [_measured(far, _bezel(given=far))])
    [problem] = refused.problems
    assert "% of the frame's width off the bezel" in problem.message and "leave its quad out" in problem.message
    assert not NUMBERS.search(problem.message)
    # Raised by the save, it is a spec error naming the field, and carries the frames to attach.
    error = insert_bezels.BezelRefusedError(refused.problems, refused.refused)
    assert isinstance(error, VideoSpecError) and "scenes.0.overlays.0.quad" in str(error) and error.measured


def test_a_quad_drawn_inside_the_surface_on_purpose_stands():
    inset = ((0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6))
    bezel = _bezel(given=inset)
    assert bezel.found_by == "frame" and bezel.error > insert_bezels.SNAP_MAX_SHARE
    kept = insert_bezels.resolve(validate_video_spec(_clip_spec(_insert(quad=inset))), [_measured(inset, bezel)])
    assert not kept.problems and not kept.changed


def test_the_measure_reads_the_first_footage_frame_the_plan_places_the_insert_on(monkeypatch, tmp_path):
    """A statement's picture backdrop: the frame read is the picture, no decoder needed."""
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Stage",
                "freestyle": True,
                "scenes": [
                    {
                        "id": "st",
                        "type": "statement",
                        "lines": ["Live"],
                        "backdrop": {"file_id": BACKDROP},
                        "overlays": [_insert()],
                    }
                ],
            }
        )
    )
    kept: dict = {}

    async def footage_of(_db, scene):
        return (("file", BACKDROP), BACKDROP, True)

    async def download(file_id, path):
        Image.fromarray(_still()).save(path, format="PNG")
        return SimpleNamespace(filename="room.png")

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    async def put_file(kind, digest, path, *, suffix=""):
        kept[(kind, digest, suffix)] = path

    from app.services.video_studio import assets, render_cache

    monkeypatch.setattr(insert_bezels, "_footage_of", footage_of)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(render_cache, "put_file", put_file)
    [item] = asyncio.run(insert_bezels.measure(None, spec))
    assert item.scene_id == "st" and item.footage == ("file", BACKDROP) and item.given is None
    assert item.bezel is not None and item.bezel.flatness.flat and _close(item.bezel.quad, SCREEN)
    # Kept by its footage, frame and quad: the next save reads it back without reading the footage.
    monkeypatch.setattr(assets, "download_file", lambda *_a: pytest.fail("measured again"))
    [again] = asyncio.run(insert_bezels.measure(None, spec))
    assert again.bezel.quad == item.bezel.quad and again.bezel.flatness.words() == item.bezel.flatness.words()


# ── The surface at generation ─────────────────────────────────────────────


def test_a_still_with_a_curved_screen_is_refused_before_anything_is_paid_and_a_flat_one_says_its_bezel(monkeypatch):
    from app.services.video_studio import pictures

    shown = {"picture": _still("curved")}

    async def read_picture(_db, _user, _file_id, *, what):
        return Image.fromarray(shown["picture"])

    monkeypatch.setattr(pictures, "read_picture", read_picture)
    reference = {"reference_file_id": str(uuid.uuid4())}

    def lint():
        # A shot asked as footage for a screen insert: the only one whose screen is measured.
        return asyncio.run(clips.lint_surface(None, None, reference, None, insert=True))

    with pytest.raises(clips.ClipError) as refused:
        lint()
    said = str(refused.value)
    assert said.startswith("Regenerate the start picture with a flat screen: the surface is curved")
    assert "its top edge bows" in said and "past 5%" in said and "Nothing was queued or paid" in said
    # Seen at an angle, it is flat: nothing refused, its angle said in its record and in degrees.
    shown["picture"] = _still("oblique")
    angled, notes = lint()
    assert angled["flatness"]["flat"] is True and angled["flatness"]["angle"] == pytest.approx(0.5, abs=0.01)
    assert notes == [
        "The start picture's screen is seen at 42 degrees: a fact, not a fault - flat, the insert is drawn onto it exactly."
    ]
    # A flat one: what validate_only answers before anything is paid - the bezel and its flatness.
    shown["picture"] = _still("flat")
    surface, notes = lint()
    assert surface["flatness"]["flat"] is True and _close(surface["quad"], SCREEN) and notes == []
    # No start picture, nothing to measure.
    assert asyncio.run(clips.lint_surface(None, None, {}, None, insert=True)) == (None, [])


def test_a_take_that_bends_its_screen_says_so_first_words_the_regeneration(monkeypatch, tmp_path):
    from app.services.video_studio import encoder

    frame = {"picture": _still("curved")}

    async def extract_frame(_path, *, at, out_path, width=None):
        Image.fromarray(frame["picture"]).save(out_path, format="PNG")

    monkeypatch.setattr(encoder, "extract_frame", extract_frame)
    record, unflat = asyncio.run(clips.take_surface("take.mp4", str(tmp_path), {}))
    assert record is not None and record["flatness"]["flat"] is False
    assert unflat.startswith("Regenerate the shot with a flat screen: the surface is curved")
    frame["picture"] = _still("flat")
    record, unflat = asyncio.run(clips.take_surface("take.mp4", str(tmp_path), {}))
    assert record["flatness"]["flat"] is True and unflat is None


# ── Footage against its narration ─────────────────────────────────────────


def _six_second_take() -> timeline.TakeMedia:
    return timeline.TakeMedia(take_id=TAKE, duration=6.0, css_width=1600, css_height=900, pixel_ratio=1.0, fps=30, frame_count=180)


def _recording_spec(extend_mode: str = "hold") -> dict:
    return {
        "title": "Demo",
        "freestyle": True,
        "scenes": [
            {"id": "r1", "type": "recording", "take_id": TAKE, "narration": "Nine seconds of words.", "extend_mode": extend_mode},
        ],
    }


def _recording_findings(extend_mode: str = "hold") -> list[quality.QualityFinding]:
    spec = parse_video_spec(validate_video_spec(_recording_spec(extend_mode)))
    take = _six_second_take()
    media = timeline.PlanMedia(takes={TAKE: take}, voices={"r1": timeline.VoiceMedia(9.0)})
    document = timeline.plan(spec, media, fps=30, theme={}).document
    facts = quality.ContentFacts(ready_takes={TAKE}, takes={TAKE: take}, spoken={"r1": 9.0})
    return quality.content_findings(spec, document, facts, None)


def test_a_6_s_recording_under_a_9_s_narration_is_refused_at_save_with_the_speed_that_fills_it():
    [frozen] = [f for f in _recording_findings() if f.kind == "frozen_hold"]
    assert frozen.severity in render_checks.BLOCKING_SEVERITIES and frozen.scene_id == "r1"
    assert "its recording plays 6.0 s" in frozen.message and "its narration, 9.0 s as spoken" in frozen.message
    assert "extend_mode zoom" in frozen.message
    speed = float(re.search(r"speed (0\.\d+) \(it then plays (\d+\.\d) s\)", frozen.message).group(1))
    # The planner's own arithmetic: at that speed the recording lasts the scene, a hundredth faster it does not.
    spec = parse_video_spec(validate_video_spec(_recording_spec()))
    planned = timeline.plan(spec, timeline.PlanMedia(takes={TAKE: _six_second_take()}, voices={"r1": timeline.VoiceMedia(9.0)}), fps=30, theme={})
    need = planned.document["scenes"][0]["time_map"][-1][0] - timeline.EXTEND_MIN
    assert timeline.recording_seconds(_six_second_take(), speed=speed) >= need
    assert timeline.recording_seconds(_six_second_take(), speed=speed + 0.01) < need


def test_the_recording_says_at_once_what_it_plays_at_each_speed_and_the_narration_it_carries():
    said = timeline.recording_lengths(_six_second_take())
    assert "6.0 s at speed 1, 8.0 s at speed 0.75, 12.0 s at speed 0.5" in said
    assert "words of narration at the reading pace" in said and "extend_mode zoom" in said


def _looped_spec() -> dict:
    return {
        "title": "Loop",
        "freestyle": True,
        "scenes": [
            {
                "id": "c1",
                "type": "clip",
                "clip_id": LOOPED,
                "narration": "Twelve seconds of words.",
                "audio": False,
                "extend_mode": "loop",
                "overlays": [_insert(quad=[list(p) for p in SCREEN], duration=12.0)],
            }
        ],
    }


def test_an_insert_of_12_s_on_an_8_s_looped_clip_says_where_the_seam_falls_and_the_duration_that_ends_before_it():
    spec = parse_video_spec(validate_video_spec(_looped_spec()))
    media = timeline.PlanMedia(clips={"c1": timeline.ClipMedia(8.0, 1280, 720, 24.0, 192)}, voices={"c1": timeline.VoiceMedia(12.0)})
    document = timeline.plan(spec, media, fps=30, theme={}).document
    facts = quality.ContentFacts(shots={LOOPED: quality.ShotFacts("Booth", JOB_READY)}, spoken={"c1": 12.0})
    [seam] = [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "insert_loop_seam"]
    assert seam.severity in render_checks.BLOCKING_SEVERITIES and seam.scene_id == "c1"
    assert "past its clip's loop seam at 8.0 s" in seam.message and "give it duration 8 (it ends at the seam)" in seam.message
    assert "extend_mode zoom" in seam.message


# ── The pre-render lint: every static fault, one answer ────────────────────


def _four_faults():
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Everything",
                "freestyle": True,
                "scenes": [
                    {"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": [_insert(quad=[list(p) for p in SCREEN])]},
                    _recording_spec()["scenes"][0],
                    _looped_spec()["scenes"][0],
                    {"id": "s4", "type": "clip", "clip_id": OFF_CLIP, "overlays": [_insert(quad=[list(p) for p in SCREEN])]},
                ],
            }
        )
    )
    take = _six_second_take()
    media = timeline.PlanMedia(
        takes={TAKE: take},
        clips={"c1": timeline.ClipMedia(8.0, 1280, 720, 24.0, 192)},
        voices={"r1": timeline.VoiceMedia(9.0), "c1": timeline.VoiceMedia(12.0)},
    )
    document = timeline.plan(spec, media, fps=30, theme={}).document
    far = ((0.27, 0.25), (0.77, 0.25), (0.77, 0.78), (0.27, 0.78))
    facts = quality.ContentFacts(
        shots={sid: quality.ShotFacts("Shot", JOB_READY) for sid in (CLIP, LOOPED, OFF_CLIP)},
        ready_takes={TAKE},
        takes={TAKE: take},
        spoken={"r1": 9.0, "c1": 12.0},
        bezels=[
            _measured(SCREEN, _bezel("curved"), scene_id="s1"),
            _measured(far, _bezel(given=far), scene_id="s4", footage=("clip", OFF_CLIP)),
        ],
    )
    return spec, quality.content_findings(spec, document, facts, None)


def test_a_spec_with_four_static_faults_is_refused_once_with_all_four(monkeypatch):
    _spec, found = _four_faults()
    kinds = {f.kind: f for f in found}
    assert {"insert_flatness", "frozen_hold", "insert_loop_seam", "insert_bezel"} <= set(kinds)
    # The one only a regeneration fixes says so in its first words.
    assert kinds["insert_flatness"].message.startswith(f"Regenerate the shot `{CLIP}` with a flat screen")
    assert all(f.severity in render_checks.BLOCKING_SEVERITIES for f in kinds.values() if f.kind in ("insert_flatness", "frozen_hold", "insert_loop_seam", "insert_bezel"))
    # No finding asks for a number to type back.
    assert not any(NUMBERS.search(f.message) for f in found)

    async def checked(*_args, **_kwargs):
        return quality.QualityReport(scene_count=4, findings=tuple(found), revision=3)

    class _Db:
        async def get(self, model, key, **_options):
            return SimpleNamespace(
                status=JOB_READY, file_id=uuid.uuid4(), expired_at=None, params={}, width=None, height=None,
                viewport_width=None, viewport_height=None, pixel_ratio=1.0, name=None,
            )

    monkeypatch.setattr(quality, "check_video_quality", checked)
    project = SimpleNamespace(id=uuid.uuid4(), revision=3, scene_previews=None)
    effective = validate_video_spec(
        {"title": "Everything", "freestyle": True, "scenes": [_recording_spec()["scenes"][0], _looped_spec()["scenes"][0]]}
    )
    reasons = asyncio.run(render_checks.static_reasons(_Db(), project, SimpleNamespace(id=uuid.uuid4()), effective, quality="high"))
    listed = "\n".join(reason.message for reason in reasons)
    for kind in ("insert_flatness", "frozen_hold", "insert_loop_seam", "insert_bezel"):
        assert f"({kind}, dense)" in listed
    # A scene's preview is refused on its own faults alone.
    [own] = asyncio.run(
        render_checks.static_reasons(_Db(), project, SimpleNamespace(id=uuid.uuid4()), effective, quality="draft", scene_id="c1")
    )
    assert own.scene_id == "c1" and "loop seam" in own.message


def test_a_flat_screen_left_to_its_footage_passes_its_lint_with_no_human_edit():
    authored = validate_video_spec(_clip_spec(_insert()))
    measured = [_measured(None, _bezel())]
    resolved = insert_bezels.resolve(authored, measured)
    assert not resolved.problems and resolved.changed
    spec = parse_video_spec(resolved.authored)
    media = timeline.PlanMedia(clips={"s1": timeline.ClipMedia(8.0, 1280, 720, 24.0, 192)})
    document = timeline.plan(spec, media, fps=30, theme={}).document
    facts = quality.ContentFacts(shots={CLIP: quality.ShotFacts("Booth", JOB_READY, width=1280, height=720)}, bezels=measured)
    blocking = [f for f in quality.content_findings(spec, document, facts, None) if f.severity in render_checks.BLOCKING_SEVERITIES]
    assert blocking == []


# ── The render's own findings carry the fix ───────────────────────────────


def test_an_insert_off_its_bezel_is_told_the_fix_never_the_corners():
    frames = {n: {"alignment": 0.012, "refined": [list(p) for p in SCREEN], "size": [1280, 720]} for n in range(10)}
    at = lambda n: n / 24  # noqa: E731
    lead, remedy = insert_checks._alignment_remedy(frames, sorted(frames), frames[0], at)
    assert lead == "" and remedy["text"].startswith("leave its quad out (quad auto) and save")
    assert remedy["kind"] == insert_checks.GIVE_QUAD
    # Aligned on its first frames, off later: the surface moves under it - its corners given frame by frame.
    lead, remedy = insert_checks._alignment_remedy(frames, list(range(6, 10)), frames[0], at)
    assert remedy["text"].startswith("the surface moves under it from t=") and "track: true" in remedy["text"]
    # A bezel seen at an angle is flat: no regeneration for it.
    oblique = {**frames[0], "refined": [[0.25, 0.2], [0.75, 0.3], [0.75, 0.7], [0.25, 0.8]], "edge_bows": [0.0, 0.0, 0.0, 0.0]}
    lead, _remedy = insert_checks._alignment_remedy(frames, sorted(frames), oblique, at)
    assert lead == ""
    # A bezel whose edge bows, its points confirming it: only a regeneration fixes it, said first.
    bowed = {**frames[0], "edge_bows": [0.08, 0.0, 0.0, 0.0], "edge_inliers": [0.3, 1.0, 1.0, 1.0], "edge_residuals": [0.01, 0.0, 0.0, 0.0]}
    lead, remedy = insert_checks._alignment_remedy(frames, sorted(frames), bowed, at)
    assert lead.startswith("Regenerate the shot with a flat screen") and "curved (its top edge bows 8%" in remedy["text"]
    assert remedy["kind"] == insert_checks.REGENERATE_FLAT
    # The same bow most of its points deny is a line of the screen's content: no regeneration.
    denied = {**bowed, "edge_inliers": [0.9, 1.0, 1.0, 1.0], "edge_residuals": [0.001, 0.0, 0.0, 0.0]}
    assert insert_checks._alignment_remedy(frames, sorted(frames), denied, at)[0] == ""
    for words in (lead, remedy["text"]):
        assert not NUMBERS.search(words)
