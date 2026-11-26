# SPDX-License-Identifier: Apache-2.0

"""The audit trail of a CTEM case (#1490): every history event with its CTEM phase and autonomy class.

The case page's History reads the case events as an audit trail grouped by
CTEM phase: when the case entered each phase, how long it stayed, what an
agent did alone, what a person validated and who, what a runbook or threshold
passed automatically, what changed from -> to and what still waits on whom.
Everything here is derived at read time from the events as the viewer is
served them (``ctem_cases_view._event``), so a restricted field stays
restricted and no stored row is rewritten: an event written before this
module renders with a phase and an autonomy class like a new one.

Pure: no I/O. ``annotate`` adds ``phase``, ``autonomy``, ``actor_type``,
``headline``, ``refs`` and, on a gate, the other half of its story (a waiting
gate names its answer, an answer names since when it waited) to each served
event; ``phases`` summarises the phases in their CTEM order.

A new event kind joins the trail by adding its phase to ``KIND_PHASE`` (or
``CARRY_KINDS`` when it belongs to whatever phase the case is in) and, when
it records a change, to ``CHANGE_KINDS``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.services.ctem_scoring import (
    STAGE_DISCOVERY,
    STAGE_KEYS,
    STAGE_PRIORITIZATION,
    STAGE_REMEDIATION,
    STAGE_SCOPING,
    STAGE_VALIDATION,
)

#: The CTEM phases a case moves through, in order (the five stages of the CTEM
#: lifecycle; ``remediation`` is the Mobilization phase of the case page).
PHASES: tuple[str, ...] = tuple(STAGE_KEYS)

#: Autonomy classes, in the order the UI counts them.
AUTONOMOUS = "autonomous"
VALIDATED_BY_PERSON = "validated_by_person"
VALIDATED_BY_POLICY = "validated_by_policy"
WAITING = "waiting"
CHANGED = "changed"
BY_PERSON = "by_person"
SYSTEM = "system"
AUTONOMY_CLASSES: tuple[str, ...] = (
    AUTONOMOUS,
    VALIDATED_BY_PERSON,
    VALIDATED_BY_POLICY,
    WAITING,
    CHANGED,
    BY_PERSON,
    SYSTEM,
)

#: Actor types.
ACTOR_PERSON = "person"
ACTOR_AGENT = "agent"
ACTOR_SYSTEM = "system"

SYSTEM_ACTOR = "system"

#: The phase an event kind belongs to, whatever the case's phase.
KIND_PHASE: dict[str, str] = {
    "created": STAGE_DISCOVERY,
    "announced": STAGE_DISCOVERY,
    "signal": STAGE_DISCOVERY,
    "issue_added": STAGE_DISCOVERY,
    "issue_reopened": STAGE_DISCOVERY,
    "retargeted": STAGE_DISCOVERY,
    "merge": STAGE_DISCOVERY,
    "merged": STAGE_DISCOVERY,
    "split": STAGE_DISCOVERY,
    "exploitation": STAGE_PRIORITIZATION,
    "attack_path": STAGE_PRIORITIZATION,
    "threat_package": STAGE_PRIORITIZATION,
    "validation": STAGE_VALIDATION,
    # The OpenAEV scenario of the validation and its playability (#2468).
    "scenario": STAGE_VALIDATION,
    "source_event": STAGE_VALIDATION,
    "retest_requested": STAGE_VALIDATION,
    "issue_closed": STAGE_VALIDATION,
    "treatment_plan": STAGE_REMEDIATION,
    "ticket": STAGE_REMEDIATION,
    "applied_change": STAGE_REMEDIATION,
    # A detection gap the detection desk proposed on the case, or its answer (#2393).
    "detection_gap": STAGE_REMEDIATION,
}

#: The phase a gate of the CTEM catalog (``builtin_fleet_docs.CTEM_GATE_KEYS``) belongs to.
GATE_PHASE: dict[str, str] = {
    "ctem.analyst_review": STAGE_PRIORITIZATION,
    "ctem.validation.non_readonly": STAGE_VALIDATION,
    "ctem.close_without_retest": STAGE_VALIDATION,
    "ctem.remediation.decision": STAGE_REMEDIATION,
    "ctem.mobilize": STAGE_REMEDIATION,
    "ctem.production_change": STAGE_REMEDIATION,
    "ctem.accept_risk": STAGE_REMEDIATION,
}

#: Kinds that happen in whatever phase the case is in (state, owner, due, SLA, a note).
CARRY_KINDS: frozenset[str] = frozenset({"state", "owner", "due", "note", "sla_breached", "owner_requested", "stalled"})

#: Kinds that record a change of the case (rendered with from -> to).
CHANGE_KINDS: frozenset[str] = frozenset(
    {
        "state",
        "owner",
        "due",
        "retargeted",
        "ticket",
        "validation",
        "treatment_plan",
        "merge",
        "merged",
        "split",
        "sla_breached",
        "issue_closed",
        "issue_reopened",
    }
)

#: How a note's opening words name its phase, for notes written before notes carried one.
_NOTE_PHASE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*(validation plan|validation|re-?test)\b", re.I), STAGE_VALIDATION),
    (re.compile(r"^\s*(mobili[sz]ation|remediation|ticket|treatment plan)\b", re.I), STAGE_REMEDIATION),
    (
        re.compile(r"^\s*(relevance verdict|relevance|expected loss|threat package|prioriti[sz]ation|triage)\b", re.I),
        STAGE_PRIORITIZATION,
    ),
    (re.compile(r"^\s*(loop opened|discovery|case read)\b", re.I), STAGE_DISCOVERY),
    (re.compile(r"^\s*scoping\b", re.I), STAGE_SCOPING),
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
#: ``task <id>``, ``run <id>``, ``item <id>`` (an 8+ hex prefix or a UUID) in a note.
_REF = re.compile(
    r"\b(task|run|(?:decision )?item)\s+(" + _UUID + r"|[0-9a-f]{8,12})\b",
    re.I,
)
_UUID_RE = re.compile(r"^" + _UUID + r"$")
#: Headline width: one sentence on one line.
HEADLINE_MAX = 160

#: The wording of the ``announced`` reasons the loop writes (catch-up, backfill).
ANNOUNCED_HEADLINE: dict[str, str] = {
    "catch-up": "Announced to the loop by the catch-up pass (the case opened while no fleet was listening)",
    "backfill": "Announced to the loop by the backfill (the case was opened before the loop existed)",
}
ANNOUNCED_DEFAULT = "Announced to the loop"


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


def actor_type(actor: str | None, people: Iterable[str] = ()) -> str:
    """``system``, ``person`` (a user id among *people*) or ``agent`` (any other id)."""
    if not actor or actor == SYSTEM_ACTOR:
        return ACTOR_SYSTEM
    if actor in set(people):
        return ACTOR_PERSON
    return ACTOR_AGENT


def note_phase(text: str | None) -> str | None:
    """The phase a note's opening words name, ``None`` when they name none."""
    for pattern, phase in _NOTE_PHASE:
        if text and pattern.search(text):
            return phase
    return None


def own_phase(event: Mapping[str, Any]) -> str | None:
    """The phase an event belongs to by itself, ``None`` for a kind that carries the case's phase."""
    kind = event.get("kind") or ""
    if kind == "gate":
        return GATE_PHASE.get(event.get("from") or "")
    if kind == "note":
        return note_phase(event.get("reason"))
    if kind == "state" and event.get("to") == "mitigated_watching":
        return STAGE_VALIDATION
    return KIND_PHASE.get(kind)


def autonomy(event: Mapping[str, Any], kind_of_actor: str) -> str:
    """The autonomy class of one event (see ``AUTONOMY_CLASSES``)."""
    kind = event.get("kind") or ""
    if kind == "gate":
        outcome = event.get("to")
        if outcome == "passed_automatically":
            return VALIDATED_BY_POLICY
        if outcome == "answered":
            return VALIDATED_BY_PERSON
        return WAITING
    if kind in CHANGE_KINDS:
        return CHANGED
    if kind_of_actor == ACTOR_PERSON:
        return BY_PERSON
    if kind_of_actor == ACTOR_AGENT:
        return AUTONOMOUS
    return SYSTEM


def headline(event: Mapping[str, Any]) -> str | None:
    """One sentence that says what the event did; a note's first sentence, never its whole body.

    ``None`` when the event's own label says it all (the UI words it from kind, from and to).
    """
    kind = event.get("kind") or ""
    reason = (event.get("reason") or "").strip()
    if kind == "announced":
        return ANNOUNCED_HEADLINE.get(reason.lower(), ANNOUNCED_DEFAULT)
    if kind != "note" or not reason:
        return None
    first = re.split(r"(?<=[.!?])\s+|\n", reason, maxsplit=1)[0].strip()
    if len(first) > HEADLINE_MAX:
        first = first[: HEADLINE_MAX - 1].rstrip() + "\u2026"
    return first


def refs(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The task, run and decision item ids a note names, in order, each once."""
    if event.get("kind") != "note":
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for match in _REF.finditer(event.get("reason") or ""):
        word = match.group(1).lower()
        kind = "task" if word == "task" else "run" if word == "run" else "item"
        ref = match.group(2).lower()
        if (kind, ref) in seen:
            continue
        seen.add((kind, ref))
        out.append({"kind": kind, "id": ref, "full": bool(_UUID_RE.match(ref))})
    return out


def annotate(
    events: list[dict[str, Any]],
    *,
    people: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """The served events, each with its phase, autonomy class, actor type, headline and refs.

    *events* are the viewer's events in any order (``hydrate`` serves them
    newest first); the result keeps that order. A carry kind (state, owner,
    due, note, SLA) takes the phase the case was in when it happened: the
    phase of the latest phased event before it, ``discovery`` before any. A
    waiting gate names its later answer (``answered_at``, ``answered_by``,
    ``outcome``) and the answer names since when it waited (``waited_since``).
    """
    people = set(people)
    order = sorted(
        range(len(events)), key=lambda i: (_parse(events[i].get("at")) or datetime.min.replace(tzinfo=UTC), i)
    )
    out: list[dict[str, Any]] = [dict(e) for e in events]
    current = STAGE_DISCOVERY
    waiting: dict[str, int] = {}
    for i in order:
        event = out[i]
        phase = own_phase(event)
        if phase is None:
            phase = current
        else:
            current = phase
        kind_of_actor = actor_type(event.get("actor"), people)
        event["phase"] = phase
        event["actor_type"] = kind_of_actor
        event["autonomy"] = autonomy(event, kind_of_actor)
        event["headline"] = headline(event)
        event["refs"] = refs(event)
        if event.get("kind") == "gate":
            key = event.get("from") or ""
            if event.get("to") == "waited":
                waiting[key] = i
                event["answer"] = None
            elif key in waiting:
                asked = out[waiting.pop(key)]
                asked["answer"] = {
                    "at": event.get("at"),
                    "outcome": event.get("to"),
                    "by": event.get("reason"),
                    "option": event.get("evidence"),
                }
                event["waited_since"] = asked.get("at")
    return out


def phases(
    annotated: list[dict[str, Any]],
    *,
    runs: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """The case's phases in CTEM order: entered, exited, duration and counts, and the current phase.

    A phase is entered at its first event and exited when the case's next
    phased event belongs to another phase (a phase re-entered keeps its first
    entry and its last exit). The current phase is the phase of the newest
    event; it has no exit and its duration runs to *now*. *runs* (fleet runs
    on the case, ``{created_at, phase?}``) count in the phase they ran in.
    """
    now = now or datetime.now(UTC)
    timed = sorted(
        ((t, e) for e in annotated if (t := _parse(e.get("at"))) is not None),
        key=lambda pair: pair[0],
    )
    rows: dict[str, dict[str, Any]] = {
        p: {
            "phase": p,
            "entered_at": None,
            "exited_at": None,
            "seconds": 0.0,
            "events": 0,
            "runs": 0,
            "autonomy": {c: 0 for c in AUTONOMY_CLASSES},
            "waiting": 0,
        }
        for p in PHASES
    }
    spans: list[tuple[str, datetime]] = []
    for at, event in timed:
        phase = event.get("phase")
        if phase not in rows:
            continue
        row = rows[phase]
        row["events"] += 1
        row["autonomy"][event.get("autonomy") or SYSTEM] += 1
        if event.get("autonomy") == WAITING and not event.get("answer"):
            row["waiting"] += 1
        if row["entered_at"] is None:
            row["entered_at"] = at
        if not spans or spans[-1][0] != phase:
            spans.append((phase, at))
    for index, (phase, start) in enumerate(spans):
        end = spans[index + 1][1] if index + 1 < len(spans) else None
        row = rows[phase]
        row["seconds"] += max(0.0, ((end or now) - start).total_seconds())
        if end is not None:
            row["exited_at"] = end
        else:
            row["exited_at"] = None
    for run in runs:
        phase = run.get("phase")
        if phase in rows:
            rows[phase]["runs"] += 1
    current = spans[-1][0] if spans else None
    for row in rows.values():
        row["entered_at"] = row["entered_at"].isoformat() if row["entered_at"] else None
        row["exited_at"] = row["exited_at"].isoformat() if row["exited_at"] else None
        row["seconds"] = round(row["seconds"], 1)
        row["current"] = row["phase"] == current
    return {"current": current, "phases": [rows[p] for p in PHASES]}


def current_phase(events: list[dict[str, Any]]) -> str | None:
    """The CTEM phase a case is in now: the phase of its newest event (``phases``' current), ``None`` with no event.

    The Cases list groups and lays out its Kanban by it (#1503); *events* are the
    viewer's served events, so a restricted field never decides a phase.
    """
    if not events:
        return None
    current = phases(annotate(events))["current"]
    return str(current) if current else None


def open_wait(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What a case waits on now: its newest gate that waited and was not answered, ``None`` when none.

    ``{gate, by, since}``: the gate key, who it waits on (the gate's ``reason``:
    a user id, a role or a policy name, as the loop wrote it) and since when. The
    Cases list shows it on a case Waiting for decision (#1503).

    With no open gate, the latest move into ``waiting_decision`` answers (a
    person set the status by hand, with its required reason; UX r1 B2 on #1515):
    ``gate`` ``None``, ``by`` the actor who moved it, ``since`` that move.
    """
    annotated = annotate(events)

    def _at(e: dict[str, Any]) -> datetime:
        return _parse(e.get("at")) or datetime.min.replace(tzinfo=UTC)

    waiting = [e for e in annotated if e.get("kind") == "gate" and e.get("to") == "waited" and not e.get("answer")]
    if waiting:
        newest = max(waiting, key=_at)
        return {"gate": newest.get("from"), "by": newest.get("reason"), "since": newest.get("at")}
    moves = [e for e in annotated if e.get("kind") == "state" and e.get("to") == "waiting_decision"]
    if not moves:
        return None
    moved = max(moves, key=_at)
    actor = moved.get("actor")
    return {"gate": None, "by": None if actor in (None, "", SYSTEM_ACTOR) else actor, "since": moved.get("at")}


def stalled_since(events: list[dict[str, Any]]) -> str | None:
    """When the case sweep found the case waiting on nobody, while that ``stalled`` event is its newest event (#1736).

    Any later event (a gate, an answer, a state move, a note) means something moved the case: ``None``.
    """
    dated = [e for e in events if _parse(e.get("at")) is not None]
    if not dated:
        return None
    newest = max(dated, key=lambda e: _parse(e.get("at")) or datetime.min.replace(tzinfo=UTC))
    return newest.get("at") if newest.get("kind") == "stalled" else None


def run_phase(created_at: Any, annotated: list[dict[str, Any]]) -> str:
    """The phase the case was in when a fleet run started: the phase of the latest event at or before it."""
    at = _parse(created_at)
    best: tuple[datetime, str] | None = None
    for event in annotated:
        t = _parse(event.get("at"))
        if t is None or at is None or t > at:
            continue
        if best is None or t >= best[0]:
            best = (t, event.get("phase") or STAGE_DISCOVERY)
    return best[1] if best else STAGE_DISCOVERY
