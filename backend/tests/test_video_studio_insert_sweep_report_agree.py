"""A sweep-failed insert frame, an empty join between two back-to-back inserts and a key line along an edge are all
findings of the render's report (issue #9065)."""

from __future__ import annotations

import numpy as np

from app.services.video_studio import insert_checks, insert_sweep, screen_insert
from app.services.video_studio.render_checks import FAIL, INSERT_ALIGNMENT, INSERT_GAP, Finding

# ── a. one source of truth between the sweep and the report ──


def _report(over: dict[str, list[str]], *, mode: str = insert_checks.EVERY_FRAME) -> dict:
    return {
        "key": "insert/s1.0",
        "label": "screen insert 1",
        "scene_id": "s1",
        "crops": [{"box": [0.1, 0.1, 0.5, 0.5], "at": 0.0}],
        "cropped_within": [0.0, 2.0],
        "coverage": {"mode": mode, "checked": 120, "of": 120, "over": len(over), "over_frames": over},
    }


def _finding(kind: str) -> Finding:
    return Finding(kind, 1.0, 1.1, "s1", f"{kind} at 0:01", FAIL, {"insert": "insert/s1.0"})


def test_a_frame_failing_a_gate_without_its_finding_is_an_advisory_said_in_the_facts() -> None:
    report = _report({"60": ["alignment"]})
    facts = insert_checks.reconcile_coverage([report], [])
    assert report["coverage"]["under_tolerance"] == {"alignment": 1}
    assert len(facts) == 1 and "alignment on 1 frame" in facts[0] and "advisory" in facts[0]


def test_a_frame_failing_a_gate_its_finding_holds_is_no_advisory() -> None:
    report = _report({"60": ["alignment"], "61": ["alignment"]})
    facts = insert_checks.reconcile_coverage([report], [_finding(INSERT_ALIGNMENT)])
    assert "under_tolerance" not in report["coverage"]
    assert report["coverage"]["findings"] == ["alignment"]
    assert facts == []


def test_a_finding_whose_frames_no_gate_fails_is_a_disagreement() -> None:
    report = _report({})
    facts = insert_checks.reconcile_coverage([report], [_finding(INSERT_ALIGNMENT)])
    assert len(facts) == 1 and "disagree" in facts[0] and "insert_alignment" in facts[0]


def test_a_sampled_coverage_never_says_a_disagreement() -> None:
    report = _report({}, mode=insert_checks.SAMPLED)
    assert insert_checks.reconcile_coverage([report], [_finding(INSERT_ALIGNMENT)]) == []


def test_the_sweep_frames_an_advisory_tile_apart_from_a_finding_tile() -> None:
    advisory = _report({"30": ["alignment"]})
    insert_checks.reconcile_coverage([advisory], [])
    sweep = insert_sweep.plan(advisory, rate=30.0, step=1.0)
    assert sweep is not None
    tile = next(t for t in sweep.tiles if t.frame == 30)
    assert tile.gates == ("alignment",) and tile.advisory and tile.row()["verdict"] == "advisory"

    held = _report({"30": ["alignment"]})
    insert_checks.reconcile_coverage([held], [_finding(INSERT_ALIGNMENT)])
    sweep = insert_sweep.plan(held, rate=30.0, step=1.0)
    assert sweep is not None
    tile = next(t for t in sweep.tiles if t.frame == 30)
    assert not tile.advisory and tile.row()["verdict"] == "finding"


def test_every_frame_gate_names_its_finding() -> None:
    for gate in ("alignment", "residual_key", "halo", "rim", "colour_shift", "sharpness", "flatness", "jitter"):
        assert insert_checks.FINDING_OF_GATE[gate].startswith("insert_")
    for gate in ("through", "ghost", "covered", "key_spill", "matte_halo", "matte_lag", "occluded"):
        assert gate in insert_checks.FINDING_OF_GATE


# ── b. an empty surface between two inserts back to back ──


def _document(first_t1: float, next_t0: float) -> dict:
    quad = [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6], [0.2, 0.6]]
    return {
        "fps": 60,
        "scenes": [
            {
                "id": "s1",
                "start": 0.0,
                "end": 6.0,
                "inserts": [
                    {"src": "insert/s1.0", "quad": quad, "t0": 0.0, "t1": first_t1},
                    {"src": "insert/s1.1", "quad": quad, "t0": next_t0, "t1": 6.0, "to_end": True},
                ],
            }
        ],
    }


def test_two_inserts_back_to_back_with_a_gap_report_insert_gap() -> None:
    # The first fades out over 0.2 s to 3.0 s; the next comes in at 3.1 s and fades in over 0.2 s: 6 frames at 60 fps
    # or more show neither past a quarter.
    found = insert_checks.insert_gaps(_document(3.0, 3.1), fps=60.0)
    assert len(found) == 1
    gap = found[0]
    assert gap.kind == INSERT_GAP and gap.severity == FAIL and gap.scene_id == "s1"
    assert gap.data["frames"] >= 6 and gap.data["insert"] == "insert/s1.1" and gap.data["previous"] == "insert/s1.0"
    assert gap.data["action"]["kind"] == insert_checks.OVERLAP_INSERTS
    assert "overlap" in (gap.fix or "") and "cross-dissolve" in (gap.fix or "")
    assert 2.9 < gap.at < 3.2


def test_two_inserts_overlapping_by_their_fade_report_no_gap() -> None:
    assert insert_checks.insert_gaps(_document(3.0, 2.8), fps=60.0) == []


def test_two_inserts_far_apart_or_on_other_surfaces_report_no_gap() -> None:
    assert insert_checks.insert_gaps(_document(2.0, 4.0), fps=60.0) == []
    document = _document(3.0, 3.1)
    document["scenes"][0]["inserts"][1]["quad"] = [[0.7, 0.7], [0.9, 0.7], [0.9, 0.9], [0.7, 0.9]]
    assert insert_checks.insert_gaps(document, fps=60.0) == []


def test_a_preview_window_reads_only_its_frames() -> None:
    assert insert_checks.insert_gaps(_document(3.0, 3.1), fps=60.0, span=(0.0, 1.0)) == []
    assert len(insert_checks.insert_gaps(_document(3.0, 3.1), fps=60.0, span=(2.5, 1.0))) == 1


# ── c. a line of the key along an edge of the quad ──


def test_a_two_pixel_key_line_along_one_side_is_named() -> None:
    keyed = np.zeros((300, 400), dtype=bool)
    quad = np.array([[50.0, 50.0], [350.0, 50.0], [350.0, 250.0], [50.0, 250.0]])
    keyed[50:250, 348:350] = True  # 2 px inside the right edge
    lines = screen_insert.key_edge_lines(keyed, quad, 1.0)
    assert set(lines) == {"right"} and lines["right"] == 2


def test_a_key_line_past_the_edge_counts_too() -> None:
    keyed = np.zeros((300, 400), dtype=bool)
    quad = np.array([[50.0, 50.0], [350.0, 50.0], [350.0, 250.0], [50.0, 250.0]])
    keyed[50:250, 48:50] = True  # 2 px past the left edge, over the bezel
    assert screen_insert.key_edge_lines(keyed, quad, 1.0) == {"left": 2}


def test_scattered_key_pixels_along_an_edge_are_no_line() -> None:
    keyed = np.zeros((300, 400), dtype=bool)
    quad = np.array([[50.0, 50.0], [350.0, 50.0], [350.0, 250.0], [50.0, 250.0]])
    keyed[50:250:4, 348:350] = True  # a quarter of the edge
    assert screen_insert.key_edge_lines(keyed, quad, 1.0) == {}


def test_an_edge_line_fails_the_residual_key_whatever_its_share() -> None:
    audit = {"residual_key": 0.0005, "residual_edge": {"right": 2}}
    assert insert_checks.residual_past(audit)
    assert not insert_checks.residual_past({"residual_key": 0.0005})
    read = screen_insert.FrameAudit.from_dict(audit)
    assert read.residual_edge == {"right": 2}
    assert read.as_dict()["residual_edge"] == {"right": 2}
