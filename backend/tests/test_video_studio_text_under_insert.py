"""The text check leaves the screen a screen insert covers unread (dev:video-text-check-under-insert, #10056).

A take made for a screen insert (``insert: true``) whose model drew gibberish on the green screen failed as text
drift, though the insert covers that screen at render time. The worker now blanks the covered rectangle
(``insert_keep_out.covered_box``) on the start picture and on every frame before the reading
(``text_drift.blank_region``), notes it, and advises ``key: null`` when the keyed screen shows something else.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import clips, frame_qc, insert_keep_out, text_drift

QUAD = [[0.30, 0.20], [0.70, 0.20], [0.70, 0.60], [0.30, 0.60]]


def _picture(width: int = 400, height: int = 200) -> bytes:
    image = Image.new("RGB", (width, height), (0, 177, 64))
    ImageDraw.Draw(image).rectangle((0, 0, 40, 40), fill=(255, 255, 255))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=95)
    return out.getvalue()


def _insert_params(**extra) -> dict:
    return {clips.SURFACE: {"quad": QUAD}, "insert": True, **extra}


def test_blank_region_paints_the_covered_rectangle_grey_and_keeps_the_rest():
    data = text_drift.blank_region(_picture(), (0.25, 0.75, 0.25, 0.75))
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
        centre = image.getpixel((200, 100))
        corner = image.getpixel((10, 10))
    assert all(abs(channel - 128) <= 6 for channel in centre)
    assert all(channel >= 240 for channel in corner)


def test_blank_region_leaves_an_unreadable_picture_and_an_empty_box_as_they_are():
    assert text_drift.blank_region(b"not a picture", (0.1, 0.9, 0.1, 0.9)) == b"not a picture"
    data = _picture()
    assert text_drift.blank_region(data, (0.5, 0.5, 0.1, 0.9)) is data


def test_covered_box_is_the_screen_of_a_shot_carrying_an_insert(monkeypatch):
    monkeypatch.setattr(clips, "carries_insert", lambda params: bool(params.get("insert")))
    assert insert_keep_out.covered_box(_insert_params()) == (0.3, 0.7, 0.2, 0.6)
    # The screen measured on the take as made wins over the start picture's.
    measured = _insert_params(**{frame_qc.QC_KEY: {frame_qc.SURFACE: {"quad": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]}}})
    assert insert_keep_out.covered_box(measured) == (0.1, 0.5, 0.1, 0.5)


def test_covered_box_is_none_without_an_insert_or_for_a_screen_replacement(monkeypatch):
    monkeypatch.setattr(clips, "carries_insert", lambda params: bool(params.get("insert")))
    assert insert_keep_out.covered_box({clips.SURFACE: {"quad": QUAD}}) is None
    # A screen replacement draws its screenshot into the take: its text is read as exact text.
    assert insert_keep_out.covered_box(_insert_params(**{clips.SCREEN: {"quad": QUAD}})) is None


def _reading(frame_words: list[dict]) -> text_drift.Reading:
    start = [{"text": "Filigran", "confidence": 0.95, "height": 0.05, "box": [0.05, 0.05, 0.2, 0.05], "known": True}]
    words = {
        "start_picture": text_drift._words_of(start) or [],
        **{name: text_drift._words_of(frame_words) or [] for name in text_drift.FRAMES},
    }
    texts = {name: " ".join(w.text for w in read) for name, read in words.items()}
    return text_drift.Reading(texts, {name: 0 for name in words}, words)


HEIGHTS = {"start_picture": 1080, **{name: 1080 for name in text_drift.FRAMES}}


def test_gibberish_outside_the_screen_still_fails_the_take():
    """The same lettering outside the covered rectangle is read: the near miss of a start word still fails."""
    garbled = [{"text": "Filigarn", "confidence": 0.95, "height": 0.05, "box": [0.05, 0.05, 0.2, 0.05], "known": False}]
    verdict = text_drift.verdict(_reading(garbled), HEIGHTS)
    assert verdict.failed
    assert verdict.covered_note is None


def test_a_take_read_with_its_screen_covered_passes_and_says_so():
    """Blanked before the reading, the screen's gibberish is not read: the take passes, its record and note name it."""
    verdict = text_drift.verdict(_reading([]), HEIGHTS)
    verdict.covered = (0.3, 0.7, 0.2, 0.6)
    assert not verdict.failed
    assert "covered by the insert" in (verdict.covered_note or "")
    assert verdict.record()["covered_by_insert"] == [0.3, 0.7, 0.2, 0.6]


@pytest.mark.asyncio
async def test_check_blanks_the_covered_rectangle_on_the_picture_and_every_frame(monkeypatch, tmp_path):
    from app.services.video_studio import encoder

    seen: dict[str, bytes] = {}

    async def extract_still(video_path, *, at, out_path, max_side):
        with open(out_path, "wb") as handle:
            handle.write(_picture())

    async def transcribe(llm, model, pictures):
        seen.update({name: data for name, (data, _mime) in pictures.items()})
        return _reading([])

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    async def get_llm_for_user(db, user_id):
        return object(), "stub"

    monkeypatch.setattr(encoder, "extract_still", extract_still)
    monkeypatch.setattr(text_drift, "transcribe", transcribe)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    monkeypatch.setattr("app.llm.router.get_llm_for_user", get_llm_for_user)

    verdict = await text_drift.check(
        "user", (_picture(), "image/jpeg"), "take.mp4", str(tmp_path), duration=4.0, covered=(0.25, 0.75, 0.25, 0.75)
    )
    assert verdict.covered == (0.25, 0.75, 0.25, 0.75)
    assert set(seen) == {"start_picture", *text_drift.FRAMES}
    for data in seen.values():
        with Image.open(io.BytesIO(data)) as opened:
            centre = opened.convert("RGB").getpixel((200, 100))
        assert all(abs(channel - 128) <= 6 for channel in centre)


def test_key_advisory_names_key_null_when_the_keyed_screen_shows_something_else():
    measure = insert_keep_out.Overlap(100, "green", 0.04, 12, 0.5, 0.9, 30, 3, 0.12, (0.4, 0.3, 0.5, 0.4), 24.0)
    params = {frame_qc.QC_KEY: {frame_qc.FOREGROUND_OVERLAP: measure.as_dict()}}
    said = insert_keep_out.key_advisory(params) or ""
    assert "key: null" in said and "green" in said


def test_key_advisory_is_silent_for_a_clear_or_unkeyed_screen():
    clear = insert_keep_out.Overlap(100, "green")
    assert insert_keep_out.key_advisory({frame_qc.QC_KEY: {frame_qc.FOREGROUND_OVERLAP: clear.as_dict()}}) is None
    unkeyed = insert_keep_out.Overlap(100, None)
    assert insert_keep_out.key_advisory({frame_qc.QC_KEY: {frame_qc.FOREGROUND_OVERLAP: unkeyed.as_dict()}}) is None
    assert insert_keep_out.key_advisory({}) is None
