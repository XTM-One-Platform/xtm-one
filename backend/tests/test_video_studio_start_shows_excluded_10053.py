"""A negative term the start picture already shows never fails a take on its first frame (issue #10053).

A shot started from a picture of a person with a raised hand, its negative
prompt "waving" to stop the motion, failed as "shows waving (first frame,
major)" on every take: the first frame is the start picture. On a stub chat
model, no provider: the start picture is read against the negative prompt
before anything is paid (kept with the shot, said by ``validate_only``); the
take's frames reading is told the first image is that picture; a take whose
hand comes down passes, one still waving on its last frame fails on the last
frame only.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import clips, content_drift, encoder

PROMPT = "a woman lowers her hand and smiles at the camera"
NEGATIVE = "waving, text"
#: The reading under test, before the suite's autouse fixture stubs it (conftest ``_no_start_negative_reading``).
READ_START_PICTURE = content_drift.read_start_picture


@pytest.fixture
def reader(monkeypatch):
    """A chat model answering the prompt reading, the start picture reading and the frames reading."""
    world = SimpleNamespace(calls=[], start={"shown": ["waving"]}, frames={})

    class _Llm:
        async def chat(self, messages, model=None, temperature=None, max_tokens=None):
            message = messages[0]
            world.calls.append(message.content)
            if not message.files:
                subjects = [{"name": "woman", "kind": "person", "count": None, "variants": []}]
                return SimpleNamespace(content=json.dumps({"subjects": subjects, "camera": "locked", "camera_terms": []}))
            if len(message.files) == 1:
                return SimpleNamespace(content=json.dumps(world.start))
            return SimpleNamespace(content=json.dumps({"frames": world.frames}))

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def get_llm(_db, _user_id):
        return _Llm(), "chat-model"

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        Path(out_path).write_bytes(b"JPG")

    monkeypatch.setattr("app.core.database.async_session", session)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", get_llm)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    monkeypatch.setattr(content_drift, "read_start_picture", READ_START_PICTURE)
    return world


def _frames(*waving_on: str) -> dict:
    woman = {"name": "woman", "present": True, "kind_matches": True, "seen_as": "", "replaced_by": "", "anomalies": []}
    return {
        frame: {"subjects": [woman], "excluded_seen": [{"term": "waving", "noticeable": True}] if frame in waving_on else []}
        for frame in content_drift.FRAMES
    }


def _check(tmp_path, start_shown) -> content_drift.ContentCheck:
    return asyncio.run(
        content_drift.check(
            "u",
            prompt=PROMPT,
            negative_prompt=NEGATIVE,
            video_path="shot.mp4",
            duration=8.0,
            workdir=str(tmp_path),
            params={},
            start_shown=start_shown,
        )
    )


def test_the_start_picture_is_read_against_the_negative_prompt_once_and_kept(reader):
    params: dict = {}
    shown = asyncio.run(content_drift.start_shown("u", params, NEGATIVE, "file-1", (b"PNG", "image/png")))
    assert shown == ["waving"]
    assert params[content_drift.START_SHOWN_KEY]["terms"] == ["waving"]
    asked = reader.calls[-1]
    assert "waving; text" in asked and "already show" in asked
    # Kept for the same picture and negative prompt: not read again.
    calls = len(reader.calls)
    assert asyncio.run(content_drift.start_shown("u", params, NEGATIVE, "file-1", None)) == ["waving"]
    assert len(reader.calls) == calls
    # Another picture or another negative prompt is read anew.
    assert content_drift.kept_start_shown(params, content_drift.start_digest(NEGATIVE, "file-2")) is None
    assert content_drift.kept_start_shown(params, content_drift.start_digest("waving", "file-1")) is None


def test_no_picture_or_no_negative_prompt_reads_nothing(reader):
    assert asyncio.run(content_drift.start_shown("u", {}, NEGATIVE, None, (b"PNG", "image/png"))) == []
    assert asyncio.run(content_drift.start_shown("u", {}, None, "file-1", (b"PNG", "image/png"))) == []
    assert reader.calls == []


def test_an_unusable_start_reading_keeps_nothing_and_the_take_is_judged_as_before(reader):
    reader.start = {"nothing": True}
    params: dict = {}
    assert asyncio.run(content_drift.start_shown("u", params, NEGATIVE, "file-1", (b"PNG", "image/png"))) == []
    assert content_drift.START_SHOWN_KEY not in params


def test_a_take_whose_hand_comes_down_passes(reader, tmp_path):
    reader.frames = _frames("first_frame")
    verdict = _check(tmp_path, ["waving"])
    assert verdict.checked and verdict.failing() == []
    # The frames reading is told the first image is the start picture.
    assert "starts from a picture that already shows: waving" in reader.calls[-1]


def test_a_take_still_waving_on_its_last_frame_fails_on_the_last_frame_only(reader, tmp_path):
    reader.frames = _frames("first_frame", "last_frame")
    [finding] = _check(tmp_path, ["waving"]).failing()
    assert finding.frame == "last_frame" and finding.excluded and finding.subject == "waving"


def test_without_the_start_reading_the_first_frame_still_fails_as_before(reader, tmp_path):
    reader.frames = _frames("first_frame")
    [finding] = _check(tmp_path, None).failing()
    assert finding.frame == "first_frame"
    assert "starts from a picture" not in reader.calls[-1]


def test_a_term_the_start_picture_does_not_show_still_fails_on_the_first_frame():
    answer = {"frames": {"first_frame": {"subjects": [], "excluded_seen": [{"term": "text", "noticeable": True}]}}}
    [finding] = content_drift.drifted([], ["waving", "text"], answer, start_shown=["waving"])
    assert finding.frame == "first_frame" and finding.subject == "text"


def test_the_note_says_which_terms_and_from_which_frame():
    note = content_drift.start_shown_note(["waving"])
    assert note.startswith("The start picture already shows: waving - judged from the middle frame on")
    assert content_drift.start_shown_note([]) is None


def test_validate_reads_the_start_picture_and_answers_the_terms(reader, monkeypatch):
    async def download_file(file_id, out_path):
        Path(out_path).write_bytes(b"PNG")
        return SimpleNamespace(content_type="image/png")

    async def start_picture(data, mime):
        return SimpleNamespace(data=data, mime=mime)

    monkeypatch.setattr("app.services.video_studio.assets.download_file", download_file)
    monkeypatch.setattr(clips, "start_picture", start_picture)
    params = {"reference_file_id": "file-1", "negative_prompt": NEGATIVE}
    [note] = asyncio.run(clips.lint_start_shown(SimpleNamespace(id="u"), params))
    assert "already shows: waving" in note
    assert params[content_drift.START_SHOWN_KEY]["terms"] == ["waving"]
    # content_check: false reads nothing and keeps nothing.
    off = {**params, "content_check": False}
    assert asyncio.run(clips.lint_start_shown(SimpleNamespace(id="u"), off)) == []
    assert content_drift.START_SHOWN_KEY not in off
