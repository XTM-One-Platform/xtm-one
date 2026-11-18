"""A run side of the Flow Lab's comparison, on real rows (#8260).

A flow run's tokens and cost come from its ``flow`` traces (``metadata.flow_run_id``),
its duration from its own timestamps, its intercepted and simulated writes from
level 2's report (none for a production run).
"""

from __future__ import annotations

import pytest

from app.models.flow_run import FlowRun
from app.services.health.flow_comparison import run_side
from tests.test_concierge_flow_lab_postgres import _database, _runs, _seed

pytestmark = pytest.mark.integration


async def test_a_flow_run_side_reads_its_cost_from_its_traces():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        runs = await _runs(sessions, seeded)
        async with sessions() as db:
            side = await run_side(db, "flow_run", await db.get(FlowRun, runs["other"]))
        assert (side.status, side.output, side.input_tokens, side.output_tokens, side.cost) == (
            "completed",
            "Closed.",
            100,
            40,
            0.25,
        )
        assert side.duration_ms == 1000.0
        # Not a sandbox run: nothing was intercepted.
        assert (side.intercepted_writes, side.simulated_writes) == (0, 0)
