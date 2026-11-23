"""The lab's sample report: one generated report with every block kind, so the report viewer, its print variant
and the PDF export render on a real row with no model call and no hand SQL.

Started by ``lab.py`` (``seed_report``) with the backend's interpreter, from ``backend/`` and with the backend's
environment, once the backend answers: no API creates a report (only the ``generate_report`` tool does, and a
lab's stub model cannot call it), so the report is written through the platform's own
:func:`app.services.reports.save_report`, which validates the spec exactly as the tool does. Arguments: the user
the report belongs to and the JSON file the answer is written to.

Found again on a restart: the report titled ``REPORT_TITLE`` of the user is answered as it is, nothing is
written twice.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

REPORT_TITLE = "Lab sample report: every block"


def spec() -> dict:
    """A report spec that carries every block kind of ``report_spec.REPORT_BLOCKS`` once."""
    return {
        "title": REPORT_TITLE,
        "theme": "light",
        "header": {
            "title": REPORT_TITLE,
            "eyebrow": "Cyber Threat Intelligence",
            "subtitle": "A seeded report for the lab: every block of the renderer, filled.",
            "tags": ["As of today", "TLP:CLEAR", "Audience: lab reviewers"],
            "verdict": {"grade": "Risk contained", "label": "Bottom line", "rich_text": "The sample threat is **contained**."},
        },
        "blocks": [
            {"block": "kpi_row", "title": "Figures", "number": "01", "items": [
                {"value": "12", "label": "Indicators", "note": "From the lab feed"},
                {"value": "3", "label": "Campaigns"},
                {"value": "> 40", "label": "Assets in scope"},
            ]},
            {"block": "prose", "title": "Summary", "number": "02",
             "lead": "A short lead paragraph, rendered larger.",
             "rich_text": "Body text with **bold** and a list:\n\n- first point\n- second point",
             "alert": {"label": "Caveat", "rich_text": "Seeded data, not a real assessment."}},
            {"block": "decisions", "title": "Decisions", "number": "03", "items": [
                {"title": "Patch the exposed gateway", "rich_text": "Within 72 hours.", "confidence": "high"},
                {"title": "Brief the board", "text": "At the next committee."},
            ]},
            {"block": "chain", "title": "Attack chain", "number": "04", "steps": [
                {"title": "Initial access", "text": "Phishing mail."}, {"title": "Execution"}, {"title": "Exfiltration"},
            ], "note": "The chain shows the observed order, not every step."},
            {"block": "assessment_table", "title": "Assessment", "number": "05", "rows": [
                {"statement": "The actor can reuse the kit.", "confidence": "high", "nature": "vendor advisory"},
                {"statement": "The actor targets the sector.", "confidence": "medium", "appraisal": "Single-sourced."},
            ]},
            {"block": "scenarios", "title": "Scenarios", "number": "06", "items": [
                {"title": "Most likely", "text": "Opportunistic reuse of the kit.", "impact": "Low"},
                {"title": "Most dangerous", "text": "Targeted intrusion.", "impact": "High"},
            ]},
            {"block": "risk_table", "title": "Risks", "number": "07", "rows": [
                {"situation": "Data theft", "level": "elevated", "posture": "Watch egress."},
                {"situation": "Service outage", "level": "moderate"},
                {"situation": "Defacement", "level": "controlled"},
                {"situation": "Ransomware", "level": "critical"},
            ]},
            {"block": "matrix", "title": "Coverage", "number": "08", "axis_label": "Layer / step",
             "column_labels": ["Detect", "Respond"], "rows": [
                {"label": "Endpoint", "cells": [{"label": "Yes", "level": "controlled"}, {"label": "Partly", "level": "moderate"}]},
                {"label": "Network", "cells": [{"label": "Partly", "level": "elevated"}, {"label": "No", "level": "critical"}]},
            ]},
            {"block": "callout", "title": "Readiness", "rich_text": "Four controls of five are in place.",
             "score": "4/5", "score_label": "controls"},
            {"block": "timeline", "title": "Plan", "number": "09", "phases": [
                {"label": "Week 1", "headline": "Contain", "items": ["Patch", "Rotate keys"]},
                {"label": "Week 2", "items": ["Hunt"]},
            ]},
            {"block": "checklist", "title": "Questions", "items": ["Who owns the gateway?", "Is MFA enforced?"]},
            {"block": "fill_in_box", "title": "Owner's answer", "fields": [{"label": "Owner"}, {"label": "Due date"}],
             "notes": ["Status: NOT ASSESSED"]},
            {"block": "annex", "title": "Annex", "number": "A", "summary": "Indicators in full",
             "items": ["198.51.100.7", "example.invalid"], "note": "Documentation addresses only."},
            {"block": "sources", "title": "Sources", "items": [
                {"label": "Lab feed", "url": "https://example.com/feed", "ref": "S01"},
                {"label": "Analyst notes", "ref": "S02"},
            ], "limits": "Seeded for the lab; no real collection."},
        ],
        "footer": "Lab sample report · TLP:CLEAR",
    }


async def seed(user_id: str) -> dict:
    """Write the report once; answer ``{"report": id, "found_again": bool}``."""
    import app.models  # noqa: F401 - every table, so the row's foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.report import Report
    from app.services.reports import save_report

    user = uuid.UUID(user_id)
    async with async_session() as db:
        found = await db.scalar(
            select(Report).where(Report.user_id == user, Report.title == REPORT_TITLE).order_by(Report.created_at)
        )
        if found is not None:
            answer = {"report": str(found.id), "found_again": True}
        else:
            report = await save_report(db, user_id=user, spec_payload=spec())
            await db.commit()
            answer = {"report": str(report.id), "found_again": False}
    await engine.dispose()
    return answer


def main() -> None:
    user_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id))))


if __name__ == "__main__":
    main()
