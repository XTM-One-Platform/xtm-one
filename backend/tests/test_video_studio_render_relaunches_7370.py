"""A render another worker takes over says so. Synthetic rows only.

* ``record_relaunch`` adds one entry per worker change on the render's row -
  the attempt, what it resumed after, where it stood - and never writes on a
  render that ended;
* ``record_resumed_from`` adds the frame the drawing resumed from to the
  newest relaunch, once;
* ``relaunch_line`` is what ``get_video_jobs`` says; ``serialize_render``
  serves the entries; a render never relaunched says nothing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.video_studio import JOB_READY, JOB_RUNNING, VideoRender
from app.services.video_studio import jobs, projects, render_process


def _row(status: str = JOB_RUNNING, **fields) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), status=status, progress=fields.pop("progress", 40), relaunches=None, **fields)


def _store(monkeypatch, row: SimpleNamespace) -> list[dict]:
    written: list[dict] = []

    async def load(_kind, _job_id):
        return row

    async def mark_if_open(_kind, _job_id, **changes):
        if row.status != JOB_RUNNING:
            return False
        written.append(changes)
        for name, value in changes.items():
            setattr(row, name, value)
        return True

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    return written


async def test_each_worker_change_is_counted_and_says_the_frame_it_resumed_from(monkeypatch):
    row = _row(progress=56)
    written = _store(monkeypatch, row)
    after = "after a deploy (1.4.1 -> 1.4.2)"
    entry = await render_process.record_relaunch(str(row.id), resumed=after, attempt=(3, 3), frames_kept=0)
    assert entry is not None and entry["attempt"] == 3 and entry["progress"] == 56 and entry["after"] == after
    assert await render_process.record_resumed_from(str(row.id), 5183) is True
    # Once per relaunch: a later progress report never moves it.
    assert await render_process.record_resumed_from(str(row.id), 6000) is False
    row.progress = 12
    await render_process.record_relaunch(str(row.id), resumed="after its worker restarted (version 1.4.2)", attempt=(3, 3), frames_kept=0)
    assert [e.get("frames_resumed_from") for e in row.relaunches] == [5183, None]
    assert len(written) == 3
    row.relaunches[0]["drawing_after_seconds"] = 42
    assert render_process.relaunch_line(row.relaunches) == (
        # Both taken by this one build: more relaunches than builds, said.
        "Relaunched on another worker 2 times (over 1 build): attempt 3 of 3 at 56% after a deploy (1.4.1 -> 1.4.2), resumed from frame "
        "5,183 42 s after its relaunch; attempt 3 of 3 at 12% after its worker restarted (version 1.4.2)."
    )


async def test_a_render_never_relaunched_or_ended_writes_nothing(monkeypatch):
    row = _row()
    written = _store(monkeypatch, row)
    # No relaunch: the frame a first attempt starts from is no resume.
    assert await render_process.record_resumed_from(str(row.id), 0) is False
    ended = _row(status=JOB_READY)
    _store(monkeypatch, ended)
    assert await render_process.record_relaunch(str(ended.id), resumed="after a deploy", attempt=(1, 3), frames_kept=0) is None
    assert written == [] and ended.relaunches is None
    assert render_process.relaunch_line(None) == "" and render_process.relaunch_line([]) == ""


async def test_a_store_that_fails_never_fails_the_render(monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(jobs, "load", broken)
    assert await render_process.record_relaunch("r", resumed="after a deploy", attempt=None, frames_kept=0) is None
    assert await render_process.record_resumed_from("r", 10) is False


def test_the_render_row_and_the_tool_say_its_relaunches():
    from app.services.builtin_tools import video_studio as tool

    relaunches = [{"attempt": 2, "attempts": 3, "progress": 94, "after": "after its worker restarted", "frames_resumed_from": 8650}]
    render = VideoRender(id=uuid.uuid4(), project_id=uuid.uuid4(), revision=1, quality="high", status=JOB_RUNNING, relaunches=relaunches)
    assert projects.serialize_render(render)["relaunches"] == relaunches
    assert tool.stalled_line(render) == (
        "\nRelaunched on another worker 1 time: attempt 2 of 3 at 94% after its worker restarted, resumed from frame 8,650."
    )
    assert projects.serialize_render(VideoRender(id=uuid.uuid4(), project_id=uuid.uuid4(), revision=1, quality="high"))["relaunches"] == []
