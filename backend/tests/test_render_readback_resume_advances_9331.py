"""A resumed presence read-back moves past the place kept (issue #9331).

A high render whose frames were all drawn re-entered the read-back on every relaunch at "frame 6,524 of 6,704
(resumed from frame 6,525)" and never moved past it: the first frame judged after the resume decoded each video
insert's source from its frame 0 in one step, with no frame said, so the read-back's watchdog ended the attempt
there every time. Now:

* a video insert's source is opened by a seek just before the first frame wanted, and each source frame decoded is
  said (``on_source``), so a working catch-up is never taken for a stall;
* the place is also kept on wall time (``PRESENCE_PLACE_SECONDS``), so a slow pass keeps a later frame before a cut.

Synthetic fixtures only: the fake decoder of ``test_render_presence_resume_8958``, no ffmpeg, no browser.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.video_studio import encoder, insert_checks
from tests.test_render_presence_resume_8958 import _run, _setup, _verdict, decoder  # noqa: F401  (the fixture)


def _video_source(monkeypatch, opened: list[float], frames: int = 400):
    """A fake video insert of *frames* 4x4 frames, each one's first byte its index; every seek recorded."""

    async def stream(_path, *, width, height, resize=False, start=0.0):
        opened.append(start)
        first = round(start * 24 + 0.5) if start > 0 else 0
        for n in range(first, frames):
            yield bytes([n % 256]) + bytes(width * height * 3 - 1)

    monkeypatch.setattr(encoder, "iter_rgb_frames", stream)
    inserts = {"clip": SimpleNamespace(kind="video", path="clip.mp4")}
    payloads = {"clip": {"src_w": 4, "src_h": 4, "src_fps": 24}}
    return inserts, payloads


async def test_a_source_wanted_late_is_opened_by_a_seek_and_its_catch_up_is_said(monkeypatch):
    opened: list[float] = []
    inserts, payloads = _video_source(monkeypatch, opened)
    said: list[int] = []
    sources = insert_checks._SourceFrames(inserts, payloads, lambda: said.append(1))
    try:
        got = await sources.get([("clip", 299), ("clip", 300), ("clip", 301)])
    finally:
        await sources.close()
    assert len(opened) == 1 and opened[0] > 0, "opened near the frame wanted, never from its start"
    assert got[("clip", 300)].getpixel((0, 0))[0] == 300 % 256, "the frame read is the one wanted"
    assert got[("clip", 299)] is not None, "the frame before is kept for the comparison"
    assert 0 < len(said) <= 5, "each source frame decoded is said, only the few around the frame wanted"


async def test_a_source_wanted_early_is_decoded_from_its_start(monkeypatch):
    opened: list[float] = []
    inserts, payloads = _video_source(monkeypatch, opened)
    sources = insert_checks._SourceFrames(inserts, payloads)
    try:
        got = await sources.get([("clip", 10)])
    finally:
        await sources.close()
    assert opened == [0.0]
    assert got[("clip", 10)].getpixel((0, 0))[0] == 10


async def test_a_slow_pass_keeps_its_place_on_wall_time(decoder, tmp_path, monkeypatch):  # noqa: F811
    document, audits, placed, frames, pictures = _setup()
    decoder["pictures"] = pictures
    whole = _verdict(await _run(document, audits, placed, workdir=tmp_path / "whole"))
    decoder["judged"].clear()

    # No place by frame count in this run; one by wall time after every frame judged.
    monkeypatch.setattr(insert_checks, "PRESENCE_PLACE_FRAMES", 10**9)
    monkeypatch.setattr(insert_checks, "PRESENCE_PLACE_SECONDS", 0.0)
    place = insert_checks.PresencePlace(tmp_path / "relaunch" / "presence-place.npz", "drawing-digest")
    key = insert_checks._place_key(place, placed, None)

    with pytest.raises(Exception):  # noqa: B017 - the fixture's stop
        await _run(document, audits, placed, workdir=tmp_path / "relaunch", place=place, stop_after=frames.start + 8)
    first_next = insert_checks.load_place(place, key)[0]["next"]
    assert first_next > frames.start, "the place was kept on wall time before the cut"
    decoder["judged"].clear()

    with pytest.raises(Exception):  # noqa: B017 - the fixture's stop, further on
        await _run(document, audits, placed, workdir=tmp_path / "relaunch", place=place, stop_after=frames.start + 16)
    second_next = insert_checks.load_place(place, key)[0]["next"]
    assert second_next > first_next, "each relaunch goes on from a later frame than the one before"
    decoder["judged"].clear()

    assert _verdict(await _run(document, audits, placed, workdir=tmp_path / "relaunch", place=place)) == whole
