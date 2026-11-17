"""A render's memory follows what is on screen, never the length of the video, in a real Chromium.

A synthetic 180 s timeline - ten clip scenes decoded from mp4, six statements
over a video backdrop, a crossfade between every two scenes, captions burned in
- is drawn and encoded by ``compositor.render_frames`` while the memory of this
process and everything it started (Chromium, the ffmpeg encoders and decoders)
is sampled. Every clip a page passed kept its decoder open to the
end of the page's chunk and the page kept every scene it built, so the memory
climbed with each scene; now it must stay under a fixed ceiling, the last third
of the frames holding no more than the middle one (Chromium warms up over the
first), with at most two decoders open on a page. Measured on this timeline:
before, the ffmpeg processes alone went from 330 MiB to 870 MiB and the tree
peaked near 1.9 GiB over this process; now ffmpeg stays near 110 MiB and the
tree near 1.3 GiB.

The clips are drawn small (320x180 at 10 frames a second) so the test runs in
about a minute; what it proves is the shape of the curve, which the output size
scales but does not bend. Needs ffmpeg and Playwright's Chromium: skipped
without them, a failure under ``REQUIRE_CHROMIUM=1`` (the ``Browser tool
(Chromium)`` CI job, which installs both).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import uuid

import pytest

from app.services.builtin_tools.html_render import font_face_css
from app.services.video_studio import compositor, encoder, resources, themes, timeline
from app.services.video_studio.spec import QUALITY_PRESETS, parse_video_spec, validate_video_spec

pytestmark = pytest.mark.chromium

_MIB = 1 << 20
#: What the render's own processes may add to this one, at 320x180 with two pages.
CEILING = 1600 * _MIB
#: How much more the last third of the frames may hold than the middle one.
GROWTH = 128 * _MIB
_WORDS = "Every scene of this synthetic video says one short sentence, so its captions are burned in."


def _missing(what: str) -> None:
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"{what} is required here and is not installed")
    pytest.skip(f"{what} is not installed")


def _clip(path: str, seconds: float) -> None:
    subprocess.run(  # noqa: S603
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24",
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            path,
        ],
        check=True,
        timeout=120,
    )


def _timeline(clip_seconds: float, statement_seconds: float):
    scenes: list[dict] = [{"id": "open", "type": "title", "title": "Synthetic", "duration": 3}]
    for index in range(10):
        scenes.append({"id": f"clip{index}", "type": "clip", "file_id": str(uuid.uuid4()), "narration": _WORDS})
        if index < 6:
            scenes.append(
                {
                    "id": f"words{index}",
                    "type": "statement",
                    "lines": [f"Statement {index}", "over a moving *backdrop*"],
                    "duration": statement_seconds,
                    "backdrop": {"file_id": str(uuid.uuid4())},
                    "narration": _WORDS,
                }
            )
    scenes.append({"id": "close", "type": "outro", "title": "The end", "duration": 3})
    return parse_video_spec(
        validate_video_spec(
            {
                "title": "Synthetic",
                "freestyle": True,
                "voiceover": {"enabled": True},
                "captions": {"enabled": True},
                "default_transition": {"type": "crossfade", "duration": 0.8},
                "scenes": scenes,
            }
        )
    )


class _Sampler(threading.Thread):
    """``(frames drawn, memory)`` of this process tree, every quarter second."""

    def __init__(self, tracker: compositor.DrawProgress) -> None:
        super().__init__(daemon=True)
        self.tracker = tracker
        self.samples: list[tuple[int, int]] = []
        self.done = threading.Event()

    def run(self) -> None:
        while not self.done.wait(0.25):
            memory = resources.tree_memory(os.getpid())
            if memory is not None:
                self.samples.append((self.tracker.done, memory))


async def test_a_180_second_clip_heavy_timeline_renders_under_a_fixed_memory_ceiling(tmp_path):
    if not encoder.encoder_available():
        _missing("ffmpeg")
    if resources.tree_memory(os.getpid()) is None:
        pytest.skip("no /proc to measure memory with")
    clip_seconds, statement_seconds = 14.0, 8.0
    clip_path, backdrop_path = str(tmp_path / "clip.mp4"), str(tmp_path / "backdrop.mp4")
    _clip(clip_path, clip_seconds)
    _clip(backdrop_path, statement_seconds + 2)
    spec = _timeline(clip_seconds, statement_seconds)
    media, assets = timeline.PlanMedia(), compositor.CompositorAssets()
    for scene in spec.scenes:
        if scene.type == "clip":
            media.clips[scene.id] = timeline.ClipMedia(clip_seconds, 640, 360, 24.0, int(clip_seconds * 24))
            assets.sources[f"clip/{scene.id}"] = compositor.FrameSource(clip_path, 24.0)
        elif scene.type == "statement":
            seconds = statement_seconds + 2
            media.backdrops[scene.id] = timeline.ClipMedia(seconds, 640, 360, 24.0, int(seconds * 24))
            assets.sources[f"backdrop/{scene.id}"] = compositor.FrameSource(backdrop_path, 24.0)
    theme = themes.build_theme(mode="dark", background_style=spec.theme.background)
    plan = timeline.plan(spec, media, fps=10, theme=theme)
    assert plan.duration >= 180 and plan.document["captions"]["cues"]
    assert compositor.decoder_budget(plan.document) == 2

    tracker = compositor.DrawProgress()
    sampler = _Sampler(tracker)
    baseline = resources.tree_memory(os.getpid()) or 0
    sampler.start()
    try:
        video = await compositor.render_frames(
            plan.document,
            assets,
            preset=QUALITY_PRESETS["draft"],
            output_size=(320, 180),
            workdir=str(tmp_path),
            font_css=font_face_css(),
            pages=2,
            tracker=tracker,
        )
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        _missing("Playwright's Chromium")
    finally:
        sampler.done.set()
        sampler.join()

    total = int(plan.document["frames"])
    assert tracker.done == total
    assert tracker.peak_open_decoders <= 2
    assert (await encoder.probe(video)).duration == pytest.approx(plan.duration, abs=0.5)
    drawing = [(done, memory - baseline) for done, memory in sampler.samples if 0 < done < total]
    assert len(drawing) > 30, "too few samples to judge the curve"
    peak = max(memory for _done, memory in drawing)
    middle = max(memory for done, memory in drawing if total / 3 <= done < 2 * total / 3)
    last = max(memory for done, memory in drawing if done >= 2 * total / 3)
    assert peak <= CEILING, f"the render's processes reached {peak / _MIB:.0f} MiB"
    assert last - middle <= GROWTH, (
        f"memory grew with the timeline: {middle / _MIB:.0f} MiB in the middle third, {last / _MIB:.0f} MiB in the last"
    )
