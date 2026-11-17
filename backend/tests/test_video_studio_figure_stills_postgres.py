"""The editor's figure stills, drawn by the worker in a process of its own, against a scratch Postgres.

The worker's adapter starts ``python -m app.services.video_studio.figure_stills
<execution id>`` under the render's supervisor: the process reads the stills
to draw from its own execution row and reports its end on its standard output.
"""

from __future__ import annotations

import uuid

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.orchestration import OrchestrationExecution
from app.services.orchestration_engine import create_execution
from app.services.video_studio import figure_stills
from tests.scratch_database import scratch_database

pytestmark = [pytest.mark.integration]


async def test_the_stills_process_reads_its_own_job_and_reports_how_it_ended(monkeypatch) -> None:
    async with scratch_database("video_figure_stills", tables=(OrchestrationExecution.__table__,)) as scratch:
        async with scratch.sessions() as db:
            payload = {"user_id": str(uuid.uuid4()), "role": "user", "stills": []}
            execution = await create_execution(db, adapter=figure_stills.ADAPTER, payload=payload, max_attempts=1)
            await db.commit()
        # The process connects where the worker does: here, the scratch database.
        monkeypatch.setenv("DATABASE_URL", scratch.url)
        ended = await figure_stills.run_isolated(execution.id)
        assert (ended.kind, ended.returncode) == ("done", 0), ended
        # A job that names no execution the process can read ends the same way: nothing to draw.
        ended = await figure_stills.run_isolated(uuid.uuid4())
        assert ended.kind == "done", ended
