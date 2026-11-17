"""Every frame of a screen insert read by its gates, said, and looked at whole (dev:video-render-wide-insert-gate).

On synthetic audits and frames, no provider, no browser:

* a 10 s insert at 24 fps whose source shows through the foreground on frames
  120 to 124 only - its first, middle-but-one and last frames clean - fails
  as ``insert_through_foreground`` at frame 120's time, its worst frame and
  its run named, and its report says every frame was read and where the worst
  one is;
* a mask 0.3 sure on 8 frames in a row in the middle of an otherwise clean
  10 s insert - 3 % of its frames, under ``GHOST_FRAME_SHARE`` - is
  ``insert_ghost`` naming the run; 5 frames in a row stay a fact;
* an insert drawn on fewer frames than it is planned on says it was sampled;
* ``insert_sweep.sheets`` on a 3-minute video with 8 insert scenes returns 8
  contact sheets and the table in one call, the frames read once, the failing
  tile framed and named.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np
from PIL import Image

from app.services.video_studio import insert_checks, insert_sweep, quality, render_checks

FPS = 24.0
FRAMES = 240
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"


def _clean(n: int) -> dict:
    return {"covered": 0.0, "through": 0.0, "front_confidence": 0.95, "foreground_share": 0.2}


def _placed(frames: int = FRAMES, *, expected: tuple[tuple[int, int], ...] = ()) -> dict:
    return {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, frames / FPS, expected=expected)}


def test_a_stroke_through_the_foreground_on_five_middle_frames_fails_at_its_time_with_its_run():
    frames = {n: _clean(n) for n in range(FRAMES)}
    for n in range(120, 125):
        frames[n]["through"] = 0.004 + 0.001 * (n - 120)
    # The three moments the crops are cut at are clean.
    assert all(frames[n]["through"] == 0.0 for n in (0, FRAMES // 2 - 1, FRAMES - 1))
    audits = {KEY: {"frames": frames, "evidence": {}}}
    found = [f for f, _proof in insert_checks.findings(audits, _placed(), fps=FPS) if f.kind == render_checks.INSERT_THROUGH_FOREGROUND]
    [finding] = found
    assert finding.severity == render_checks.FAIL
    assert finding.at == 120 / FPS and finding.end == 124 / FPS
    assert finding.data["worst_frame"] == 124 and finding.data["frames"] == 5
    assert finding.data["run"] == [120, 124] and finding.data["of"] == FRAMES
    assert finding.message.startswith(f"insert seen through the foreground at t={render_checks.clock(124 / FPS)}")
    [report] = insert_checks.reports(audits, _placed(), fps=FPS)
    read = report["coverage"]
    assert read["mode"] == insert_checks.EVERY_FRAME and read["checked"] == read["of"] == FRAMES
    assert read["over"] == 5 and read["run"] == [120, 124] and read["worst_gates"] == ["through"]
    assert read["worst_at"] == round(120 / FPS, 3)
    line = insert_checks.coverage_words(report)
    assert line is not None and line.startswith("its gates read 240 of 240 frames (every frame); 5 over a gate")
    assert any(m.text == line for m in insert_checks.classify(report))


def test_a_ghost_on_eight_frames_in_a_row_is_insert_ghost_naming_the_run():
    frames = {n: _clean(n) for n in range(FRAMES)}
    for n in range(116, 124):
        frames[n]["front_confidence"] = 0.3
    assert 8 / FRAMES < insert_checks.GHOST_FRAME_SHARE
    audits = {KEY: {"frames": frames, "evidence": {}}}
    [finding] = [f for f, _proof in insert_checks.findings(audits, _placed(), fps=FPS) if f.kind == render_checks.INSERT_GHOST]
    assert finding.severity == render_checks.FAIL and finding.data["run"] == [116, 123]
    assert finding.data["frames"] == 8 and finding.data["of"] == FRAMES
    assert "its longest run frames 116-123 (8 in a row" in finding.message
    # Five in a row stay a fact: a matte's doubt for a fifth of a second is said, never a finding.
    for n in range(116, 119):
        frames[n]["front_confidence"] = 0.95
    assert not [f for f, _proof in insert_checks.findings(audits, _placed(), fps=FPS) if f.kind == render_checks.INSERT_GHOST]


def test_an_insert_drawn_on_fewer_frames_than_planned_says_it_was_sampled():
    frames = {n: _clean(n) for n in range(0, FRAMES, 2)}
    audits = {KEY: {"frames": frames, "evidence": {}}}
    [report] = insert_checks.reports(audits, _placed(expected=((0, FRAMES),)), fps=FPS)
    read = report["coverage"]
    assert read["mode"] == insert_checks.SAMPLED and read["checked"] == FRAMES // 2 and read["of"] == FRAMES
    assert read["over"] == 0 and "worst_frame" not in read
    assert insert_checks.coverage_words(report) == "its gates read 120 of 240 frames (sampled)"
    assert quality.QUALITY_REPORT_VERSION >= 26


# ── The sweep: a whole insert in one picture per scene ─────────────────────

W, H = 640, 360
BOX = [0.25, 0.25, 0.75, 0.75]


def _reports(scenes: int, seconds_each: float) -> list[dict]:
    out = []
    for index in range(scenes):
        first = index * seconds_each + 1.0
        last = first + seconds_each - 2.0
        report = {
            "key": f"insert/s{index}.0",
            "label": f"Scene s{index}: its screen insert 0",
            "scene_id": f"s{index}",
            "cropped_within": [first, last],
            "crops": [{"frame": round(first * FPS), "at": first, "word": "first", "box": BOX}],
            "coverage": {"mode": "every_frame", "checked": 10, "of": 10, "over": 0},
        }
        if index == 3:
            bad = round((first + 4.0) * FPS)
            report["coverage"].update(
                {"over": 1, "worst_frame": bad, "worst_at": round(bad / FPS, 3), "worst_gates": ["through"], "run": [bad, bad], "over_frames": {str(bad): ["through"]}}
            )
        out.append(report)
    return out


def test_the_sweep_of_a_3_minute_video_with_8_insert_scenes_is_8_sheets_in_one_call(tmp_path):
    reports = _reports(8, 22.5)
    calls: list[list[int]] = []
    picture = tmp_path / "frame.png"
    Image.fromarray(np.full((H, W, 3), 90, dtype=np.uint8)).save(picture)

    async def read(frames: list[int]) -> dict[int, str]:
        calls.append(frames)
        return dict.fromkeys(frames, str(picture))

    started = time.monotonic()
    found, skipped = asyncio.run(insert_sweep.sheets(reports, read, rate=FPS, step=1.0, out_dir=str(tmp_path)))
    assert time.monotonic() - started < 60.0
    assert len(calls) == 1 and skipped == []
    assert [sheet.scene_id for sheet in found] == [f"s{n}" for n in range(8)]
    for sheet in found:
        [sweep] = sheet.inserts
        # 1 s apart through its 20.5 s span, its last moment included.
        assert len(sweep.tiles) == 22 and sweep.tiles[1].at - sweep.tiles[0].at == 1.0
        with Image.open(sheet.path) as shown:
            # Each tile is the region at 100 %: half the frame each way, four across.
            assert shown.width >= 4 * (W // 2)
    flagged = [tile for tile in found[3].inserts[0].tiles if tile.gates]
    assert [tile.gates for tile in flagged] == [("through",)]
    row = found[3].as_dict()["inserts"][0]
    assert row["coverage"]["worst_gates"] == ["through"] and "over_frames" not in row["coverage"]


def test_a_long_insert_s_step_is_widened_to_fit_and_an_insert_without_a_box_is_said(tmp_path):
    [report] = _reports(1, 200.0)
    sweep = insert_sweep.plan(report, rate=FPS, step=1.0)
    assert sweep is not None and sweep.widened and len(sweep.tiles) <= insert_sweep.MAX_TILES + 1
    assert insert_sweep.step_of(None) == 1.0 and insert_sweep.step_of(0.01) == insert_sweep.MIN_STEP_SECONDS
    assert insert_sweep.step_of("x") == 1.0
    boxless = {**report, "crops": []}

    async def read(frames: list[int]) -> dict[int, str]:
        return {}

    found, skipped = asyncio.run(insert_sweep.sheets([boxless], read, rate=FPS, step=1.0, out_dir=str(tmp_path)))
    assert found == [] and skipped and "no region box" in skipped[0]
    assert not list(Path(tmp_path).glob("sweep-*.png"))
