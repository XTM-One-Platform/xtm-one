"""``get_video_jobs`` answers renders resumed over many attempts compact (issue #10480).

Four ended renders, each resumed across builds with 80+ kept segment runs, answered about 100,000 characters in
compact mode: every kept segment run ("frames A-B (n segments) drawn for revision R by the attempt T") in a headline,
and each headline twice (the JSON head and the Checks list). The compact answer now says the kept segments in one
summary and each headline once; the full report file and ``verbose=true`` keep every run.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace

from app.models.video_studio import JOB_FAILED, JOB_READY, VideoRender
from app.services.builtin_tools import _shared
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, projects, render_checks, render_preflight
from app.tools.base import ToolCall

READER = uuid.UUID("00000000-0000-0000-0000-00000000a480")
VIDEO = uuid.UUID("00000000-0000-0000-0000-00000000c480")
RUNS = 90


def _segments() -> dict:
    """90 kept runs of 2 segments each, drawn by 30 attempts over 3 revisions."""
    return {
        "segments": [
            {"frames": [n * 120, n * 120 + 120], "segments": 2, "revision": 10 + n % 3, "attempt": f"started 0{n % 10}:{n % 30:02d}"}
            for n in range(RUNS)
        ],
        "matched": {"document": "a1b2c3d4e5f6", "insert screen 1": "0f0e0d0c0b0a"},
    }


def _render(status: str = JOB_READY) -> VideoRender:
    checks = render_checks.Checks(audio={"status": render_checks.PASSED}, frames={"status": render_checks.PASSED}).as_dict()
    return VideoRender(
        id=uuid.uuid4(),
        project_id=VIDEO,
        user_id=READER,
        status=status,
        error=None if status == JOB_READY else "The render process was killed by signal 9.",
        quality="high",
        revision=12,
        width=1920,
        height=1080,
        fps=30,
        duration_ms=360_000,
        size_bytes=90_000_000,
        # A killed render leaves no MP4 behind.
        file_id=uuid.uuid4() if status == JOB_READY else None,
        poster_file_id=None,
        scene_id=None,
        keep=False,
        checks=checks,
        measured={"frames": 400, "seconds": 120, "pages": 2, "cpus": 4, "frames_reused": RUNS * 120, "segments_reused": _segments()},
    )


def _answer(monkeypatch, rows: list[VideoRender], **arguments):
    stored: list[dict] = []

    async def wait_for_jobs(_groups, *, timeout, told_to=None):
        return {"render": rows}, jobs.WAIT_BUDGET

    @contextlib.asynccontextmanager
    async def session():
        yield None

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=READER, role="user")

    async def visible(_db, _video_id, _user):
        return SimpleNamespace(id=VIDEO)

    async def persist(**kwargs):
        stored.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name="chat/report.md",
            source=kwargs["source"],
            description=kwargs["description"],
        )

    monkeypatch.setattr(jobs, "wait_for_jobs", wait_for_jobs)
    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    _defs, executor = tool.build_video_studio_tools(user_id=str(READER), conversation_id=str(uuid.uuid4()))
    call = ToolCall(id="j", name="get_video_jobs", arguments={"render_ids": [str(row.id) for row in rows], **arguments})
    return asyncio.run(executor.execute_tool(call)), stored


def test_the_compact_summary_counts_the_frames_segments_and_attempts():
    said = render_preflight.reused_line(_segments(), compact=True)
    assert said == f"{RUNS * 120:,} frames in {RUNS * 2} segments by 30 earlier attempts, each listed in the full report"
    assert render_preflight.reused_line({"segments": [{"frames": [0, 60], "segments": 1}]}, compact=True) == (
        "60 frames in 1 segment, each listed in the full report"
    )
    assert render_preflight.reused_line(None, compact=True) == ""


def test_four_resumed_renders_answer_compact_under_15000_characters(monkeypatch):
    rows = [_render(), _render(), _render(JOB_FAILED), _render(JOB_FAILED)]
    result, stored = _answer(monkeypatch, rows)
    assert len(result.content) < 15_000, len(result.content)
    assert "drawn for revision" not in result.content
    assert result.content.count(f"{RUNS * 120:,} frames in {RUNS * 2} segments by 30 earlier attempts") == 4
    # Each headline once per render: never in the JSON head and the Checks list both.
    assert result.content.count("Audio: passed.") == 4 and '"headlines":' not in result.content
    # The full report of each render keeps every kept segment run.
    assert len(stored) == 4
    for report in stored:
        full = report["raw_bytes"].decode("utf-8")
        assert full.count("drawn for revision") >= RUNS and "frames 0-120 (2 segments) drawn for revision 10" in full


def test_verbose_still_lists_every_kept_segment_run(monkeypatch):
    result, stored = _answer(monkeypatch, [_render()], verbose=True)
    assert stored == []
    assert "frames 10680-10800 (2 segments) drawn for revision 12" in result.content
    assert result.content.count("drawn for revision") >= RUNS
