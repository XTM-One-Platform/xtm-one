# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: what a validation of a ``control_decay`` case measures in OpenCRQ (#2369).

A ``control_decay`` case (evidence state ``never_measured``, ``attested_only`` or
``expired``) closes only when OpenCRQ books a measurement against the control.
OpenCRQ books one kind of OpenAEV result as a measurement: the observation of a
simulation inject that carries a DETECTION or PREVENTION expectation, on a MITRE
technique the control covers, on a host mapped to the supporting asset. A port
or version scan carries no expectation and reaches OpenCRQ as nothing (no CVE,
no finding, no test): approving one never closes the case.

The evidence of a one-off run expires after ``evidence_health.max_age_days``
(``ctem_evidence_health``), and the next pass opens the case again: the
measuring test is proposed recurring, at a period below that delay.

This module states both on the validation gate card of such a case, from the
case and the organization profile, so the line never depends on the prompt:

- :func:`recurrence`: the cron and the words of a re-run period below the delay;
- :func:`card_line`: the plain statement of what the run updates and what not;
- :func:`complete_gate_ask`: the platform completion of a ``ctem.validation.non_readonly``
  card keyed by a ``control_decay`` case, called where the work item is written.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The keys of the validation gate of a case, each followed by the case id
#: (``ctem_case_gate_records``: ``ctem.validation.non_readonly``).
GATE_KEY_PREFIXES: tuple[str, ...] = ("ctem:validation:", "ctem:f2:validate:")
#: The trigger path whose validation is a measurement of a control.
CONTROL_DECAY = "control_decay"
#: The only OpenAEV expectation types OpenCRQ maps to a control function (detect, prevent).
MEASURING_EXPECTATIONS: tuple[str, ...] = ("DETECTION", "PREVENTION")
#: The line every completed card carries, first in its context.
LINE_LEAD = "OpenCRQ measurement:"
#: The re-test delay when the profile states none (``EvidenceHealthSettings.max_age_days``).
DEFAULT_MAX_AGE_DAYS = 30
_MAX_CONTEXT_CHARS = 800


def recurrence(max_age_days: int) -> dict[str, Any]:
    """``{cron, period_hours, words}``: a re-run period strictly below *max_age_days* (pure).

    Weekly when the delay is above a week, daily when above a day, else every
    twelve hours: the next result always lands before the evidence expires.
    """
    days = max(int(max_age_days or DEFAULT_MAX_AGE_DAYS), 1)
    if days > 7:
        return {"cron": "0 3 * * 1", "period_hours": 7 * 24, "words": "weekly"}
    if days > 1:
        return {"cron": "0 3 * * *", "period_hours": 24, "words": "daily"}
    return {"cron": "0 */12 * * *", "period_hours": 12, "words": "every 12 hours"}


def card_line(max_age_days: int) -> str:
    """What the validation updates in OpenCRQ and what not, and the recurrence it needs (pure)."""
    days = max(int(max_age_days or DEFAULT_MAX_AGE_DAYS), 1)
    period = recurrence(days)["words"]
    return (
        f"{LINE_LEAD} only injects with {' or '.join(MEASURING_EXPECTATIONS)} expectations on a technique the control "
        "covers update its technique test result; a port or version scan, findings and other controls "
        f"are not updated, so they never close this case. Evidence expires after {days} days: "
        f"the measuring test repeats {period}."
    )


def has_line(ask: Mapping[str, Any] | None) -> bool:
    """Whether an ask already states the measurement line (its context names it)."""
    return LINE_LEAD.casefold() in str((ask or {}).get("context") or "").casefold()


def complete_ask(ask: Mapping[str, Any], max_age_days: int) -> dict[str, Any]:
    """*ask* with the measurement line first in its context (pure; an ask that has it is kept as is)."""
    out = dict(ask)
    if has_line(out):
        return out
    from app.services.decision_ask import fit

    head = card_line(max_age_days)
    rest = str(out.get("context") or "").strip()
    out["context"] = fit(f"{head}\n{rest}" if rest else head, _MAX_CONTEXT_CHARS)
    return out


def case_id_of(correlation_key: object) -> uuid.UUID | None:
    """The case id of a validation gate key, else ``None`` (pure)."""
    if not isinstance(correlation_key, str):
        return None
    key = correlation_key.strip()
    for prefix in GATE_KEY_PREFIXES:
        if key.startswith(prefix):
            try:
                return uuid.UUID(key[len(prefix) :].strip())
            except ValueError:
                return None
    return None


async def _max_age_days(db: AsyncSession, organization_id: uuid.UUID | None) -> int:
    from app.services import organization_context as org_context

    row = await org_context.current_version(db, organization_id)
    return org_context.parse_profile(row.profile if row else {}).evidence_health.max_age_days


async def complete_gate_ask(
    db: AsyncSession, correlation_key: object, ask: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """*ask* completed with the measurement line when it is the validation card of a control_decay case.

    Any other card is returned as given. Never fails a work item write: a case
    or a profile that cannot be read leaves the ask as given.
    """
    if not ask or has_line(ask):
        return ask
    case_id = case_id_of(correlation_key)
    if case_id is None:
        return ask
    from app.models.ctem_case import CtemCase

    try:
        case = await db.get(CtemCase, case_id)
        if case is None or getattr(case, "trigger_path", None) != CONTROL_DECAY:
            return ask
        days = await _max_age_days(db, getattr(case, "organization_id", None))
    except Exception:  # the card is written whatever the profile answers
        logger.info("CTEM control measurement line unavailable for %s", case_id, exc_info=True)
        return ask
    return complete_ask(ask, days)
