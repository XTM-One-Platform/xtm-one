"""A keyed screen whose side stands hidden behind what is in front of it (issue #11426).

A person beside the screen, their shoulder over its right edge: that edge's bezel is not visible, so the bezel read
stopped the quad's right side at the person's edge and left the screen's own key between the quad and the person. The
render gate then read that strip as ``key_spill_outside_quad`` and advised a retake or a despill - neither clears it,
the green being the screen. Synthetic frames only:

* the save's measure places the hidden side at the key colour's extent, said in the save answer, and leaves a screen
  whose four bezels show exactly as before;
* the render's spill read tells a key region joined to the quad (the screen continuing) from a detached one (a
  reflection on the floor), and the finding advises widening the named side to the measured value, never a retake.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import (
    insert_bezels,
    insert_checks,
    render_checks,
    screen_insert,
    screens,
    surface_refine,
)

FRAME = (1280, 720)
KEY = screens.Key("green", (0, 177, 64))
#: The screen: x 300-1000, y 150-500 of the frame.
SCREEN = (300, 150, 1000, 500)


def _frame(*, person: bool = True) -> np.ndarray:
    """A dim room, a green screen in a dark bezel, and with *person* someone standing over its right edge: the
    screen's right part and its bezel there hidden, green left between the person and the screen's middle."""
    picture = Image.new("RGB", FRAME, (62, 58, 54))
    draw = ImageDraw.Draw(picture)
    x0, y0, x1, y1 = SCREEN
    draw.rectangle((x0 - 14, y0 - 14, x1 + 14, y1 + 14), fill=(24, 24, 28))
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=KEY.rgb)
    if person:
        draw.ellipse((880, 80, 1180, 720), fill=(150, 110, 90))
    return np.asarray(picture)


def test_a_right_side_hidden_behind_a_person_is_placed_at_the_key_colour_s_extent():
    bezel = surface_refine.measure_bezel(_frame(), KEY)
    assert (
        bezel is not None
        and bezel.flatness.visible[1] is not None
        and bezel.flatness.visible[1] < surface_refine.HIDDEN_EDGE_VISIBLE
    )
    [(side, value)] = bezel.hidden
    assert side == "right"
    # The key runs on behind the person's outline up to where the person stands widest: the right side reaches it,
    # past where the person's edge cut the quad before (about x 0.69).
    right = (bezel.quad[1][0] + bezel.quad[2][0]) / 2.0
    assert abs(right - value) < 0.001 and right > 0.70
    # The other three sides stay on their bezel.
    assert abs(bezel.quad[0][0] - SCREEN[0] / FRAME[0]) < 0.005 and abs(bezel.quad[0][1] - SCREEN[1] / FRAME[1]) < 0.005
    assert abs(bezel.quad[2][1] - SCREEN[3] / FRAME[1]) < 0.005
    assert (
        bezel.hidden_words()
        == f"right side hidden behind the foreground: placed at the key colour's extent, x {value:.3f}"
    )
    # Kept with the measure and read back.
    assert surface_refine.Bezel.from_dict(bezel.as_dict()).hidden == bezel.hidden


def test_a_screen_whose_four_bezels_show_is_measured_as_before():
    bezel = surface_refine.measure_bezel(_frame(person=False), KEY)
    assert bezel is not None and bezel.hidden == () and "hidden" not in bezel.as_dict()
    assert bezel.hidden_words() is None
    right = (bezel.quad[1][0] + bezel.quad[2][0]) / 2.0
    assert abs(right - SCREEN[2] / FRAME[0]) < 0.005
    # Without a key nothing is moved: no colour says where the screen continues.
    assert surface_refine.measure_bezel(_frame()).hidden == ()


def test_a_key_coloured_wall_joined_to_the_screen_never_moves_the_side():
    pixels = _frame().copy()
    # The whole right part of the frame keyed past the person: a green wall, no screen edge to stop at.
    pixels[150:500, 1000:] = KEY.rgb
    corners = np.array([(300.0, 150.0), (880.0, 150.0), (880.0, 500.0), (300.0, 500.0)])
    pixels[150:500, 880:1000] = KEY.rgb
    moved, said = surface_refine.hidden_sides(pixels, corners, (1.0, 0.0, 1.0, 1.0), KEY)
    assert said == () and np.allclose(moved, corners)
    # A side whose bezel shows is never moved, whatever runs past it.
    moved, said = surface_refine.hidden_sides(pixels, corners, (1.0, 1.0, 1.0, 1.0), KEY)
    assert said == () and np.allclose(moved, corners)


def test_the_save_answer_says_the_hidden_side_and_writes_the_quad():
    bezel = surface_refine.measure_bezel(_frame(), KEY)
    item = insert_bezels.Measured("s1", 0, ("clip", "c"), "f", 0, 0.0, None, bezel, "d")
    spec = {"scenes": [{"id": "s1", "type": "clip", "overlays": [{"type": "screen_insert", "source": {}}]}]}
    out = insert_bezels.resolve(spec, [item])
    assert out.problems == [] and out.authored["scenes"][0]["overlays"][0]["quad"] == [[x, y] for x, y in bezel.quad]
    [said] = [note for note in out.notes if "hidden behind the foreground" in note]
    assert said.startswith(
        "Scene s1: its screen insert 0's right side hidden behind the foreground: placed at the key colour's extent, x 0."
    )
    # A quad given at the occluder's edge, on the visible bezel otherwise: snapped out to the extent, said as a snap.
    short = ((0.2344, 0.2083), (0.6875, 0.2083), (0.6875, 0.6944), (0.2344, 0.6944))
    given = surface_refine.measure_bezel(_frame(), KEY, given=short)
    assert given is not None and [side for side, _value in given.hidden] == ["right"]
    snapped = insert_bezels.resolve(
        {
            "scenes": [
                {
                    "id": "s1",
                    "type": "clip",
                    "overlays": [{"type": "screen_insert", "source": {}, "quad": [list(p) for p in short]}],
                }
            ]
        },
        [insert_bezels.Measured("s1", 0, ("clip", "c"), "f", 0, 0.0, short, given, "d")],
    )
    assert snapped.problems == []
    assert snapped.authored["scenes"][0]["overlays"][0]["quad"] == [[x, y] for x, y in given.quad]
    assert any("hidden behind the foreground" in note for note in snapped.notes)


# ── The render gate: the screen continuing, or a detached spill ───────────


def _spill(pixels: np.ndarray, quad_px) -> screen_insert.Spill:
    return screen_insert.key_spill(pixels, KEY, np.asarray(quad_px, dtype=float))


def test_two_neighbouring_hidden_sides_both_keep_their_shift_at_their_shared_corner():
    # A person over the screen's bottom-right corner: the right and bottom bezels both hidden, the quad short of both.
    short = np.array([(300.0, 150.0), (900.0, 150.0), (900.0, 450.0), (300.0, 450.0)])
    moved, said = surface_refine.hidden_sides(_frame(person=False), short, (1.0, 0.0, 0.0, 1.0), KEY)
    assert [side for side, _value in said] == ["right", "bottom"]
    # The shared corner carries both shifts: the screen's own corner, not the right side's line on the old bottom.
    assert np.allclose(moved[2], (SCREEN[2] - 1, SCREEN[3] - 1), atol=2.0)
    assert np.allclose(moved[1], (SCREEN[2] - 1, SCREEN[1]), atol=2.0)
    assert np.allclose(moved[3], (SCREEN[0], SCREEN[3] - 1), atol=2.0)
    assert np.allclose(moved[0], short[0])


def test_key_joined_to_a_short_quad_is_the_screen_continuing_on_that_side():
    pixels = _frame(person=False)
    # Short of the screen's right edge by less than the save's reach (HIDDEN_REACH of the quad's width).
    short = [(300.0, 150.0), (880.0, 150.0), (880.0, 500.0), (300.0, 500.0)]
    spill = _spill(pixels, short)
    assert spill.share is not None and spill.share > screen_insert.SPILL_MIN_SHARE
    assert spill.joined == "right" and abs(spill.reaches - SCREEN[2] / FRAME[0]) < 0.01


def test_a_key_coloured_wall_joined_to_the_quad_is_no_continuation():
    # The key runs on from the quad's right side to the frame's edge, past the reach the save keeps: a key-coloured
    # wall, so the gate never advises widening the quad onto it (the save would refuse the same widening).
    pixels = _frame(person=False).copy()
    pixels[SCREEN[1] - 14 : SCREEN[3] + 14, 880:] = KEY.rgb
    short = [(300.0, 150.0), (880.0, 150.0), (880.0, 500.0), (300.0, 500.0)]
    spill = _spill(pixels, short)
    assert spill.share is not None and spill.share > screen_insert.SPILL_MIN_SHARE
    assert spill.joined is None and spill.reaches is None


def test_a_detached_reflection_on_the_floor_is_no_continuation():
    picture = Image.fromarray(_frame(person=False))
    ImageDraw.Draw(picture).rectangle((300, 600, 1000, 700), fill=(10, 120, 40))
    whole = [(SCREEN[0], SCREEN[1]), (SCREEN[2], SCREEN[1]), (SCREEN[2], SCREEN[3]), (SCREEN[0], SCREEN[3])]
    spill = _spill(np.asarray(picture), whole)
    assert spill.share is not None and spill.share > screen_insert.SPILL_MIN_SHARE
    assert spill.joined is None and spill.reaches is None


def _finding(audit: dict) -> render_checks.Finding:
    key = "insert/s1.0"
    frames = {n: dict(audit) for n in range(6)}
    placed = {key: insert_checks.Placed(key, "s1", "its screen insert 0", 0.0, 6 / 30.0, keyed=True)}
    [finding] = [
        f
        for f, _p in insert_checks.findings({key: {"frames": frames, "evidence": {}}}, placed, fps=30.0)
        if f.kind == render_checks.KEY_SPILL_OUTSIDE_QUAD
    ]
    return finding


def test_the_finding_on_a_joined_spill_names_the_side_and_the_value_with_no_retake():
    finding = _finding(
        {"key_spill": 0.015, "key_spill_box": [0.69, 0.2, 0.78, 0.69], "key_spill_side": "right", "key_spill_to": 0.78}
    )
    chosen = finding.data["action"]
    assert chosen["kind"] == insert_checks.GIVE_QUAD and finding.fix == chosen["text"]
    assert chosen["text"].startswith("widen the quad's right side to x 0.780: the screen continues there")
    assert finding.data["side"] == "right" and finding.data["widen_to"] == 0.78
    assert "again" not in chosen["text"] and "despill" not in chosen["text"]


def test_the_finding_on_a_detached_spill_still_advises_the_shot_made_again():
    finding = _finding({"key_spill": 0.015, "key_spill_box": [0.3, 0.8, 0.7, 0.95]})
    assert finding.data["action"]["kind"] == insert_checks.REGENERATE_SHOT
    assert finding.data["side"] is None and finding.data["widen_to"] is None
