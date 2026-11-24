# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The module checkers' check reports and agentic findings (#101 S15c, ``dev-docs/platform-health.md`` 13.6).

A module checker ends its round with ``record_module_check_report``: every check of its
playbook with a verdict (``pass``, ``warn``, ``fail``, ``not_run``) and the evidence it
read. :func:`clean_checks` bounds what the model sent; :func:`apply_verdicts` turns each
verdict into the life of the agentic finding ``<module>.agentic.<check>`` of that
registration:

* ``warn`` opens or refreshes it as a warning, ``fail`` as a critical, at once: the
  round already is the confirming read, so the engine's two-read rule does not apply;
* ``pass`` resolves an open one in the same cycle;
* ``not_run`` leaves it as it is (the 7-day expiry of section 12.4 covers it).

A dismissal still live is respected: the row is refreshed, never reopened.

Every agentic finding and every report entry records the check logic that produced it
(:func:`check_logic_version`: the running build and a hash of the module checker's
playbook skill). A finding whose recorded logic differs from the running one, or that
records none (written before #1732), is *stale* (:func:`is_stale`): the trigger pass
starts one ``deploy`` round for its registration and Analyse now never recommends a
disruptive action from it (``dev-docs/platform-health.md`` 13.6).
"""

from __future__ import annotations

import functools
import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: The most checks one report holds (the playbooks hold 8 to 12, section 13.4).
MAX_CHECKS: Final = 30
#: The most evidence entries one check keeps.
MAX_CHECK_EVIDENCE: Final = 10
#: The longest recommendation or not-run reason kept.
MAX_TEXT: Final = 1500
#: A check id: the playbook's own name, lower case.
CHECK_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
#: The finding severity of each failing verdict.
SEVERITY_BY_VERDICT: Final[dict[str, str]] = {"warn": "warning", "fail": "critical"}
#: The category an agentic finding gets when the check names none.
DEFAULT_CATEGORY: Final = "configuration"
#: ``evidence.source`` of an agentic finding written from a check report.
SOURCE: Final = "module_checker"
#: The segment between the module and the check in an agentic check id (never a component name).
AGENTIC_SEGMENT: Final = "agentic"
#: ``evidence`` key of the check logic that produced an agentic finding.
CHECK_LOGIC_KEY: Final = "check_logic"
#: Evidence older than this is too old to act on without a new round (Analyse now).
MAX_EVIDENCE_AGE: Final = timedelta(hours=24)


#: The one module checks skill (#1943): shared rules and report, then one ``## <Label>`` section per module.
MODULE_CHECKS_SKILL_SLUG: Final = "health-module-checks"
#: The section heading of each module in :data:`MODULE_CHECKS_SKILL_SLUG`.
MODULE_SECTION_LABELS: Final[dict[str, str]] = {"opencti": "OpenCTI", "openaev": "OpenAEV", "opencrq": "OpenCRQ"}


def module_playbook(module: str) -> str:
    """The part of the module checks skill that is *module*'s playbook: the shared rules and
    report (everything before the first module section) and *module*'s own section.

    Empty when the build ships no such section, so a module without one hashes as empty.
    """
    from app.agents.seed import SEED_DATA_DIR

    path = SEED_DATA_DIR / "skill_content" / f"{MODULE_CHECKS_SKILL_SLUG}.md"
    label = MODULE_SECTION_LABELS.get(module)
    if label is None or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    headings = {f"## {name}" for name in MODULE_SECTION_LABELS.values()}
    shared: list[str] = []
    own: list[str] = []
    current: list[str] | None = shared
    for line in text.splitlines(keepends=True):
        if line.rstrip() in headings:
            current = own if line.rstrip() == f"## {label}" else None
        if current is not None:
            current.append(line)
    return "".join(shared) + "".join(own) if own else ""


@functools.cache
def _skill_hash(module: str) -> str:
    """The first 16 hex of the sha256 of *module*'s checker playbook as this build ships it:
    its section of the module checks skill with the shared rules, so an edit of another
    module's section leaves this module's findings current (#1742, #1943).
    """
    return hashlib.sha256(module_playbook(module).encode("utf-8")).hexdigest()[:16]


def check_logic_version(module: str) -> dict[str, str]:
    """The check logic running now for *module*: ``{"build", "skill_hash"}``."""
    from app.core.config import settings

    return {"build": str(settings.app_version or ""), "skill_hash": _skill_hash(module)}


def finding_check_logic(evidence: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The check logic an agentic finding's evidence records, ``None`` when it records none."""
    logic = (evidence or {}).get(CHECK_LOGIC_KEY)
    return dict(logic) if isinstance(logic, Mapping) else None


def is_agentic_evidence(evidence: Mapping[str, Any] | None) -> bool:
    return (evidence or {}).get("source") == SOURCE


def is_stale(module: str, evidence: Mapping[str, Any] | None) -> bool:
    """Whether an agentic finding comes from another check logic than the running one.

    A finding written before the logic was recorded is stale too: nothing says which
    logic produced it. A deterministic finding is never stale (the engine re-reads it).
    """
    if not is_agentic_evidence(evidence):
        return False
    return finding_check_logic(evidence) != check_logic_version(module)


def evidence_time(evidence: Mapping[str, Any] | None) -> datetime | None:
    """When an agentic finding's evidence was read (``evidence.read_at``), ``None`` when unknown."""
    raw = (evidence or {}).get("read_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class CheckReportError(ValueError):
    """What the model sent cannot be stored; the message is safe to return."""


@dataclass(frozen=True)
class CheckVerdict:
    """One check of a report, bounded."""

    check: str
    verdict: str
    category: str = DEFAULT_CATEGORY
    recommendation: str = ""
    reason: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def as_entry(self, finding_id: str | None, check_logic: Mapping[str, str] | None = None) -> dict[str, Any]:
        return {
            "id": self.check,
            "verdict": self.verdict,
            "category": self.category,
            "recommendation": self.recommendation or None,
            "reason": self.reason or None,
            "evidence": self.evidence,
            "finding_id": finding_id,
            CHECK_LOGIC_KEY: dict(check_logic) if check_logic else None,
        }


def agentic_check_id(module: str, check: str) -> str:
    """The check id of the agentic finding a verdict on *check* drives."""
    return f"{module}.{AGENTIC_SEGMENT}.{check}"


def _clean_evidence(evidence: Any) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    if not isinstance(evidence, list) or len(evidence) > MAX_CHECK_EVIDENCE:
        raise CheckReportError(f"Error: the evidence of a check is a list of at most {MAX_CHECK_EVIDENCE} entries.")
    cleaned: list[dict[str, Any]] = []
    for entry in evidence:
        if not isinstance(entry, dict) or not str(entry.get("tool") or "").strip():
            raise CheckReportError("Error: each evidence entry names the tool it read.")
        ids = entry.get("object_ids") or []
        cleaned.append(
            {
                "tool": str(entry["tool"]).strip()[:128],
                "arguments": entry.get("arguments") if isinstance(entry.get("arguments"), dict) else {},
                "object_ids": [str(i)[:256] for i in ids][:20] if isinstance(ids, list) else [],
                "values": entry.get("values") if isinstance(entry.get("values"), dict) else {},
                "read_at": str(entry.get("read_at") or "")[:64] or None,
            }
        )
    return cleaned


def clean_checks(checks: Any) -> list[CheckVerdict]:
    """The checks of a report, bounded and validated; :class:`CheckReportError` on anything else."""
    from app.models.health_check import CATEGORIES
    from app.models.health_module_analyst import CHECK_VERDICTS

    if not isinstance(checks, list) or not checks or len(checks) > MAX_CHECKS:
        raise CheckReportError(f"Error: checks must be a list of 1 to {MAX_CHECKS} entries.")
    seen: set[str] = set()
    cleaned: list[CheckVerdict] = []
    for entry in checks:
        if not isinstance(entry, dict):
            raise CheckReportError("Error: each check is an object with an id and a verdict.")
        check = str(entry.get("id") or "").strip()
        if not CHECK_ID_PATTERN.match(check):
            raise CheckReportError("Error: a check id is the playbook's name, lower case, letters, digits and _.")
        if check in seen:
            raise CheckReportError(f"Error: check {check} is reported twice.")
        seen.add(check)
        verdict = str(entry.get("verdict") or "")
        if verdict not in CHECK_VERDICTS:
            raise CheckReportError("Error: a verdict is pass, warn, fail or not_run.")
        category = str(entry.get("category") or DEFAULT_CATEGORY)
        if category not in CATEGORIES:
            raise CheckReportError("Error: unknown finding category.")
        recommendation = str(entry.get("recommendation") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        if verdict in SEVERITY_BY_VERDICT and not recommendation:
            raise CheckReportError(f"Error: check {check} is {verdict}: it needs a recommendation.")
        if verdict == "not_run" and not reason:
            raise CheckReportError(f"Error: check {check} was not run: it needs a reason.")
        if len(recommendation) > MAX_TEXT or len(reason) > MAX_TEXT:
            raise CheckReportError(f"Error: a recommendation or a reason holds at most {MAX_TEXT} characters.")
        cleaned.append(
            CheckVerdict(
                check=check,
                verdict=verdict,
                category=category,
                recommendation=recommendation,
                reason=reason,
                evidence=_clean_evidence(entry.get("evidence")),
            )
        )
    return cleaned


def counts(verdicts: list[CheckVerdict]) -> dict[str, int]:
    """The report counts: ``passed``, ``warned``, ``failed``, ``not_run``."""
    tally = {"pass": 0, "warn": 0, "fail": 0, "not_run": 0}
    for v in verdicts:
        tally[v.verdict] += 1
    return {"passed": tally["pass"], "warned": tally["warn"], "failed": tally["fail"], "not_run": tally["not_run"]}


async def apply_verdicts(
    db: AsyncSession,
    *,
    module: str,
    registration_id: uuid.UUID,
    verdicts: list[CheckVerdict],
    now: datetime,
) -> tuple[list[str | None], dict[str, int]]:
    """Open, refresh or resolve the agentic finding of each verdict; the finding id per verdict and the counts.

    Nothing is committed: the caller writes the report in the same transaction.
    """
    from app.models.health_check import STATUS_DISMISSED, STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding
    from app.services.health.engine import dismissal_live, finding_key

    subject = {"registration_id": str(registration_id)}
    logic = check_logic_version(module)
    tally = {"opened": 0, "refreshed": 0, "resolved": 0}
    finding_ids: list[str | None] = []
    for v in verdicts:
        check_id = agentic_check_id(module, v.check)
        key = finding_key(check_id, subject)
        row = await db.get(HealthCheckFinding, key)
        severity = SEVERITY_BY_VERDICT.get(v.verdict)
        if severity is None:
            if v.verdict == "pass" and row is not None and row.status == STATUS_OPEN:
                row.status, row.resolved_at, row.pass_streak = STATUS_RESOLVED, now, 0
                tally["resolved"] += 1
            finding_ids.append(row.id if row is not None else None)
            continue
        evidence = {"source": SOURCE, "read_at": now.isoformat(), "checks": v.evidence, CHECK_LOGIC_KEY: logic}
        if row is None:
            row = HealthCheckFinding(
                id=key,
                check_id=check_id,
                module=module,
                subject=dict(subject),
                category=v.category,
                severity=severity,
                status=STATUS_OPEN,
                evidence=evidence,
                recommendation=v.recommendation,
                doc_link="",
                pass_streak=0,
                fail_streak=0,
                opened_at=now,
                first_seen=now,
                last_seen=now,
            )
            db.add(row)
            tally["opened"] += 1
        else:
            row.severity, row.category, row.evidence = severity, v.category, evidence
            row.recommendation, row.last_seen, row.pass_streak = v.recommendation, now, 0
            if row.status == STATUS_OPEN or (row.status == STATUS_DISMISSED and dismissal_live(row, now)):
                tally["refreshed"] += 1
            else:
                row.status, row.resolved_at, row.dismissal = STATUS_OPEN, None, None
                row.opened_at, row.alerted_at, row.fail_streak = now, None, 0
                tally["opened"] += 1
        finding_ids.append(key)
    await db.flush()
    return finding_ids, tally


#: The most failing checks listed per module in the weekly digest's Modules part.
WEEKLY_MOST_FAILED: Final = 5


async def weekly_module_summary(db: AsyncSession, *, since: datetime, until: datetime) -> dict[str, dict[str, Any]]:
    """The weekly digest's Modules part (13.8): per module, the rounds of the window and the checks failed most.

    Read from the stored reports only (finished in ``[since, until)``), so the digest's
    figures are the reports' own and never a model's count. ``rounds`` is the number of
    reports, ``registrations`` the registrations they cover; ``most_failed`` ranks the
    checks by the rounds that failed them (warn counted apart), ties by check id.
    """
    from sqlalchemy import select

    from app.models.health_module_analyst import HealthModuleCheckReport

    rows = (
        await db.execute(
            select(HealthModuleCheckReport).where(
                HealthModuleCheckReport.finished_at >= since, HealthModuleCheckReport.finished_at < until
            )
        )
    ).scalars()
    summary: dict[str, dict[str, Any]] = {}
    failing: dict[str, dict[str, list[int]]] = {}
    registrations: dict[str, set[str]] = {}
    for row in rows:
        part = summary.setdefault(
            row.module, {"rounds": 0, "registrations": 0, "passed": 0, "warned": 0, "failed": 0, "not_run": 0}
        )
        part["rounds"] += 1
        for key in ("passed", "warned", "failed", "not_run"):
            part[key] += int(getattr(row, key) or 0)
        registrations.setdefault(row.module, set()).add(str(row.registration_id))
        by_check = failing.setdefault(row.module, {})
        for entry in row.checks or []:
            if not isinstance(entry, dict) or entry.get("verdict") not in SEVERITY_BY_VERDICT:
                continue
            tally = by_check.setdefault(str(entry.get("id")), [0, 0])
            tally[0 if entry["verdict"] == "fail" else 1] += 1
    for module, part in summary.items():
        part["registrations"] = len(registrations.get(module, ()))
        ranked = sorted(failing.get(module, {}).items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
        part["most_failed"] = [
            {"id": check, "failed": fail, "warned": warn} for check, (fail, warn) in ranked[:WEEKLY_MOST_FAILED]
        ]
    return dict(sorted(summary.items()))
