"""The mixed video the render pipeline is measured on: every kind of scene a render composes, inserts included.

Shared by the 2160p page peak measurement (``test_video_studio_page_peak_chromium``) and the estimate held to a
render's measured time (``test_video_studio_estimate_chromium``). Its media are made by ffmpeg's test sources;
nothing is faked but the database: the plan is the render's (``timeline.plan``) and its screen inserts are matted
as a render mattes them (``render._matte_inserts``).

``build(directory, seconds=60)``: a title, a recording with a video insert on its monitor, a clip of a green
screen with a keyed video insert, a statement over a clip, a picture, a clip with a video and a picture insert,
a closing card - about *seconds* long with their crossfades.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.services.video_studio import compositor, render, themes, timeline
from app.services.video_studio.screen_insert import InsertSource, KeySpec
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

TAKE_ID = "00000000-0000-0000-0000-0000000062a1"
FILES = {
    "insert": "00000000-0000-0000-0000-0000000062a2",
    "green": "00000000-0000-0000-0000-0000000062a3",
    "keyed": "00000000-0000-0000-0000-0000000062a4",
    "backdrop": "00000000-0000-0000-0000-0000000062a5",
    "picture": "00000000-0000-0000-0000-0000000062a6",
    "shot": "00000000-0000-0000-0000-0000000062a7",
    "panel": "00000000-0000-0000-0000-0000000062a8",
    "logo": "00000000-0000-0000-0000-0000000062a9",
}
#: A monitor on the recording's desk, seen a little from the left.
MONITOR = [[0.30, 0.20], [0.80, 0.25], [0.78, 0.75], [0.32, 0.70]]
#: The green screen of the clip, as ffmpeg draws it (``drawbox``), and the quad on it.
GREEN_BOX = (576, 238, 806, 432)
GREEN = [[0.30, 0.22], [0.72, 0.22], [0.72, 0.62], [0.30, 0.62]]
#: Two surfaces of the last clip: a video on the left, a picture on the right.
LEFT = [[0.08, 0.18], [0.46, 0.20], [0.45, 0.62], [0.09, 0.60]]
RIGHT = [[0.54, 0.22], [0.92, 0.18], [0.91, 0.64], [0.55, 0.60]]


@dataclass
class Fixture:
    spec: object
    plan: object
    media: object
    #: ``(kind, path)`` of each file made, by its name.
    files: dict[str, Path]


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _lavfi(path: Path, source: str, *, seconds: float | None = None, frames: int | None = None) -> None:
    args = [ffmpeg() or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source]
    args += ["-t", str(seconds)] if seconds is not None else []
    args += ["-frames:v", str(frames)] if frames is not None else []
    if path.suffix == ".mp4":
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    subprocess.run([*args, str(path)], check=True, timeout=300)  # noqa: S603


def _scenes(seconds: float) -> list[dict]:
    # Seven scenes, six crossfades of 0.6 s: their lengths scaled to *seconds* in all.
    scale = seconds / 61.4
    length = {name: round(value * scale, 2) for name, value in {"open": 5, "claim": 9, "picture": 8, "close": 4}.items()}
    return [
        {"id": "f01-open", "type": "title", "title": "Every frame", "duration": max(1.0, length["open"])},
        {
            "id": "f02-console",
            "type": "recording",
            "take_id": TAKE_ID,
            "overlays": [
                {"type": "screen_insert", "source": {"file_id": FILES["insert"]}, "quad": MONITOR, "start": 0.5, "duration": round(13 * scale, 2)}
            ],
        },
        {
            "id": "f03-green",
            "type": "clip",
            "file_id": FILES["green"],
            "overlays": [
                {
                    "type": "screen_insert",
                    "source": {"file_id": FILES["keyed"]},
                    "quad": GREEN,
                    "key": {"color": "green"},
                    "duration": round(14 * scale, 2),
                }
            ],
        },
        {
            "id": "f04-claim",
            "type": "statement",
            "lines": ["Every alert", "*triaged* first"],
            "duration": max(1.0, length["claim"]),
            "backdrop": {"file_id": FILES["backdrop"]},
        },
        {"id": "f05-picture", "type": "image", "file_id": FILES["picture"], "duration": max(1.0, length["picture"])},
        {
            "id": "f06-panels",
            "type": "clip",
            "file_id": FILES["shot"],
            "overlays": [
                {"type": "screen_insert", "source": {"file_id": FILES["panel"]}, "quad": LEFT, "duration": round(10 * scale, 2)},
                {"type": "screen_insert", "source": {"file_id": FILES["logo"]}, "quad": RIGHT, "duration": round(10 * scale, 2)},
            ],
        },
        {"id": "f07-close", "type": "outro", "title": "Thanks", "duration": max(1.0, length["close"])},
    ]


def build(directory: Path, *, seconds: float = 60.0, fps: int = 60, scenes: tuple[str, ...] | None = None) -> Fixture:
    """The mixed video, its media made in *directory*, planned at *fps*; *scenes*, the ids of those it keeps (all by
    default): another mix of the same scenes."""
    scale = seconds / 61.4
    clip = lambda value: round(value * scale, 2)  # noqa: E731 - the scene lengths above, in the media
    files = {
        "take": directory / "take.mp4",
        "insert": directory / "insert.mp4",
        "green": directory / "green.mp4",
        "keyed": directory / "keyed.mp4",
        "backdrop": directory / "backdrop.mp4",
        "picture": directory / "picture.png",
        "shot": directory / "shot.mp4",
        "panel": directory / "panel.mp4",
        "logo": directory / "logo.png",
    }
    x, y, w, h = GREEN_BOX
    _lavfi(files["take"], "testsrc2=size=1600x900:rate=30", seconds=clip(15))
    _lavfi(files["insert"], "mandelbrot=size=1280x720:rate=24", seconds=clip(14))
    _lavfi(files["green"], f"testsrc2=size=1920x1080:rate=24,drawbox=x={x}:y={y}:w={w}:h={h}:color=0x00b140@1:t=fill", seconds=clip(14))
    _lavfi(files["keyed"], "life=size=1280x720:rate=24:mold=10:ratio=0.2:death_color=#203040", seconds=clip(14))
    _lavfi(files["backdrop"], "testsrc2=size=1920x1080:rate=24", seconds=clip(10))
    _lavfi(files["picture"], "gradients=size=1600x1000:nb_colors=4", frames=1)
    _lavfi(files["shot"], "smptehdbars=size=1920x1080:rate=24", seconds=clip(10))
    _lavfi(files["panel"], "testsrc=size=1280x720:rate=24", seconds=clip(10))
    _lavfi(files["logo"], "gradients=size=800x600:nb_colors=2", frames=1)
    raw = {
        "title": "Render measurement",
        "freestyle": True,
        "format": {"aspect_ratio": "16:9"},
        "captions": {"enabled": False},
        "default_transition": {"type": "crossfade", "duration": 0.6},
        "scenes": [scene for scene in _scenes(seconds) if scenes is None or scene["id"] in scenes],
    }
    spec = parse_video_spec(validate_video_spec(raw))
    media = render._Media()
    plan_media = media.plan
    plan_media.takes[TAKE_ID] = timeline.TakeMedia(
        take_id=TAKE_ID, duration=clip(15), css_width=1600, css_height=900, pixel_ratio=1.0, fps=30, frame_count=round(clip(15) * 30)
    )
    media.assets.sources["take/f02-console"] = compositor.FrameSource(str(files["take"]), 30.0)
    for scene_id, name in (("f03-green", "green"), ("f06-panels", "shot")):
        length = clip(14) if name == "green" else clip(10)
        plan_media.clips[scene_id] = timeline.ClipMedia(length, 1920, 1080, 24.0, round(length * 24))
        media.assets.sources[f"clip/{scene_id}"] = compositor.FrameSource(str(files[name]), 24.0)
    plan_media.backdrops["f04-claim"] = timeline.ClipMedia(clip(10), 1920, 1080, 24.0, round(clip(10) * 24))
    media.assets.sources["backdrop/f04-claim"] = compositor.FrameSource(str(files["backdrop"]), 24.0)
    plan_media.images["f05-picture"] = timeline.ImageMedia(1600, 1000)
    media.assets.images["image/f05-picture"] = str(files["picture"])
    plan_media.inserts_gathered = True
    for scene_id, index, name, quad, kind, key in (
        ("f02-console", 0, "insert", MONITOR, "video", None),
        ("f03-green", 0, "keyed", GREEN, "video", KeySpec("green")),
        ("f06-panels", 0, "panel", LEFT, "video", None),
        ("f06-panels", 1, "logo", RIGHT, "image", None),
    ):
        insert = timeline.insert_key(scene_id, index)
        if kind == "video":
            length = clip(14) if name != "panel" else clip(10)
            plan_media.inserts[insert] = timeline.ClipMedia(length, 1280, 720, 24.0, round(length * 24))
        else:
            plan_media.inserts[insert] = timeline.ImageMedia(800, 600)
        media.assets.inserts[insert] = InsertSource(
            str(files[name]), kind, tuple((a, b) for a, b in quad), fps=24.0, key=key, label=f"Scene {scene_id}: its screen insert {index}"
        )
    theme = themes.build_theme(mode=spec.theme.mode, background_style=spec.theme.background)
    plan = timeline.plan(spec, plan_media, fps=fps, theme=theme)
    return Fixture(spec=spec, plan=plan, media=media, files=files)


async def matte(fixture: Fixture, size: tuple[int, int]) -> None:
    """The fixture's screen inserts matted as a render mattes them, at the render's *size* (``render._matte_inserts``)."""
    await render._matte_inserts(fixture.plan.document, fixture.media, size, timeline.insert_sources(fixture.spec))
