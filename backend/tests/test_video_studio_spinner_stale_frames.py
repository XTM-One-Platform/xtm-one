"""A CSS spinner still turning is never judged stopped on frames that prove nothing (the settle wait's spinner flake).

Proven here without a browser, with the frames a starved runner's screencast hands the settle watch:

* reads served the same frame again (the screencast painted nothing new since) show the spinner unchanged: the wait
  does not settle on them, however long the frame stalls;
* one fresh frame showing it as it was (a whole turn later, or a frame that skipped the turn) is no proof either;
* fresh frames spanning ``STILL_SECONDS`` that show it unchanged do settle it, at the frame it last moved on, and
  a spinner gone from the page settles on the DOM change that removed it.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.video_studio import page_settle
from app.services.video_studio.page_settle import PageRead, PageWatch

SPINNER = "div.spinner"
BOX = (100.0, 100.0, 40.0, 40.0)
KEY = page_settle.canvas_key(f"loop:{SPINNER}", BOX)


def _spinner(angle: int) -> np.ndarray:
    crop = np.full((40, 40), 30, dtype=np.int16)
    y, x = np.ogrid[:40, :40]
    ring = (np.hypot(x - 20, y - 20) - 14) ** 2 < 9
    arc = np.abs(((np.degrees(np.arctan2(y - 20, x - 20)) - angle + 180) % 360) - 180) < 60
    crop[ring & arc] = 220
    return crop


def _read(watch: PageWatch, t: float, angle: int, frame_t: float, *, changed: float = 0.0) -> None:
    watch.observe(PageRead(t=t, changed=changed, looping=((SPINNER, BOX),)), {KEY: _spinner(angle)}, frame_t=frame_t)


def _turning(watch: PageWatch, until: float) -> float:
    t = 1.0
    while t < until:
        _read(watch, t, round(t * 450) % 360, frame_t=t)
        t = round(t + page_settle.SAMPLE_SECONDS, 2)
    return t


def test_reads_of_a_stalled_frame_never_settle_a_spinner_still_turning():
    watch = PageWatch(began=1.0)
    t = _turning(watch, 2.5)
    moved = watch.loop_moved
    # The screencast stalls: every read gets the last frame again, the spinner on it unchanged, for 2 s.
    for _ in range(10):
        _read(watch, t, round(moved * 450) % 360, frame_t=moved)
        assert watch.settled_at(t) is None and "an animation not yet seen still" in watch.moving(t)
        t = round(t + page_settle.SAMPLE_SECONDS, 2)


def test_one_fresh_frame_a_turn_later_is_no_proof_the_spinner_stopped():
    watch = PageWatch(began=1.0)
    t = _turning(watch, 2.5)
    moved = watch.loop_moved
    # One frame 0.8 s later (a whole turn of a 0.8 s spinner) shows it as it was, then the screencast stalls again.
    turn = round(moved + 0.8, 2)
    for _ in range(5):
        _read(watch, t, round(moved * 450) % 360, frame_t=turn)
        assert watch.settled_at(t) is None
        t = round(t + page_settle.SAMPLE_SECONDS, 2)


def test_fresh_frames_spanning_the_still_time_settle_it_at_its_last_move():
    watch = PageWatch(began=1.0)
    t = _turning(watch, 2.5)
    last_turn = watch.busy_at
    for _ in range(4):
        _read(watch, t, 90, frame_t=t)
        t = round(t + page_settle.SAMPLE_SECONDS, 2)
    # The first read at rest is its last move (it turned to 90 degrees); 3 more fresh frames span 0.6 s.
    assert watch.settled_at(t) == pytest.approx(round(last_turn + page_settle.SAMPLE_SECONDS, 2))


def test_a_spinner_removed_from_the_page_settles_on_the_dom_change():
    watch = PageWatch(began=1.0)
    t = _turning(watch, 2.5)
    gone = t
    for _ in range(4):
        watch.observe(PageRead(t=t, changed=gone), {}, frame_t=t)
        t = round(t + page_settle.SAMPLE_SECONDS, 2)
    assert watch.loop_moved is None
    assert watch.settled_at(t) == pytest.approx(gone)
