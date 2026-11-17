"""A frame's video named by an id prefix (8+ hexadecimal characters).

``save_video_frame`` / ``save_video_frames`` (``clips.resolve_frame``) and a shot's start frame
(``clips.resolve_reference``) take a recording, an AI shot or a file by the prefix ``get_video_jobs``
resolves: the one row of the caller's own it starts stands for its full id, several are refused naming
them, none is the usual not found. A full id reads no prefix. The readability check is unchanged.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.models.video_studio import JOB_READY, VideoClip
from app.services.video_studio import clips

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a001")
CLIP_ID = "48b4e177-0b0c-4d1e-9f00-1234567890ab"
OTHER_ID = "48b4e177-ffff-4d1e-9f00-1234567890ab"
CLIP_FILE = "00000000-0000-0000-0000-0000000000c2"


def _clip() -> VideoClip:
    return VideoClip(
        id=uuid.UUID(CLIP_ID),
        user_id=OWNER,
        name="Lobby",
        provider="gemini",
        model="veo-3.1",
        prompt="Slow orbit",
        params={},
        status=JOB_READY,
        duration_ms=8_000,
        file_id=uuid.UUID(CLIP_FILE),
        expired_at=None,
    )


class _Db:
    """The clip by id; each prefix query answered with *candidates*, its statement kept."""

    def __init__(self, candidates):
        self.candidates = candidates
        self.statements: list = []
        self.clip = _clip()

    async def get(self, model, key):
        return self.clip if model is VideoClip and str(key) == CLIP_ID else None

    async def execute(self, statement):
        self.statements.append(statement)
        rows = self.candidates
        return SimpleNamespace(all=lambda: rows, scalars=lambda: SimpleNamespace(first=lambda: None))


def _user():
    return SimpleNamespace(id=OWNER, role="user")


def test_an_ai_shot_prefix_stands_for_the_one_own_shot_it_starts():
    db = _Db([(uuid.UUID(CLIP_ID), "Lobby")])
    source = asyncio.run(clips.resolve_frame(db, _user(), clip_id="48b4e177"))
    assert source["kind"] == "shot" and source["id"] == CLIP_ID
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    # A range on the primary key, among the caller's own shots, as get_video_jobs looks it up.
    assert "BETWEEN" in sql and "user_id" in sql
    assert clips.resolved_prefix_note("48b4e177", source) == f"`48b4e177` is AI shot `{CLIP_ID}`. "


def test_a_full_id_reads_no_prefix_and_says_nothing_more():
    db = _Db([])
    source = asyncio.run(clips.resolve_frame(db, _user(), clip_id=CLIP_ID))
    assert source["id"] == CLIP_ID
    assert db.statements == []
    assert clips.resolved_prefix_note(CLIP_ID, source) == ""


def test_a_prefix_of_several_shots_is_refused_naming_them():
    db = _Db([(uuid.UUID(CLIP_ID), "Lobby"), (uuid.UUID(OTHER_ID), "Hall")])
    with pytest.raises(clips.ClipError) as refused:
        asyncio.run(clips.resolve_frame(db, _user(), clip_id="48b4e177"))
    text = str(refused.value)
    assert CLIP_ID in text and OTHER_ID in text and "AI shots" in text
    assert not text.startswith("Error:")


def test_a_prefix_of_nothing_is_the_usual_not_found():
    with pytest.raises(clips.ClipError, match="No AI shot `deadbeef` you can read"):
        asyncio.run(clips.resolve_frame(_Db([]), _user(), clip_id="deadbeef"))


def test_a_recording_prefix_is_looked_up_among_the_callers_recordings():
    db = _Db([])
    with pytest.raises(clips.ClipError, match="No recording `0badc0de` you can read"):
        asyncio.run(clips.resolve_frame(db, _user(), recording_id="0badc0de"))
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "video_takes" in sql and "BETWEEN" in sql
