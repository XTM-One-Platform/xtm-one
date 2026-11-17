"""The lab's Concierge improvement proposals: the Improvements inbox renders on real rows.

Started by ``lab.py`` (``seed_concierge_improvements``) with the backend's interpreter, from
``backend/`` and with the backend's environment, after ``seed_concierge_signals.py``. Without it
``GET /api/v1/concierge/improvements`` answers ``{total: 0}``: a proposal is filed only by the
Experimenter at the end of a measured cycle, so every review of the inbox
(``/concierge?tab=improvements``), of the agent page's Concierge card and of the flow builder's
Concierge panel overrode ``window.fetch``. This writes the rows through the model
(``ImprovementProposal``); no model is called, no experiment runs and no target changes.

Arguments: the agent, the two assignments (the Concierge target and a second one), the lab
Concierge flow, the user who owns the targets, and the JSON file the ids are written to.

* every ``proposal_type`` on its own target type (persona and new flow on the agent, skill on
  two lab skills, assignment prompt and agent-to-flow on the assignments, flow repair and flow
  optimization on the Concierge flow and a second lab flow);
* every status, every ``experiment_verdict`` and a human request (verdict ``NULL``);
* open rows on distinct targets (the table's one-open-change-per-target index), the pending ones
  decidable by the administrator who owns the targets, their ``base_content`` the target's
  current text so Approve does not answer "changed since";
* long summaries (over 200 characters) and one ``NULL`` summary.

Idempotent: a lab that already holds its seeded proposals (``finding.lab_seed``) writes nothing
and answers their ids.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SKILL_NAMES = ("Lab Concierge skill: triage", "Lab Concierge skill: enrichment")
SKILL_CONTENT = (
    "# Triage\n\nRead the report.\nRank it by severity.\nAnswer with the top three actions.\n",
    "# Enrichment\n\nLook the indicators up.\nAdd the sightings.\nCite every source.\n",
)
SECOND_FLOW_NAME = "Lab Concierge flow: enrichment"

_LONG = (
    "The persona asks for a ranked answer but never says how to rank; on the last 52 runs the "
    "feedback dropped from 0.81 to 0.42 on reports with more than ten indicators. The rewrite "
    "states the ranking rule (severity, then confidence, then recency) and keeps every other line."
)
_LONG_FLOW = (
    "The enrichment branch fails on 38% of the runs since the model change: the parse node reads "
    "a field the new model no longer returns. The repair reads the field through a fallback and "
    "routes the empty case to a stop node with an explicit message instead of the error path."
)

#: (proposal type, target key, status, experiment verdict, summary). Target keys:
#: agent, skill1, skill2, assignment, assignment2, flow (the Concierge flow), flow2.
#: A verdict ``None`` is a person's request (no author agent).
PLAN: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    # Open, one per target (new_flow is outside the index).
    ("persona", "agent", "pending_approval", "progression", _LONG),
    ("skill", "skill1", "pending_approval", "neutral", "Shorten the triage steps to three lines."),
    ("assignment_prompt", "assignment", "pending_approval", "unmeasured",
     "Name the output format in the prompt: a table of the three actions with an owner each."),
    ("flow_repair", "flow", "pending_approval", "progression", _LONG_FLOW),
    ("new_flow", "agent", "pending_approval", "unmeasured", None),
    ("new_flow", "agent", "pending_approval", "progression",
     "Automate the weekly report digest the agent writes by hand every Monday."),
    ("skill", "skill2", "watching", "progression", "Cite the source of every sighting."),
    ("agent_to_flow", "assignment2", "shadow", "unmeasured",
     "Hand the assignment over to a flow: the same steps every run, no reasoning needed."),
    ("flow_optimization", "flow2", "applying", "progression", "Run the two lookups in parallel."),
    # Closed.
    ("persona", "agent", "kept", "progression", "Answer in the reader's language."),
    ("persona", "agent", "rolled_back", "regression", "Drop the closing summary."),
    ("persona", "agent", "rejected", "neutral", "Use a friendlier tone."),
    ("persona", "agent", "regression_kept", "progression", _LONG),
    ("persona", "agent", "failed", None, "Make the answers shorter."),
    ("skill", "skill1", "kept", "progression", "Rank by severity first."),
    ("skill", "skill1", "rejected", "regression", "Add a fourth action."),
    ("skill", "skill2", "rolled_back", "neutral", "Group the sightings by source."),
    ("assignment_prompt", "assignment", "kept", "progression", "Say which reports to skip."),
    ("assignment_prompt", "assignment", "rejected", "unmeasured", "Ask for a longer answer."),
    ("assignment_prompt", "assignment2", "failed", None, "Name the report feed."),
    ("flow_repair", "flow", "kept", "progression", "Route the parse error to a stop node."),
    ("flow_repair", "flow", "rejected", "regression", "Retry the parse three times."),
    ("flow_repair", "flow", "rolled_back", "neutral", _LONG_FLOW),
    ("flow_optimization", "flow", "kept", "progression", "Skip the lookup for known indicators."),
    ("flow_optimization", "flow2", "failed", "unmeasured", "Cache the lookups for an hour."),
    ("flow_optimization", "flow2", "regression_kept", "neutral", "Batch the lookups by ten."),
    ("new_flow", "agent", "kept", "progression", "Automate the daily feed check."),
    ("new_flow", "agent", "rejected", "unmeasured", "Automate the answer to every mention."),
    ("agent_to_flow", "assignment2", "kept", "progression", "Hand the nightly sweep over to a flow."),
    ("agent_to_flow", "assignment", "rolled_back", "regression", "Hand the triage over to a flow."),
)

TARGET_TYPE = {"agent": "agent", "skill1": "skill", "skill2": "skill", "assignment": "assignment",
               "assignment2": "assignment", "flow": "flow", "flow2": "flow"}
OPEN = ("pending_approval", "applying", "shadow", "watching")
FLOW_TYPES = ("flow_repair", "flow_optimization", "new_flow", "agent_to_flow")


def _graph(label: str) -> dict:
    return {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "label": "Report received", "position": {"x": 40, "y": 40},
             "config": {"prompt": "A new threat report arrived."}},
            {"id": "st", "kind": "util.stop", "label": label, "position": {"x": 360, "y": 40},
             "config": {"result": f"{label}: {{{{t1.prompt}}}}"}},
        ],
        "edges": [{"id": "e1", "source": "t1", "source_handle": "out", "target": "st", "target_handle": "in",
                   "kind": "data"}],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def _graph_text(graph: dict) -> str:
    """The graph as its nodes read, one line each: the diff a flow proposal shows."""
    return "\n".join(f"{node['id']} {node['kind']}: {node.get('label', '')}" for node in graph["nodes"]) + "\n"


def _proposed(base: str, index: int) -> str:
    lines = base.splitlines() or [""]
    lines[-1] = f"{lines[-1]} (revised {index})".strip()
    return "\n".join([*lines, f"Rule {index}: rank by severity, then confidence, then recency."]) + "\n"


def plan_proposals(targets: dict[str, str], texts: dict[str, str], admin: str, agent: str,
                   now: datetime) -> list[dict]:
    """The proposal rows, newest first (pure: unit-tested).

    *targets* maps a target key to its id, *texts* a text target key to its current text.
    """
    rows: list[dict] = []
    for index, (proposal_type, key, status, verdict, summary) in enumerate(PLAN):
        created = now - timedelta(hours=3 * index + 1)
        target_type = TARGET_TYPE[key]
        row: dict = {
            "proposal_type": proposal_type, "target_type": target_type, "target_id": targets[key],
            "agent_id": agent if target_type in ("agent", "assignment") else None,
            "status": status, "summary": summary, "experiment_verdict": verdict,
            # sha256, 64 characters: the column's width (services/improvement_proposals/dedup.py).
            "dedup_key": hashlib.sha256(f"lab-seed|{index}|{proposal_type}|{targets[key]}".encode()).hexdigest(),
            "finding": {"lab_seed": True, "fingerprint": f"lab-seed-{index:02d}",
                        "pattern": "Seeded by the lab."},
            "evidence_refs": [{"kind": "trace", "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"lab-trace-{index}"))}],
            "experiment_refs": [], "experiment": {}, "change": {},
            "author_agent_id": agent if verdict is not None else None,
            "requested_by_user_id": admin if verdict is None else None,
            "created_at": created,
        }
        if proposal_type in FLOW_TYPES:
            graph = _graph(f"Seeded variant {index}")
            row["change"] = {"sandbox_flow_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"lab-sandbox-{index}")),
                             "graph": graph}
            if proposal_type == "new_flow":
                row["change"]["name"] = f"Lab automation {index}"
            row["base_content"] = _graph_text(_graph("Done")) if proposal_type != "new_flow" else ""
            row["proposed_content"] = _graph_text(graph)
            row["experiment_refs"] = [{"kind": "sandbox_copy", "id": row["change"]["sandbox_flow_id"]}]
        else:
            base = texts.get(key, "") if status in OPEN else f"Earlier text {index}.\nRead the report.\n"
            row["base_content"], row["proposed_content"] = base, _proposed(base, index)
        if verdict is not None and verdict != "unmeasured":
            baseline = 0.62
            delta = {"progression": 0.14, "neutral": 0.01, "regression": -0.11}[verdict]
            row["experiment"] = {"quality": round(baseline + delta, 2), "baseline_quality": baseline,
                                 "cost_usd": 0.042, "latency_ms": 5800, "simulated_writes": index % 4}
        elif verdict == "unmeasured":
            row["experiment"] = {"reason": "No evaluation covers this target yet."}
        if status != "pending_approval":
            row["decided_at"] = created + timedelta(minutes=30)
            row["decided_by_user_id"] = admin
        if status in ("watching", "kept", "rolled_back", "regression_kept", "applying", "shadow"):
            row["applied_at"] = created + timedelta(minutes=31)
            row["base_version_number"], row["applied_version_number"] = 3, 4
        if status in ("watching", "shadow"):
            row["watch_until"] = now + timedelta(days=5)
        if status in ("kept", "rolled_back", "regression_kept", "failed", "rejected"):
            row["settled_at"] = created + timedelta(days=1) if status != "rejected" else created + timedelta(minutes=30)
        if status in ("kept", "rolled_back", "regression_kept"):
            delta = {"kept": 0.06, "rolled_back": -0.09, "regression_kept": -0.07}[status]
            row.update(baseline_score=0.61, candidate_score=round(0.61 + delta, 2), delta=delta,
                       verdict="progression" if delta > 0 else "regression")
        if status == "rolled_back":
            row["rollback_version_number"] = 5
        row["outcome_note"] = {
            "kept": "The watch did not regress beyond the margin: the change stays.",
            "rolled_back": "The re-evaluation regressed: the target went back to version 3.",
            "regression_kept": "It regressed, but the target was edited by hand meanwhile: that edit stays.",
            "failed": "The re-evaluation run failed: no verdict.",
            "rejected": "Rejected by the administrator.",
        }.get(status)
        rows.append(row)
    return rows


async def seed(agent_id: str, assignment_id: str, assignment2_id: str, flow_id: str,
               user_id: str) -> dict[str, object]:
    """Write the extra targets and (when absent) the proposals; answer their ids."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.agent_skill import AgentSkill
    from app.models.assignment import Assignment
    from app.models.flow import Flow
    from app.models.improvement_proposal import ImprovementProposal

    now = datetime.now(UTC)
    owner = uuid.UUID(user_id)
    async with async_session() as db:
        existing = list((await db.execute(
            select(ImprovementProposal.id).where(ImprovementProposal.finding["lab_seed"].as_boolean().is_(True))
        )).scalars())
        if existing:
            await engine.dispose()
            return {"proposals": [str(i) for i in existing], "new": 0}
        skills: list[AgentSkill] = []
        for name, content in zip(SKILL_NAMES, SKILL_CONTENT, strict=True):
            skill = (await db.execute(select(AgentSkill).where(AgentSkill.name == name))).scalars().first()
            if skill is None:
                skill = AgentSkill(user_id=owner, name=name, description="Seeded by the lab.", content=content,
                                   tags=["lab"])
                db.add(skill)
            skills.append(skill)
        flow2 = (await db.execute(select(Flow).where(Flow.name == SECOND_FLOW_NAME))).scalars().first()
        if flow2 is None:
            flow2 = Flow(user_id=owner, name=SECOND_FLOW_NAME, description="Seeded by the lab.",
                         graph=_graph("Enriched"), tags=["lab"])
            db.add(flow2)
        await db.flush()
        agent = await db.get(Agent, uuid.UUID(agent_id))
        assignment = await db.get(Assignment, uuid.UUID(assignment_id))
        assignment2 = await db.get(Assignment, uuid.UUID(assignment2_id))
        targets = {"agent": agent_id, "skill1": str(skills[0].id), "skill2": str(skills[1].id),
                   "assignment": assignment_id, "assignment2": assignment2_id, "flow": flow_id,
                   "flow2": str(flow2.id)}
        texts = {"agent": (agent.persona or "") if agent else "", "skill1": skills[0].content or "",
                 "skill2": skills[1].content or "", "assignment": assignment.prompt if assignment else "",
                 "assignment2": assignment2.prompt if assignment2 else ""}
        ids: list[str] = []
        for row in plan_proposals(targets, texts, user_id, agent_id, now):
            values = dict(row)
            for column in ("target_id", "agent_id", "author_agent_id", "requested_by_user_id", "decided_by_user_id"):
                if values.get(column):
                    values[column] = uuid.UUID(values[column])
            values["updated_at"] = values["created_at"]
            proposal = ImprovementProposal(**values)
            db.add(proposal)
            await db.flush()
            ids.append(str(proposal.id))
        await db.commit()
        answer = {"proposals": ids, "new": len(ids), "flow2": str(flow2.id),
                  "skills": [str(skill.id) for skill in skills]}
    await engine.dispose()
    return answer


def main() -> None:
    agent_id, assignment_id, assignment2_id, flow_id, user_id, out = sys.argv[1:7]
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, assignment_id, assignment2_id, flow_id, user_id))))


if __name__ == "__main__":
    main()
