"""The node-locate probe never holds the recording's screencast back (#11382).

A take's pointer step locating a node by its highlight read the highlight inline on the event loop that receives and
acknowledges the screencast's frames, and fitted each run of link pixels with ``np.cov`` (BLAS, a thread pool sized to
the node's cores): the step painted at 15 fps on a 16-CPU worker busy 2.0 CPUs, 1.7 s held back by its quota.
"""

from __future__ import annotations

import asyncio
import io
import threading

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import node_locate
from app.services.video_studio.node_locate import Entry


@pytest.mark.parametrize("seed", range(6))
def test_the_principal_axes_match_the_covariance_eigen_decomposition(seed):
    rng = np.random.default_rng(seed)
    angle = rng.uniform(0, np.pi)
    t = rng.uniform(-40, 40, 300)
    xs = np.cos(angle) * t + rng.normal(0, 0.6, t.size)
    ys = np.sin(angle) * t + rng.normal(0, 0.6, t.size)
    dx, dy = xs - xs.mean(), ys - ys.mean()
    values, vectors = np.linalg.eigh(np.cov(np.vstack([dx, dy])))
    across, along, ux, uy = node_locate.principal_axes(dx, dy)
    assert across == pytest.approx(float(values[0]), rel=1e-9, abs=1e-9)
    assert along == pytest.approx(float(values[1]), rel=1e-9)
    # The same direction, up to its sign.
    assert abs(ux * float(vectors[0, 1]) + uy * float(vectors[1, 1])) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize(
    ("dx", "dy", "expected"), [([-1.0, 1.0], [0.0, 0.0], (1.0, 0.0)), ([0.0, 0.0], [-2.0, 2.0], (0.0, 1.0))]
)
def test_an_axis_aligned_run_has_its_axis_for_direction(dx, dy, expected):
    across, along, ux, uy = node_locate.principal_axes(np.array(dx), np.array(dy))
    assert across == pytest.approx(0.0)
    assert along > 0
    assert (abs(ux), abs(uy)) == pytest.approx(expected)


def test_a_single_pixel_has_no_axis():
    assert node_locate.principal_axes(np.array([0.0]), np.array([0.0]))[1] == 0.0


def test_the_link_runs_are_fitted_without_a_covariance_call(monkeypatch):
    crop = np.zeros((200, 200), dtype=bool)
    crop[150, 10:190] = True
    crop[10:120, 50] = True
    monkeypatch.setattr(np, "cov", lambda *_a, **_k: pytest.fail("np.cov wakes the BLAS pool during a take"))
    lines = node_locate._segments(crop, 1.0)
    assert lines
    assert all(abs(ux) == pytest.approx(1.0) or abs(uy) == pytest.approx(1.0) for _cx, _cy, ux, uy, _l in lines)


class _Page:
    keyboard = None

    async def evaluate(self, script: str, args=None):
        if script is node_locate._FOCUS_JS:
            return {"focused": True, "active": {"pos": -1, "text": ""}, "count": 1}
        return None

    async def screenshot(self, **_kwargs) -> bytes:
        out = io.BytesIO()
        Image.new("RGB", (60, 40), (20, 20, 20)).save(out, "PNG")
        return out.getvalue()


async def test_the_highlight_is_read_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    read_on: list[int] = []

    def read(*_args, **_kwargs):
        read_on.append(threading.get_ident())
        return "nothing changed on the canvas"

    monkeypatch.setattr(node_locate, "highlight_of", read)
    grey = np.full((40, 60), 20, dtype=np.uint8)
    before = node_locate._Shot((0.0, 0.0, 60.0, 40.0), [], b"", 1.0, grey)
    entry = Entry("City Paris, 1 relationships", list=0, index=0, count=1, pos=0, child=0)
    found, _picture, _logged = await node_locate._highlight(_Page(), entry, before)
    assert found == "nothing changed on the canvas"
    assert read_on and read_on[0] != loop_thread
    await asyncio.sleep(0)
