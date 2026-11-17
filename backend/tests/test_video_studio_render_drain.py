"""A stopping worker waits for the renders it draws past half their frames, and hands the others over at once.

The 22 Oct 2026 render was handed over at 72% and at 81% by two deploys, each costing it 10-20 min waiting for a slot
elsewhere. Synthetic processes and attempts:

* a stop waits for a whole render past 50% to end, up to ``VIDEO_STUDIO_RENDER_DRAIN_SECONDS`` from the stop, never
  past what the deployment's termination grace leaves for the hand-over; a scene preview, or a render under 50%, is
  never waited for;
* while the worker drains, a render under 50% is handed over at once when a running worker draws renders, and keeps
  drawing when none does;
* the supervisor hands over an attempt handed on and stops one whose slot is asked back, each attempt told the
  progress its process reports;
* an attempt whose slot was asked back keeps its work and says so; the adapter puts it back in its place in the queue,
  due now, its attempt unspent.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import orchestration_reconciler, worker_presence
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import jobs, render_process, runners
from tests.test_video_studio_render_handover import _child, _execution, _noop, adapter, attempt  # noqa: F401


@pytest.fixture
def drawing(monkeypatch):
    """This worker's attempts, empty, polled often; the stop asked now."""
    attempts: dict[str, render_process._Attempt] = {}
    asked = time.monotonic()
    monkeypatch.setattr(render_process, "_ATTEMPTS", attempts)
    monkeypatch.setattr(render_process, "_DRAIN_POLL_SECONDS", 0.05)
    monkeypatch.setattr(orchestration_reconciler, "stop_asked_at", lambda: asked)
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 1260)
    return attempts


def _attempt(progress: int, *, preview: bool = False) -> render_process._Attempt:
    found = render_process._Attempt(preview=preview)
    found.progress = progress
    return found


def test_the_wait_is_bounded_by_what_the_deployment_lets_a_stop_take(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_drain_seconds", 1200)
    # Kubernetes' 30 s when the deployment says nothing: no time left to wait beside the hand-over.
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 0)
    assert render_process.drain_seconds() == 0
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 1260)
    assert render_process.drain_seconds() == 1200
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 600)
    assert render_process.drain_seconds() == 600 - render_process.DRAIN_MARGIN_SECONDS
    monkeypatch.setattr(settings, "video_studio_render_drain_seconds", 0)
    assert render_process.drain_seconds() == 0


async def test_the_stop_waits_for_a_render_past_half_until_it_ends(drawing, monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_drain_seconds", 30)
    drawing["render-at-80"] = _attempt(80)
    drawing["render-at-30"] = _attempt(30)
    drawing["preview-at-90"] = _attempt(90, preview=True)
    asyncio.get_running_loop().call_later(0.4, drawing.pop, "render-at-80")
    began = time.monotonic()
    await asyncio.wait_for(render_process.finish_before_stop(), 10)
    # It ended: the stop goes on at once, never waiting for the preview nor for the render under half.
    assert 0.3 <= time.monotonic() - began < 3


async def test_the_stop_hands_a_render_past_half_over_once_its_wait_is_spent(drawing, monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_drain_seconds", 1)
    drawing["render-at-81"] = _attempt(81)
    began = time.monotonic()
    await asyncio.wait_for(render_process.finish_before_stop(), 10)
    assert 0.8 <= time.monotonic() - began < 3 and "render-at-81" in drawing


async def test_nothing_past_half_and_the_stop_waits_for_nothing(drawing, monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_drain_seconds", 1200)
    drawing["render-at-49"] = _attempt(49)
    drawing["preview-at-99"] = _attempt(99, preview=True)
    began = time.monotonic()
    await render_process.finish_before_stop()
    assert time.monotonic() - began < 0.5


async def test_a_render_under_half_goes_to_a_running_worker_at_once(drawing, monkeypatch):
    drawing["render-at-30"] = under = _attempt(30)
    drawing["render-at-72"] = past = _attempt(72)
    drawing["preview-at-10"] = preview = _attempt(10, preview=True)
    counted: list[tuple[str | None, bool]] = []

    async def count(*, at_most: int = 256, claiming: str | None = None, incoming: bool = False):
        counted.append((claiming, incoming))
        return running

    monkeypatch.setattr(worker_presence, "count", count)
    # No running worker of the incoming release draws renders yet (a Compose recreate): it keeps drawing.
    running = 0
    assert await render_process.hand_over_early() == [] and not under.handing.is_set()
    running = 1
    assert await render_process.hand_over_early() == ["render-at-30"]
    assert under.handing.is_set() and not past.handing.is_set() and not preview.handing.is_set()
    assert counted == [(jobs.RENDER_ADAPTER, True), (jobs.RENDER_ADAPTER, True)]
    # Handed on once.
    assert await render_process.hand_over_early() == []


def _records(*workers: tuple[str, str, str]) -> list[dict]:
    return [
        {"worker_id": worker, "version": version, "state": state, "claims": [jobs.RENDER_ADAPTER]}
        for worker, version, state in workers
    ]


async def test_a_render_at_30_on_a_draining_old_worker_goes_to_the_new_release_only(drawing, monkeypatch):
    """Two workers of the outgoing release (one draining, one still running and stopped next) and one of the
    incoming release: the render at 30 % is handed over, to the incoming worker alone."""
    monkeypatch.setattr(settings, "app_version", "1.0.0-old")
    world = _records(("old-a", "1.0.0-old", "draining"), ("old-b", "1.0.0-old", "running"))

    async def present(*, at_most: int = 256):
        return world

    monkeypatch.setattr(worker_presence, "present", present)
    drawing["render-at-30"] = under = _attempt(30)
    # Only a running worker of its own release: stopped by the same rollout next, so it keeps drawing.
    assert await worker_presence.count(claiming=jobs.RENDER_ADAPTER) == 1
    assert await render_process.hand_over_early() == [] and not under.handing.is_set()
    assert await adapters.incoming_hand("old-a") is None
    # The incoming release comes up: the render is handed over, held for that worker.
    world += _records(("new-a", "1.0.1-new", "running"))
    assert await render_process.hand_over_early() == ["render-at-30"] and under.handing.is_set()
    assert await adapters.incoming_hand("old-a") == ("new-a", adapters.INCOMING_HOLD_SECONDS)


async def test_with_no_other_release_a_running_worker_takes_it_once_the_wait_passed(drawing, monkeypatch):
    """A plain restart or a scale-down: no other release ever comes, so past the incoming wait from the stop
    any running worker takes the render, as before."""
    monkeypatch.setattr(settings, "app_version", "1.0.0")

    async def present(*, at_most: int = 256):
        return _records(("me", "1.0.0", "draining"), ("peer", "1.0.0", "running"))

    monkeypatch.setattr(worker_presence, "present", present)
    drawing["render-at-20"] = under = _attempt(20)
    assert await render_process.hand_over_early() == []
    asked = time.monotonic() - render_process.incoming_wait_seconds() - 1
    monkeypatch.setattr(orchestration_reconciler, "stop_asked_at", lambda: asked)
    assert await render_process.hand_over_early() == ["render-at-20"] and under.handing.is_set()


def test_the_incoming_wait_never_outlasts_the_drain(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_drain_seconds", 1200)
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 1260)
    assert render_process.incoming_wait_seconds() == render_process.INCOMING_WAIT_SECONDS
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 0)
    assert render_process.incoming_wait_seconds() == 0


def test_relaunches_over_fewer_builds_say_so():
    """A render relaunched more than once per build shows it: N rollouts, at most N relaunches expected."""
    one_per_build = [{"progress": 10, "version": "b1"}, {"progress": 20, "version": "b2"}]
    assert "over" not in render_process.relaunch_line(one_per_build)
    twice = [*one_per_build, {"progress": 25, "version": "b2"}]
    assert render_process.relaunch_line(twice).startswith("Relaunched on another worker 3 times (over 2 builds): ")
    # Older rows name no build: said as before.
    assert render_process.relaunch_line([{"progress": 5}, {"progress": 6}]).startswith("Relaunched on another worker 2 times: ")


async def test_the_supervisor_hands_an_attempt_on_and_stops_one_whose_slot_is_asked_back():
    progress = {"type": "progress", "progress": 30, "stage": "Drawing frames (24%)"}
    command = _child(f"report({json.dumps(progress)})\ntime.sleep(120)\n")
    for event, kind in (("handing", "handed_over"), ("give_back", "gave_back")):
        attempt_of = render_process._Attempt()
        give_back = asyncio.Event()
        stop = attempt_of.handing if event == "handing" else give_back

        async def once_reported(attempt_of=attempt_of, stop=stop) -> None:
            # Asked only once the attempt was told its process's report: never on a timer the child's start can outrun.
            while attempt_of.progress != 30:
                await asyncio.sleep(0.01)
            stop.set()

        asking = asyncio.create_task(once_reported())
        began = time.monotonic()
        ended = await asyncio.wait_for(
            render_process.supervise(
                command,
                memory_limit=None,
                stall_seconds=60,
                poll_seconds=0.1,
                attempt=attempt_of,
                give_back=give_back,
            ),
            30,
        )
        assert asking.done()
        assert ended.kind == kind and ended.progress == 30 and attempt_of.progress == 30
        assert time.monotonic() - began < 15


async def test_an_attempt_whose_slot_is_asked_back_keeps_its_work_and_says_so(attempt, monkeypatch):  # noqa: F811
    attempt.exit = render_process.RenderExit(kind="handed_over", progress=12, stage="Drawing frames (0%)")
    render_id = str(uuid.uuid4())
    give_back = asyncio.Event()
    drawing = render_process.supervise

    async def asked_back_while_drawing(command, **options):
        give_back.set()
        return await drawing(command, **options)

    monkeypatch.setattr(render_process, "supervise", asked_back_while_drawing)
    outcome = await render_process.run_isolated(render_id, give_back=give_back)
    assert outcome.kind == "gave_back" and render_process.workdir_of(render_id).is_dir() and attempt.forgotten == []
    assert attempt.marks[-1]["stage"].startswith("Gave its slot back at 12% to a render that held one before it")
    # Asked back before its process started: none is started.
    asked = asyncio.Event()
    asked.set()
    supervised = len(attempt.supervised)
    assert (await render_process.run_isolated(render_id, give_back=asked)).kind == "gave_back"
    assert len(attempt.supervised) == supervised


async def test_a_render_that_gave_its_slot_back_waits_in_its_place_its_attempt_unspent(adapter):  # noqa: F811
    give_back = asyncio.Event()
    adapter.outcome = runners.JobOutcome("gave_back", "Gave its slot back at 12%.")
    ctx = SimpleNamespace(
        db=SimpleNamespace(commit=_noop), worker_id="new:1", cancel_event=None, stopping=None, give_back=give_back
    )
    result = await adapters.VideoRenderAdapter().execute(ctx, _execution())
    assert adapter.options["give_back"] is give_back
    assert (result.kind, result.adapter_state, result.place) == ("defer", jobs.SLOT_GIVEN_STATE, "keep")
    # Due at once on any other worker; on this one only once the render it gave its slot to is no longer in its hand.
    assert result.not_for == "new:1" and result.backoff_seconds == orchestration_reconciler.GIVE_BACK_HOLD_SECONDS
    # Never a state a slot is kept through: it takes no slot back from anyone.
    assert jobs.SLOT_GIVEN_STATE not in adapters.VideoRenderAdapter.keeps_slot_through
    assert adapters.VideoPreviewAdapter.keeps_slot_through == ()
