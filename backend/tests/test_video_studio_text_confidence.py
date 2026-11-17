"""The text check against lettering the start picture itself holds unreadable.

* the reading comes back word by word with a confidence, a height and a box;
  the older shapes still read;
* the expected words are the start picture's words read confidently and tall
  enough; the others are recorded as unreadable there and left out;
* a start picture with crisp foreground text and soft distant flags: a take
  that keeps the foreground text and garbles the flags passes, one that
  garbles the foreground text fails naming the word, the frame and its box,
  one that adds a new word is reported and not failed;
* the start picture's own illegible lettering is the baseline of the
  unreadable count; the frame QC's text match reads the expected words alone;
* ``run_clip`` keeps the record with the take and stores the failed word's crop.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from app.services.video_studio import clips, frame_qc, runners, text_drift
from app.services.video_studio.jobs import JOB_FAILED, JOB_READY
from tests.test_video_studio_shot_rescue import _logo, _png, take  # noqa: F401 - the run_clip fixture

NAMES = ["start_picture", *text_drift.FRAMES]
#: The pictures' heights in pixels: the start picture 900 tall, the frames 882 (1568 wide, 16:9).
HEIGHTS = {"start_picture": 900, "first_frame": 882, "middle_frame": 882, "last_frame": 882}


def _word(text: str, confidence: float = 0.97, height: float = 0.06, x: float = 0.1, y: float = 0.1) -> dict:
    return {"text": text, "confidence": confidence, "height": height, "box": [x, y, 0.3, height]}


#: Crisp foreground lettering, and three flags on poles far away: small, soft, read as garbage.
FOREGROUND = [_word("Filigran"), _word("Orchestration", x=0.1, y=0.3)]
FLAGS = [
    _word("Fillgran", confidence=0.3, height=0.012, x=0.6, y=0.05),
    _word("Flgrn", confidence=0.2, height=0.011, x=0.7, y=0.05),
    _word("Orchestrotion", confidence=0.4, height=0.013, x=0.8, y=0.05),
    _word("Bakery", confidence=0.35, height=0.012, x=0.9, y=0.05),
]


def _answer(start: list[dict], first: list[dict], last: list[dict], unreadable: dict | None = None) -> str:
    counts = unreadable or {}
    return json.dumps(
        {
            "start_picture": {"words": start, "unreadable": counts.get("start_picture", 0)},
            "first_frame": {"words": first, "unreadable": counts.get("first_frame", 0)},
            "last_frame": {"words": last, "unreadable": counts.get("last_frame", 0)},
        }
    )


def _verdict(start: list[dict], first: list[dict], last: list[dict], unreadable: dict | None = None) -> text_drift.TextCheck:
    reading = text_drift._parse(_answer(start, first, last, unreadable), NAMES)
    assert reading is not None
    return text_drift.verdict(reading, HEIGHTS)


# ── The reading ───────────────────────────────────────────────────────────


def test_the_reading_comes_word_by_word_and_the_older_shapes_still_read():
    reading = text_drift._parse("```json\n" + _answer(FOREGROUND, FOREGROUND, [], {"last_frame": 2}) + "\n```", NAMES)
    assert reading is not None and reading.texts["start_picture"] == "Filigran Orchestration"
    [first, second] = reading.words["start_picture"]
    assert first == text_drift.Word("Filigran", 0.97, 0.06, (0.1, 0.1, 0.3, 0.06)) and second.text == "Orchestration"
    assert reading.unreadable == {"start_picture": 0, "first_frame": 0, "middle_frame": 0, "last_frame": 2}
    assert reading.words["last_frame"] == []
    # The older shape: one text per picture, every word at full confidence with no size.
    old = text_drift._parse('{"start_picture": {"text": "Orchestration", "unreadable": 1}, "first_frame": "", "last_frame": "Orcheatration"}', NAMES)
    assert old is not None and old.words == {} and old.words_of("last_frame") == [text_drift.Word("Orcheatration")]
    assert old.words_of("start_picture")[0].readable(900) and old.unreadable["start_picture"] == 1
    # A bare list of words, a word with a box and no height, a word that is no word, a malformed box.
    loose = text_drift._parse(
        json.dumps(
            {
                "start_picture": ["Filigran", {"text": "Badge", "box": [0.1, 0.1, 0.2, 0.05]}, {"text": ""}, 7],
                "first_frame": {"words": [{"text": "Filigran", "confidence": "high", "box": [1, 2]}]},
                "last_frame": None,
            }
        ),
        NAMES,
    )
    assert loose is not None
    assert loose.words["start_picture"] == [text_drift.Word("Filigran"), text_drift.Word("Badge", 1.0, 0.05, (0.1, 0.1, 0.2, 0.05))]
    assert loose.words["first_frame"] == [text_drift.Word("Filigran")] and loose.texts["last_frame"] == ""
    assert text_drift._parse("no json", NAMES) is None and text_drift._parse("[1]", NAMES) is None


def test_the_expected_words_are_the_ones_the_start_picture_reads_clearly():
    reading = text_drift._parse(_answer([*FOREGROUND, *FLAGS, _word("Tiny", height=0.01), _word("Soft", confidence=0.59)], [], []), NAMES)
    kept, left = text_drift.readable_words(reading.words_of("start_picture"), 900)
    assert [word.text for word in kept] == ["Filigran", "Orchestration"]
    # Each word left out says why it does not count: its confidence, and its height in the start picture's pixels.
    assert [(item.word, item.confidence) for item in left] == [
        ("Fillgran", 0.3),
        ("Flgrn", 0.2),
        ("Orchestrotion", 0.4),
        ("Bakery", 0.35),
        ("Tiny", 0.97),
        ("Soft", 0.59),
    ]
    assert left[4].height_px == pytest.approx(9.0) and left[0].height_px == pytest.approx(10.8)
    # Without a size (an older reading) the height is not held against a word; a short word is never compared.
    assert text_drift.readable_words([text_drift.Word("Filigran", 0.9), text_drift.Word("OK", 1.0)], 900) == ([text_drift.Word("Filigran", 0.9)], [])


# ── The verdict ───────────────────────────────────────────────────────────


def test_a_take_that_keeps_the_foreground_text_and_garbles_the_flags_passes():
    garbled_flags = [
        _word("Filgron", confidence=0.25, height=0.012, x=0.6, y=0.05),
        _word("Flgm", confidence=0.2, height=0.011, x=0.7, y=0.05),
    ]
    verdict = _verdict([*FOREGROUND, *FLAGS], [*FOREGROUND, *garbled_flags], [*FOREGROUND, *garbled_flags], {"last_frame": 1})
    assert verdict.checked and not verdict.failed, verdict.reason
    assert verdict.expected == ["Filigran", "Orchestration"]
    assert [item.word for item in verdict.excluded] == ["Fillgran", "Flgrn", "Orchestrotion", "Bakery"]
    assert verdict.hallucinated == [] and verdict.expected_text == "Filigran Orchestration"
    record = verdict.record()
    assert record["status"] == "passed" and record["words_checked"] == ["Filigran", "Orchestration"]
    assert record["excluded"] == [
        {"word": "Fillgran", "confidence": 0.3},
        {"word": "Flgrn", "confidence": 0.2},
        {"word": "Orchestrotion", "confidence": 0.4},
        {"word": "Bakery", "confidence": 0.35},
    ]
    assert record["failure"] is None and record["hallucinated"] == []
    # The same words as plain text would have failed it: "Filgron" is a near miss of "Filigran".
    assert text_drift.drifted_words("Filigran Orchestration Fillgran", {"last_frame": "Filigran Filgron"}) != []


def test_a_take_that_garbles_the_foreground_text_fails_naming_the_word_the_frame_and_its_box():
    verdict = _verdict([*FOREGROUND, *FLAGS], FOREGROUND, [_word("Filigran"), _word("Orcheatration", x=0.1, y=0.3)])
    assert verdict.failed
    [drift] = verdict.drifts
    assert drift == text_drift.Drift("orchestration", "Orcheatration", "last_frame", (0.1, 0.3, 0.3, 0.06))
    assert verdict.failure == drift
    assert verdict.reason.startswith('text drift: the video model redrew the text of the start picture - "orchestration" reads "Orcheatration" (last frame)')
    record = verdict.record()
    assert record["status"] == "failed"
    assert record["failure"] == {"word": "orchestration", "seen": "Orcheatration", "frame": "last_frame", "box": [0.1, 0.3, 0.3, 0.06]}
    # A near miss of a word the start picture holds unreadable is no drift: "Flgrn" was never compared.
    assert _verdict([*FOREGROUND, *FLAGS], FOREGROUND, [*FOREGROUND, _word("Flgrm", confidence=0.9)]).drifts == []


def test_a_take_that_adds_a_new_word_is_reported_and_not_failed():
    verdict = _verdict([*FOREGROUND, *FLAGS], FOREGROUND, [*FOREGROUND, _word("WELCOME", x=0.5, y=0.6), _word("Bakery", x=0.5, y=0.8)])
    assert not verdict.failed
    # "Bakery" is a flag's word, at any confidence: not new (and no near miss of a word checked); "WELCOME" is new.
    assert verdict.hallucinated == [text_drift.NewText("WELCOME", "last_frame", (0.5, 0.6, 0.3, 0.06))]
    assert verdict.new_text_note.startswith('The take shows text the start picture does not: "WELCOME" (last frame) - hallucinated lettering')
    assert verdict.record()["hallucinated"] == [{"word": "WELCOME", "frame": "last_frame"}]
    # New lettering too small or too soft to read is no report: it is the unreadable count's.
    small = _verdict([*FOREGROUND, *FLAGS], FOREGROUND, [*FOREGROUND, _word("Noise", confidence=0.3, height=0.01)])
    assert small.hallucinated == [] and small.new_text_note is None
    # A take that shows nothing of the start picture's words reports nothing either: a word missing is no drift.
    assert not _verdict(FOREGROUND, [], []).failed


def test_the_start_pictures_own_illegible_lettering_is_the_baseline_of_the_unreadable_count():
    # Four soft flags on the start picture, melted on the take and counted there as unreadable places: the same
    # lettering, no new failure.
    melted = _verdict([*FOREGROUND, *FLAGS], FOREGROUND, FOREGROUND, {"first_frame": 4, "last_frame": 4})
    assert not melted.failed and melted.unreadable == []
    # One place more than the start picture's own garbles the text.
    worse = _verdict([*FOREGROUND, *FLAGS], FOREGROUND, FOREGROUND, {"last_frame": 5})
    assert worse.failed and worse.unreadable == [("last_frame", 1)] and worse.failure is None
    assert "text the start picture reads drawn unreadable (1 place in the last frame)" in worse.reason
    reading = text_drift._parse(_answer([*FOREGROUND, *FLAGS], FOREGROUND, [], {"start_picture": 1, "last_frame": 4}), NAMES)
    assert text_drift.illegible_counts(reading, HEIGHTS) == {
        "start_picture": 5,
        "first_frame": 0,
        "middle_frame": 0,
        "last_frame": 4,
    }
    # A start picture reading nothing clearly fails no take for unreadable text.
    assert not _verdict(FLAGS, [], [], {"last_frame": 5}).failed


def test_the_frame_qc_text_match_reads_the_expected_words_alone():
    verdict = _verdict([*FOREGROUND, *FLAGS], FOREGROUND, FOREGROUND)
    assert frame_qc.text_match(verdict.expected_text, verdict.texts["first_frame"]) == 1.0
    # Read whole, the start picture's flags would have dragged the match down.
    assert frame_qc.text_match(verdict.texts["start_picture"], verdict.texts["first_frame"]) < 1.0
    # A check from before the words were sized keeps its text as the expected text.
    assert text_drift.TextCheck(True, texts={"start_picture": "Filigran Badge"}).expected_text == "Filigran Badge"
    assert text_drift.TextCheck(True, texts={"start_picture": "Filigran Badge"}).record()["words_checked"] == ["Filigran", "Badge"]
    assert text_drift.TextCheck(False, note="not checked").record() == {
        "status": "skipped",
        "words_checked": [],
        "excluded": [],
        "hallucinated": [],
        "invented": [],
        "places": [],
        "failure": None,
        "note": "not checked",
    }


def test_the_height_of_a_picture_is_read_off_its_bytes():
    assert text_drift.picture_height(_png(Image.new("RGB", (64, 48)))) == 48
    assert text_drift.picture_height(b"not a picture") == 0
    # With no height known, a word's size is not held against it.
    assert text_drift.Word("Filigran", 0.9, 0.001).readable(0)


# ── run_clip ──────────────────────────────────────────────────────────────


def _frames_of(tmp_path: Path) -> dict[str, str]:
    paths = {}
    for name in text_drift.FRAMES:
        picture = Image.new("RGB", (640, 360), (20, 30, 40))
        picture.paste(Image.new("RGB", (192, 22), (240, 240, 240)), (64, 108))
        path = tmp_path / f"text-check-{name}.jpg"
        picture.save(path, format="JPEG")
        paths[name] = str(path)
    return paths


def test_run_clip_keeps_the_text_check_with_the_take_and_stores_the_failed_words_crop(take, tmp_path, monkeypatch):  # noqa: F811
    take.video, take.start = b"MP4", _png(_logo())
    paths = _frames_of(tmp_path)
    verdict = text_drift.TextCheck(
        True,
        [text_drift.Drift("orchestration", "Orcheatration", "last_frame", (0.1, 0.3, 0.3, 0.06))],
        texts={"start_picture": "Filigran Orchestration", "first_frame": "Filigran Orchestration", "last_frame": "Filigran Orcheatration"},
        expected=["Filigran", "Orchestration"],
        excluded=[text_drift.Excluded("Fillgran", 0.3, 10.8)],
        frame_paths=paths,
    )

    async def text_check(*_args, **_kwargs):
        return verdict

    async def no_lettering(*_args, **_kwargs):
        return []

    async def probe(_path):
        from types import SimpleNamespace

        return SimpleNamespace(duration=2.0, width=640, height=360, fps=24, has_audio=False)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(_png(_logo()))

    from app.services.video_studio import encoder

    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(text_drift, "check", text_check)
    monkeypatch.setattr(text_drift, "locate_lettering", no_lettering)
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_FAILED
    assert row.error.startswith('text drift: the video model redrew the text of the start picture - "orchestration" reads "Orcheatration" (last frame)')
    qc = next(qc for qc in [row.params.get(frame_qc.QC_KEY), *(a.get("frame_qc") for a in row.params.get(clips.ATTEMPTS) or [])] if qc)
    record = frame_qc.text_check(qc)
    assert record["words_checked"] == ["Filigran", "Orchestration"] and record["excluded"] == [{"word": "Fillgran", "confidence": 0.3}]
    crop_id = record["failure"]["crop_file_id"]
    assert row.error.endswith(f"(crop: file `{crop_id}`)") and record["failure"]["word"] == "orchestration"
    # The crop is the word's box out of the last frame, grown by half its height, a file of the take.
    crop = Image.open(io.BytesIO(take.files[crop_id]))
    assert crop.size == (round(640 * 0.43) - round(640 * 0.07), round(360 * 0.39) - round(360 * 0.27))
    assert crop_id in frame_qc.frame_file_ids({frame_qc.QC_KEY: qc})
    assert frame_qc.summary({frame_qc.QC_KEY: qc})["text_check"]["failure"]["crop_file_id"] == crop_id


def test_run_clip_records_a_passed_text_check_and_says_the_new_text(take, monkeypatch):  # noqa: F811
    take.video, take.start = b"MP4", _png(_logo())
    verdict = text_drift.TextCheck(
        True,
        texts={"start_picture": "Filigran", "first_frame": "Filigran WELCOME", "last_frame": "Filigran"},
        expected=["Filigran"],
        hallucinated=[text_drift.NewText("WELCOME", "first_frame")],
    )

    async def text_check(*_args, **_kwargs):
        return verdict

    async def probe(_path):
        from types import SimpleNamespace

        return SimpleNamespace(duration=2.0, width=640, height=360, fps=24, has_audio=False)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(_png(_logo()))

    from app.services.video_studio import encoder

    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(text_drift, "check", text_check)
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_READY, row.error
    record = frame_qc.text_check(row.params[frame_qc.QC_KEY])
    assert record["status"] == "passed" and record["hallucinated"] == [{"word": "WELCOME", "frame": "first_frame"}]
    assert any(note.startswith('The take shows text the start picture does not: "WELCOME" (first frame)') for note in row.params["notes"])
