"""The lab's Platform Health findings, so the health screens render on real rows (#1303).

Started by ``lab.py`` (``seed_health_findings``) with the backend's interpreter, from ``backend/`` and with the
backend's environment, in an XTM One mode lab. A check opens a finding only on its second failing pass and the
Module Analyst needs a live model and reachable products, so a fresh lab shows every module at 0 open findings:
the findings list, the severity counts, the Dismissed and Resolved filters and the agentic badge never render.
This writes them through the engine's own writer (``engine.record_results``: two failing reads open each one,
``engine.dismiss_finding``, two passing reads resolve one) then ``engine.rollup_day``:

* :func:`results`: one finding per severity on ``xtm_one`` (checks that genuinely fail in a lab), one agentic
  finding (``<module>.agentic.<slug>``) on each of ``opencti``, ``openaev`` and ``opencrq``;
* the first ``info`` finding dismissed with a reason, the ``smtp`` one resolved.

Arguments: the administrator who dismisses (the lab administrator) and the JSON file the answer
(``{"open": [ids], "dismissed": [ids], "resolved": [ids]}``) is written to. No model call.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEEDED_BY = "Seeded by the lab"
#: The finding the seed dismisses and the one it resolves (check ids of :func:`results`).
DISMISSED = "xtm_one.hub_registration"
RESOLVED = "xtm_one.smtp"


def results(failing: bool = True) -> list[dict]:
    """The findings, as ``CheckResult`` keyword arguments: every severity, the XTM One module and the three analyst modules."""
    rows = [
        ("xtm_one.registration_token", "xtm_one", "security", "critical",
         "Set PLATFORM_REGISTRATION_TOKEN to a secret of your own and restart the platform."),
        ("xtm_one.licence_expiry", "xtm_one", "licence", "warning",
         "Renew the Filigran licence before it expires."),
        (DISMISSED, "xtm_one", "configuration", "info", "Register this platform with XTM Hub."),
        (RESOLVED, "xtm_one", "configuration", "info", "Configure an SMTP host so alerts can be emailed."),
        ("opencti.agentic.connector_queue_growing", "opencti", "performance", "warning",
         "Scale the connector's workers or lower its ingestion rate."),
        ("openaev.agentic.injector_never_ran", "openaev", "configuration", "critical",
         "Check the injector's registration and its last heartbeat."),
        ("opencrq.agentic.scenario_without_assets", "opencrq", "coverage", "info",
         "Link the scenario to the assets it quantifies."),
    ]
    return [
        {
            "check_id": check_id,
            "module": module,
            "category": category,
            "severity": severity,
            "failing": failing,
            "evidence": {"values": {"seeded": True}, "source": SEEDED_BY},
            "recommendation": recommendation,
        }
        for check_id, module, category, severity, recommendation in rows
    ]


async def seed(admin_id: str) -> dict:
    """Open every finding of :func:`results`, dismiss one, resolve one; answer the ids by status."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine as db_engine
    from app.models.health_check import STATUS_DISMISSED, STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding
    from app.services.health import engine
    from app.services.health.engine import CheckResult, finding_key

    now = datetime.now(UTC)
    failing = [CheckResult(**row) for row in results()]
    async with async_session() as db:
        for step in range(engine.OPEN_AFTER_FAILS):
            await engine.record_results(db, failing, now=now - timedelta(minutes=engine.OPEN_AFTER_FAILS - step))
        passing = [CheckResult(**row) for row in results(failing=False) if row["check_id"] == RESOLVED]
        for _ in range(engine.RESOLVE_AFTER_PASSES):
            await engine.record_results(db, passing, now=now)
        await engine.dismiss_finding(
            db, finding_key(DISMISSED, {}), reason=f"{SEEDED_BY}: not registered on purpose.", by=admin_id,
            by_name="Lab administrator", now=now,
        )
        await engine.rollup_day(db, now.date())
        await db.commit()
        answer: dict[str, list[str]] = {"open": [], "dismissed": [], "resolved": []}
        names = {STATUS_OPEN: "open", STATUS_DISMISSED: "dismissed", STATUS_RESOLVED: "resolved"}
        for row in failing:
            stored = await db.get(HealthCheckFinding, finding_key(row.check_id, row.subject))
            if stored is not None and stored.status in names:
                answer[names[stored.status]].append(stored.id)
    await db_engine.dispose()
    return answer


def main() -> None:
    admin_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id))))


if __name__ == "__main__":
    main()
