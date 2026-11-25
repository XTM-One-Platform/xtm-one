# SPDX-License-Identifier: Apache-2.0

"""One resolved decision record per gate of a CTEM case (#1518).

The case page drew its gate cards from the newest ``gate`` event alone, so an
answered decision read "Waited for a person" until a run wrote the answer on
the case, and the card had nothing to say about the answer itself. Here each
gate of the case is resolved from both sources:

* the ``decisions`` row (dev-docs/human-gates.md G2) of the gate's work item,
  the source of truth of who answered, when, from which surface, which option
  and with which comment;
* the case's ``gate`` events (``ctem_case_loop.record_gate``), which say when
  the gate was asked, whether it passed automatically and why.

An answered decision is answered whether or not the case event was written
(``recorded_on_case`` says which). Pure: no I/O; ``ctem_case_gate_records_view``
does the reads with the viewer's visibility.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.services.ctem_case_audit import GATE_PHASE

#: Record statuses.
WAITING = "waiting"
ANSWERED = "answered"
PASSED_AUTOMATICALLY = "passed_automatically"
EXPIRED = "expired"
CANCELLED = "cancelled"
STATUSES: tuple[str, ...] = (WAITING, ANSWERED, PASSED_AUTOMATICALLY, EXPIRED, CANCELLED)

#: Why a gate ran without a person (or waited), the kinds the backend writes.
REASON_KINDS: tuple[str, ...] = (
    "runbook",
    "within_threshold",
    "above_threshold",
    "loss_unknown",
    "fleet_level",
    "person",
)

#: The correlation keys of the work item a waiting gate opens, by gate key, for one case id
#: (``builtin_agents.CTEM_*_GATE_PREFIXES``: the personas and the "Gate answered" trigger use them).
GATE_ITEM_PREFIXES: dict[str, tuple[str, ...]] = {
    "ctem.analyst_review": ("ctem:review:",),
    "ctem.validation.non_readonly": ("ctem:validation:", "ctem:f2:validate:"),
    "ctem.remediation.decision": ("ctem:remediation:",),
    "ctem.mobilize": ("ctem:f1:case-mobilize:", "ctem:f2:mobilize:"),
}

_DECISION_STATUS = {"open": WAITING, "answered": ANSWERED, "expired": EXPIRED, "cancelled": CANCELLED}
_EVENT_STATUS = {"waited": WAITING, "answered": ANSWERED, "passed_automatically": PASSED_AUTOMATICALLY}
_RUNBOOK = re.compile(r"pre-approved runbook (.+?)(?: \([^()]*\))?$", re.I)


def item_keys(case_id: str) -> dict[str, str]:
    """``{correlation key: gate key}`` of every work item a gate of *case_id* may open."""
    return {prefix + case_id: gate for gate, prefixes in GATE_ITEM_PREFIXES.items() for prefix in prefixes}


def _parse(at: Any) -> datetime | None:
    if isinstance(at, datetime):
        return at if at.tzinfo else at.replace(tzinfo=UTC)
    if not isinstance(at, str) or not at:
        return None
    try:
        parsed = datetime.fromisoformat(at)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(at: Any) -> str | None:
    parsed = _parse(at)
    return parsed.isoformat() if parsed else None


def reason_kind(reason: str | None, outcome: str | None) -> dict[str, Any] | None:
    """The kind of reason a ``gate`` event carries, ``{kind, runbook?}``; ``person`` for an answer."""
    if outcome == "answered":
        return {"kind": "person"}
    text = (reason or "").strip()
    if not text and outcome != PASSED_AUTOMATICALLY:
        return None
    found = _RUNBOOK.search(text)
    if found:
        return {"kind": "runbook", "runbook": found.group(1).strip()}
    lowered = text.lower()
    if "within the approval threshold" in lowered:
        return {"kind": "within_threshold"}
    if "above the approval threshold" in lowered:
        return {"kind": "above_threshold"}
    if "expected loss is unknown" in lowered:
        return {"kind": "loss_unknown"}
    return {"kind": "fleet_level"}


def option_label(ask: Mapping[str, Any] | None, key: str | None) -> str | None:
    """The label of option *key* in an ask (``None`` when the ask does not list it)."""
    if not key:
        return None
    for option in (ask or {}).get("options") or []:
        if str(option.get("key", "")).casefold() == str(key).casefold():
            return str(option.get("label") or option.get("key"))
    return None


def _person(raw: Any, names: Mapping[str, str]) -> dict[str, Any] | None:
    if not raw:
        return None
    ident = str(raw)
    return {"id": ident, "name": names.get(ident)}


def _recipient(raw: str | None, names: Mapping[str, str]) -> dict[str, Any] | None:
    """``user:<id>`` / ``group:<id>`` / ``company`` / ``admins`` as ``{type, id?, name?}``."""
    if not raw:
        return None
    kind, _, ident = raw.partition(":")
    if kind == "user":
        return {"type": "user", "id": ident, "name": names.get(ident)}
    if kind == "group":
        return {"type": "group", "id": ident, "name": names.get(ident)}
    return {"type": kind}


def _gate_events(history: Iterable[Mapping[str, Any]], key: str) -> list[Mapping[str, Any]]:
    events = [e for e in history if e.get("kind") == "gate" and e.get("from") == key and e.get("to") in _EVENT_STATUS]
    return sorted(events, key=lambda e: _parse(e.get("at")) or datetime.min.replace(tzinfo=UTC))


def _next_event(history: Iterable[Mapping[str, Any]], after: datetime | None, key: str) -> dict[str, Any] | None:
    """The first event of the case after the answer that is not the same gate: what the answer unblocked."""
    if after is None:
        return None
    best: tuple[datetime, Mapping[str, Any]] | None = None
    for event in history:
        at = _parse(event.get("at"))
        if at is None or at <= after or (event.get("kind") == "gate" and event.get("from") == key):
            continue
        if best is None or at < best[0]:
            best = (at, event)
    if best is None:
        return None
    event = best[1]
    return {
        "at": best[0].isoformat(),
        "kind": event.get("kind"),
        # The words the History timeline draws for the event read these (a state's new value, a gate and its outcome).
        "from": event.get("from"),
        "to": event.get("to"),
        "headline": event.get("headline"),
        "actor_type": event.get("actor_type"),
        "actor_name": event.get("actor_name"),
        "refs": event.get("refs") or [],
    }


def resolve(
    history: list[dict[str, Any]],
    decisions: Iterable[Mapping[str, Any]],
    *,
    names: Mapping[str, str] | None = None,
    now: datetime | None = None,
    filed: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """One record per gate the case met, in the CTEM order of ``GATE_PHASE``.

    *history* is the annotated history (``ctem_case_audit.annotate``);
    *decisions* the decision rows of the case's gate work items the viewer may
    read, each ``{gate, work_item_id, created_by_agent_id?, decision}`` where
    ``decision`` holds the row's ``id, public_id, status, ask, recipient,
    answer, answered_by, answered_at, surface, deadline, created_at``.
    *names* maps the user, group and agent ids the viewer may see to names.
    *filed* says, by gate key, whether an open decision item of the gate exists
    for anyone (``ctem_gate_order.open_gate_item_keys``, #1954); a waiting record
    carries it as ``decision_filed`` (``None`` when unknown).
    """
    names = names or {}
    now = now or datetime.now(UTC)
    rows: dict[str, Mapping[str, Any]] = {}
    for entry in decisions:
        gate = entry.get("gate")
        decision = entry.get("decision") or {}
        if not gate:
            continue
        kept = rows.get(gate)
        # The newest decision of a gate wins (a gate asked again opens a new one).
        if kept is None or (_parse(decision.get("created_at")) or datetime.min.replace(tzinfo=UTC)) >= (
            _parse((kept.get("decision") or {}).get("created_at")) or datetime.min.replace(tzinfo=UTC)
        ):
            rows[gate] = entry
    records: list[dict[str, Any]] = []
    for gate in GATE_PHASE:
        events = _gate_events(history, gate)
        chosen = rows.get(gate)
        if not events and chosen is None:
            continue
        record = _record(gate, events, chosen, history, names, now)
        # A gate can read waiting with no decision anyone can answer (#1954): the page says so.
        record["decision_filed"] = (filed or {}).get(gate) if record["status"] == WAITING else None
        records.append(record)
    return records


def _record(
    gate: str,
    events: list[Mapping[str, Any]],
    entry: Mapping[str, Any] | None,
    history: list[dict[str, Any]],
    names: Mapping[str, str],
    now: datetime,
) -> dict[str, Any]:
    last = events[-1] if events else None
    asked = next((e for e in reversed(events) if e.get("to") == "waited"), None)
    answered_event = next((e for e in reversed(events) if e.get("to") == "answered"), None)
    decision = (entry or {}).get("decision") or {}
    status = _EVENT_STATUS.get(str(last.get("to")), WAITING) if last else WAITING
    row_status = _DECISION_STATUS.get(decision.get("status") or "")
    last_at = _parse(last.get("at")) if last else None
    row_at = _parse(decision.get("answered_at")) or _parse(decision.get("created_at"))
    # The decision row wins when it says more than the case does: an answer,
    # an expiry or a cancellation the case has not recorded yet, or a newer ask.
    row_newer = bool(row_at and last_at and row_at >= last_at)
    pass_after_ask = bool(
        status == PASSED_AUTOMATICALLY and row_status == WAITING and last_at and row_at and last_at > row_at
    )
    if row_status and (status == WAITING or last is None or row_newer) and not pass_after_ask:
        status = row_status
    answer = decision.get("answer") or {}
    answered_at = _parse(decision.get("answered_at")) or (
        _parse(answered_event.get("at")) if answered_event is not None and status == ANSWERED else None
    )
    answered_by = decision.get("answered_by") or (
        answered_event.get("reason") if answered_event is not None and status == ANSWERED else None
    )
    option_key = answer.get("option") or (
        answered_event.get("evidence") if answered_event is not None and status == ANSWERED else None
    )
    asked_at = _parse(decision.get("created_at")) or (_parse(asked.get("at")) if asked is not None else None)
    end = answered_at if status == ANSWERED else (now if status == WAITING else None)
    wait_seconds = round((end - asked_at).total_seconds(), 1) if end and asked_at and end >= asked_at else None
    policy = last if status == PASSED_AUTOMATICALLY else None
    agent_id = (entry or {}).get("created_by_agent_id") or (
        asked.get("actor") if asked is not None and asked.get("actor_type") == "agent" else None
    )
    return {
        "key": gate,
        "phase": GATE_PHASE.get(gate),
        "status": status,
        "asked_at": asked_at.isoformat() if asked_at else None,
        "asked_of": _recipient(decision.get("recipient"), names),
        "asked_by_agent": {"id": str(agent_id), "name": names.get(str(agent_id))} if agent_id else None,
        "deadline": _iso(decision.get("deadline")),
        "answered_at": answered_at.isoformat() if answered_at and status == ANSWERED else None,
        "answered_by": _person(answered_by, names) if status == ANSWERED else None,
        "surface": decision.get("surface") if status == ANSWERED else None,
        "option": (
            {
                "key": str(option_key),
                "label": answer.get("label") or option_label(decision.get("ask"), option_key),
            }
            if option_key and status == ANSWERED
            else None
        ),
        "comment": (answer.get("text") or None) if status == ANSWERED else None,
        "reason": reason_kind(policy.get("reason"), PASSED_AUTOMATICALLY) if policy is not None else None,
        "passed_at": _iso(policy.get("at")) if policy is not None else None,
        "wait_seconds": wait_seconds,
        "decision_id": decision.get("public_id"),
        "work_item_id": (entry or {}).get("work_item_id"),
        # False while the answer is on the decision but no ``gate answered`` event was written on the case yet.
        "recorded_on_case": status != ANSWERED or answered_event is not None,
        "next": _next_event(history, answered_at or (_parse(policy.get("at")) if policy is not None else None), gate)
        if status in (ANSWERED, PASSED_AUTOMATICALLY)
        else None,
    }


def attach(history: list[dict[str, Any]], records: Iterable[Mapping[str, Any]]) -> None:
    """Extend ``answer`` on the newest waiting gate event of each record with the decision record.

    The History timeline (#1490) reads a waiting gate and its answer as one
    story from ``answer``; the record adds what the case event never carried
    (option label, comment, surface, wait) and fills ``answer`` when the case
    event lags the decision. In place.
    """
    for record in records:
        if record.get("status") not in (ANSWERED, EXPIRED, CANCELLED):
            continue
        asked = None
        for event in history:
            if event.get("kind") != "gate" or event.get("from") != record.get("key") or event.get("to") != "waited":
                continue
            if asked is None or (_parse(event.get("at")) or datetime.min.replace(tzinfo=UTC)) >= (
                _parse(asked.get("at")) or datetime.min.replace(tzinfo=UTC)
            ):
                asked = event
        if asked is None:
            continue
        current = dict(asked.get("answer") or {})
        by = record.get("answered_by") or {}
        option = record.get("option") or {}
        current.setdefault("at", record.get("answered_at"))
        current.setdefault("outcome", record.get("status"))
        current.setdefault("by", by.get("id"))
        current.setdefault("option", option.get("key"))
        current.update(
            {
                "by_name": by.get("name"),
                "option_label": option.get("label"),
                "comment": record.get("comment"),
                "surface": record.get("surface"),
                "wait_seconds": record.get("wait_seconds"),
                "recorded_on_case": record.get("recorded_on_case"),
                "decision_id": record.get("decision_id"),
                "work_item_id": record.get("work_item_id"),
            }
        )
        asked["answer"] = current
