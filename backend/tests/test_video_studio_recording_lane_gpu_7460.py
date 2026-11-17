"""A Video Studio recording lane on a GPU node: the GPU or nothing.

``VIDEO_STUDIO_RECORDING_GL=gpu`` is the recording lane of a worker scheduled on a GPU node. It probes its GPU (ANGLE on
Vulkan, then on EGL), logs the WebGL renderer, and records only once Chromium draws WebGL on the GPU: a worker that
reads SwiftShader there claims no recording rather than recording one in software, and a take that reaches it anyway
is refused before its browser opens. ``auto`` and ``software`` are unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.orchestration_adapters.video_studio import VideoTakeAdapter
from app.services.video_studio import capture_rate


@pytest.fixture
def fresh_backend(monkeypatch):
    monkeypatch.setattr(capture_rate, "_BACKEND", [])
    monkeypatch.setattr(capture_rate.settings, "video_studio_take_concurrency", 1)


def _gpu(monkeypatch, statuses: dict) -> list:
    """``VIDEO_STUDIO_RECORDING_GL=gpu`` with a probe answering *statuses* per launch flags (an exception raised)."""
    probed: list = []

    async def probe(args):
        probed.append(args)
        found = statuses[args]
        if isinstance(found, Exception):
            raise found
        return dict(found)

    monkeypatch.setattr(capture_rate, "_probe", probe)
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "gpu")
    return probed


def test_a_lane_whose_chromium_draws_on_the_gpu_records_and_says_its_renderer(monkeypatch, fresh_backend):
    renderer = "ANGLE (NVIDIA, Vulkan 1.3.277 (NVIDIA L4 (0x000027B8)), NVIDIA-550.90.7.0)"
    probed = _gpu(monkeypatch, {capture_rate.GPU_GL_ARGS: {"webgl": "enabled", "canvas_2d": "enabled", "renderer": renderer}})
    assert capture_rate.recording_refused() == "its GPU is not probed yet"
    assert VideoTakeAdapter().claims_here is False

    backend = asyncio.run(capture_rate.gl_backend())
    assert probed == [capture_rate.GPU_GL_ARGS]
    assert (backend.gpu, backend.refused, backend.args, backend.renderer) == (True, False, capture_rate.GPU_GL_ARGS, renderer)
    assert backend.words() == "GPU GL"
    assert capture_rate.recording_refused() is None
    assert VideoTakeAdapter().claims_here is True


def test_a_lane_tries_egl_when_vulkan_did_not_reach_the_gpu(monkeypatch, fresh_backend):
    probed = _gpu(
        monkeypatch,
        {
            capture_rate.GPU_GL_ARGS: {"webgl": "unavailable_software", "canvas_2d": "unavailable_software", "renderer": "SwiftShader"},
            capture_rate.GPU_EGL_ARGS: {"webgl": "enabled", "canvas_2d": "enabled", "renderer": "ANGLE (NVIDIA, EGL)"},
        },
    )
    backend = asyncio.run(capture_rate.gl_backend())
    assert probed == [capture_rate.GPU_GL_ARGS, capture_rate.GPU_EGL_ARGS]
    assert backend.gpu and backend.args == capture_rate.GPU_EGL_ARGS and not backend.refused


@pytest.mark.parametrize(
    "vulkan",
    [
        {"webgl": "unavailable_software", "canvas_2d": "unavailable_software", "renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device))"},
        # A status read as enabled is not enough where the renderer names SwiftShader.
        {"webgl": "enabled", "canvas_2d": "enabled", "renderer": "ANGLE (Google, SwiftShader Device)"},
        RuntimeError("Executable doesn't exist"),
    ],
)
def test_a_lane_reading_swiftshader_refuses_every_recording(monkeypatch, fresh_backend, vulkan):
    probed = _gpu(
        monkeypatch,
        {
            capture_rate.GPU_GL_ARGS: vulkan,
            capture_rate.GPU_EGL_ARGS: {"webgl": "unavailable_software", "canvas_2d": "unavailable_software", "renderer": "SwiftShader"},
        },
    )
    backend = asyncio.run(capture_rate.gl_backend())
    assert len(probed) == 2
    assert backend.refused and not backend.gpu
    assert backend.why.startswith("VIDEO_STUDIO_RECORDING_GL=gpu and Chromium found no GPU to draw WebGL on (ANGLE on Vulkan: ")
    assert "ANGLE on EGL: webgl unavailable_software, renderer SwiftShader" in backend.why
    assert capture_rate.recording_refused() == backend.why
    assert VideoTakeAdapter().claims_here is False
    # Decided once: a lane without its GPU stays refused, never probed again into a software recording.
    assert asyncio.run(capture_rate.gl_backend()) is backend and len(probed) == 2


@pytest.mark.parametrize("mode", ["auto", "software", "AUTO ", "unknown"])
def test_the_other_modes_claim_recordings_as_before(monkeypatch, fresh_backend, mode):
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", mode)
    assert capture_rate.gl_mode() in ("auto", "software")
    assert capture_rate.recording_refused() is None
    assert VideoTakeAdapter().claims_here is True


def test_a_worker_that_records_nothing_still_claims_nothing(monkeypatch, fresh_backend):
    _gpu(monkeypatch, {capture_rate.GPU_GL_ARGS: {"webgl": "enabled", "canvas_2d": "enabled", "renderer": "NVIDIA"}})
    asyncio.run(capture_rate.gl_backend())
    monkeypatch.setattr(capture_rate.settings, "video_studio_take_concurrency", 0)
    assert VideoTakeAdapter().claims_here is False
