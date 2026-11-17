"""A recording lane's EGL launch asks GLVND for the NVIDIA vendor alone, on the surfaceless platform.

On a GPU node whose host and container both carry the NVIDIA graphics libraries and both vendor files (10_nvidia.json
and 50_mesa.json), the lane's ANGLE-on-EGL launch read Mesa's llvmpipe: GLVND tried every vendor file and Mesa answered
where the NVIDIA one did not. The EGL launch now names the NVIDIA vendor file and the surfaceless platform in its
environment (the probe, the warm-up and the recording alike); every other launch keeps the worker's environment.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import capture_rate


def test_the_egl_launch_names_the_nvidia_vendor_on_the_surfaceless_platform(monkeypatch):
    monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "compute,utility,graphics")
    env = capture_rate.launch_env(capture_rate.GPU_EGL_ARGS)
    assert env is not None
    assert env["__EGL_VENDOR_LIBRARY_FILENAMES"] == "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
    assert env["EGL_PLATFORM"] == "surfaceless"
    # The worker's own environment is kept (PLAYWRIGHT_BROWSERS_PATH, the toolkit's variables).
    assert env["NVIDIA_DRIVER_CAPABILITIES"] == "compute,utility,graphics"


@pytest.mark.parametrize("args", ["GPU_GL_ARGS", "SOFTWARE_GL_ARGS"])
def test_every_other_launch_keeps_the_workers_environment(args):
    assert capture_rate.launch_env(getattr(capture_rate, args)) is None


def test_the_probe_launches_chromium_with_that_environment(monkeypatch):
    import asyncio
    import sys
    import types

    launched: list = []

    class _Page:
        async def evaluate(self, js):
            return "ANGLE (NVIDIA, Tesla V100S-PCIE-32GB/PCIe/SSE2, OpenGL ES 3.2)"

    class _Context:
        async def new_page(self):
            return _Page()

    class _Browser:
        async def new_context(self, **kwargs):
            return _Context()

        async def close(self):
            return None

    class _Chromium:
        async def launch(self, **kwargs):
            launched.append(kwargs)
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake = types.ModuleType("playwright.async_api")
    fake.async_playwright = lambda: _Playwright()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake)

    async def status(browser):
        return {"webgl": "enabled", "canvas_2d": "enabled"}

    monkeypatch.setattr(capture_rate, "gl_status", status)
    asyncio.run(capture_rate._probe(capture_rate.GPU_EGL_ARGS))
    asyncio.run(capture_rate._probe(capture_rate.GPU_GL_ARGS))
    assert launched[0]["env"]["__EGL_VENDOR_LIBRARY_FILENAMES"] == capture_rate.NVIDIA_EGL_VENDOR_FILE
    assert launched[1]["env"] is None
