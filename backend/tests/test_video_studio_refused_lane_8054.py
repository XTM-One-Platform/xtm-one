"""A recording lane that refuses its GL says so beyond its own log.

A worker under ``VIDEO_STUDIO_RECORDING_GL=gpu`` whose Chromium draws on no GPU (llvmpipe) claims no take
for the life of its process. Its queued takes read "0 recordings being made now" and failed on their deadline, the
reason only in the pod log. What is proven here:

* the lane's presence record carries its GL decision (``recording_gl``: refused and why, the renderer read);
* a queued take no running worker claims names each worker refusing recordings, and why, in its line and its dict;
* while a worker claims takes, nothing changes.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import worker_presence
from app.services.orchestration_adapters.video_studio import VideoTakeAdapter
from app.services.video_studio import capture_rate, jobs, recording_lane

LLVMPIPE = "ANGLE (Mesa, llvmpipe (LLVM 19.1.7 256 bits), OpenGL ES 3.2)"


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setattr(capture_rate, "_BACKEND", [])
    monkeypatch.setattr(capture_rate.settings, "video_studio_take_concurrency", 1)
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "gpu")


def test_a_refused_lane_says_its_gl_in_its_presence_record(lane):
    why = f"VIDEO_STUDIO_RECORDING_GL=gpu and Chromium found no GPU to draw WebGL on (ANGLE on EGL: renderer {LLVMPIPE})"
    capture_rate._BACKEND[:] = [
        capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False, why=why, refused=True, renderer=LLVMPIPE)
    ]
    adapter = VideoTakeAdapter()
    assert adapter.claims_here is False
    assert adapter.presence() == {"recording_gl": {"gpu": False, "refused": why, "renderer": LLVMPIPE}}


def test_a_lane_not_probed_yet_and_a_gpu_lane_say_so(lane):
    assert VideoTakeAdapter().presence()["recording_gl"] == {
        "gpu": False,
        "refused": "its GPU is not probed yet",
        "renderer": None,
    }
    nvidia = "ANGLE (NVIDIA, NVIDIA L4/PCIe/SSE2, OpenGL ES 3.2)"
    capture_rate._BACKEND[:] = [capture_rate.GlBackend(capture_rate.GPU_EGL_ARGS, gpu=True, renderer=nvidia)]
    assert VideoTakeAdapter().presence() == {
        "recording_gl": {"gpu": True, "refused": None, "renderer": nvidia},
        "recording_class": "l4",
        "recording_gpu": "L4",
    }


def test_a_worker_outside_the_gpu_mode_adds_nothing(monkeypatch):
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "auto")
    assert VideoTakeAdapter().presence() == {}


def _present(monkeypatch, records):
    async def present(*, at_most: int = 256):
        return records

    monkeypatch.setattr(worker_presence, "present", present)


REFUSING = {
    "worker_id": "w-rec",
    "host": "copilot-worker-recording-7f9c",
    "version": "1.2.3",
    "state": worker_presence.STATE_RUNNING,
    "claims": ["video_studio_render"],
    "recording_gl": {"gpu": False, "refused": "Chromium found no GPU to draw WebGL on (llvmpipe)", "renderer": LLVMPIPE},
}


def test_a_queued_take_no_worker_claims_names_the_workers_refusing_recordings(monkeypatch):
    other = {"worker_id": "w-1", "host": "copilot-worker-1", "state": worker_presence.STATE_RUNNING, "claims": ["video_studio_render"]}
    _present(monkeypatch, [REFUSING, other])
    refusing = asyncio.run(recording_lane.refusing_workers())
    assert refusing == (
        "copilot-worker-recording-7f9c (version 1.2.3) refuses recordings: Chromium found no GPU to draw WebGL on (llvmpipe)",
    )
    place = jobs.QueuePlace(running=0, ahead=0, per_worker=1, lane="recording", recordings=(), refusing=refusing)
    line = recording_lane.line(place)
    assert line.startswith("waiting for a worker in the recording lane, place 1 (nothing queued ahead of it), but no worker takes recordings now: ")
    assert "copilot-worker-recording-7f9c (version 1.2.3) refuses recordings: Chromium found no GPU" in line
    assert line.endswith("It fails on its deadline unless a recording lane draws on a GPU")
    assert place.as_dict()["recording_lane_refused"] == list(refusing)


def test_nothing_changes_while_a_worker_claims_takes(monkeypatch):
    claiming = {"worker_id": "w-2", "state": worker_presence.STATE_RUNNING, "claims": [jobs.TAKE_ADAPTER]}
    _present(monkeypatch, [REFUSING, claiming])
    assert asyncio.run(recording_lane.refusing_workers()) == ()
    _present(monkeypatch, None)
    assert asyncio.run(recording_lane.refusing_workers()) == ()
    place = jobs.QueuePlace(running=0, ahead=0, per_worker=1, lane="recording", recordings=())
    assert "recording_lane_refused" not in place.as_dict()
    assert recording_lane.line(place).startswith("waiting for a worker in the recording lane, place 1: 0 recordings")
