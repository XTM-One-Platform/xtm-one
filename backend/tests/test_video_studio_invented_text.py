"""Invented lettering inside a generated shot (dev:invented-text-check). Synthetic readings and specs only.

The text check compared a take's words with the start picture's and reported
the rest as "new text, never failed": three feature pills in gibberish passed
a branded booth. Now:

* F67: a take with a garbled pill is ``invented_text`` with its crop, and a
  render is refused (a dense finding no preview clears);
* F68: the same pill reading a brand term of the lexicon is no finding;
* F69: a plate already carrying gibberish is refused at validate_only, naming
  the box, unless ``keep_regions`` covers it;
* F70: a shot asked with ``text_check: false`` gets an advisory only.

And the prompt sent holds the start picture's printed words ("Every printed
word stays exactly: ...") with a negative term for new lettering.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services.video_studio import clips, text_drift, timeline
from app.services.video_studio.quality import ContentFacts, ShotFacts, content_findings
from app.services.video_studio.render_checks import BLOCKING_SEVERITIES
from app.services.video_studio.spec import authored_spec, parse_video_spec, validate_video_spec

pytestmark = pytest.mark.usefixtures("no_default_video_template")

SHOT = "00000000-0000-0000-0000-00000000c001"
PLATE = "00000000-0000-0000-0000-00000000f001"
CROP = "00000000-0000-0000-0000-00000000f101"
NAMES = ["start_picture", *text_drift.FRAMES]
#: The start picture 900 pixels tall, the frames 882 (1568 wide, 16:9).
HEIGHTS = {"start_picture": 900, "first_frame": 882, "middle_frame": 882, "last_frame": 882}
PILL = (0.3, 0.4, 0.2, 0.04)


def _word(text: str, *, known: bool | None, height: float = 0.04, box=PILL, confidence: float = 0.95) -> text_drift.Word:
    return text_drift.Word(text, confidence, height, box, known)


FILIGRAN = _word("Filigran", known=True, box=(0.1, 0.1, 0.3, 0.06), height=0.06)


def _reading(start: list[text_drift.Word], **frames: list[text_drift.Word]) -> text_drift.Reading:
    read = {"start_picture": start, **{name: frames.get(name, list(start)) for name in text_drift.FRAMES}}
    return text_drift.Reading(
        {name: " ".join(w.text for w in listed) for name, listed in read.items()},
        dict.fromkeys(read, 0),
        read,
    )


# ── F67 / F68: what the frames invent ────────────────────────────────────


def test_f67_a_garbled_pill_the_reading_knows_no_word_for_is_invented_lettering_with_its_box():
    verdict = text_drift.verdict(
        _reading([FILIGRAN], first_frame=[FILIGRAN, _word("Qorvex", known=False)]), HEIGHTS, brand={"xtm"}
    )
    assert not verdict.failed
    [found] = verdict.invented
    assert found == text_drift.Invented("Qorvex", "first_frame", PILL)
    assert found.where == "x 0.30-0.50, y 0.40-0.44"
    assert verdict.invented_note is not None and verdict.invented_note.startswith(
        'The take shows invented lettering: "Qorvex" (first frame, x 0.30-0.50, y 0.40-0.44) - letters that make no word'
    )
    assert "invented_text" in verdict.invented_note
    record = verdict.record()
    assert record["status"] == "passed" and record["invented"] == [{"word": "Qorvex", "frame": "first_frame", "box": list(PILL)}]
    # The record read back for the video's quality check, the crop the worker stored kept with it.
    stored = {"frame_qc": {"text_check": {**record, "invented": [{**record["invented"][0], "crop_file_id": CROP}]}}}
    [kept] = text_drift.invented_of(stored)
    assert kept == text_drift.Invented("Qorvex", "first_frame", PILL, CROP)
    assert text_drift.invented_of({"frame_qc": {"text_check": {"status": "passed"}}}) == [] and text_drift.invented_of(None) == []


def test_f68_the_same_pill_reading_a_brand_term_a_number_or_a_word_of_the_plate_is_no_invention():
    frame = [
        FILIGRAN,
        _word("OpenCTI", known=False),
        _word("CTEM", known=False, box=(0.55, 0.4, 0.1, 0.04)),
        _word("2026", known=False, box=(0.7, 0.4, 0.1, 0.04)),
        _word("Filigrqn", known=False, box=(0.1, 0.6, 0.3, 0.06), height=0.06),
    ]
    # The brand terms come from the platform's lexicon (``lexicon.brand_lexicon``) when none are given.
    verdict = text_drift.verdict(_reading([FILIGRAN], first_frame=frame, last_frame=frame), HEIGHTS)
    assert verdict.invented == [] and verdict.invented_note is None
    # "Filigrqn" is a near miss of the plate's word: the drift check's, never an invention.
    assert [drift.seen for drift in verdict.drifts] == ["Filigrqn"]
    brand = text_drift.brand_terms()
    assert {"opencti", "ctem", "xtm one", "xtm", "one"} <= brand
    # A word the reading knows, or did not judge, is never invented; lettering under 10 px at 1080p is the unreadable count's.
    assert text_drift.invented_words([], {"first_frame": [_word("Qorvex", known=True)]}, brand) == []
    assert text_drift.invented_words([], {"first_frame": [_word("Qorvex", known=None)]}, brand) == []
    assert text_drift.invented_words([], {"first_frame": [_word("Qorvex", known=False, height=0.008)]}, brand) == []
    # Once per word and frame, however often the frame shows it.
    twice = {"first_frame": [_word("Qorvex", known=False), _word("qorvex", known=False, box=(0.6, 0.4, 0.2, 0.04))]}
    assert [item.word for item in text_drift.invented_words([], twice, brand)] == ["Qorvex"]


# ── F67 / F70: the video's quality check ─────────────────────────────────


def _raw() -> dict:
    scene: dict = {"id": "booth", "type": "clip", "clip_id": SHOT}
    return {"title": "Synthetic", "freestyle": True, "scenes": [scene]}


def _findings(raw: dict, facts: ContentFacts) -> list:
    spec = parse_video_spec(validate_video_spec(raw))
    shots = {
        scene.id: timeline.ClipMedia(duration=4.0, width=1920, height=1080, fps=24.0, frame_count=96)
        for scene in spec.scenes
        if scene.type == "clip"
    }
    document = timeline.estimate_plan(spec, {}, shots).document
    return content_findings(spec, document, facts, authored_spec(raw))


def _facts(**changes) -> ContentFacts:
    shot = ShotFacts("Booth", "ready", width=1920, height=1080, camera="unspecified", motion_ssim=0.9, **changes)
    return ContentFacts(shots={SHOT: shot}, output=(1920, 1080))


INVENTED = (text_drift.Invented("Qorvex", "first_frame", PILL, CROP), text_drift.Invented("Blenta", "last_frame"))


def test_f67_a_shot_with_invented_lettering_is_a_dense_finding_with_the_crop_that_refuses_a_render():
    found = [f for f in _findings(_raw(), _facts(invented=INVENTED)) if f.kind == "invented_text"]
    [finding] = found
    assert finding.scene_id == "booth" and finding.severity == "dense" and finding.group == "content"
    assert f'"Qorvex" (first frame, x 0.30-0.50, y 0.40-0.44, crop `{CROP}`), "Blenta" (last frame)' in finding.message
    assert f"Generate it again in place (clip_id `{SHOT}`), keep that region from the start picture" in finding.message
    # A render is refused on it, and no passing preview clears it: the lettering does not change with the camera.
    assert finding.severity in BLOCKING_SEVERITIES and finding.clip_id is None
    assert not [f for f in _findings(_raw(), _facts()) if f.kind == "invented_text"]


def test_f70_a_shot_asked_with_text_check_false_gets_an_advisory_only():
    [finding] = [f for f in _findings(_raw(), _facts(invented=INVENTED, text_check=False)) if f.kind == "invented_text"]
    assert finding.severity == "advisory" and finding.severity not in BLOCKING_SEVERITIES
    assert finding.message.endswith("accepted, the shot was asked with text_check: false") and finding.clip_id == SHOT


# ── F69: the plate read before anything is paid ──────────────────────────


def test_f69_a_plate_already_carrying_gibberish_is_refused_naming_the_box():
    reading = _reading([FILIGRAN, _word("Qorvex", known=False), _word("Blenta", known=False, box=None)])
    lettering = text_drift.start_lettering(reading, 900, brand={"xtm"})
    assert lettering.words == ["Filigran"]
    assert [item.word for item in lettering.invented] == ["Qorvex", "Blenta"]
    assert lettering.refusal == (
        'The start picture shows unreadable lettering at x 0.30-0.50, y 0.40-0.44: "Qorvex" (and 1 more). Clean the '
        "plate, or set keep_regions over that lettering so it is drawn back from the picture."
    )
    assert text_drift.StartLettering(["Filigran"]).refusal is None
    # The record kept with the shot's params, and read back.
    assert text_drift.StartLettering.from_dict(lettering.as_dict()) == lettering
    assert text_drift.StartLettering.from_dict("no") is None


@pytest.fixture
def plate(monkeypatch):
    """A start picture the reading finds gibberish on; what ``lint_lettering`` was asked is kept."""
    from app.services.video_studio import pictures

    asked: dict = {}

    async def read_picture(_db, _user, file_id, *, what):
        asked["file_id"] = file_id
        return Image.new("RGB", (900, 600), (40, 40, 40))

    async def read_start_picture(user_id, picture):
        asked["user_id"], asked["mime"] = user_id, picture[1]
        return text_drift.StartLettering(["Filigran"], [text_drift.Invented("Qorvex", "start_picture", PILL)])

    monkeypatch.setattr(pictures, "read_picture", read_picture)
    monkeypatch.setattr(text_drift, "read_start_picture", read_start_picture)
    return asked


def _lint(reference: dict, *, text_check: bool = True, regions=None):
    user = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000a001"))
    return asyncio.run(clips.lint_lettering(None, user, reference, text_check=text_check, regions=regions))


def test_f69_validate_only_refuses_the_plate_before_anything_is_paid(plate):
    with pytest.raises(clips.ClipError) as refused:
        _lint({"reference_file_id": PLATE})
    said = str(refused.value)
    assert said.startswith('The start picture shows unreadable lettering at x 0.30-0.50, y 0.40-0.44: "Qorvex".')
    assert said.endswith("Nothing was queued or paid.")
    assert plate == {"file_id": PLATE, "user_id": uuid.UUID("00000000-0000-0000-0000-00000000a001"), "mime": "image/png"}


def test_a_keep_region_over_the_gibberish_lets_the_plate_through_and_the_words_are_kept(plate):
    kept, notes = _lint({"reference_file_id": PLATE}, regions=[[0.25, 0.35, 0.3, 0.15]])
    assert kept == {"words": ["Filigran"], "invented": [{"word": "Qorvex", "frame": "start_picture", "box": list(PILL)}]}
    assert any(note.startswith('Lettering: the start picture reads 1 printed word ("Filigran")') for note in notes)
    assert any(note.startswith("The start picture's unreadable lettering (1 place) is covered by keep_regions") for note in notes)
    # A region that does not cover the box is no cover.
    with pytest.raises(clips.ClipError, match="unreadable lettering"):
        _lint({"reference_file_id": PLATE}, regions=[[0.0, 0.0, 0.2, 0.2]])
    # No start picture, or the text check off: nothing is read.
    assert _lint({}) == (None, []) and _lint({"reference_file_id": PLATE}, text_check=False) == (None, [])


def test_a_plate_that_cannot_be_read_leaves_the_shot_asked_as_before(monkeypatch):
    from app.services.video_studio import pictures

    async def gone(_db, _user, _file_id, *, what):
        raise RuntimeError("the picture is gone")

    monkeypatch.setattr(pictures, "read_picture", gone)
    assert _lint({"reference_file_id": PLATE}) == (None, [])


# ── The prompt sent ───────────────────────────────────────────────────────


def test_the_prompt_sent_holds_every_printed_word_of_the_plate_and_refuses_new_lettering():
    params = {text_drift.START_LETTERING_KEY: {"words": ["Filigran", "Orchestration"], "invented": []}}
    prompt, negative = clips.with_lettering(params, "The booth comes alive.", "blur")
    assert prompt == (
        'The booth comes alive. Every printed word stays exactly: "Filigran", "Orchestration". No other lettering appears.'
    )
    assert negative == f"blur, {text_drift.NEGATIVE_LETTERING}"
    # Never for a shot whose plate reads no word; the negative term is added once.
    assert text_drift.with_negative_lettering(negative) == negative
    assert clips.with_lettering({}, "A heron lands.", None) == ("A heron lands.", None)
    assert text_drift.lettering_line([]) is None and text_drift.with_negative_lettering(None) == text_drift.NEGATIVE_LETTERING
    # The whole art direction keeps it (``provider_prompts``), within the prompt's room.
    sent, _negative = clips.provider_prompts(params, "A" * clips.MAX_PROMPT_CHARS, None)
    assert len(sent) <= clips.MAX_PROMPT_CHARS and sent.endswith("No other lettering appears.")
