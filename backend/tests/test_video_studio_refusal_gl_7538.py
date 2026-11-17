"""A take refused by the capture-rate warm-up names the GL its warm-up drew on.

On a recording lane (``VIDEO_STUDIO_RECORDING_GL=gpu``) a refusal used to say the painted size, the fps measured and the
worker shape, never whether its Chromium drew on the GPU or on SwiftShader: that lived in the worker's start log alone.
The refusal now ends with the backend, the WebGL renderer its probe read and the mode; a take that records is unchanged.
"""

from __future__ import annotations

import contextlib

import pytest

from app.services.video_studio import capture_rate, spec

RENDERER = "ANGLE (NVIDIA, Vulkan 1.3.277 (NVIDIA L4 (0x000027B8)), NVIDIA-550.90.7.0)"


def _thirty() -> spec.TakeScript:
    return spec.parse_take_script(
        spec.validate_take_script(
            {"steps": [{"action": "wait", "seconds": 1}], "viewport": {"width": 1920, "height": 1080}, "pixel_ratio": 1.5, "fps": 30}
        )
    )


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(capture_rate, "_TABLE", {})
    monkeypatch.setattr(capture_rate, "_BACKEND", [])


def _measuring(monkeypatch, fps: float) -> None:
    """A worker whose warm-up captures every size at *fps*, without a browser."""

    @contextlib.asynccontextmanager
    async def warm_ups(_script):
        async def measure(_window, _at):
            return fps

        yield measure

    monkeypatch.setattr(capture_rate, "warm_ups", warm_ups)
    monkeypatch.setattr(capture_rate, "worker_shape", lambda: (4, 16384))


async def test_a_refusal_on_a_gpu_lane_names_gpu_gl_its_renderer_and_the_mode(monkeypatch):
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "gpu")
    capture_rate._BACKEND[:] = [capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True, renderer=RENDERER)]
    _measuring(monkeypatch, 10.0)

    planned = await capture_rate.plan(_thirty(), (1920, 1080), 1.5)

    assert planned.refused.startswith("Refused before recording: this worker captures 2880x1620 frames at 10 fps")
    assert " Its warm-ups captured JPEG screencast frames at quality 95; smaller sizes measured: " in planned.refused
    assert planned.refused.endswith(
        f" Its warm-up drew on GPU GL, WebGL renderer {RENDERER} (VIDEO_STUDIO_RECORDING_GL=gpu)."
    )


async def test_a_refusal_on_software_gl_says_why(monkeypatch):
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "auto")
    capture_rate._BACKEND[:] = [
        capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False, why="Chromium found no GPU to draw WebGL on: webgl unavailable_software")
    ]
    _measuring(monkeypatch, 10.0)

    planned = await capture_rate.plan(_thirty(), (1920, 1080), 1.5)

    assert planned.refused.endswith(
        " Its warm-up drew on software GL (Chromium found no GPU to draw WebGL on: webgl unavailable_software)"
        " (VIDEO_STUDIO_RECORDING_GL=auto)."
    )


async def test_a_take_the_worker_sustains_says_nothing_of_its_gl(monkeypatch):
    capture_rate._BACKEND[:] = [capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True, renderer=RENDERER)]
    _measuring(monkeypatch, 60.0)

    planned = await capture_rate.plan(_thirty(), (1920, 1080), 1.5)

    assert planned.refused is None and planned.note is None


def test_the_probe_words_list_each_size_measured():
    measured = {"fps": 8.0, "size": [2880, 1620], "probes": [[1280, 720, 16.0], [960, 540, 22.5]]}
    assert capture_rate.refusal_probe_words(measured) == (
        " Its warm-ups captured JPEG screencast frames at quality 95; smaller sizes measured: "
        "1280x720 at 16 fps, 960x540 at 22.5 fps."
    )
    assert capture_rate.refusal_probe_words({"fps": 8.0}) == " Its warm-ups captured JPEG screencast frames at quality 95."


def test_the_words_without_a_renderer(monkeypatch):
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "software")
    backend = capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False, why="VIDEO_STUDIO_RECORDING_GL=software")
    assert capture_rate.refusal_gl_words(backend) == " Its warm-up drew on software GL (VIDEO_STUDIO_RECORDING_GL=software)."
