"""The lab's evaluation runs: one completed and one running on the sample evaluation.

Started by ``lab.py`` (``seed_evaluation_runs``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: a run is only
finished by a real model, so this script writes ``evaluation_runs`` rows (and the completed
run's one ``evaluation_results`` row) through the platform's own model. Arguments: the
evaluation, the lab agent, the dataset and the JSON file the run ids are written to.

* ``completed``: headlines set (Overall Quality, Overall Performance, the score), one scored
  result on the dataset's first entry, so the run detail, its results table and the run search
  render a finished run;
* ``running``: half its entries done, no score, so the run detail shows its headline cells in
  their running state. Its ``owner_execution_id`` names no execution row: the stranded-run sweep
  (``sweep_stranded_runs``) selects an owned run only through its execution's terminal clock,
  so a run whose execution row does not exist is never failed and stays ``running``.

Lab-only sample figures, never production data. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

MODEL = "lab-model"
QUALITY_KEY = "prose.token_prf"


def runs(agent_id: str) -> list[dict[str, object]]:
    """The two runs, completed first: plain values, no ORM object (pure: unit-tested)."""
    agent = uuid.UUID(agent_id)
    return [
        {
            "agent_id": agent, "model_id": MODEL, "is_default_model": True, "status": "completed",
            "trigger_reason": "manual", "score": 82.0, "quality_score": 82.0, "performance_score": 100.0,
            "headline_details": {
                "quality_metric_keys": [QUALITY_KEY], "performance_metric_keys": [],
                "metric_entry_counts": {QUALITY_KEY: 1}, "quality_contributions": {QUALITY_KEY: 82.0},
                "excluded": [],
            },
            "execution_mode": "llm_only", "apply_prompt_guards": False, "require_tool_approval": False,
            "duration_seconds": 12.4, "completed_entries": 1, "total_entries": 1,
            "age_minutes": 90, "minutes_to_finish": 2, "result_score": 82.0,
        },
        {
            "agent_id": agent, "model_id": MODEL, "is_default_model": True, "status": "running",
            "trigger_reason": "manual", "score": None, "quality_score": None, "performance_score": None,
            "headline_details": None, "execution_mode": "llm_only", "apply_prompt_guards": False,
            "require_tool_approval": False, "duration_seconds": None, "completed_entries": 1,
            "total_entries": 2, "age_minutes": 5, "minutes_to_finish": None, "result_score": None,
            # An execution id with no execution row: no sweep ever ends this run.
            "owner_execution_id": uuid.uuid5(uuid.NAMESPACE_URL, "lab-evaluation-running-run"),
        },
    ]


async def seed(evaluation_id: str, agent_id: str, dataset_id: str) -> list[str]:
    """Write the two runs; answer their ids, completed first."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.dataset import DatasetEntry
    from app.models.evaluation import EvaluationResult, EvaluationRun

    now = datetime.now(UTC)
    written = []
    async with async_session() as db:
        entry = (
            await db.execute(
                select(DatasetEntry.id, DatasetEntry.expected_answer)
                .where(DatasetEntry.dataset_id == uuid.UUID(dataset_id))
                .order_by(DatasetEntry.position)
                .limit(1)
            )
        ).first()
        for spec in runs(agent_id):
            started = now - timedelta(minutes=int(spec["age_minutes"]))
            finish = spec["minutes_to_finish"]
            run = EvaluationRun(
                evaluation_id=uuid.UUID(evaluation_id),
                agent_id=spec["agent_id"], model_id=spec["model_id"], is_default_model=spec["is_default_model"],
                status=spec["status"], trigger_reason=spec["trigger_reason"], score=spec["score"],
                quality_score=spec["quality_score"], performance_score=spec["performance_score"],
                headline_details=spec["headline_details"], execution_mode=spec["execution_mode"],
                apply_prompt_guards=spec["apply_prompt_guards"],
                require_tool_approval=spec["require_tool_approval"],
                duration_seconds=spec["duration_seconds"], completed_entries=spec["completed_entries"],
                total_entries=spec["total_entries"], owner_execution_id=spec.get("owner_execution_id"),
                tags={"lab": "sample"}, created_at=started, started_at=started,
                completed_at=started + timedelta(minutes=int(finish)) if finish is not None else None,
            )
            db.add(run)
            await db.flush()
            if entry is not None and spec["result_score"] is not None:
                db.add(EvaluationResult(
                    run_id=run.id, entry_id=entry.id, agent_output=entry.expected_answer,
                    score=spec["result_score"], sub_scores={"response_accuracy": spec["result_score"]},
                    evaluator_reasoning="Seeded by the lab: the answer names the expected model.",
                    duration_seconds=spec["duration_seconds"], created_at=started,
                ))
            written.append(run)
        await db.commit()
        ids = [str(run.id) for run in written]
    await engine.dispose()
    return ids


def main() -> None:
    evaluation_id, agent_id, dataset_id, out = sys.argv[1:5]
    Path(out).write_text(json.dumps(asyncio.run(seed(evaluation_id, agent_id, dataset_id))))


if __name__ == "__main__":
    main()
