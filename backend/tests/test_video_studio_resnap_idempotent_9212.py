"""A save never re-snaps a screen insert the edit left alone (#9212).

Two ``edit_video`` saves that touched only scene A's overlays each moved the
quad of a tracked insert in scene B by about 0.1% ("snapped to it, where its
track starts"): the bezel read again from the stored, already snapped quad
lands a hair off, the quad was written again and scene B's preview read
"changed since preview". ``insert_bezels.resolve`` now takes the stored spec:
a scene the edit left identical keeps its stored quads.
"""

from __future__ import annotations

import copy

from app.services.video_studio import insert_bezels, surface_refine

STORED = ((0.25, 0.236), (0.75, 0.236), (0.75, 0.764), (0.25, 0.764))
#: The bezel read again from STORED: about 0.1% of the frame's width off it.
REREAD = ((0.251, 0.237), (0.751, 0.237), (0.751, 0.765), (0.251, 0.765))
SIZE = (1920, 1080)


def _spec(quad_b=STORED, text_a: str = "Hello", track: bool = True) -> dict:
    return {
        "scenes": [
            {"id": "a", "overlays": [{"type": "text", "text": text_a}]},
            {
                "id": "b",
                "overlays": [
                    {"type": "screen_insert", "source": "file-1", "quad": [list(p) for p in quad_b], "track": track}
                ],
            },
        ]
    }


def _measured(given=STORED, *, tracked: bool = True) -> insert_bezels.Measured:
    corners = [(x * SIZE[0], y * SIZE[1]) for x, y in REREAD]
    flat = surface_refine.flatness(corners, (0.0,) * 4, (1.0,) * 4, (0.0,) * 4, SIZE)
    bezel = surface_refine.Bezel(REREAD, flat, 0.001, found_by="seed")
    return insert_bezels.Measured(
        "b", 0, ("clip", "clip-1"), "file-1", 0, 0.0, tuple(given), bezel, "d", size=SIZE, tracked=tracked
    )


def test_an_edit_elsewhere_keeps_an_untouched_tracked_inserts_quad() -> None:
    held = _spec()
    edited = _spec(text_a="Hello again")
    out = insert_bezels.resolve(edited, [_measured()], held=held)
    assert not out.changed
    assert out.authored["scenes"][1] == held["scenes"][1]
    assert not any("snapped to it" in note for note in out.notes)


def test_two_saves_in_a_row_leave_the_untouched_scene_as_stored() -> None:
    held = _spec()
    first = insert_bezels.resolve(_spec(text_a="one"), [_measured()], held=held)
    second = insert_bezels.resolve(first.authored, [_measured()], held=first.authored)
    assert not first.changed and not second.changed
    assert second.authored["scenes"][1] == held["scenes"][1]


def test_an_untouched_static_insert_is_not_snapped_again() -> None:
    held = _spec(track=False)
    out = insert_bezels.resolve(_spec(text_a="other", track=False), [_measured(tracked=False)], held=held)
    assert not out.changed
    assert out.authored["scenes"][1] == held["scenes"][1]


def test_an_edited_insert_is_still_snapped() -> None:
    held = _spec()
    moved = ((0.252, 0.238), (0.752, 0.238), (0.752, 0.766), (0.252, 0.766))
    edited = _spec(quad_b=moved)
    out = insert_bezels.resolve(edited, [_measured(given=moved)], held=held)
    assert out.changed
    assert out.authored["scenes"][1]["overlays"][0]["quad"] == [list(p) for p in REREAD]


def test_without_the_stored_spec_the_snap_is_as_before() -> None:
    out = insert_bezels.resolve(copy.deepcopy(_spec()), [_measured()])
    assert out.changed
    assert any("where its track starts" in note for note in out.notes)
