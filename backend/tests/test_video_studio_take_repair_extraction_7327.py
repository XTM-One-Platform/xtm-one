"""A take repaired from its MP4 reads its painted frames in passes ffmpeg can parse: never one ``select``
term per frame, a short reason when the extraction fails. Synthetic indices and a stubbed ffmpeg only."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.services.video_studio import encoder, take_repair


def _terms(expression: str) -> int:
    return len(re.findall(r"(?:eq|between)\(", expression))


def test_500_scattered_frames_are_read_in_short_select_expressions():
    # Every other frame of 1000 at 60 fps: 500 indices, all within a second of the one before.
    frames = list(range(0, 1000, 2))
    runs = encoder.select_runs(frames, rate=60.0)
    assert sorted(f for run in runs for f in run) == frames
    assert all(_terms(encoder.select_expression(run)) <= encoder.MAX_SELECT_TERMS for run in runs)
    assert len(runs) == -(-500 // encoder.MAX_SELECT_TERMS)


def test_consecutive_frames_are_one_between_term_and_a_pass_stays_numbered_on_three_digits():
    frames = list(range(5000, 6200))
    runs = encoder.select_runs(frames, rate=60.0)
    assert sorted(f for run in runs for f in run) == frames
    assert all(len(run) <= encoder.MAX_PASS_FRAMES for run in runs)
    assert encoder.select_expression(runs[0]) == f"between(n\\,0\\,{len(runs[0]) - 1})"
    assert encoder.select_expression([10, 11, 12, 20]) == "between(n\\,0\\,2)+eq(n\\,10)"


def test_frames_more_than_a_second_apart_still_start_a_new_pass():
    assert encoder.select_runs([0, 1, 200, 201, 202], rate=60.0) == [[0, 1], [200, 201, 202]]
    assert encoder.select_runs([3, 3, -1, 2], rate=60.0) == [[2, 3]]


def test_extract_frames_maps_500_indices_back_from_its_passes(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    async def fake_ffmpeg(args, **_kwargs):
        calls.append(list(args))
        expression = args[args.index("-vf") + 1]
        assert _terms(expression) <= encoder.MAX_SELECT_TERMS
        for count in range(1, int(args[args.index("-frames:v") + 1]) + 1):
            Path(str(args[-1]).replace("%03d", f"{count:03d}")).write_bytes(b"png")

    monkeypatch.setattr(encoder, "ffmpeg", fake_ffmpeg)
    frames = list(range(0, 1000, 2))
    read = asyncio.run(encoder.extract_frames("video.mp4", frames, fps=60.0, out_dir=str(tmp_path)))
    assert sorted(read) == frames
    assert len(set(read.values())) == 500
    assert all("-threads" in call for call in calls)


def test_a_failed_extraction_says_one_short_reason():
    log = (
        "ffmpeg failed (exit 244):\n[Parsed_select_0 @ 0x1] Error while parsing expression 'eq(n,0)+eq(n,1)'\n"
        "Error initializing filters\nError opening output file /tmp/xtm-repair-x/frame-0000001-%03d.png"
    )
    said = take_repair.extraction_failure(encoder.EncoderError(log))
    assert said.startswith("Frame extraction failed: expression too long.")
    assert "\n" not in said and "xtm-repair" not in said
    assert "out of memory" in take_repair.extraction_failure("x\nCannot allocate memory")
    other = take_repair.extraction_failure("ffmpeg failed (exit 1):\n" + "z" * 400)
    assert "\n" not in other and len(other) < 260
