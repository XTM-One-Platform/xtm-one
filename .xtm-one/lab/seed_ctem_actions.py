"""The lab's stored CTEM artifacts: a ready assessment per stage and a ready exposure briefing.

Started by ``lab.py`` (``seed_ctem_actions``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers, under
``LAB_CTEM_ACTIONS=on`` (``[service.lab-ctem-actions]``). The stage assessment and
briefing GETs answer the administrator's latest completed run of each analyst whose
``result_summary`` parses as JSON authored for the active vertical; a stub model writes
none, and no API stores a run's result, so this writes them through the platform's model.
Arguments: the user who owns every run (the lab administrator) and the JSON file the
answer is written to (stage or ``briefing`` -> run id).

* one completed run of each seeded stage analyst (``CTEM_STAGE_AGENT_SLUGS``), its
  assessment carrying a ``recommended_action`` (``STAGE_ACTIONS``);
* one completed run of the exposure briefing analyst with three ``next_steps``, high,
  medium and low, the medium one repeating the Prioritization action word for word, so the
  Act now queue's dedupe by title keeps one row.

So ``GET /api/v1/ctem-metrics/stages/{stage}/assessment`` and ``GET /api/v1/ctem-metrics/briefing``
answer ``ready`` with no model call and no fetch override. Lab-only.

With ``LAB_CTEM_STALE=on`` (``[service.lab-ctem-stale]``, #12381) every run is stored ``STALE_AGE`` old, past the
briefing's daily window (``BRIEFING_FRESH_HOURS``) and the assessments' weekly one (``STAGE_ASSESSMENT_FRESH_DAYS``),
so both GETs answer ``ready`` with ``stale`` true: the Out of date states render after a reload.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The vertical every artifact is authored for: the one the command center opens on.
VERTICAL = "cyber"
#: The briefing analyst's slug (``ctem_metrics._BRIEFING_AGENT_SLUG``).
BRIEFING_SLUG = "exposure-briefing-analyst"
SEEDED_BY = {"seeded_by": "lab"}
#: How old ``LAB_CTEM_STALE=on`` stores every artifact: past the weekly assessment window and the daily briefing one.
STALE_AGE = timedelta(days=8)


def stored_age(environ: dict) -> timedelta:
    """How far before now the artifacts are stored: ``STALE_AGE`` under ``LAB_CTEM_STALE=on``, else none."""
    return STALE_AGE if environ.get("LAB_CTEM_STALE", "").strip().lower() == "on" else timedelta(0)


#: (headline, score_read, recommended_action) of each stage's stored assessment.
STAGE_ACTIONS: dict[str, tuple[str, str, str]] = {
    "scoping": ("OpenCTI and OpenAEV are connected but no scope is defined yet", "unknown",
                "Define the primary-asset scope in OpenCTI so every later stage has assets to measure"),
    "discovery": ("ATT&CK detection coverage is at 42 percent", "at_risk",
                  "Map detection rules to the uncovered ATT&CK techniques used by the tracked actors"),
    "prioritization": ("Seven known-exploited vulnerabilities are open", "critical",
                       "Patch the 7 known-exploited vulnerabilities on internet-facing assets this week"),
    "validation": ("Detection expectations fail most often in the last simulations", "at_risk",
                   "Re-run the failed detection expectations once the new rules are deployed"),
    "remediation": ("Case closure holds at 55 percent", "fair",
                    "Automate the containment playbook for the most frequent case type"),
}

#: The briefing's next steps: (action, rationale, priority); the medium one repeats a stage action.
NEXT_STEPS: tuple[tuple[str, str, str], ...] = (
    ("Validate the EDR policy against the failed prevention expectations",
     "Prevention fails on 6 of 21 expectations in the last simulations", "high"),
    (STAGE_ACTIONS["prioritization"][2],
     "Known-exploited vulnerabilities are the shortest path to the primary assets", "medium"),
    ("Review the phishing awareness results with the human response team",
     "Human response expectations are mostly pending or partial", "low"),
)


def stage_assessment(stage: str) -> dict:
    """The stored assessment of *stage*, in the stage analysts' output schema."""
    headline, score_read, action = STAGE_ACTIONS[stage]
    return {
        "stage": stage, "vertical": VERTICAL, "headline": headline,
        "assessment": f"Seeded by the lab: {headline.lower()}.",
        "score_read": score_read,
        "key_metrics": [{"label": "Seeded", "value": "lab", "trend": "flat"}],
        "recommended_action": action,
        "coverage": {"opencti": True, "openaev": True},
    }


def briefing() -> dict:
    """The stored exposure briefing, in the briefing analyst's output schema."""
    return {
        "vertical": VERTICAL,
        "headline": "Known-exploited vulnerabilities and failed detections lead the exposure",
        "summary": "Seeded by the lab: seven known-exploited vulnerabilities are open and detection "
                   "expectations fail in the last simulations.",
        "posture_trend": "stable",
        "coverage": {"opencti": True, "openaev": True},
        "key_findings": [{"title": "Seven known-exploited vulnerabilities", "severity": "high", "source": "opencti",
                          "recommendation": STAGE_ACTIONS["prioritization"][2]}],
        "recommended_focus": NEXT_STEPS[0][0],
        "next_steps": [{"action": a, "rationale": r, "priority": p} for a, r, p in NEXT_STEPS],
    }


def plan(agents: dict[str, str], now: datetime) -> list[dict]:
    """The ``AssignmentRun`` columns of every stored artifact; *agents* maps a stage or ``briefing`` to an agent id."""
    rows = []
    for index, (key, artifact) in enumerate(
        [*((stage, stage_assessment(stage)) for stage in STAGE_ACTIONS), ("briefing", briefing())]
    ):
        started = now - timedelta(minutes=30 - index)
        rows.append({
            "key": key, "agent_id": agents[key], "status": "completed", "error": None,
            "trigger_payload": SEEDED_BY, "trigger_context": {"type": "manual", "ctem_vertical": VERTICAL, **SEEDED_BY},
            "result_summary": json.dumps(artifact),
            "created_at": started, "started_at": started, "completed_at": started + timedelta(minutes=1),
        })
    return rows


async def seed(user_id: str) -> dict[str, str]:
    """Create the runs; answer stage or ``briefing`` -> run id."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the runs' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.services.ctem_verticals import CTEM_STAGE_AGENT_SLUGS

    slugs = {**{stage: CTEM_STAGE_AGENT_SLUGS[stage] for stage in STAGE_ACTIONS}, "briefing": BRIEFING_SLUG}
    async with async_session() as db:
        found = {a.slug: str(a.id) for a in (await db.execute(select(Agent).where(Agent.slug.in_(slugs.values())))).scalars()}
        missing = sorted(slug for slug in slugs.values() if slug not in found)
        if missing:
            raise SystemExit(f"seeded analysts missing: {', '.join(missing)}")
        rows = plan({key: found[slug] for key, slug in slugs.items()}, datetime.now(UTC) - stored_age(dict(os.environ)))
        runs = {
            row["key"]: AssignmentRun(**{k: v for k, v in row.items() if k != "key"} | {"agent_id": uuid.UUID(row["agent_id"])},
                                      user_id=uuid.UUID(user_id))
            for row in rows
        }
        db.add_all(runs.values())
        await db.commit()
    await engine.dispose()
    return {key: str(run.id) for key, run in runs.items()}


def main() -> None:
    user_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id))))


if __name__ == "__main__":
    main()
