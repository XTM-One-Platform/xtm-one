"""A preview of a card the template adds keeps its three frames.

``render_video_scene`` on the "intro" or the "outro" of XTM Blast failed with
ffmpeg exit 234, "No filtered frames for output stream": the card was drawn,
its MP4 complete, but its last settled frame was asked at the exact time of
the MP4's last frame - the window ends with the card when nothing follows it
(the outro, an intro cut into the next scene) - and that time, rounded to the
millisecond for the seek, landed past it. What is proven:

* the three frames are frames the composed MP4 holds, whatever its length, and
  the seek of each keeps that very frame (ffmpeg keeps the first frame at or
  after the time it is given);
* in Chromium and ffmpeg, the intro and the outro of every built-in video
  template preview with three frames, each showing the card out of the
  video's fades from and to black, the card drawn exactly as the whole render
  plans it.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.user import User
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoProject, VideoRender
from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS
from app.services.video_studio import (
    assets,
    compositor,
    encoder,
    jobs,
    lexicon_store,
    projects,
    render,
    render_checks,
    render_retention,
    templates,
    timeline,
)
from app.services.video_studio.spec import authored_spec, parse_video_spec

FPS = 30


# ── Which frames ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("count", range(60, 241))
def test_the_last_settled_frame_of_a_window_ending_with_its_scene_is_the_mp4s_last(count):
    """An outro of *count* frames: its window ends with it, and its last frame is cut inside the MP4."""
    start = 10.0
    times = (start, start + count / FPS, start + 0.8, start + count / FPS)
    frame_range = (math.floor(times[0] * FPS), math.ceil(times[1] * FPS))
    first, middle, last = render.settled_frames(times, frame_range, FPS, count)
    assert last == count - 1 and 0 <= first < middle < last
    # The seek, as ffmpeg is given it: the first frame at or after it is the one asked, never past the MP4's end.
    for frame in (first, middle, last):
        seek = float(f"{max(0.0, (frame - 0.25) / FPS):.3f}")
        assert (frame - 1) / FPS < seek <= frame / FPS
    # The time the frames were asked at before: the last frame's own start, rounded up past it for 2 counts in 3.
    if (count - 1) % 3 == 2:
        assert float(f"{(count - 1) / FPS:.3f}") > (count - 1) / FPS


def test_the_frames_are_the_mp4s_own_even_when_it_holds_fewer_than_planned():
    """A muxed MP4 a frame shorter than its window (its sound a hair shorter): the last frame it has."""
    times = (2.2, 6.2, 3.0, 6.2)
    frame_range = (66, 186)
    assert render.settled_frames(times, frame_range, FPS, 119) == (26, 72, 118)
    assert render.settled_frames(times, frame_range, FPS, 120) == (26, 72, 119)
    # A scene followed by another: its last settled frame is the one before the next scene comes in.
    assert render.settled_frames((0.0, 4.4, 0.0, 3.6), (0, 132), FPS, 132)[2] == 107


# ── In Chromium and ffmpeg: every built-in template's cards ────────────────

_SEEDED = [entry for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["kind"] == "video"]
_CARDS = [
    pytest.param(entry, card, id=f"{entry['slug']}-{card}")
    for entry in _SEEDED
    for card in ("intro", "outro")
    if (entry["extra_metadata"]["video"].get(card) or {}).get("required")
]


def _template(entry: dict) -> templates.VideoTemplate:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        name=entry["name"],
        kind="video",
        description=None,
        tags=entry["tags"],
        theme_config=entry["theme_config"],
        extra_metadata=entry["extra_metadata"],
    )
    return templates.VideoTemplate.from_container(row)


def _xtm_blast() -> dict:
    """The video of the report: its template's cards around two statements, the first cut in after the intro."""
    return authored_spec(
        {
            "title": "XTM Blast",
            "scenes": [
                {"id": "s1", "type": "statement", "lines": ["One platform"], "transition": {"type": "cut"}},
                {"id": "s2", "type": "statement", "lines": ["Every exposure, validated"]},
            ],
        }
    )


@pytest.fixture
def card_preview(monkeypatch, tmp_path):
    """``run_render`` of a card preview, its database, media gathering, checks and storage stood in for."""
    world = SimpleNamespace(stored=[], drawn=None, row=None)

    def prepare(entry: dict, card: str) -> SimpleNamespace:
        template = _template(entry)
        applied = templates.apply_template(_xtm_blast(), template)
        maker = User(id=uuid.uuid4(), email="maker@example.com", role="user")
        project = VideoProject(id=uuid.uuid4(), title="XTM Blast", spec=_xtm_blast(), revision=1)
        row = VideoRender(
            id=uuid.uuid4(), project_id=project.id, user_id=maker.id, status=JOB_PENDING, quality="draft", scene_id=card
        )
        rows = {VideoRender: row, VideoProject: project, User: maker}
        world.row, world.applied = row, applied

        class _Db:
            async def get(self, model, _key, **_options):
                return rows.get(model)

            async def commit(self):
                return None

            async def flush(self):
                return None

            def expunge(self, _row):
                return None

        @asynccontextmanager
        async def session():
            yield _Db()

        async def effective_spec(_db, _project, _user):
            return SimpleNamespace(spec=applied.spec, template=template, notes=[])

        monkeypatch.setattr(render, "async_session", session)
        monkeypatch.setattr(projects, "effective_spec", effective_spec)
        return world

    async def gather(_spec, **_kwargs):
        return render._Media()

    real_frames = compositor.render_frames

    async def render_frames(document, *args, **kwargs):
        world.drawn = document
        return await real_frames(document, *args, **kwargs)

    async def check_render(**_kwargs):
        return render_checks.Checks(audio={"status": "passed"}, frames={"status": "passed"})

    async def store_path(path, **kwargs):
        stored = SimpleNamespace(id=uuid.uuid4(), size=Path(path).stat().st_size, path=path, **kwargs)
        world.stored.append(stored)
        return stored

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(world.row, key, value)

    async def mark_if_open(kind, job_id, **changes):
        await mark(kind, job_id, **changes)
        return True

    async def nothing(*_args, **_kwargs):
        return None

    async def none(*_args, **_kwargs):
        return []

    async def none_replaced(*_args, **_kwargs):
        return render_retention.Deletion()

    async def no_digests(*_args, **_kwargs):
        return render_checks.SceneDigests({}, {})

    async def not_cancelled(*_args, **_kwargs):
        return False

    monkeypatch.setattr(render, "bind_llm_caller", nothing)
    monkeypatch.setattr(render, "_gather_media", gather)
    monkeypatch.setattr(render, "check_render", check_render)
    monkeypatch.setattr(compositor, "render_frames", render_frames)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(jobs, "is_cancelled", not_cancelled)
    monkeypatch.setattr(render.retention, "touch", nothing)
    monkeypatch.setattr(render_retention, "replaced_previews", none_replaced)
    monkeypatch.setattr(render_checks, "digest_scenes", no_digests)
    monkeypatch.setattr(lexicon_store, "for_voiceover", nothing)
    world.prepare, world.workdir = prepare, tmp_path
    return world


@pytest.mark.chromium
@pytest.mark.parametrize(("entry", "card"), _CARDS)
def test_the_intro_and_outro_of_every_built_in_template_preview_with_three_frames(card_preview, entry, card):
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    world = card_preview.prepare(entry, card)
    assert card in world.applied.added_scenes
    try:
        asyncio.run(render.run_render(str(world.row.id), workdir=world.workdir))
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    row = world.row
    assert row.status == JOB_READY, row.error
    frames = [s for s in world.stored if s.filename.endswith(("-first.jpg", "-mid.jpg", "-last.jpg"))]
    assert len(frames) == 3 and row.frame_file_ids == [str(s.id) for s in frames]
    # Each shows the card, out of the video's fades from and to black: its words are lit.
    from PIL import Image

    for stored in frames:
        with Image.open(stored.path) as picture:
            assert picture.width <= 1280 and picture.convert("L").getextrema()[1] >= 128, stored.filename
    # The card drawn is the one the whole render plans, only placed in its window.
    full = timeline.estimate_plan(parse_video_spec(world.applied.spec), {}).document
    planned = next(scene for scene in full["scenes"] if scene["id"] == card)
    drawn = next(scene for scene in card_preview.drawn["scenes"] if scene["id"] == card)

    def placed(scene: dict) -> dict:
        return {key: value for key, value in scene.items() if key not in ("start", "end")}

    assert placed(drawn) == placed(planned)
    assert drawn["end"] - drawn["start"] == pytest.approx(planned["end"] - planned["start"])
