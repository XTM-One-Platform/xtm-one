"""The presence read-back goes on from where a stopped attempt left it (issue #8958, slice 2).

A high render drew all its frames, then restarted "Reading the screen inserts back off the render" from 94 % at every
rollout: the presence gate decoded and judged the whole encoded video again on each relaunch, and a render longer than
the rollout interval never ended. The gate now keeps its place (``insert_checks.PresencePlace``) and a gate over the
same video goes on from there.

Synthetic fixtures only - the encoded frames come from a fake decoder, no ffmpeg, no browser:

* stopped twice during one read-back, each relaunch reads only frames past the place kept (monotonic progress), and the
  verdict equals the one an uninterrupted read gives, a missing insert included;
* a place of another video (another key) is never taken: the gate reads from its start;
* the place round-trips through the store copy hook.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.services.video_studio import encoder, insert_checks
from app.services.video_studio.frame_owners import FrameOwners
from tests.test_video_studio_frame_owners_6433 import KEY, QUAD, SOURCE, STARTS, H, W, _document, _frame, _inserts

FPS = 30


class _StopError(Exception):
    """The worker stopping mid read-back."""


def _setup(gone: frozenset[int] = frozenset()):
    document = _document(FPS, STARTS[FPS])
    owners = FrameOwners.of(document)
    cut = math.ceil(document["scenes"][0]["end"] * FPS)
    frames = range(cut - 40, cut + 4)
    mine = set(owners.frames_of(0))
    audits = {KEY: {"frames": {}, "placed": {str(n): [*QUAD[0], *QUAD[2], 1.0] for n in frames if n in mine}}}
    placed = insert_checks.placed_inserts(document, _inserts(), owners=owners)
    pictures = {n: _frame(owners.ids[owners.owner(n)], shown=n not in gone).tobytes() for n in frames}
    return document, audits, placed, frames, pictures


@pytest.fixture
def decoder(monkeypatch):
    """The encoded video as the fake decoder yields it, and every frame the gate judged, in order."""
    state = {"pictures": {}, "first": 0, "judged": []}

    async def frames(_path, *, width, height, resize=False):
        assert (width, height) == (W, H)
        black = bytes(W * H * 3)
        for n in range(state["first"], max(state["pictures"]) + 1):
            yield state["pictures"].get(n, black)

    real_read = insert_checks.PresenceGate.read

    def read(self, frame, picture, sources=None):
        state["judged"].append(frame)
        return real_read(self, frame, picture, sources)

    monkeypatch.setattr(encoder, "iter_rgb_frames", frames)
    monkeypatch.setattr(insert_checks.PresenceGate, "read", read)
    monkeypatch.setattr(insert_checks.screen_insert, "read_picture", lambda _path: SOURCE)
    monkeypatch.setattr(insert_checks, "PRESENCE_PLACE_FRAMES", 5)
    return state


async def _run(document, audits, placed, *, workdir, place=None, stop_after=None):
    seen = [0]

    def on_frame():
        seen[0] += 1
        if stop_after is not None and seen[0] >= stop_after:
            raise _StopError

    return await insert_checks.presence(
        "encoded.mp4",
        audits,
        placed,
        document,
        _inserts(),
        fps=FPS,
        size=(W, H),
        workdir=workdir,
        on_frame=on_frame,
        place=place,
    )


def _verdict(result: insert_checks.Presence) -> tuple:
    by_key = {key: {k: v for k, v in summary.items() if k != "first_missing"} for key, summary in result.by_key.items()}
    return [(finding.kind, finding.data.get("frame"), finding.data.get("frames")) for finding, _ in result.findings], by_key


@pytest.mark.parametrize("gone", [frozenset(), "last_two"])
async def test_stopped_twice_the_read_back_goes_on_from_its_place_and_ends_with_the_same_verdict(decoder, tmp_path, gone):
    probe = _setup()
    cut = probe[3].stop - 4
    missing = frozenset({cut - 2, cut - 1}) if gone == "last_two" else frozenset()
    document, audits, placed, frames, pictures = _setup(missing)
    decoder["pictures"] = pictures
    decoder["first"] = 0

    # The whole read in one go: the verdict to match.
    whole = _verdict(await _run(document, audits, placed, workdir=tmp_path / "whole"))
    decoder["judged"].clear()

    place = insert_checks.PresencePlace(tmp_path / "relaunch" / "presence-place.npz", "drawing-digest")
    first_stop = frames.start + 15
    with pytest.raises(_StopError):
        await _run(document, audits, placed, workdir=tmp_path / "relaunch", place=place, stop_after=first_stop + 1)
    run1 = list(decoder["judged"])
    decoder["judged"].clear()
    with pytest.raises(_StopError):
        await _run(document, audits, placed, workdir=tmp_path / "relaunch", place=place, stop_after=frames.start + 32)
    run2 = list(decoder["judged"])
    decoder["judged"].clear()
    resumed = _verdict(await _run(document, audits, placed, workdir=tmp_path / "relaunch", place=place))
    run3 = list(decoder["judged"])

    assert run1 and run2 and run3
    # Monotonic: each relaunch starts past the frames the attempt before kept, never from the video's start.
    assert run2[0] > run1[0] and run3[0] > run2[0]
    assert run2[0] <= run1[-1] + 1 and run3[0] <= run2[-1] + 1, "no frame skipped unread"
    assert len(run1) + len(run2) + len(run3) < 2 * len(set(run1) | set(run2) | set(run3))
    assert resumed == whole


async def test_a_place_of_another_video_is_never_taken(decoder, tmp_path):
    document, audits, placed, frames, pictures = _setup()
    decoder["pictures"] = pictures
    path = tmp_path / "presence-place.npz"
    with pytest.raises(_StopError):
        await _run(document, audits, placed, workdir=tmp_path, place=insert_checks.PresencePlace(path, "one"), stop_after=frames.start + 20)
    assert path.is_file()
    decoder["judged"].clear()
    await _run(document, audits, placed, workdir=tmp_path, place=insert_checks.PresencePlace(path, "another"))
    first_judged = min(insert_checks.presence_frames(placed))
    assert decoder["judged"][0] == first_judged


async def test_each_place_kept_is_handed_to_the_store(decoder, tmp_path):
    document, audits, placed, _frames, pictures = _setup()
    decoder["pictures"] = pictures
    stored: list[bytes] = []

    async def keep(path):
        stored.append(path.read_bytes())

    place = insert_checks.PresencePlace(tmp_path / "presence-place.npz", "digest", keep)
    await _run(document, audits, placed, workdir=tmp_path, place=place)
    assert stored, "the place went beside the render"
    state, arrays = insert_checks.load_place(place, insert_checks._place_key(place, placed, None))
    assert state["next"] > 0 and isinstance(arrays, dict)
    assert np.load(place.path, allow_pickle=False)["state"].dtype.kind == "U"
