"""The preview lane's slots grow with its queue, bounded by the worker's CPUs. Synthetic workers and rows.

A sweep of every scene of a video queued 24 previews on a lane of 2 slots: the p95 wait grew with the queue depth,
and the slots were read once per process when the adapter's semaphore was built. What is proven:

* the reconciler contract: an adapter's ``slots_added`` - ``slots_held_back``'s counterpart - is read each cycle, adds
  to its dedicated budget, grows its semaphore before a claim and never shrinks it; what cannot be read adds none;
* the preview lane adds a slot for each preview queued beyond its own, at most ``VIDEO_STUDIO_PREVIEW_CONCURRENCY_MAX``
  and what the CPUs its whole renders leave hold (2 a preview); none when the setting is ``0`` or the CPUs are taken;
* its ``periodic`` counts the lane's queue each tick, and the queue answer says the slots added and starts earlier.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import orchestration_reconciler
from app.services.orchestration_adapters.video_studio import VideoPreviewAdapter
from app.services.video_studio import jobs, preview_lane, preview_routing, render_process, resources


@pytest.fixture
def worker(monkeypatch):
    """A worker of 8 CPUs, 2 preview slots of its own, at most 4 while previews are queued, nothing running."""
    monkeypatch.setattr(resources, "cpu_budget", lambda: 8)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 2)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency_max", 4)
    monkeypatch.setattr(settings, "video_studio_render_pages", 8)
    monkeypatch.setattr(render_process, "_ATTEMPTS", {})
    monkeypatch.setattr(render_process, "_ADMITTED", {})
    monkeypatch.setattr(render_process, "_QUEUED", [0])
    monkeypatch.setattr(orchestration_reconciler, "_inflight_by_adapter", {})
    monkeypatch.setattr(orchestration_reconciler, "_adapter_semaphores", {})
    monkeypatch.setattr(orchestration_reconciler, "_adapter_semaphore_sizes", {})
    return render_process._ATTEMPTS


# -- 1. The reconciler contract ----------------------------------------------------------------


def test_slots_added_raise_a_dedicated_budget_as_slots_held_back_lower_it(monkeypatch):
    monkeypatch.setattr(orchestration_reconciler, "_inflight_by_adapter", {"lane": 3})
    lane = SimpleNamespace(name="lane", concurrency_limit=2, slots_held_back=0, slots_added=2)
    assert orchestration_reconciler._available_slots_for(lane, [lane]) == 1
    lane.slots_held_back = 1
    assert orchestration_reconciler._available_slots_for(lane, [lane]) == 0
    # The addition falls below what runs: none claimed, the running keep their slots.
    lane.slots_held_back, lane.slots_added = 0, 0
    assert orchestration_reconciler._available_slots_for(lane, [lane]) == 0


@pytest.mark.parametrize("said", [None, -2, True, "3", 2.5])
def test_slots_added_that_are_not_a_positive_int_add_none(said):
    assert orchestration_reconciler._slots_added(SimpleNamespace(name="lane", slots_added=said)) == 0


def test_slots_added_that_cannot_be_read_add_none():
    class _Broken:
        name = "lane"

        @property
        def slots_added(self) -> int:
            raise RuntimeError("queue not read")

    assert orchestration_reconciler._slots_added(_Broken()) == 0
    assert orchestration_reconciler._slots_added(SimpleNamespace(name="other")) == 0


def test_the_semaphore_grows_to_the_slots_added_and_never_shrinks(worker):
    lane = SimpleNamespace(name="lane", concurrency_limit=2, slots_added=0)
    orchestration_reconciler._grow_adapter_semaphore(lane)
    sem = orchestration_reconciler._adapter_semaphores["lane"]
    assert sem._value == 2
    lane.slots_added = 3
    orchestration_reconciler._grow_adapter_semaphore(lane)
    assert orchestration_reconciler._adapter_semaphores["lane"] is sem and sem._value == 5
    lane.slots_added = 1
    orchestration_reconciler._grow_adapter_semaphore(lane)
    assert sem._value == 5 and orchestration_reconciler._adapter_semaphore_sizes["lane"] == 5
    # An adapter of the shared pool has no semaphore of its own to grow.
    orchestration_reconciler._grow_adapter_semaphore(SimpleNamespace(name="shared", slots_added=4))
    assert "shared" not in orchestration_reconciler._adapter_semaphores


# -- 2. The preview lane's slots ---------------------------------------------------------------


def test_the_preview_lane_adds_a_slot_for_each_preview_queued_beyond_its_own(worker):
    lane = VideoPreviewAdapter()
    assert render_process.preview_slots_most() == 4
    assert lane.slots_added == 0 and render_process.preview_slots() == 2
    render_process.note_queued(1)
    assert lane.slots_added == 0
    render_process.note_queued(3)
    assert lane.slots_added == 1 and render_process.preview_slots() == 3
    render_process.note_queued(24)
    assert lane.slots_added == 2 and render_process.preview_slots() == 4
    assert orchestration_reconciler._available_slots_for(lane, [lane]) == 4
    # Two previews running and none queued: the added slots go as the queue empties.
    worker["p1"] = render_process._Attempt(preview=True)
    worker["p2"] = render_process._Attempt(preview=True)
    render_process.note_queued(0)
    assert lane.slots_added == 0


def test_the_slots_added_are_bounded_by_the_cpus_a_render_leaves(worker):
    render_process.note_queued(24)
    # A render on 6 of the 8 CPUs leaves 2: one preview's, no slot added; held back to the one kept beside it.
    worker["r1"] = render_process._Attempt(pages=6)
    assert render_process.preview_slots_most() == 2 and render_process.preview_slots_added() == 0
    assert render_process.preview_slots_held_back() == 1 and render_process.preview_slots() == 1


def test_no_slot_is_added_where_the_setting_is_static_or_the_worker_has_no_cpu_to_spare(worker, monkeypatch):
    render_process.note_queued(24)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency_max", 0)
    assert not render_process.adds_preview_slots() and render_process.preview_slots_added() == 0
    assert render_process.preview_slots_most() == 2
    # A preview worker of 4 CPUs already runs the 2 previews they hold.
    monkeypatch.setattr(settings, "video_studio_preview_concurrency_max", 6)
    monkeypatch.setattr(resources, "cpu_budget", lambda: 4)
    assert render_process.preview_slots_added() == 0
    # A worker that draws no preview adds none.
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    assert render_process.preview_slots_added() == 0 and render_process.preview_slots_most() == 0


def test_its_periodic_counts_the_lanes_queue_each_tick(worker, monkeypatch):
    counted: list[int] = []
    published: list[str] = []

    async def queued(_db):
        counted.append(1)
        return 9

    async def publish(worker_id, **_kwargs):
        published.append(worker_id)

    monkeypatch.setattr(preview_lane, "queued_count", queued)
    monkeypatch.setattr(preview_routing, "publish", publish)
    asyncio.run(VideoPreviewAdapter().periodic(SimpleNamespace(db=None, worker_id="w1")))
    assert counted == [1] and published == ["w1"] and render_process._QUEUED == [9]
    # Static: the queue is not counted.
    monkeypatch.setattr(settings, "video_studio_preview_concurrency_max", 0)
    asyncio.run(VideoPreviewAdapter().periodic(SimpleNamespace(db=None, worker_id="w1")))
    assert counted == [1] and published == ["w1", "w1"]


def test_the_lanes_queue_is_its_pending_previews_counted():
    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalar=lambda: 7)

    assert asyncio.run(preview_lane.queued_count(_Db())) == 7


# -- 3. The queue answer -----------------------------------------------------------------------


def test_the_workers_say_the_slots_they_add_and_the_lane_sums_them(monkeypatch):
    record = asdict(preview_routing.Room("w1", None, 4, capacity=4, added=2))
    assert preview_routing._room(json.dumps(record)).added == 2

    async def two(**_kwargs):
        return [
            preview_routing.Room("w1", None, 4, capacity=4, added=2),
            preview_routing.Room("w2", None, 2, capacity=2),
        ]

    monkeypatch.setattr(preview_routing, "rooms", two)
    preview_routing.forget_lane_workers()
    lane = asyncio.run(preview_routing.lane_workers())
    preview_routing.forget_lane_workers()
    assert (lane.workers, lane.slots, lane.added) == (2, 6, 2)
    assert lane.words() == "2 workers draw previews, 6 at a time (2 added while previews are queued)"
    both = preview_routing.LaneWorkers(1, 4, 1, 2)
    assert both.words() == (
        "1 worker draws previews, 4 at a time (2 added while previews are queued; 1 held back while a whole render "
        "draws on its CPUs)"
    )


def test_a_queued_preview_starts_earlier_on_the_slots_added_and_says_them():
    now = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)

    def place(workers):
        return jobs.QueuePlace(
            running=2,
            ahead=12,
            per_worker=2,
            lane="preview",
            videos=(),
            workers=workers,
            expected_wait_s=preview_lane.expected_wait(12, 2, preview_lane.open_slots(workers), 90.0),
            draw=(90.0, 20),
        )

    static, grown = place(preview_routing.LaneWorkers(1, 2)), place(preview_routing.LaneWorkers(1, 4, 0, 2))
    assert grown.expected_start_at(now) < static.expected_start_at(now)
    assert grown.expected_start_at(now) == now + timedelta(seconds=grown.expected_wait_s)
    assert grown.as_dict()["preview_slots"] == 4 and grown.as_dict()["preview_slots_added"] == 2
    assert static.as_dict()["preview_slots_added"] == 0
