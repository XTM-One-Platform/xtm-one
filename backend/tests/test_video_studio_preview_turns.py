"""The preview lane takes one preview per video in turn, dealt when a slot claims.

Six previews of one video sat at places 3 to 15 behind another video's batch for 30 minutes, the queue line
saying "0 ahead" while two previews of other videos ran. No worker, no database: the lane is read from
synthetic executions (``preview_lane.read_lane`` patched) and dealt by the claim's own hook
(``preview_lane.deal_claims``) and the queue line's (``preview_lane.place_of``). What is proven:

* two videos queuing six previews each alternate in the action log, one slot or two, whichever asked first;
* a video's preview waiting out a hold leaves its video's turn to its next preview, never to another video;
* a video running its cap of previews is not claimed for; a resumed render and a preview handed to this worker
  go first;
* a queued preview's line names the video ahead and its remaining previews: "1 preview of video X ahead", the
  previews being made included - never a bare "0 ahead".
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.video_studio import jobs, preview_lane
from app.services.video_studio.preview_lane import Lane, VideoAhead

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
A, B = uuid.uuid4(), uuid.uuid4()


def _execution(n: int, *, state: str = "pending", adapter_state: str | None = None, payload: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        state=state,
        adapter_state=adapter_state,
        payload=payload or {},
        run_at=T0 + timedelta(seconds=n),
        created_at=T0 + timedelta(seconds=n),
    )


class _World:
    """The preview lane as the claims leave it: queued, being made, and when each video was last served."""

    def __init__(self, queued: list[tuple[uuid.UUID, int]]):
        self.pending = []
        self.video_of: dict = {}
        for video, count in queued:
            for _ in range(count):
                execution = _execution(len(self.pending))
                self.pending.append(execution)
                self.video_of[execution.id] = video
        self.running: list = []
        self.served: dict = {}
        self.clock = T0 + timedelta(hours=1)

    def lane(self) -> Lane:
        return Lane(list(self.pending), list(self.running), dict(self.video_of), dict(self.served))

    async def claim(self, worker: str = "w1", due: list | None = None):
        order = await preview_lane.deal_claims(None, list(self.pending if due is None else due), worker)
        if not order:
            return None
        taken = order[0]
        self.pending.remove(taken)
        self.running.append(taken)
        self.clock += timedelta(seconds=1)
        self.served[self.video_of[taken.id]] = self.clock
        return self.video_of[taken.id]

    def finish_oldest(self) -> None:
        self.running.pop(0)


@pytest.fixture
def world(monkeypatch):
    holder: dict = {}

    async def read_lane(_db):
        return holder["world"].lane()

    monkeypatch.setattr(preview_lane, "read_lane", read_lane)
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 10)

    def make(queued):
        holder["world"] = _World(queued)
        return holder["world"]

    return make


@pytest.mark.parametrize("slots", [1, 2])
def test_two_videos_queuing_six_previews_each_alternate_in_the_action_log(world, slots):
    lane = world([(A, 6), (B, 6)])
    names = {A: "A", B: "B"}
    log: list[str] = []

    async def run():
        while lane.pending:
            while len(lane.running) < slots and lane.pending:
                log.append(names[await lane.claim()])
            lane.finish_oldest()

    asyncio.run(run())
    assert log == ["A", "B"] * 6


def test_the_video_asked_second_is_served_before_the_first_videos_second_preview(world):
    lane = world([(A, 6)])

    async def run():
        # The first video's sweep already holds both slots when the second video asks.
        first = [await lane.claim(), await lane.claim()]
        for _ in range(6):
            execution = _execution(100 + len(lane.video_of))
            lane.pending.append(execution)
            lane.video_of[execution.id] = B
        lane.finish_oldest()
        return first, await lane.claim()

    first, next_one = asyncio.run(run())
    assert first == [A, A] and next_one == B


def test_a_preview_waiting_out_a_hold_leaves_its_turn_to_its_videos_next_preview(world):
    lane = world([(A, 3), (B, 3)])

    async def run():
        assert await lane.claim() == A
        # B's first is in a hold (its not_before window): due are the others only.
        held = next(e for e in lane.pending if lane.video_of[e.id] == B)
        return await preview_lane.deal_claims(None, [e for e in lane.pending if e is not held], "w1"), held

    order, held = asyncio.run(run())
    assert lane.video_of[order[0].id] == B and order[0] is not held


def test_a_video_at_its_cap_is_not_claimed_for_and_a_resumed_or_handed_preview_goes_first(world, monkeypatch):
    lane = world([(A, 3), (B, 1)])
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 1)

    async def run():
        assert await lane.claim() == A
        capped = await preview_lane.deal_claims(None, list(lane.pending), "w1")
        handed = _execution(50, payload={"claim_for": {"worker": "w1", "until": T0.isoformat()}})
        resumed = _execution(60, adapter_state=jobs.HANDED_OVER_STATE)
        lane.video_of[handed.id] = lane.video_of[resumed.id] = A
        lane.pending += [handed, resumed]
        first = await preview_lane.deal_claims(None, list(lane.pending), "w1")
        elsewhere = await preview_lane.deal_claims(None, list(lane.pending), "w2")
        return capped, first, elsewhere, handed, resumed

    capped, first, elsewhere, handed, resumed = asyncio.run(run())
    # A runs its cap of one: only B's preview is claimed for.
    assert [lane.video_of[e.id] for e in capped] == [B]
    assert first[:2] == [handed, resumed]
    # A resumed render goes first on every worker; handed to w1, the other is only A's next preview on w2, where A
    # runs its cap.
    assert elsewhere[0] is resumed and handed not in elsewhere


def test_the_queue_line_names_the_video_ahead_and_its_remaining_previews(world, monkeypatch):
    lane = world([(A, 1), (B, 1)])
    ahead, mine = lane.pending

    async def titles(_db, videos):
        return {A: "X", B: "Y"}

    monkeypatch.setattr(preview_lane, "_titles", titles)
    place = asyncio.run(preview_lane.place_of(None, mine))
    assert (place.ahead, place.running) == (1, 0)
    assert place.videos == (VideoAhead(str(A), "X", previews=1, being_made=0),)
    queue = jobs.QueuePlace(running=place.running, ahead=place.ahead, per_worker=2, lane="preview", videos=place.videos)
    row = SimpleNamespace(id=uuid.uuid4(), status="pending", progress=0, stage="Queued", progress_at=None)
    line = jobs.describe(jobs.job_state("render", row, queue=queue))
    assert line == "waiting for a worker: 1 preview of video X ahead (a worker makes 2 at a time)"
    assert queue.as_dict()["videos_ahead"] == [{"video_id": str(A), "title": "X", "previews": 1, "being_made": 0, "own": False}]

    # Two previews of X being made and none queued ahead: named, never "0 ahead".
    lane.pending.remove(ahead)
    lane.running = [ahead, _execution(9, state="running")]
    lane.video_of[lane.running[1].id] = A
    place = asyncio.run(preview_lane.place_of(None, mine))
    assert place.ahead == 0 and preview_lane.ahead_words(place.videos) == "2 previews of video X (being made) ahead"
    # Its own video's previews ahead are its own.
    assert VideoAhead(str(B), "Y", previews=3, being_made=1, own=True).words() == "3 of this video's previews (1 being made)"
