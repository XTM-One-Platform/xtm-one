"""A refused recording lane's reason is read whole and names where NVIDIA EGL stops (second round).

With the NVIDIA EGL vendor pinned, the lane's ANGLE-on-EGL launch fell back to SwiftShader instead of Mesa
llvmpipe: NVIDIA's EGL never gave ANGLE a display, before or after the pin. The container evidence that should have
said why was cut at 400 characters ("/dev: nvidia-m") in every place a queued take shows it. What is proven here:

* a queued take carries a refusing worker's reason whole, its container evidence included;
* each probe of the refusal says the GPUs Chromium's GPU process saw (``SystemInfo`` ``gpu.devices``);
* the container evidence ends with what libEGL answers under the lane's own EGL environment, asked in a child process
  run with that environment (the NVIDIA vendor pinned, the surfaceless platform);
* that check runs for real and answers one line, whatever libEGL the machine holds.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import worker_presence
from app.services.video_studio import capture_rate, recording_lane

SWIFTSHADER = "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)"


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setattr(capture_rate, "_BACKEND", [])
    monkeypatch.setattr(capture_rate.settings, "video_studio_take_concurrency", 1)
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "gpu")


def test_a_queued_take_carries_the_refusal_whole(monkeypatch):
    why = "VIDEO_STUDIO_RECORDING_GL=gpu and Chromium found no GPU to draw WebGL on (" + "x" * 600 + "). In the container: /dev: nvidia-modeset, nvidia1, nvidiactl"
    record = {
        "worker_id": "w-rec",
        "host": "copilot-worker-recording-7f9c",
        "version": "1.2.3",
        "state": worker_presence.STATE_RUNNING,
        "claims": ["video_studio_render"],
        "recording_gl": {"gpu": False, "refused": why, "renderer": SWIFTSHADER},
    }

    async def present(*, at_most: int = 256):
        return [record]

    monkeypatch.setattr(worker_presence, "present", present)
    (said,) = asyncio.run(recording_lane.refusing_workers())
    assert said.endswith("In the container: /dev: nvidia-modeset, nvidia1, nvidiactl")
    assert len(why) > 400 and recording_lane.REFUSED_CHARS >= 4000


def test_chromiums_gpu_devices_are_read_from_its_system_info():
    class _Session:
        async def send(self, method):
            return {
                "gpu": {
                    "featureStatus": {"webgl": "unavailable_software", "2d_canvas": "unavailable_software"},
                    "devices": [
                        {"vendorId": 0x10DE, "deviceId": 0x1DF6, "deviceString": "Tesla V100S", "driverVendor": "NVIDIA", "driverVersion": "550.163.01"},
                        {"vendorId": 0, "deviceId": 0},
                    ],
                }
            }

        async def detach(self):
            return None

    class _Browser:
        async def new_browser_cdp_session(self):
            return _Session()

    status = asyncio.run(capture_rate.gl_status(_Browser()))
    assert status["devices"] == "0x10de:0x1df6 Tesla V100S (driver NVIDIA 550.163.01), 0x0000:0x0000"
    assert capture_rate.gpu_devices({}) == ""


def test_each_probe_of_a_refusal_says_the_gpus_chromium_saw(monkeypatch, lane):
    statuses = {
        capture_rate.GPU_GL_ARGS: {"webgl": "unavailable_software", "renderer": SWIFTSHADER, "devices": "0x0000:0x0000"},
        capture_rate.GPU_EGL_ARGS: {"webgl": "unavailable_software", "renderer": SWIFTSHADER},
    }

    async def probe(args):
        return dict(statuses[args])

    async def evidence():
        return "/dev: nvidia1, nvidiactl"

    monkeypatch.setattr(capture_rate, "_probe", probe)
    monkeypatch.setattr(capture_rate, "container_gl_evidence", evidence)
    backend = asyncio.run(capture_rate.gl_backend())
    assert backend.refused
    assert f"ANGLE on Vulkan: webgl unavailable_software, renderer {SWIFTSHADER}, GPU seen by Chromium: 0x0000:0x0000" in backend.why
    assert f"ANGLE on EGL: webgl unavailable_software, renderer {SWIFTSHADER}, GPU seen by Chromium: none" in backend.why


def test_the_egl_check_runs_in_a_child_under_the_lanes_egl_environment(monkeypatch):
    seen: dict = {}

    async def child(code, *args, env=None, ok):
        seen.update(code=code, env=env, ok=ok)
        return "client extensions EXT_device_enumeration+ EXT_platform_device+ MESA_platform_surfaceless-, devices 1"

    monkeypatch.setattr(capture_rate, "_child", child)
    said = asyncio.run(capture_rate._egl_check())
    assert said.endswith("devices 1")
    assert seen["code"] is capture_rate._EGL_PY and seen["ok"] is None
    assert seen["env"]["__EGL_VENDOR_LIBRARY_FILENAMES"] == capture_rate.NVIDIA_EGL_VENDOR_FILE
    assert seen["env"]["EGL_PLATFORM"] == "surfaceless"


def test_the_egl_check_answers_one_line_on_any_machine():
    # No NVIDIA here: libEGL is absent (the loader's error) or answers with its own vendor and errors. One line either way.
    said = asyncio.run(capture_rate._egl_check())
    assert said and "\n" not in said and len(said) <= 300
    assert said.startswith(("libEGL.so.1: ", "client extensions ")) or said.startswith(("exit ", "hung over "))


def test_the_container_evidence_ends_with_the_egl_check(monkeypatch):
    monkeypatch.setattr(capture_rate, "GL_VENDOR_DIRS", ())
    monkeypatch.setattr(capture_rate, "_listed", lambda d, p="": [])

    async def load(name):
        return "loads"

    async def egl():
        raise RuntimeError("boom")

    monkeypatch.setattr(capture_rate, "_load_library", load)
    monkeypatch.setattr(capture_rate, "_egl_check", egl)
    assert asyncio.run(capture_rate.container_gl_evidence()).endswith("EGL under the lane's environment: not checked (boom)")
