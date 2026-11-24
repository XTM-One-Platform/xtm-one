# SPDX-License-Identifier: Apache-2.0

"""The CTEM gates that guard no tool call, held by the case tools (#1644).

Three gates of the CTEM catalog (``builtin_fleet_docs.CTEM_GATE_KEYS``) name no
tool: ``ctem.production_change``, ``ctem.accept_risk`` and
``ctem.close_without_retest``. ``fleet_autonomy.gated_tool_rules`` pauses a call
per tool, so a wait pin on one of them (or a level that makes it wait) was only
stated to the agent and the loop could move on before a person answered.

The case tools enforce them instead, with these rules (pure, no I/O):

- :func:`check_record`: a gate that waits is never recorded
  ``passed_automatically``.
- :func:`check_close`: an agent closes a case without a passing validation only
  once ``ctem.close_without_retest`` was answered on the case, when that gate
  waits.
- :func:`pending` and :func:`check_hold`: while one of the three gates is
  recorded ``waited`` on the case and not answered since, the case is held: no
  state move, ticket or remediation decision until the answer (an expiry is
  recorded as an answer by the Gate answered listener) is written on the case.

A fleet that does not declare a gate reads ``None`` for it and nothing is
enforced for that gate: the pin and the level are the fleet's own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.services.ctem_cases import CaseRuleError

TOOL_LESS_GATES: tuple[str, ...] = (
    "ctem.production_change",
    "ctem.accept_risk",
    "ctem.close_without_retest",
)
CLOSE_WITHOUT_RETEST = "ctem.close_without_retest"
#: The states that close a case on the loop's own word (``accepted_risk`` is a person's).
CLOSING_STATES: tuple[str, ...] = ("resolved", "mitigated_watching")


def _field(event: Any, name: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _at(event: Any) -> datetime:
    at = _field(event, "at")
    if isinstance(at, datetime):
        return at if at.tzinfo else at.replace(tzinfo=UTC)
    if isinstance(at, str) and at:
        try:
            parsed = datetime.fromisoformat(at)
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)


def newest_outcomes(history: Iterable[Any]) -> dict[str, str]:
    """``{gate key: newest outcome}`` of the tool-less gates the case history records.

    *history* holds the case's events (``CtemCaseEvent`` rows or their dicts):
    a ``gate`` event carries the key in ``from_value`` (``from`` in a dict) and
    the outcome in ``to_value`` (``to``).
    """
    newest: dict[str, tuple[datetime, str]] = {}
    for event in history:
        if _field(event, "kind") != "gate":
            continue
        key = _field(event, "from_value") or _field(event, "from")
        outcome = _field(event, "to_value") or _field(event, "to")
        if key not in TOOL_LESS_GATES or not outcome:
            continue
        at = _at(event)
        kept = newest.get(key)
        if kept is None or at >= kept[0]:
            newest[key] = (at, str(outcome))
    return {key: outcome for key, (_, outcome) in newest.items()}


def pending(history: Iterable[Any]) -> list[str]:
    """The tool-less gates waited on the case and not answered since, in catalog order."""
    outcomes = newest_outcomes(history)
    return [key for key in TOOL_LESS_GATES if outcomes.get(key) == "waited"]


def check_record(key: str, outcome: str, waits: bool | None) -> None:
    """Refuse recording a tool-less gate that waits as passed automatically."""
    if key in TOOL_LESS_GATES and outcome == "passed_automatically" and waits:
        raise CaseRuleError(
            f"gate {key} waits for a person at this fleet's level or pin: record it waited, "
            "ask the person, and record the answer once given"
        )


def check_hold(waiting: Iterable[str], action: str, *, answering: str = "") -> None:
    """Refuse *action* while a tool-less gate waits on the case; *answering* is the gate the call answers."""
    held = [key for key in waiting if key != answering]
    if held:
        raise CaseRuleError(
            f"the case waits on a person at gate {', '.join(held)}: {action} once the answer "
            "is recorded on the case (update_ctem_case gate, outcome answered)"
        )


def check_close(
    target: str | None,
    *,
    proven: bool,
    waits: bool | None,
    history: Iterable[Any],
    answering: str = "",
) -> None:
    """Refuse closing a case without a passing validation unless ``ctem.close_without_retest`` was answered."""
    if target not in CLOSING_STATES or proven or not waits:
        return
    if answering == CLOSE_WITHOUT_RETEST or newest_outcomes(history).get(CLOSE_WITHOUT_RETEST) == "answered":
        return
    raise CaseRuleError(
        f"closing this case without a passing re-test is gate {CLOSE_WITHOUT_RETEST}, which waits for a "
        "person: record it waited, ask the person, and close once the answer is recorded"
    )
