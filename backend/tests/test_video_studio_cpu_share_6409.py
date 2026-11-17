"""The CPUs a whole render keeps beside the preview lane and the recordings. Synthetic workers, rows and clocks.

An ultra render on 8 pages of 8 CPUs drew at 0.30x its pre-flight, its pages getting 3.7 of the worker's 8 CPUs while
2 scene previews and a recording ran beside it, every job at the worker's priority:

* a render reserves its pages' CPUs - the worker's preview slots shrink to what is left - and a preview starts at
  background priority: beside two previews its pages keep at least 75% of the worker's CPUs;
* the pre-flight prices a render against its worker's committed load, and says the shared estimate beside the idle one;
* ``get_video_jobs`` on a render lists the jobs sharing its worker with their owner and video, and which the caller
  may cancel - one the caller may not read by its kind alone;
* the render's measure keeps the CPUs its pages got: its time corrects the next estimates at that share, over the part
  of its run it shared its worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.video_studio import VideoRender
from app.services import orchestration_reconciler
from app.services.orchestration_adapters.video_studio import VideoPreviewAdapter
from app.services.video_studio import (
    compositor,
    cpu_share,
    jobs,
    render_history,
    render_pace,
    render_preflight,
    render_process,
    resources,
)
from app.services.video_studio.cpu_share import Job
from app.services.video_studio.render_pace import Neighbour, PaceWatch

UHD = (3840, 2160)
TWO_PREVIEWS = [Neighbour("scene preview", "a1" * 16, scene="s03"), Neighbour("scene preview", "b2" * 16, scene="s07")]


@pytest.fixture
def worker(monkeypatch):
    """A worker of 8 CPUs drawing on 8 pages, with 2 preview slots and nothing running yet."""
    monkeypatch.setattr(resources, "cpu_budget", lambda: 8)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 2)
    monkeypatch.setattr(settings, "video_studio_render_pages", 8)
    monkeypatch.setattr(render_process, "_ATTEMPTS", {})
    monkeypatch.setattr(render_process, "_ADMITTED", {})
    monkeypatch.setattr(orchestration_reconciler, "_inflight_by_adapter", {})
    return render_process._ATTEMPTS


# -- 1. An ultra render beside two previews -----------------------------------------------------


def test_an_ultra_render_beside_two_previews_keeps_three_quarters_of_its_worker_s_cpus(worker, monkeypatch):
    preview_lane = VideoPreviewAdapter()
    worker["p1"] = render_process._Attempt(preview=True)
    worker["p2"] = render_process._Attempt(preview=True)
    # Two previews and no render: both preview slots, nothing held back.
    assert render_process.cpus_reserved() == 0 and render_process.preview_slots() == 2
    assert preview_lane.slots_held_back == 0

    # The ultra render starts: its 8 pages reserve the worker's 8 CPUs, before and once its process laid them out.
    ultra = worker["ultra"] = render_process._Attempt()
    assert render_process.cpus_reserved() == 8
    render_process._holding(ultra)({"type": "layout", "pages": 8, "wanted": 8, "budget": 20 << 30})
    assert ultra.pages == 8 and render_process.cpus_reserved() == 8
    # The preview lane shrinks to the one slot it keeps beside a render: no preview claimed while the two run.
    assert render_process.preview_slots() == render_process.PREVIEW_SLOTS_BESIDE_A_RENDER == 1
    assert preview_lane.slots_held_back == 1
    monkeypatch.setattr(orchestration_reconciler, "_inflight_by_adapter", {preview_lane.name: 2})
    assert orchestration_reconciler._available_slots_for(preview_lane, [preview_lane]) == 0
    monkeypatch.setattr(orchestration_reconciler, "_inflight_by_adapter", {})
    assert orchestration_reconciler._available_slots_for(preview_lane, [preview_lane]) == 1

    # The previews start at background priority, the render at the worker's.
    monkeypatch.setattr(resources, "_nice_binary", lambda: "/usr/bin/nice")
    command = ["python", "-m", "app.services.video_studio.render_process", "r1"]
    assert resources.niced(command, render_process.nice_for(True)) == ["/usr/bin/nice", "-n", "10", *command]
    assert resources.niced(command, render_process.nice_for(False)) == command

    # Its pages keep at least 75% of the worker's CPUs beside the two previews - each its pages and their encoders.
    preview = (2.0 * render_process.preview_cpus(), render_process.nice_for(True))
    got = cpu_share.shares([(8.0, render_process.nice_for(False)), preview, preview], 8)
    assert got[0] >= 0.75 * 8 and sum(got) == pytest.approx(8.0)
    # At one priority, as before: the previews took a third of them.
    assert cpu_share.shares([(8.0, 0), (preview[0], 0), (preview[0], 0)], 8)[0] < 0.75 * 8
    # Beside a recording too, which captures a live page in real time at the worker's priority: it keeps its weighted
    # share of 2 CPUs, the previews nearly none - 46% of the CPU used went to the render's pages on 23 Oct.
    with_recording = cpu_share.shares([(8.0, 0), preview, preview, (cpu_share.RECORDING_CPUS, 0)], 8)
    assert with_recording[0] > 0.7 * 8 and with_recording[1] < 0.05 * 8

    # The render ends: the preview lane has its slots back.
    del worker["ultra"]
    assert preview_lane.slots_held_back == 0 and render_process.preview_slots() == 2


def test_the_status_line_says_the_share_the_render_s_pages_got_beside_the_previews():
    clock = {"now": 0.0, "render": 0.0, "worker": 0.0}

    async def neighbours():
        return TWO_PREVIEWS

    watch = PaceWatch(
        None,
        pages=8,
        cpus=8,
        clock=lambda: clock["now"],
        cpu=lambda: clock["render"],
        stat=lambda: resources.CpuStat(clock["worker"]),
        neighbours=neighbours,
    )
    tracker = compositor.DrawProgress(total=1000)
    for _ in range(13):
        asyncio.run(watch.observe(tracker))
        # Its pages get 7.2 of the 8 CPUs the worker spends: the previews draw on the rest, at background priority.
        clock["now"] += 10.0
        clock["render"] += 7.2 * 10
        clock["worker"] += 8.0 * 10
    line = watch.line("Drawing frames (40%)")
    assert line == (
        "Drawing frames (40%) - its pages get 7.2 of 8 CPUs (90% of the CPU used) beside 2 scene previews "
        "(previews at background priority)"
    )
    assert len(line) <= render_pace.STAGE_ROOM
    # Alone on its worker, nothing more is said.
    alone = PaceWatch(None, pages=8, cpus=8, clock=lambda: 0.0, cpu=lambda: 0.0, stat=lambda: None, neighbours=_none)
    asyncio.run(alone.observe(tracker))
    assert alone.line("Drawing frames (40%)") == "Drawing frames (40%)"


async def _none() -> list:
    return []


# -- 2. A pre-flight on a worker with queued previews -------------------------------------------


def _preflight() -> render_preflight.Preflight:
    """28 min of an ultra render on 8 pages of an idle 8-CPU worker."""
    return render_preflight.Preflight(
        quality="ultra",
        frames=10_087,
        size=UHD,
        pages=8,
        cpus=8,
        seconds=28 * 60,
        measured=3,
        measured_quality=3,
        measured_here=3,
        frame_seconds=1.3,
        memory_bytes=20 << 30,
        ceiling_bytes=64 << 30,
    )


def test_a_pre_flight_on_a_worker_with_queued_previews_says_the_shared_estimate_and_the_idle_one(worker, monkeypatch):
    load = [
        Job(cpu_share.PREVIEW, 3.0, 50.0, running=True, job="p1", measured=True),
        Job(cpu_share.PREVIEW, 3.0, 10.0, running=True, job="p2", measured=True),
        Job(cpu_share.RECORDING, cpu_share.RECORDING_CPUS, 100.0, running=True, job="t1"),
        *[Job(cpu_share.PREVIEW, 3.0, 80.0, running=False, measured=True) for _ in range(3)],
    ]

    async def committed(db, *, preview_cpus, now=None):
        assert preview_cpus == render_process.preview_cpus()
        return load

    monkeypatch.setattr(cpu_share, "committed_load", committed)
    idle = _preflight()
    said = asyncio.run(render_preflight.with_load(object(), idle))
    shared = said.shared
    assert shared is not None and shared.idle_seconds == idle.expected_seconds == 28 * 60
    # Slower than on an idle worker - the recording draws at the render's priority -, never by the previews' count.
    assert idle.expected_seconds < shared.seconds < 1.25 * idle.expected_seconds
    assert shared.preview_slots == 1 and shared.preview_capacity == 2
    minutes = render_preflight._duration(shared.seconds)
    headline = f"about {minutes} on a worker shared with 5 scene previews and 1 recording (about 28 min on an idle worker)"
    line = said.line()
    assert line.startswith(f"Expected to take {headline}: 10,087 frames at 3840x2160")
    assert (
        "Its worker's committed load: 5 scene previews (2 running and 3 queued, about 3.0 CPUs each, measured on its "
        "kind's last 20 at most) and 1 recording (1 running, about 2.0 CPUs each, in the worker's own process, never "
        "measured apart)."
    ) in line
    assert "Scene previews draw at background priority, and only in the preview slots a render leaves (1 of 2 beside this one)" in line
    # Kept on the render: its card's ETA reads the shared estimate, its correction the idle one; get_video_jobs says both.
    kept = said.as_dict()
    assert kept["expected_seconds"] == round(shared.seconds) and kept["idle_seconds"] == 28 * 60
    assert [job["kind"] for job in kept["load"]["jobs"]] == ["scene preview"] * 2 + ["recording"] + ["scene preview"] * 3
    row = VideoRender(id=uuid.uuid4(), project_id=uuid.uuid4(), quality="ultra", status="pending", preflight=kept)
    assert f"The pre-flight expected {headline}: 10,087 frames" in render_preflight.said_of(row)
    # An idle worker: the bare estimate, as before.
    monkeypatch.setattr(cpu_share, "committed_load", _no_load)
    alone = asyncio.run(render_preflight.with_load(object(), idle))
    assert alone.shared is None and alone.line().startswith("Expected to take about 28 min: ")
    assert alone.as_dict()["expected_seconds"] == alone.as_dict()["idle_seconds"] == 28 * 60


async def _no_load(db, *, preview_cpus, now=None):
    return []


class _LoadDb:
    """The executions, finished previews and recordings a pre-flight reads its worker's committed load from."""

    def __init__(self, now: datetime) -> None:
        self.p1, self.p2, self.t1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        running_w2 = [("video_preview", "running", "w2", {"render_id": str(uuid.uuid4())}) for _ in range(4)]
        self.executions = [
            ("video_preview", "running", "w1", {"render_id": str(self.p1)}),
            ("video_preview", "running", "w1", {"render_id": str(self.p2)}),
            ("video_take", "running", "w1", {"take_id": str(self.t1)}),
            *running_w2,
            *[("video_preview", "pending", None, {"render_id": str(uuid.uuid4())}) for _ in range(6)],
        ]
        # The last previews took 80 s on 3 CPUs, the last recordings 120 s.
        self.previews = [(now - timedelta(seconds=80 + i), now - timedelta(seconds=i), {"cpu_seconds": 240.0, "total_seconds": 80.0}) for i in range(3)]
        self.takes = [(now - timedelta(seconds=120 + i), now - timedelta(seconds=i)) for i in range(3)]
        self.started_previews = [(self.p1, now - timedelta(seconds=30)), (self.p2, now - timedelta(seconds=70))]
        self.started_takes = [(self.t1, now - timedelta(seconds=20))]
        #: ``(adapter, count)`` of the jobs of its own process the target worker runs.
        self.own: list[tuple[str, int]] = []

    def begin_nested(self):
        return contextlib.nullcontext()

    async def execute(self, statement):
        text = str(statement)
        if "orchestration_executions" in text and "count(" in text:
            rows = self.own
        elif "orchestration_executions" in text:
            rows = self.executions
        elif "video_takes" in text:
            rows = self.takes if "finished_at" in text else self.started_takes
        else:
            rows = self.previews if "measured" in text else self.started_previews
        return SimpleNamespace(all=lambda: rows)


def test_the_committed_load_is_read_on_the_render_worker_running_the_fewest_jobs_with_its_share_of_the_queue(monkeypatch):
    now = datetime(2026, 10, 23, 18, 0, tzinfo=UTC)
    db = _LoadDb(now)

    async def render_workers():
        return {"w1": {"worker_id": "w1"}, "w2": {"worker_id": "w2"}}

    monkeypatch.setattr(cpu_share, "_render_workers", render_workers)
    found = asyncio.run(cpu_share.committed_load(db, preview_cpus=2.0, now=now))
    running = [job for job in found if job.running]
    # w1 runs 3 jobs, w2 4: the render goes to w1, beside its 2 previews and its recording.
    assert [(job.kind, job.job) for job in running] == [
        ("scene preview", str(db.p1)),
        ("scene preview", str(db.p2)),
        ("recording", str(db.t1)),
    ]
    # Each preview at the 3 CPUs and 80 s the last ones measured, less what it ran; the recording at its 2 CPUs.
    assert running[0].cpus == pytest.approx(3.0) and running[0].measured and running[0].seconds == pytest.approx(50.0)
    assert running[1].seconds == pytest.approx(10.0)
    assert running[2].cpus == cpu_share.RECORDING_CPUS and not running[2].measured and running[2].seconds == pytest.approx(100.0)
    # 6 previews queued for 2 workers that draw renders: 3 of them its worker's.
    queued = [job for job in found if not job.running]
    assert [(job.kind, job.seconds, job.cpus) for job in queued] == [("scene preview", 80.0, 3.0)] * 3


# -- 3. get_video_jobs: the jobs slowing a render -----------------------------------------------


class _CompetitorDb:
    """The executions on a render's worker and the rows they name, each select read off *objects* by its model's ids."""

    def __init__(self, objects: dict, rows: list) -> None:
        self.objects = objects
        self.rows = rows
        self.statements: list[str] = []

    async def get(self, model, key):
        return self.objects.get((model.__name__, str(key)))

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"].__name__
        self.statements.append(entity)
        if entity == "OrchestrationExecution":
            return SimpleNamespace(all=lambda: self.rows)
        ids = {str(value) for value in statement.whereclause.right.value}
        found = [value for (name, key), value in self.objects.items() if name == entity and key in ids]
        if entity == "User":
            people = [(uuid.UUID(key), value.full_name, value.email) for (name, key), value in self.objects.items() if name == entity and key in ids]
            return SimpleNamespace(all=lambda: people)
        if entity == "VideoProject":
            titles = [(uuid.UUID(key), value.title) for (name, key), value in self.objects.items() if name == entity and key in ids]
            return SimpleNamespace(all=lambda: titles)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: found))


def test_get_video_jobs_lists_the_jobs_sharing_a_render_s_worker_with_owner_video_and_who_may_cancel(monkeypatch):
    alice, bob = uuid.uuid4(), uuid.uuid4()
    launch, pitch = uuid.uuid4(), uuid.uuid4()
    own_execution = uuid.uuid4()
    render = VideoRender(id=uuid.uuid4(), project_id=launch, user_id=alice, quality="ultra", status="running", execution_id=own_execution)
    mine = SimpleNamespace(id=uuid.uuid4(), user_id=alice, project_id=launch, scene_id="s03")
    bobs = SimpleNamespace(id=uuid.uuid4(), user_id=bob, project_id=pitch, scene_id="s07")
    take = SimpleNamespace(id=uuid.uuid4(), user_id=bob, name="Login demo", scene_id=None)
    objects = {
        ("OrchestrationExecution", own_execution): SimpleNamespace(id=own_execution, state="running", lease_owner="w1"),
        ("VideoRender", mine.id): mine,
        ("VideoRender", bobs.id): bobs,
        ("VideoTake", take.id): take,
        ("User", alice): SimpleNamespace(full_name="Alice Martin", email="alice@example.com"),
        ("User", bob): SimpleNamespace(full_name="Bob Stone", email="bob@example.com"),
        ("VideoProject", launch): SimpleNamespace(title="Launch film"),
        ("VideoProject", pitch): SimpleNamespace(title="Board pitch"),
    }
    objects = {(name, str(key)): value for (name, key), value in objects.items()}
    rows = [
        (uuid.uuid4(), {"render_id": str(mine.id)}),
        (uuid.uuid4(), {"render_id": str(bobs.id)}),
        (uuid.uuid4(), {"take_id": str(take.id)}),
    ]
    caller = SimpleNamespace(id=alice)

    async def may_read_job(db, kind, row, user):
        # Alice opens her own video only: Bob's pitch and his recording are his.
        return row.project_id == launch if kind == "render" else row.user_id == user.id

    monkeypatch.setattr(jobs, "may_read_job", may_read_job)
    db = _CompetitorDb(objects, rows)
    found = asyncio.run(cpu_share.competitors(db, render, caller))
    assert [(item.kind, item.owner, item.video, item.yours) for item in found] == [
        ("scene preview", "Alice Martin", "Launch film", True),
        ("scene preview", None, None, False),
        ("recording", None, None, False),
    ]
    # One query per kind of job, one for their owners and one for their videos: never one per job.
    assert db.statements == ["OrchestrationExecution", "VideoRender", "VideoTake", "User", "VideoProject"]
    said = cpu_share.competitors_words(found)
    assert said.startswith("Sharing its worker now: ")
    assert (
        f'scene preview `{str(mine.id)[:8]}` of scene s03 of "Launch film", started by Alice Martin - yours to cancel: '
        f"cancel_video_job(render_ids=['{mine.id}'])"
    ) in said
    # Bob's jobs, which Alice may not read, by their kind alone: no name, e-mail, id, scene or video of his.
    assert "a scene preview of another user" in said and "a recording of another user" in said
    for secret in ("Bob", "bob@example.com", str(bobs.id)[:8], str(take.id)[:8], "s07", "Board pitch", "Login demo"):
        assert secret not in said
    assert found[1].as_dict() == {
        "kind": "scene preview",
        "owner": cpu_share.ANOTHER_USER,
        "may_cancel": False,
        "background_priority": True,
    }
    assert "Scene previews draw at background priority" in said and "ask them first" in said
    assert found[0].as_dict() == {
        "kind": "scene preview",
        "id": str(mine.id),
        "owner": "Alice Martin",
        "video": "Launch film",
        "scene": "s03",
        "may_cancel": True,
        "background_priority": True,
    }
    # A render no worker runs yet has nothing sharing its worker; the cancel rule is the one cancel_video_job applies.
    objects[("OrchestrationExecution", str(own_execution))] = SimpleNamespace(id=own_execution, state="pending", lease_owner=None)
    assert asyncio.run(cpu_share.competitors(_CompetitorDb(objects, rows), render, caller)) == []
    assert jobs.may_cancel(mine, caller) and not jobs.may_cancel(bobs, caller)


# -- 4. What the render's measure keeps ---------------------------------------------------------


def test_the_render_s_record_keeps_its_estimate_contention_and_real_time_for_the_next_estimate():
    clock = {"now": 0.0, "render": 0.0, "worker": 0.0}

    async def neighbours():
        return TWO_PREVIEWS

    watch = PaceWatch(
        None,
        pages=8,
        cpus=8,
        clock=lambda: clock["now"],
        cpu=lambda: clock["render"],
        stat=lambda: resources.CpuStat(clock["worker"]),
        neighbours=neighbours,
    )
    tracker = compositor.DrawProgress(total=1000)
    for _ in range(31):
        asyncio.run(watch.observe(tracker))
        clock["now"] += 60.0
        clock["render"] += 6.4 * 60
        clock["worker"] += 8.0 * 60
    contention = watch.contention()
    assert contention == {
        "cpus_used": 6.4,
        "cpus": 8,
        "cpu_share": 0.8,
        "neighbours": {"scene preview": 2},
        "shared": True,
        "overlap": 1.0,
        "seconds": 1800,
        # What the rest of its container used, and the CPUs nobody used.
        "others_cpus": 1.6,
        "unused_cpus": 0.0,
    }
    measured = render_preflight.measurement(
        render_preflight.Layout(pages=8, cpus=8, wanted=8),
        frames=10_000,
        seconds=1700.0,
        total_seconds=2100.0,
        cpu_seconds=13_440.0,
        contention=contention,
    )
    assert measured["contention"] == contention and measured["cpu_seconds"] == 13_440.0
    assert cpu_share.measured_cpus(measured) == pytest.approx(6.4)
    # Its pre-flight said 31 min beside the load, 28 min on an idle worker: the correction reads its idle-worker time.
    expected = {**_preflight().as_dict(), "raw_seconds": 1680, "expected_seconds": 1860, "idle_seconds": 1680}
    assert render_history.estimate_error(expected, measured) == (1680.0, pytest.approx(2100.0 * 0.8))
    told = render_preflight.said_of(SimpleNamespace(preflight=expected, measured=measured, quality="ultra", status="ready"))
    assert (
        "Its pages got 6.4 of the worker's 8 CPUs on average (80% of the CPU used), beside 2 scene previews; the rest of "
        "its container used 1.60 CPUs: about 28 min on an idle worker, what the next estimates are corrected by, against "
        "the 28 min its idle estimate said."
    ) in told
    assert "It took 28 min on an idle worker against the 28 min said (+0%)" in told
    # A render alone on its worker corrects them at its real time.
    alone = {**contention, "neighbours": {}, "cpu_share": 0.97, "shared": False}
    assert render_history.estimate_error(expected, {**measured, "contention": alone}) == (1680.0, 2100.0)


# -- 5. Follow-ups ---------------------------------------------------------------


def test_a_job_seen_beside_a_render_for_part_of_its_run_sets_back_only_that_part():
    clock = {"now": 0.0, "render": 0.0, "worker": 0.0}
    seen = {"jobs": []}

    async def neighbours():
        return seen["jobs"]

    watch = PaceWatch(
        None,
        pages=8,
        cpus=8,
        clock=lambda: clock["now"],
        cpu=lambda: clock["render"],
        stat=lambda: resources.CpuStat(clock["worker"]),
        neighbours=neighbours,
    )
    tracker = compositor.DrawProgress(total=1000)
    for minute in range(61):
        # Two previews beside it over the last 6 of its 60 minutes only.
        seen["jobs"] = TWO_PREVIEWS if minute >= 54 else []
        asyncio.run(watch.observe(tracker))
        clock["now"] += 60.0
        clock["render"] += 7.6 * 60
        clock["worker"] += 8.0 * 60
    contention = watch.contention()
    assert contention["shared"] and contention["neighbours"] == {"scene preview": 2}
    assert contention["overlap"] == pytest.approx(0.1, abs=0.02)
    # Its hour is set back by its share over the tenth of it it shared the worker, never over the whole hour.
    took = 3600.0
    assert cpu_share.idle_equivalent(took, contention) == pytest.approx(
        took * (1 - contention["overlap"] * (1 - contention["cpu_share"]))
    )
    assert cpu_share.idle_equivalent(took, contention) > took * contention["cpu_share"]
    # A record older than the overlap sets back the whole run, as before.
    older = {key: value for key, value in contention.items() if key != "overlap"}
    assert cpu_share.idle_equivalent(took, older) == pytest.approx(took * contention["cpu_share"])


@pytest.mark.skipif(os.name != "posix" or shutil.which("nice") is None, reason="the POSIX nice path: Linux CI runs it")
def test_a_niced_command_really_runs_its_steps_below_the_worker_s_priority():
    command = resources.niced([sys.executable, "-c", "import os; print(os.nice(0))"], render_process.nice_for(True))
    assert command[0] == shutil.which("nice")
    ran = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)  # noqa: S603 - our interpreter
    assert int(ran.stdout) == min(19, os.nice(0) + cpu_share.PREVIEW_NICE)


def test_the_committed_load_counts_renders_and_reads_one_worker_when_presence_is_unknown(monkeypatch):
    now = datetime(2026, 10, 23, 18, 0, tzinfo=UTC)
    db = _LoadDb(now)
    # w1 runs 2 previews, a recording and 2 whole renders: 5 jobs against w2's 4 previews.
    db.executions.append(("video_render", "running", "w1", {"render_id": str(uuid.uuid4())}))
    db.executions.append(("video_render", "running", "w1", {"render_id": str(uuid.uuid4())}))

    async def unknown():
        return None

    monkeypatch.setattr(cpu_share, "_render_workers", unknown)
    found = asyncio.run(cpu_share.committed_load(db, preview_cpus=2.0, now=now))
    running = [job for job in found if job.running]
    # Presence unknown: the workers running jobs stand for those that draw renders, never every worker's load at once.
    # w1's renders make it the busier one, so the render is priced beside w2's 4 previews alone.
    assert [job.kind for job in running] == ["scene preview"] * 4
    assert all(job.job not in (str(db.p1), str(db.p2), str(db.t1)) for job in running)
    # 6 previews queued, shared between the 2 workers seen.
    assert len([job for job in found if not job.running]) == 3
