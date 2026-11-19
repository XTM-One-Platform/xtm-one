"""The lab's Concierge signals and cycles: Concierge > Signals and Cycles render on real rows.

Started by ``lab.py`` (``seed_concierge_signals``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers. Without it
``GET /api/v1/concierge/signals`` answers ``[]``: a signal is written only by the signal pass
(``services/health/signals.py``) reading real traces, and no API writes one, so every review
of the Signals tab overrode ``window.fetch``. This writes the rows through the models
(``HealthSignalState``, ``HealthCycle``, ``HealthCycleRun``); no model is called and
no run is started. Arguments: the agent and the assignment the rows target, the user who owns
the lab flow, and the JSON file the ids are written to.

* a draft flow, ``Lab sample Concierge flow``, the flow target (found by name when present), with a
  real graph (a manual trigger, a stop, positions and a viewport): the builder reads
  ``graph.viewport`` and crashed on the bare ``{nodes, edges}`` this seed wrote before (#10401);
  a flow written that way by an older lab is repaired in place;
* six crossed signals (``active`` true), every kind and every target type: a cycle started
  (open on the agent, completed on the flow), refused ``cycle_open``, refused
  ``month_budget_reached``, refused with a code the frontend does not know, one with
  ``fired_at`` null and ``last_value`` null;
* the cycles, written only when the lab holds none (a restarted lab writes nothing twice):
  the two the signals started, a completed on-demand cycle on the assignment, a failed
  schedule cycle with no target and a ``budget_reached`` one.

Idempotent: a signal is keyed by (target type, target id, kind) and rewritten in place.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

FLOW_NAME = "Lab sample Concierge flow"


def flow_graph() -> dict:
    """The lab flow's canvas document, as ``normalize_graph`` stores one: positions and a viewport."""
    return {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "label": "Report received", "position": {"x": 40, "y": 40},
             "config": {"prompt": "A new threat report arrived."}},
            {"id": "st", "kind": "util.stop", "label": "Done", "position": {"x": 360, "y": 40},
             "config": {"result": "Triaged: {{t1.prompt}}"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "out", "target": "st", "target_handle": "in",
             "kind": "data"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }

#: A refusal code no ``CycleRefusal`` carries: the list must render it, not crash.
UNKNOWN_REFUSAL = "lab_unknown_refusal"

#: The refusal codes the seed covers, known and unknown.
REFUSALS: tuple[str, ...] = ("cycle_open", "month_budget_reached", UNKNOWN_REFUSAL)

#: (target type, kind, cycle: "open" | "completed" | None, refusal, fired, last_value)
SIGNALS: tuple[tuple[str, str, str | None, str | None, bool, dict | None], ...] = (
    ("agent", "feedback_drop", "open", None, True, {"value": 0.42, "baseline": 0.81, "votes": 19}),
    ("agent", "cost_drift", None, "cycle_open", True, {"value": 3.4, "baseline": 1.1, "runs": 52}),
    ("flow", "failure_spike", "completed", None, True, {"value": 0.38, "baseline": 0.05, "runs": 26}),
    ("flow", "model_change", None, "month_budget_reached", True,
     {"value": None, "baseline": None, "model": "lab-model-2", "previous_model": "lab-model"}),
    ("assignment", "failure_spike", None, UNKNOWN_REFUSAL, True, {"value": 0.5, "baseline": 0.1, "runs": 12}),
    ("assignment", "feedback_drop", None, None, False, None),
)


def plan_signals(targets: dict[str, str], now: datetime) -> list[dict]:
    """The signal rows, newest evaluation first, on the given targets (pure: unit-tested)."""
    rows: list[dict] = []
    for index, (target_type, kind, cycle, refusal, fired, last_value) in enumerate(SIGNALS):
        evaluated = now - timedelta(minutes=10 * index)
        rows.append({
            "target_type": target_type, "target_id": targets[target_type], "kind": kind, "active": True,
            "last_value": last_value, "fired_at": evaluated - timedelta(minutes=1) if fired else None,
            "cycle": cycle, "refusal": refusal, "evaluated_at": evaluated,
        })
    return rows


async def seed(agent_id: str, assignment_id: str, user_id: str) -> dict[str, object]:
    """Write the flow, the signals and (when absent) the cycles; answer their ids."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import func, select

    from app.core.database import async_session, engine
    from app.models.flow import Flow
    from app.models.health import HealthCycle
    from app.models.health_cycle import HealthCycleRun, HealthSignalState

    now = datetime.now(UTC)
    month = now.strftime("%Y-%m")
    async with async_session() as db:
        flow = (await db.execute(select(Flow).where(Flow.name == FLOW_NAME))).scalars().first()
        if flow is None:
            flow = Flow(user_id=uuid.UUID(user_id), name=FLOW_NAME, description="Seeded by the lab.",
                        graph=flow_graph(), tags=["lab"])
            db.add(flow)
            await db.flush()
        elif not isinstance((flow.graph or {}).get("viewport"), dict):
            flow.graph = flow_graph()
        targets = {"agent": agent_id, "flow": str(flow.id), "assignment": assignment_id}
        rows = plan_signals(targets, now)
        fresh = (await db.execute(select(func.count(HealthCycle.id)))).scalar_one() == 0
        cycles: list[str] = []

        def cycle(specialist: str, trigger: str, target_type: str | None, target_id: str | None,
                  status: str, opened: datetime, *, spent: int = 0, stop: str | None = None) -> HealthCycle:
            row = HealthCycle(
                specialist=specialist, trigger=trigger, target_type=target_type,
                target_id=uuid.UUID(target_id) if target_id else None, month=month, status=status,
                stop_reason=stop, spent_tokens=spent, spent_cost_usd=Decimal(spent) / Decimal(1_000_000),
                opened_at=opened, closed_at=None if status == "open" else opened + timedelta(minutes=7),
            )
            db.add(row)
            return row

        signals: list[str] = []
        for row in rows:
            state = (await db.execute(select(HealthSignalState).where(
                HealthSignalState.target_type == row["target_type"],
                HealthSignalState.target_id == uuid.UUID(row["target_id"]),
                HealthSignalState.kind == row["kind"],
            ))).scalar_one_or_none()
            if state is None:
                state = HealthSignalState(target_type=row["target_type"],
                                          target_id=uuid.UUID(row["target_id"]), kind=row["kind"])
                db.add(state)
            started = None
            if fresh and row["cycle"]:
                started = cycle("quality_analyst", "signal", row["target_type"], row["target_id"], row["cycle"],
                                row["fired_at"] or row["evaluated_at"], spent=41_250)
                await db.flush()
                db.add(HealthCycleRun(
                    cycle_id=started.id, target_type=row["target_type"], target_id=uuid.UUID(row["target_id"]),
                    signal={"kind": row["kind"], "target_type": row["target_type"],
                            "target_id": row["target_id"], **(row["last_value"] or {})},
                ))
                cycles.append(str(started.id))
            state.active, state.last_value, state.fired_at = True, row["last_value"], row["fired_at"]
            state.refusal, state.evaluated_at = row["refusal"], row["evaluated_at"]
            state.cycle_id = started.id if started is not None else state.cycle_id if row["cycle"] else None
            await db.flush()
            signals.append(str(state.id))
        if fresh:
            for args, kwargs in (
                (("orchestrator", "on_demand", "assignment", assignment_id, "completed", now - timedelta(days=2)),
                 {"spent": 88_000}),
                (("memory_steward", "schedule", None, None, "failed", now - timedelta(days=1)),
                 {"stop": "The lab's model refused the request."}),
                (("flow_lab", "schedule", None, None, "budget_reached", now - timedelta(hours=5)),
                 {"spent": 250_000, "stop": "Cycle budget reached."}),
            ):
                extra = cycle(*args, **kwargs)
                await db.flush()
                cycles.append(str(extra.id))
        await db.commit()
        answer = {"flow": str(flow.id), "signals": signals, "cycles": cycles}
    await engine.dispose()
    return answer


def main() -> None:
    agent_id, assignment_id, user_id, out = sys.argv[1:5]
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, assignment_id, user_id))))


if __name__ == "__main__":
    main()
