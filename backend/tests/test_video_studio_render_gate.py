"""The render's quality gate (``render_checks.gate``). Synthetic videos, shots and findings only.

Through the real gate, and the real ``projects.start_render`` over a fake session:

* an AI shot or a recording a scene plays that is not ready refuses the
  render, each with what a render would do with it (the scene's footage, a
  screen insert's source), named once even when the quality check says it too;
* the quality check saved for the revision refuses on a finding the audience
  would see, never on an advisory one - read as saved, the video never played;
* a ready video with a clean check passes; ``force`` starts it anyway, the
  override recording these reasons too; ``render_video`` never forces on a
  ``"false"``;
* previews saving together each keep their scene: ``run_render`` records the
  scenes on the video's row locked.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING, VideoClip, VideoProject
from app.services.video_studio import jobs, projects, quality, render_checks
from app.services.video_studio.spec import validate_video_spec

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000d064"), role="user", email="maker@example.com")
SHOT = "00000000-0000-0000-0000-00000000d0c1"
INSERT_SHOT = "00000000-0000-0000-0000-00000000d0c2"


def _shot(clip_id: str, status: str = JOB_READY, **fields) -> VideoClip:
    return VideoClip(
        id=uuid.UUID(clip_id),
        user_id=USER.id,
        provider="p",
        model="m",
        prompt="A fox",
        name=fields.pop("name", None),
        status=status,
        file_id=uuid.uuid4() if status == JOB_READY else None,
        finished_at=datetime(2026, 10, 20, tzinfo=UTC),
        params={},
        **fields,
    )


def _effective() -> dict:
    return validate_video_spec(
        {
            "title": "Brand film",
            "freestyle": True,
            "scenes": [
                {"id": "open", "type": "title", "title": "Brand film"},
                {
                    "id": "shot",
                    "type": "clip",
                    "clip_id": SHOT,
                    "overlays": [
                        {
                            "type": "screen_insert",
                            "source": {"clip_id": INSERT_SHOT},
                            "quad": [[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]],
                        }
                    ],
                },
                {"id": "close", "type": "statement", "lines": ["Every alert closed"]},
            ],
        }
    )


_TYPES = {"open": "title", "shot": "clip", "close": "statement"}


def _finding(scene_id, index, kind, severity, message) -> quality.QualityFinding:
    return quality.QualityFinding(index, scene_id, _TYPES[scene_id], "", kind, severity, message)


class _Db:
    """The rows the gate and ``start_render`` read: the shots, no render of the revision yet, what is added."""

    def __init__(self) -> None:
        self.rows = {SHOT: _shot(SHOT), INSERT_SHOT: _shot(INSERT_SHOT)}
        self.added: list = []

    async def get(self, model, key, **_options):
        return self.rows.get(str(key)) if model is VideoClip else None

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


PROJECT = SimpleNamespace(id=uuid.uuid4(), revision=4, title="Brand film", scene_previews=None)


@pytest.fixture
def world(monkeypatch):
    world = SimpleNamespace(db=_Db(), findings=[], asked=[], audits=[])

    async def checked(project_id, user_id, **kwargs):
        world.asked.append((project_id, user_id, kwargs))
        return quality.QualityReport(scene_count=3, findings=tuple(world.findings), revision=4)

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec=_effective())

    async def none(*_args, **_kwargs):
        return []

    async def nothing(*_args, **_kwargs):
        return None

    async def audit(_db, **kwargs):
        world.audits.append(kwargs)

    monkeypatch.setattr(quality, "check_video_quality", checked)
    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(jobs, "renders_in_flight", none)
    monkeypatch.setattr(jobs, "enqueue", nothing)
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    return world


def _gate(world) -> list[render_checks.GateReason]:
    return asyncio.run(render_checks.gate(world.db, PROJECT, USER, _effective(), quality="high"))


def test_a_ready_video_with_a_clean_check_passes_and_is_never_played(world):
    world.findings = [_finding("open", 0, "overlay_short", "advisory", "the badge shows for 0.5 s")]
    assert _gate(world) == []
    # The check of this revision: its layout as kept, measured only when none is (the pre-render lint never
    # skips text fit and overlap) - the music's level is never measured at a render's start, nor any frame drawn.
    assert world.asked == [(PROJECT.id, USER.id, {"expected_revision": 4, "measure": False, "measure_layout": True})]


def test_a_shot_not_ready_refuses_the_render_with_what_a_render_would_do(world):
    world.db.rows[SHOT] = _shot(SHOT, JOB_RUNNING, name="Fox at dusk")
    world.db.rows[INSERT_SHOT] = _shot(INSERT_SHOT, "failed", error="content drift: a dog. Generate it again.")
    world.findings = [_finding("shot", 1, "shot_state", "overloaded", "its AI shot is still being generated")]
    reasons = _gate(world)
    assert [(r.code, r.scene_id) for r in reasons] == [("media_not_ready", "shot"), ("media_not_ready", "shot")]
    assert reasons[0].message == (
        f'scene shot: its AI shot `{SHOT}` "Fox at dusk" is still being made - a render refuses it; wait for it '
        "(get_video_jobs) or generate it again"
    )
    assert reasons[1].message.startswith(
        f"scene shot: its screen insert's AI shot `{INSERT_SHOT}` failed (content drift: a dog. Generate it again) - "
        "a render refuses it"
    )
    # A shot deleted since: said so.
    del world.db.rows[INSERT_SHOT]
    world.db.rows[SHOT] = _shot(SHOT)
    assert [r.message.split(" - ")[0] for r in _gate(world)] == [f"scene shot: its screen insert's AI shot `{INSERT_SHOT}` was deleted"]


def test_the_saved_quality_check_refuses_on_what_the_audience_would_see(world):
    world.findings = [
        _finding("close", 2, "reading", "dense", "its 3 words are all on screen for 0.4 s"),
        _finding("open", 0, "overlay_short", "advisory", "the badge shows for 0.5 s"),
    ]
    reasons = _gate(world)
    assert [(r.code, r.scene_id, r.message) for r in reasons] == [
        ("quality", "close", "scene close: its 3 words are all on screen for 0.4 s (reading, dense)")
    ]


def test_start_render_refuses_on_them_and_a_forced_render_records_them(world):
    world.db.rows[SHOT] = _shot(SHOT, JOB_PENDING)
    with pytest.raises(render_checks.RenderGateError) as refused:
        asyncio.run(projects.start_render(world.db, PROJECT, USER, quality="high"))
    assert [r.code for r in refused.value.reasons] == ["media_not_ready"] and world.db.added == []
    started = asyncio.run(projects.start_render(world.db, PROJECT, USER, quality="draft", force=True))
    assert [r["code"] for r in started.render.gate_override["reasons"]] == ["media_not_ready"]
    assert world.db.added == [started.render] and world.audits[-1]["details"]["forced"] is True


def test_render_video_never_forces_on_a_false_string(monkeypatch):
    from app.services.builtin_tools.video_studio import build_video_studio_tools
    from app.tools.base import ToolCall

    project = SimpleNamespace(id=uuid.uuid4(), title="Brand film", revision=2)
    seen: list = []

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def start(_db, _project, _user, **kwargs):
        seen.append(kwargs["force"])
        raise render_checks.RenderGateError([render_checks.GateReason("media_not_ready", "scene shot: its AI shot is still being made")])

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(id=uuid.UUID(int=1), email="maker@example.com", role="user")

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "start_render", start)
    monkeypatch.setattr("app.services.builtin_tools.video_studio.async_session", lambda: _Session())
    definitions, executor = build_video_studio_tools(user_id=str(uuid.UUID(int=1)))
    for force in ("false", False, "true", True):
        call = ToolCall(id="r", name="render_video", arguments={"video_id": str(project.id), "force": force})
        result = asyncio.run(executor.execute_tool(call))
        assert result.is_error and "- scene shot: its AI shot is still being made" in result.content
    assert seen == [False, False, True, True]
    [described] = [d for d in definitions if d.name == "render_video"]
    assert "while an AI shot or a recording it plays is not ready" in described.description
    assert "A scene never previewed never holds a render" in described.description


# ── The scenes a render drew, recorded together ───────────────────────────


class _Table:
    """The video's row as the database keeps it: what was last committed, and its row lock (held until commit)."""

    def __init__(self) -> None:
        self.committed: dict | None = None
        self.lock = asyncio.Lock()


class _Transaction:
    """A read-committed transaction on :class:`_Table`: a ``FOR UPDATE`` read waits for the row's lock."""

    def __init__(self, table: _Table) -> None:
        self.table, self.holds, self.row = table, False, None

    async def get(self, model, key, *, with_for_update=False):
        assert model is VideoProject
        if with_for_update:
            await self.table.lock.acquire()
            self.holds = True
        self.row = SimpleNamespace(id=key, scene_previews=copy.deepcopy(self.table.committed))
        return self.row

    async def commit(self) -> None:
        self.table.committed = self.row.scene_previews
        if self.holds:
            self.table.lock.release()


def test_previews_finishing_together_each_keep_their_scene():
    video_id = uuid.uuid4()

    def saving(locked: bool):
        async def save(db, scene, digest):
            row = await db.get(VideoProject, video_id, **({"with_for_update": True} if locked else {}))
            row.scene_previews = render_checks.record_seen(row.scene_previews, {scene: digest}, uuid.uuid4())

        return save

    async def race(save) -> dict:
        table = _Table()

        async def finish(scene, digest):
            db = _Transaction(table)
            await save(db, scene, digest)
            await asyncio.sleep(0)  # the other preview saves meanwhile
            await db.commit()

        await asyncio.gather(finish("open", "d1"), finish("close", "d2"))
        return {scene: kept["digest"] for scene, kept in table.committed.items()}

    # Read without the lock, the last preview to save drops the other's scene ...
    assert asyncio.run(race(saving(False))) == {"close": "d2"}
    # ... under it, each keeps its own.
    assert asyncio.run(race(saving(True))) == {"open": "d1", "close": "d2"}


def test_a_render_records_its_scenes_on_the_video_row_locked():
    import inspect

    from app.services.video_studio import render

    source = inspect.getsource(render.finish_made)
    locked = source.index("project = await db.get(VideoProject, row.project_id, with_for_update=True)")
    assert locked < source.index("project.scene_previews = render_checks.record_seen(")
