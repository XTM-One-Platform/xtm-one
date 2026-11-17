"""Video Studio jobs as platform events: ``video.*`` triggers.

Covers the event a job's end raises (kind, status, scene preview), the stable
event id that makes the same end one event, the payload's scope (the
requester's assignments only, the agents of the conversations that started
it), the catalog, and that every adapter raises it next to the conversation
wake.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.orchestration import OrchestrationExecution
from app.services import platform_events as pe
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import job_events, wake

USER = uuid.uuid4()
AGENT = uuid.uuid4()


@pytest.mark.parametrize(
    ("kind", "status", "preview", "expected"),
    [
        ("render", "ready", False, "video.render.finished"),
        ("render", "failed", False, "video.render.failed"),
        ("render", "ready", True, "video.scene_preview.finished"),
        ("render", "failed", True, "video.scene_preview.failed"),
        ("clip", "ready", False, "video.clip.ready"),
        ("clip", "failed", False, "video.clip.failed"),
        ("music", "ready", False, "video.music.ready"),
        ("music", "failed", False, "video.music.failed"),
        ("render", "running", False, None),
        ("render", "cancelled", False, None),
        ("take", "ready", False, None),
    ],
)
def test_event_name_of_each_job_end(kind, status, preview, expected):
    assert job_events.event_name(kind, status, scene_preview=preview) == expected


def test_every_video_event_is_in_the_catalog():
    names = {
        job_events.event_name(kind, status, scene_preview=preview)
        for kind in job_events.EVENT_KINDS
        for status in ("ready", "failed")
        for preview in (False, True)
    }
    assert names <= set(pe.PLATFORM_EVENTS)
    assert pe.trigger_config_error(pe.TRIGGER_TYPE, {"event": sorted(names)}) is None


def test_the_same_end_is_one_event_and_another_end_another():
    job = uuid.uuid4()
    at = datetime(2026, 11, 1, 11, 0, tzinfo=UTC)
    first = job_events.event_id("render", job, "ready", at)
    assert first == job_events.event_id("render", job, "ready", at)
    assert first != job_events.event_id("render", job, "failed", at)
    assert first != job_events.event_id("render", job, "ready", datetime(2026, 11, 1, 12, 0, tzinfo=UTC))


def _render(**extra) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "user_id": USER,
        "scene_id": None,
        "revision": 4,
        "quality": "high",
        "status": "ready",
        "checks": {"findings": [{"kind": "stutter", "at": 1.0}, {"kind": "frozen", "at": 2.0}, "noise"]},
        "error": None,
        "file_id": uuid.uuid4(),
    }
    base.update(extra)
    return SimpleNamespace(**base)


def test_render_payload_names_the_video_its_findings_and_its_requester():
    row = _render()
    data = job_events.job_data("render", row, project=SimpleNamespace(title="Launch"), participant_agent_ids=[AGENT])
    assert data["video_id"] == str(row.project_id) and data["title"] == "Launch"
    assert data["revision"] == 4 and data["quality"] == "high" and data["findings_count"] == 2
    assert data["object_type"] == "video_render" and data["object_id"] == str(row.id)
    assert data["personal_owner_id"] == str(USER) and data["participant_agent_ids"] == [str(AGENT)]


def test_a_video_event_reaches_only_its_requesters_agents():
    data = job_events.job_data("render", _render(), participant_agent_ids=[AGENT])
    mine = pe.Subscription(uuid.uuid4(), AGENT, ("video.render.finished",), {}, False, user_id=USER)
    other_user = pe.Subscription(uuid.uuid4(), AGENT, ("video.render.finished",), {}, False, user_id=uuid.uuid4())
    other_agent = pe.Subscription(uuid.uuid4(), uuid.uuid4(), ("video.render.finished",), {}, False, user_id=USER)
    assert pe.in_scope(mine, data)
    assert not pe.in_scope(other_user, data)
    assert not pe.in_scope(other_agent, data)


def test_a_failed_shot_bounds_its_error_and_has_no_video():
    row = SimpleNamespace(id=uuid.uuid4(), user_id=USER, status="failed", error="x" * 2000, name="Sky", file_id=None)
    data = job_events.job_data("clip", row)
    assert data["video_id"] is None and data["title"] == "Sky" and data["findings_count"] == 0
    assert len(data["error"]) == 500 and data["object_type"] == "video_clip"


@pytest.mark.asyncio
async def test_publish_passes_a_stable_event_id(monkeypatch):
    from app.services import event_bus as bus_module

    seen: list = []

    async def emit(name, data=None, *, wait=False):
        seen.append(data)

    monkeypatch.setattr(bus_module.event_bus, "emit", emit)
    assert await pe.publish("video.music.ready", {"object_id": "m"}, event_id="e-1") == "e-1"
    assert seen[0]["event_id"] == "e-1"


@pytest.mark.asyncio
async def test_a_job_still_in_flight_raises_nothing(monkeypatch):
    published: list = []

    async def load(kind, job_id):
        return _render(status="running")

    async def publish(name, data, *, event_id=None):
        published.append(name)

    monkeypatch.setattr("app.services.video_studio.jobs.load", load)
    monkeypatch.setattr(pe, "publish", publish)
    assert await job_events.publish_ended("render", uuid.uuid4()) is None
    assert await job_events.publish_ended("take", uuid.uuid4()) is None
    assert published == []


@pytest.mark.asyncio
async def test_publish_ended_never_raises(monkeypatch):
    async def load(kind, job_id):
        raise RuntimeError("database gone")

    monkeypatch.setattr("app.services.video_studio.jobs.load", load)
    assert await job_events.publish_ended("clip", uuid.uuid4()) is None


def test_scheduling_outside_an_event_loop_is_a_no_op():
    job_events.schedule("render", "r-1")


@pytest.mark.parametrize("kind", ["clip", "music"])
def test_every_adapter_raises_the_event_next_to_the_wake(monkeypatch, kind):
    woken: list[tuple[str, str]] = []
    raised: list[tuple[str, str]] = []
    monkeypatch.setattr(wake, "schedule", lambda k, job_id: woken.append((k, str(job_id))))
    monkeypatch.setattr(job_events, "schedule", lambda k, job_id: raised.append((k, str(job_id))))
    adapter = {"clip": adapters.VideoClipAdapter, "music": adapters.VideoMusicAdapter}[kind]()

    async def run(_job_id, _cancel_event, **_slot):
        return SimpleNamespace(kind="done", error=None)

    monkeypatch.setattr(adapter, "_run", run)

    class _Db:
        async def commit(self):
            return None

    ctx = SimpleNamespace(db=_Db(), cancel_event=None, worker_id="w1")
    asyncio.run(adapter.execute(ctx, OrchestrationExecution(payload={f"{kind}_id": "job-1"})))
    assert woken == raised == [(kind, "job-1")]


def test_the_dead_letter_raises_the_event(monkeypatch):
    raised: list[tuple[str, str]] = []
    monkeypatch.setattr(wake, "schedule", lambda *_a: None)
    monkeypatch.setattr(job_events, "schedule", lambda k, job_id: raised.append((k, str(job_id))))

    async def dead_letter(kind, job_id, error, *, lease=None):
        return None

    monkeypatch.setattr("app.services.video_studio.runners.dead_letter", dead_letter)
    monkeypatch.setattr("app.services.orchestration_engine.lease_expiry", lambda _execution: None)

    class _Db:
        async def commit(self):
            return None

    execution = OrchestrationExecution(payload={"render_id": "r-9"}, last_error="Deadline exceeded")
    asyncio.run(adapters.VideoRenderAdapter().on_dead_letter(SimpleNamespace(db=_Db()), execution))
    assert raised == [("render", "r-9")]
