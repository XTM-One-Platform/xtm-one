"""The gate decision records of one CTEM case, for ``[service.lab-ctem-screens]`` (#1628).

Started by ``lab.py`` (``seed_ctem_gates``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once ``seed_ctem_screens`` has read the cases back. The case
page's gate cards (``GET /ctem-cases/{id}`` ``gates``, #1518) join each gate's work item, its
``decisions`` row and the case's ``gate`` events; the recompute writes none of them and no API
writes a decision row in a chosen state, so a UX round seeded them by hand through lab-sql. This
writes them through the platform's own models on the first case, one gate per record status:

* ``ctem.analyst_review`` answered from a Teams card (option, comment, the case's next step);
* ``ctem.validation.non_readonly`` waiting for the administrator, with a deadline (the item
  turns ``needs_human`` on, so the platform's own hook opens its decision);
* ``ctem.remediation.decision`` expired at its deadline;
* ``ctem.mobilize`` cancelled;
* ``ctem.accept_risk`` passed automatically by a pre-approved runbook (an event, no decision).

The ``gate`` events go through ``ctem_case_loop.record_gate``, the loop's own writer. A restart
finds the gate work items by their correlation keys and writes nothing. Arguments: the
administrator's id, the case id and the JSON file the answer is written to (gate key -> status).

dev-docs/reference/agentic-labs.md, "The xtm-one lab". Lab-only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The orchestrator the gate items are asked by (``asked_by_agent`` on the card), when seeded.
ASKED_BY_SLUG = "ctem-vuln-prioritization-orchestrator"
#: (gate key, correlation key prefix, record status) of every gate this seeds with a work item.
GATES: tuple[tuple[str, str, str], ...] = (
    ("ctem.analyst_review", "ctem:review:", "answered"),
    ("ctem.validation.non_readonly", "ctem:validation:", "waiting"),
    ("ctem.remediation.decision", "ctem:remediation:", "expired"),
    ("ctem.mobilize", "ctem:f1:case-mobilize:", "cancelled"),
)
#: The gate passed automatically (no work item, no decision): the policy card.
POLICY_GATE = ("ctem.accept_risk", "pre-approved runbook Accept low residual risk (lab)")
OPTIONS = [
    {"key": "A", "label": "Confirm the case and continue", "effect": "the loop moves to validation"},
    {"key": "B", "label": "Send it back to triage", "effect": "the case returns to analysis"},
]


def ask(gate: str, deadline: datetime | None) -> dict:
    """The structured ask of the gate's work item (``work_item.ask``)."""
    body = {
        "question": f"Lab sample: decide gate {gate} for this case?",
        "context": "Seeded by the lab so the case page renders every gate record state.",
        "options": OPTIONS,
        "recommendation": "A",
    }
    if deadline is not None:
        body["deadline"] = deadline.isoformat()
    return body


def plan(case_id: str, now: datetime) -> list[dict]:
    """One row per gate work item: correlation key, status, times, decision state, events to write."""
    rows = []
    for index, (gate, prefix, status) in enumerate(GATES):
        asked = now - timedelta(hours=6 - index)
        row = {
            "gate": gate, "key": prefix + case_id, "record": status, "asked_at": asked,
            "deadline": None, "events": [("waited", asked, "a person", "")],
        }
        if status == "answered":
            answered = asked + timedelta(minutes=42)
            row["answered_at"] = answered
            row["events"].append(("answered", answered, "{admin}", "A"))
        elif status == "waiting":
            row["deadline"] = now + timedelta(days=1)
        elif status == "expired":
            row["deadline"] = asked + timedelta(hours=1)
        rows.append(row)
    return rows


async def seed(admin_id: str, case_id: str) -> dict[str, str]:
    """Write the gate items, decisions and events; answer gate key -> record status (empty on a restart)."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.ctem_case import CtemCase
    from app.models.decision import Decision, DecisionEvent
    from app.models.work_item import WorkItem
    from app.services.ctem_case_loop import record_gate

    admin = uuid.UUID(admin_id)
    now = datetime.now(UTC)
    rows = plan(case_id, now)
    async with async_session() as db:
        keys = [row["key"] for row in rows]
        found = (await db.execute(select(WorkItem.id).where(WorkItem.correlation_key.in_(keys)))).first()
        if found is not None:
            await engine.dispose()
            return {}
        case = await db.get(CtemCase, uuid.UUID(case_id))
        if case is None:
            raise SystemExit(f"case {case_id} not found")
        agent = (await db.execute(select(Agent.id).where(Agent.slug == ASKED_BY_SLUG))).scalar()
        written: dict[str, str] = {}
        for row in rows:
            status = row["record"]
            waiting = status == "waiting"
            item = WorkItem(
                id=uuid.uuid4(), user_id=admin, owner_agent_id=agent, created_by_agent_id=agent,
                title=f"Lab sample gate {row['gate']}", correlation_key=row["key"], priority="high",
                status={"answered": "done", "waiting": "blocked", "expired": "cancelled"}.get(status, "cancelled"),
                needs_human=waiting, waits_for_user_id=admin if waiting else None,
                blocked_reason="Waits for the administrator's decision" if waiting else None,
                ask=ask(row["gate"], row["deadline"]), payload={"seeded_by": "lab"},
                created_at=row["asked_at"], last_activity_at=row["asked_at"],
            )
            db.add(item)
            if not waiting:
                # A waiting item opens its decision through the platform's hook; the others are past states.
                decision = Decision(
                    id=uuid.uuid4(), source_type="work_item", source_id=item.id, kind="work_item",
                    ask=item.ask, recipient=f"user:{admin}", status=status, deadline=row["deadline"],
                    created_at=row["asked_at"], updated_at=row.get("answered_at") or row["asked_at"],
                )
                if status == "answered":
                    decision.answer = {"option": "A", "label": OPTIONS[0]["label"],
                                       "text": "Confirmed: the exposed assets match the scope."}
                    decision.answered_by = admin
                    decision.answered_at = row["answered_at"]
                    decision.surface = "teams_card"
                db.add(decision)
                db.add(DecisionEvent(decision_id=decision.id, event="asked", detail={"kind": "work_item"},
                                     created_at=row["asked_at"]))
                db.add(DecisionEvent(
                    decision_id=decision.id, event=status, actor_id=admin if status == "answered" else None,
                    surface=decision.surface, detail=decision.answer,
                    created_at=row.get("answered_at") or row["deadline"] or row["asked_at"],
                ))
            for outcome, at, by, option in row["events"]:
                record_gate(db, case, key=row["gate"], outcome=outcome, by=by.replace("{admin}", admin_id),
                            option=option, now=at)
            written[row["gate"]] = status
        record_gate(db, case, key=POLICY_GATE[0], outcome="passed_automatically", by=POLICY_GATE[1],
                    now=now - timedelta(minutes=30))
        written[POLICY_GATE[0]] = "passed_automatically"
        await db.commit()
    await engine.dispose()
    return written


def main() -> None:
    admin_id, case_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id, case_id))))


if __name__ == "__main__":
    main()
