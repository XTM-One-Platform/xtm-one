"""A render start answers the ready render that already covers the revision. Synthetic rows and specs only.

A revision that changes nothing drawn - a screen insert's legibility declaration, a deck's speaker notes - left the
previous render valid, yet ``render_video`` on it queued a new full render of 20 to 60 minutes:

* the latest ready whole render of the video, at the quality asked, its gates green, whose every scene's drawn digest
  and whose sound match the revision's, is answered - nothing queued, gated, cancelled or audited - and becomes the
  render of that revision, keeping the one it was drawn for; ``force`` renders again anyway;
* a scene's trim (drawn) or its own sound (heard) changed, another quality, a render with a finding, or one that
  recorded nothing to compare queues a new render;
* ``render_video`` says the render already covers the revision and that ``force`` renders it again.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_READY, VideoRender
from app.services.video_studio import jobs, lexicon_store, projects, render, render_checks
from app.services.video_studio import quality as quality_report
from app.services.video_studio.spec import validate_video_spec

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-000000006354"), role="user", email="maker@example.com")
CLIP = "00000000-0000-0000-0000-0000000063c1"
INSERTED = "00000000-0000-0000-0000-0000000063c2"
DECK = "00000000-0000-0000-0000-0000000063d3"


def _deck(*, notes: str = "Say the number.", revision: int = 1) -> SimpleNamespace:
    return SimpleNamespace(revision=revision, spec={"slides": [{"title": "Q3", "notes": notes}]}, theme="dark", template_container_id=None)


def _film(*, trim: float = 0.0, audio: bool = False, legibility: str | None = None) -> dict:
    overlay = {
        "type": "screen_insert",
        "source": {"clip_id": INSERTED},
        "quad": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]],
        "duration": 2.0,
    }
    if legibility:
        overlay["legibility"] = legibility
    shot = {"id": "shot", "type": "clip", "clip_id": CLIP, "trim_start": trim, "audio": audio, "overlays": [overlay]}
    scenes = [
        {"id": "open", "type": "title", "title": "Hello", "duration": 3},
        shot,
        {"id": "deck", "type": "slide", "presentation_id": DECK, "slide": 1},
    ]
    return validate_video_spec({"title": "Covered", "freestyle": True, "voiceover": {"voice": "marin"}, "scenes": scenes})


class _Db:
    """The start's reads: no render in flight, the video's latest ready whole render, the deck as it is now."""

    def __init__(self, ready: VideoRender | None, deck: SimpleNamespace) -> None:
        self.ready = ready
        self.deck = deck
        self.asked: list[str] = []
        self.added: list = []

    async def execute(self, statement, _params=None):
        sql = str(statement)
        self.asked.append(sql)
        latest = "video_renders.status = " in sql and "ORDER BY video_renders.finished_at DESC" in sql
        rows = [self.ready] if latest and self.ready is not None else []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, model, _key, **_options):
        return self.deck if model.__name__ == "Presentation" else None

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(effective=_film(), gated=0, enqueued=[], audits=[])

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec=state.effective)

    async def own_lexicon(_db, _voiceover):
        return None

    async def gate(*_args, **_kwargs):
        state.gated += 1
        return []

    async def lint(_db, _project, _user, **_kwargs):
        return state.effective, [], []

    async def nothing(*_args, **_kwargs):
        return None

    async def none(*_args, **_kwargs):
        return []

    async def enqueue(_db, kind, row, **kwargs):
        state.enqueued.append((kind, row))

    async def audit(_db, **kwargs):
        state.audits.append(kwargs)

    async def no_preflight(*_args, **_kwargs):
        raise RuntimeError("no estimate in this test")

    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(lexicon_store, "for_voiceover", own_lexicon)
    monkeypatch.setattr(render_checks, "gate", gate)
    monkeypatch.setattr(projects, "_lint", lint)
    monkeypatch.setattr(projects, "_lock_render_starts", nothing)
    monkeypatch.setattr(projects, "render_left_unresumed", nothing)
    monkeypatch.setattr(jobs, "renders_in_flight", none)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    monkeypatch.setattr("app.services.video_studio.render_preflight.preflight", no_preflight)
    return state


def _ready(effective: dict, deck: SimpleNamespace, *, quality: str = "high", revision: int = 12, checks=None, heard=True) -> VideoRender:
    """A whole render of *effective* finished ready, recording what it drew and played as a render does
    (``render.drawing_of``)."""
    marks = asyncio.run(render_checks.digest_scenes(_Db(None, deck), effective))
    drawing = render.drawing_of(SimpleNamespace(drawing=None), revision, marks, render_checks.sound_sources(effective))
    if not heard:
        drawing.pop("heard")
    return VideoRender(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=USER.id,
        revision=revision,
        quality=quality,
        status=JOB_READY,
        finished_at=datetime.now(UTC),
        checks=checks
        if checks is not None
        else {"status": "passed", "findings": [], "quality_report_version": quality_report.QUALITY_REPORT_VERSION},
        drawing=drawing,
    )


def _project(revision: int = 13) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), revision=revision, title="Covered", scene_previews=None)


def _start(world, ready, deck, **kwargs) -> projects.StartedRender:
    project = _project()
    db = _Db(ready, deck)
    started = asyncio.run(projects.start_render(db, project, USER, quality=kwargs.pop("quality", "high"), **kwargs))
    world.db, world.project = db, project
    return started


def test_a_revision_that_draws_and_plays_the_same_answers_the_ready_render_and_queues_nothing(world):
    ready = _ready(_film(), _deck())
    # Revision 13 only edits the deck's speaker notes: its revision moves, its slides as an audience sees them do not.
    started = _start(world, ready, _deck(notes="Pause on the chart.", revision=2))
    assert started.render is ready and started.covered_from == 12 and not started.reused and started.superseded == []
    # Nothing queued, gated or audited: nothing to draw.
    assert (world.enqueued, world.gated, world.audits, world.db.added) == ([], 0, [], [])
    # The render of revision 13 now, keeping the revision it was drawn for.
    assert ready.revision == 13 and ready.drawing["revision"] == 13 and ready.drawing["drawn_for"] == 12
    # A screen insert's legibility declaration says how it is judged: the same render covers that revision too.
    world.effective = _film(legibility="ambient")
    again = _start(world, _ready(_film(), _deck()), _deck())
    assert again.covered_from == 12 and world.enqueued == []


def test_a_trim_a_sound_another_quality_or_a_finding_queues_a_new_render(world):
    deck = _deck()
    # A scene's trim: its frames change.
    world.effective = _film(trim=1.5)
    trimmed = _start(world, _ready(_film(), deck), deck)
    assert trimmed.covered_from is None and trimmed.render is not None and world.enqueued[-1][1] is trimmed.render
    # A clip's own sound: nothing drawn changed, but the ready render plays the sound of the revision it was mixed for.
    world.effective = _film(audio=True)
    heard = _start(world, _ready(_film(), deck), deck)
    assert heard.covered_from is None and world.enqueued[-1][1] is heard.render
    world.effective = _film()
    # Another quality is another render.
    assert _start(world, _ready(_film(), deck), deck, quality="ultra").covered_from is None
    # A ready render whose checks left a finding (a warning too) is not all green: drawn again.
    warned = {
        "status": "passed",
        "findings": [{"kind": "frozen", "severity": "warn", "at": 1.0, "scene_id": "shot", "message": "still"}],
        "quality_report_version": quality_report.QUALITY_REPORT_VERSION,
    }
    assert _start(world, _ready(_film(), deck, checks=warned), deck).covered_from is None
    # A render made before it recorded what it plays covers nothing.
    assert _start(world, _ready(_film(), deck, heard=False), deck).covered_from is None


def test_f52_a_ready_render_drawn_at_an_older_drawing_version_is_drawn_again_without_force(world):
    # Render 1d30f3fe: drawn at DRAWING_VERSION 7, main at 8 - the matte fix changes every insert frame and no
    # scene's spec - answered "already covers revision 136, nothing to draw".
    deck = _deck()
    now = render_checks.release_drawing()
    assert set(now) == {"drawing", "insert_build"}
    older = _ready(_film(), deck)
    older.drawing["release"] = {**now, "drawing": now["drawing"] - 1}
    started = _start(world, older, deck)
    assert started.covered_from is None and started.render is not older and world.enqueued[-1][1] is started.render
    assert started.redrawn == (
        f"Render `{older.id}` draws every scene of revision 13 at high as it is now, but was drawn at screen-insert "
        f"drawing version {now['drawing'] - 1}, now {now['drawing']}: rendering again."
    )
    assert older.revision == 12
    # Its insert build alone moved: drawn again too.
    built = _ready(_film(), deck)
    built.drawing["release"] = {**now, "insert_build": now["insert_build"] - 1}
    again = _start(world, built, deck)
    assert again.covered_from is None and f"insert build version {now['insert_build'] - 1}, now {now['insert_build']}" in again.redrawn
    # A render made before renders recorded their release: drawn again, said unrecorded.
    unrecorded = _ready(_film(), deck)
    unrecorded.drawing.pop("release")
    said = _start(world, unrecorded, deck).redrawn
    assert "drawn at screen-insert drawing version unrecorded, now" in said and "insert build version unrecorded, now" in said
    # This release's covers it, nothing said.
    current = _start(world, _ready(_film(), deck), deck)
    assert current.covered_from == 12 and current.redrawn is None


def test_a_ready_render_judged_under_an_older_quality_report_is_drawn_again_without_force(world):
    # Hard cut joins snapped to whole frames with the cut_blend gate (quality report v48 -> v49), the screen-insert
    # drawing version unmoved: a v48 render was still answered "already covers revision N".
    deck = _deck()
    running = quality_report.QUALITY_REPORT_VERSION
    older = _ready(_film(), deck, checks={"status": "passed", "findings": [], "quality_report_version": running - 1})
    started = _start(world, older, deck)
    assert started.covered_from is None and started.render is not older and world.enqueued[-1][1] is started.render
    assert started.redrawn == (
        f"Render `{older.id}` draws every scene of revision 13 at high as it is now, but was judged under quality "
        f"report v{running - 1}, now v{running}: rendering again."
    )
    assert older.revision == 12 and world.audits and "forced" not in world.audits[-1]["details"]
    # A render whose checks recorded no quality report version predates every gate since: drawn again, said so.
    unjudged = _ready(_film(), deck, checks={"status": "passed", "findings": []})
    said = _start(world, unjudged, deck).redrawn
    assert f"judged under quality report version unrecorded, now v{running}" in said
    # An older drawing version and an older report: both said, one render queued.
    both = _ready(_film(), deck, checks={"status": "passed", "findings": [], "quality_report_version": running - 1})
    both.drawing["release"] = {**both.drawing["release"], "drawing": both.drawing["release"]["drawing"] - 1}
    said = _start(world, both, deck).redrawn
    assert "drawn at screen-insert drawing version" in said and f", and judged under quality report v{running - 1}" in said
    # Drawn by this release and judged under the running report: covered, nothing queued.
    queued = len(world.enqueued)
    current = _start(world, _ready(_film(), deck), deck)
    assert current.covered_from == 12 and current.redrawn is None and len(world.enqueued) == queued


def test_get_video_lists_the_release_each_render_was_drawn_at():
    from app.services.builtin_tools import video_studio as tool

    now = render_checks.release_drawing()
    ready = _ready(_film(), _deck())
    line = tool.render_list_line(ready, SimpleNamespace(status=jobs.STATE_READY), current=None)
    assert (
        f"drawn at screen-insert drawing version {now['drawing']}, insert build version {now['insert_build']} (current)"
        in line
    )
    assert f"checks v{quality_report.QUALITY_REPORT_VERSION}" in line
    ready.drawing["release"] = {**now, "drawing": now["drawing"] - 1}
    older = tool.render_list_line(ready, SimpleNamespace(status=jobs.STATE_READY), current=None)
    assert f"(older than this release: screen-insert drawing version {now['drawing']})" in older
    ready.drawing.pop("release")
    assert "drawn at" not in tool.render_list_line(ready, SimpleNamespace(status=jobs.STATE_READY), current=None)


def test_f52_render_video_says_why_the_ready_render_is_drawn_again(monkeypatch):
    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    queued = _ready(_film(), _deck(), revision=13)
    project = SimpleNamespace(id=queued.project_id, title="Covered", revision=13)
    line = "Render `1d30f3fe` draws every scene of revision 13 at high as it is now, but was drawn at screen-insert drawing version 7, now 8: rendering again."

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def start(*_args, **_kwargs):
        return projects.StartedRender(queued, [], redrawn=line)

    async def waited(_kind, _ids, **_kwargs):
        return [queued]

    async def nothing(*_args, **_kwargs):
        return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(id=USER.id, email=USER.email, role="user")

        async def commit(self):
            return None

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "start_render", start)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", waited)
    monkeypatch.setattr(tool, "async_session", lambda: _Session())
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    _defs, executor = tool.build_video_studio_tools(user_id=str(USER.id))
    call = ToolCall(id="r", name="render_video", arguments={"video_id": str(project.id), "quality": "high"})
    said = asyncio.run(executor.execute_tool(call)).content
    assert line in said and "already covers" not in said


def test_force_renders_a_covered_revision_again(world):
    deck = _deck()
    ready = _ready(_film(), deck)
    started = _start(world, ready, deck, force=True)
    assert started.covered_from is None and started.render is not ready and world.enqueued[-1][1] is started.render
    assert ready.revision == 12


def test_render_video_says_the_ready_render_covers_the_revision(monkeypatch):
    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    ready = _ready(_film(), _deck(), revision=13)
    ready.drawing["drawn_for"] = 12
    project = SimpleNamespace(id=ready.project_id, title="Covered", revision=13)

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def start(*_args, **_kwargs):
        return projects.StartedRender(ready, [], covered_from=12)

    async def waited(_kind, _ids, **_kwargs):
        return [ready]

    async def nothing(*_args, **_kwargs):
        return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(id=USER.id, email=USER.email, role="user")

        async def commit(self):
            return None

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "start_render", start)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", waited)
    monkeypatch.setattr(tool, "async_session", lambda: _Session())
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    _defs, executor = tool.build_video_studio_tools(user_id=str(USER.id))
    call = ToolCall(id="r", name="render_video", arguments={"video_id": str(project.id), "quality": "high"})
    said = asyncio.run(executor.execute_tool(call)).content
    assert f"Render `{ready.id}` already covers revision 13, nothing to draw" in said
    assert "(drawn for revision 12)" in said and "Nothing was queued" in said and "render_video(force: true) renders it again" in said
