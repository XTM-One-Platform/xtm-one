"""A ready AI shot generated again keeps its take, and a provider's quota is the batch's, not a shot's.

* A ready shot generated again in place keeps playing its take - its video, poster and frames on the row -
  until the new take is ready and passed its checks. An attempt the provider refuses, the content check fails,
  a cancel stops or the engine gives up on leaves the shot READY on that take, the attempt recorded beside it
  with its reason and what it made; a render and the preview play the take meanwhile; nothing paid is asked
  again. ``discard: true`` drops the take at once instead.
* A provider answering 429 - or a quota its error's structure says - cools for every shot asked of it: the shot
  is held (no attempt spent, never a failure, a quota up to a day), the next shot on that provider is not
  even asked, the job line says until when, ``validate_only`` says it before anything is queued.
* ``start_frame: true`` with a ``reference_*`` and no ``reference_image_file_ids`` asks the start frame alone,
  and every answer says the mode.
* A term of the negative prompt two takes in a row showed is asked in the third take's prompt itself ("no
  X anywhere"), its guidance lowered where the model has one, and the answer says so; a model named alone is
  asked of its own provider; ``validate_only`` with a locked camera says the model will not hold it.

Synthetic pictures, synthetic prompts, a fake provider and an in-memory Redis only.
"""

from __future__ import annotations

import asyncio
import copy
import io
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from app.llm.video import VideoGenerationError, VideoProviderBusyError, VideoResult
from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import (
    assets,
    clips,
    content_drift,
    encoder,
    frame_qc,
    jobs,
    provider_cooling,
    provider_waits,
    runners,
)

USER = SimpleNamespace(id=uuid.uuid4(), email="maker@example.com")
PROVIDER = str(uuid.uuid4())
TAKE_VIDEO, TAKE_POSTER = uuid.uuid4(), uuid.uuid4()
TAKE_FRAMES = {"first_frame_file_id": "take-first", "mid_frame_file_id": "take-mid", "last_frame_file_id": "take-last"}
PROMPT = "A paper boat drifts across a still pond, slow push in"


def _picture(shift: int = 0) -> bytes:
    """A synthetic frame: a grey field with a lighter block, moved by *shift*."""
    image = Image.new("RGB", (320, 180), (40, 44, 52))
    ImageDraw.Draw(image).rectangle((60 + shift, 50, 180 + shift, 130), fill=(200, 210, 220))
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


class _Redis:
    """The few Redis commands the cooling uses, in memory."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key):
        return self.data.get(key)

    async def mget(self, keys):
        return [self.data.get(key) for key in keys]

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex
        return True

    async def delete(self, *keys):
        return sum(self.data.pop(key, None) is not None for key in keys)

    async def eval(self, script, _numkeys, key, expected, value, ttl):
        """The cooling's compare-and-set (``provider_cooling._CAS_SCRIPT``), the one script it runs."""
        assert script == provider_cooling._CAS_SCRIPT
        current = self.data.get(key)
        if (current is None and expected == "") or current == expected:
            self.data[key], self.ttl[key] = value, int(ttl)
            return 1
        return 0


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def client():
        return store

    monkeypatch.setattr(provider_cooling, "redis_client", client)
    return store


class _Provider:
    """A video provider: makes a video, refuses with *refuse*, or answers 429 (*busy*)."""

    def __init__(self):
        self.calls = 0
        self.refuse: str | None = None
        self.busy: VideoProviderBusyError | None = None
        #: Every request it was asked, oldest first.
        self.requests: list = []
        #: ``(lowest, default)`` of its guidance, ``None`` for none; whether it holds a locked-off camera.
        self.guidance: tuple[float, float] | None = None
        self.holds_camera: bool | None = None

    def takes_negative_prompt(self, _model):
        return False

    def video_guidance(self, _model):
        return self.guidance

    def holds_locked_camera(self, _model, *, start_frame):
        return self.holds_camera

    def max_reference_images(self, _model):
        return 0

    def reference_images_with_start_frame(self, _model):
        return False

    async def generate_video(self, request, *, progress=None, on_job=None, resume_job=None, on_state=None):
        self.calls += 1
        self.requests.append(request)
        if self.busy is not None:
            raise self.busy
        if self.refuse is not None:
            raise VideoGenerationError(self.refuse)
        await on_job(f"op/{self.calls}")
        return VideoResult(data=b"MP4", job_id=f"op/{self.calls}")


class _Drifted:
    """A content check that fails the take."""

    note = None
    avoided_seen: list = []

    def failing(self, *, accept_minor=True):
        return [content_drift.Finding("boat", "middle_frame", "the boat turned into a leaf")]

    def reason(self, *, accept_minor=True):
        return "content drift: the shot does not show what its prompt asks - the boat turned into a leaf."

    def accepted_note(self, *, accept_minor=True):
        return None

    def avoided_note(self, _template):
        return None


class _Session:
    async def refresh(self, _row, *, with_for_update=False):
        return None

    async def flush(self):
        return None


@pytest.fixture
def world(monkeypatch, redis):
    """A READY shot played by a video, its worker's provider, storage and checks faked."""
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="gemini",
        model="veo-synthetic",
        prompt=PROMPT,
        params={
            "provider_id": PROVIDER,
            "duration_seconds": 8,
            "freestyle": True,
            "attempt": 0,
            frame_qc.QC_KEY: {**TAKE_FRAMES, "motion_ssim": 0.82},
            "notes": ["The first take."],
        },
        status=JOB_READY,
        file_id=TAKE_VIDEO,
        poster_file_id=TAKE_POSTER,
        duration_ms=8000,
        width=1280,
        height=720,
        has_audio=False,
        finished_at=datetime.now(UTC) - timedelta(hours=1),
        keep=False,
    )
    state = SimpleNamespace(
        row=row, provider=_Provider(), stored={}, deleted=[], enqueued=[], marks=[], content=content_drift.ContentCheck(True)
    )
    config = SimpleNamespace(id=PROVIDER, provider="gemini", display_name="Synthetic Veo")

    class _Db:
        async def get(self, _model, _key):
            return row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return state.provider, model or row.model, config

    async def mark(_kind, _job_id, **changes):
        state.marks.append(changes)
        for key, value in changes.items():
            setattr(row, key, copy.deepcopy(value) if key == "params" else value)

    async def store_path(path, **kwargs):
        file_id = uuid.uuid4()
        state.stored[file_id] = Path(path).read_bytes()
        return SimpleNamespace(id=file_id, **kwargs)

    async def download_file(file_id, path):
        key = uuid.UUID(str(file_id))
        if key not in state.stored:
            return None
        Path(path).write_bytes(state.stored[key])
        return SimpleNamespace(content_type="video/mp4")

    async def delete_files(ids):
        state.deleted.extend(str(i) for i in ids or [] if i)

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1280, height=720, has_audio=False)

    async def extract_frame(_path, *, at, out_path, width):
        Path(out_path).write_bytes(_picture())

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        Path(out_path).write_bytes(_picture(shift=int(at or 30)))

    async def enqueue(_db, kind, clip, **kwargs):
        state.enqueued.append((kind, clip.id, kwargs))

    async def content_check(*_args, **_kwargs):
        return state.content

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "download_file", download_file)
    monkeypatch.setattr(assets, "delete_files", delete_files)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", extract_frame)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    monkeypatch.setattr(content_drift, "check", content_check)
    return state


def _regenerate(world, **changes) -> clips.ClipPlan:
    return asyncio.run(clips.restart_clip(_Session(), USER, world.row, prompt="A paper boat drifts, camera still", **changes))


def _run(world) -> runners.JobOutcome:
    return asyncio.run(runners.run_clip(str(world.row.id)))


def _asked(world) -> None:
    """The fixture's shot as a new one: queued, nothing made yet."""
    row = world.row
    row.status, row.file_id, row.poster_file_id = JOB_PENDING, None, None
    row.params = {key: value for key, value in row.params.items() if key not in (frame_qc.QC_KEY, "notes")}


def _take_files() -> set[str]:
    return {str(TAKE_VIDEO), str(TAKE_POSTER), *TAKE_FRAMES.values()}


# ── The ready take is kept ────────────────────────────────────────────────


def test_a_ready_shot_generated_again_keeps_playing_its_take_while_the_new_one_is_made(world):
    plan = _regenerate(world)
    row = world.row
    assert plan.previous_take == "kept" and row.status == JOB_PENDING
    assert (row.file_id, row.poster_file_id, row.duration_ms) == (TAKE_VIDEO, TAKE_POSTER, 8000)
    assert clips.playable(row) and clips.REPLACED_MEDIA not in row.params
    # Its frames and notes wait beside it: what the inspector and the quality check read of the take that plays.
    played = clips.take_params(row.params)
    assert frame_qc.summary(played)["first_frame_file_id"] == "take-first" and played["notes"] == ["The first take."]
    assert jobs.serialize_clip(row)["first_frame_file_id"] == "take-first"
    assert jobs.serialize_clip(row)["regeneration"]["state"] == "running"


def test_a_render_and_the_preview_play_the_kept_take_while_it_is_generated_again(world):
    from app.services.video_studio import preview, render

    _regenerate(world)

    class _Rows:
        async def get(self, _model, _key):
            return world.row

    assert asyncio.run(render._ready_clip(_Rows(), "s1", str(world.row.id))) == TAKE_VIDEO
    file_id, _numbers, state = asyncio.run(preview._clip(_Rows(), str(world.row.id)))
    assert file_id == TAKE_VIDEO and state is None


def test_a_refused_regeneration_leaves_the_shot_ready_on_its_take(world):
    _regenerate(world)
    world.provider.refuse = "The video was not made: the prompt was filtered."
    assert _run(world).kind == "done"
    row = world.row
    assert row.status == JOB_READY and row.error is None and row.progress == 100
    assert (row.file_id, row.poster_file_id) == (TAKE_VIDEO, TAKE_POSTER)
    # The take's frames and notes are back; the attempt is beside it, with its reason.
    assert frame_qc.frame_file_ids(row.params) == list(TAKE_FRAMES.values())
    assert row.params["notes"] == ["The first take."]
    (attempt,) = row.params[clips.ATTEMPTS]
    assert attempt["status"] == JOB_FAILED and "prompt was filtered" in attempt["reason"] and attempt["attempt"] == 1
    # Nothing of the take was deleted, and it was asked once.
    assert not _take_files() & set(world.deleted) and world.provider.calls == 1
    state = jobs.job_state("clip", row)
    assert state.status == jobs.STATE_FAILED and state.previous_take_plays
    assert state.reason.startswith("Last regeneration failed: The video was not made") and "the previous take plays" in state.reason
    assert clips.regeneration_state(row)["state"] == "failed"


def test_a_take_failing_its_content_check_is_kept_beside_the_take_that_plays(world):
    _regenerate(world)
    world.content = _Drifted()
    _run(world)
    row = world.row
    assert row.status == JOB_READY and (row.file_id, row.poster_file_id) == (TAKE_VIDEO, TAKE_POSTER)
    (attempt,) = row.params[clips.ATTEMPTS]
    assert attempt["reason"].startswith("content drift")
    # What the attempt made stays to look at: its video, poster and three frames, none of them the take's.
    made = clips._attempt_files(attempt)
    assert attempt["file_id"] and attempt["poster_file_id"] and len(frame_qc.frame_file_ids({frame_qc.QC_KEY: attempt["frame_qc"]})) == 3
    assert not set(made) & _take_files() and not set(made) & set(world.deleted)
    assert clips.ATTEMPT_MEDIA not in row.params
    # The shot owns every one of those files: deleting it deletes them.
    assert set(made) | _take_files() <= set(clips.owned_files(row))


def test_a_new_take_that_passes_its_checks_replaces_the_kept_one(world):
    _regenerate(world)
    world.content = _Drifted()
    _run(world)
    failed = clips._attempt_files(world.row.params[clips.ATTEMPTS][0])
    _regenerate(world)
    world.content = content_drift.ContentCheck(True)
    _run(world)
    row = world.row
    assert row.status == JOB_READY and row.file_id not in (TAKE_VIDEO, None)
    assert not clips.keeps_take(row.params) and clips.ATTEMPTS not in row.params and clips.ATTEMPT_MEDIA not in row.params
    # Now, and only now, the attempt that failed beside it goes; the old take is kept to be put back (#10901).
    assert set(failed) <= set(world.deleted) and not _take_files() & set(world.deleted)
    [previous] = clips.previous_takes(row.params)
    assert previous["file_id"] == str(TAKE_VIDEO) and previous["poster_file_id"] == str(TAKE_POSTER)
    assert _take_files() <= set(clips.owned_files(row))
    assert str(row.file_id) not in world.deleted and jobs.job_state("clip", row).status == jobs.STATE_READY


def test_a_kept_attempt_is_never_paid_twice_after_a_crash(world, monkeypatch):
    _regenerate(world)
    probes = {"left": 1}
    real_probe = encoder.probe

    async def flaky(path):
        if probes["left"]:
            probes["left"] -= 1
            raise RuntimeError("ffprobe crashed")
        return await real_probe(path)

    monkeypatch.setattr(encoder, "probe", flaky)
    assert _run(world).kind == "retry"
    # The paid video is beside the take, never on the row's columns.
    assert world.row.file_id == TAKE_VIDEO and clips.attempt_file(world.row.params) in world.stored
    assert _run(world).kind == "done"
    assert world.provider.calls == 1 and world.row.status == JOB_READY and world.row.file_id != TAKE_VIDEO


def test_a_cancelled_regeneration_leaves_the_shot_ready_on_its_take(world):
    _regenerate(world)
    world.row.execution_id = None
    assert asyncio.run(jobs.cancel(_Session(), "clip", world.row))
    row = world.row
    assert row.status == JOB_READY and row.file_id == TAKE_VIDEO
    assert row.params[clips.ATTEMPTS][-1]["status"] == JOB_CANCELLED


def test_a_regeneration_the_engine_gave_up_on_leaves_the_shot_ready(world, monkeypatch):
    _regenerate(world)

    async def load(_kind, _job_id):
        return world.row

    monkeypatch.setattr(jobs, "load", load)
    asyncio.run(runners.dead_letter("clip", str(world.row.id), "The job did not finish."))
    row = world.row
    assert row.status == JOB_READY and row.file_id == TAKE_VIDEO
    assert row.params[clips.ATTEMPTS][-1]["reason"].startswith("The job did not finish.")


def test_a_shot_that_kept_its_take_expires_whole_then_is_read_expired_generated_anew_and_deleted(world, monkeypatch):
    """The retention of a ready shot keeping a take with an attempt beside it, and what it does after."""
    from app.core.config import settings
    from app.services.video_studio import quality, retention
    from app.services.video_studio.spec import parse_video_spec, validate_video_spec

    _regenerate(world)
    world.content = _Drifted()
    _run(world)
    row = world.row
    attempt = row.params[clips.ATTEMPTS][0]
    attempt_video, attempt_poster = attempt["file_id"], attempt["poster_file_id"]

    class _Db:
        """No video uses the shot; it is the one ready shot past its period."""

        async def execute(self, statement, *_args, **_kwargs):
            sql = str(statement.compile())
            if "asset_refs IS NULL" in sql:
                return SimpleNamespace(all=lambda: [])
            rows = [row] if "FROM video_clips" in sql and "status IN" not in sql else []
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows, first=lambda: None))

        async def commit(self):
            return None

    monkeypatch.setattr(settings, "video_studio_media_retention_days", 1)
    row.last_used_at = datetime.now(UTC) - timedelta(days=3)
    assert asyncio.run(retention.sweep(_Db()))["expired"] == 1
    # Its videos go - the take's and the attempt's -, its posters and frames stay with the row.
    assert row.expired_at is not None and row.file_id is None and not clips.playable(row)
    assert {str(TAKE_VIDEO), attempt_video} <= set(world.deleted)
    assert not {str(TAKE_POSTER), attempt_poster, *TAKE_FRAMES.values()} & set(world.deleted)

    # The quality check reads it expired: no take plays, so no regeneration that failed beside one.
    raw = {"title": "Synthetic", "freestyle": True, "scenes": [{"id": "boat", "type": "clip", "clip_id": str(row.id)}]}

    class _Rows:
        async def get(self, _model, _key):
            return row

    shot = asyncio.run(quality.content_facts(_Rows(), parse_video_spec(validate_video_spec(raw)), USER)).shots[str(row.id)]
    assert shot.status == "expired" and shot.regeneration_failed is None

    # Generated again, it has nothing to keep: a new generation, the expired take's remains and the attempt's
    # leftovers deleted with it - never a dead video named as the take that plays.
    world.deleted.clear()
    plan = _regenerate(world)
    assert plan.previous_take is None and row.status == JOB_PENDING and row.expired_at is None
    assert not clips.keeps_take(row.params) and clips.ATTEMPTS not in row.params
    assert {str(TAKE_POSTER), attempt_poster} <= set(row.params[clips.REPLACED_MEDIA])

    # Deleted instead, the shot names every file it ever owned: the ones already gone are named harmlessly.
    row.params[clips.ATTEMPTS] = [attempt]
    assert {attempt_video, attempt_poster} <= set(clips.owned_files(row))


def test_discard_drops_the_ready_take_and_a_failed_attempt_then_fails_the_shot(world):
    plan = _regenerate(world, discard=True)
    row = world.row
    assert plan.previous_take == "discarded" and row.file_id is None and not clips.playable(row)
    assert _take_files() <= set(row.params[clips.REPLACED_MEDIA])
    world.provider.refuse = "The video was not made."
    _run(world)
    assert row.status == JOB_FAILED and _take_files() <= set(world.deleted)


def test_a_failed_regeneration_is_said_as_such_in_the_quality_check(world):
    from app.services.video_studio import quality
    from app.services.video_studio.spec import parse_video_spec, validate_video_spec

    _regenerate(world)
    world.provider.refuse = "HTTP 429: quota exceeded"
    _run(world)
    raw = {"title": "Synthetic", "freestyle": True, "scenes": [{"id": "boat", "type": "clip", "clip_id": str(world.row.id)}]}
    spec = parse_video_spec(validate_video_spec(raw))

    class _Rows:
        async def get(self, _model, _key):
            return world.row

    facts = asyncio.run(quality.content_facts(_Rows(), spec, USER))
    shot = facts.shots[str(world.row.id)]
    assert shot.status == JOB_READY and "quota exceeded" in shot.regeneration_failed


# ── The provider's quota is the batch's ───────────────────────────────────


def test_a_429_cools_the_provider_and_the_shot_waits_in_the_queue(world, redis):
    _asked(world)
    world.provider.busy = VideoProviderBusyError("HTTP 429: quota", status_code=429, retry_after=37.0, quota=True)
    outcome = _run(world)
    # Held, no attempt spent, for the cooling's end - and as a quota: waited out up to a day, never failed sooner.
    assert outcome.kind == "hold" and 35 <= outcome.wait <= 37 and outcome.quota
    row = world.row
    # Never a failure: queued again, saying why and until when.
    assert row.status == JOB_PENDING and row.error is None and row.progress_at is None
    assert row.provider_state == provider_cooling.HELD_BY_PROVIDER
    state = jobs.job_state("clip", row)
    assert state.status == jobs.STATE_QUEUED
    # A quota is the model's: the line names it.
    assert jobs.describe(state).startswith("waiting in the queue: Synthetic Veo (veo-synthetic): quota exceeded, retry after ")
    cooling = asyncio.run(provider_cooling.cooling_of(PROVIDER, model="veo-synthetic"))
    assert cooling.reason == provider_cooling.QUOTA and cooling.status_code == 429
    assert asyncio.run(provider_cooling.cooling_of(PROVIDER, model="another-model")) is None
    # The next shot on that provider is not even asked: the batch shares the provider's state.
    assert _run(world).kind == "hold" and world.provider.calls == 1


def test_a_held_shot_is_asked_once_its_provider_cooled(world, redis):
    _asked(world)
    world.provider.busy = VideoProviderBusyError("HTTP 429", status_code=429)
    _run(world)
    redis.data.clear()
    world.provider.busy = None
    assert _run(world).kind == "done" and world.row.status == JOB_READY and world.provider.calls == 2


def test_a_refusal_without_a_wait_backs_off_doubling_and_a_job_taken_ends_the_streak(redis):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = asyncio.run(provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, now=now))
    assert first.until - now == timedelta(seconds=provider_cooling.FIRST_BACKOFF_SECONDS)
    second = asyncio.run(provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, now=first.until))
    assert second.refusals == 2 and second.until - first.until == timedelta(seconds=2 * provider_cooling.FIRST_BACKOFF_SECONDS)
    assert second.since == first.since and second.reason == provider_cooling.RATE_LIMITED
    asyncio.run(provider_cooling.clear(PROVIDER))
    assert asyncio.run(provider_cooling.cooling_of(PROVIDER, now=now)) is None


def test_an_outage_is_not_a_cooling():
    assert provider_cooling.is_refusal(VideoProviderBusyError("x", status_code=429))
    assert provider_cooling.is_refusal(VideoProviderBusyError("x", status_code=403, quota=True))
    assert not provider_cooling.is_refusal(VideoProviderBusyError("x", status_code=503))
    assert not provider_cooling.is_refusal(VideoProviderBusyError("x", status_code=None))


def test_without_redis_no_cooling_is_known_and_the_shot_alone_is_held(world, monkeypatch):
    async def unreachable():
        raise ConnectionError("refused")

    monkeypatch.setattr(provider_cooling, "redis_client", unreachable)
    _asked(world)
    world.provider.busy = VideoProviderBusyError("HTTP 429", status_code=429, retry_after=12.0)
    outcome = _run(world)
    # The shot's own hold, for the wait its provider named: no cooling for the others to read.
    assert (outcome.kind, outcome.quota, outcome.busy) == ("hold", False, True) and 12.0 <= outcome.wait <= 13.2
    assert world.row.provider_state == provider_waits.WAITING and world.row.status == JOB_PENDING


def test_a_shot_queued_on_a_cooling_provider_is_due_once_it_cools(world, redis):
    cooling = asyncio.run(provider_cooling.note_refusal(PROVIDER, label="Synthetic Veo", status_code=429, retry_after=120))
    _regenerate(world)
    ((_kind, _id, kwargs),) = world.enqueued
    assert kwargs["not_before"] == cooling.until
    assert world.row.provider_state == provider_cooling.HELD_BY_PROVIDER and world.row.provider_eta_at == cooling.until
    # The previous take still plays while the shot waits.
    assert clips.playable(world.row)


def test_validate_only_says_the_provider_cools_before_anything_is_queued(world, redis):
    cooling = asyncio.run(provider_cooling.note_refusal(PROVIDER, label="Synthetic Veo", status_code=429, retry_after=60, quota=True))
    plan = asyncio.run(clips.plan_restart(_Session(), USER, world.row))
    summary = plan.summary(cooling=cooling)
    assert summary["provider_state"]["state"] == "cooling" and summary["provider_state"]["reason"] == "quota exceeded"
    assert summary["provider_state"]["message"].startswith("Synthetic Veo: quota exceeded, retry after ")
    assert summary["previous_take"] == "kept" and summary["mode"] == clips.MODE_PROMPT
    assert world.enqueued == [] and world.row.status == JOB_READY


def test_a_cooling_provider_is_listed_with_its_models(redis):
    asyncio.run(provider_cooling.note_refusal(PROVIDER, label="Synthetic Veo", status_code=429, retry_after=60))
    found = asyncio.run(provider_cooling.coolings([(PROVIDER, "veo-a"), (str(uuid.uuid4()), "veo-a"), (None, None)]))
    assert list(found) == [(PROVIDER, "veo-a")] and found[(PROVIDER, "veo-a")].as_dict()["retry_after_seconds"] <= 60


def test_a_quota_cools_the_model_that_refused_and_a_rate_limit_the_whole_provider(redis):
    """Veo counts a quota per model, so another model of the provider still takes the shot."""
    quota = asyncio.run(
        provider_cooling.note_refusal(PROVIDER, label="Synthetic Veo", model="veo-a", status_code=429, retry_after=60, quota=True)
    )
    assert quota.model == "veo-a" and quota.sentence().startswith("Synthetic Veo (veo-a): quota exceeded, retry after ")
    assert asyncio.run(provider_cooling.cooling_of(PROVIDER, model="veo-a")) is not None
    assert asyncio.run(provider_cooling.cooling_of(PROVIDER, model="veo-b")) is None
    found = asyncio.run(provider_cooling.coolings([(PROVIDER, "veo-a"), (PROVIDER, "veo-b")]))
    assert list(found) == [(PROVIDER, "veo-a")]
    # A job taken on the other model clears nothing of the first one's quota.
    asyncio.run(provider_cooling.clear(PROVIDER, model="veo-b"))
    assert asyncio.run(provider_cooling.cooling_of(PROVIDER, model="veo-a")) is not None
    # A 429 with no quota in it is the provider's: every model meets it.
    asyncio.run(provider_cooling.note_refusal(PROVIDER, label="Synthetic Veo", model="veo-b", status_code=429))
    assert asyncio.run(provider_cooling.cooling_of(PROVIDER, model="veo-b")).model is None
    assert set(asyncio.run(provider_cooling.coolings([(PROVIDER, "veo-a"), (PROVIDER, "veo-b")]))) == {
        (PROVIDER, "veo-a"),
        (PROVIDER, "veo-b"),
    }
    # A job taken on veo-a ends both: the provider took a request, and so did the model.
    asyncio.run(provider_cooling.clear(PROVIDER, model="veo-a"))
    assert asyncio.run(provider_cooling.cooling_of(PROVIDER, model="veo-a")) is None


def test_a_batch_refused_together_is_one_refusal_and_waits_one_backoff(redis):
    """The shots of a batch refused while the cooling lasts add nothing to the backoff."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = asyncio.run(provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, now=now))
    for offset in range(1, 6):
        moment = now + timedelta(seconds=offset)
        same = asyncio.run(provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, now=moment))
        assert (same.refusals, same.until, same.since) == (1, first.until, first.since)
    # Asked again once it cooled, refused again: the next step of the backoff, one doubling.
    second = asyncio.run(provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, now=first.until))
    assert second.refusals == 2 and second.until - first.until == timedelta(seconds=2 * provider_cooling.FIRST_BACKOFF_SECONDS)
    # A refusal naming a longer wait while it cools moves the end further, never closer.
    longer = asyncio.run(
        provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, retry_after=600, now=first.until + timedelta(seconds=1))
    )
    assert longer.until == first.until + timedelta(seconds=601) and longer.refusals == 2
    shorter = asyncio.run(
        provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, retry_after=5, now=first.until + timedelta(seconds=2))
    )
    assert shorter.until == longer.until


def test_two_workers_racing_a_refusal_never_shorten_the_cooling(redis):
    """The write is a compare-and-set; one that lost to another worker is computed again from it."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    raced: list = []
    original_get = redis.get

    async def get_then_race(key):
        raw = await original_get(key)
        if not raced:
            raced.append(None)
            # Another worker records a day's quota between this worker's read and its write.
            raced[0] = await provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, retry_after=86400, now=now)
        return raw

    redis.get = get_then_race
    mine = asyncio.run(provider_cooling.note_refusal(PROVIDER, label="P", status_code=429, now=now))
    assert mine.until == raced[0].until == now + timedelta(seconds=86400) and mine.refusals == 1
    stored = provider_cooling.Cooling.loads(redis.data[provider_cooling._key(PROVIDER)])
    assert stored.until == raced[0].until


def test_a_quota_is_waited_out_for_a_day_and_a_kept_take_survives_the_bound(world, monkeypatch):
    from app.services.orchestration_adapters import video_studio as adapters
    from app.services.orchestration_engine import HOLD_PAYLOAD_KEY

    async def load(_kind, _job_id):
        return world.row

    outcomes: list = []

    async def run(_job_id, _cancel_event):
        return outcomes[-1]

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(adapters.wake, "schedule", lambda *_a: None)
    adapter = adapters.VideoClipAdapter()
    monkeypatch.setattr(adapter, "_run", run)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=lambda: asyncio.sleep(0)))
    execution = SimpleNamespace(payload={"clip_id": str(world.row.id)})
    # A quota that names a two-hour wait is held, where a plain rate limit that long fails at once.
    outcomes.append(runners.JobOutcome("hold", "Synthetic Veo: quota exceeded", wait=7200.0, quota=True))
    result = asyncio.run(adapter.execute(ctx, execution))
    assert result.kind == "defer" and result.adapter_state == adapters.RATE_LIMITED_STATE
    assert 7200 <= result.backoff_seconds <= 7260
    # A Retry-After of the whole day the cooling honours is waited out, whatever spread is added to it.
    for _ in range(20):
        outcomes.append(
            runners.JobOutcome("hold", "Synthetic Veo: quota exceeded", wait=provider_cooling.MAX_RETRY_AFTER_SECONDS, quota=True)
        )
        result = asyncio.run(adapter.execute(ctx, SimpleNamespace(payload={"clip_id": str(world.row.id)})))
        assert result.kind == "defer" and result.backoff_seconds >= provider_cooling.MAX_RETRY_AFTER_SECONDS
    # Past a day, the attempt ends - and a ready shot generated again stays ready on its take.
    _regenerate(world)
    since = (datetime.now(UTC) - provider_cooling.MAX_HOLD).isoformat()
    execution = SimpleNamespace(payload={"clip_id": str(world.row.id), HOLD_PAYLOAD_KEY: {"since": since, "count": 40}})
    result = asyncio.run(adapter.execute(ctx, execution))
    assert result.kind == "failed" and not result.retryable
    row = world.row
    assert row.status == JOB_READY and row.file_id == TAKE_VIDEO
    assert "past the 24 h a job waits for its provider" in row.params[clips.ATTEMPTS][-1]["reason"]


# ── A term the takes keep drawing, the model's own provider, a locked camera said ──


def _drew(term: str) -> content_drift.ContentCheck:
    """A content check failing on *term* of the negative prompt, shown on the middle frame."""
    shown = content_drift.Finding(term, "middle_frame", f"shows {term}, which the negative prompt excludes", excluded=True)
    return content_drift.ContentCheck(True, findings=[shown])


def test_a_term_two_takes_in_a_row_showed_is_asked_in_the_prompt_itself_the_third_time(world):
    world.row.params["negative_prompt"] = "red light, people"
    world.content = _drew("red light")
    for _attempt in range(2):
        plan = _regenerate(world)
        assert clips.INSISTED not in plan.params and not any("No red light" in note for note in plan.notes)
        _run(world)
    assert world.row.params[clips.EXCLUDED_SHOWN] == [{"attempt": 1, "terms": ["red light"]}, {"attempt": 2, "terms": ["red light"]}]
    # The third take: the term moved into the prompt as a sentence of its own, out of the negative prompt; said.
    plan = _regenerate(world)
    assert plan.params[clips.INSISTED] == {"terms": ["red light"], "model": "veo-synthetic"}
    [said] = [note for note in plan.notes if "against the negative prompt" in note]
    assert '"No red light anywhere in the shot."' in said and "has no guidance setting to lower" in said
    assert "no red light anywhere" in plan.summary()["sent_prompt"].lower()
    # It passes its check: the take made says, in its own notes, what it was asked and why.
    world.content = content_drift.ContentCheck(True)
    _run(world)
    third = world.provider.requests[-1]
    assert "no red light anywhere" in third.prompt.lower() and third.negative_prompt == "people"
    assert third.guidance is None
    assert world.row.status == JOB_READY and said in world.row.params["notes"]
    # The first two were asked as written.
    assert all("no red light" not in request.prompt.lower() for request in world.provider.requests[:2])
    # A take whose check passes ends the run of failures: the next is asked as written again.
    assert world.row.params[clips.EXCLUDED_SHOWN][-1] == {"attempt": 3, "terms": []}
    assert clips.insistent_terms(world.row.params, "red light, people", attempt=4) == []


def test_a_take_never_checked_breaks_the_run_of_takes_in_a_row(world):
    world.row.params["negative_prompt"] = "red light"
    world.content = _drew("red light")
    _regenerate(world)
    _run(world)
    # The second take is refused by the provider: no take, no content check.
    world.provider.refuse = "The video was not made: the prompt was filtered."
    _regenerate(world)
    _run(world)
    world.provider.refuse = None
    _regenerate(world)
    _run(world)
    # Two failures on the term, but not two takes in a row: the next take is asked as written.
    assert [entry["attempt"] for entry in world.row.params[clips.EXCLUDED_SHOWN]] == [1, 3]
    plan = _regenerate(world)
    assert clips.INSISTED not in plan.params


def test_a_model_with_a_guidance_setting_is_asked_with_it_lowered_on_its_own_scale(world):
    world.row.params.update(
        {
            "negative_prompt": "red light",
            "attempt": 2,
            clips.EXCLUDED_SHOWN: [{"attempt": 1, "terms": ["red light"]}, {"attempt": 2, "terms": ["Red Light"]}],
        }
    )
    world.provider.guidance = (0.0, 0.5)
    plan = _regenerate(world)
    assert plan.params[clips.INSISTED] == {"terms": ["red light"], "model": "veo-synthetic", "guidance": 0.25}
    assert any("its guidance lowered to 0.25" in note for note in plan.notes)
    world.content = _drew("red light")
    _run(world)
    request = world.provider.requests[-1]
    assert request.guidance == 0.25 and request.negative_prompt is None
    # Drawn once more on the same model: halfway again from where it stood, never under its lowest.
    params = world.row.params
    assert clips.lowered_guidance(world.provider, "veo-synthetic", params) == 0.125
    world.provider.guidance = (0.2, 0.5)
    assert clips.lowered_guidance(world.provider, "veo-synthetic", {clips.INSISTED: {"model": "veo-synthetic", "guidance": 0.05}}) == 0.2
    # Another model (a fallback chain) starts from its own default: the last lowering was on another scale.
    world.provider.guidance = (1.0, 7.0)
    assert clips.lowered_guidance(world.provider, "other-model", params) == 4.0
    assert clips.insisted_guidance(params, world.provider, "other-model") is None


def test_the_terms_asked_in_the_prompt_are_few_and_never_cut_the_prompt():
    long_term = "the reflection of an office building in the water with every one of its windows lit at night"
    negative = f"{long_term}, red light, blue light, dogs, cats, people"
    every = content_drift.excluded_terms(negative)
    params = {"attempt": 2, clips.EXCLUDED_SHOWN: [{"attempt": 1, "terms": every}, {"attempt": 2, "terms": every}]}
    # At most three, none longer than a term: a phrase that long is never written into the prompt.
    terms = clips.insistent_terms(params, negative, attempt=3)
    assert terms == ["red light", "blue light", "dogs"]
    prompt = "A long establishing shot of a quiet harbour at dawn. " * 70
    sent, rest = clips.insisted_prompts({clips.INSISTED: {"terms": terms}}, prompt, negative)
    assert sent.startswith(prompt.rstrip()) and len(sent) <= clips.MAX_PROMPT_CHARS
    assert sent.endswith("No red light anywhere in the shot. No blue light anywhere in the shot. No dogs anywhere in the shot.")
    assert rest == f"{long_term}, cats, people"


def test_a_regeneration_naming_a_model_alone_asks_that_model_s_own_provider(world, monkeypatch):
    asked: list[dict] = []
    resolved = clips.resolve

    async def resolve(db, user_id, *, model=None, provider_id=None, **kwargs):
        asked.append({"model": model, "provider_id": provider_id})
        return await resolved(db, user_id, model=model, provider_id=provider_id, **kwargs)

    monkeypatch.setattr(clips, "resolve", resolve)
    asyncio.run(clips.plan_restart(_Session(), USER, world.row, model="kling-v3"))
    # Never the previous take's provider: the model resolves to its own (``router.get_video_provider``).
    assert asked == [{"model": "kling-v3", "provider_id": None}]
    asked.clear()
    asyncio.run(clips.plan_restart(_Session(), USER, world.row))
    assert asked == [{"model": world.row.model, "provider_id": PROVIDER}]


def test_validate_only_with_a_locked_camera_says_the_model_will_not_hold_it(world, monkeypatch):
    from app.llm import router
    from app.services.builtin_tools.video_studio import _validated

    async def listed(_db, *, user_id):
        return []

    monkeypatch.setattr(router, "list_video_models_for", listed)
    world.provider.holds_camera = False
    plan = asyncio.run(clips.plan_restart(_Session(), USER, world.row, max_camera_motion=0.4))
    answer = _validated(plan, clip_id=str(world.row.id)).content
    assert "veo-synthetic does not hold a locked-off camera from a prompt without a start frame" in answer
    assert "max_camera_motion 0.40" in answer and world.enqueued == []
    # A model that holds one says nothing.
    world.provider.holds_camera = True
    plan = asyncio.run(clips.plan_restart(_Session(), USER, world.row, max_camera_motion=0.4))
    assert not any("locked-off camera" in note for note in plan.notes)


# ── The mode of a retry ───────────────────────────────────────────────────


def test_start_frame_true_with_a_new_start_and_no_reference_images_drops_the_kept_ones(world, monkeypatch):
    picture = str(uuid.uuid4())
    world.row.params[clips.REFERENCE_IMAGES] = [str(uuid.uuid4())]

    async def reference(_db, _user, **asked):
        return {"reference_file_id": picture} if asked.get("file_id") else {}

    monkeypatch.setattr(clips, "resolve_reference", reference)
    plan = asyncio.run(clips.plan_restart(_Session(), USER, world.row, start_frame=True, reference_file_id=picture))
    assert clips.REFERENCE_IMAGES not in plan.params and plan.params["reference_file_id"] == picture
    assert clips.request_mode(plan.params) == clips.MODE_START_FRAME
    assert plan.summary()["mode"] == "start_frame"
    assert any(note.startswith("Reference images dropped") for note in plan.notes)
    assert clips.mode_note(plan.params) == "Mode: start frame only (the shot continues its start picture)."


def test_the_modes_are_named_from_what_the_shot_is_sent():
    picture, reference = str(uuid.uuid4()), str(uuid.uuid4())
    assert clips.request_mode({}) == clips.MODE_PROMPT
    assert clips.request_mode({"reference_file_id": picture}) == clips.MODE_START_FRAME
    assert clips.request_mode({clips.REFERENCE_IMAGES: [reference]}) == clips.MODE_REFERENCE_IMAGES
    assert clips.request_mode({"reference_file_id": picture, clips.REFERENCE_IMAGES: [reference]}) == clips.MODE_BOTH


def test_an_empty_list_of_reference_images_is_a_valid_argument():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, _executor = build_video_studio_tools(user_id=str(USER.id))
    (tool,) = [d for d in definitions if d.name == "generate_video_clip"]
    properties = tool.input_schema["properties"]
    assert "minItems" not in properties["reference_image_file_ids"] and "minItems" not in properties["ingredients"]
    assert properties["discard"]["type"] == "boolean"
    for mode in ("start frame only", "reference images only", "both"):
        assert mode in tool.description


# ── Reading the provider's own refusal ────────────────────────────────────


def test_a_quota_is_read_off_the_errors_structure_never_its_words():
    from app.llm import provider_errors

    google = SimpleNamespace(
        details={
            "error": {
                "code": 429,
                "message": "Rate limited.",
                "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []}],
            }
        }
    )
    assert provider_errors.quota_exceeded(google)
    assert provider_errors.quota_exceeded(SimpleNamespace(body={"type": "insufficient_quota", "message": "x"}))
    # Words alone say nothing: a message naming a quota is no quota without the structure saying it.
    assert not provider_errors.quota_exceeded(SimpleNamespace(details={"error": {"code": 429, "message": "quota"}}))
    assert not provider_errors.quota_exceeded(SimpleNamespace(details=None))


def test_a_busy_error_carries_the_wait_and_the_quota_its_answer_said():
    from app.llm.video import VideoAPIError, status_error

    busy = status_error(429, VideoAPIError(429, "slow down", retry_after=12.0), "synthetic-model")
    assert isinstance(busy, VideoProviderBusyError) and busy.retry_after == 12.0 and not busy.quota
    quota = SimpleNamespace(
        code=429,
        details={"error": {"message": "x", "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure"}]}},
    )
    assert status_error(429, quota, "synthetic-model").quota
