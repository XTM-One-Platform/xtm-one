"""An ultra render has a memory ceiling of its own, 16 GiB, and is given memory, never a lower quality.

A 19-scene, 130 s 2160p60 render with clips passed the 7.2 GiB ceiling a 12 GiB
worker gives every render (60% of its limit) at 59%. An ultra render may use
``VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB`` instead: never past the container's
limit less the 4 GiB the worker keeps for its own jobs or the 8 GiB preview
reserve (a hard floor), never less than the other renders'
ceiling. The supervisor reads the render's quality before its process starts,
the worker publishes each quality's ceiling, and the pre-flight holds an ultra
render to its own.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.video_studio import render_preflight, render_process, resources

_GIB = 1 << 30


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_ultra_render_memory_mb", 16384)
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 8192)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 2)
    monkeypatch.setattr(settings, "video_studio_render_pages", 0)
    # Three CPUs: the two pages they want need less than 16 GiB at their measured peak, the floor stands.
    monkeypatch.setattr(resources, "cpu_budget", lambda: 3)


def _container(monkeypatch, limit: int | None, host: int | None = None) -> None:
    monkeypatch.setattr(resources, "memory_limit", lambda: limit)
    monkeypatch.setattr(resources, "host_memory", lambda: host)


@pytest.mark.parametrize(
    ("limit", "high", "ultra"),
    [
        (24 * _GIB, int(24 * _GIB * 0.6), 16 * _GIB),  # Copilot's worker: 16 GiB, 8 GiB left to its previews
        # The worker of the failed render: all but the 8 GiB its scene previews keep, a hard floor.
        (12 * _GIB, 4 * _GIB, 4 * _GIB),
        (8 * _GIB, 0, 0),  # no bigger than the reserve: it holds no render at all
    ],
    ids=["24GiB", "12GiB", "8GiB"],
)
def test_an_ultra_render_may_use_16_gib_never_past_the_container_less_the_workers_own(monkeypatch, limit, high, ultra):
    _container(monkeypatch, limit)
    assert render_process.memory_ceiling() == render_process.memory_ceiling("high") == high
    assert render_process.memory_ceiling("ultra") == ultra


def test_the_preview_reserve_is_never_set_below_its_floor_and_a_worker_without_previews_keeps_none(monkeypatch):
    """``VIDEO_STUDIO_PREVIEW_RESERVE_MB`` raises the 8 GiB floor, never lowers it."""
    _container(monkeypatch, 12 * _GIB)
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 0)
    assert render_process.preview_reserve() == render_process.PREVIEW_RESERVE_FLOOR == 8 * _GIB
    assert render_process.memory_ceiling("ultra") == 4 * _GIB
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 10240)
    assert render_process.preview_reserve() == 10 * _GIB
    # A worker that draws no preview: its renders leave it its own 4 GiB only.
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    assert render_process.preview_reserve() == 0 and render_process.memory_ceiling("ultra") == 8 * _GIB


def test_an_explicit_budget_holds_within_the_preview_reserve_and_an_ultra_one_off_holds_ultra_to_it(monkeypatch):
    _container(monkeypatch, 24 * _GIB)
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 18000)
    # Never the 8 GiB a worker keeps for its scene previews: a render is admitted only where 16 GiB holds it.
    assert render_process.memory_ceiling("ultra") == render_process.memory_ceiling() == 16 * _GIB
    # A reserve set to nothing keeps its floor; a worker without previews keeps the 18000 asked.
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 0)
    assert render_process.memory_ceiling("ultra") == 16 * _GIB
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    assert render_process.memory_ceiling("ultra") == 18000 << 20  # higher than 16 GiB: kept
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 2)
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_ultra_render_memory_mb", 0)
    assert render_process.memory_ceiling("ultra") == int(24 * _GIB * 0.6)


def test_without_a_container_limit_an_ultra_render_is_bound_by_the_host(monkeypatch):
    _container(monkeypatch, None, host=64 * _GIB)
    assert render_process.memory_ceiling() == 4096 << 20
    assert render_process.memory_ceiling("ultra") == 16 * _GIB
    # A host no bigger than the reserve holds no render beside its previews.
    _container(monkeypatch, None, host=8 * _GIB)
    assert render_process.memory_ceiling("ultra") == 0
    _container(monkeypatch, None, host=12 * _GIB)
    assert render_process.memory_ceiling("ultra") == 4 * _GIB
    _container(monkeypatch, None, host=None)
    assert render_process.memory_ceiling("ultra") == 16 * _GIB


def test_an_ultra_render_draws_on_the_pages_its_cpus_want_where_the_container_has_the_memory(monkeypatch):
    """Ultra renders drew on 2 of 8 pages - a fixed 16 GiB held two 2160p pages at their measured peak."""
    monkeypatch.setattr(resources, "cpu_budget", lambda: 8)
    size = (3840, 2160)
    # The fixture's load at its peak: two video inserts and a picture on one page.
    inserts = render_preflight.InsertLoad(videos=2, pictures=1)

    def pages(limit: int) -> int:
        _container(monkeypatch, limit)
        ceiling = render_process.memory_ceiling("ultra")
        return render_preflight.layout(size, x264_preset="slow", ceiling=ceiling, cpus=8, inserts=inserts).pages

    # 24 GiB: all but the 8 GiB its previews keep, 16 GiB - what it held before.
    held = pages(24 * _GIB)
    assert held <= 3
    # 64 GiB: raised to what the 7 pages its 8 CPUs want need at their measured peak (never less than the other
    # renders' 60%), within the 56 GiB a render may take.
    _container(monkeypatch, 64 * _GIB)
    need = render_process.ultra_pages_need()
    assert 16 * _GIB < need <= 56 * _GIB
    assert render_process.memory_ceiling("ultra") == max(need, render_process.memory_ceiling())
    assert pages(64 * _GIB) == 7
    # 32 GiB: as many pages as the 24 GiB left to it hold, more than at 24 GiB.
    assert held < pages(32 * _GIB) < 7
    # Set to 0, an ultra render keeps the other renders' ceiling whatever the container has.
    monkeypatch.setattr(settings, "video_studio_ultra_render_memory_mb", 0)
    _container(monkeypatch, 64 * _GIB)
    assert render_process.memory_ceiling("ultra") == render_process.memory_ceiling()


def test_an_ultra_render_short_of_memory_is_told_the_memory_to_give_never_a_lower_quality(monkeypatch):
    _container(monkeypatch, 12 * _GIB)
    said = render_preflight.estimate(frames=7800, size=(3840, 2160), quality="ultra", ceiling=int(0.6 * _GIB))
    problem = said.problems[0]
    assert "set VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB to" in problem
    assert "of it stay with the worker's own jobs and its scene previews" in problem
    assert not any(word in problem.lower() for word in ("draft", "standard", " high", "shorter", "lower quality"))
    # A render of another quality keeps the message of the others.
    high = render_preflight.estimate(frames=7800, size=(1920, 1080), quality="high", ceiling=int(0.1 * _GIB))
    assert "set VIDEO_STUDIO_RENDER_MEMORY_MB to" in high.problems[0]


def test_a_render_stopped_at_its_ceiling_names_the_ultra_setting_too():
    ended = render_process.RenderExit(kind="memory", peak_memory=int(7.3 * _GIB), progress=59, stage="Drawing frames (62%)")
    said = ended.describe(ceiling=int(7.2 * _GIB), stall_seconds=120)
    # All but the 8 GiB its scene previews keep, more than the worker's own 4 GiB.
    assert "VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB for a 4K render" in said and "all but 8 GiB to a 4K one" in said


async def test_the_supervisor_holds_a_render_to_the_ceiling_of_its_quality(monkeypatch, tmp_path):
    _container(monkeypatch, 24 * _GIB)
    seen: dict = {}

    async def supervise(command, **options):
        seen["limit"] = options["memory_limit"]
        return render_process.RenderExit(kind="done", returncode=0)

    async def load(kind, job_id):
        return SimpleNamespace(quality="ultra", status="running")

    async def heartbeat(*_args, **_kwargs):
        return None

    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process.jobs, "load", load)
    monkeypatch.setattr(render_process.jobs, "heartbeat", heartbeat)
    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)
    outcome = await render_process.run_isolated(str(uuid.uuid4()))
    assert outcome.kind == "done" and seen["limit"] == 16 * _GIB


async def test_a_render_whose_quality_cannot_be_read_keeps_the_other_renders_ceiling(monkeypatch):
    async def load(kind, job_id):
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(render_process.jobs, "load", load)
    assert await render_process._render_of("r") is None
    assert render_process.ceiling_of(None) == render_process.memory_ceiling()


async def test_the_worker_publishes_the_ceiling_of_each_quality_and_the_preflight_reads_its_own(monkeypatch):
    _container(monkeypatch, 24 * _GIB)
    stored: dict = {}

    class _Redis:
        async def set(self, key, value, ex=None):
            stored[key] = value

        async def get(self, key):
            return stored.get(key)

    async def client():
        return _Redis()

    from app.core import redis as core_redis

    monkeypatch.setattr(core_redis, "redis_client", client)
    monkeypatch.setattr(render_preflight, "_published_at", [])
    await render_preflight.publish_worker()
    published = json.loads(next(iter(stored.values())))
    assert published["memory_ceiling"] == int(24 * _GIB * 0.6)
    assert published["memory_ceilings"]["ultra"] == 16 * _GIB
    assert published["memory_ceilings"]["high"] == int(24 * _GIB * 0.6)
    assert (await render_preflight.worker_capacity())["memory_ceilings"]["ultra"] == 16 * _GIB
