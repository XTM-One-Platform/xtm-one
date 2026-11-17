"""A keyed insert's presence and guard reads leave the bezel's own key colour out.

The presence gate (``insert_checks.PresenceGate``, why ``key``) and the render's guard (``insert_checks.judge_dropped``)
read the key's colour on the encoded frames with ``screen_insert.key_read``. Only what sits inside the quad, past the
strip :data:`screen_insert.RESIDUAL_MARGIN` wide along its edge, says the insert is gone: the footage's glow along the
bezel, the encoder's chroma across it and the margin past it are the footage's, as the residual-key gate judges them
on the layer. A real drop - the key across the quad - is still caught, and the finding says where the colour sat.

Synthetic fixtures only - no provider, no browser, no database.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import insert_checks, quality, screen_insert
from app.services.video_studio.render_checks import FAIL, INSERT_DROPPED
from app.services.video_studio.screens import Key
from tests.test_video_studio_insert_transitions import CROSSFADE, _document, _join_frames, _Render
from tests.video_studio_insert_fixture import SCREEN, H, W

GREEN = Key("green", (0, 177, 64))


def _frame() -> np.ndarray:
    """A grey frame with a dark screen drawn in the quad: nothing of the key's colour anywhere."""
    picture = np.full((H, W, 3), 128, dtype=np.uint8)
    x0, y0, x1, y1 = (int(v) for v in (*SCREEN[0], *SCREEN[2]))
    picture[y0:y1, x0:x1] = (40, 46, 60)
    return picture


def _bezel_glow(picture: np.ndarray, colour: tuple[int, int, int], width: int = 2, corners=SCREEN) -> None:
    """The key's colour on a ring *width* pixels each side of the quad's edge (*corners*, axis-aligned): the bezel's
    glow, the encoder's bleed."""
    xs, ys = [float(p[0]) for p in corners], [float(p[1]) for p in corners]
    x0, y0, x1, y1 = round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))
    ring = np.zeros(picture.shape[:2], dtype=bool)
    ring[y0 - width : y1 + width, x0 - width : x1 + width] = True
    ring[y0 + width : y1 - width, x0 + width : x1 - width] = False
    picture[ring] = colour


def _bar(picture: np.ndarray, colour: tuple[int, int, int]) -> None:
    """The key's colour across the middle of the quad: its surface keyed through, the insert not drawn there."""
    x0, y0, _x1, y1 = (int(v) for v in (*SCREEN[0], *SCREEN[2]))
    picture[y0 + 10 : y1 - 10, x0 + 20 : x0 + 60] = colour


def test_key_read_puts_a_glow_along_the_bezel_on_the_edge_and_margin_never_inside():
    picture = _frame()
    _bezel_glow(picture, GREEN.rgb)
    read = screen_insert.key_read(picture, SCREEN, GREEN)
    # What key_left reads - the residual key's own measure - sees it, past the fail share ...
    assert read.share == screen_insert.key_left(picture, SCREEN, GREEN) > screen_insert.RESIDUAL_FAIL_SHARE
    # ... all of it on the strip along the edge and the margin past it: nothing inside the quad.
    assert read.inside == 0.0
    assert read.edge > 0.0 and read.margin > 0.0
    assert abs(read.edge + read.margin - read.share) < 1e-4


def test_key_read_puts_the_key_across_the_quad_inside():
    picture = _frame()
    _bar(picture, GREEN.rgb)
    read = screen_insert.key_read(picture, SCREEN, GREEN)
    assert read.inside > screen_insert.RESIDUAL_FAIL_SHARE
    assert read.edge == 0.0 and read.margin == 0.0


def test_key_read_on_a_clean_frame_and_off_the_frame_is_nothing():
    assert screen_insert.key_read(_frame(), SCREEN, GREEN) == screen_insert.KeyRead()
    off = np.array([(W + 10, 10), (W + 40, 10), (W + 40, 40), (W + 10, 40)], dtype=float)
    assert screen_insert.key_read(_frame(), off, GREEN) == screen_insert.KeyRead()


def test_the_quality_report_version_moved_with_the_presence_read():
    assert quality.QUALITY_REPORT_VERSION >= 43


def test_key_where_text_names_where_the_colour_sat():
    text = insert_checks.key_where_text({"inside": 0.012, "edge": 0.0039, "margin": 0.001, "source_key_hue": 0.002})
    assert text == (
        " (1.20% inside the quad; 0.39% on its edge strip along the bezel, not counted; 0.10% in the bezel margin past "
        "it, not counted; 0.20% the source's own key hue, allowed)"
    )
    assert insert_checks.key_where_text({"inside": 0.01, "edge": 0.0, "margin": 0.0}) == " (1.00% inside the quad)"
    assert insert_checks.key_where_text(None) == ""


def _guard_frames(tmp_path):
    document = _document(CROSSFADE)
    render = _Render(document, {"a": (40, 46, 60), "b": (60, 44, 48)}, keep=tmp_path)
    join = _join_frames(document)
    frames = {n: render.frame(n).copy() for n in join}
    placed = insert_checks.placed_inserts(document, render.inserts)
    guard = insert_checks.guard_frames(placed)
    alone = [n for n in guard if any(render.audits[k]["placed"][n][4] >= insert_checks.GUARD_ALONE for k in guard[n])]
    assert alone, "a guard frame shows its scene alone: the encoded frame itself is read"
    return render, document, frames, placed, guard, alone


def _read_corners(render, document, frame: int, key: str):
    """The quad the guard reads *key* in on *frame*: the bezel found there, carried to where the page placed its box."""
    payload = next(p for scene in document["scenes"] for p in scene["inserts"] if p["src"] == key)
    audit = render.audits[key]["frames"][frame]
    return insert_checks._quad_on_frame(render.audits[key]["placed"][frame][:4], payload, audit, (W, H))


def test_a_drawn_keyed_insert_with_the_key_s_glow_along_its_bezel_is_never_read_dropped(tmp_path):
    """Acceptance (1): the insert drawn, its residual key 0.00% inside the quad, the footage's key colour on the bezel
    strip of every guard frame read on the encoded frame - no insert_dropped."""
    render, document, frames, placed, guard, alone = _guard_frames(tmp_path)
    for n in alone:
        for name in guard[n]:
            if render.audits[name]["placed"][n][4] < insert_checks.GUARD_ALONE:
                continue
            corners = _read_corners(render, document, n, name)
            key = render.keys()[name]
            _bezel_glow(frames[n], key.rgb, corners=corners)
            assert screen_insert.key_left(frames[n], corners, key) > screen_insert.RESIDUAL_FAIL_SHARE
    assert insert_checks.judge_dropped(frames, render.audits, placed, guard, document, render.keys(), fps=24) == []


def test_a_keyed_insert_not_drawn_on_a_frame_is_still_read_dropped_naming_where_the_key_sat(tmp_path):
    """Acceptance (2) and (3): the key across the quad on one frame read alone fails, the glow along the bezel told apart."""
    render, document, frames, placed, guard, alone = _guard_frames(tmp_path)
    frame = alone[0]
    name = next(k for k in guard[frame] if render.audits[k]["placed"][frame][4] >= insert_checks.GUARD_ALONE)
    key = render.keys()[name]
    _bezel_glow(frames[frame], key.rgb, corners=_read_corners(render, document, frame, name))
    _bar(frames[frame], key.rgb)
    judged = insert_checks.judge_dropped(frames, render.audits, placed, guard, document, render.keys(), fps=24)
    assert judged, "the key across the quad is the insert dropped"
    finding, _proof = judged[0]
    assert finding.kind == INSERT_DROPPED and finding.severity == FAIL
    assert finding.data["frame"] == frame
    sat = finding.data["key_where"]
    assert sat["inside"] > screen_insert.RESIDUAL_FAIL_SHARE and sat["edge"] > 0.0 and sat["margin"] > 0.0
    assert finding.data["share"] == round(sat["inside"], 5)
    assert "inside the quad;" in finding.message and "on its edge strip along the bezel, not counted" in finding.message
