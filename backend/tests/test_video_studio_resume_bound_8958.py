"""A relaunched render says how long it took to draw again, against a stated bound (issue #8958). Synthetic rows only.

* ``record_resumed_from`` keeps, on the newest relaunch, the seconds from the relaunch to the first frame drawn;
* past ``RESUME_TO_DRAWING_BOUND_SECONDS`` the line ``get_video_jobs`` reads says the bound was passed;
* a relaunch with no readable time keeps no figure and its line stays as before.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.video_studio import JOB_RUNNING
from app.services.video_studio import jobs, render_process


def _store(monkeypatch, row: SimpleNamespace) -> None:
    async def load(_kind, _job_id):
        return row

    async def mark_if_open(_kind, _job_id, **changes):
        for name, value in changes.items():
            setattr(row, name, value)
        return True

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)


def _relaunched(seconds_ago: float | None) -> SimpleNamespace:
    at = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat() if seconds_ago is not None else None
    entry = {"at": at, "attempt": 1, "attempts": 3, "after": "after a deploy", "progress": 23, "frames_kept": 900}
    return SimpleNamespace(id=uuid.uuid4(), status=JOB_RUNNING, progress=23, relaunches=[entry])


async def test_a_resume_within_the_bound_says_how_long_it_took(monkeypatch):
    row = _relaunched(40)
    _store(monkeypatch, row)
    assert await render_process.record_resumed_from(str(row.id), 900) is True
    took = row.relaunches[-1]["drawing_after_seconds"]
    assert 40 <= took < render_process.RESUME_TO_DRAWING_BOUND_SECONDS
    line = render_process.relaunch_line(row.relaunches)
    assert f"resumed from frame 900 {took} s after its relaunch." in line
    assert "bound" not in line


async def test_a_resume_past_the_bound_says_so(monkeypatch):
    row = _relaunched(render_process.RESUME_TO_DRAWING_BOUND_SECONDS + 120)
    _store(monkeypatch, row)
    assert await render_process.record_resumed_from(str(row.id), 900) is True
    took = row.relaunches[-1]["drawing_after_seconds"]
    assert took > render_process.RESUME_TO_DRAWING_BOUND_SECONDS
    assert render_process.relaunch_line(row.relaunches).endswith(
        f"resumed from frame 900 {took} s after its relaunch (past its 5 min bound)."
    )


async def test_a_relaunch_with_no_time_keeps_no_figure(monkeypatch):
    row = _relaunched(None)
    _store(monkeypatch, row)
    assert await render_process.record_resumed_from(str(row.id), 900) is True
    assert "drawing_after_seconds" not in row.relaunches[-1]
    assert render_process.relaunch_line(row.relaunches).endswith("resumed from frame 900.")


def test_the_bound_is_five_minutes():
    # Stated in the issue's acceptance: a resume reaches drawing within this bound; a change says so in its PR.
    assert render_process.RESUME_TO_DRAWING_BOUND_SECONDS == 300
