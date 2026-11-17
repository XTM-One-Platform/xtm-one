"""Who a Video Studio job's outcome is owed to, against PostgreSQL.

The wake itself is ``test_video_studio_job_wake.py``; this one proves the rows
it reads and claims, and the path through the real tools:

* a job still queued or running keeps its conversation waiting for the last
  of the batch; the claim takes every owed job that ended ready or failed -
  never a cancelled one, never another conversation's - once, however many
  wakes race for it;
* a report to the job's own conversation clears it, a report anywhere else
  does not;
* ``render_video`` in a chat turn that ends before the render: the render
  finishing resumes the conversation with ``get_video_jobs``' words for it,
  the player marker included; a turn that polled it to the end owes nothing.

``integration``-marked and skips when PostgreSQL is unreachable; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.user import User
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_READY,
    JOB_RUNNING,
    VideoClip,
    VideoMusic,
    VideoProject,
    VideoRender,
    VideoTake,
)
from app.services import parent_wake
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, wake
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration]


async def _seed(db) -> tuple[User, VideoProject]:
    owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
    db.add(owner)
    await db.flush()
    project = VideoProject(
        user_id=owner.id,
        title="Launch film",
        spec={"title": "Launch film", "freestyle": True, "scenes": [{"type": "title", "title": "Launch"}]},
    )
    db.add(project)
    await db.flush()
    return owner, project


def _clip(owner: User, status: str, owed: uuid.UUID | None) -> VideoClip:
    return VideoClip(
        user_id=owner.id,
        provider="openai",
        model="sora-2",
        prompt="A slow orbit",
        status=status,
        owed_conversation_id=owed,
    )


async def test_the_last_job_of_a_conversation_claims_every_ended_one_once() -> None:
    async with scratch_database(
        "video_wake", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        conversation, elsewhere = uuid.uuid4(), uuid.uuid4()
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            render = VideoRender(
                project_id=project.id,
                user_id=owner.id,
                revision=1,
                quality="high",
                status=JOB_READY,
                owed_conversation_id=conversation,
            )
            running_clip = _clip(owner, JOB_RUNNING, conversation)
            failed_music = VideoMusic(
                user_id=owner.id,
                provider="google",
                model="lyria-2",
                prompt="A calm bed",
                status=JOB_FAILED,
                owed_conversation_id=conversation,
            )
            cancelled_clip = _clip(owner, JOB_CANCELLED, conversation)
            other_take = VideoTake(
                user_id=owner.id, name="Demo", script={"steps": []}, status=JOB_READY, owed_conversation_id=elsewhere
            )
            db.add_all([render, running_clip, failed_music, cancelled_clip, other_take])
            await db.commit()
            ids = {
                "render": str(render.id),
                "clip": str(running_clip.id),
                "music": str(failed_music.id),
                "cancelled": str(cancelled_clip.id),
                "take": str(other_take.id),
            }

        # A shot of the batch still runs: the render that ended waits for it.
        assert await jobs.owed_jobs_in_flight(conversation) is True
        await jobs.mark("clip", ids["clip"], status=JOB_READY, progress=100)
        assert await jobs.owed_jobs_in_flight(conversation) is False

        first, second = await asyncio.gather(jobs.claim_owed_jobs(conversation), jobs.claim_owed_jobs(conversation))
        claimed = first or second
        assert not (first and second), "two wakes never both claim"
        assert {kind: [job_id for job_id, _at in taken] for kind, taken in claimed.items()} == {
            "render": [ids["render"]],
            "clip": [ids["clip"]],
            "music": [ids["music"]],
        }
        assert all(isinstance(at, datetime) for taken in claimed.values() for _id, at in taken)
        assert await jobs.claim_owed_jobs(conversation) == {}
        # Another conversation's job is still owed to it.
        take = await jobs.load("take", ids["take"])
        assert take.owed_conversation_id == elsewhere


async def test_a_report_clears_only_its_own_conversation() -> None:
    async with scratch_database(
        "video_wake_told", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        conversation = uuid.uuid4()
        async with scratch.sessions() as db:
            owner, _project = await _seed(db)
            ended = _clip(owner, JOB_READY, conversation)
            db.add(ended)
            await db.commit()
            clip_id = str(ended.id)

        row = await jobs.load("clip", clip_id)
        await jobs.mark_told("clip", [row], uuid.uuid4())
        assert (await jobs.load("clip", clip_id)).owed_conversation_id == conversation
        await jobs.mark_told("clip", [row], conversation)
        assert row.owed_conversation_id is None
        assert (await jobs.load("clip", clip_id)).owed_conversation_id is None


async def test_what_a_conversation_waits_on_and_what_a_lost_wake_left_owed() -> None:
    """The reads behind the awaited-work banner and the lost-wake sweep."""
    async with scratch_database(
        "video_wake_owed", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        conversation, lost, recent, elsewhere = (uuid.uuid4() for _ in range(4))
        now = datetime.now(UTC)
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            running_render = VideoRender(
                project_id=project.id,
                user_id=owner.id,
                revision=1,
                quality="high",
                status=JOB_RUNNING,
                owed_conversation_id=conversation,
                created_at=now - timedelta(minutes=3),
            )
            queued_clip = _clip(owner, JOB_PENDING, conversation)
            queued_clip.name = "Cows at the airport"
            queued_clip.created_at = now - timedelta(minutes=1)
            ended_clip = _clip(owner, JOB_READY, conversation)
            ended_clip.finished_at = now - timedelta(minutes=10)
            other_take = VideoTake(
                user_id=owner.id, name="Demo", script={"steps": []}, status=JOB_RUNNING, owed_conversation_id=elsewhere
            )
            lost_music = VideoMusic(
                user_id=owner.id,
                provider="google",
                model="lyria-2",
                prompt="A calm bed",
                status=JOB_FAILED,
                owed_conversation_id=lost,
                finished_at=now - timedelta(minutes=10),
            )
            recent_clip = _clip(owner, JOB_READY, recent)
            recent_clip.finished_at = now - timedelta(seconds=5)
            aged_clip = _clip(owner, JOB_READY, uuid.uuid4())
            aged_clip.finished_at = now - timedelta(days=3)
            db.add_all([running_render, queued_clip, ended_clip, other_take, lost_music, recent_clip, aged_clip])
            await db.commit()
            render_id, clip_id = running_render.id, queued_clip.id

        pending = await jobs.owed_jobs_pending(conversation)
        assert [(kind, row.id, name) for kind, row, name in pending] == [
            ("render", render_id, "Launch film"),
            ("clip", clip_id, "Cows at the airport"),
        ]

        owed = await jobs.conversations_owed_ended_jobs(
            ended_before=now - timedelta(minutes=2), ended_after=now - timedelta(days=1), limit=10
        )
        assert dict(owed) == {conversation: owner.id, lost: owner.id}


@pytest.fixture
def worker(monkeypatch):
    """No worker to wake, no wait inside the call, and the wakes recorded instead of run."""
    woken: list[dict] = []

    async def nothing(*_args, **_kwargs):
        return None

    async def record(**kwargs):
        woken.append(kwargs)

    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    monkeypatch.setattr(parent_wake, "wake_conversation", record)
    return woken


async def _render_in_chat(owner: User, project: VideoProject, conversation: uuid.UUID) -> tuple[str, str]:
    _defs, executor = tool.build_video_studio_tools(user_id=str(owner.id), conversation_id=str(conversation))
    answer = await executor.execute_tool(
        ToolCall(id="r", name="render_video", arguments={"video_id": str(project.id), "quality": "high"})
    )
    assert "is queued" in answer.content, answer.content
    assert "this conversation is resumed with the outcome" in answer.content
    render_id = answer.content.split("`")[1]
    return render_id, answer.content


async def test_a_render_ending_after_the_turn_resumes_the_conversation(worker) -> None:
    async with scratch_database(
        "video_wake_render", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        conversation = uuid.uuid4()
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            await db.commit()

        render_id, _answer = await _render_in_chat(owner, project, conversation)
        assert (await jobs.load("render", render_id)).owed_conversation_id == conversation
        # The turn has ended; the worker finishes the render minutes later.
        await jobs.mark(
            "render", render_id, status=JOB_READY, progress=100, width=1920, height=1080, fps=30, duration_ms=42_000
        )

        assert await wake.wake_owed_conversation("render", render_id) is True

        [woken] = worker
        assert woken["conversation_id"] == str(conversation)
        assert woken["user_id"] == str(owner.id)
        text = woken["wake_text"]
        assert "1 render" in text
        assert f"Render `{render_id}` is ready: 42.0 s, 1920x1080 at 30 fps" in text
        assert f"[[VIDEO:{project.id}]]" in text
        assert (await jobs.load("render", render_id)).owed_conversation_id is None
        # Told once: the next wake finds nothing.
        assert await wake.wake_owed_conversation("render", render_id) is False
        assert len(worker) == 1


async def test_a_render_the_turn_polled_to_the_end_owes_nothing(worker) -> None:
    async with scratch_database(
        "video_wake_polled", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        conversation = uuid.uuid4()
        async with scratch.sessions() as db:
            owner, project = await _seed(db)
            await db.commit()

        render_id, _answer = await _render_in_chat(owner, project, conversation)
        await jobs.mark("render", render_id, status=JOB_READY, progress=100)
        _defs, executor = tool.build_video_studio_tools(user_id=str(owner.id), conversation_id=str(conversation))
        polled = await executor.execute_tool(
            ToolCall(id="p", name="get_video_jobs", arguments={"render_ids": [render_id]})
        )
        assert f"Render `{render_id}` is ready" in polled.content

        assert await wake.wake_owed_conversation("render", render_id) is False
        assert worker == []
        assert (await jobs.load("render", render_id)).owed_conversation_id is None
