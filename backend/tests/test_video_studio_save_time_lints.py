"""What only the whole render used to find, refused or settled at save.

Synthetic fixtures only - no provider, no browser, no database:

* a two-scene spec whose narrations share one sentence: each scene's preview hears its own narration alone and passes,
  the whole render hears the sentence twice and fails its audio check (``repeated_sentence``) - refused at save,
  naming both scenes (``audio_qc.shared_sentences``, ``projects.shared_sentence_problems``);
* a 1280 px wide recording on a 2400 px quad at ultra - 0.53 of its pixels per pixel, sharp in a 1080p preview -
  refused at save with the window and the pixel ratio to record it at (``insert_checks.soft_takes``,
  ``projects.soft_recording_problems``), judged at the quality the video is made for: its newest whole render's,
  ultra before its first (``spec.FINAL_QUALITY``);
* a keyed screen whose bezel bows 6% under an insert drawn on it: on its bezel, no key left past it - a fact said at
  save, no finding (``insert_bezels.bow_covered``); one bowing 8% that leaves key past its edge stays a finding. The
  save's measure, on the footage frame, is the one a preview and a render hold to, never read again on frames drawn
  at another resolution (``insert_checks.findings``' *measured*);
* a scene preview whose picture failed under an older quality report than the running one: the render gate asks for a
  re-preview under the running report instead of a fix (``render_checks.judged_before``, ``re_preview_words``).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import (
    audio_qc,
    insert_bezels,
    insert_checks,
    projects,
    render_checks,
    screen_insert,
    spec,
    surface_refine,
)
from app.services.video_studio.render_checks import INSERT_FLATNESS, REPEATED_SENTENCE
from app.services.video_studio.spec import VideoSpecError, parse_video_spec, validate_video_spec

pytestmark = pytest.mark.usefixtures("no_default_video_template")

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000b431"), role="user")
TAKE = "00000000-0000-0000-0000-00000000a431"
CLIP = "00000000-0000-0000-0000-00000000c431"
BACKDROP = "00000000-0000-0000-0000-00000000e431"
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"


async def _nothing(*_args, **_kwargs):
    return []


@pytest.fixture
def saving(monkeypatch):
    """``projects.save_spec`` on a session that answers nothing, its references and its bezels left out."""
    monkeypatch.setattr(projects, "verify_references", _nothing)
    monkeypatch.setattr(insert_bezels, "measure", _nothing)

    def save(raw: dict):
        return asyncio.run(projects.save_spec(SimpleNamespace(), user=USER, raw=raw))

    return save


# ── A sentence two narrations say ─────────────────────────────────────────


def _narrated(first: str, second: str) -> dict:
    return {
        "title": "Alerts",
        "freestyle": True,
        "scenes": [
            {"id": "intro", "type": "statement", "lines": ["Every alert"], "narration": first},
            {"id": "triage", "type": "statement", "lines": ["Triaged"], "narration": second},
        ],
    }


SHARED = "Every alert is triaged by an agent before an analyst sees it."


def test_a_sentence_two_narrations_say_is_found_normalised_and_close_ones_too():
    [shared] = audio_qc.shared_sentences(
        [("intro", f"Welcome to the console. {SHARED}"), ("triage", "EVERY alert is triaged by an agent, before an analyst sees it! Then it closes.")]
    )
    assert shared.scene_ids == ("intro", "triage") and shared.sentence == SHARED
    # Close enough that the audio check hears one as the other: a word apart in twelve.
    [close] = audio_qc.shared_sentences([("intro", SHARED), ("triage", SHARED.replace("an agent", "the agent"))])
    assert close.scene_ids == ("intro", "triage")
    # Distinct sentences, a short one said twice, or one narration repeating itself are not.
    assert audio_qc.shared_sentences([("intro", SHARED), ("triage", "The analyst reads the verdict and closes the case.")]) == []
    assert audio_qc.shared_sentences([("intro", "Let's go."), ("triage", "Let's go.")]) == []
    assert audio_qc.shared_sentences([("intro", f"{SHARED} {SHARED}")]) == []
    # Three scenes saying it are one sentence, all three named.
    [thrice] = audio_qc.shared_sentences([("a", SHARED), ("b", "Something else entirely here."), ("c", SHARED), ("d", SHARED)])
    assert thrice.scene_ids == ("a", "c", "d")


def test_the_whole_render_hears_a_close_pair_more_times_than_either_is_written():
    """Why each preview passes and the whole render fails: the audio check counts a sentence's hearings against how many
    times it is written, every narration together."""
    first, second = SHARED, SHARED.replace("an agent", "the agent")

    def heard(*texts: str) -> list[tuple[str, float, float]]:
        from app.services.video_studio.voice_check import normalized_tokens

        words = [word for text in texts for word in normalized_tokens(text)]
        return [(word, float(n), float(n) + 0.4) for n, word in enumerate(words)]

    alone = [audio_qc.Narration("intro", first, 0.0, 6.0)]
    assert audio_qc.repeated_sentences(heard(first), alone) == []
    both = [audio_qc.Narration("intro", first, 0.0, 6.0), audio_qc.Narration("triage", second, 7.0, 13.0)]
    found = audio_qc.repeated_sentences(heard(first, second), both)
    assert {finding.kind for finding in found} == {REPEATED_SENTENCE} and len(found) == 2


def test_a_two_scene_spec_sharing_one_sentence_is_refused_at_save_naming_both_scenes(saving):
    with pytest.raises(VideoSpecError) as refused:
        saving(_narrated(f"Welcome. {SHARED}", f"{SHARED} The analyst decides."))
    [problem] = refused.value.problems
    assert problem.path == "scenes.1.narration"
    assert problem.message.startswith(f'scenes `intro` and `triage` each say "{SHARED}"')
    assert "the whole render hears it 2 times and fails its audio check (repeated_sentence)" in problem.message
    # Without a voice-over nothing is spoken: nothing to hear twice.
    raw = _narrated(SHARED, SHARED)
    assert projects.shared_sentence_problems(raw, {**raw, "voiceover": {"enabled": False}}) == []


# ── A recording soft at the render the video is made for ──────────────────


#: A 2400 x 1296 px quad of a 3840 x 2160 frame: a 16:9 shot covering it exactly.
QUAD = [[0.1875, 0.2], [0.8125, 0.2], [0.8125, 0.8], [0.1875, 0.8]]
SHOT = (1920, 1080)


def _recorded(**overlay) -> dict:
    return {
        "title": "Console",
        "freestyle": True,
        "scenes": [
            {
                "id": "s1",
                "type": "clip",
                "clip_id": CLIP,
                "overlays": [{"type": "screen_insert", "source": {"take_id": TAKE}, "quad": QUAD, "duration": 6, **overlay}],
            }
        ],
    }


def _geometry(frame: tuple[int, int] = (1280, 720), ratio: float = 1.0) -> render_checks.InsertGeometry:
    return render_checks.InsertGeometry({("take", TAKE): frame, ("clip", CLIP): SHOT}, {"s1": SHOT}, {TAKE: ratio}, {}, {TAKE: 100})


def _async(value):
    async def answer(*_args, **_kwargs):
        return value

    return answer


def test_a_1280_px_take_on_a_2400_px_quad_at_ultra_needs_a_window_at_pixel_ratio_1_5():
    parsed = parse_video_spec(validate_video_spec(_recorded()))
    geometry = _geometry()
    [soft] = insert_checks.soft_takes(
        parsed, (3840, 2160), "ultra", sources=geometry.sources, footage=geometry.footage, pixel_ratios=geometry.pixel_ratios
    )
    assert soft.soft.surface.quad == pytest.approx((2400.0, 1296.0)) and round(soft.soft.density.value, 2) == 0.53
    recording = soft.recording
    assert (recording.viewport, recording.pixel_ratio, recording.frame) == ((1280, 720), 1.5, (1920, 1080))
    assert not recording.density.soft
    said = soft.words()
    assert said.startswith(
        f"its screen insert 0 draws take `{TAKE}` (1280x720 px) on a quad 2400x1296 px at 3840x2160, the ultra render the "
        "video is made for: 0.53 source pixels per pixel, under 0.8"
    )
    assert "Record it again as a 1280x720 window at pixel ratio 1.5, its frames 1920x1080 px (0.80 per pixel there)" in said
    assert 'record_browser_demo(target_render: "ultra", video_id, scene: "s1", insert: 0) records it at that size' in said
    # The same take is sharp on that quad at 1080p (1.07 per pixel): what every preview drew.
    assert insert_checks.soft_takes(
        parsed, (1920, 1080), "high", sources=geometry.sources, footage=geometry.footage, pixel_ratios=geometry.pixel_ratios
    ) == []
    # Pushed in 4x by its zoom, no window a recording takes passes: the quad or the push to change.
    pushed = parse_video_spec(validate_video_spec(_recorded(zoom={"end": 2, "scale": 4})))
    [none] = insert_checks.soft_takes(
        pushed, (3840, 2160), "ultra", sources=geometry.sources, footage=geometry.footage, pixel_ratios=geometry.pixel_ratios
    )
    assert none.recording is None and none.words().endswith("give the insert a smaller quad or a smaller push")


def test_a_1280_px_take_on_a_2400_px_quad_is_refused_at_save_with_the_size_to_record(saving, monkeypatch):
    monkeypatch.setattr(render_checks, "insert_geometry", _async(_geometry()))
    with pytest.raises(VideoSpecError) as refused:
        saving(_recorded())
    [problem] = refused.value.problems
    assert problem.path == "scenes.0.overlays.0.source"
    assert problem.message.startswith("scene s1: its screen insert 0 draws take")
    assert "the ultra render the video is made for" in problem.message
    assert "a 1280x720 window at pixel ratio 1.5, its frames 1920x1080 px" in problem.message
    # Recorded at that size, it saves past the lint.
    monkeypatch.setattr(render_checks, "insert_geometry", _async(_geometry((1920, 1080), 1.5)))
    assert asyncio.run(render_checks.soft_recordings(None, parse_video_spec(validate_video_spec(_recorded())), "ultra")) == []


def test_the_quality_a_save_holds_recordings_to_is_the_newest_whole_renders_else_ultra(monkeypatch):
    class Renders:
        def __init__(self, newest):
            self.newest = newest

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: self.newest)

    video = SimpleNamespace(id=uuid.uuid4())
    assert spec.FINAL_QUALITY == "ultra"
    assert asyncio.run(render_checks.target_quality(Renders(None), video, default=spec.FINAL_QUALITY)) == "ultra"
    assert asyncio.run(render_checks.target_quality(Renders("high"), video, default=spec.FINAL_QUALITY)) == "high"
    assert asyncio.run(render_checks.target_quality(Renders(None), None, default=spec.FINAL_QUALITY)) == "ultra"
    # The legibility advice keeps its own default.
    assert asyncio.run(render_checks.target_quality(Renders(None), video)) == spec.DEFAULT_QUALITY
    # A video rendered at high is held to high: the take sharp there saves, the ultra soft said as advice.
    monkeypatch.setattr(render_checks, "insert_geometry", _async(_geometry()))
    raw = validate_video_spec(_recorded())
    assert asyncio.run(projects.soft_recording_problems(Renders("high"), video, raw, raw)) == []
    [refused] = asyncio.run(projects.soft_recording_problems(Renders(None), video, raw, raw))
    assert "the ultra render the video is made for" in refused.message
    # A reading that cannot be made refuses nothing: the render's gate reads it again.
    monkeypatch.setattr(render_checks, "insert_geometry", _raise)
    assert asyncio.run(render_checks.soft_recordings(None, parse_video_spec(raw), "ultra")) == []


async def _raise(*_args, **_kwargs):
    raise OSError("database gone")


# ── A bow the insert drawn on it covers ───────────────────────────────────


SIZE = (1280, 720)


def _bowed_bezel(sag: float) -> np.ndarray:
    """A 1280 x 720 room, a 640 x 380 green screen in a 14 px dark bezel whose top and bottom edges bulge *sag* px."""
    image = Image.new("RGB", SIZE, (46, 46, 46))
    draw = ImageDraw.Draw(image)
    ts = np.linspace(0.0, 1.0, 120)
    top = [(320 + t * 640, 170 - sag * (1 - (2 * t - 1) ** 2)) for t in ts]
    bottom = [(320 + t * 640, 550 + sag * (1 - (2 * t - 1) ** 2)) for t in ts[::-1]]
    outer_top, outer_bottom = [(x, y - 14) for x, y in top], [(x, y + 14) for x, y in bottom]
    draw.polygon(
        [(306, outer_top[0][1]), *outer_top, (974, outer_top[-1][1]), (974, outer_bottom[0][1]), *outer_bottom, (306, outer_bottom[-1][1])],
        fill=(18, 18, 20),
    )
    draw.polygon(top + bottom, fill=(0, 177, 64))
    return np.asarray(image)


def _measured_on(frame: np.ndarray, monkeypatch) -> insert_bezels.Measured:
    """The save's measure of a statement whose picture backdrop is *frame*, its keyed insert's quad left to it."""
    from app.services.video_studio import assets, render_cache

    parsed = parse_video_spec(
        validate_video_spec(
            {
                "title": "Booth",
                "freestyle": True,
                "scenes": [
                    {
                        "id": "s1",
                        "type": "statement",
                        "lines": ["Live"],
                        "backdrop": {"file_id": BACKDROP},
                        "overlays": [{"type": "screen_insert", "source": {"file_id": CLIP}, "key": {"color": "green"}, "duration": 4.0}],
                    }
                ],
            }
        )
    )
    kept: dict = {}

    async def footage_of(_db, _scene):
        return (("file", BACKDROP), BACKDROP, True)

    async def download(_file_id, path):
        Image.fromarray(frame).save(path, format="PNG")
        return SimpleNamespace(filename="booth.png")

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    async def put_file(kind, digest, path, *, suffix=""):
        kept[(kind, digest, suffix)] = path

    monkeypatch.setattr(insert_bezels, "_footage_of", footage_of)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(render_cache, "put_file", put_file)
    [item] = asyncio.run(insert_bezels.measure(None, parsed))
    # Kept with the measure: the next save reads the insert drawn back without drawing it.
    monkeypatch.setattr(screen_insert, "drawn_on", lambda *_a, **_k: pytest.fail("drawn again"))
    [again] = asyncio.run(insert_bezels.measure(None, parsed))
    assert again.drawn == item.drawn
    return item


def test_a_bowed_bezel_under_an_aligned_insert_is_a_fact_at_save_never_a_finding(monkeypatch):
    item = _measured_on(_bowed_bezel(40.0), monkeypatch)
    shape = item.bezel.flatness
    # The bow is real - 6% of the edge, its points confirming it - and the save's own reading of it.
    assert not shape.flat and shape.convex and shape.words().startswith(("its top edge bows 6%", "its bottom edge bows 6%"))
    # The insert drawn on that frame sits on the bezel with no key left past it: it covers the bow.
    assert insert_checks.off_bezel(item.drawn) <= surface_refine.ALIGNMENT_MAX_SHARE
    assert item.drawn["residual_key"] <= screen_insert.RESIDUAL_FAIL_SHARE
    assert insert_bezels.bow_covered(item) and insert_bezels.flat_problem(item) is None
    assert insert_bezels.findings([item]) == []
    assert insert_bezels.verdicts([item], 9)[KEY][0]["gates"]["flatness"] is True
    note = insert_bezels.bow_note(item)
    assert note.startswith("Scene s1: the surface under its screen insert 0 bows on frame 0 (0.00 s) of its footage")
    assert note.endswith("of its quad left showing the key: a fact, not a finding.")
    authored = {"title": "Booth", "freestyle": True, "scenes": [{"id": "s1", "type": "statement", "overlays": [{"type": "screen_insert"}]}]}
    assert note in insert_bezels.resolve(authored, [item]).notes


def test_a_bow_that_leaves_key_past_the_insert_stays_a_finding(monkeypatch):
    item = _measured_on(_bowed_bezel(48.0), monkeypatch)
    assert not item.bezel.flatness.flat
    assert item.drawn["residual_key"] > screen_insert.RESIDUAL_FAIL_SHARE and not insert_bezels.bow_covered(item)
    [(scene_id, kind, words, _shot)] = insert_bezels.findings([item])
    assert (scene_id, kind) == ("s1", "insert_flatness") and "a planar insert cannot cover it" in words
    assert insert_bezels.bow_note(item) is None
    # Unkeyed, nothing reads the key left past it: a bow is never taken as covered.
    assert not insert_bezels.bow_covered(insert_bezels.Measured("s1", 0, ("file", BACKDROP), BACKDROP, 0, 0.0, None, item.bezel, "d"))


# ── One measure, at every resolution ──────────────────────────────────────


#: A frame's audit whose top and bottom edges bow 8%, their points confirming it: what a 2160p frame read.
CURVED = {
    "refined": [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]],
    "size": [3840, 2160],
    "edge_bows": [0.08, 0.0, 0.08, 0.0],
    "edge_inliers": [0.3, 1.0, 0.3, 1.0],
    "edge_residuals": [0.0099, 0.0, 0.0099, 0.0],
    "edge_visible": [1.0, 1.0, 1.0, 1.0],
}


def _placed() -> dict:
    return {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 8 / 24, keyed=True)}


def _audits(**fields) -> dict:
    return {KEY: {"frames": {str(n): {**CURVED, **fields} for n in range(8)}, "evidence": {}}}


def _save_measure(curved: bool, drawn: dict | None = None) -> insert_bezels.Measured:
    corners = [(320.0, 180.0), (960.0, 180.0), (960.0, 540.0), (320.0, 540.0)]
    shape = (
        surface_refine.flatness(corners, (0.08, 0.0, 0.08, 0.0), (0.3, 1.0, 0.3, 1.0), (0.008, 0.0, 0.008, 0.0), SIZE)
        if curved
        else surface_refine.flatness(corners, (0.0,) * 4, (1.0,) * 4, (0.0,) * 4, SIZE)
    )
    bezel = surface_refine.Bezel(((0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)), shape)
    return insert_bezels.Measured("s1", 0, ("clip", CLIP), "file-1", 12, 0.5, None, bezel, "d", drawn=drawn)


def test_a_bow_a_4k_frame_reads_on_a_surface_the_save_measured_flat_is_no_finding_and_no_disagreement():
    flat = insert_bezels.measured_surfaces([_save_measure(False)])
    found = insert_checks.findings(_audits(alignment=0.0003, residual_key=0.0), _placed(), fps=24, measured=flat)
    assert found == []
    # Not even a warning that the stages disagree: the render never read the flatness of its own frames.
    passed = insert_bezels.verdicts([_save_measure(False)], 9)
    assert insert_checks.findings(_audits(), _placed(), fps=24, passed=passed, measured=flat) == []
    [report] = insert_checks.reports(_audits(), _placed(), fps=24, measured=flat)
    assert report["verdicts"]["flatness"] is True
    assert report["flatness"]["measured"] == "at save, on frame 12 (0.50 s) of its footage" and "curved" not in report["flatness"]
    # A pane of a wall is held to its overlay's measure.
    assert insert_checks.saved_measure(flat, f"{KEY}.1") is flat[KEY]


def test_a_surface_the_save_measured_curved_is_the_renders_finding_whatever_its_frames_read():
    curved = insert_bezels.measured_surfaces([_save_measure(True)])
    flat_frames = _audits(edge_bows=[0.0] * 4, alignment=0.0003, residual_key=0.0)
    [(finding, _proof)] = insert_checks.findings(flat_frames, _placed(), fps=24, measured=curved)
    assert finding.kind == INSERT_FLATNESS and finding.severity == render_checks.FAIL and finding.data["measured"] == "save"
    assert "surface is curved as the save measured it on frame 12 (0.50 s) of its footage (its top edge bows 8%" in finding.message
    # Covered by the insert drawn on it at save, it is a fact there and here.
    covered = insert_bezels.measured_surfaces([_save_measure(True, {"alignment": 0.0002, "residual_key": 0.0})])
    assert insert_checks.findings(_audits(), _placed(), fps=24, measured=covered) == []
    [report] = insert_checks.reports(_audits(), _placed(), fps=24, measured=covered)
    assert report["flatness"]["covered_by_insert"] is True
    [line] = render_checks.insert_lines(render_checks.Checks(inserts=[report]).as_dict())
    assert "fact: surface curved at save, on frame 12 (0.50 s) of its footage: its top edge bows 8%" in line
    assert "which the insert drawn on its bezel covers - no key left past it" in line


def test_a_bow_the_frames_read_under_an_insert_on_its_bezel_with_no_key_left_is_a_fact():
    """A tracked insert, which no save measures: its frames' own reading, a bow the insert drawn there covers a fact."""
    assert insert_checks.findings(_audits(alignment=0.0003, residual_key=0.0), _placed(), fps=24) == []
    [report] = insert_checks.reports(_audits(alignment=0.0003, residual_key=0.0), _placed(), fps=24)
    assert report["flatness"]["frames_covered"] == 8 and report["flatness"]["covered_by_insert"] is True
    assert report["verdicts"]["flatness"] is True
    # Key left past its edge, or no residual key read (an unkeyed insert): the finding stands.
    [(left, _proof)] = insert_checks.findings(_audits(alignment=0.0003, residual_key=0.01), _placed(), fps=24)
    assert left.kind == INSERT_FLATNESS and render_checks.INSERT_RESIDUAL_KEY in left.data["follows"]
    [(unkeyed, _proof)] = insert_checks.findings(_audits(alignment=0.0003), _placed(), fps=24)
    assert unkeyed.kind == INSERT_FLATNESS


# ── A preview judged by an older checker ──────────────────────────────────


def test_a_preview_judged_under_an_older_report_is_answered_with_a_re_preview(monkeypatch):
    """A scene's failed drawing judged at v47 while v48 runs: nothing in the spec to fix, a preview to draw again."""
    from app.services.video_studio import quality

    monkeypatch.setattr(quality, "QUALITY_REPORT_VERSION", 48)
    assert render_checks.judged_before(47) == 47
    assert render_checks.judged_before(48) is None and render_checks.judged_before(None) is None
    words = render_checks.re_preview_words(47, "intro")
    assert "judged under quality report v47, older than the running v48" in words
    assert "re-preview under report v48 (render_video_scene(scene='intro'))" in words
    assert "fix what it points at" not in words
    # The gate's words name the version it judged by, in both branches.
    current = render_checks.failed_drawing_words(48, "s3")
    assert current.startswith("judged under the running quality report v48: fix what it points at")
    assert "judged under quality report v45, older than the running v48" in render_checks.failed_drawing_words(45, "s3")
    # A drawing recorded before drawings kept their version, whose render row is gone or names none: not current.
    unknown = render_checks.failed_drawing_words(None, "s3")
    assert "judged under a quality report version that was not recorded, older than the running v48" in unknown
    assert "re-preview under report v48 (render_video_scene(scene='s3'))" in unknown
    assert "fix what it points at" not in unknown

    gone, silent = uuid.UUID(int=0xD432), uuid.UUID(int=0xD433)

    class Rows:
        def all(self):
            # The purged render answers no row; the other stores checks without a version.
            return [(silent, {"findings": []})]

    class Db:
        async def execute(self, _query):
            return Rows()

    drawings = [{"render_id": str(gone)}, {"render_id": str(silent)}]
    versions = asyncio.run(render_checks._drawing_report_versions(Db(), drawings))
    assert versions.get(str(gone)) is None and versions[str(silent)] is None
    for render_id in (gone, silent):
        words = render_checks.failed_drawing_words(versions.get(str(render_id)), "s3")
        assert "nothing to fix yet" in words and "fix what it points at" not in words


def test_a_drawing_keeps_the_report_version_that_judged_it_and_older_drawings_read_their_render():
    seen = render_checks.record_seen({}, {"intro": "d1"}, "r1", checks={"quality_report_version": 47, "findings": []})
    assert seen["intro"]["report_version"] == 47
    assert "report_version" not in render_checks.record_seen({}, {"intro": "d1"}, "r1", checks=None)["intro"]

    old_render = uuid.UUID("00000000-0000-0000-0000-00000000d431")

    class Rows:
        def all(self):
            return [(old_render, {"quality_report_version": 46})]

    class Db:
        async def execute(self, _query):
            return Rows()

    drawings = [{"render_id": "r1", "report_version": 47}, {"render_id": str(old_render)}, {"render_id": "not-a-uuid"}]
    versions = asyncio.run(render_checks._drawing_report_versions(Db(), drawings))
    assert versions == {"r1": 47, str(old_render): 46, "not-a-uuid": None}
