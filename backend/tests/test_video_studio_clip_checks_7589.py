"""``get_video_jobs`` serves a made AI shot's check record.

A shot that went past ``generate_video_clip``'s inline wait was made in the
background: its frames, ``frame_qc`` measures, stabilisation, logo and text
checks were saved on the row at completion but no answer carried them.
``get_video_jobs`` on a ready or failed clip job now carries them as ``checks``,
the block the inline answer gives, read from the row and never recomputed; a
failed regeneration adds its ``failed_attempt`` (reason, file, frames).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.models.video_studio import JOB_FAILED, JOB_READY, JOB_RUNNING, VideoClip
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import clips, jobs
from app.tools.base import ToolCall

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000758901")
CLIP_ID = "00000000-0000-0000-0000-000000758902"
FIRST, MID, LAST = (f"00000000-0000-0000-0000-00000075891{n}" for n in (1, 2, 3))
FAILED_FIRST = "00000000-0000-0000-0000-000000758914"
FAILED_FILE = "00000000-0000-0000-0000-000000758915"

#: What the worker saved on the row when the shot was made.
_RECORD = {
    "first_frame_file_id": FIRST,
    "mid_frame_file_id": MID,
    "last_frame_file_id": LAST,
    "motion_ssim": 0.61,
    "camera_motion": 0.04,
    "frame_change": 0.22,
    "stabilisation": {"applied": True, "raw_motion": 0.09, "residual_motion": 0.02, "unstabilised_file_id": FAILED_FILE},
    "text_check": {"checked": True, "words": ["Filigran"], "unreadable": []},
}


@asynccontextmanager
async def _session_of(db):
    yield db


def _executor(monkeypatch, shot: VideoClip):
    async def find_user(_db, _user_id):
        return SimpleNamespace(id=USER_ID, role="user", email="maker@example.com")

    async def wait_for_jobs(asked, *, timeout, told_to=None):
        return {"clip": [shot] if asked.get("clip") else []}, jobs.WAIT_BUDGET

    async def states(kind, rows):
        return [jobs.job_state(kind, row) for row in rows]

    async def may_read(*_args, **_kwargs):
        return True

    async def file_refs(file_ids, **_kwargs):
        # Each file as a file of the conversation under its own id: the naming is tested on its own.
        named = [str(fid) for fid in file_ids if fid]
        return tool._Pictures(ids=named, named=named, copies={fid: fid for fid in named})

    monkeypatch.setattr(jobs, "wait_for_jobs", wait_for_jobs)
    monkeypatch.setattr(jobs, "job_states", states)
    monkeypatch.setattr(jobs, "may_read_job", may_read)
    monkeypatch.setattr(tool, "_file_refs", file_refs)
    monkeypatch.setattr(tool, "async_session", lambda: _session_of(SimpleNamespace()))
    monkeypatch.setattr(tool, "_user", find_user)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER_ID), None)
    return executor


def _job(monkeypatch, shot: VideoClip) -> dict:
    executor = _executor(monkeypatch, shot)
    result = asyncio.run(executor.execute_tool(ToolCall(id="j", name="get_video_jobs", arguments={"clip_ids": [CLIP_ID]})))
    assert not result.is_error, result.content
    head = json.loads(result.content.split("\n\n", 1)[0])
    return head["jobs"][0]


def test_a_shot_made_in_the_background_carries_its_saved_checks(monkeypatch):
    shot = VideoClip(
        id=uuid.UUID(CLIP_ID),
        user_id=USER_ID,
        status=JOB_READY,
        progress=100,
        model="a-video-model",
        duration_ms=10_000,
        params={"frame_qc": dict(_RECORD)},
    )
    job = _job(monkeypatch, shot)
    # The job's own fields are unchanged (added, none removed).
    assert (job["kind"], job["status"], job["id"]) == ("clip", "ready", CLIP_ID)
    checks = job["checks"]
    assert (checks["first_frame_file_id"], checks["mid_frame_file_id"], checks["last_frame_file_id"]) == (FIRST, MID, LAST)
    assert checks["frame_qc"] == {"motion_ssim": 0.61, "camera_motion": 0.04, "frame_change": 0.22}
    # The stabilisation as the inline answer says it: the un-stabilised take said kept, never named.
    assert checks["stabilisation"] == {
        "applied": True,
        "raw_motion": 0.09,
        "residual_motion": 0.02,
        "unstabilised_kept": True,
    }
    assert checks["text_check"] is not None and checks["model"] == "a-video-model"
    assert {"logo_check", "logo_tracked", "surface", "foreground_overlap", "mode"} <= checks.keys()
    assert "failed_attempt" not in job


def test_the_block_equals_what_an_inline_completion_answers(monkeypatch):
    """One helper builds both answers: the inline outcome's check keys are the job's ``checks`` keys."""
    shot = VideoClip(id=uuid.UUID(CLIP_ID), user_id=USER_ID, status=JOB_READY, model="m", params={"frame_qc": dict(_RECORD)})
    checks = _job(monkeypatch, shot)["checks"]
    # The inline answer's keys (test_video_studio_job_state._SHAPE) less the ones about the call itself.
    assert set(checks) == {
        "first_frame_file_id",
        "mid_frame_file_id",
        "last_frame_file_id",
        "frame_qc",
        "stabilisation",
        "logo_check",
        "logo_tracked",
        "text_check",
        "surface",
        "foreground_overlap",
        "mode",
        "model",
    }


def test_a_failed_regeneration_carries_its_reason_frame_and_the_checks_of_the_take_that_plays(monkeypatch):
    shot = VideoClip(
        id=uuid.UUID(CLIP_ID),
        user_id=USER_ID,
        status=JOB_READY,
        model="a-video-model",
        params={
            "frame_qc": dict(_RECORD),
            clips.KEPT_TAKE: {"prompt": "A slow orbit", "model": "a-video-model"},
            clips.ATTEMPTS: [
                {
                    "status": JOB_FAILED,
                    "reason": "camera moved 0.31 of the frame, over 0.10",
                    "file_id": FAILED_FILE,
                    "frame_qc": {"first_frame_file_id": FAILED_FIRST, "camera_motion": 0.31},
                }
            ],
        },
    )
    job = _job(monkeypatch, shot)
    failed = job["failed_attempt"]
    assert failed["reason"] == "camera moved 0.31 of the frame, over 0.10" and failed["file_id"] == FAILED_FILE
    assert failed["first_frame_file_id"] == FAILED_FIRST and failed["frame_qc"] == {"camera_motion": 0.31}
    # The take that plays keeps its own checks.
    assert job["checks"]["frame_qc"]["camera_motion"] == 0.04


def test_a_failed_shot_carries_its_reason_and_what_was_saved(monkeypatch):
    shot = VideoClip(
        id=uuid.UUID(CLIP_ID),
        user_id=USER_ID,
        status=JOB_FAILED,
        model="a-video-model",
        error="the text drifted: Orcheatration for orchestration",
        params={"frame_qc": {"first_frame_file_id": FIRST, "motion_ssim": 0.5}},
    )
    job = _job(monkeypatch, shot)
    assert job["status"] == "failed" and job["reason"]
    assert job["checks"]["first_frame_file_id"] == FIRST and job["checks"]["frame_qc"] == {"motion_ssim": 0.5}


def test_a_shot_still_running_carries_no_checks(monkeypatch):
    shot = VideoClip(id=uuid.UUID(CLIP_ID), user_id=USER_ID, status=JOB_RUNNING, progress=30, model="m", params={})
    job = _job(monkeypatch, shot)
    assert job["status"] == "running" and "checks" not in job and "failed_attempt" not in job
