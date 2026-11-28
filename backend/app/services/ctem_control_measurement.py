# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: what a validation of a ``control_decay`` case measures in OpenCRQ (#2369).

A ``control_decay`` case (evidence state ``never_measured``, ``attested_only`` or
``expired``) closes only when OpenCRQ books a measurement against the control:
the observation of an OpenAEV inject that carries a DETECTION or PREVENTION
expectation on a MITRE technique the control covers, or CVE observations on the
asset (an integration's vulnerability scanning, OpenAEV generic vulnerability
scanning). A port-only scan with no expectation and no CVE output reaches
OpenCRQ as nothing: approving one never closes the case.

The agent chooses the measurement from the asset type and the tenant's live
integrations, a permanent control first (``CTEM_CONTROL_MEASUREMENT_RULE``). The
evidence of a one-off run expires after ``evidence_health.max_age_days``
(``ctem_evidence_health``), and the next pass opens the case again: a recurring
option repeats at a period below that delay.

A ``never_measured`` case (#2461) closes only on a first measurement OpenCRQ
books for the asset (:data:`NEVER_MEASURED_CLOSES`): a DETECTION or PREVENTION
expectation result on a covered technique, or CVE findings from a scan that
reads versions. A clean unauthenticated scan has nothing to book, so its pass is
inconclusive and never closes the case; OpenCRQ reads OpenAEV itself, no agent
writes an OpenAEV result back into it as evidence. Each option of the gate says
whether its pass closes the case (:data:`CLOSES_WORDS` or
:data:`NOT_CLOSING_WORDS`), and the platform never leaves the recommendation on
an option that announces it does not close the case while another one does.

This module states both on the validation gate card of such a case, from the
case and the organization profile, so the line never depends on the prompt:

- :func:`recurrence`: the cron and the words of a re-run period below the delay;
- :func:`card_line`: the plain statement of what the run updates and what not;
- :func:`is_never_measured`: whether a case is a first measurement (its platform title);
- :func:`closing_recommendation`: the recommendation moved off an option that announces it
  does not close the case, onto the first option that closes it;
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
#: The words an option of a never_measured gate uses for its outcome (#2461), quoted by the rule.
CLOSES_WORDS = "closes the case"
NOT_CLOSING_WORDS = "does not close the case"
#: The tail of the platform title of a never_measured case (``ctem_case_loop._CONTROL_DECAY_TITLES``).
NEVER_MEASURED_TITLE_TAIL = "for the first time (no test evidence yet)"
#: What closes a never_measured case (#2461): the evidence OpenCRQ books as a first measurement.
NEVER_MEASURED_CLOSES = (
    "this never-measured control closes only when OpenCRQ books a first measurement for the asset: a "
    "DETECTION or PREVENTION expectation result on a technique the control covers, or CVE findings from a "
    "scan that reads versions (authenticated, agent-based, or a connected vulnerability scanner). A clean "
    "unauthenticated scan gives OpenCRQ nothing to book: its pass " + NOT_CLOSING_WORDS + "."
)


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
    """What the validation updates in OpenCRQ and what not, permanent versus one-off (pure).

    The platform does not know which option the agent proposes, so the line states
    both: the re-run period applies to a recurring option only.
    """
    days = max(int(max_age_days or DEFAULT_MAX_AGE_DAYS), 1)
    period = recurrence(days)["words"]
    return (
        f"{LINE_LEAD} a permanent control (an integration's continuous scanning or policy, or a recurring "
        f"OpenAEV scenario repeating {period}) keeps the control measured; a one-off test counts once and its "
        f"evidence expires after {days} days. OpenCRQ updates the control's technique test result "
        f"(injects with {' or '.join(MEASURING_EXPECTATIONS)} expectations on a covered technique) or the "
        "asset's CVE findings; a port-only scan with no expectation and no CVE output updates nothing and "
        "never closes this case."
    )


def never_measured_line(max_age_days: int) -> str:
    """The card line of a never_measured case: what closes it, then the one-off expiry (pure, #2461)."""
    days = max(int(max_age_days or DEFAULT_MAX_AGE_DAYS), 1)
    return (
        f"{LINE_LEAD} {NEVER_MEASURED_CLOSES} Each option says whether its pass {CLOSES_WORDS}; a one-off "
        f"test counts once and its evidence expires after {days} days."
    )


def is_never_measured(case: object) -> bool:
    """Whether *case* is the first measurement of a control: the platform's never_measured title (pure)."""
    if getattr(case, "trigger_path", None) != CONTROL_DECAY:
        return False
    return str(getattr(case, "title", "") or "").strip().endswith(NEVER_MEASURED_TITLE_TAIL)


def option_outcome(option: object) -> str | None:
    """``closes``, ``not_closing`` or ``None``: what an option says of its pass (pure)."""
    if not isinstance(option, Mapping):
        return None
    text = f"{option.get('label') or ''} {option.get('effect') or ''}".casefold()
    if NOT_CLOSING_WORDS in text:
        return "not_closing"
    return "closes" if CLOSES_WORDS in text else None


def closing_recommendation(ask: Mapping[str, Any]) -> dict[str, Any]:
    """*ask* with its recommendation on an option whose pass closes the case (pure, #2461).

    A recommended option that announces its pass does not close the case
    gives way to the first option that says it closes it; with no such option
    the ask is kept as given (the structural decision is the agent's to word).
    """
    out = dict(ask)
    options = [o for o in (out.get("options") or []) if isinstance(o, Mapping)]
    recommended = next((o for o in options if str(o.get("key")) == str(out.get("recommendation"))), None)
    if recommended is None or option_outcome(recommended) != "not_closing":
        return out
    closing = next((o for o in options if option_outcome(o) == "closes"), None)
    if closing is not None:
        out["recommendation"] = closing.get("key")
    return out


def has_line(ask: Mapping[str, Any] | None) -> bool:
    """Whether an ask already states the measurement line (its context names it)."""
    return LINE_LEAD.casefold() in str((ask or {}).get("context") or "").casefold()


def complete_ask(ask: Mapping[str, Any], max_age_days: int, *, never_measured: bool = False) -> dict[str, Any]:
    """*ask* with the measurement line first in its context (pure; an ask that has it keeps its context).

    A never_measured case gets the line of what closes it and its recommendation
    kept on an option that closes the case (:func:`closing_recommendation`).
    """
    out = closing_recommendation(ask) if never_measured else dict(ask)
    if has_line(out):
        return out
    from app.services.decision_ask import full_context, lead_full_context

    head = never_measured_line(max_age_days) if never_measured else card_line(max_age_days)
    # The agent's whole context follows the line, kept reachable when the card cuts it (#2800).
    return lead_full_context(out, head, full_context(out), _MAX_CONTEXT_CHARS)


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
    if not ask:
        return ask
    case_id = case_id_of(correlation_key)
    if case_id is None:
        return ask
    from app.models.ctem_case import CtemCase

    try:
        case = await db.get(CtemCase, case_id)
        if case is None or getattr(case, "trigger_path", None) != CONTROL_DECAY:
            return ask
        first = is_never_measured(case)
        if has_line(ask) and not first:
            return ask
        days = await _max_age_days(db, getattr(case, "organization_id", None))
    except Exception:  # the card is written whatever the profile answers
        logger.info("CTEM control measurement line unavailable for %s", case_id, exc_info=True)
        return ask
    return complete_ask(ask, days, never_measured=first)
