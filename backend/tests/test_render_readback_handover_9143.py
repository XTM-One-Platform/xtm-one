"""The post-draw read-back of the screen inserts across a hand-over (issue #9143).

A high render drew all its frames, then restarted "Reading the screen inserts back off the render" (94 %) on each of
three rollouts, with no ETA and an attempt count that went from 3 of 3 back to 1 of 3. Now:

* a hand-over during the presence read-back keeps the place at the frame reached and stops at once
  (``PresenceHandedOverError``), and the next attempt judges none of the frames already judged;
* the read-back's stage line says frame X of Y and the time left at this attempt's pace;
* the relaunch line says a relaunch past the draw goes on with its checks, and why an attempt count went down.

Synthetic fixtures only: the fake decoder of ``test_render_presence_resume_8958``, no ffmpeg, no browser.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import insert_checks, render_process
from app.services.video_studio.render import READBACK_PACE_MIN_FRAMES, ReadbackWhere
from tests.test_render_presence_resume_8958 import _run, _setup, _verdict, decoder  # noqa: F401  (the fixture)


async def _gate(document, audits, placed, *, workdir, place, handing_over=None, resumed=None):
    from tests.test_video_studio_frame_owners_6433 import H, W, _inserts

    return await insert_checks.presence(
        "encoded.mp4",
        audits,
        placed,
        document,
        _inserts(),
        fps=30,
        size=(W, H),
        workdir=workdir,
        place=place,
        handing_over=handing_over,
        resumed=resumed,
    )


async def test_a_hand_over_mid_read_back_keeps_its_place_and_the_resume_judges_no_frame_twice(decoder, tmp_path):  # noqa: F811
    document, audits, placed, _frames, pictures = _setup()
    decoder["pictures"] = pictures
    whole = _verdict(await _run(document, audits, placed, workdir=tmp_path / "whole"))
    decoder["judged"].clear()

    stored: list[int] = []

    async def keep(path):
        stored.append(path.stat().st_size)

    # PRESENCE_PLACE_FRAMES is 5 under the fixture: hand over well before a periodic place would be due again.
    place = insert_checks.PresencePlace(tmp_path / "relaunch" / "presence-place.npz", "drawing-digest", keep)
    asked = {"stop": False}

    def handing_over() -> bool:
        if len(decoder["judged"]) >= 3:
            asked["stop"] = True
        return asked["stop"]

    with pytest.raises(insert_checks.PresenceHandedOverError) as stopped:
        await _gate(document, audits, placed, workdir=tmp_path / "relaunch", place=place, handing_over=handing_over)
    first = list(decoder["judged"])
    assert stopped.value.kept and stored, "the place was kept at the frame reached and handed to the store"
    decoder["judged"].clear()

    went_on_from: list[int] = []
    resumed = _verdict(
        await _gate(document, audits, placed, workdir=tmp_path / "relaunch", place=place, resumed=went_on_from.append)
    )
    second = list(decoder["judged"])

    assert went_on_from == [stopped.value.frame]
    assert first and second
    assert not set(first) & set(second), "no frame judged twice across the hand-over"
    assert second[0] >= stopped.value.frame
    assert resumed == whole


async def test_a_hand_over_while_decoding_past_the_kept_frames_keeps_the_place_before(decoder, tmp_path):  # noqa: F811
    document, audits, placed, frames, pictures = _setup()
    decoder["pictures"] = pictures
    place = insert_checks.PresencePlace(tmp_path / "presence-place.npz", "digest")
    with pytest.raises(Exception):  # noqa: B017 - the fixture's stop, past one place kept
        await _run(document, audits, placed, workdir=tmp_path, place=place, stop_after=frames.start + 20)
    key = insert_checks._place_key(place, placed, None)
    kept_next = insert_checks.load_place(place, key)[0]["next"]
    decoder["judged"].clear()

    with pytest.raises(insert_checks.PresenceHandedOverError) as stopped:
        await _gate(document, audits, placed, workdir=tmp_path, place=place, handing_over=lambda: True)
    assert stopped.value.frame == kept_next and stopped.value.kept
    assert decoder["judged"] == []
    assert insert_checks.load_place(place, key)[0]["next"] == kept_next


def test_the_read_back_stage_says_where_it_stands_and_the_time_left():
    where = ReadbackWhere(step="presence frames", last=13_562)
    where.began_frame, where.began_at = 1_000, 100.0
    where.frame = 1_000 + READBACK_PACE_MIN_FRAMES - 1
    assert "time left measured after its first frames" in where.stage_words(110.0)
    where.frame = 2_000  # 1,000 frames in 100 s: 11,562 left at 10 frames/s, about 20 min
    said = where.stage_words(200.0)
    assert said.startswith("Reading the screen inserts back off the render: presence frames, frame 2,000 of 13,562")
    assert said.endswith("about 20 min left")
    where.resumed_from = 1_000
    assert where.stage_words(200.0).endswith("about 20 min left (resumed from frame 1,000)")
    assert (
        ReadbackWhere(step="join frames").stage_words(0.0)
        == "Reading the screen inserts back off the render: join frames"
    )


def test_the_relaunch_line_says_a_relaunch_past_the_draw_and_attempts_given_back():
    relaunches = [
        {"attempt": 3, "attempts": 3, "progress": 94, "after": "after a deploy", "frames_kept": 13_563},
        {"attempt": 1, "attempts": 3, "progress": 94, "after": "after a deploy", "frames_kept": 13_563},
    ]
    first, second = render_process.relaunch_line(relaunches).split(": ", 1)[1].split("; ")
    assert first.endswith("its frames drawn, its checks going on from where they stood")
    assert "handed on before its drawing resumed" not in first
    assert second.startswith("attempt 1 of 3 (its attempts given back: its lease lapsed while it still moved) at 94%")
    # A count that only goes up says nothing more.
    assert "given back" not in render_process.relaunch_line(
        [{"attempt": 1, "progress": 10}, {"attempt": 2, "progress": 20}]
    )
