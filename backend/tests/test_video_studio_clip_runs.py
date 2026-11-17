"""An AI shot is never paid for twice by the job engine's retries.

``run_clip`` runs again after any failure. The upstream job id is stored the
moment the provider created the job, the video the moment it is downloaded:
a later run resumes the job, or takes the stored video and only does again
what followed it (the probe, the poster) - ``generate_video`` is never asked
for a second generation.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm import identity
from app.llm.video import VideoResult
from app.models.user import User
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import assets, clips, content_drift, encoder, jobs, runners


class _Provider:
    """Makes a video; ``crash_after_job`` dies once the job exists, before the video is back."""

    def __init__(self, *, crash_after_job: bool = False):
        self.calls: list[str | None] = []
        self.callers: list[identity.LLMCallerIdentity] = []
        self.crash_after_job = crash_after_job

    def takes_negative_prompt(self, model):
        return True

    async def generate_video(self, request, *, progress=None, on_job=None, resume_job=None, on_state=None):
        self.calls.append(resume_job)
        self.callers.append(identity.resolve_llm_caller())
        if resume_job is None:
            await on_job("op/1")
            if self.crash_after_job:
                self.crash_after_job = False
                raise RuntimeError("worker lost its lease")
        return VideoResult(data=b"MP4", job_id=resume_job or "op/1")


@pytest.fixture
def world(monkeypatch):
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="A slow orbit around a dark globe",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8},
        status=JOB_PENDING,
    )
    stored: dict = {}
    state = {"provider": _Provider(), "probe_fails": 0}
    maker = User(id=row.user_id, email="maker@example.com", is_active=True)

    class _Db:
        async def get(self, model, _key):
            return maker if model is User else row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return state["provider"], model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def store_path(path, **_kwargs):
        file_id = uuid.uuid4()
        stored[file_id] = Path(path).read_bytes()
        return SimpleNamespace(id=file_id)

    async def download_file(file_id, path):
        if file_id not in stored:
            return None
        Path(path).write_bytes(stored[file_id])
        return SimpleNamespace(content_type="video/mp4")

    async def probe(_path):
        if state["probe_fails"]:
            state["probe_fails"] -= 1
            raise RuntimeError("ffprobe crashed")
        return SimpleNamespace(duration=4.0, width=1280, height=720, has_audio=False)

    async def extract_frame(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def content_check(*_args, **_kwargs):
        return content_drift.ContentCheck(True)

    # The worker has no service identity: only the job itself can say whom a call is for.
    monkeypatch.setattr(identity, "_service_identity", None)
    monkeypatch.setattr(content_drift, "check", content_check)
    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "download_file", download_file)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", extract_frame)
    return SimpleNamespace(row=row, state=state, stored=stored)


def test_a_failure_after_the_video_was_made_only_redoes_what_followed(world):
    world.state["probe_fails"] = 1
    first = asyncio.run(runners.run_clip(str(world.row.id)))
    assert first.kind == "retry"
    # The paid video and its job are on the row before the step that failed.
    assert world.row.provider_job_id == "op/1" and world.row.file_id in world.stored
    second = asyncio.run(runners.run_clip(str(world.row.id)))
    assert second.kind == "done" and world.row.status == JOB_READY
    assert world.state["provider"].calls == [None]


def test_a_run_lost_while_the_job_ran_resumes_it(world):
    world.state["provider"] = _Provider(crash_after_job=True)
    assert asyncio.run(runners.run_clip(str(world.row.id))).kind == "retry"
    assert world.row.provider_job_id == "op/1" and world.row.file_id is None
    assert asyncio.run(runners.run_clip(str(world.row.id))).kind == "done"
    # One generation started, then resumed by its id - never a second one.
    assert world.state["provider"].calls == [None, "op/1"] and world.row.status == JOB_READY


def test_the_shot_is_asked_for_as_the_person_it_is_made_for(world):
    assert asyncio.run(runners.run_clip(str(world.row.id))).kind == "done"
    assert world.state["provider"].callers == [
        identity.LLMCallerIdentity(user_id=world.row.user_id, email="maker@example.com")
    ]


def test_a_stored_video_survives_its_provider_being_removed(world, monkeypatch):
    world.state["probe_fails"] = 1
    asyncio.run(runners.run_clip(str(world.row.id)))

    async def gone(*_args, **_kwargs):
        raise clips.ClipError("No video provider with this id is configured.", about_model=True)

    monkeypatch.setattr(clips, "resolve", gone)
    assert asyncio.run(runners.run_clip(str(world.row.id))).kind == "done"
    assert world.row.status == JOB_READY
