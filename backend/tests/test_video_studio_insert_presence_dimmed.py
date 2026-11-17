"""A screen insert inside a transition that dims or blurs its whole scene.

A clip scene with a screen insert entering on a ``blur`` transition failed its render as "insert missing at t=..., its
quad is one flat colour" on the first frames of the join: the transition dims and gaussian-blurs the whole frame, the
insert with it, so its quad measured a detail variance under the presence floor (5.7 and 4.8 under 6) - with "nothing
in the spec causes it: render again" as its fix, and rendering again draws the same frames.

Synthetic fixtures only - no provider, no browser, no database. Each frame is composed as the render composes it: each
scene's keyed insert drawn onto its own footage (``screen_insert.compose``), the scenes blended as the page's
``applyTransition`` blends them - a ``blur`` blurs each scene's layer (14 px on a 1920 px stage at its peak) and fades it
over the other on the page's ``move`` ease -, the frame then put through a lossy encode. The presence gate reads those
encoded frames (``insert_checks.PresenceGate``):

* a static insert entering on a blur passes, with a fact and no finding - its source's structure read on the quad
  under the floor, or, where it is not, the quad judged against the frame around it;
* a dark dashboard of dim text under a crossfade in reads under the floor and is present by its structure,
  the same with its surface flat on the join is missing, and two readings of one scene agree;
* the same scene with the insert's source removed on its first 5 frames fails as missing;
* a fade_black into an insert scene passes;
* on footage whose texture the transition leaves above the floor, the quad is judged against the frame around it.

And a finding nothing in the spec causes: the render harness's defect, said once with its frame to the
administrator, never a finding - neither on its first drawing nor when the same frames come back on a drawing of the
same digest. One whose frames all lie in a transition names the edit that keeps the insert off them: a
finding of the video.
"""

from __future__ import annotations

import functools
import io
import math

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import insert_checks, render_answer, render_checks, screen_insert
from app.services.video_studio.render_checks import FAIL, INSERT_MISSING, Finding
from tests.video_studio_insert_fixture import (
    FPS,
    SCREEN,
    H,
    PresenceRender,
    W,
    booth,
    clip_document,
    dim_ui,
    key_of,
    tiled_booth,
)

#: The insert's scene coming in over 0.55 s, the length ``timeline`` gives a calm blur.
BLUR = (("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 1.7, 3.2, 1.5, "blur", 0.55))
FADE = (("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 1.7, 3.2, 1.5, "fade_black", 0.55))


@functools.cache
def _probe(scenes: tuple, wall=booth) -> dict[str, tuple[list[int], list[int]]]:
    """``insert key -> (the frames the presence gate judges it on, those its quad reads under the floor on)`` in a
    render of *scenes*: shown whole and enough of it on screen - and, of them, those the floor alone fails."""
    render_ = PresenceRender(clip_document(list(scenes)), wall=wall)
    encoded = {n: render_.frame(n) for n in range(int(render_.document["frames"]))}
    placed = insert_checks.placed_inserts(render_.document, render_.inserts)
    out = {}
    for key, where in placed.items():
        payload = next(p for scene in render_.document["scenes"] for p in scene["inserts"] if p["src"] == key)
        read, flat = [], []
        for frame in where.whole_frames():
            spot = render_.audits[key]["placed"].get(frame)
            if spot is None or spot[4] < insert_checks.PRESENCE_MIN_SHOWN:
                continue
            read.append(frame)
            corners = insert_checks._quad_on_frame(spot[:4], payload, {}, (W, H))
            seen = insert_checks._luma(screen_insert.rectified(np.ascontiguousarray(encoded[frame]), corners, (180, 134), rim=0.0))
            inset = np.zeros(seen.shape, dtype=bool)
            inset[8:-8, 11:-11] = True
            if insert_checks.detail_variance(seen, inset) < insert_checks.FLAT_VAR_MIN:
                flat.append(frame)
        out[key] = (read, flat)
    return out


def _facts(render_: PresenceRender, gate: insert_checks.PresenceGate, key: str) -> list[str]:
    placed = insert_checks.placed_inserts(render_.document, render_.inserts)
    result = gate.result()
    report = next(r for r in insert_checks.reports(render_.audits, placed, fps=FPS, presence=result.by_key) if r["key"] == key)
    return [m.line() for m in insert_checks.classify(report)]


# ── A static insert entering on a blur ────────────────────────────────────


def _unrecognised(monkeypatch) -> None:
    """A quad under the floor whose source's structure the gate does not read: what a blur that smears the
    text leaves - the frame around it is then what judges it."""
    original = insert_checks.structural_similarity

    def blind(seen, expected, mask):
        if insert_checks.detail_variance(seen, mask) < insert_checks.FLAT_VAR_MIN:
            return 0.0
        return original(seen, expected, mask)

    monkeypatch.setattr(insert_checks, "structural_similarity", blind)


def test_a_static_insert_entering_on_a_blur_passes_with_a_fact_and_no_finding():
    coming, leaving = key_of("b"), key_of("a")
    assert _probe(BLUR)[coming][1]
    render_ = PresenceRender(clip_document(list(BLUR)))
    gate = render_.gate()
    result = gate.result()
    assert [finding.message for finding, _ in result.findings] == []
    # Unblended, the quad coming in is still blurred under the floor on frames it shows
    # enough to be judged - its structure still the source's. The scene leaving is read while its blur is light.
    (dimmed,) = result.by_key[coming]["dimmed"]
    assert dimmed["window"] == "blur in" and dimmed["frames"] >= 1 and dimmed["recognised"] == dimmed["frames"]
    assert result.by_key[coming]["missing"] == 0 and result.by_key[leaving]["missing"] == 0
    # Said as a fact of its report, never a finding.
    facts = [line for line in _facts(render_, gate, coming) if "inside the blur in" in line]
    assert facts and facts[0].startswith("fact: ") and "under the presence floor (6)" in facts[0]
    assert "by its source's structure there (SSIM over 0.3)" in facts[0]


def test_a_static_insert_entering_on_a_blur_whose_structure_is_not_read_is_judged_by_the_frame_around_it(monkeypatch):
    _unrecognised(monkeypatch)
    coming, leaving = key_of("b"), key_of("a")
    render_ = PresenceRender(clip_document(list(BLUR)))
    gate = render_.gate()
    result = gate.result()
    assert [finding.message for finding, _ in result.findings] == []
    # Judged against the frame around it: the whole frame under the floor too.
    (dimmed,) = result.by_key[coming]["dimmed"]
    assert dimmed["window"] == "blur in" and dimmed["frames"] >= 1 and dimmed["recognised"] == 0
    assert result.by_key[coming]["missing"] == 0 and result.by_key[leaving]["missing"] == 0
    facts = [line for line in _facts(render_, gate, coming) if "inside the blur in" in line]
    assert facts and facts[0].startswith("fact: ") and "under the presence floor (6) with the whole frame" in facts[0]


def test_the_same_scene_with_its_source_removed_on_its_first_frames_fails_as_missing():
    key = key_of("b")
    placed = insert_checks.placed_inserts(clip_document(list(BLUR)), PresenceRender(clip_document(list(BLUR))).inserts)
    # The first frames it is judged on inside the blur: from 65% shown to the end of the scene it leaves.
    removed = [n for n in _probe(BLUR)[key][0] if placed[key].blend_at(n) is not None][:5]
    assert len(removed) >= 3, "the blur's frames that show enough of it to be judged"
    render_ = PresenceRender(clip_document(list(BLUR)), removed=set(removed))
    result = render_.gate().result()
    (finding, _proof), *_ = [hit for hit in result.findings if hit[0].data["insert"] == key]
    assert finding.kind == INSERT_MISSING and finding.data["why"] == "flat" and finding.severity == FAIL
    assert finding.data["frame"] == removed[0] and finding.data["frames"] == len(removed)
    assert "inside the blur in: flat against the frame around it" in finding.message
    assert finding.data["span"] == [round(removed[0] / FPS, 3), round(removed[-1] / FPS, 3)]
    assert result.by_key[key]["joins_missing"] == len(removed)
    # Its frames all lie in the blur in: the edit that keeps the insert off them - a finding of the video, never
    # the harness's.
    assert finding.data["action"]["kind"] == insert_checks.AVOID_TRANSITION
    assert "make scene b's transition a cut, or start the insert after it" in finding.message
    assert render_checks.harness_defects([finding], {}) == ([finding], [])


def test_a_fade_black_into_an_insert_scene_passes():
    result = PresenceRender(clip_document(list(FADE))).gate().result()
    assert [finding.message for finding, _ in result.findings] == []
    # A fade_black only dims its layers: unblended, each quad is read as its scene paints it. The frames its
    # quad read under the floor on show under 65% of it: faint, never judged.
    for key in (key_of("a"), key_of("b")):
        assert result.by_key[key]["missing"] == 0 and result.by_key[key]["blended"] >= 1 and "dimmed" not in result.by_key[key]
    assert result.by_key[key_of("b")]["faint"] >= 1


# ── A dark dashboard under a crossfade in ─────────────────────────────────

#: The insert's scene coming in over a crossfade of 0.5 s, as a statement over a clip takes it.
CROSSFADE = (("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 1.7, 3.2, 1.5, "crossfade", 0.5))


class _DarkRender(PresenceRender):
    """``PresenceRender`` whose insert of scene ``b`` is a dark dashboard of a near-black ground and dim text, as an
    exposure overview reads - its detail a little over the presence floor whole, under it once a crossfade or the
    encoder dims it -, its frames softened as an encoder softens them. On the frames *removed* the surface shows one
    flat dark colour, as a surface drawn in the bezel's colour does (F34)."""

    def __init__(self, document: dict, **kwargs) -> None:
        super().__init__(document, pictures={"b": dim_ui((10, 16, 28), lift=11)}, **kwargs)

    def _scene(self, scene: dict, frame: int, layer, above) -> np.ndarray:
        rgb = super()._scene(scene, frame, layer, above)
        if scene["id"] == "b" and frame in self.removed:
            picture = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
            ImageDraw.Draw(picture).polygon([tuple(p) for p in SCREEN], fill=(13, 18, 30))
            rgb = np.asarray(picture, dtype=np.float32)
        return rgb

    def frame(self, frame: int) -> np.ndarray:
        picture = Image.fromarray(np.clip(self._stack(frame, self.layers(frame)), 0, 255).astype(np.uint8))
        buffer = io.BytesIO()
        picture.filter(ImageFilter.GaussianBlur(0.8)).save(buffer, format="JPEG", quality=90)
        return np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))


def test_a_dark_dashboard_under_a_crossfade_in_is_present_by_its_structure_never_flat():
    """Preview 274de279 of revision 128: a statement over a clip with one insert, an exposure overview on a
    near-black ground, blocked as "insert missing: its quad is one flat colour (#010A16, detail variance 4.9 under 6),
    inside the crossfade in" on 11 frames of its join - the same scene, unchanged, previewed green four minutes later
    and its high render read the source on every frame. At alpha 0.66 under the crossfade the quad's detail falls under
    the floor while its structure is still the source's; the frame around it, the scene it leaves, is dimmed by
    nothing."""
    key = key_of("b")
    render_ = _DarkRender(clip_document(list(CROSSFADE)))
    gate = render_.gate()
    result = gate.result()
    assert [finding.message for finding, _ in result.findings] == []
    assert result.by_key[key]["missing"] == 0 and result.by_key[key]["joins_missing"] == 0
    # Read under the floor on frames of the crossfade and recognised there: said as a fact, never a finding.
    dimmed = {d["window"]: d for d in result.by_key[key].get("dimmed", [])}
    recognised = dimmed.get("crossfade in", {}).get("recognised", 0) + result.by_key[key].get("recognised", 0)
    assert recognised >= 1, "the case: the quad under the floor with its source's structure on it"
    said = ("by its source's structure there", "recognised by its structure")
    facts = [line for line in _facts(render_, gate, key) if any(words in line for words in said)]
    assert facts and all(line.startswith("fact: ") for line in facts)


def test_the_same_dark_dashboard_absent_on_its_join_is_missing():
    key = key_of("b")
    probe = _DarkRender(clip_document(list(CROSSFADE)), wall=tiled_booth)
    placed = insert_checks.placed_inserts(probe.document, probe.inserts)
    gate = probe.gate()
    # Its first frames judged inside the crossfade: shown enough of it, its scene's own. On a wall the
    # encoder leaves textured: the screen alone flat is the insert missing.
    judged = [
        n for n in placed[key].whole_frames() if placed[key].blend_at(n) is not None and placed[key].owns_frame(n, gate._placed[key])
    ]
    removed = set(judged[:3])
    assert len(removed) == 3
    render_ = _DarkRender(clip_document(list(CROSSFADE)), wall=tiled_booth, removed=removed)
    result = render_.gate().result()
    hits = [hit for hit in result.findings if hit[0].data["insert"] == key]
    assert hits, "the surface in the bezel's colour under a declared insert"
    (finding, _proof), *_ = hits
    assert finding.kind == INSERT_MISSING and finding.severity == FAIL and finding.data["why"] == "flat"
    assert finding.data["frame"] == min(removed) and finding.data["frames"] == len(removed)
    assert result.by_key[key]["joins_missing"] == len(removed)


def test_two_readings_of_one_scene_agree():
    key = key_of("b")
    first = _DarkRender(clip_document(list(CROSSFADE))).gate().result()
    second = _DarkRender(clip_document(list(CROSSFADE))).gate().result()
    assert [f.message for f, _ in first.findings] == [f.message for f, _ in second.findings] == []
    assert first.by_key[key] == second.by_key[key]


# ── Judged against the frame around it ────────────────────────────────────


def test_on_a_wall_the_blur_leaves_textured_the_quad_is_present_when_its_detail_falls_with_the_frame_s(monkeypatch):
    _unrecognised(monkeypatch)
    key = key_of("b")
    assert _probe(BLUR, tiled_booth)[key][1]
    render_ = PresenceRender(clip_document(list(BLUR)), wall=tiled_booth)
    gate = render_.gate()
    result = gate.result()
    assert [finding.message for finding, _ in result.findings] == []
    (dimmed,) = result.by_key[key]["dimmed"]
    # The frame around the quad stays over the floor: judged by how much of its detail the blur left.
    assert dimmed["whole_frame"] < dimmed["frames"]
    assert any("with the detail of the frame around them" in line for line in _facts(render_, gate, key))


def test_on_a_textured_wall_the_screen_alone_flat_inside_the_blur_is_missing():
    key = key_of("b")
    render_ = PresenceRender(clip_document(list(BLUR)), wall=tiled_booth, removed=set(_probe(BLUR, tiled_booth)[key][0][:5]))
    result = render_.gate().result()
    (finding, _proof), *_ = [hit for hit in result.findings if hit[0].data["insert"] == key]
    assert finding.kind == INSERT_MISSING and finding.data["why"] == "flat" and finding.data["frames"] == 5


def test_frame_attenuation_reads_the_median_block_and_needs_texture():
    reference = np.full((9, 16), 20.0, dtype=np.float32)
    blocks = reference * 0.1
    blocks[0, :3] = 20.0  # a caption over three blocks, as sharp as before
    assert insert_checks.frame_attenuation(blocks, reference) == pytest.approx(0.1)
    flat = np.full((9, 16), 0.5, dtype=np.float32)
    assert insert_checks.frame_attenuation(blocks, flat) is None
    assert insert_checks.around_detail(np.full((9, 16), np.nan, dtype=np.float32)) == 0.0


def test_the_windows_a_transition_or_a_fade_dims_its_scene_on():
    document = clip_document(list(BLUR))
    document["fade_in"], document["fade_out"] = 0.5, 0.4
    # The frames both scenes are painted on: the page's applyTransition blurs nothing once the scene it leaves
    # (to 2.2 s) is no longer painted, whatever is left of the blur's 0.55 s.
    first, end = math.ceil(1.7 * FPS), math.ceil(2.2 * FPS)
    assert document["frames"] == 77
    assert insert_checks.blend_windows(document, 1) == ((first, end, "blur in"), (0, 12, "video fade in"), (67, 77, "video fade out"))
    assert insert_checks.blend_windows(document, 0)[0] == (first, end, "blur out")
    cut = clip_document([("a", 0.0, 2.2, 2.0, "cut", 0.0), ("b", 2.2, 4.2, 2.0, "cut", 0.0)])
    assert insert_checks.blend_windows(cut, 1) == ()


# ── What nothing in the spec causes is the harness's, said once ───────────


def _missing(span: list[float], *, insert: str = "insert/b.0", scene: str = "b") -> Finding:
    fix = (
        "nothing in the spec causes it: render again, and report the render with this frame if it stays - an insert "
        "that runs to its scene's end is shown to the scene's last frame"
    )
    return Finding(
        INSERT_MISSING,
        span[0],
        span[1],
        scene,
        f"insert missing at t=0:02.00: Scene b: its screen insert 0 - its quad is one flat colour - {fix}",
        FAIL,
        {"insert": insert, "frame": 48, "span": span, "gate": "presence", "action": insert_checks.action(insert_checks.NOTHING, fix)},
        fix=fix,
    )


def test_a_finding_nothing_in_the_spec_causes_is_the_harness_s_said_once_with_its_frame():
    missing = _missing([2.0, 2.167])
    kept, harness = render_checks.harness_defects([missing], {})
    # The same object, so the render stores its frame as any finding's.
    assert kept == [] and harness == [missing] and harness[0] is missing and missing.data["said"] is True
    missing.data["frame_file_id"] = "frame-1"
    checks = render_checks.Checks(harness=harness)
    assert not checks.failed and checks.findings == []
    stored = checks.as_dict()
    assert stored["findings"] == [] and [item["data"]["insert"] for item in stored["harness"]] == ["insert/b.0"]
    assert render_checks.open_findings(stored, same_quality=True) == [] and render_checks.scene_passed(stored, "b")
    # Its frame is the render's file, deleted with it, attached to the answer.
    assert render_checks.frame_file_ids(stored) == ["frame-1"]
    # Said to the administrator, never among the answer's findings.
    answer = render_answer.of_checks(stored)
    assert answer.findings == [] and answer.status == render_answer.GREEN
    (said,) = answer.infra_once
    assert said.startswith("render harness, 0:02.00 scene `b` (insert_missing): insert missing at t=0:02.00: ")
    assert "(frame: file `frame-1`)" in said and said.endswith("no finding of the video, it blocks nothing")
    assert "Platform (said once, for the administrator; no finding):" in answer.text()
    # Kept on the scene's drawing, which the next drawing of the same digest reads.
    previews = render_checks.record_seen({}, {"b": "digest-b"}, "render-1", checks=stored)
    assert previews["b"]["drawings"][0]["unexplained"] == [{"kind": INSERT_MISSING, "insert": "insert/b.0", "span": [2.0, 2.167]}]
    assert previews["b"]["drawings"][0]["passed"] is True
    assert render_checks.Checks.from_dict(stored).harness[0].data["said"] is True


def test_the_same_frames_again_on_the_same_digest_are_never_said_again_nor_a_block():
    _kept, first = render_checks.harness_defects([_missing([2.0, 2.167])], {})
    previews = render_checks.record_seen({}, {"b": "digest-b"}, "render-1", checks=render_checks.Checks(harness=first).as_dict())
    before = render_checks.unexplained_before(previews, {"b": "digest-b"})
    kept, (again,) = render_checks.harness_defects([_missing([2.042, 2.167])], before)
    assert kept == [] and again.severity == FAIL and again.data["said"] is False and again.data["repeated_from"] == "render-1"
    answer = render_answer.of_checks(render_checks.Checks(harness=[again]).as_dict())
    assert answer.findings == [] and answer.infra_once == []
    assert [fact["what"] for fact in answer.facts] == [
        "the render harness's defect render render-1 said, on the same frames: insert_missing at 0:02.04"
    ]
    # Other frames, another insert or another digest: said, as the first time.
    assert render_checks.harness_defects([_missing([3.0, 3.1])], before)[1][0].data["said"] is True
    assert render_checks.harness_defects([_missing([2.0, 2.167], insert="insert/b.1")], before)[1][0].data["said"] is True
    assert render_checks.unexplained_before(previews, {"b": "digest-b2"}) == {}


def test_a_finding_the_spec_causes_stays_a_finding_and_one_stored_before_blocks_nothing():
    finding = _missing([2.0, 2.167])
    finding.data["action"] = insert_checks.action(insert_checks.REPLACE_SOURCE, "give the insert a source opaque there")
    assert render_checks.harness_defects([finding], {}) == ([finding], [])
    # A render stored earlier kept it among its findings, failing: no gate holds it.
    stored = {"findings": [_missing([2.0, 2.167]).as_dict()]}
    assert render_checks.open_findings(stored, same_quality=True) == [] and render_checks.scene_passed(stored, "b")
    assert render_checks.picture_findings(stored, "b") == []
