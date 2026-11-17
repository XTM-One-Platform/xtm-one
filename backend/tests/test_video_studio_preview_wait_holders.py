"""A scene preview that waited for memory says what held it, and once what to give its worker.

Production previews waited 295 s and 604 s for 7 GiB on a 24 GiB worker, their
answer saying "the runs of its worker hold 18.6 GiB": read as other videos'
renders, the report blamed the preview reserve. It held: a render (about
11.6 GiB) and the first preview (7 GiB) took the memory, and the worker runs
two previews at a time on a reserve that holds one. What is proven:

* the reason a preview waits names what holds its worker's memory, by kind;
* a worker whose reserve holds fewer previews of a size than it runs at once
  is told the reserve that holds them all and the memory to add, or a
  preview-only worker; one whose reserve holds them is told nothing;
* a preview that waited records that advice on its row, and its answer lists
  it under ``infra_once``; the next preview with the same numbers keeps it
  unsaid.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.video_studio import JOB_READY
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import (
    jobs,
    preview_routing,
    render_answer,
    render_preflight,
    render_process,
    resources,
    runners,
)

_GIB = 1 << 30
#: The preview that waited: 7.5 GiB on its two pages, a 1080p scene with screen inserts.
_LARGEST = render_process.PreviewSize(ceiling=int(7.5 * _GIB), one_page=int(4.2 * _GIB), inserts=True)


@pytest.fixture(autouse=True)
def _worker(monkeypatch):
    """The production worker: 24 GiB, the 8 GiB reserve floor, two preview slots."""
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_ultra_render_memory_mb", 16384)
    monkeypatch.setattr(settings, "video_studio_preview_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 8192)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 2)
    monkeypatch.setattr(render_process, "_ATTEMPTS", {})
    monkeypatch.setattr(render_process, "_ADMITTED", {})
    monkeypatch.setattr(resources, "memory_limit", lambda: 24 * _GIB)
    monkeypatch.setattr(resources, "host_memory", lambda: None)


def _render_holding(held: int) -> None:
    attempt = render_process._Attempt()
    attempt.ceiling = held
    render_process._ATTEMPTS["render"] = attempt


def test_the_reason_a_preview_waits_names_what_holds_its_workers_memory():
    _render_holding(int(11.6 * _GIB))
    assert render_process.admit_preview("p1", render_process.PreviewSize(ceiling=7 * _GIB, one_page=4 * _GIB)) is None
    assert render_process.holders() == "1 render (11.6 GiB) and 1 scene preview (7.0 GiB)"
    assert render_process.admit_preview("p2", render_process.PreviewSize(ceiling=7 * _GIB, one_page=4 * _GIB)) == (
        "its 2 pages need 7.0 GiB, and its worker holds 18.6 GiB of its 24.0 GiB for 1 render (11.6 GiB) and "
        "1 scene preview (7.0 GiB) (5.4 GiB left)"
    )
    render_process.release_preview("p1")
    render_process._ATTEMPTS.clear()
    assert render_process.holders() == "nothing"


def test_a_reserve_short_of_the_previews_a_worker_runs_says_what_holds_them_all(monkeypatch):
    advice = render_process.reserve_advice(_LARGEST)
    assert advice is not None and advice.kind == "preview_reserve"
    assert advice.key == "preview_reserve:8192:2:16384"
    assert (
        "keeps 8 GiB out of every render's reach, which holds 1 preview of 7.5 GiB at a time, and it runs 2 at once"
        in advice.text
    )
    assert (
        "set VIDEO_STUDIO_PREVIEW_RESERVE_MB to 16384 and give the worker's container 8 GiB more memory" in advice.text
    )
    assert "a second worker that draws scene previews only (VIDEO_STUDIO_RENDER_CONCURRENCY=0)" in advice.text
    # A reserve that holds both, one slot, or a worker that draws no preview: nothing to give.
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 16384)
    assert render_process.reserve_advice(_LARGEST) is None
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 8192)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 1)
    assert render_process.reserve_advice(_LARGEST) is None
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    assert render_process.reserve_advice(_LARGEST) is None
    # Three slots, a draft that did not fit beside two others: a reserve that holds three at their worst case.
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 3)
    draft = render_process.PreviewSize(ceiling=int(2.7 * _GIB), one_page=int(1.6 * _GIB))
    assert render_process.reserve_advice(draft).key == "preview_reserve:8192:3:24576"
    # A preview past the floor (its pages' budget alone is more): held at its own size.
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 2)
    large = render_process.PreviewSize(ceiling=9 * _GIB, one_page=5 * _GIB)
    assert render_process.reserve_advice(large).key == "preview_reserve:8192:2:18432"
    assert "which holds no preview of 9 GiB at a time" in render_process.reserve_advice(large).text


class _Previews:
    """The rows a preview's advice is recorded on: the preview itself, and the earlier previews, newest first."""

    def __init__(self, row: SimpleNamespace, earlier: list[dict]) -> None:
        self.row, self.earlier, self.commits = row, earlier, 0

    async def get(self, _model, key):
        return self.row if key == self.row.id else None

    async def execute(self, _statement):
        return SimpleNamespace(all=lambda: [(preflight,) for preflight in self.earlier])

    async def commit(self):
        self.commits += 1


async def test_a_preview_that_waited_records_the_advice_once_and_lists_it_under_infra_once():
    advice = render_process.reserve_advice(_LARGEST)
    first = SimpleNamespace(id=uuid.uuid4(), preflight=None, status=JOB_READY, notes=[], scene_id="intro")
    db = _Previews(first, [])
    await render_preflight.record_preview_infra(db, str(first.id), advice)
    assert db.commits == 1 and first.preflight["infra"] == [advice.as_dict()]
    assert render_preflight.infra_of(first) == [advice.text]
    answer = render_answer.of_render(first, None, infra_once=render_preflight.infra_of(first))
    assert answer.infra_once == [advice.text] and advice.text not in [fact["what"] for fact in answer.facts]
    assert "Platform (said once, for the administrator; no finding):" in answer.text()
    # The next preview with the same numbers: recorded, unsaid.
    second = SimpleNamespace(id=uuid.uuid4(), preflight=None, status=JOB_READY, notes=[], scene_id="outro")
    await render_preflight.record_preview_infra(_Previews(second, [first.preflight]), str(second.id), advice)
    assert second.preflight["infra"][0]["said"] is False and render_preflight.infra_of(second) == []
    # Other numbers: said again.
    third = SimpleNamespace(id=uuid.uuid4(), preflight=None)
    newer = render_preflight.Infra(kind="preview_reserve", key="preview_reserve:8192:3:17408", text="other numbers")
    await render_preflight.record_preview_infra(
        _Previews(third, [second.preflight, first.preflight]), str(third.id), newer
    )
    assert render_preflight.infra_of(third) == ["other numbers"]


@pytest.fixture
def lane(monkeypatch):
    world = SimpleNamespace(ran=[], recorded=[], hold=None)

    async def run_isolated(render_id, **_options):
        world.ran.append(render_id)
        if world.hold is not None:
            await world.hold.wait()
        return runners.JobOutcome("done")

    async def size_of(_render_id):
        return _LARGEST

    async def nothing(*_args, **_kwargs):
        return None

    async def no_rooms(**_kwargs):
        return []

    async def mark_if_open(_kind, _job_id, **_changes):
        return True

    async def record(_db, render_id, advice):
        world.recorded.append((render_id, advice.key))

    monkeypatch.setattr(render_process, "run_isolated", run_isolated)
    monkeypatch.setattr(render_process, "preview_size_of", size_of)
    monkeypatch.setattr(preview_routing, "rooms", no_rooms)
    monkeypatch.setattr(preview_routing, "publish", nothing)
    monkeypatch.setattr(jobs, "preview_waits", nothing)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(render_preflight, "record_preview_infra", record)
    monkeypatch.setattr(adapters.wake, "schedule", lambda *_args: None)
    return world


def _ctx() -> SimpleNamespace:
    async def commit():
        return None

    return SimpleNamespace(db=SimpleNamespace(commit=commit), worker_id="w:1", cancel_event=None, stopping=None)


async def test_only_a_preview_that_waited_for_memory_records_the_advice(lane):
    _render_holding(int(11.6 * _GIB))
    lane.hold = asyncio.Event()
    adapter = adapters.VideoPreviewAdapter()
    running = asyncio.create_task(
        adapter.execute(
            _ctx(),
            SimpleNamespace(
                id=uuid.uuid4(), payload={"render_id": "p1"}, attempt_count=1, max_attempts=3, adapter_state=None
            ),
        )
    )
    await asyncio.sleep(0)
    waiting = SimpleNamespace(
        id=uuid.uuid4(), payload={"render_id": "p2"}, attempt_count=1, max_attempts=3, adapter_state=None
    )
    deferred = await adapter.execute(_ctx(), waiting)
    assert deferred.adapter_state == adapters.MEMORY_WAITING_STATE
    lane.hold.set()
    assert (await running).kind == "completed"
    # The first started at once: nothing to say of the platform's sizing.
    assert lane.recorded == []
    # The second, run again once the first ended, waited for its pages: its row carries what to give the worker.
    since = (datetime.now(UTC) - timedelta(seconds=295)).isoformat()
    waiting.payload = {**waiting.payload, **deferred.payload_update, adapters.MEMORY_WAIT_KEY: since}
    waiting.adapter_state = deferred.adapter_state
    lane.hold = None
    assert (await adapter.execute(_ctx(), waiting)).kind == "completed"
    assert lane.recorded == [("p2", "preview_reserve:8192:2:16384")]
