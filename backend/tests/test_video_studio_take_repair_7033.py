"""A finished take repaired without recording it again: what it was made from kept and read back, and the
frames of its MP4 chosen as its keyframes when it was recorded before they were kept."""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path

import pytest

from app.services.video_studio import canvas_alone, scroll_pacing, take_repair


def _write(workdir: Path, name: str, data: bytes = b"\xff\xd8jpeg") -> str:
    (workdir / name).write_bytes(data)
    return name


def test_keyframes_round_trip(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    frames = [(0.0, _write(src, "f0.jpg", b"a")), (0.1, _write(src, "f1.jpg", b"b"))]
    alone = [canvas_alone.AloneFrame(0.05, "1,2,3,4", "canvas#g", (1.0, 2.0, 3.0, 4.0), ((5.0, 6.0, 7.0, 8.0),), _write(src, "c0.png", b"c"))]
    events = [{"step": 1, "scrolls": [{"t": [0.0, 0.1], "samples": [[0.0, 0, 0], [0.1, 40, 0]]}]}]
    packed = take_repair.pack_keyframes(
        tmp_path / "k.zip",
        workdir=src,
        frames=frames,
        events=events,
        duration=0.2,
        pixel_ratio=2.0,
        viewport=(800, 450),
        fps=30,
        cursor=[[0.0, 1.0, 2.0]],
        painted={"f0.jpg": 0.0, "f1.jpg": 40.0},
        capture={"gl": {"gpu": False}},
        alone=alone,
    )
    assert packed == 3
    source = take_repair.unpack_keyframes(tmp_path / "k.zip", dst)
    assert source.frames == frames
    assert source.events == events
    assert (source.duration, source.pixel_ratio, source.viewport, source.fps) == (0.2, 2.0, (800, 450), 30)
    assert source.painted == {"f0.jpg": 0.0, "f1.jpg": 40.0}
    assert source.capture == {"gl": {"gpu": False}}
    assert source.alone == alone
    assert (dst / "f1.jpg").read_bytes() == b"b"
    assert (dst / "c0.png").read_bytes() == b"c"


def test_unpack_refuses_unknown_version_and_paths(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr(take_repair.MANIFEST, json.dumps({"version": 99}))
    with pytest.raises(take_repair.KeyframesError):
        take_repair.unpack_keyframes(bad, tmp_path)
    escape = tmp_path / "escape.zip"
    with zipfile.ZipFile(escape, "w") as archive:
        archive.writestr(take_repair.MANIFEST, json.dumps({"version": 1, "frames": []}))
        archive.writestr("frames/../evil.jpg", b"x")
    with pytest.raises(take_repair.KeyframesError):
        take_repair.unpack_keyframes(escape, tmp_path / "out")
    with pytest.raises(take_repair.KeyframesError):
        take_repair.unpack_keyframes(tmp_path / "missing.zip", tmp_path)


def test_unpack_refuses_missing_keyframe(tmp_path: Path) -> None:
    path = tmp_path / "k.zip"
    manifest = {"version": 1, "frames": [[0.0, "gone.jpg"]], "duration": 1, "pixel_ratio": 1, "viewport": [1, 1], "fps": 30}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(take_repair.MANIFEST, json.dumps(manifest))
    with pytest.raises(take_repair.KeyframesError, match="missing"):
        take_repair.unpack_keyframes(path, tmp_path)


def test_raw_scroll_window_reads_back_as_the_kept_log() -> None:
    kept = {
        "t": [1.0, 2.0],
        "scroller": "window",
        "log": [[1.0, 0.0], [1.5, 200.0], [2.0, 400.0]],
        "wheel": [[1.0, 0.0], [1.4, 0.5], [2.0, 1.0]],
        "paced": True,
        "fps": 60.0,
    }
    raw = take_repair.raw_scroll(kept)
    assert raw is not None
    [scroll] = scroll_pacing.read_scrolls(raw)
    assert scroll.scroller == "window"
    assert [(round(t, 3), round(v, 1)) for t, v in scroll.log] == [(1.0, 0.0), (1.5, 200.0), (2.0, 400.0)]
    assert scroll.wheel is not None and [round(s, 3) for _t, s in scroll.wheel] == [0.0, 0.5, 1.0]


def test_raw_scroll_container_keeps_its_box_and_name() -> None:
    kept = {"t": [0.0, 1.0], "scroller": "element", "element": "main#content", "box": [0, 100, 800, 300], "log": [[0.0, 0.0], [1.0, 120.0]]}
    [scroll] = scroll_pacing.read_scrolls(take_repair.raw_scroll(kept) or {})
    assert (scroll.scroller, scroll.element, scroll.box) == ("element", "main#content", (0.0, 100.0, 800.0, 300.0))
    assert scroll.log[-1][1] == 120.0


def test_raw_scroll_canvas_zoom_keeps_its_progress() -> None:
    log = [[0.0, 0.0], [0.5, 0.3], [1.0, 0.6]]
    kept = {"t": [0.0, 1.0], "scroller": "canvas", "box": [100, 50, 500, 300], "progress": "transform", "log": log, "anchor": [300, 200]}
    [scroll] = scroll_pacing.read_scrolls(take_repair.raw_scroll(kept) or {})
    assert (scroll.scroller, scroll.progress, scroll.anchor) == ("canvas", "transform", (300.0, 200.0))
    assert [round(v, 4) for _t, v in scroll.log] == [0.0, 0.3, 0.6]
    wheel = {**kept, "progress": "wheel", "log": [[0.0, 0.0], [1.0, -240.0]]}
    [by_wheel] = scroll_pacing.read_scrolls(take_repair.raw_scroll(wheel) or {})
    assert (by_wheel.progress, by_wheel.log[-1][1]) == ("wheel", -240.0)


def test_raw_scroll_refuses_what_it_cannot_rebuild() -> None:
    assert take_repair.raw_scroll({"t": [0, 1], "log": [[0, 0]]}) is None
    assert take_repair.raw_scroll({"t": [0, 1], "scroller": "element", "log": [[0, 0], [1, 5]]}) is None


def test_raw_events_drop_what_the_passes_wrote() -> None:
    events = [
        {
            "step": 3,
            "t0": 1.0,
            "fps": 12.0,
            "painted_fps": 8.0,
            "capture_fps": 9.0,
            "scrolls": [{"t": [1.0, 2.0], "scroller": "window", "log": [[1.0, 0.0], [2.0, 100.0]], "paced": True}],
            "motion": [{"t": [2.1, 2.4], "element": "div.toast", "paced": False}],
        }
    ]
    [raw] = take_repair.raw_events(events)
    assert not take_repair.PASS_FIELDS & raw.keys()
    assert "samples" in raw["scrolls"][0]
    assert raw["motion"] == [[2.1, 2.4, "div.toast"]]
    assert events[0]["fps"] == 12.0 and "samples" not in events[0]["scrolls"][0]


def test_select_painted_keeps_changes_and_the_painted_rate() -> None:
    times = [i / 60 for i in range(121)]  # 2 s at 60 fps
    changed = [True] * len(times)
    for i in range(10, 20):
        changed[i] = False  # a hold before the scroll
    span = take_repair.Span(start=0.5, end=1.5, painted=10.0)
    picked = take_repair.select_painted(times, changed, spans=[span])
    inside = [i for i in picked if 0.5 - 1e-6 <= times[i] <= 1.5 + 1e-6]
    assert len(inside) == 11  # the span's start, nine paints between, its end
    assert all(math.isclose(times[i] * 10, round(times[i] * 10), abs_tol=1 / 60 * 10) for i in inside)
    assert not set(range(10, 20)) & set(picked)
    assert 0 in picked and 120 in picked


def test_select_painted_drops_off_scale_runs() -> None:
    times = [i / 60 for i in range(60)]
    picked = take_repair.select_painted(times, [True] * 60, faults=[(0.2, 0.3)])
    # Half-open: [0.2, 0.3) dropped, the frame at 0.3 is the first valid one after the run.
    assert not [i for i in picked if 0.2 - 1e-6 <= times[i] < 0.3 - 1e-6]
    assert 11 in picked and 18 in picked


def test_spans_read_from_a_finished_take() -> None:
    events = [
        {"scrolls": [{"t": [1.0, 2.0], "paced": True, "painted": 7.5}, {"t": [3, 4], "paced": False}]},
        {"motion": [{"t": [5.0, 5.5], "paced": True, "painted": 4.0}]},
    ]
    assert take_repair.paced_spans(events) == [take_repair.Span(1.0, 2.0, 7.5), take_repair.Span(5.0, 5.5, 4.0)]
    assert take_repair.fault_spans({"off_scale": [{"t": [1.0, 1.2]}, {"bad": 1}]}) == [(1.0, 1.2)]


def test_a_queued_repair_names_the_take_it_repairs() -> None:
    assert take_repair.repair_source({take_repair.REPAIR_KEY: "abc"}) == "abc"
    assert take_repair.repair_source({}) is None and take_repair.repair_source(None) is None
    finished = {"gl": {"gpu": False}, "pixel_ratio": 2, "low": [1], "stepping": [2], "off_scale": [3], "keyframes": "f"}
    summary = take_repair.recorded_summary(finished)
    assert summary == {"gl": {"gpu": False}, "pixel_ratio": 2}
    summary["gl"]["gpu"] = True
    assert finished["gl"]["gpu"] is False  # a copy, the finished take's capture untouched


def test_changed_flags_ignore_encode_noise() -> None:
    still = bytes([100] * 16)
    noisy = bytes([102] * 16)  # within the crf 14 noise
    moved = bytes([100] * 8 + [180] * 8)
    assert take_repair.changed_flags([still, noisy, moved, moved, b"\x00" * 4]) == [True, False, True, False, True]


def test_a_repair_says_what_it_repaired_and_its_residual() -> None:
    events = [{"scrolls": [{"t": [1.0, 2.0], "paced": True, "painted": 7.5}]}]
    spans = take_repair.repaired(events, [(0.4, 0.6)])
    assert [s["t"] for s in spans] == [[0.4, 0.6], [1.0, 2.0]]
    assert "dropped" in spans[0]["what"] and spans[1]["painted"] == 7.5
    exact = take_repair.residual(take_repair.FROM_KEYFRAMES, recorded={}, notes=[])
    assert exact == []
    approx = take_repair.residual(
        take_repair.FROM_MP4, recorded={"stepping": [{"canvas": "canvas#g"}]}, notes=["stepping motion at step 4", ""]
    )
    assert len(approx) == 3 and approx[-1] == "stepping motion at step 4"
    assert "canvases were not copied alone" in approx[1]
    said = take_repair.repair_words(
        {take_repair.REPAIRED_KEY: {"of": "t1", "from": take_repair.FROM_MP4, "spans": spans, "residual": approx}}
    )
    assert "`t1`" in said and "its MP4 (approximate)" in said and "record it again for these" in said
    clean = take_repair.repair_words(
        {take_repair.REPAIRED_KEY: {"of": "t2", "from": take_repair.FROM_KEYFRAMES, "spans": [], "residual": []}}
    )
    assert "its keyframes (exact)" in clean and "No residual" in clean
    assert take_repair.repair_words({}) == ""


def test_the_quality_findings_offer_a_repair() -> None:
    from app.services.video_studio import quality

    assert quality.QUALITY_REPORT_VERSION >= 31
