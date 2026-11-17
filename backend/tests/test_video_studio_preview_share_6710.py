"""The preview lane holds each video to its fair share of the slots, and a queued preview says how long it waits.

13 previews of one video waited about 20 minutes behind another video's batch: the lane dealt one preview per video
in turn, but only among the previews due at the instant a slot freed - one waiting for memory or handed to
another worker was not, and the other video's batch took the slot and kept it for its draw time. And the preview's
start answer named its place, never its wait. No worker, no database: the lane is read from synthetic executions
(``preview_lane.read_lane`` patched) and its workers from ``preview_routing.lane_workers`` patched. What is proven:

* while a video asking the lane holds less than its share of the slots, every video asking is held to that share: the
  batch of the other is not claimed for, even with nothing of the short video due at that instant;
* once each holds its share, the slots left go to whoever is due (a render slot lent to the lane is never left idle);
* a video whose previews are not asking (held further off than ``SHARE_HORIZON``) takes no share;
* a queued preview's line says its wait at the lane's pace, from its place, the slots and the median draw time.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.video_studio import jobs, preview_lane, preview_routing
from app.services.video_studio.preview_lane import Lane, VideoAhead

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _execution(seconds: float, *, state: str = "pending") -> SimpleNamespace:
    at = datetime.now(UTC) + timedelta(seconds=seconds)
    return SimpleNamespace(id=uuid.uuid4(), state=state, adapter_state=None, payload={}, run_at=at, created_at=at)


def _lane(pending: list[tuple[uuid.UUID, float]], running: list[uuid.UUID]) -> Lane:
    """*pending* ``(video, seconds until due)``, *running* the video of each preview being made."""
    video_of: dict = {}
    queued, made = [], []
    for video, due_in in pending:
        execution = _execution(due_in)
        video_of[execution.id] = video
        queued.append(execution)
    for video in running:
        execution = _execution(-120, state="running")
        video_of[execution.id] = video
        made.append(execution)
    return Lane(queued, made, video_of, {})


@pytest.fixture
def lane_of(monkeypatch):
    holder: dict = {}

    async def read_lane(_db):
        return holder["lane"]

    monkeypatch.setattr(preview_lane, "read_lane", read_lane)
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 10)

    def make(lane: Lane, *, slots: int, held_back: int = 0, workers: int = 1) -> Lane:
        holder["lane"] = lane

        async def lane_workers():
            return preview_routing.LaneWorkers(workers, slots, held_back)

        monkeypatch.setattr(preview_routing, "lane_workers", lane_workers)
        return lane

    return make


@pytest.mark.parametrize(
    ("slots", "videos", "share"), [(2, 2, 1), (1, 2, 1), (4, 3, 2), (2, 3, 1), (5, 2, 3), (2, 1, 10), (None, 3, 10)]
)
def test_a_videos_fair_share_is_its_even_part_of_the_slots_rounded_up(slots, videos, share):
    assert preview_lane.fair_share(slots, videos, 10) == share
    assert preview_lane.fair_share(8, 2, 3) == 3


def test_a_batch_holding_both_slots_is_not_claimed_for_while_another_video_waits_for_memory(lane_of):
    """Video A holds one slot and queues 12 more; B's preview waits for its memory, claimed again in 5 s. The free
    slot is B's: A is held to its share of 1, its batch left out of the claim."""
    lane = lane_of(_lane([*((A, -60 + n) for n in range(12)), (B, 5)], [A]), slots=2)
    due = [e for e in lane.pending if lane.video(e) == A]
    assert lane.caps(2, 10) == {A: 1, B: 1}
    assert asyncio.run(preview_lane.deal_claims(None, due, "w1")) == []
    # B's preview due: it is the one claimed.
    claimed = asyncio.run(preview_lane.deal_claims(None, list(lane.pending), "w1"))
    assert [lane.video(e) for e in claimed] == [B]


def test_the_slots_left_once_each_video_holds_its_share_go_to_whoever_is_due(lane_of):
    """A and B each hold one of 2 slots; a render slot lent to the lane claims: A's next is taken, never left idle."""
    lane = lane_of(_lane([*((A, -60 + n) for n in range(3)), (B, 5)], [A, B]), slots=2)
    assert lane.caps(2, 10) == {}
    claimed = asyncio.run(preview_lane.deal_claims(None, [e for e in lane.pending if lane.video(e) == A], "w1"))
    assert [lane.video(e) for e in claimed] == [A, A, A]


def test_a_video_held_further_off_than_the_horizon_asks_no_share(lane_of):
    far = preview_lane.SHARE_HORIZON.total_seconds() + 120
    lane = lane_of(_lane([*((A, -60 + n) for n in range(4)), (B, far)], [A]), slots=2)
    assert lane.asking() == {A}
    assert lane.caps(2, 10) == {}
    claimed = asyncio.run(preview_lane.deal_claims(None, [e for e in lane.pending if lane.video(e) == A], "w1"))
    assert len(claimed) == 4


def test_slots_held_back_beside_a_render_shrink_the_share(lane_of):
    """2 slots, 1 held back while a whole render draws: one open slot, so each of two videos gets one."""
    assert preview_lane.open_slots(preview_routing.LaneWorkers(1, 2, 1)) == 1
    assert preview_lane.open_slots(preview_routing.LaneWorkers(1, 2, 2)) == 1
    assert preview_lane.open_slots(None) is None
    lane = lane_of(_lane([(A, -30), (A, -20), (B, -10)], []), slots=2, held_back=1)
    claimed = asyncio.run(preview_lane.deal_claims(None, list(lane.pending), "w1"))
    assert [lane.video(e) for e in claimed] == [A, B]


def test_the_queue_line_deals_the_lane_as_a_claim_does(lane_of, monkeypatch):
    """A queued preview of A behind its own video's share: B's preview, due in 5 s, is dealt before it."""
    lane = lane_of(_lane([(A, -60), (A, -50), (B, 5)], [A]), slots=2)
    own = lane.pending[1]

    async def titles(_db, _videos):
        return {A: "Launch film", B: "Brand film"}

    monkeypatch.setattr(preview_lane, "_titles", titles)
    place = asyncio.run(preview_lane.place_of(None, own, preview_routing.LaneWorkers(1, 2, 0)))
    # Held to its share: B's preview and A's first are dealt before it.
    assert place.ahead == 2
    assert {(v.title, v.previews, v.being_made) for v in place.videos} == {("Launch film", 2, 1), ("Brand film", 1, 0)}


@pytest.mark.parametrize(
    ("ahead", "running", "slots", "draw", "wait"),
    [
        (13, 1, 1, 90.0, 45 + 13 * 90),
        (0, 1, 2, 90.0, 0.0),
        (3, 2, 2, 60.0, 30 + 60),
        (2, 1, 2, 60.0, 30 + 0),
        (5, 0, 2, 40.0, 20 + 40),
        (4, 1, None, 60.0, None),
        (4, 1, 2, None, None),
    ],
)
def test_a_queued_previews_wait_is_its_place_at_the_lanes_pace(ahead, running, slots, draw, wait):
    assert preview_lane.expected_wait(ahead, running, slots, draw) == wait


def test_a_queued_preview_says_its_wait_as_it_is_queued():
    """The start answer and get_video_jobs both read describe(): the wait is said with what it is estimated from."""
    place = jobs.QueuePlace(
        running=1,
        ahead=13,
        per_worker=2,
        lane="preview",
        videos=(VideoAhead(str(A), "Launch film", 14, 1),),
        workers=preview_routing.LaneWorkers(1, 2, 1),
        expected_wait_s=preview_lane.expected_wait(13, 1, 1, 90.0),
        draw=(90.0, 20),
    )
    state = jobs.JobState(kind="render", id="r", status=jobs.STATE_QUEUED, percent=0, stage="Queued", last_progress_at=None, queue=place)
    said = jobs.describe(state)
    assert said.endswith(
        "(1 worker draws previews, 2 at a time (1 held back while a whole render draws on its CPUs)); about 20 min "
        "before it starts at the lane's pace (a scene preview drawn in about 90 s, the median of the last 20)"
    )
    assert place.as_dict()["expected_wait_seconds"] == 1215 and place.as_dict()["median_draw_seconds"] == 90.0
    # A slot free for it, or nothing drawn yet: nothing said.
    assert preview_lane.wait_words(0.0, (90.0, 20)) == "" and preview_lane.wait_words(None, None) == ""
    assert preview_lane.wait_words(300.0, (120.0, 1)).endswith("(a scene preview drawn in about 2 min, the last one drawn)")


def test_a_queued_preview_says_the_lanes_p95_wait_and_when_it_is_expected_to_start():
    """Beside its own wait, the answer carries the lane's load - the p95 wait of the previews started in the last
    hour - and the instant it is expected to start."""
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    place = jobs.QueuePlace(
        running=1,
        ahead=13,
        per_worker=2,
        lane="preview",
        videos=(VideoAhead(str(A), "Launch film", 14, 1),),
        workers=preview_routing.LaneWorkers(1, 2, 0),
        expected_wait_s=preview_lane.expected_wait(13, 1, 2, 90.0),
        draw=(90.0, 20),
        p95_wait_s=1500.0,
    )
    assert place.expected_start_at(now) == now + timedelta(seconds=place.expected_wait_s)
    data = place.as_dict()
    assert data["p95_wait_seconds"] == 1500 and data["expected_start_at"] is not None
    state = jobs.JobState(kind="render", id="r", status=jobs.STATE_QUEUED, percent=0, stage="Queued", last_progress_at=None, queue=place)
    assert jobs.describe(state).endswith(
        "the median of the last 20; the previews started in the last hour waited 25 min at the 95th percentile)"
    )
    # No preview started in the last hour, or a slot free: nothing said of the load, no start instant.
    assert "95th" not in preview_lane.wait_words(300.0, (90.0, 20), None)
    assert preview_lane.wait_words(0.0, (90.0, 20), 1500.0) == ""
    unknown = jobs.QueuePlace(running=0, ahead=0, per_worker=2, lane="preview", videos=())
    assert unknown.expected_start_at(now) is None and unknown.as_dict()["expected_start_at"] is None


def test_the_p95_wait_is_read_off_the_previews_started_in_the_last_hour_never_a_kept_one():
    now = datetime.now(UTC)

    def row(wait: float, *, kept: bool = False):
        asked = now - timedelta(seconds=wait + 10)
        return (asked, now - timedelta(seconds=10), now, {"kept_from": "x"} if kept else {})

    class _Db:
        async def execute(self, _statement):
            rows = [row(10), row(20), row(30), row(40), row(1500), row(9000, kept=True)]
            return SimpleNamespace(all=lambda: rows)

    assert asyncio.run(preview_lane.recent_p95_wait(_Db(), now=now)) == 1500.0

    class _Empty:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [])

    assert asyncio.run(preview_lane.recent_p95_wait(_Empty(), now=now)) is None


def test_a_sweep_of_one_video_never_starves_another_videos_first_preview(lane_of, monkeypatch):
    """24 previews of A queued, 2 of B, 2 slots, nothing running: the first claim deals one of each, so B's
    first preview starts within the first slots' time whatever A's depth."""
    lane = lane_of(_lane([*((A, -120 + n) for n in range(24)), (B, -5), (B, -4)], []), slots=2)
    claimed = asyncio.run(preview_lane.deal_claims(None, list(lane.pending), "w1"))
    assert {lane.video(e) for e in claimed[:2]} == {A, B}

    async def titles(_db, _videos):
        return {A: "Launch film", B: "Brand film"}

    monkeypatch.setattr(preview_lane, "_titles", titles)
    # And B's first preview, queued behind A's 24, is dealt second: one video's share, never A's whole batch.
    place = asyncio.run(preview_lane.place_of(None, lane.pending[24], preview_routing.LaneWorkers(1, 2, 0)))
    assert place.ahead <= 1


def test_the_draw_time_is_the_median_of_the_last_previews_drawn_never_a_kept_one():
    now = datetime.now(UTC)

    def row(seconds: float, *, kept: bool = False):
        return (now - timedelta(seconds=seconds + 10), now - timedelta(seconds=10), {"kept_from": "x"} if kept else {})

    class _Db:
        async def execute(self, _statement):
            rows = [row(60), row(1), row(90), row(120, kept=False), row(5, kept=True)]
            return SimpleNamespace(all=lambda: rows)

    assert asyncio.run(preview_lane.draw_seconds(_Db())) == (75.0, 4)

    class _Empty:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [])

    assert asyncio.run(preview_lane.draw_seconds(_Empty())) is None
