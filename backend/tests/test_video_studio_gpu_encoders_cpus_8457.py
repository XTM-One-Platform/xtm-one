"""#8457, slice 15: a GPU page encodes its canvas copies on as many threads as the worker's CPUs leave.

The production probe after slice 14 (take 20d277ef, a 4-CPU GPU worker) read "the encoders 95 ms (22 ms comparing)
... the worker busy 2.5 of its 4 CPUs, 19.0 s held back by its quota" over a 16 s take: three encoder threads,
encoding in bursts of about 19 copies a second, beside the page's own thread and the screencast's encode, ran the
container into its CPU quota while the canvas painted at 13 to 16 fps. A GPU page now keeps
``canvas_alone.GPU_SPARE_CPUS`` of the worker's CPUs for the page and the screencast, one at least.

Slice 16: two encoders on 4 CPUs (take 07a5505d) held the container back longer (23.3 s), left the page's paint at
12 to 13 fps and skipped 6 copies, every encoder behind: one CPU is spared, three encoders on 4 CPUs again.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import canvas_alone, capture_rate, resources


@pytest.mark.parametrize(
    ("cpus", "encoders"),
    [(1, 1), (2, 1), (3, 2), (4, 3), (5, 3), (8, 3), (64, 3)],
)
def test_the_encoders_leave_the_page_and_the_screencast_their_cpus(cpus, encoders):
    assert canvas_alone.gpu_encoders(cpus) == encoders


def test_a_gpu_backend_reads_the_worker_cpus(monkeypatch):
    monkeypatch.setattr(resources, "cpu_budget", lambda: 4)
    gpu = capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True)
    software = capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False)
    assert gpu.alone_encoders == 3
    assert software.alone_encoders == canvas_alone.ENCODERS == 1
    script = canvas_alone.tap_script(
        30, "(el) => 'c'", ratio=gpu.alone_ratio, quality=gpu.alone_quality, encoders=gpu.alone_encoders
    )
    assert f"ENCODERS = 3, FLIGHT = {canvas_alone.flight_depth(3)};" in script
