"""A recording lane takes a Mesa CPU rasterizer for what it is: software GL.

Chromium reads ``enabled`` for WebGL drawn by ANGLE on Mesa's llvmpipe, so its feature status alone took a CPU
rasterizer for a GPU: a recording lane (``VIDEO_STUDIO_RECORDING_GL=gpu``) said "GPU GL" and measured the CPU. The
renderer the probe reads now decides with it: llvmpipe, softpipe, lavapipe and SwiftShader are software GL, and the
lane refuses saying what each launch read.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.orchestration_adapters.video_studio import VideoTakeAdapter
from app.services.video_studio import capture_rate

LLVMPIPE = "ANGLE (Mesa, llvmpipe (LLVM 19.1.7 256 bits), OpenGL ES 3.2)"
LAVAPIPE = "ANGLE (Mesa, Vulkan 1.3.255 (llvmpipe (LLVM 19.1.7, 256 bits) (0x0000C0DE)), lavapipe)"
NVIDIA = "ANGLE (NVIDIA, Vulkan 1.3.277 (NVIDIA L4 (0x000027B8)), NVIDIA-550.90.7.0)"


@pytest.fixture
def fresh_backend(monkeypatch):
    monkeypatch.setattr(capture_rate, "_BACKEND", [])
    monkeypatch.setattr(capture_rate.settings, "video_studio_take_concurrency", 1)


def _probes(monkeypatch, mode: str, statuses: dict) -> list:
    probed: list = []

    async def probe(args):
        probed.append(args)
        return dict(statuses[args])

    monkeypatch.setattr(capture_rate, "_probe", probe)
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", mode)
    return probed


@pytest.mark.parametrize(
    ("renderer", "found"),
    [
        (LLVMPIPE, "llvmpipe"),
        (LAVAPIPE, "llvmpipe"),
        ("Mesa softpipe", "softpipe"),
        ("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device))", "swiftshader"),
        (NVIDIA, None),
        (None, None),
    ],
)
def test_the_cpu_rasterizer_a_renderer_names(renderer, found):
    assert capture_rate.software_renderer(renderer) == found


def test_a_lane_on_llvmpipe_refuses_and_says_it_is_a_cpu_rasterizer(monkeypatch, fresh_backend):
    enabled = {"webgl": "enabled", "canvas_2d": "enabled"}
    probed = _probes(
        monkeypatch,
        "gpu",
        {
            capture_rate.GPU_GL_ARGS: {**enabled, "renderer": LAVAPIPE},
            capture_rate.GPU_EGL_ARGS: {**enabled, "renderer": LLVMPIPE},
        },
    )
    backend = asyncio.run(capture_rate.gl_backend())
    assert probed == [capture_rate.GPU_GL_ARGS, capture_rate.GPU_EGL_ARGS]
    assert backend.refused and not backend.gpu and backend.args == capture_rate.SOFTWARE_GL_ARGS
    assert backend.words().startswith("software GL (VIDEO_STUDIO_RECORDING_GL=gpu and Chromium found no GPU")
    assert f"ANGLE on EGL: webgl enabled, renderer {LLVMPIPE}, a CPU rasterizer (llvmpipe)" in backend.why
    assert "ANGLE on Vulkan: webgl enabled, renderer " in backend.why
    assert capture_rate.recording_refused() == backend.why
    assert VideoTakeAdapter().claims_here is False
    assert "GPU GL" not in capture_rate.refusal_gl_words(backend)


def test_a_lane_keeps_the_egl_launch_on_the_gpu_after_vulkan_read_llvmpipe(monkeypatch, fresh_backend):
    enabled = {"webgl": "enabled", "canvas_2d": "enabled"}
    _probes(
        monkeypatch,
        "gpu",
        {
            capture_rate.GPU_GL_ARGS: {**enabled, "renderer": LAVAPIPE},
            capture_rate.GPU_EGL_ARGS: {**enabled, "renderer": "ANGLE (NVIDIA, NVIDIA L4/PCIe/SSE2, OpenGL ES 3.2)"},
        },
    )
    backend = asyncio.run(capture_rate.gl_backend())
    assert backend.gpu and backend.args == capture_rate.GPU_EGL_ARGS and backend.words() == "GPU GL"


def test_auto_records_on_swiftshader_where_the_gpu_probe_read_llvmpipe(monkeypatch, fresh_backend):
    _probes(monkeypatch, "auto", {capture_rate.GPU_GL_ARGS: {"webgl": "enabled", "canvas_2d": "enabled", "renderer": LLVMPIPE}})
    backend = asyncio.run(capture_rate.gl_backend())
    assert not backend.gpu and not backend.refused and backend.args == capture_rate.SOFTWARE_GL_ARGS
    assert backend.why == f"Chromium found no GPU to draw WebGL on: webgl enabled, renderer {LLVMPIPE}, a CPU rasterizer (llvmpipe)"
    assert capture_rate.recording_refused() is None


def test_auto_keeps_a_hardware_renderer(monkeypatch, fresh_backend):
    _probes(monkeypatch, "auto", {capture_rate.GPU_GL_ARGS: {"webgl": "enabled", "canvas_2d": "enabled", "renderer": NVIDIA}})
    backend = asyncio.run(capture_rate.gl_backend())
    assert backend.gpu and backend.renderer == NVIDIA
