"""An AI track whose provider is busy waits the time the provider named.

``MusicProviderBusyError.retry_after`` - the gateway's ``retry_after_seconds``
- holds the job for that long, no attempt spent; a busy refusal that names no
wait keeps the engine's back-off (``retry``).
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.llm.music import MusicProviderBusyError
from app.models.video_studio import JOB_PENDING, VideoMusic
from app.services.video_studio import jobs, runners
from app.services.video_studio import music as music_tracks

USER_ID = uuid.uuid4()


def _row() -> VideoMusic:
    return VideoMusic(
        id=uuid.uuid4(),
        user_id=USER_ID,
        provider="gateway",
        model="lyria-3.5",
        prompt="A warm synth bed",
        params={"duration_seconds": 60, "provider_id": "p-gateway"},
        status=JOB_PENDING,
    )


def _composing(monkeypatch, row: VideoMusic, refusal: Exception) -> list[dict]:
    from app.models.user import User

    marks: list[dict] = []

    class _Db:
        async def get(self, model, _key):
            return User(id=USER_ID, email="maker@example.com") if model is User else row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def resolve(_db, _user_id, *, model=None, provider_id=None, asked=False):
        return SimpleNamespace(), model, SimpleNamespace(id=provider_id, provider="gateway")

    async def generate(_provider, _request):
        raise refusal

    async def mark(_kind, _job_id, **changes):
        marks.append(changes)

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(music_tracks, "resolve", resolve)
    monkeypatch.setattr(music_tracks, "generate", generate)
    monkeypatch.setattr(jobs, "mark", mark)
    return marks


def test_a_busy_provider_that_names_its_wait_holds_the_track_for_it(monkeypatch):
    row = _row()
    _composing(monkeypatch, row, MusicProviderBusyError("The gateway could not compose now (HTTP 429)", retry_after=42))
    outcome = asyncio.run(runners.run_music(str(row.id)))
    assert (outcome.kind, outcome.wait) == ("hold", 42.0)
    assert "HTTP 429" in (outcome.error or "")


@pytest.mark.parametrize("retry_after", [None, 0.0])
def test_a_busy_provider_that_names_no_wait_is_asked_again_after_the_back_off(monkeypatch, retry_after):
    row = _row()
    _composing(monkeypatch, row, MusicProviderBusyError("The gateway could not compose now (no answer)", retry_after=retry_after))
    outcome = asyncio.run(runners.run_music(str(row.id)))
    assert (outcome.kind, outcome.wait) == ("retry", None)
