"""``save_video_frames`` on a recording (a take) the caller reads with ``get_video_recording``.

A take read by ``get_video_recording`` is swept by its full id or by the 8+ hexadecimal prefix that tool
accepts: the frames come back on one contact sheet. A take of someone else that no video the caller can open
uses is refused with the reason, and nothing is downloaded. Synthetic video only: ffprobe and ffmpeg are faked.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.models.video_studio import JOB_READY, VideoTake
from app.services.video_studio import assets, encoder, projects

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a001")
STRANGER = uuid.UUID("00000000-0000-0000-0000-00000000b002")
CONVERSATION = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
TAKE_ID = "9262b803-1c2d-4e5f-8a9b-0123456789ab"
TAKE_FILE = "00000000-0000-0000-0000-0000000000c3"


def _take(owner: uuid.UUID) -> VideoTake:
    return VideoTake(
        id=uuid.UUID(TAKE_ID),
        user_id=owner,
        name="Onboarding demo",
        status=JOB_READY,
        duration_ms=4_000,
        file_id=uuid.UUID(TAKE_FILE),
        expired_at=None,
        viewport_width=1280,
        viewport_height=720,
        pixel_ratio=1.0,
        events=[],
    )


class _Db:
    """The take by id; a prefix query answered with that take."""

    def __init__(self, take: VideoTake):
        self.take = take

    async def get(self, model, key):
        return self.take if model is VideoTake and str(key) == TAKE_ID else None

    async def execute(self, _statement):
        rows = [(self.take.id, self.take.name)]
        return SimpleNamespace(all=lambda: rows, scalars=lambda: SimpleNamespace(first=lambda: None))


@pytest.fixture
def world(monkeypatch):
    """The take's MP4 as ffprobe and ffmpeg see it; downloads counted; no project shares the take."""
    state = SimpleNamespace(downloads=0, stored=[], take=_take(OWNER))

    async def download(file_id, path):
        assert str(file_id) == TAKE_FILE
        state.downloads += 1
        Path(path).write_bytes(b"MP4")
        return SimpleNamespace(content_type="video/mp4")

    async def probe(_path):
        return SimpleNamespace(duration=4.0, width=1280, height=720, fps=30.0, has_audio=False)

    async def extract_still(_path, *, at, out_path, max_side, crop_to=None):
        Image.new("RGB", (1280, 720), (int((at or 4.0) * 40),) * 3).save(out_path, "PNG")

    async def in_no_visible_project(_db, _kind, _asset_id, _user):
        return False

    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    monkeypatch.setattr(projects, "_asset_in_visible_project", in_no_visible_project)

    from app.services.builtin_tools import _shared
    from app.services.builtin_tools import video_studio as module

    @asynccontextmanager
    async def session():
        yield _Db(state.take)

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=OWNER, role="user")

    async def persist(**saved):
        row = SimpleNamespace(
            id=uuid.uuid4(),
            size=len(saved["raw_bytes"]),
            s3_object_name="s3",
            **{k: v for k, v in saved.items() if k != "raw_bytes"},
        )
        state.stored.append(row)
        return row

    monkeypatch.setattr(module, "async_session", session)
    monkeypatch.setattr(module, "_user", find_user)
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    _definitions, state.executor = module.build_video_studio_tools(None, str(OWNER), str(CONVERSATION))
    return state


def _call(world, **arguments):
    from app.tools.base import ToolCall

    return asyncio.run(world.executor.execute_tool(ToolCall(id="f1", name="save_video_frames", arguments=arguments)))


@pytest.mark.parametrize("recording_id", [TAKE_ID, TAKE_ID[:8]], ids=["full-id", "id-prefix"])
def test_a_readable_take_gives_its_frames(world, recording_id):
    result = _call(world, recording_id=recording_id, step_seconds=1)
    assert not result.is_error, result.content
    head = json.loads(result.content.split("\n", 1)[0])
    assert [f["at_seconds"] for f in head["frames"]] == [0, 1, 2, 3]
    assert world.downloads == 1
    assert [f.file_id for f in result.files] == [head["sheet_file_id"]]
    assert world.stored[-1].filename.endswith("contact sheet.png")


def test_a_take_the_caller_cannot_read_is_refused_with_the_reason(world):
    world.take = _take(STRANGER)
    result = _call(world, recording_id=TAKE_ID, step_seconds=1)
    assert result.is_error
    assert f"No recording `{TAKE_ID}` you can read" in result.content
    assert world.downloads == 0 and world.stored == []
