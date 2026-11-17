"""The regeneration cap of an AI shot, per prompt and start frame. Synthetic shots, no provider.

* a shot is generated again at most ``MAX_RETRIES`` times from the same
  prompt and start frame; a new prompt or a new start frame starts the count
  again, whatever the shot's own attempt number;
* a retry asking only another model or another ``max_camera_motion`` - the
  worker's fallback to the next model included - is never counted, and never
  refused past the cap;
* through the real ``clips.restart_clip``.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_FAILED, JOB_PENDING, VideoClip
from app.services.video_studio import clips, jobs
from app.services.video_studio.clips import MAX_RETRIES, REGENERATIONS

CLIP_ID = "00000000-0000-0000-0000-0000000061d1"
START = "00000000-0000-0000-0000-0000000061d2"
PROMPT = "A fox crossing a snowy field at dusk, the camera locked off"


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False, email="user@example.com", is_active=True)


def _clip(**params) -> VideoClip:
    return VideoClip(
        id=uuid.UUID(CLIP_ID),
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt=PROMPT,
        params={"duration_seconds": 8, "provider_id": str(uuid.uuid4()), **params},
        status=JOB_FAILED,
        error="camera moved",
    )


class _Session:
    async def refresh(self, _row, *, with_for_update=False):
        return None

    async def flush(self):
        return None


@pytest.fixture
def restarting(monkeypatch):
    enqueued: list = []

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        provider = SimpleNamespace(holds_locked_camera=lambda _model, *, start_frame: None)
        return provider, model or "veo-3.1-generate-preview", SimpleNamespace(id=uuid.uuid4(), provider="gemini")

    async def enqueue(_db, kind, row, *, generation=0, owed_to=None, **_kwargs):
        enqueued.append(generation)

    async def no_cooling(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "_kept_direction", no_cooling)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr("app.services.video_studio.provider_cooling.cooling_of", no_cooling)
    return enqueued


def _again(clip: VideoClip, **changes) -> None:
    """Generate *clip* again with *changes*, then let that generation fail, ready for the next retry."""
    asyncio.run(clips.restart_clip(_Session(), _user(), clip, **changes))
    assert clip.status == JOB_PENDING
    clip.status, clip.error = JOB_FAILED, "camera moved"


def test_the_same_prompt_is_generated_again_five_times_then_refused(restarting):
    clip = _clip()
    for count in range(1, MAX_RETRIES + 1):
        _again(clip)
        assert clip.params[REGENERATIONS]["count"] == count
    with pytest.raises(clips.ClipError, match="from the same prompt and start frame: change its prompt or its start frame"):
        asyncio.run(clips.restart_clip(_Session(), _user(), clip))
    # Repeating the prompt word for word is the same try.
    with pytest.raises(clips.ClipError, match="from the same prompt"):
        asyncio.run(clips.restart_clip(_Session(), _user(), clip, prompt=f"  {PROMPT} "))


def test_a_new_prompt_starts_the_count_again(restarting):
    clip = _clip()
    for _ in range(MAX_RETRIES):
        _again(clip)
    _again(clip, prompt="A fox crossing a snowy field at dusk, seen from far away, no camera move")
    assert clip.params[REGENERATIONS]["count"] == 1
    # The shot's own attempt number goes on counting every paid generation.
    assert clip.params["attempt"] == MAX_RETRIES + 1 and restarting[-1] == MAX_RETRIES + 1


def test_a_model_or_a_camera_tolerance_alone_is_never_counted_nor_refused(restarting):
    clip = _clip()
    for _ in range(MAX_RETRIES):
        _again(clip)
    _again(clip, model="veo-3.1-fast")
    _again(clip, max_camera_motion=0.3)
    _again(clip, model="kling-2.5", provider_id=str(uuid.uuid4()), max_camera_motion=0.5)
    assert clip.params[REGENERATIONS]["count"] == MAX_RETRIES
    # The worker's fallback to the next model is such a retry (``runners._fall_back``).
    _again(clip, tried=[{"model": "veo-3.1-generate-preview", "reason": "camera moved"}], model="sora-2")
    assert clip.params[REGENERATIONS]["count"] == MAX_RETRIES
    # Anything more is another try of the same shot: refused at the cap.
    with pytest.raises(clips.ClipError, match="generated again 5 times"):
        asyncio.run(clips.restart_clip(_Session(), _user(), clip, model="sora-2", duration_seconds=6))


def test_a_new_start_frame_starts_the_count_again():
    params = {"reference_file_id": START}
    for _ in range(MAX_RETRIES):
        clips.count_regeneration(params, prompt=PROMPT, changes={})
    with pytest.raises(clips.ClipError):
        clips.count_regeneration(params, prompt=PROMPT, changes={})
    params["reference_file_id"] = str(uuid.uuid4())
    clips.count_regeneration(params, prompt=PROMPT, changes={})
    assert params[REGENERATIONS]["count"] == 1
    # A frame of a recording at another moment is another start.
    params.pop("reference_file_id")
    params["reference_video"] = {"kind": "take", "id": START, "at_seconds": 4.0}
    clips.count_regeneration(params, prompt=PROMPT, changes={})
    assert params[REGENERATIONS]["count"] == 1


def test_what_a_retry_names_of_its_context_counts_for_nothing():
    assert clips.counts_toward_cap({})
    assert clips.counts_toward_cap({"prompt": PROMPT})
    assert not clips.counts_toward_cap({"model": "veo-3.1-fast", "discard": False, "video_id": None, "freestyle": False})
    assert not clips.counts_toward_cap({"max_camera_motion": 0.3, "template_id": str(uuid.uuid4())})
    assert clips.counts_toward_cap({"model": "veo-3.1-fast", "sound": False})
    # A shot made before the count: its attempts never refuse it.
    legacy = {"attempt": 9}
    clips.count_regeneration(legacy, prompt=PROMPT, changes={})
    assert legacy[REGENERATIONS]["count"] == 1
