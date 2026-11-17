"""#8457 slice 14: a take's capture line says how busy the worker's CPUs were while it copied its canvases."""

from __future__ import annotations

import asyncio

from app.services.video_studio import canvas_alone, resources
from app.services.video_studio.resources import CpuStat


def test_the_worker_cpu_is_said_on_the_capture_line() -> None:
    said = canvas_alone.worker_words({"cpus": 3.6, "of": 4, "throttled": 1.24})
    assert said == "the worker busy 3.6 of its 4 CPUs, 1.2 s held back by its quota"
    assert canvas_alone.worker_words({"cpus": 0.8, "of": 1, "throttled": 0}) == "the worker busy 0.8 of its 1 CPU"
    assert canvas_alone.worker_words({"cpus": 2.0}) == "the worker busy 2.0 CPUs"


def test_a_take_recorded_before_it_was_kept_says_nothing() -> None:
    assert canvas_alone.worker_words(None) == ""
    assert canvas_alone.worker_words({"of": 4}) == ""
    capture = {"alone": {"runs": [{"composited": True}]}}
    assert "worker busy" not in canvas_alone.capture_words(capture)


def test_capture_words_ends_with_the_worker_cpu() -> None:
    capture = {
        "alone": {
            "runs": [{"composited": True}],
            "cost": {"page": 3.0, "copied": 2},
            "worker": {"cpus": 3.9, "of": 4, "throttled": 0.0},
        }
    }
    said = canvas_alone.capture_words(capture)
    assert said.endswith("copies cost the page 1.5 ms (2 drawn, 0 encoded); the worker busy 3.9 of its 4 CPUs")


class _Page:
    async def evaluate(self, *_args, **_kwargs):
        return None


def test_the_tap_keeps_the_container_cpu_between_its_start_and_stop(monkeypatch, tmp_path) -> None:
    stats = [CpuStat(100.0, 10, 1, 0.5), CpuStat(107.5, 30, 4, 2.0)]
    clock = [1000.0, 1002.5]
    monkeypatch.setattr(resources, "cpu_stat", lambda: stats.pop(0) if len(stats) > 1 else stats[0])
    monkeypatch.setattr(resources, "cpu_budget", lambda: 4)
    monkeypatch.setattr(canvas_alone, "_clock", lambda: clock.pop(0) if len(clock) > 1 else clock[0])

    async def run() -> dict:
        tap = canvas_alone.Tap(_Page(), str(tmp_path), origin=0.0, max_copies=10)
        tap.start()
        assert tap._cpu_began is not None and tap._cpu_began[1] is not None, tap._cpu_began
        return await tap.stop()

    kept = asyncio.run(run())
    assert "worker" in kept, kept
    assert kept["worker"] == {"cpus": 3.0, "of": 4, "throttled": 1.5}


def test_an_unreadable_container_keeps_no_worker_cpu(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(resources, "cpu_stat", lambda: None)

    async def run() -> dict:
        tap = canvas_alone.Tap(_Page(), str(tmp_path), origin=0.0, max_copies=10)
        tap.start()
        return await tap.stop()

    assert "worker" not in asyncio.run(run())


def test_a_take_too_short_keeps_no_worker_cpu(monkeypatch, tmp_path) -> None:
    stats = [CpuStat(100.0), CpuStat(100.4)]
    clock = [1000.0, 1000.5]
    monkeypatch.setattr(resources, "cpu_stat", lambda: stats.pop(0) if len(stats) > 1 else stats[0])
    monkeypatch.setattr(canvas_alone, "_clock", lambda: clock.pop(0) if len(clock) > 1 else clock[0])

    async def run() -> dict:
        tap = canvas_alone.Tap(_Page(), str(tmp_path), origin=0.0, max_copies=10)
        tap.start()
        return await tap.stop()

    assert "worker" not in asyncio.run(run())
