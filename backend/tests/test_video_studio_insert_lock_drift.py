"""A static screen insert's bezel is read on every frame against its lock. Synthetic frames only.

A locked-off AI shot whose camera drifted a few percent over its seconds, placed with a fixed quad (no ``track``),
passed every insert gate at 0.00% while its last crop showed the monitor some 150 px left of the insert: the quad is
locked once, on the first footage frame, and the alignment the audit kept was that frame's on all of
them. Only ``track: true`` let the gate see it, two previews later.

* on every matted frame of a locked or filled insert the bezel the frame shows is read against the lock
  (``screen_insert._drift_of``) and kept on the audit (``alignment_drift``, ``drift_sides``, ``bezel_found``), never
  drawn: the quad drawn stays the lock's;
* a still camera reads the lock on every frame: no drift, no finding; a tracked insert carries no drift field;
* a bezel moving from under the lock past ``ALIGNMENT_MAX_SHARE`` on ``PERSIST_FRAMES`` frames is ``insert_alignment``
  on the frame it moved farthest, with the drift's sides and one action (track it, or lock the shot off), on an unkeyed
  and on a keyed static insert alike; the report keeps the worst drift and says it;
* the drawing version moved: a render drawn before it is drawn again.
"""

from __future__ import annotations

import dataclasses
from itertools import pairwise
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import insert_checks, screen_insert, surface_refine
from app.services.video_studio.render_checks import INSERT_ALIGNMENT

W, H = 640, 360
QUAD = ((0.30, 0.22), (0.72, 0.22), (0.72, 0.62), (0.30, 0.62))
KEY = "insert/s1.0"
#: Two pixels a frame: 0.31% of the frame's width each, past the gate on the second frame, 3.4% on the twelfth.
DRIFT_PX = 2


def _room(shift: int = 0, *, green: bool = False) -> np.ndarray:
    """A room with a bezelled monitor, moved *shift* pixels to the right (the camera drifting left)."""
    picture = np.zeros((H, W, 3), dtype=np.uint8)
    picture[...] = np.linspace(40, 120, W, dtype=np.uint8)[None, :, None]
    x0, y0, x1, y1 = int(0.30 * W) + shift, int(0.22 * H), int(0.72 * W) + shift, int(0.62 * H)
    picture[y0 - 6 : y1 + 6, x0 - 6 : x1 + 6] = (60, 60, 64)
    picture[y0:y1, x0:x1] = (0, 177, 64) if green else (16, 20, 24)
    return picture


def _insert(**fields) -> screen_insert.InsertSource:
    values = {"path": "insert.png", "kind": "image", "quad": QUAD, "label": "Scene s1: its screen insert 0", "fit": "contain"}
    values.update(fields)
    insert = screen_insert.InsertSource(**values)
    return dataclasses.replace(insert, box=tuple(screen_insert.grown_box(insert.quad, W / H)))


def _picture() -> Image.Image:
    rng = np.random.default_rng(3)
    return Image.fromarray(rng.integers(0, 255, (300, 400, 3), dtype=np.uint8), "RGB")


def _placed(insert: screen_insert.InsertSource) -> dict:
    keyed = insert.key is not None
    return {
        KEY: insert_checks.Placed(
            KEY,
            "s1",
            insert.label,
            0.0,
            1.0,
            keyed=keyed,
            fit=insert.fit,
            key_settings=insert_checks.key_settings(insert.key) if keyed else None,
        )
    }


def _drawn(insert: screen_insert.InsertSource, shifts: list[int], *, green: bool = False) -> dict:
    """The audits of *insert* drawn on the room moved by *shifts*, frame by frame, as the frame server draws it."""
    first = _room(green=green)
    surface = screen_insert.Surface.of(first, [(x * W, y * H) for x, y in QUAD])
    lock = fill = None
    if insert.key is not None:
        fill = screen_insert.static_fill(insert, first, (W, H))
    else:
        lock = screen_insert.static_lock(insert, first, (W, H))
    state = screen_insert.Matting()
    audits = {}
    for n, shift in enumerate(shifts):
        layer = screen_insert.compose(
            insert, _picture(), (W, H), n, footage=_room(shift, green=green), surface=surface, fill=fill, lock=lock, state=state, window_frame=n
        )
        audits[str(n)] = layer.audit.as_dict()
    return audits


def _findings(insert: screen_insert.InsertSource, audits: dict) -> list:
    return [finding for finding, _proof in insert_checks.findings({KEY: {"frames": audits, "evidence": {}}}, _placed(insert), fps=24.0)]


def _report(insert: screen_insert.InsertSource, audits: dict) -> dict:
    [report] = insert_checks.reports({KEY: {"frames": audits, "evidence": {}}}, _placed(insert), fps=24.0)
    return report


def test_a_still_camera_reads_the_lock_on_every_frame_and_fails_nothing():
    insert = _insert()
    audits = _drawn(insert, [0] * 8)
    drifts = [audits[str(n)]["alignment_drift"] for n in range(8)]
    assert all(drift is not None and drift < surface_refine.ALIGNMENT_MAX_SHARE / 10 for drift in drifts), drifts
    assert {kind.kind for kind in _findings(insert, audits)} == set()
    report = _report(insert, audits)
    assert report["verdicts"]["alignment"] is True and report["drift_max"] < surface_refine.ALIGNMENT_MAX_SHARE / 10
    assert any(line.cls == insert_checks.FACT and "bezel read on every frame against the lock" in line.text for line in insert_checks.classify(report))


def test_a_bezel_moving_from_under_the_lock_is_measured_on_every_frame_and_never_drawn_after():
    insert = _insert()
    audits = _drawn(insert, [n * DRIFT_PX for n in range(12)])
    drifts = [audits[str(n)]["alignment_drift"] for n in range(12)]
    # The drift grows with the camera, two pixels a frame; the quad drawn and the lock's own error never move.
    assert all(later > earlier for earlier, later in pairwise(drifts)), drifts
    assert drifts[11] == pytest.approx(11 * DRIFT_PX / W, abs=0.001)
    assert audits["11"]["refined"] == audits["0"]["refined"]
    assert audits["11"]["alignment"] == audits["0"]["alignment"]
    # The bezel found on the last frame is the monitor where the room moved it.
    found = audits["11"]["bezel_found"]
    assert found[0][0] == pytest.approx(QUAD[0][0] + 11 * DRIFT_PX / W, abs=0.004)
    assert audits["11"]["drift_sides"]["left"] == pytest.approx(drifts[11], abs=0.001)


def test_a_bezel_moving_from_under_the_lock_fails_the_alignment_gate_naming_the_frame_and_the_fix():
    insert = _insert()
    audits = _drawn(insert, [n * DRIFT_PX for n in range(12)])
    [finding] = [finding for finding in _findings(insert, audits) if finding.kind == INSERT_ALIGNMENT]
    assert finding.data["worst"] == pytest.approx(11 * DRIFT_PX / W, abs=0.001)
    assert finding.data["worst_at"] == pytest.approx(11 / 24.0, abs=0.01)
    assert finding.data["sides"]["left"] == pytest.approx(finding.data["worst"], abs=0.001)
    assert finding.data["action"]["kind"] == insert_checks.GIVE_QUAD
    assert "moves from under the locked quad" in finding.message and "track: true" in finding.message
    assert "locked off" in finding.message
    report = _report(insert, audits)
    assert report["verdicts"]["alignment"] is False
    assert report["drift_max"] == pytest.approx(finding.data["worst"], abs=0.001) and report["drift_frame"] == 11
    lines = insert_checks.classify(report, {INSERT_ALIGNMENT})
    assert any(line.cls == insert_checks.FINDING and "moves at most 3.4" in line.text for line in lines), [line.text for line in lines]


def test_a_drift_the_save_never_measured_blocks_whatever_the_save_passed():
    insert = _insert()
    audits = _drawn(insert, [n * DRIFT_PX for n in range(12)])
    passed = {KEY: [{"stage": "save", "id": "revision 3", "gates": {"alignment": True, "flatness": True}}]}
    found = [f for f, _p in insert_checks.findings({KEY: {"frames": audits, "evidence": {}}}, _placed(insert), fps=24.0, passed=passed)]
    [finding] = [f for f in found if f.kind == INSERT_ALIGNMENT]
    assert finding.severity == insert_checks.FAIL and finding.data["drifted"] is True and "disagrees" not in finding.message


def test_a_keyed_static_insert_reads_its_drift_on_the_key_s_matte():
    insert = screen_insert.keyed_on(_insert(key=screen_insert.KeySpec("green")), _room(green=True))
    still = _drawn(insert, [0] * 6, green=True)
    assert all(still[str(n)]["alignment_drift"] < surface_refine.ALIGNMENT_MAX_SHARE / 10 for n in range(6))
    assert {finding.kind for finding in _findings(insert, still)} == set()
    moved = _drawn(insert, [n * DRIFT_PX for n in range(10)], green=True)
    assert moved["9"]["alignment_drift"] == pytest.approx(9 * DRIFT_PX / W, abs=0.001)
    assert [finding.kind for finding in _findings(insert, moved) if finding.kind == INSERT_ALIGNMENT]


def test_a_tracked_insert_and_an_unmatted_one_carry_no_drift():
    insert = _insert()
    first = _room()
    lock = screen_insert.static_lock(insert, first, (W, H))
    surface = screen_insert.Surface.of(first, [(x * W, y * H) for x, y in QUAD])
    tracked = dataclasses.replace(insert, track=SimpleNamespace(quad_at=lambda _frame: list(QUAD), box_quad=lambda: list(insert.box)))
    layer = screen_insert.compose(tracked, _picture(), (W, H), 0, footage=first, surface=surface, lock=lock, state=screen_insert.Matting())
    assert layer.audit.alignment_drift is None and layer.audit.bezel_found is None
    assert screen_insert.compose(insert, _picture(), (W, H)).audit.alignment_drift is None


def test_the_drift_is_in_the_audit_s_record_and_moved_the_drawing_version():
    audit = screen_insert.FrameAudit(alignment=0.001, alignment_drift=0.02, drift_sides={"left": 0.02}, bezel_found=[[0.3, 0.2]] * 4)
    record = audit.as_dict()
    assert record["alignment_drift"] == 0.02 and record["drift_sides"] == {"left": 0.02} and record["bezel_found"]
    assert screen_insert.FrameAudit.from_dict(record).misaligned
    assert insert_checks._alignment_of(record) == 0.02 and insert_checks._drifted(record)
    assert screen_insert.DRAWING_VERSION >= 9
