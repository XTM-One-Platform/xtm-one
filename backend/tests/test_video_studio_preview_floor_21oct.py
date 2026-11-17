"""The preview floor, insert findings against facts, infra advice said once, structured answers.

Three gaps of the 21 Oct 2026 sweep, and the loop that kept producing them:

* **Preview starvation.** A 24 GiB worker drawing one 2160p render on 3 pages
  left the preview lane one page: a 7 s scene with a screen insert previewed
  at a frame a second. The preview reserve is a hard floor (8 GiB) no render
  borrows from, a render's pages are laid out at launch in the room the
  worker has then and it holds their budget, and a preview runs on its pages
  only: on the worker that took it, routed to one whose free memory holds
  them, or after waiting for them - its answer says which.
* **Findings and facts.** An unkeyed contain insert's bands were said on its
  Insert line as if they were a defect: every insert measurement is now a
  finding, by design or a fact, said with its class, and only findings make
  the verdict.
* **Advice repeated.** The memory to give the worker was said on every render:
  it is said in the pre-flight of the first render that needs it, then only
  when its numbers change.
* **The loop.** A render's and a preview's answers start with ``status``,
  ``findings``, ``by_design``, ``facts`` and ``infra_once``; "all gates green"
  is ``findings`` empty.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.models.video_studio import JOB_FAILED, JOB_READY
from app.services import orchestration_engine as engine
from app.services import orchestration_reconciler as reconciler
from app.services import worker_presence
from app.services.orchestration_adapters import video_studio as adapters
from app.services.orchestration_adapters.base import AdapterResult
from app.services.video_studio import (
    insert_checks,
    jobs,
    preview_routing,
    render_answer,
    render_checks,
    render_preflight,
    render_process,
    resources,
    runners,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_GIB = 1 << 30
UHD = (3840, 2160)
#: The 7 s scene of the sweep, previewed at 1080p30 with its screen insert: 210 frames.
SCENE_FRAMES = 210


@pytest.fixture(autouse=True)
def _worker(monkeypatch):
    """The documented worker (``docs/docs/deployment/configuration.md``): 8 CPUs, a 24 GiB limit."""
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_ultra_render_memory_mb", 16384)
    monkeypatch.setattr(settings, "video_studio_preview_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 8192)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 2)
    monkeypatch.setattr(settings, "video_studio_render_concurrency", 1)
    monkeypatch.setattr(settings, "video_studio_render_pages", 0)
    monkeypatch.setattr(render_process, "_ATTEMPTS", {})
    monkeypatch.setattr(render_process, "_ADMITTED", {})
    monkeypatch.setattr(resources, "memory_limit", lambda: 24 * _GIB)
    monkeypatch.setattr(resources, "host_memory", lambda: None)
    monkeypatch.setattr(resources, "cpu_budget", lambda: 8)


def _ultra_in_flight() -> render_process._Attempt:
    """One 2160p render in flight on this worker, its pages laid out at launch in the room it had then."""
    room = render_process.launch_room("ultra", excluding="ultra")
    attempt = render_process._Attempt()
    attempt.ceiling = room
    render_process._ATTEMPTS["ultra"] = attempt
    inserts = render_preflight.page_peak(UHD, "slow").inserts
    drawn = render_preflight.layout(UHD, x264_preset="slow", ceiling=room, cpus=8, inserts=inserts)
    render_process._holding(attempt)(render_preflight.layout_event(drawn, UHD, x264_preset="slow", inserts=inserts))
    return attempt


def _preview_rate(pages: int) -> float:
    """Frames a second a 1080p scene preview with a screen insert draws on *pages* pages of an 8-CPU worker, at the
    rate the pre-flight counts before any render was measured."""
    return render_preflight.frames_per_second(
        render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE),
        size=(1920, 1080),
        quality="standard",
        pages=pages,
        cpus=8,
        kind="insert",
    )


async def _noop(*_args, **_kwargs):
    return None


@pytest.fixture
def lane(monkeypatch):
    world = SimpleNamespace(ran=[], admissions={}, rooms=[], marks=[], hold=None)

    async def run_isolated(render_id, **_options):
        world.ran.append(render_id)
        world.admissions[render_id] = render_process.admission_of(render_id)
        if world.hold is not None:
            await world.hold.wait()
        return runners.JobOutcome("done")

    async def size_of(_render_id):
        return render_process.preview_size(render_preflight.InsertLoad(videos=1), quality="standard")

    async def rooms(*, me, **_kwargs):
        return [room for room in world.rooms if room.worker != me]

    async def mark_if_open(_kind, _job_id, **changes):
        world.marks.append(changes)
        return True

    monkeypatch.setattr(render_process, "run_isolated", run_isolated)
    monkeypatch.setattr(render_process, "preview_size_of", size_of)
    monkeypatch.setattr(preview_routing, "rooms", rooms)
    monkeypatch.setattr(preview_routing, "publish", _noop)
    monkeypatch.setattr(jobs, "preview_waits", _noop)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(adapters.wake, "schedule", lambda *_args: None)
    return world


def _execution(render_id: str, **fields):
    return SimpleNamespace(
        id=uuid.uuid4(), payload={"render_id": render_id}, attempt_count=1, max_attempts=3, adapter_state=None, **fields
    )


def _ctx(worker_id: str):
    return SimpleNamespace(db=SimpleNamespace(commit=_noop), worker_id=worker_id, cancel_event=None, stopping=None)


# ── 1. The preview floor ──────────────────────────────────────────────────


def test_no_render_ever_reaches_into_the_preview_reserve():
    """A hard floor: an ultra render on 24 GiB keeps 16 GiB at most, whatever it is asked, and lays its pages out in
    the room it has at launch, holding their budget."""
    assert render_process.preview_reserve() == render_process.PREVIEW_RESERVE_FLOOR == 8 * _GIB
    attempt = _ultra_in_flight()
    assert attempt.ceiling == 16 * _GIB and 0 < attempt.held < attempt.ceiling
    # The 19.5 GiB of the sweep: a ceiling set past the reserve is never given - 16 GiB held at most, 8 GiB free.
    assert render_process.memory_held() == attempt.held <= 24 * _GIB - render_process.PREVIEW_RESERVE_FLOOR
    assert render_process.room_for_previews() >= render_process.PREVIEW_RESERVE_FLOOR
    # A second render starting now is laid out in what the first leaves beside the reserve.
    assert render_process.launch_room("ultra", excluding="second") == 24 * _GIB - 8 * _GIB - attempt.held


async def test_a_7_s_insert_scene_previews_at_5_frames_a_second_beside_an_ultra_render_or_on_another_worker(lane):
    """The issue's first test: one ultra render in flight, two previews of a 7 s scene with a screen insert."""
    _ultra_in_flight()
    lane.rooms = [preview_routing.Room(worker="w:2", free=24 * _GIB, slots=2, bound=24 * _GIB)]
    lane.hold = asyncio.Event()
    adapter = adapters.VideoPreviewAdapter()
    first, second = _execution("p1"), _execution("p2")
    running = asyncio.create_task(adapter.execute(_ctx("w:1"), first))
    await asyncio.sleep(0)
    routed = await adapter.execute(_ctx("w:1"), second)
    lane.hold.set()
    assert (await running).kind == "completed"
    # The first draws beside the render on its pages: at 5 frames a second or more, 42 s at most for its 210 frames.
    assert lane.admissions["p1"].pages == 2 and _preview_rate(2) >= 5.0 and SCENE_FRAMES / _preview_rate(2) <= 42
    # One page would have drawn it at under 3 a second: never asked for want of memory.
    assert _preview_rate(1) < 3.0
    # The second does not fit beside them: handed to the worker with room, its stage saying so, no attempt spent.
    assert (routed.kind, routed.adapter_state, routed.place, routed.backoff_seconds) == (
        "defer",
        preview_routing.ROUTED_STATE,
        "keep",
        0.0,
    )
    assert routed.claim_for == ("w:2", preview_routing.ROUTE_HOLD_SECONDS)
    route = routed.payload_update[preview_routing.ROUTE_KEY]
    assert (route["from"], route["to"], route["tried"]) == ("w:1", "w:2", ["w:1"])
    assert lane.marks[-1]["stage"].startswith(
        "Routed to worker w:2, which has 24.0 GiB free: on worker w:1 its 2 pages need "
    )
    # On that worker (its own memory, none of it held): drawn on its pages, its answer saying where it came from.
    render_process._ATTEMPTS.clear()
    second.payload, second.adapter_state = {**second.payload, **routed.payload_update}, routed.adapter_state
    assert (await adapter.execute(_ctx("w:2"), second)).kind == "completed"
    admitted = lane.admissions["p2"]
    assert admitted.pages == 2 and admitted.note.startswith(
        "Drawn on its 2 pages on worker w:2, routed there from worker w:1, where its 2 pages need "
    )


async def test_with_no_other_worker_a_preview_waits_for_its_pages_and_says_it(lane):
    _ultra_in_flight()
    lane.hold = asyncio.Event()
    adapter = adapters.VideoPreviewAdapter()
    running = asyncio.create_task(adapter.execute(_ctx("w:1"), _execution("p1")))
    await asyncio.sleep(0)
    waiting = await adapter.execute(_ctx("w:1"), _execution("p2"))
    assert (waiting.kind, waiting.adapter_state, waiting.claim_for) == ("defer", adapters.MEMORY_WAITING_STATE, None)
    assert lane.marks[-1]["stage"].startswith(
        "Waiting for its pages on worker w:1 (no other worker has them free with a preview slot): its 2 pages need "
    )
    # What holds the worker's memory, by kind: a render and the first preview.
    assert "for 1 render (" in lane.marks[-1]["stage"]
    lane.hold.set()
    await running
    # A worker tried already, or one without a free slot, is never routed to.
    full = preview_routing.Room(worker="w:3", free=24 * _GIB, slots=0)
    assert (
        preview_routing.best(
            [full, preview_routing.Room(worker="w:1", free=24 * _GIB, slots=2)], 6 * _GIB, tried=["w:1"]
        )
        is None
    )


def test_the_worker_with_the_most_memory_free_takes_a_routed_preview():
    small = preview_routing.Room(worker="a", free=7 * _GIB, slots=2)
    large = preview_routing.Room(worker="b", free=20 * _GIB, slots=1)
    unbounded = preview_routing.Room(worker="c", free=None, slots=1)
    assert preview_routing.best([small, large], 6 * _GIB).worker == "b"
    assert preview_routing.best([small, large], 8 * _GIB).worker == "b"
    assert preview_routing.best([small, large, unbounded], 6 * _GIB).worker == "c"
    assert preview_routing.best([small], 8 * _GIB) is None


async def test_what_a_worker_has_free_is_published_and_read_by_the_others(monkeypatch):
    stored: dict[str, str] = {}

    class _Redis:
        async def set(self, key, value, ex=None):
            stored[key] = value

        async def scan_iter(self, match=None, count=None):
            for key in list(stored):
                yield key

        async def mget(self, keys):
            return [stored.get(key) for key in keys]

    async def client():
        return _Redis()

    from app.core import redis as core_redis

    monkeypatch.setattr(core_redis, "redis_client", client)
    monkeypatch.setattr(preview_routing, "_published", {})
    _ultra_in_flight()
    await preview_routing.publish("w:1", force=True)
    await preview_routing.publish("w:2", force=True)
    [other] = await preview_routing.rooms(me="w:2")
    assert other.worker == "w:1" and other.slots == 2 and other.free == render_process.room_for_previews()
    # A worker that draws no preview says nothing: none is routed to it.
    stored.clear()
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    await preview_routing.publish("w:1", force=True)
    assert stored == {}


async def test_a_render_with_no_room_at_launch_fails_saying_what_to_give_never_unbounded(monkeypatch, tmp_path):
    monkeypatch.setattr(resources, "memory_limit", lambda: 8 * _GIB)
    seen: dict = {}

    async def load(_kind, _job_id):
        return SimpleNamespace(quality="high", scene_id=None, status="running")

    async def supervise(*_args, **_kwargs):
        raise AssertionError("a render with no room is never started")

    async def mark_if_open(*_args, **changes):
        seen.update(changes)
        return True

    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process.jobs, "load", load)
    monkeypatch.setattr(render_process.jobs, "heartbeat", _noop)
    monkeypatch.setattr(render_process.jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(render_process.render_segments, "forget", _noop)
    monkeypatch.setattr(render_process.render_ranges, "forget", _noop)
    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)
    outcome = await render_process.run_isolated(str(uuid.uuid4()))
    assert outcome.kind == "failed" and seen["status"] == JOB_FAILED
    assert (
        "less the 8 GiB its scene previews keep" in seen["error"]
        and "VIDEO_STUDIO_PREVIEW_CONCURRENCY=0" in seen["error"]
    )


# ── The second worker: a lane a worker does not claim ────────────────────


def test_a_preview_worker_claims_no_render_and_a_render_worker_keeps_no_reserve(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_concurrency", 0)
    assert not adapters.VideoRenderAdapter().claims_here and adapters.VideoPreviewAdapter().claims_here
    monkeypatch.setattr(settings, "video_studio_render_concurrency", 1)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    assert adapters.VideoRenderAdapter().claims_here and not adapters.VideoPreviewAdapter().claims_here
    assert render_process.preview_reserve() == 0
    assert reconciler.claimed_adapters([adapters.VideoRenderAdapter(), adapters.VideoPreviewAdapter()]) == [
        jobs.RENDER_ADAPTER
    ]


async def test_the_reconciler_claims_only_the_lanes_this_worker_runs_and_lends_none_to_one_it_does_not(monkeypatch):
    claimed: list[str] = []
    said: list[dict] = []

    async def claim_due(_db, *, adapter, **_kwargs):
        claimed.append(adapter)
        return []

    async def publish(worker_id, *, state, claims=None, **_kwargs):
        said.append({"state": state, "claims": claims})

    class _Db:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    class _Cycle:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    monkeypatch.setattr(reconciler, "async_session", lambda: _Cycle())
    monkeypatch.setattr(engine, "sweep_deadlines", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine, "reclaim_expired_leases", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine, "claim_due", claim_due)
    monkeypatch.setattr(reconciler, "_drive_outbox", AsyncMock())
    monkeypatch.setattr(reconciler, "_reclaim_from_gone_workers", AsyncMock(return_value=[]))
    # No other worker holds a lease: no slot is kept for a stopping one.
    monkeypatch.setattr(engine, "leased_owners", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        reconciler, "registered_adapters", lambda: [adapters.VideoRenderAdapter(), adapters.VideoPreviewAdapter()]
    )
    monkeypatch.setattr(worker_presence, "publish", publish)
    monkeypatch.setattr(render_preflight, "publish_worker", AsyncMock())
    monkeypatch.setattr(reconciler, "_draining", None)
    await reconciler._run_cycle("w:render")
    # The render lane claims, and finding nothing would lend its idle slot - never to a lane this worker does not run.
    assert claimed == [jobs.RENDER_ADAPTER]
    assert said == [{"state": worker_presence.STATE_RUNNING, "claims": [jobs.RENDER_ADAPTER]}]


async def test_a_render_is_split_only_across_the_workers_that_draw_renders(monkeypatch):
    records = {
        "xtm:orchestration:worker:a": json.dumps(
            {"state": "running", "claims": [jobs.RENDER_ADAPTER, jobs.PREVIEW_ADAPTER]}
        ),
        "xtm:orchestration:worker:b": json.dumps({"state": "running", "claims": [jobs.PREVIEW_ADAPTER]}),
        # A worker of a release that listed nothing claims everything.
        "xtm:orchestration:worker:c": json.dumps({"state": "running"}),
    }

    class _Redis:
        async def scan_iter(self, match=None, count=None):
            for key in records:
                yield key

        async def mget(self, keys):
            return [records[key] for key in keys]

    async def client():
        return _Redis()

    from app.core import redis as core_redis

    monkeypatch.setattr(core_redis, "redis_client", client)
    assert await worker_presence.count() == 3
    assert await worker_presence.count(claiming=jobs.RENDER_ADAPTER) == 2
    assert await render_preflight.workers_present() == 2


def _compose(name: str) -> dict:
    """A compose file of the repository, its ``!reset`` / ``!override`` tags read as the value they carry."""
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    def tagged(loader, _suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_mapping(node, deep=True)

    _Loader.add_multi_constructor("!", tagged)
    return yaml.load((REPO_ROOT / name).read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 - SafeLoader with tags read as values


def test_the_default_deployment_runs_a_second_worker_that_draws_previews_only():
    services = _compose("docker-compose.yml")["services"]
    worker, preview = services["worker"], services["worker-preview"]
    # The same worker - image, health check, environment - but for its role: no whole render, no range.
    assert preview["build"] == worker["build"] and preview["healthcheck"] == worker["healthcheck"]
    assert preview["environment"].pop("VIDEO_STUDIO_RENDER_CONCURRENCY") == "0"
    assert preview["environment"] == worker["environment"]
    # In development it runs the checkout's sources with hot reload, as the worker does.
    dev = _compose("docker-compose.dev.yml")["services"]
    assert (
        dev["worker-preview"]["command"] == dev["worker"]["command"]
        and dev["worker-preview"]["volumes"] == dev["worker"]["volumes"]
    )


# ── The engine: an execution handed to one worker ─────────────────────────


async def test_a_deferral_hands_an_execution_to_one_worker_for_a_while(monkeypatch):
    now = datetime.now(UTC)
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        adapter=jobs.PREVIEW_ADAPTER,
        state="running",
        payload={"render_id": "p2", engine.CLAIM_FOR_PAYLOAD_KEY: {"worker": "old", "until": now.isoformat()}},
        attempt_count=1,
        max_attempts=3,
        run_at=now - timedelta(minutes=1),
        deadline_at=now + timedelta(hours=4),
        adapter_state=None,
        last_error=None,
        lease_owner="w:1",
        lease_expires_at=now + timedelta(seconds=150),
        updated_at=now,
    )
    monkeypatch.setattr(engine, "_locked_get", AsyncMock(return_value=execution))

    class _Db:
        async def flush(self):
            return None

    await engine.defer(
        _Db(),
        execution_id=execution.id,
        worker_id="w:1",
        reason="Routed",
        delay_seconds=0.0,
        adapter_state=preview_routing.ROUTED_STATE,
        place="keep",
        claim_for=("w:2", 10.0),
    )
    hand = execution.payload[engine.CLAIM_FOR_PAYLOAD_KEY]
    assert hand["worker"] == "w:2" and timedelta(seconds=9) < datetime.fromisoformat(hand["until"]) - now <= timedelta(
        seconds=11
    )
    assert execution.state == "pending" and execution.attempt_count == 0 and execution.lease_owner is None
    # A deferral naming no worker drops a former hand: anyone claims it.
    execution.state, execution.lease_owner = "running", "w:2"
    await engine.defer(
        _Db(),
        execution_id=execution.id,
        worker_id="w:2",
        reason="Waiting",
        delay_seconds=5.0,
        adapter_state="x",
        place="keep",
    )
    assert engine.CLAIM_FOR_PAYLOAD_KEY not in execution.payload
    # Applied by the reconciler: the hand reaches the engine, and every worker is woken for it.
    defer, woken = AsyncMock(), AsyncMock()
    monkeypatch.setattr(engine, "defer", defer)
    monkeypatch.setattr(engine, "notify_orchestration_wake", woken)

    class _Commit:
        async def commit(self):
            return None

    result = AdapterResult.defer(
        "Routed", delay_seconds=0.0, adapter_state=preview_routing.ROUTED_STATE, place="keep", claim_for=("w:2", 10.0)
    )
    await reconciler._apply_result(_Commit(), execution.id, "w:1", result)
    assert defer.await_args.kwargs["claim_for"] == ("w:2", 10.0)
    woken.assert_awaited_once()


# ── 2. Findings, by design, facts ─────────────────────────────────────────

KEY = "s5:0:screen_insert"
LABEL = "Scene s5: its screen insert 0"


def _contain_insert(*, frames: int = 30, **audit) -> tuple[dict, dict]:
    """An unkeyed insert with fit contain on a 16:10 surface: bands 5.5% of its quad, showing the footage."""
    placed = {KEY: insert_checks.Placed(KEY, "s5", LABEL, 0.0, frames / 30, fit="contain", keyed=False, light=0.15)}
    audits = {
        KEY: {
            "frames": {
                str(n): {
                    "alignment": 0.002,
                    "band_share": 0.055,
                    "band_fill": "footage",
                    "band_colour": None,
                    "sharpness": 0.93,
                    **audit,
                }
                for n in range(frames)
            },
            "evidence": {},
        }
    }
    return audits, placed


def test_an_unkeyed_contain_insert_has_its_band_by_design_and_no_finding():
    """The issue's second test."""
    audits, placed = _contain_insert()
    found = [finding for finding, _proof in insert_checks.findings(audits, placed, fps=30)]
    [report] = insert_checks.reports(audits, placed, fps=30)
    assert found == [] and report["contain_band"] == {"share": 0.055, "filled_with": "footage", "colour": None}
    assert report["drawn_by"] == {"fit": "contain", "light": 0.15, "key": None}
    checks = render_checks.Checks(frames={"status": render_checks.PASSED}, inserts=[report], findings=found).as_dict()
    answer = render_answer.of_checks(checks)
    assert answer.findings == [] and answer.status == render_answer.GREEN
    [band] = [item for item in answer.by_design if "contain bands" in item["what"]]
    assert band == {
        "insert": LABEL,
        "scene": "s5",
        "what": "contain bands 5.5% of the quad, showing the footage (no key)",
        "because": "fit contain, no key",
    }
    assert not any("contain bands" in item["what"] for item in answer.facts)
    # Its Insert line says the class with it; the bezel's distance, a fact.
    [line] = render_checks.insert_lines(checks)
    assert "by design (fit contain, no key): contain bands 5.5% of the quad, showing the footage (no key)" in line
    assert (
        "fact: bezel found, the quad 0.20% of the frame's width off it" in line
        and "fact: sharpness 0.93 of the source" in line
    )


def test_a_measurement_past_its_gate_is_the_finding_it_makes_never_a_fact():
    audits, placed = _contain_insert(halo=0.2)
    found = [finding for finding, _proof in insert_checks.findings(audits, placed, fps=30)]
    [report] = insert_checks.reports(audits, placed, fps=30)
    [finding] = found
    assert finding.kind == render_checks.INSERT_HALO and finding.fix and finding.message.endswith(f" - {finding.fix}")
    checks = render_checks.Checks(inserts=[report], findings=found).as_dict()
    classed = {item.text: item for item in render_checks.insert_measured(report, {render_checks.INSERT_HALO})}
    assert classed["halo 20.0% of the foreground's edge"].cls == insert_checks.FINDING
    answer = render_answer.of_checks(checks)
    [item] = answer.findings
    assert (item["kind"], item["severity"], item["fix"], item["insert"]) == (
        render_checks.INSERT_HALO,
        "blocks",
        finding.fix,
        KEY,
    )
    assert item["what"].startswith("insert halo at t=0:00.00") and not item["what"].endswith(finding.fix)
    assert not any("halo" in entry["what"] for entry in answer.facts + answer.by_design)
    # Its band stays by design beside it.
    assert any("contain bands" in entry["what"] for entry in answer.by_design)


# ── 3. Infra advice, said once ────────────────────────────────────────────


def _ultra(ceiling: int = 16 * _GIB) -> render_preflight.Preflight:
    peak = render_preflight.page_peak(UHD, "slow")
    return render_preflight.estimate(
        frames=7317, size=UHD, quality="ultra", cpus=8, ceiling=ceiling, inserts=peak.inserts, workers=1
    )


class _Renders:
    """The whole renders at a quality the pre-flight reads what was said from, newest first."""

    def __init__(self, preflights: list[dict]) -> None:
        self.preflights = preflights

    async def execute(self, _statement):
        return SimpleNamespace(all=lambda: [(preflight,) for preflight in self.preflights])


async def test_two_consecutive_ultra_renders_on_one_worker_say_the_memory_advice_once():
    """The issue's third test: only the first carries it; a change of its numbers says it again."""
    first = render_preflight.with_infra_said(_ultra(), await render_preflight.infra_said_before(_Renders([]), "ultra"))
    [advice] = first.infra_once()
    assert (
        "To draw on every one, set VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB to" in advice
        and "give the worker's container" in advice
    )
    # With the time at full pages against the time at the pages opened.
    assert f"on {first.pages} pages, against about" in advice and f"on all {first.wanted_pages}" in advice
    # Its own pages are its fact, the advice never among them.
    assert first.notes[0].startswith(f"It draws on {first.pages} pages at 3840x2160") and advice not in first.line()
    recorded = first.as_dict()
    second = render_preflight.with_infra_said(
        _ultra(), await render_preflight.infra_said_before(_Renders([recorded]), "ultra")
    )
    assert second.infra and second.infra_once() == [] and second.notes[0] == first.notes[0]
    # A render in between that needed no advice does not make it new again.
    roomy = _ultra(ceiling=64 * _GIB).as_dict()
    third = render_preflight.with_infra_said(
        _ultra(), await render_preflight.infra_said_before(_Renders([roomy, recorded]), "ultra")
    )
    assert third.infra_once() == []
    # The worker given more memory, the numbers change: said again, once.
    grown = render_preflight.with_infra_said(
        _ultra(20 * _GIB), await render_preflight.infra_said_before(_Renders([recorded]), "ultra")
    )
    assert len(grown.infra_once()) == 1 and grown.infra_once() != first.infra_once()


# ── 4. The answer: four lists, one verdict ────────────────────────────────


def _row(checks: dict, *, scene_id: str | None, status: str = JOB_READY, notes: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        scene_id=scene_id,
        status=status,
        error=None if status == JOB_READY else "The render failed its picture check.",
        superseded_by_id=None,
        frame_file_ids=[str(uuid.uuid4()) for _ in range(3)] if scene_id else None,
        file_id=str(uuid.uuid4()),
        poster_file_id=str(uuid.uuid4()),
        checks=checks,
        width=1920,
        height=1080,
        fps=30,
        duration_ms=7000,
        size_bytes=9_000_000,
        notes=notes,
        gate_override=None,
        quality="standard" if scene_id else "ultra",
        stalls=None,
        preflight=None,
        measured=None,
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, *_args, **_kwargs):
        return SimpleNamespace(id=uuid.UUID(int=1), email="maker@example.com", role="user")

    async def commit(self):
        return None


def _tools(monkeypatch, row: SimpleNamespace, *, preflight: object = None):
    from app.services.builtin_tools import video_studio as tool
    from app.services.video_studio import projects

    project = SimpleNamespace(id=row.project_id, title="Sweep film", revision=4)

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def started(*_args, **_kwargs):
        return projects.StartedRender(render=SimpleNamespace(id=row.id), superseded=[], preflight=preflight)

    async def waited(*_args, **_kwargs):
        return [row]

    async def file_refs(file_ids, *, show=True, **_kwargs):
        ids = [f"conv-{f}" for f in file_ids if f]
        return tool._Pictures(ids=ids, copies={str(f): f"conv-{f}" for f in file_ids if f})

    async def wait_for_jobs(asked, **_kwargs):
        return ({"render": [row]} if asked.get("render") else {}), "it finished"

    async def readable(*_args, **_kwargs):
        return True

    async def job_states(_kind, rows):
        return [
            SimpleNamespace(
                status="ready" if r.status == JOB_READY else "failed", as_dict=lambda r=r: {"id": str(r.id)}
            )
            for r in rows
        ]

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "start_scene_render", started)
    monkeypatch.setattr(projects, "start_render", started)
    monkeypatch.setattr(jobs, "wake", _noop)
    monkeypatch.setattr(jobs, "wait_for", waited)
    monkeypatch.setattr(jobs, "wait_for_jobs", wait_for_jobs)
    monkeypatch.setattr(jobs, "may_read_job", readable)
    monkeypatch.setattr(jobs, "job_states", job_states)
    monkeypatch.setattr(tool, "_file_refs", file_refs)
    monkeypatch.setattr(tool, "async_session", lambda: _Session())
    _defs, executor = tool.build_video_studio_tools(user_id=str(uuid.UUID(int=1)), conversation_id=str(uuid.uuid4()))
    return executor, project


def _call(executor, name: str, **arguments):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments)))


def _lists(content: str) -> dict:
    head = json.loads(content.split("\n", 1)[0])
    return {key: head[key] for key in ("status", "findings", "by_design", "facts", "infra_once")}


def test_a_preview_answer_parses_into_the_four_lists_and_its_verdict_reads_its_findings_alone(monkeypatch):
    """The issue's fourth test, on a scene preview: green with by-design and facts; blocked by one finding only."""
    audits, placed = _contain_insert()
    [report] = insert_checks.reports(audits, placed, fps=30)
    neighbour = render_checks.Finding(
        render_checks.INSERT_RESIDUAL_KEY,
        6.5,
        7.0,
        "s6",
        "on scene s6, drawn beside this one (its own preview judges it): residual key",
        render_checks.WARN,
        {"neighbour": True},
    )
    checks = render_checks.Checks(
        audio={"status": render_checks.PASSED},
        frames={"status": render_checks.PASSED},
        inserts=[report],
        findings=[neighbour],
    ).as_dict()
    row = _row(checks, scene_id="s5", notes=["Scene s5 previewed at standard quality (1920x1080, 30 fps)."])
    executor, project = _tools(monkeypatch, row)
    result = _call(executor, "render_video_scene", video_id=str(project.id), scene="s5")
    assert not result.is_error, result.content
    lists = _lists(result.content)
    assert lists["status"] == "green" and lists["findings"] == [] and lists["infra_once"] == []
    assert [item["because"] for item in lists["by_design"]] == ["fit contain, no key"]
    facts = [item["what"] for item in lists["facts"]]
    # A neighbour's finding is this preview's fact; its checks that passed and its notes too.
    assert any(text.startswith("seen in the transition from this scene: on scene s6") for text in facts)
    assert (
        "Audio: passed." in facts
        and "Picture: passed." in facts
        and "Scene s5 previewed at standard quality (1920x1080, 30 fps)." in facts
    )
    assert "Verdict: all gates green - no finding." in result.content
    # One finding of its own: blocked, by it alone - the by-design band and the facts unchanged beside it.
    halo = render_checks.Finding(
        render_checks.INSERT_HALO,
        1.0,
        2.0,
        "s5",
        "insert halo at t=0:01.00: halo - widen the key's feather",
        data={"insert": KEY},
        fix="widen the key's feather",
    )
    row.checks = render_checks.Checks(inserts=[report], findings=[halo, neighbour]).as_dict()
    blocked = _lists(_call(executor, "render_video_scene", video_id=str(project.id), scene="s5").content)
    assert blocked["status"] == "blocked" and [item["kind"] for item in blocked["findings"]] == [
        render_checks.INSERT_HALO
    ]
    [item] = blocked["findings"]
    assert (item["severity"], item["at"], item["scene"], item["fix"]) == (
        "blocks",
        1.0,
        "s5",
        "widen the key's feather",
    )
    assert item["what"] == "insert halo at t=0:01.00: halo"
    assert blocked["by_design"][0] == lists["by_design"][0]


def test_a_render_answer_carries_the_infra_advice_on_the_call_that_started_it_only(monkeypatch):
    """The issue's fourth test, on a whole render: its verdict, its facts, and the platform's sizing once."""
    estimate = render_preflight.with_infra_said(_ultra(), set())
    row = _row(render_checks.Checks(frames={"status": render_checks.PASSED}).as_dict(), scene_id=None)
    executor, project = _tools(monkeypatch, row, preflight=estimate)
    started = _call(executor, "render_video", video_id=str(project.id), quality="ultra")
    lists = _lists(started.content)
    assert lists["status"] == "green" and lists["findings"] == [] and lists["infra_once"] == estimate.infra_once()
    assert any(item["what"] == estimate.line() for item in lists["facts"])
    assert not any("VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB" in item["what"] for item in lists["facts"])
    assert "Platform (said once, for the administrator; no finding):" in started.content
    # The same render followed later: its measurements, never the sizing again.
    followed = _call(executor, "get_video_jobs", render_ids=[str(row.id)], verbose=True)
    assert not followed.is_error, followed.content
    assert "VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB" not in followed.content
    assert _lists(followed.content.split("\n\n", 1)[1])["infra_once"] == []
    # A render that ended without a finding of its checks to say why blocks with that reason.
    row.status, row.error = JOB_FAILED, "The render process was killed by signal 9."
    failed = _lists(_call(executor, "get_video_jobs", render_ids=[str(row.id)], verbose=True).content.split("\n\n", 1)[1])
    assert failed["status"] == "blocked" and failed["findings"][0]["kind"] == render_answer.RENDER_FAILED
    assert failed["findings"][0]["what"] == "failed: The render process was killed by signal 9."


# ── Done when: a 26-scene sweep with two inserts ──────────────────────────


async def test_a_26_scene_sweep_with_two_inserts_needs_one_preview_per_scene_and_lists_nothing_outside_findings(lane):
    """One ultra render in flight on w:1, a second worker w:2 drawing previews: every scene changed is previewed
    once, on its pages - no one-page run, no run again -, and every answer's verdict is its findings: none."""
    _ultra_in_flight()
    lane.rooms = [preview_routing.Room(worker="w:2", free=24 * _GIB, slots=2, bound=24 * _GIB)]
    adapter = adapters.VideoPreviewAdapter()
    drawn_on: dict[str, str] = {}
    scenes = [f"s{n}" for n in range(1, 27)]
    for pair in zip(scenes[::2], scenes[1::2], strict=True):
        lane.hold = asyncio.Event()
        executions = {scene: _execution(scene) for scene in pair}
        first = asyncio.create_task(adapter.execute(_ctx("w:1"), executions[pair[0]]))
        await asyncio.sleep(0)
        second = await adapter.execute(_ctx("w:1"), executions[pair[1]])
        lane.hold.set()
        assert (await first).kind == "completed"
        drawn_on[pair[0]] = "w:1"
        if second.kind == "defer":
            # Handed to w:2, which draws it there on its pages.
            assert second.claim_for[0] == "w:2" and second.adapter_state == preview_routing.ROUTED_STATE
            drawn_on[pair[1]] = "w:2"
        else:
            drawn_on[pair[1]] = "w:1"
    assert sorted(lane.ran) == sorted(pair_scene for pair_scene in scenes if drawn_on[pair_scene] == "w:1")
    assert set(drawn_on) == set(scenes) and all(lane.admissions[scene].pages == 2 for scene in lane.ran)
    assert len(lane.ran) == len(set(lane.ran))
    # Each answer: two of the scenes show an unkeyed contain insert - by design -, none has a finding.
    for scene in scenes:
        inserts = []
        if scene in ("s5", "s19"):
            audits, placed = _contain_insert()
            inserts = insert_checks.reports(audits, placed, fps=30)
        answer = render_answer.of_checks(
            render_checks.Checks(frames={"status": render_checks.PASSED}, inserts=inserts).as_dict()
        )
        assert answer.status == render_answer.GREEN and answer.findings == []
        assert answer.verdict().startswith("Verdict: all gates green - no finding.")
        assert bool(answer.by_design) == (scene in ("s5", "s19"))
