"""A recorded ``scroll`` moves the same distance every frame of its take, at 60 and at 30 fps, in a real Chromium.

A 4000 px page under a fixed header, scrolled 1200 px over 2 s by one ``scroll`` step in a 1600x900 window at pixel
ratio 1, recorded at the take's fps and paced: on the take's own frames, the vertical shift from one frame to the next
- read by phase correlation on a fixed crop under the header - has a stdev under 25 % of its mean across the step, no
frame repeated and none past twice the mean, and the step keeps its cadence, not stepping.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and sets
``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import os
import statistics
import tempfile
from pathlib import Path

import pytest

from app.services.browser_profiles.browser import TargetPolicy
from app.services.video_studio import capture, scroll_pacing, spec
from tests.test_video_studio_scroll_cadence import assert_steady, shifts
from tests.test_video_studio_scroll_pacing_chromium import site  # noqa: F401 - the page's server, a fixture

pytestmark = pytest.mark.chromium

#: The step: 1200 px over 2 s; its first and last tenths of a second are left out of the measure (the wheel's first
#: turn painted, the page coming to rest).
BY, SECONDS, EDGE = 1200, 2.0, 0.1
#: The crop the shift is read on: under the fixed header, clear of the window's sides.
CROP = (slice(140, 840), slice(200, 1400))


async def _record(start_url: str, workdir: str, fps: int) -> capture.CaptureResult:
    script = spec.parse_take_script(
        spec.validate_take_script(
            {
                "viewport": {"width": 1600, "height": 900},
                "pixel_ratio": 1,
                "fps": fps,
                "settle_ms": 0,
                "end_hold_ms": 400,
                "steps": [{"action": "wait", "seconds": 0.3}, {"action": "scroll", "by": BY, "seconds": SECONDS}],
            }
        )
    )
    try:
        return await capture.record_take(script, workdir=workdir, profile=None, start_url=start_url, policy=TargetPolicy(), max_seconds=60)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        raise


@pytest.mark.parametrize("fps", [60, 30])
async def test_a_recorded_1200_px_scroll_over_2_s_moves_the_same_distance_every_frame(site, fps):  # noqa: F811
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(site, workdir, fps)
        paced = scroll_pacing.pace(result.frames, result.events, workdir=workdir, fps=fps, pixel_ratio=result.pixel_ratio, painted=result.painted)
        event = next(event for event in result.events if event["action"] == "scroll")
        [scroll] = [s for s in event["scrolls"] if s.get("why") != scroll_pacing.DID_NOT_MOVE]
        (began, _nothing), *_turned, (ended, _all) = scroll["wheel"]
        assert ended - began == pytest.approx(SECONDS - 1 / fps, abs=0.2), "the wheel turned once a frame over the step's 2 s"
        moving = [(t, name) for t, name in paced.frames if began + EDGE < t < ended - EDGE]
        assert len(moving) >= 0.9 * fps * (ended - began - 2 * EDGE) - 1, scroll
        moved = shifts(Path(workdir), moving, CROP)
        assert_steady(moved)
        assert statistics.fmean(moved) == pytest.approx(BY / SECONDS / fps, rel=0.2), moved
        # The step keeps its cadence, said as steady, the action log saying nothing stepping.
        steady = event["scroll_cadence"]
        assert not steady["stepping"] and scroll_pacing.cadence_words(steady) == "", scroll
