"""Issue #10054: a scene preview watches the take frames the render's watch reads for static_flicker."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from app.services.video_studio import flicker_take


def _page(rng: np.random.Generator) -> np.ndarray:
    return rng.uniform(0, 255, size=(64, 96)).astype(np.float32)


def _hover(page: np.ndarray) -> np.ndarray:
    # A hover dim over a third of the page: many textured tiles leave their picture.
    dimmed = page.copy()
    dimmed[:, :32] *= 0.4
    return dimmed


def _scene(frames: int) -> dict:
    return {
        "id": "s1",
        "type": "recording",
        "start": 0.0,
        "end": frames / 60,
        "src_fps": 60,
        "src_frames": frames,
        "time_map": [[0.0, 0.0], [frames / 60, frames / 60]],
    }


def test_the_watch_reads_every_other_render_frame_from_the_document_start() -> None:
    assert flicker_take.watch_step(60.0) == 2
    assert flicker_take.watch_step(30.0) == 1
    scene = {**_scene(10), "start": 1 / 60, "end": 11 / 60}
    order = flicker_take.watched_frames(scene, 60.0, 2)
    # The scene starts on odd render frame 1: the watch reads 2, 4, ... (its phase is the document's).
    assert [frame for frame, _take in order] == [2, 4, 6, 8, 10]
    assert [take for _frame, take in order] == [1, 3, 5, 7, 9]


@pytest.mark.asyncio
async def test_a_flicker_shorter_than_one_preview_frame_interval_is_found(monkeypatch) -> None:
    """A 60 fps take with a hover drawn on take frames 4 and 6 only (A A A A B A B A ...): the render's watch reads the
    even frames and sees A/B/A/B, a preview drawn from the odd ones sees A alone."""
    page = _page(np.random.default_rng(7))
    hover = _hover(page)
    pictures = {n: (hover if n in (4, 6, 8) else page) for n in range(16)}

    async def frames(path, wanted, *, src_fps, size):
        return {n: pictures[n] for n in wanted}

    monkeypatch.setattr(flicker_take, "read_take_frames", frames)
    sources = {"take/s1": SimpleNamespace(path="s1.mp4", fps=60.0)}
    found, summary = await flicker_take.findings(
        {"scenes": [_scene(16)]}, sources=sources, render_fps=60.0, span=(0.0, 16 / 60)
    )
    assert summary["scenes"] == ["s1"]
    assert len(found) == 1
    finding = found[0]
    assert finding.kind == "static_flicker" and finding.severity == "fail"
    assert finding.data["take_frame"] in (4, 6, 8)
    assert finding.data["render_fps"] == 60.0


@pytest.mark.asyncio
async def test_a_clean_take_stays_green(monkeypatch) -> None:
    page = _page(np.random.default_rng(3))

    async def frames(path, wanted, *, src_fps, size):
        return {n: page for n in wanted}

    monkeypatch.setattr(flicker_take, "read_take_frames", frames)
    sources = {"take/s1": SimpleNamespace(path="s1.mp4", fps=60.0)}
    found, summary = await flicker_take.findings(
        {"scenes": [_scene(16), {"id": "t", "type": "title", "start": 16 / 60, "end": 1.0}]},
        sources=sources,
        render_fps=60.0,
    )
    assert found == []
    assert summary["scenes"] == ["s1"] and summary["static_flickers"] == 0
