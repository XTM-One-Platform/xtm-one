"""``quad: auto`` on a scene of several screen inserts: one screen each (#10896).

What is proven, without a worker or a database:

* every flat keyed screen of a frame is found (``screens.found_screens``),
  read left to right then top to bottom (``surface_refine.screens_on``),
  the one largest screen still what ``found_screen`` answers;
* the inserts left to the footage are handed one screen each
  (``insert_bezels.assign_screens``): in reading order, or the screen
  nearest each previous quad on a re-measure - quads off by up to 35% of
  their screen land on their own screen, whatever their overlay order -,
  a screen under a quad given being no one's;
* fewer free screens than inserts is refused, naming the screens found;
* the bezel measured on the screen handed out is that screen's, and a quad
  given far off its screen is judged against the screen nearest it;
* the save writes each measured quad and says the screen it took, and a
  refusal is said once for its scene.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import insert_bezels, screens, surface_refine
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

CLIP = "00000000-0000-0000-0000-000000010896"
GREEN = (0, 177, 64)
#: Three monitors side by side, each screen (x0, x1) on rows 200 to 440 of a 1280x720 frame.
COLUMNS = ((60, 380), (480, 800), (900, 1220))
TOP, BOTTOM = 200, 440


def _monitors(columns=COLUMNS, rows=((TOP, BOTTOM),)):
    """A dark room, a black bezel round each screen, each screen one flat green."""
    import numpy as np

    frame = np.full((720, 1280, 3), 46, dtype=np.uint8)
    for top, bottom in rows:
        for left, right in columns:
            frame[top - 16 : bottom + 16, left - 16 : right + 16] = (18, 18, 20)
            frame[top:bottom, left:right] = GREEN
    return frame


def _expected(left: int, right: int, top: int = TOP, bottom: int = BOTTOM):
    return (
        (left / 1280, top / 720),
        (right / 1280, top / 720),
        (right / 1280, bottom / 720),
        (left / 1280, bottom / 720),
    )


def _close(quad, expected, tolerance=0.006) -> bool:
    return all(
        abs(x - ex) < tolerance and abs(y - ey) < tolerance for (x, y), (ex, ey) in zip(quad, expected, strict=True)
    )


def _unit(found, size=(1280, 720)):
    return [tuple((x / size[0], y / size[1]) for x, y in item.corners) for item in found]


# ── finding every screen ─────────────────────────────────────────────────


def test_every_flat_screen_of_the_frame_is_found_in_reading_order():
    found = surface_refine.screens_on(_monitors())
    assert len(found) == 3
    for quad, (left, right) in zip(_unit(found), COLUMNS, strict=True):
        assert _close(quad, _expected(left, right), 0.01), quad
    # The one screen ``found_screen`` answers is still the largest.
    from PIL import Image

    one = screens.found_screen(Image.fromarray(_monitors(), "RGB"))
    assert one.corners in [item.corners for item in found]


def test_a_wall_of_screens_is_read_row_by_row():
    found = surface_refine.screens_on(_monitors(columns=COLUMNS[:2], rows=((90, 300), (420, 630))))
    centres = [(sum(x for x, _ in q) / 4, sum(y for _, y in q) / 4) for q in _unit(found)]
    assert len(centres) == 4
    assert centres[0][1] < 0.5 and centres[1][1] < 0.5 and centres[2][1] > 0.5 and centres[3][1] > 0.5
    assert centres[0][0] < centres[1][0] and centres[2][0] < centres[3][0]


def test_no_screen_is_none_found():
    import numpy as np

    assert surface_refine.screens_on(np.full((720, 1280, 3), 46, dtype=np.uint8)) == []


# ── handing them out ─────────────────────────────────────────────────────


def _three():
    return [_expected(left, right) for left, right in COLUMNS]


def test_inserts_without_a_previous_quad_take_the_screens_left_to_right():
    assigned, why = insert_bezels.assign_screens([0, 1, 2], _three())
    assert why is None
    assert assigned == {0: 0, 1: 1, 2: 2}


@pytest.mark.parametrize("off", [0.0, 0.2, 0.35])
def test_a_re_measure_maps_each_insert_to_its_own_screen(off):
    screens_ = _three()
    # The stored quads, each moved by *off* of its screen's width and height, listed in another overlay order.
    order = {0: 2, 1: 0, 2: 1}
    previous = {}
    for index, number in order.items():
        quad = screens_[number]
        width, height = quad[1][0] - quad[0][0], quad[2][1] - quad[1][1]
        previous[index] = tuple((x + off * width, y - off * height) for x, y in quad)
    assigned, why = insert_bezels.assign_screens([0, 1, 2], screens_, previous=previous)
    assert why is None
    assert assigned == order


def test_a_screen_under_a_quad_given_is_no_ones():
    screens_ = _three()
    assigned, why = insert_bezels.assign_screens([1, 2], screens_, taken=[screens_[0]])
    assert why is None
    assert assigned == {1: 1, 2: 2}


def test_two_inserts_on_a_single_screen_are_refused_with_the_screens_found():
    one = [_expected(*COLUMNS[1])]
    assigned, why = insert_bezels.assign_screens([0, 1], one)
    assert assigned == {}
    assert why is not None
    assert "2 screen inserts" in why and "1 free flat screen" in why and "screen 1 [" in why
    assigned, why = insert_bezels.assign_screens([0, 1], [])
    assert assigned == {} and "found: none" in why


# ── measuring the screen handed out ──────────────────────────────────────


def test_the_bezel_measured_is_the_one_of_the_screen_handed_out():
    frame = _monitors()
    found = surface_refine.screens_on(frame)
    for item, (left, right) in zip(found, COLUMNS, strict=True):
        bezel = surface_refine.measure_bezel(frame, surface=item)
        assert bezel is not None
        assert _close(bezel.quad, _expected(left, right)), (left, bezel.quad)


def test_a_quad_given_far_off_is_judged_against_the_screen_nearest_it():
    frame = _monitors()
    # The right screen's quad, 30 px down and right: off its bezel, never measured against the left one.
    left, right = COLUMNS[2]
    given = tuple((x + 30 / 1280, y + 30 / 720) for x, y in _expected(left, right))
    bezel = surface_refine.measure_bezel(frame, given=given)
    assert bezel is not None
    centre = sum(x for x, _ in bezel.quad) / 4
    assert abs(centre - (left + right) / 2 / 1280) < 0.02


# ── the save ─────────────────────────────────────────────────────────────


def _authored(count: int = 3) -> dict:
    overlay = {"type": "screen_insert", "source": {"clip_id": CLIP}}
    return validate_video_spec(
        {
            "title": "Three monitors",
            "freestyle": True,
            "scenes": [{"id": "s", "type": "clip", "clip_id": CLIP, "overlays": [dict(overlay) for _ in range(count)]}],
        }
    )


def _measured(index: int, *, bezel=None, digest: str | None = None, **fields) -> insert_bezels.Measured:
    digest = f"d{index}" if digest is None else digest
    return insert_bezels.Measured("s", index, ("clip", CLIP), "file", 0, 0.0, None, bezel, digest, **fields)


def test_three_inserts_left_to_the_footage_save_three_distinct_quads():
    authored = _authored()
    assert len(parse_video_spec(authored).scenes[0].overlays) == 3
    frame = _monitors()
    found = surface_refine.screens_on(frame)
    measured = [
        _measured(
            index, bezel=surface_refine.measure_bezel(frame, surface=found[index]), screen=f"screen {index + 1} of 3"
        )
        for index in range(3)
    ]
    resolution = insert_bezels.resolve(authored, measured)
    assert resolution.problems == []
    quads = [resolution.authored["scenes"][0]["overlays"][i]["quad"] for i in range(3)]
    centres = [sum(x for x, _ in quad) / 4 for quad in quads]
    assert centres == sorted(centres) and len({round(c, 3) for c in centres}) == 3
    for quad, (left, right) in zip(quads, COLUMNS, strict=True):
        assert _close(quad, _expected(left, right))
    assert any("screen 2 of 3" in note for note in resolution.notes)


def test_a_refusal_is_said_once_for_its_scene():
    why = "2 screen inserts are left to the footage (quad auto) and it shows 1 free flat screen(s) for them (found: x)"
    measured = [_measured(0, digest="", refusal=why), _measured(1, digest="", refusal=why)]
    resolution = insert_bezels.resolve(_authored(2), measured)
    assert [problem.message for problem in resolution.problems] == [f"scene s: {why}"]
    assert len(resolution.refused) == 1
    assert [found[2] for found in insert_bezels.findings(measured)] == [why]


def test_stored_quads_are_read_back_by_scene_and_index():
    stored = {
        "scenes": [
            {
                "id": "s",
                "overlays": [
                    {"type": "screen_insert", "quad": [[0.1, 0.2], [0.3, 0.2], [0.3, 0.4], [0.1, 0.4]]},
                    {"type": "screen_insert"},
                    {"type": "text", "quad": [[0, 0], [1, 0], [1, 1], [0, 1]]},
                ],
            }
        ]
    }
    assert insert_bezels.stored_quads(stored) == {("s", 0): ((0.1, 0.2), (0.3, 0.2), (0.3, 0.4), (0.1, 0.4))}
    assert insert_bezels.stored_quads(None) == {}
