"""One model per round: a shot is made on its author's model, waits for it, and is never moved to another.

Video b56997a2 (22-26 Oct 2026, Vertex AI ``veo-3.1-generate-001``): a take that failed its content check was made
again on ``veo-3.1-fast-generate-001`` on ElevenLabs - a chain an earlier call had named, inherited by every
regeneration - on a provider out of credits; Vertex ending its operations "experiencing high load" failed three
regenerations of one shot; ElevenLabs' 401 ``quota_exceeded`` failed every call for a day while ``validate_only`` said
nothing. Synthetic prompts, a Vertex provider on a fake client, an ElevenLabs stub, an in-memory Redis:

* F47 an account with 1,000 credits asked a shot costing 7,272: refused at ``validate_only`` with the numbers;
* F48 a Vertex take failing its checks: left failed on Vertex - no other provider asked, ``fallback.tried`` empty - and
  made again on Vertex unless the call names another model;
* F49 a 401 ``quota_exceeded``: the shot waits, its model cooling with the credits it said, no other provider asked;
* F50 Vertex ending five operations for its load, then making the shot: one ready shot, five waits logged on the
  schedule, zero failures, the same model.
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.llm import video
from app.llm.elevenlabs_provider import ElevenLabsProvider
from app.llm.vertex_ai_provider import VertexAIProvider
from app.llm.video import VideoAPIError, VideoCredits, VideoProviderBusyError, VideoRequest, VideoResult
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import (
    assets,
    clips,
    content_drift,
    encoder,
    jobs,
    provider_cooling,
    provider_waits,
    runners,
)

pytestmark = pytest.mark.usefixtures("no_default_video_template")

USER = SimpleNamespace(id=uuid.uuid4(), email="maker@example.com", role="user")
VERTEX_MODEL, FAST_MODEL = "veo-3.1-generate-001", "veo-3.1-fast-generate-001"
VERTEX_ID, ELEVEN_ID = str(uuid.uuid4()), str(uuid.uuid4())
CONFIGS = {
    VERTEX_ID: SimpleNamespace(id=VERTEX_ID, provider="vertex_ai", display_name="Vertex AI"),
    ELEVEN_ID: SimpleNamespace(id=ELEVEN_ID, provider="elevenlabs", display_name="ElevenLabs"),
}
PROMPT = "A slow push across a security operations centre at night, analysts at their desks"
HIGH_LOAD = "The service is currently experiencing high load and cannot process your request. Please try again later."


def _picture() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 180), (40, 60, 90)).save(out, format="JPEG")
    return out.getvalue()


class _Redis:
    """The few Redis commands the cooling uses, in memory."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def mget(self, keys):
        return [self.data.get(key) for key in keys]

    async def delete(self, *keys):
        return sum(self.data.pop(key, None) is not None for key in keys)

    async def eval(self, script, _numkeys, key, expected, value, _ttl):
        assert script == provider_cooling._CAS_SCRIPT
        current = self.data.get(key)
        if (current is None and expected == "") or current == expected:
            self.data[key] = value
            return 1
        return 0


class _Veo:
    """Veo on Vertex AI's operations, faked: each generation an operation, the next *load* of them ended by the service
    for its load (a ``google.rpc.Status`` of ``RESOURCE_EXHAUSTED``), the rest made."""

    def __init__(self):
        self.load = 0
        self.started = 0

    async def generate_videos(self, **_kwargs):
        self.started += 1
        return SimpleNamespace(done=False, name=f"projects/p/operations/{self.started}", error=None, response=None)

    async def get(self, operation):
        if self.load:
            self.load -= 1
            return SimpleNamespace(done=True, name=operation.name, error={"code": 8, "message": HIGH_LOAD}, response=None)
        made = SimpleNamespace(generated_videos=[SimpleNamespace(video=SimpleNamespace(video_bytes=b"VEO-MP4"))])
        return SimpleNamespace(done=True, name=operation.name, error=None, response=made)


def _vertex(veo: _Veo) -> VertexAIProvider:
    provider = VertexAIProvider.__new__(VertexAIProvider)
    provider.project, provider.location, provider.service_account = "p", "us-central1", "sa@p.iam.gserviceaccount.com"
    provider._api_key = ""
    provider.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_videos=veo.generate_videos), operations=SimpleNamespace(get=veo.get))
    )
    return provider


class _Other:
    """Every other provider of the platform - ElevenLabs serving ``veo-3.1-fast-generate-001``: its calls are counted,
    and the shot under test must never make one unless the call names its model."""

    def __init__(self):
        self.calls = 0
        self.credits: VideoCredits | None = None
        self.raises: list[BaseException] = []

    def takes_negative_prompt(self, _model):
        return True

    def video_guidance(self, _model):
        return None

    def holds_locked_camera(self, _model, *, start_frame):
        return None

    def max_reference_images(self, _model):
        return 0

    def reference_images_with_start_frame(self, _model):
        return False

    async def video_credits(self, _request):
        return self.credits

    async def generate_video(self, request, *, progress=None, on_job=None, resume_job=None, on_state=None):
        self.calls += 1
        if self.raises:
            raise self.raises.pop(0)
        await on_job(f"eleven/{self.calls}")
        return VideoResult(data=b"ELEVEN-MP4", job_id=f"eleven/{self.calls}")


@pytest.fixture
def world(monkeypatch):
    """A shot of *USER*, queued on Vertex AI: the platform's video model, the one the author chose."""
    redis = _Redis()
    veo = _Veo()
    state = SimpleNamespace(
        veo=veo,
        vertex=_vertex(veo),
        other=_Other(),
        resolved=[],
        drifting=set(),
        marks=[],
        stored={},
        enqueued=[],
        redis=redis,
    )
    row = VideoClip(id=uuid.uuid4(), user_id=USER.id, provider="vertex_ai", model=VERTEX_MODEL, prompt=PROMPT, params={}, status=JOB_PENDING)
    state.row = row

    class _Db:
        async def get(self, _model, _key, **_kwargs):
            return row

        async def commit(self):
            return None

        async def flush(self):
            return None

        async def refresh(self, _row, with_for_update=False):
            return None

        async def rollback(self):
            return None

        def add(self, _row):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        # The platform's model is Vertex's; FAST_MODEL is ElevenLabs' alone (``router.get_video_provider``).
        chosen = model or VERTEX_MODEL
        state.resolved.append(chosen)
        if chosen == FAST_MODEL:
            return state.other, chosen, CONFIGS[ELEVEN_ID]
        return state.vertex, chosen, CONFIGS[VERTEX_ID]

    async def mark(_kind, _job_id, **changes):
        state.marks.append(changes)
        for key, value in changes.items():
            setattr(row, key, copy.deepcopy(value) if key == "params" else value)

    async def store_path(path, **kwargs):
        file_id = uuid.uuid4()
        state.stored[file_id] = Path(path).read_bytes()
        return SimpleNamespace(id=file_id, **kwargs)

    async def download_file(file_id, path):
        data = state.stored.get(uuid.UUID(str(file_id)))
        if data is None:
            return None
        Path(path).write_bytes(data)
        return SimpleNamespace(content_type="video/mp4")

    async def delete_files(_ids):
        return None

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1280, height=720, has_audio=False)

    async def extract_frame(_path, *, at, out_path, width):
        Path(out_path).write_bytes(_picture())

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        Path(out_path).write_bytes(_picture())

    async def enqueue(_db, kind, clip, **kwargs):
        state.enqueued.append((kind, clip.model, kwargs))

    async def content_check(*_args, **_kwargs):
        if row.model in state.drifting:
            return content_drift.ContentCheck(True, [content_drift.Finding("analysts", "middle_frame", "the analysts turned into mannequins")])
        return content_drift.ContentCheck(True)

    async def client():
        return redis

    async def woken():
        return None

    monkeypatch.setattr(video, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(provider_cooling, "redis_client", client)
    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(jobs, "wake", woken)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "download_file", download_file)
    monkeypatch.setattr(assets, "delete_files", delete_files)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", extract_frame)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    monkeypatch.setattr(content_drift, "check", content_check)
    state.session = session
    return state


def _queue(world, **asked) -> clips.ClipPlan:
    """A new shot as ``generate_video_clip`` plans it (no model named: the platform's), queued on the world's row."""
    plan = asyncio.run(clips.plan_clip(None, USER, prompt=PROMPT, freestyle=True, **asked))
    row = world.row
    row.params, row.model, row.provider, row.status = dict(plan.params), plan.request.model, plan.config.provider, JOB_PENDING
    return plan


def _run(world) -> runners.JobOutcome:
    return asyncio.run(runners.run_clip(str(world.row.id)))


def _regenerate(world, **changes) -> clips.ClipPlan:
    async def again():
        async with world.session() as db:
            return await clips.restart_clip(db, USER, world.row, **changes)

    return asyncio.run(again())


# ── F48: a take failing its checks stays on Vertex ────────────────────────


def test_f48_a_vertex_take_failing_its_checks_is_left_failed_and_no_other_provider_is_asked(world):
    _queue(world)
    world.drifting = {VERTEX_MODEL}
    assert _run(world).kind == "done"
    row = world.row
    assert row.status == JOB_FAILED and row.error.startswith("content drift:") and row.model == VERTEX_MODEL
    assert world.veo.started == 1 and world.other.calls == 0 and world.enqueued == []
    # Nothing fell back: ``fallback.tried`` is empty - the answer has no fallback to say at all.
    assert jobs.serialize_clip(row)["fallback"] is None and clips.fallback_note(row) is None
    assert set(world.resolved) == {VERTEX_MODEL}


def test_f48_a_regeneration_stays_on_the_shots_model_and_drops_a_chain_an_earlier_call_named(world):
    # b56997a2: an earlier call had named the fast model on ElevenLabs; every regeneration since inherited it.
    _queue(world, fallback_models=[FAST_MODEL])
    assert [item["model"] for item in world.row.params[clips.FALLBACK_MODELS]] == [FAST_MODEL]
    world.row.status, world.row.error = JOB_FAILED, "content drift: ..."
    plan = _regenerate(world, prompt=f"{PROMPT}, calm camera")
    row = world.row
    assert plan.request.model == VERTEX_MODEL and row.model == VERTEX_MODEL and row.params["provider_id"] == VERTEX_ID
    assert clips.FALLBACK_MODELS not in row.params and plan.summary()["fallback_models"] == []
    # Its take fails its checks again: left failed on Vertex, ElevenLabs never asked.
    world.drifting = {VERTEX_MODEL}
    _run(world)
    assert row.status == JOB_FAILED and world.other.calls == 0 and world.veo.started == 1
    assert jobs.serialize_clip(row)["fallback"] is None
    # Made again on Vertex, again: a regeneration keeps the shot's model.
    world.drifting = set()
    _regenerate(world)
    _run(world)
    assert row.status == JOB_READY and row.model == VERTEX_MODEL and world.veo.started == 2 and world.other.calls == 0


def test_f48_another_model_is_asked_only_when_the_call_names_it(world):
    _queue(world)
    world.drifting = {VERTEX_MODEL}
    _run(world)
    plan = _regenerate(world, model=FAST_MODEL)
    assert plan.request.model == FAST_MODEL and world.row.params["provider_id"] == ELEVEN_ID
    world.drifting = set()
    _run(world)
    assert world.row.status == JOB_READY and world.row.model == FAST_MODEL and world.other.calls == 1


def test_f48_a_shot_whose_model_is_gone_is_refused_never_handed_to_the_platforms(world, monkeypatch):
    _queue(world)
    world.row.status = JOB_FAILED

    async def gone(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        if model == VERTEX_MODEL:
            raise clips.ClipError(f"{VERTEX_MODEL} was retired by its provider on 2026-10-20: pick another video model.", about_model=True)
        return world.other, model or FAST_MODEL, CONFIGS[ELEVEN_ID]

    monkeypatch.setattr(clips, "resolve", gone)
    with pytest.raises(clips.ClipError, match=rf"^The shot's model {VERTEX_MODEL} can no longer make it .*: name the model") as refused:
        _regenerate(world)
    assert refused.value.about_model and world.row.model == VERTEX_MODEL and world.enqueued == []


# ── F53: a model several providers offer, made on the video's ─────────────


def test_f53_a_shot_for_a_video_is_asked_of_the_provider_its_shots_of_that_model_are_made_on(world, monkeypatch):
    asked: list[dict] = []
    said = f"{VERTEX_MODEL} is offered by Vertex AI, ElevenLabs: asked of Vertex AI, {clips.VIDEO_SHOTS_CHOICE} - ..."

    async def resolve(_db, _user_id, *, model=None, provider_id=None, prefer=(), notes=None):
        asked.append({"model": model, "provider_id": provider_id, "prefer": list(prefer)})
        if notes is not None:
            notes.append(said)
        return world.vertex, model or VERTEX_MODEL, CONFIGS[VERTEX_ID]

    async def providers(_db, _user, video_id, model):
        return [(VERTEX_ID, clips.VIDEO_SHOTS_CHOICE)] if video_id and model else []

    async def no_direction(*_args, **_kwargs):
        return None

    async def keeps_sound(*_args, **_kwargs):
        return False

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "video_providers", providers)
    monkeypatch.setattr(clips, "resolve_direction", no_direction)
    monkeypatch.setattr(clips, "video_drops_clip_sound", keeps_sound)
    video_id = str(uuid.uuid4())
    plan = asyncio.run(clips.plan_clip(None, USER, prompt=PROMPT, model=VERTEX_MODEL, video_id=video_id))
    assert asked[-1] == {"model": VERTEX_MODEL, "provider_id": None, "prefer": [(VERTEX_ID, clips.VIDEO_SHOTS_CHOICE)]}
    # The answer names the provider it is asked of, and why.
    summary = plan.summary()
    assert summary["provider"] == "Vertex AI" and summary["provider_id"] == VERTEX_ID and said in summary["notes"]
    # A provider named is asked as named: no preference read.
    asyncio.run(clips.plan_clip(None, USER, prompt=PROMPT, model=VERTEX_MODEL, provider_id=ELEVEN_ID, video_id=video_id))
    assert asked[-1]["prefer"] == [] and asked[-1]["provider_id"] == ELEVEN_ID
    # A regeneration naming another model alone prefers the shot's own provider, then its video's.
    world.row.status, world.row.params = JOB_FAILED, {"provider_id": VERTEX_ID}
    _regenerate(world, model=VERTEX_MODEL, video_id=video_id)
    assert asked[-1]["prefer"] == [(VERTEX_ID, clips.OWN_SHOT_CHOICE), (VERTEX_ID, clips.VIDEO_SHOTS_CHOICE)]


# ── F50: a busy provider is waited for ────────────────────────────────────


def test_f50_vertex_ending_five_operations_for_its_load_then_making_the_shot(world):
    _queue(world)
    world.veo.load = 5
    row = world.row
    waits: list[float] = []
    for attempt in range(1, 6):
        before = datetime.now(UTC)
        outcome = _run(world)
        # Back to the queue, never failed, no attempt spent: the exact wait of the schedule, the shot's own spread.
        assert (outcome.kind, outcome.busy, outcome.quota) == ("hold", True, False)
        base = provider_waits.SCHEDULE_SECONDS[attempt - 1]
        assert base <= outcome.wait <= base + min(base * provider_waits.SPREAD_SHARE, provider_waits.SPREAD_MAX_SECONDS)
        waits.append(outcome.wait)
        assert row.status == JOB_PENDING and row.error is None and row.provider_state == provider_waits.WAITING
        # The operation the service ended is never resumed: the next try asks anew.
        assert row.provider_job_id is None
        assert before + timedelta(seconds=outcome.wait - 1) <= row.provider_eta_at <= datetime.now(UTC) + timedelta(seconds=outcome.wait + 1)
        line = jobs.describe(jobs.job_state("clip", row))
        assert line.startswith(f"provider busy, next try at {row.provider_eta_at:%H:%M} UTC (in ")
        assert f", {attempt} {'try' if attempt == 1 else 'tries'}: Vertex AI ({VERTEX_MODEL}) - " in line
        assert HIGH_LOAD in line and line.endswith("- it starts by itself then, nothing failed")
    assert waits == sorted(waits)
    outcome = _run(world)
    assert outcome.kind == "done" and row.status == JOB_READY and row.error is None
    # One ready shot on the same model, its five waits logged, nothing failed and nothing asked of another provider.
    assert row.model == VERTEX_MODEL and world.veo.started == 6 and world.other.calls == 0
    record = row.params[provider_waits.KEY]
    assert record["tries"] == 5 and record["model"] == VERTEX_MODEL and record["taken_at"] and "next_at" not in record
    assert not any(mark.get("status") == JOB_FAILED for mark in world.marks)


def test_a_busy_hold_waits_its_exact_time_for_up_to_a_day_then_ends_saying_so(world, monkeypatch):
    from app.services.orchestration_adapters import video_studio as adapters
    from app.services.orchestration_engine import HOLD_PAYLOAD_KEY

    outcomes: list = []

    async def run(_job_id, _cancel_event):
        return outcomes[-1]

    async def dead(_kind, _job_id, reason, **_kwargs):
        outcomes.append(reason)

    monkeypatch.setattr(adapters.wake, "schedule", lambda *_a: None)
    monkeypatch.setattr(runners, "dead_letter", dead)
    adapter = adapters.VideoClipAdapter()
    monkeypatch.setattr(adapter, "_run", run)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=lambda: asyncio.sleep(0)))
    # Twelve hours in, the provider still busy: deferred the very wait its row says, never spread again.
    since = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    outcomes.append(runners.JobOutcome("hold", "provider busy", wait=612.5, busy=True))
    execution = SimpleNamespace(payload={"clip_id": str(world.row.id), HOLD_PAYLOAD_KEY: {"since": since, "count": 60}})
    result = asyncio.run(adapter.execute(ctx, execution))
    assert result.kind == "defer" and result.backoff_seconds == 612.5
    # Past a day it ends, saying it waited a day for its provider - the only way a busy provider ends a shot.
    since = (datetime.now(UTC) - provider_cooling.MAX_HOLD).isoformat()
    execution = SimpleNamespace(payload={"clip_id": str(world.row.id), HOLD_PAYLOAD_KEY: {"since": since, "count": 140}})
    result = asyncio.run(adapter.execute(ctx, execution))
    assert result.kind == "failed" and not result.retryable
    assert "Busy for 1440 min" in outcomes[-1] and "past the 24 h a job waits for its provider" in outcomes[-1]


def test_an_operation_the_service_ended_for_its_state_is_busy_and_any_other_failure_is_final():
    from app.llm.provider_errors import rpc_busy_status

    # Read off the status's code (or its canonical name), never its words.
    assert [rpc_busy_status({"code": code}) for code in (4, 8, 14)] == [504, 429, 503]
    assert rpc_busy_status({"status": "RESOURCE_EXHAUSTED", "message": "anything"}) == 429
    assert rpc_busy_status({"code": 3, "message": HIGH_LOAD}) is None
    assert rpc_busy_status({"code": 13}) is None and rpc_busy_status("high load") is None
    veo = _Veo()
    veo.load = 1
    with pytest.raises(VideoProviderBusyError) as busy:
        asyncio.run(_vertex(veo).generate_video(VideoRequest(model=VERTEX_MODEL, prompt=PROMPT)))
    assert busy.value.status_code == 429 and busy.value.started and HIGH_LOAD in str(busy.value)


# ── F47, F49: the quota, read before paying and waited for ────────────────


def test_f47_an_account_whose_credits_cannot_pay_for_the_shot_is_refused_at_validate_only(world, monkeypatch):
    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    world.other.credits = VideoCredits(remaining=1000, needed=7272)
    with pytest.raises(clips.ClipError, match=r"^quota: 1000 remaining, 7272 needed - the ElevenLabs account cannot pay") as refused:
        asyncio.run(clips.plan_clip(None, USER, prompt=PROMPT, model=FAST_MODEL, freestyle=True))
    assert not refused.value.about_model

    async def find_user(_db, _user_id):
        return USER

    monkeypatch.setattr(tool, "async_session", world.session)
    monkeypatch.setattr(tool, "_user", find_user)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER.id))
    answer = asyncio.run(
        executor.execute_tool(
            ToolCall(id="c1", name="generate_video_clip", arguments={"prompt": PROMPT, "model": FAST_MODEL, "validate_only": True})
        )
    )
    head = json.loads(answer.content.splitlines()[0])
    # Nothing queued or paid, nothing the agent's loop counts as a failure - and no other model offered.
    assert head["status"] == "invalid_parameters" and head["queued"] is False and not answer.is_error
    assert head["reason"].startswith("quota: 1000 remaining, 7272 needed")
    assert "video_studio_capabilities" not in head["reason"]
    assert world.other.calls == 0 and world.enqueued == []
    # With the credits there, it plans; one the provider does not say is asked as before.
    world.other.credits = VideoCredits(remaining=9000, needed=7272)
    assert asyncio.run(clips.plan_clip(None, USER, prompt=PROMPT, model=FAST_MODEL, freestyle=True)).request.model == FAST_MODEL
    world.other.credits = VideoCredits(remaining=None, needed=7272)
    assert asyncio.run(clips.plan_clip(None, USER, prompt=PROMPT, model=FAST_MODEL, freestyle=True)).request.model == FAST_MODEL


def test_f49_a_401_quota_refusal_has_the_shot_wait_its_model_cooling_and_no_other_provider_asked(world):
    _queue(world, model=FAST_MODEL)
    refusal = VideoAPIError(
        401,
        "This request exceeds your quota.",
        payload={
            "detail": {
                "status": "quota_exceeded",
                "message": "This request exceeds your quota of 10000. You have 3,448 credits remaining, while 7,272 "
                "credits are required for this request.",
            }
        },
    )
    from app.llm.elevenlabs_provider import _quota_refusal

    world.other.raises = [_quota_refusal(refusal, FAST_MODEL)]
    outcome = _run(world)
    row = world.row
    assert (outcome.kind, outcome.quota) == ("hold", True) and row.status == JOB_PENDING and row.error is None
    assert row.provider_state == provider_cooling.HELD_BY_PROVIDER and world.other.calls == 1 and world.veo.started == 0
    cooling = asyncio.run(provider_cooling.cooling_of(ELEVEN_ID, model=FAST_MODEL))
    assert cooling.reason == provider_cooling.QUOTA and (cooling.remaining, cooling.needed) == (3448, 7272)
    # Cooling for that model for the hour a quota naming no wait cools.
    assert 3590 <= cooling.seconds_left() <= provider_cooling.QUOTA_COOLING_SECONDS
    said = jobs.describe(jobs.job_state("clip", row))
    assert said.startswith(f"waiting in the queue: ElevenLabs ({FAST_MODEL}): quota exceeded (quota: 3448 remaining, 7272 needed)")
    # validate_only says it now, with the numbers - and the next shot asked of it is refused before it is paid.
    plan = asyncio.run(clips.plan_restart(None, USER, VideoClip(id=uuid.uuid4(), user_id=USER.id, provider="elevenlabs", model=FAST_MODEL, prompt=PROMPT, params=dict(row.params), status=JOB_FAILED)))
    state = plan.summary(cooling=cooling)["provider_state"]
    assert (state["state"], state["remaining"], state["needed"]) == ("cooling", 3448, 7272)
    world.other.credits = VideoCredits(remaining=3448)
    with pytest.raises(clips.ClipError, match=r"^quota: 3448 remaining, 7272 needed"):
        asyncio.run(clips.plan_clip(None, USER, prompt=PROMPT, model=FAST_MODEL, freestyle=True))
    # The worker asks nothing while it cools, and never another provider.
    assert _run(world).kind == "hold" and world.other.calls == 1 and world.veo.started == 0


def test_f49_credits_read_short_at_the_worker_cool_the_model_before_it_is_paid(world):
    _queue(world, model=FAST_MODEL)
    world.other.credits = VideoCredits(remaining=1000, needed=7272)
    outcome = _run(world)
    assert (outcome.kind, outcome.quota) == ("hold", True) and world.other.calls == 0 and world.veo.started == 0
    cooling = asyncio.run(provider_cooling.cooling_of(ELEVEN_ID, model=FAST_MODEL))
    assert (cooling.reason, cooling.remaining, cooling.needed) == (provider_cooling.QUOTA, 1000, 7272)


def test_elevenlabs_reads_its_credits_and_its_quota_refusal_off_its_own_answers():
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/user/subscription"
        return httpx.Response(200, json={"character_count": 96552, "character_limit": 100000})

    provider = ElevenLabsProvider("key-elevenlabs-test")
    transport = httpx.MockTransport(answer)
    provider._client = lambda timeout: httpx.AsyncClient(base_url=provider.base_url, transport=transport, timeout=timeout)
    assert asyncio.run(provider.video_credits(VideoRequest(model=FAST_MODEL, prompt=PROMPT))) == VideoCredits(remaining=3448)
    provider._client = lambda timeout: httpx.AsyncClient(
        base_url=provider.base_url, transport=httpx.MockTransport(lambda _r: httpx.Response(401, json={"detail": "no"})), timeout=timeout
    )
    assert asyncio.run(provider.video_credits(VideoRequest(model=FAST_MODEL, prompt=PROMPT))) is None
    from app.llm.elevenlabs_provider import _quota_refusal

    # Any other refusal is the request's own: final, never a quota.
    assert _quota_refusal(VideoAPIError(401, "Invalid API key", payload={"detail": {"status": "invalid_api_key"}}), FAST_MODEL) is None
    assert _quota_refusal(VideoAPIError(None, "unreachable"), FAST_MODEL) is None
