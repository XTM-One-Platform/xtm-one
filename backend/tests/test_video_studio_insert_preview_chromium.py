"""A scene preview of a clip with one static screen insert, at 1080p on 2 CPUs, for real.

A 10 s scene with one screen insert took about 12 minutes to preview at 1080p (4.26 s a frame on 2 CPUs): every page
redid the insert's whole pipeline on every frame, and the next preview of the same revision did it all again. Here
a title, a 10 s clip of a monitor with one static video insert on it - its key read on a green screen, or its
occlusion on a dark one - and a closing card, crossfaded, are previewed as ``render.run_render`` previews the clip
scene: its window planned at the standard preset (1080p30), its insert matted as a render mattes it, its frames drawn
by ``compositor.render_frames`` on two pages, the process held to two CPUs. Twice, the second preview in a working
directory of its own as the next preview of the same revision is:

* the second reads at least 90% of its insert frames back from the worker's disk (``insert_frames``), its hit rate
  logged - the contract, asserted on every runner;
* a frame of the insert scene takes a page under 2 s on the first preview, on the runner the bound was set on.

The 2 s is wall time on a shared 2-CPU hosted runner, whose speed varies with its load (2.10 s a frame on a
loaded runner, the code unchanged). So the runner is calibrated: a fixed draw (``_probe``, a 1280x720 picture warped
to 1080p and lit, as an insert frame is) on the same two pinned CPUs, before and after the first preview, the slower
of the two its speed around the preview; and the share of the two pinned CPUs the hypervisor took over the first
preview's own window (``steal`` in ``/proc/stat``), the contention the probes either side of it cannot see. The
slower of the two readings is the runner's speed. The first preview's cost a frame is read at the speed of the hosted runner
the bound holds on (``PROBE_SECONDS``, its median there): never looser on a runner as fast or faster (the ratio is held
to 1 or more), and a runner more than ``PROBE_UNFIT`` times slower skips the bound with its figures, the read-back
asserted before.

Minutes on a hosted runner: it runs with ``VIDEO_STUDIO_ULTRA_RENDER=1`` (the ``Video Studio ultra render`` workflow),
with ffmpeg and Playwright's Chromium - a failure, not a skip, under ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

import pytest

from app.services.builtin_tools.html_render import font_face_css
from app.services.video_studio import compositor, render, render_preflight, themes, timeline
from app.services.video_studio.screen_insert import InsertSource, KeySpec
from app.services.video_studio.spec import QUALITY_PRESETS, parse_video_spec, validate_video_spec

pytestmark = [pytest.mark.chromium, pytest.mark.video_ultra]

SIZE = (1920, 1080)
FOOTAGE_ID = "00000000-0000-0000-0000-0000000063a1"
SOURCE_ID = "00000000-0000-0000-0000-0000000063a2"
#: The monitor ffmpeg draws on the clip (``drawbox``), and the quad on it.
MONITOR = (576, 238, 806, 432)
QUAD = [[0.30, 0.22], [0.72, 0.22], [0.72, 0.62], [0.30, 0.62]]
CLIP_SECONDS = 10.0
#: The longest a frame of the insert scene may take a page on the first preview ...
FRAME_SECONDS = 2.0
#: ... and the least of its insert frames the second preview reads back drawn.
REUSED_SHARE = 0.9
#: What the fixed draw (``_probe``) takes on two pinned CPUs of the hosted runner the bound holds on (its median,
#: as the workflow logs it: 0.094 to 0.110 s over the nightly runs, 0.100 s their median) ...
PROBE_SECONDS = 0.10
#: ... and how many times slower a runner may run it before the bound is skipped there, its figures said.
PROBE_UNFIT = 2.0


@pytest.fixture(autouse=True)
def _asked():
    if os.environ.get("VIDEO_STUDIO_ULTRA_RENDER") != "1":
        pytest.skip("previews at 1080p take minutes: VIDEO_STUDIO_ULTRA_RENDER=1 runs them")


def _missing(what: str) -> None:
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"{what} is required here and is missing")
    pytest.skip(f"{what} is missing")


@contextlib.contextmanager
def _two_cpus():
    """This process - and the Chromium, encoders and decoders it starts - held to two CPUs, where it can be: the
    CPUs it holds, ``None`` where it cannot be held."""
    if not hasattr(os, "sched_setaffinity"):
        yield None
        return
    held = os.sched_getaffinity(0)
    pinned = set(sorted(held)[:2])
    os.sched_setaffinity(0, pinned)
    try:
        yield pinned
    finally:
        os.sched_setaffinity(0, held)


def _cpu_ticks(cpus: set[int] | None) -> tuple[int, int, int] | None:
    """``(all, steal, iowait)`` ticks of *cpus* since boot (``/proc/stat``), ``None`` where they cannot be read."""
    if not cpus:
        return None
    try:
        lines = Path("/proc/stat").read_text().splitlines()
    except OSError:
        return None
    total = steal = iowait = 0
    for line in lines:
        if not line.strip():
            continue
        name, *fields = line.split()
        if not name.startswith("cpu") or name == "cpu" or int(name[3:]) not in cpus or len(fields) < 8:
            continue
        # user nice system idle iowait irq softirq steal - guest and guest_nice are counted in user already.
        ticks = [int(value) for value in fields[:8]]
        total += sum(ticks)
        steal += ticks[7]
        iowait += ticks[4]
    return (total, steal, iowait) if total else None


def _shares(before: tuple[int, int, int] | None, after: tuple[int, int, int] | None) -> tuple[float, float] | None:
    """The steal and iowait shares of the pinned CPUs between two :func:`_cpu_ticks` readings."""
    if before is None or after is None or after[0] <= before[0]:
        return None
    span = after[0] - before[0]
    return (after[1] - before[1]) / span, (after[2] - before[2]) / span


def _probe(rounds: int = 7) -> float:
    """The median seconds of a fixed draw on the CPUs this process holds: a 1280x720 picture warped to 1920x1080 and
    lit, the work an insert frame is made of, never what the code under test does - the runner's speed, not the test's."""
    import numpy as np
    from PIL import Image

    picture = Image.fromarray(np.random.default_rng(7699).integers(0, 256, (720, 1280, 3), dtype=np.uint8))
    warp = (0.66, 0.0, -380.0, 0.0, 0.66, -155.0, 0.0, 0.0)

    def once() -> float:
        began = time.perf_counter()
        drawn = np.asarray(picture.transform(SIZE, Image.Transform.PERSPECTIVE, warp, Image.Resampling.BICUBIC), dtype=np.float32)
        np.clip(drawn * 0.85 + 12.0, 0, 255).astype(np.uint8)
        return time.perf_counter() - began

    once()
    return statistics.median(once() for _ in range(rounds))


def _lavfi(path: Path, source: str) -> None:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(CLIP_SECONDS)]
    subprocess.run([*args, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True, timeout=300)  # noqa: S603


def _preview(directory: Path, keyed: bool) -> tuple[dict, object, tuple[int, int], object]:
    """The clip scene's preview as ``render.run_render`` makes it: the window's plan, its media, its frame range."""
    x, y, w, h = MONITOR
    footage, source = directory / "monitor.mp4", directory / "insert.mp4"
    _lavfi(footage, f"testsrc2=size=1920x1080:rate=24,drawbox=x={x}:y={y}:w={w}:h={h}:color={'0x00b140' if keyed else '0x101418'}@1:t=fill")
    _lavfi(source, "mandelbrot=size=1280x720:rate=24")
    overlay = {"type": "screen_insert", "source": {"file_id": SOURCE_ID}, "quad": QUAD, "duration": CLIP_SECONDS}
    if keyed:
        overlay["key"] = {"color": "green"}
    raw = {
        "title": "Insert preview",
        "freestyle": True,
        "format": {"aspect_ratio": "16:9"},
        "captions": {"enabled": False},
        "default_transition": {"type": "crossfade", "duration": 0.6},
        "scenes": [
            {"id": "p01-open", "type": "title", "title": "Before", "duration": 2},
            {"id": "p02-screen", "type": "clip", "file_id": FOOTAGE_ID, "overlays": [overlay]},
            {"id": "p03-close", "type": "outro", "title": "After", "duration": 2},
        ],
    }
    spec = parse_video_spec(validate_video_spec(raw))
    media = render._Media()
    frames = round(CLIP_SECONDS * 24)
    media.plan.clips["p02-screen"] = timeline.ClipMedia(CLIP_SECONDS, 1920, 1080, 24.0, frames)
    media.assets.sources["clip/p02-screen"] = compositor.FrameSource(str(footage), 24.0)
    media.plan.inserts_gathered = True
    key = timeline.insert_key("p02-screen", 0)
    media.plan.inserts[key] = timeline.ClipMedia(CLIP_SECONDS, 1280, 720, 24.0, frames)
    media.assets.inserts[key] = InsertSource(
        str(source), "video", tuple((a, b) for a, b in QUAD), fps=24.0, key=KeySpec("green") if keyed else None, label="Scene p02-screen: its screen insert 0"
    )
    theme = themes.build_theme(mode=spec.theme.mode, background_style=spec.theme.background)
    plan = timeline.plan(spec, media.plan, fps=QUALITY_PRESETS["standard"].fps, theme=theme)
    times = render.window_times(plan.document, 1)
    fps = int(plan.document["fps"])
    frame_range = (math.floor(times[0] * fps), min(int(plan.document["frames"]), math.ceil(times[1] * fps)))
    return plan.document, media, frame_range, spec


@pytest.mark.parametrize("keyed", [False, True], ids=["occlusion", "key"])
async def test_an_insert_scene_previews_under_2_s_a_frame_and_its_next_preview_reads_its_frames_back(tmp_path, keyed):
    if not shutil.which("ffmpeg"):
        _missing("ffmpeg")
    document, media, frame_range, spec = _preview(tmp_path, keyed)
    await render._matte_inserts(document, media, SIZE, timeline.insert_sources(spec))
    preset = QUALITY_PRESETS["standard"]
    drawn: list[compositor.DrawProgress] = []
    walls: list[float] = []
    probes: list[float] = []
    window: tuple[float, float] | None = None
    with _two_cpus() as pinned:
        probes.append(_probe())
        for attempt in ("first", "second"):
            if attempt == "second":
                probes.append(_probe())
            tracker = compositor.DrawProgress()
            ticks = _cpu_ticks(pinned)
            began = time.monotonic()
            try:
                await compositor.render_frames(
                    document,
                    media.assets,
                    preset=preset,
                    output_size=SIZE,
                    workdir=str(tmp_path / attempt),
                    font_css=font_face_css(),
                    pages=2,
                    tracker=tracker,
                    frame_range=frame_range,
                    audits={},
                    scene_classes=render_preflight.scene_classes(spec),
                )
            except Exception as exc:
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
                    _missing(f"Playwright's Chromium: {str(exc)[:200]}")
                raise
            walls.append(time.monotonic() - began)
            if attempt == "first":
                window = _shares(ticks, _cpu_ticks(pinned))
            drawn.append(tracker)
    first, second = drawn
    name = "clip+insert"
    per_frame = first.scene_seconds[name] / first.scene_frames[name]
    again = second.scene_seconds[name] / second.scene_frames[name]
    inserts = second.insert_frames()
    stolen, waited = window if window is not None else (0.0, 0.0)
    # The runner's speed during the first preview: the slower of the probe's (read just before and after it) and the
    # CPU time the hypervisor left the two pinned CPUs over its own window - never above 1, never looser.
    slower = max(1.0, max(probes) / PROBE_SECONDS, 1.0 / max(1e-6, 1.0 - stolen))
    report = (
        f"{'keyed' if keyed else 'occlusion'} insert, {frame_range[1] - frame_range[0]} frames at 1920x1080 on 2 pages "
        f"({'2 CPUs' if hasattr(os, 'sched_setaffinity') else 'unpinned'}): the first preview in {walls[0]:.0f} s, "
        f"{per_frame:.2f} s a frame of the insert scene a page, its insert frames {first.insert_frames()}; the second "
        f"in {walls[1]:.0f} s, {again:.2f} s a frame, its insert frames {inserts} - "
        f"{100 * (inserts or {}).get('share', 0):.0f}% read back from the worker's disk; the runner's probe "
        f"{' then '.join(f'{p:.3f}' for p in probes)} s against {PROBE_SECONDS:.3f} s, over the first preview "
        f"{'its CPUs unread' if window is None else f'{100 * stolen:.1f}% of its CPUs stolen, {100 * waited:.1f}% waiting on I/O'}, "
        f"{slower:.2f}x the reference: "
        f"{per_frame / slower:.2f} s a frame at its speed"
    )
    logging.getLogger(__name__).warning("%s", report)
    assert inserts is not None and inserts["share"] >= REUSED_SHARE, report
    if slower > PROBE_UNFIT:
        pytest.skip(f"the runner ran the probe {slower:.2f}x slower than the reference, past {PROBE_UNFIT}x: the {FRAME_SECONDS} s bound is not read here. {report}")
    assert per_frame / slower < FRAME_SECONDS, report
