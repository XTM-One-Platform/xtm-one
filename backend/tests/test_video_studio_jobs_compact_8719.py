"""``get_video_jobs`` answers a finished whole render compact (issue #8719).

A 150 s render with 9 screen inserts answered about 100,000 characters: every by-design and facts line, per insert,
in its JSON head and again in its text. By default the answer is now its verdict, every finding in full, the headline
lines its checks heard and watched, the counts of the rest and the file id of the full report, stored as a file the
run reads; ``verbose=true`` answers the full answer inline, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from types import SimpleNamespace

from app.models.video_studio import JOB_FAILED, JOB_READY, VideoRender
from app.services.builtin_tools import _shared
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, projects, render_answer, render_checks
from app.tools.base import ToolCall

READER = uuid.UUID("00000000-0000-0000-0000-00000000a719")
VIDEO = uuid.UUID("00000000-0000-0000-0000-00000000c719")
MP4 = uuid.UUID("00000000-0000-0000-0000-00000000d719")
REPORT = uuid.UUID("00000000-0000-0000-0000-00000000e719")


def _checks() -> dict:
    """What 9 inserts over 150 s leave: hundreds of by-design holds and notes, one finding."""
    holds = [
        {"scene": f"s{n % 9}", "seconds": 2.5, "at": n * 0.5, "end": n * 0.5 + 2.5, "because": "a statement holds its picture"}
        for n in range(300)
    ]
    halo = render_checks.Finding(
        render_checks.INSERT_HALO,
        12.0,
        13.0,
        "s3",
        "insert halo at t=0:12.00: halo - widen the key's feather",
        data={"insert": "screen 3"},
        fix="widen the key's feather",
    )
    checks = render_checks.Checks(
        audio={"status": render_checks.PASSED},
        frames={"status": render_checks.PASSED},
        findings=[halo],
    ).as_dict()
    checks["frames"]["by_design"] = holds
    checks["notes"] = [f"insert {n % 9} measured sharpness 0.{n:03d} and colours at its light, per frame" for n in range(300)]
    return checks


def _render(status: str = JOB_READY, **values) -> VideoRender:
    fields = {
        "id": uuid.uuid4(),
        "project_id": VIDEO,
        "user_id": READER,
        "status": status,
        "quality": "high",
        "revision": 3,
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "duration_ms": 150_000,
        "size_bytes": 90_000_000,
        "file_id": MP4,
        "poster_file_id": None,
        "scene_id": None,
        "keep": False,
        "checks": _checks(),
    }
    return VideoRender(**{**fields, **values})


def _answer(monkeypatch, row: VideoRender, **arguments):
    stored: list[dict] = []

    async def wait_for_jobs(_groups, *, timeout, told_to=None):
        return {"render": [row]}, jobs.WAIT_BUDGET

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
            id=REPORT,
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
    call = ToolCall(id="j", name="get_video_jobs", arguments={"render_ids": [str(row.id)], **arguments})
    return asyncio.run(executor.execute_tool(call)), stored


def _head(content: str) -> dict:
    """The render's JSON head, after the batch summary ``get_video_jobs`` opens with."""
    for line in content.splitlines():
        if line.startswith('{"render_id"'):
            return json.loads(line)
    raise AssertionError(content[:400])


def test_a_finished_render_answers_compact_with_every_finding_and_its_report_file(monkeypatch):
    result, stored = _answer(monkeypatch, _render())
    assert not result.is_error, result.content[:600]
    assert len(result.content) < 6_000, len(result.content)
    head = _head(result.content)
    assert head["status"] == render_answer.BLOCKED and [item["kind"] for item in head["findings"]] == [
        render_checks.INSERT_HALO
    ]
    assert head["findings"][0]["fix"] == "widen the key's feather" and head["findings"][0]["what"] == "insert halo at t=0:12.00: halo"
    assert head["by_design_count"] == 300 and head["facts_count"] >= 300
    assert "by_design" not in head and "facts" not in head
    assert head["mp4_file_id"] == str(MP4) and head["report_file_id"] == str(REPORT)
    # The headlines are listed once, in the text; the head counts them (issue #10480).
    assert "headlines" not in head and head["headlines_count"] >= 2
    assert result.content.count("Audio: passed.") == 1 and result.content.count("Picture: passed.") == 1
    assert "[blocks] 0:12.00-0:13.00 scene `s3` (insert_halo)" in result.content
    assert f"is file `{REPORT}`" in result.content and "verbose=true" in result.content
    # The full report is the whole answer, every by-design and facts line in it.
    [report] = stored
    full = report["raw_bytes"].decode("utf-8")
    assert report["content_type"] == "text/markdown" and len(full) > 40_000
    assert "a statement holds its picture" in full and "measured sharpness 0.299" in full


def test_verbose_answers_the_full_report_inline_and_stores_nothing(monkeypatch):
    row = _render()
    result, stored = _answer(monkeypatch, row, verbose=True)
    assert stored == []
    head = _head(result.content)
    assert len(head["by_design"]) == 300 and "report_file_id" not in head
    assert "By design (what a setting the author chose shows; no finding):" in result.content
    _compact, kept = _answer(monkeypatch, row)
    assert kept[0]["raw_bytes"].decode("utf-8") in result.content


def test_a_failed_render_answers_compact_too_with_its_reason(monkeypatch):
    # A killed render leaves no MP4 behind.
    row = _render(JOB_FAILED, error="The render process was killed by signal 9.", checks=None, file_id=None)
    result, stored = _answer(monkeypatch, row)
    # The batch answer is not an error: the failed job's own line carries its reason.
    assert len(stored) == 1, result.content[:600]
    head = _head(result.content)
    assert head["findings"][0]["kind"] == render_answer.RENDER_FAILED
    assert head["findings"][0]["what"] == "failed: The render process was killed by signal 9."


def test_a_report_that_cannot_be_stored_falls_back_to_the_full_answer(monkeypatch):
    row = _render()

    async def broken(**_kwargs):
        raise OSError("storage down")

    _answer(monkeypatch, row)
    monkeypatch.setattr(_shared, "_persist_generated_file", broken)
    _defs, executor = tool.build_video_studio_tools(user_id=str(READER), conversation_id=str(uuid.uuid4()))
    fallback = asyncio.run(
        executor.execute_tool(ToolCall(id="j", name="get_video_jobs", arguments={"render_ids": [str(row.id)]}))
    )
    head = _head(fallback.content)
    assert len(head["by_design"]) == 300 and "report_file_id" not in head
