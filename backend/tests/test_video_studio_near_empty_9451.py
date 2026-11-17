"""A recording's canvas left near empty past a beat, the cursor alone moving (issue #9451)."""

from __future__ import annotations

import numpy as np

from app.services.video_studio import near_empty
from app.services.video_studio.frame_freeze import Shown
from app.services.video_studio.near_empty import NearEmptyWatch, read_emptiness
from app.services.video_studio.render_checks import HELD_WARNINGS, NEAR_EMPTY

W, H = 192, 108
RATE = 25.0


def _shell(dark: bool = True) -> np.ndarray:
    """A product page: a canvas of one grey, a sidebar and a header of another (dark or light shell)."""
    frame = np.full((H, W), 245.0, dtype=np.float32)
    shell = 30.0 if dark else 200.0
    frame[:, :28] = shell
    frame[:12, :] = shell
    return frame


def _cursor(frame: np.ndarray, t: int) -> np.ndarray:
    out = frame.copy()
    x, y = 60 + (t * 3) % 100, 40 + (t * 2) % 50
    out[y : y + 3, x : x + 2] = 0.0
    return out


def _nodes(frame: np.ndarray, count: int) -> np.ndarray:
    out = frame.copy()
    for i in range(count):
        x, y = 40 + (i * 37) % 140, 20 + (i * 23) % 75
        out[y : y + 6, x : x + 6] = 90.0
    return out


def _graph(frame: np.ndarray) -> np.ndarray:
    out = _nodes(frame, 8)
    out[50, 40:120] = 120.0  # an edge across the canvas
    out[51, 40:120] = 120.0
    return out


def _one_dot(frame: np.ndarray) -> np.ndarray:
    out = frame.copy()
    out[60:64, 120:124] = 90.0  # the one node a hide step left
    return out


def _document(scenes: list[dict]) -> dict:
    return {"fps": RATE, "duration": max(s["end"] for s in scenes), "scenes": scenes}


def _watch(frames: list[np.ndarray], document: dict) -> list:
    shown = Shown.of(document, RATE, RATE)
    watch = NearEmptyWatch()
    for count, frame in enumerate(frames):
        watch.feed(frame, count, shown.scene(count))
    return near_empty.findings(watch.hits, RATE, document, shown=shown)


def test_a_cursor_alone_reads_as_an_empty_canvas_and_a_graph_does_not():
    page = _shell()
    assert read_emptiness(_cursor(page, 3)).share == 0.0
    assert read_emptiness(_one_dot(page)).share < near_empty.EMPTY_SHARE
    assert read_emptiness(_graph(page)).share >= near_empty.SHOWN_SHARE
    # A sparse graph of a few nodes is content, never an empty canvas.
    assert read_emptiness(_nodes(page, 3)).share >= near_empty.EMPTY_SHARE


def test_the_shell_never_counts_as_content_dark_or_light():
    for dark in (True, False):
        read = read_emptiness(_cursor(_shell(dark), 5))
        assert read.share == 0.0 and read.canvas > 0.5, dark


def test_a_canvas_emptied_by_a_hide_step_and_held_is_a_finding_with_a_trim():
    page = _shell()
    graph = [_cursor(_graph(page), t) for t in range(50)]  # 2 s of a drawn graph
    hidden = [_cursor(_one_dot(page), t) for t in range(50, 88)]  # 1.52 s near empty, to the scene's end
    scene = {
        "id": "s3",
        "type": "recording",
        "start": 0.0,
        "end": 88 / RATE,
        "time_map": [[0.0, 10.0], [88 / RATE, 10.0 + 88 / RATE]],
    }
    found = _watch(graph + hidden, _document([scene]))
    assert [(f.kind, f.scene_id, f.severity) for f in found] == [(NEAR_EMPTY, "s3", "warn")]
    finding = found[0]
    assert abs(finding.at - 2.0) < 0.05 and abs(finding.end - 88 / RATE) < 0.05
    assert finding.data["trim_end"] == 12.0 and finding.data["source_seconds"][0] == 12.0
    assert "trim_end to 12.00 s" in finding.fix
    assert NEAR_EMPTY in HELD_WARNINGS


def test_a_short_empty_beat_a_sparse_graph_and_a_statement_are_never_judged():
    page = _shell()
    # Under the threshold: 0.6 s of an empty canvas between two drawn stretches.
    short = [_cursor(_graph(page), t) for t in range(25)] + [_cursor(page, t) for t in range(15)]
    short += [_cursor(_graph(page), t) for t in range(25)]
    scene = {"id": "s1", "type": "recording", "start": 0.0, "end": 65 / RATE}
    assert _watch(short, _document([scene])) == []
    # A sparse graph held for 3 s.
    sparse = [_cursor(_nodes(page, 3), t) for t in range(75)]
    assert _watch(sparse, _document([{**scene, "end": 3.0}])) == []
    # A statement card that empties: never a recording, never judged.
    card = [_graph(page)] * 25 + [page] * 50
    assert _watch(card, _document([{"id": "s2", "type": "statement", "start": 0.0, "end": 3.0}])) == []


def test_a_page_empty_from_its_first_frame_is_no_emptied_canvas():
    page = _shell()
    frames = [_cursor(page, t) for t in range(50)]
    assert _watch(frames, _document([{"id": "s1", "type": "recording", "start": 0.0, "end": 2.0}])) == []


def test_an_empty_span_inside_a_crossfade_is_excused_and_mid_scene_names_the_step():
    page = _shell()
    frames = [_cursor(_graph(page), t) for t in range(25)] + [_cursor(page, t) for t in range(30)]
    frames += [_cursor(_graph(page), t) for t in range(25)]
    scene = {
        "id": "s1",
        "type": "recording",
        "start": 0.0,
        "end": 80 / RATE,
        "time_map": [[0.0, 0.0], [80 / RATE, 80 / RATE]],
    }
    found = _watch(frames, _document([scene]))
    assert len(found) == 1 and "cut the step" in found[0].fix
    assert "trim_end" not in found[0].data and "trim_start" not in found[0].data
    fade = {**_document([scene]), "fade_in": 3.0}
    assert _watch(frames, fade) == []


def test_the_watch_keeps_and_restores_its_hits():
    watch = NearEmptyWatch()
    watch.restore([[3, 0.001], [9, 0.0], "junk", [1]])
    assert watch.hits == [[3, 0.001], [9, 0.0]] and watch.kept(5) == [[3, 0.001]]
