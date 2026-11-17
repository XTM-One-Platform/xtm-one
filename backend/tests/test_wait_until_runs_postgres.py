"""Which runs and Video Studio jobs a wait may watch, on real rows.

Against a real PostgreSQL (``tests/scratch_database.py``), as a NON-admin
(rule 19): ``watched_run_statuses`` answers the person's own assignment runs,
the runs they started under another identity, company-managed runs and the
runs of flows they can see - with a terminal status in the wait's words - and
leaves out a colleague's private run, which then stays pending exactly like an
unknown id. ``watched_job_statuses`` answers the person's own AI shots and the
renders of a video they can open, and leaves out a colleague's shot and a
render of a colleague's private video. Marked ``integration``: CI's
integration job is the one with a PostgreSQL service.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.user import User
from app.models.video_studio import VideoClip, VideoProject, VideoRender
from app.services.builtin_tools.wait import make_task_condition, watched_job_statuses, watched_run_statuses
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator:
    async with scratch_database("waitruns", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _user(name: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
    )


async def test_a_wait_watches_the_runs_the_person_may_see():
    async with _database() as sessions:
        member, other = _user("member"), _user("other")
        async with sessions() as db:
            db.add_all([member, other])
            await db.flush()
            agent = Agent(id=uuid.uuid4(), user_id=other.id, name="Brief agent", slug=f"brief-{uuid.uuid4().hex[:6]}")
            db.add(agent)
            await db.flush()
            assignment = Assignment(
                id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=other.id,
                name="Daily product brief",
                trigger_type="schedule",
                trigger_config={},
                prompt="Brief.",
                enabled=True,
            )
            flow = Flow(id=uuid.uuid4(), user_id=member.id, name="Triage", graph={})
            db.add_all([assignment, flow])
            await db.flush()

            def run(**kwargs) -> AssignmentRun:
                return AssignmentRun(id=uuid.uuid4(), agent_id=agent.id, assignment_id=assignment.id, **kwargs)

            own = run(user_id=member.id, status="completed", result_summary="Sent to Sam.")
            started = run(user_id=other.id, triggered_by_user_id=member.id, status="skipped")
            private = run(user_id=other.id, status="completed", result_summary="Not yours.")
            company = run(user_id=other.id, is_company_managed=True, status="running")
            flow_run = FlowRun(
                id=uuid.uuid4(),
                flow_id=flow.id,
                user_id=member.id,
                trigger_type="manual",
                status="failed",
                error="Step 2 failed.",
            )
            db.add_all([own, started, private, company, flow_run])
            await db.commit()

        ids = [str(r.id) for r in (own, started, private, company, flow_run)]
        # An id given in another spelling is answered under that spelling.
        ids[0] = ids[0].upper()
        watched = await watched_run_statuses(ids, str(member.id))
        done, statuses = await make_task_condition(str(member.id))(ids)

    assert watched[ids[0]] == {
        "status": "completed",
        "name": "Daily product brief",
        "summary": "Sent to Sam.",
        "started_at": None,
    }
    # Started by the member, run as the assignment's identity: watched, and
    # "skipped" is the wait's "cancelled".
    assert watched[str(started.id)]["status"] == "cancelled"
    assert watched[str(company.id)]["status"] == "running"
    assert watched[str(flow_run.id)] == {
        "status": "failed",
        "name": "Triage",
        "summary": "Step 2 failed.",
        "started_at": None,
    }
    # A colleague's private run is not the member's to watch.
    assert str(private.id) not in watched

    assert done is False
    assert statuses[str(private.id)] == "unknown"
    assert statuses[str(company.id)] == "running"
    assert statuses[ids[0]] == "completed"


async def test_a_wait_watches_the_video_studio_jobs_the_person_may_read():
    async with _database() as sessions:
        member, other = _user("member"), _user("other")
        async with sessions() as db:
            db.add_all([member, other])
            await db.flush()
            mine = VideoProject(id=uuid.uuid4(), user_id=member.id, title="Cats", spec={})
            theirs = VideoProject(id=uuid.uuid4(), user_id=other.id, title="Private cut", spec={})
            db.add_all([mine, theirs])
            await db.flush()

            def clip(owner: User, **kwargs) -> VideoClip:
                return VideoClip(
                    id=uuid.uuid4(), user_id=owner.id, provider="google", model="veo", prompt="A cat.", params={}, **kwargs
                )

            def render(project: VideoProject, **kwargs) -> VideoRender:
                return VideoRender(
                    id=uuid.uuid4(), project_id=project.id, user_id=other.id, revision=91, quality="draft", **kwargs
                )

            own_shot = clip(member, name="s03", status="running")
            their_shot = clip(other, name="s04", status="ready")
            # A colleague asked for it, but the video is the member's: they can open it, so they may watch it.
            preview = render(mine, scene_id="demo", status="pending")
            hidden = render(theirs, status="ready")
            db.add_all([own_shot, their_shot, preview, hidden])
            await db.commit()

        ids = [str(job.id) for job in (own_shot, their_shot, preview, hidden)]
        watched = await watched_job_statuses(ids, str(member.id))
        done, statuses = await make_task_condition(str(member.id))(ids)

    assert watched[str(own_shot.id)] == {
        "kind": "clip",
        "status": "running",
        "name": "s03",
        "noun": "AI shot",
        "reason": None,
        "started_at": None,
    }
    assert watched[str(preview.id)]["status"] == "queued"
    assert watched[str(preview.id)]["name"] == "Cats - scene demo"
    assert watched[str(preview.id)]["noun"] == "scene preview"
    assert str(their_shot.id) not in watched
    assert str(hidden.id) not in watched

    assert done is False
    assert statuses[str(own_shot.id)] == "running"
    assert statuses[str(their_shot.id)] == "unknown"
    assert statuses[str(hidden.id)] == "unknown"
