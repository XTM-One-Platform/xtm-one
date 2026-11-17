"""A screen insert drawn on a neighbour's footage in a scene preview, read as the scene's own preview reads it.

Preview `3a9b2466` of `s03-stat` in the brand film (20 Oct 2026) drew its
neighbour `s02-noise` without the screen insert its recording carries ("its
screen insert's recording is not ready", reworded since), which `s02-noise`'s own preview drew:
an insert's recording was read by a lighter check than a scene's footage and,
failing it, left out with a note.

* the window of `s03-stat` reads the insert of `s02-noise` through the render's
  own references and media (``render._read_references``, ``_gather_media``):
  the recording is the one rule of a scene's footage (``_ready_take``) and
  the file the footage uses (``_take_file``), downloaded once when both play it;
* the window's plan places the insert on `s02-noise` over the transition into
  `s03-stat`, and in Chromium its pixels are on the transition frames;
* a source that cannot be read fails the preview, naming the scene, where it
  used to leave the insert out.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_READY, JOB_RUNNING, VideoTake
from app.services.video_studio import assets, compositor, encoder, lexicon_store, render, themes, timeline, voice
from app.services.video_studio.spec import QUALITY_PRESETS, parse_video_spec, validate_video_spec

FOOTAGE_TAKE = uuid.UUID("00000000-0000-0000-0000-00000000a501")
INSERT_TAKE = uuid.UUID("00000000-0000-0000-0000-00000000a502")
FOOTAGE_FILE = uuid.UUID("00000000-0000-0000-0000-00000000f501")
INSERT_FILE = uuid.UUID("00000000-0000-0000-0000-00000000f502")
QUAD = [[0.30, 0.20], [0.80, 0.25], [0.78, 0.75], [0.32, 0.70]]
INSERT_KEY = timeline.insert_key("s02-noise", 0)


def _effective(insert_take: uuid.UUID = INSERT_TAKE) -> dict:
    return validate_video_spec(
        {
            "title": "Brand film",
            "freestyle": True,
            "voiceover": {"enabled": False},
            "captions": {"enabled": False},
            "default_transition": {"type": "crossfade", "duration": 0.8},
            "scenes": [
                {"id": "s01-open", "type": "title", "title": "Filigran", "duration": 3},
                {
                    "id": "s02-noise",
                    "type": "recording",
                    "take_id": str(FOOTAGE_TAKE),
                    "overlays": [
                        {"type": "screen_insert", "source": {"take_id": str(insert_take)}, "quad": QUAD, "start": 0.0, "duration": 30}
                    ],
                },
                {"id": "s03-stat", "type": "statement", "lines": ["6,000+ alerts a day"], "duration": 4},
            ],
        }
    )


def _take(take_id: uuid.UUID, file_id: uuid.UUID, *, status: str = JOB_READY) -> VideoTake:
    return VideoTake(
        id=take_id,
        name=f"take {str(take_id)[-4:]}",
        status=status,
        file_id=file_id if status == JOB_READY else None,
        duration_ms=6000,
        viewport_width=1600,
        viewport_height=900,
        pixel_ratio=1.0,
        fps=30,
        events=[],
        cursor=[],
        finished_at=datetime(2026, 10, 20, 14, tzinfo=UTC),
    )


class _Session:
    """The render's short session: the takes as they are now, no group, nothing else."""

    def __init__(self, rows: dict) -> None:
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, model, key, **_options):
        return self.rows.get(key) if model is VideoTake else None

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: [])


@pytest.fixture
def world(monkeypatch, tmp_path):
    world = SimpleNamespace(
        rows={FOOTAGE_TAKE: _take(FOOTAGE_TAKE, FOOTAGE_FILE), INSERT_TAKE: _take(INSERT_TAKE, INSERT_FILE)},
        stored={},
        downloads=[],
    )

    async def download_file(file_id, path):
        world.downloads.append(uuid.UUID(str(file_id)))
        source = world.stored.get(uuid.UUID(str(file_id)))
        if source is None:
            return None
        shutil.copyfile(source, path)
        return SimpleNamespace(filename="take.mp4", content_type="video/mp4")

    async def own_lexicon(_db, voiceover):
        return voice.lexicon_of(voiceover)

    monkeypatch.setattr(render, "async_session", lambda: _Session(world.rows))
    monkeypatch.setattr(assets, "download_file", download_file)
    monkeypatch.setattr(lexicon_store, "for_voiceover", own_lexicon)
    return world


def _window(effective: dict):
    full = timeline.estimate_plan(parse_video_spec(effective), {}).document
    source, target = render.scene_window(effective, "s03-stat", full)
    return parse_video_spec(source), target


async def _progress(*_args, **_kwargs):
    return None


def _gathered(world, workdir: Path, effective: dict | None = None):
    spec, target = _window(effective or _effective())
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    workdir.mkdir(parents=True, exist_ok=True)
    media = asyncio.run(render._gather_media(spec, user=user, template_id=None, workdir=workdir, progress=_progress))
    return spec, target, media


def _probe(monkeypatch):
    async def probe(_path):
        return SimpleNamespace(duration=6.0, width=1280, height=720, fps=30.0, frame_count=180, has_audio=False)

    monkeypatch.setattr(encoder, "probe", probe)


def test_the_window_of_the_next_scene_reads_the_neighbours_insert_as_its_own_preview_does(world, monkeypatch, tmp_path):
    _probe(monkeypatch)
    for file_id in (FOOTAGE_FILE, INSERT_FILE):
        world.stored[file_id] = tmp_path / f"{file_id}.mp4"
        world.stored[file_id].write_bytes(b"mp4")
    spec, target, media = _gathered(world, tmp_path / "work")
    assert [scene.id for scene in spec.scenes] == ["s02-noise", "s03-stat"] and target == 1
    # The insert's recording is the file a scene playing it as footage would use.
    assert media.assets.inserts[INSERT_KEY].path == str(tmp_path / "work" / f"take-{INSERT_TAKE}.mp4")
    assert media.plan.inserts[INSERT_KEY].duration == 6.0 and not media.notes
    plan = timeline.plan(spec, media.plan, fps=30, theme={})
    neighbour = plan.document["scenes"][0]
    [placed] = neighbour["inserts"]
    start, _end, settled_from, _to = render.window_times(plan.document, target)
    # On the neighbour's footage all through the transition into the scene previewed.
    assert placed["src"] == INSERT_KEY and float(placed["t0"]) <= start - float(neighbour["start"])
    assert float(neighbour["start"]) + float(placed["t1"]) >= settled_from - 1e-3


def test_a_recording_played_as_footage_and_as_an_insert_is_downloaded_once(world, monkeypatch, tmp_path):
    _probe(monkeypatch)
    world.stored[FOOTAGE_FILE] = tmp_path / "footage.mp4"
    world.stored[FOOTAGE_FILE].write_bytes(b"mp4")
    _spec, _target, media = _gathered(world, tmp_path / "work", _effective(insert_take=FOOTAGE_TAKE))
    assert world.downloads == [FOOTAGE_FILE]
    assert media.assets.inserts[INSERT_KEY].path == media.assets.sources["take/s02-noise"].path


def test_a_neighbours_insert_that_cannot_be_read_fails_the_preview_naming_it(world, monkeypatch, tmp_path):
    _probe(monkeypatch)
    world.stored[FOOTAGE_FILE] = tmp_path / "footage.mp4"
    world.stored[FOOTAGE_FILE].write_bytes(b"mp4")
    world.rows[INSERT_TAKE] = _take(INSERT_TAKE, INSERT_FILE, status=JOB_RUNNING)
    with pytest.raises(RuntimeError, match=f"Scene s02-noise: the recording of its screen insert 0 {INSERT_TAKE} is not ready"):
        _gathered(world, tmp_path / "running")
    # Ready, but its file gone from the store: the same, never an insert left out.
    world.rows[INSERT_TAKE] = _take(INSERT_TAKE, INSERT_FILE)
    with pytest.raises(RuntimeError, match=f"Scene s02-noise: the file of recording {INSERT_TAKE} is gone"):
        _gathered(world, tmp_path / "gone")
    world.rows[INSERT_TAKE].expired_at = datetime(2026, 10, 20, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="the recording of its screen insert 0 take a502 expired with the retention"):
        _gathered(world, tmp_path / "expired")


def _with_neighbour(source: dict | None = None, *, backdrop: dict | None = None, scene: dict | None = None) -> dict:
    """The film whose ``s02`` neighbour of ``s03-stat`` shows *source* in a screen insert on its recording, or is a
    statement over *backdrop*, or is *scene*."""
    if scene is not None:
        neighbour = scene
    elif backdrop is not None:
        neighbour = {"id": "s02", "type": "statement", "lines": ["Every alert"], "duration": 4, "backdrop": backdrop}
    else:
        neighbour = {
            "id": "s02",
            "type": "recording",
            "take_id": str(FOOTAGE_TAKE),
            "overlays": [{"type": "screen_insert", "source": source, "quad": QUAD, "start": 0.0, "duration": 30}],
        }
    return validate_video_spec(
        {
            "title": "Brand film",
            "freestyle": True,
            "voiceover": {"enabled": False},
            "captions": {"enabled": False},
            "scenes": [
                {"id": "s01-open", "type": "title", "title": "Filigran", "duration": 3},
                neighbour,
                {"id": "s03-stat", "type": "statement", "lines": ["6,000+ alerts a day"], "duration": 4},
            ],
        }
    )


GONE_SHOT = uuid.UUID("00000000-0000-0000-0000-00000000c503")
GONE_FILE = uuid.UUID("00000000-0000-0000-0000-00000000f503")


@pytest.mark.parametrize(
    ("film", "reason"),
    [
        (
            lambda: _with_neighbour({"file_id": str(GONE_FILE)}),
            f"Scene s02: the source of its screen insert 0 ({GONE_FILE}) is gone",
        ),
        (lambda: _with_neighbour({"clip_id": str(GONE_SHOT)}), f"Scene s02 (its screen insert 0): AI shot {GONE_SHOT} was deleted"),
        (lambda: _with_neighbour({"take_id": str(uuid.UUID(int=0xA599))}), "Scene s02: the recording of its screen insert 0"),
        (lambda: _with_neighbour(backdrop={"clip_id": str(GONE_SHOT)}), f"Scene s02: AI shot {GONE_SHOT} was deleted"),
        (lambda: _with_neighbour(backdrop={"file_id": str(GONE_FILE)}), f"Scene s02: the file behind it ({GONE_FILE}) is gone"),
        (
            lambda: _with_neighbour(scene={"id": "s02", "type": "clip", "clip_id": str(GONE_SHOT)}),
            f"Scene s02: AI shot {GONE_SHOT} was deleted",
        ),
    ],
    ids=["insert-file", "insert-shot", "insert-recording", "backdrop-shot", "backdrop-file", "clip-scene"],
)
def test_a_neighbours_source_that_is_missing_fails_the_preview_of_the_next_scene(world, monkeypatch, tmp_path, film, reason):
    """Preview scene N+1, scene N's source missing: the preview fails with the reason its own would give."""
    _probe(monkeypatch)
    world.stored[FOOTAGE_FILE] = tmp_path / "footage.mp4"
    world.stored[FOOTAGE_FILE].write_bytes(b"mp4")
    effective = film()
    with pytest.raises(RuntimeError, match=re.escape(reason)):
        _gathered(world, tmp_path / "next", effective)
    # The scene's own preview fails on the very same words.
    own, own_target = render.scene_window(effective, "s02", timeline.estimate_plan(parse_video_spec(effective), {}).document)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    (tmp_path / "own").mkdir()
    with pytest.raises(RuntimeError, match=re.escape(reason)):
        asyncio.run(
            render._gather_media(parse_video_spec(own), user=user, template_id=None, workdir=tmp_path / "own", progress=_progress)
        )
    assert own_target == 1


def test_a_brand_mark_the_template_lacks_is_left_out_alike_in_both_previews(world, monkeypatch, tmp_path):
    """A brand mark the template lacks is missing the same way, as a neighbour or not - never a crash, and an
    ``insert_missing`` finding of its scene, never a note."""
    _probe(monkeypatch)
    world.stored[FOOTAGE_FILE] = tmp_path / "footage.mp4"
    world.stored[FOOTAGE_FILE].write_bytes(b"mp4")
    effective = _with_neighbour({"asset": "logo_mark"})
    _spec, _target, as_neighbour = _gathered(world, tmp_path / "next", effective)
    own, _own_target = render.scene_window(effective, "s02", timeline.estimate_plan(parse_video_spec(effective), {}).document)
    (tmp_path / "own").mkdir()
    itself = asyncio.run(
        render._gather_media(
            parse_video_spec(own), user=SimpleNamespace(id=uuid.uuid4(), role="user"), template_id=None,
            workdir=tmp_path / "own", progress=_progress,
        )
    )  # fmt: skip
    assert as_neighbour.missing_inserts == itself.missing_inserts
    assert list(as_neighbour.missing_inserts.values()) == ["the template has no file for its logo_mark"]
    assert not [note for note in (*as_neighbour.notes, *itself.notes) if "logo_mark" in note]


# ── In Chromium: the insert is on the transition frames ───────────────────


def _clip(path: Path, colour: str, size: str) -> None:
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
            f"color=c={colour}:size={size}:rate=30",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _red_share(picture) -> float:
    """The share of *picture*'s pixels that are the insert's red: neither the blue footage nor the statement's theme."""
    import numpy as np

    rgb = np.asarray(picture.convert("RGB"), dtype=np.int16)
    red = (rgb[..., 0] > 150) & (rgb[..., 1] < 90) & (rgb[..., 2] < 90)
    return float(red.mean())


@pytest.mark.chromium
async def test_the_neighbours_insert_is_drawn_on_the_transition_frames(world, tmp_path):
    if not encoder.encoder_available():
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail("ffmpeg is required here and is not installed")
        pytest.skip("ffmpeg is not installed")
    from PIL import Image

    from app.services.builtin_tools.html_render import font_face_css

    world.stored[FOOTAGE_FILE], world.stored[INSERT_FILE] = tmp_path / "footage.mp4", tmp_path / "insert.mp4"
    _clip(world.stored[FOOTAGE_FILE], "blue", "1600x900")
    _clip(world.stored[INSERT_FILE], "red", "1280x720")
    spec, target = _window(_effective())
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    (tmp_path / "work").mkdir()
    media = await render._gather_media(spec, user=user, template_id=None, workdir=tmp_path / "work", progress=_progress)
    theme = themes.build_theme(mode=spec.theme.mode, background_style=spec.theme.background)
    plan = timeline.plan(spec, media.plan, fps=30, theme=theme)
    start, _end, settled_from, _to = render.window_times(plan.document, target)
    # The transition into the scene previewed: its first frames, the neighbour still mostly on screen.
    frame_range = (math.floor(start * 30), math.floor(start * 30) + max(2, math.floor((settled_from - start) * 30 / 3)))
    try:
        video = await compositor.render_frames(
            plan.document,
            media.assets,
            preset=QUALITY_PRESETS["draft"],
            output_size=(640, 360),
            workdir=str(tmp_path / "work"),
            font_css=font_face_css(),
            pages=1,
            frame_range=frame_range,
        )
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    still = tmp_path / "transition.jpg"
    await encoder.extract_still(video, at=0.0, out_path=str(still), max_side=640)
    with Image.open(still) as picture:
        assert _red_share(picture) > 0.03, "the neighbour's screen insert is missing from the transition frames"
