# SPDX-License-Identifier: Apache-2.0

"""The cases in motion, as the CTEM command center and the stage pages draw them (#1621).

One read of the cases the caller sees, rolled up for the overview: the counts by
status (the Cases band), the open cases and the waiting decisions per CTEM phase
(the lifecycle stage cards and the "Cases in this phase" card of each stage
page), the decisions waiting with the oldest one, and the last step of the
prioritization funnel (the OpenCTI vulnerabilities carried by an open case).

Pure: the route reads the cases, the viewer's view and the history of every
visible case in one query each (no read per stage card), then calls
:func:`build`. A case the viewer sees none of the issues of is in no figure,
the same rule as the list (``ctem_cases_view.list_for_viewer``), so every count
here equals the list it links to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.ctem_case import CASE_STATES, OPEN_STATES, CtemCase, CtemCaseEvent, CtemCaseIssue
from app.services import ctem_case_audit as audit
from app.services import ctem_cases_view as view_of

#: How many cases a phase card lists, the ones that need attention first.
TOP_PER_PHASE = 5
#: The window of the funnel step's "resolved" sub-label.
RESOLVED_WINDOW_DAYS = 30
#: The issue source the prioritization funnel counts (its unit: OpenCTI vulnerabilities).
FUNNEL_SOURCE = "opencti"

_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _attention_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    """Waiting for a decision first, then priority, then the oldest status."""
    waiting = 0 if row["state"] == "waiting_decision" else 1
    since = _parse(row.get("state_since"))
    oldest_first = int(since.timestamp()) if since else 0
    return (waiting, _PRIORITY_RANK.get(row["priority"], 9), oldest_first, row["id"])


def _card_row(row: dict[str, Any], pa_row: str | None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "state": row["state"],
        "priority": row["priority"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "state_since": row.get("state_since"),
        "waiting_on": row.get("waiting_on"),
        # The primary asset the case touches, among its issues the viewer sees; None when it touches none.
        "primary_asset": pa_row,
    }


def build(
    cases: list[tuple[CtemCase, list[CtemCaseIssue]]],
    view: view_of.ViewerView,
    history: dict[uuid.UUID, list[CtemCaseEvent]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The overview of the cases *view* sees (see the module docstring)."""
    now = now or datetime.now(UTC)
    by_state = {s: 0 for s in CASE_STATES}
    phases: dict[str, dict[str, Any]] = {
        p: {"open": 0, "waiting": 0, "counts": {s: 0 for s in CASE_STATES}, "top": []} for p in audit.PHASES
    }
    no_phase_open = 0
    waiting: list[dict[str, Any]] = []
    in_case: set[str] = set()
    resolved_recent = 0
    since_window = now - timedelta(days=RESOLVED_WINDOW_DAYS)
    open_rows: dict[str, list[tuple[dict[str, Any], str | None]]] = {p: [] for p in audit.PHASES}

    for case, members in cases:
        row = view_of.summary(case, members, view)
        if row is None:
            continue
        row.update(view_of.case_position(history.get(case.id, []), view, case.owner_user_id))
        state = row["state"]
        by_state[state] = by_state.get(state, 0) + 1
        is_open = state in OPEN_STATES
        phase = row.get("phase")
        if phase in phases:
            phases[phase]["counts"][state] = phases[phase]["counts"].get(state, 0) + 1
        if state == "resolved":
            since = _parse(row.get("state_since"))
            if since is not None and since >= since_window:
                resolved_recent += 1
        if not is_open:
            continue
        visible = view_of.visible_members(members, view)
        for member in visible:
            if member.source == FUNNEL_SOURCE and member.open:
                in_case.add(member.external_id)
        pa_rows = sorted(
            {
                j
                for member in visible
                if member.open
                for j in getattr(view.issues.get(f"{member.source}:{member.external_id}"), "primary_assets", ())
            }
        )
        pa_row = pa_rows[0] if pa_rows else None
        if state == "waiting_decision":
            wait = row.get("waiting_on") or {}
            waiting.append(
                {
                    "case_id": row["id"],
                    "title": row["title"],
                    "phase": phase,
                    "gate": wait.get("gate"),
                    "since": wait.get("since") or row.get("state_since"),
                }
            )
        if phase in phases:
            phases[phase]["open"] += 1
            if state == "waiting_decision":
                phases[phase]["waiting"] += 1
            open_rows[phase].append((row, pa_row))
        else:
            no_phase_open += 1

    for phase, rows in open_rows.items():
        rows.sort(key=lambda pair: _attention_key(pair[0]))
        phases[phase]["top"] = [_card_row(row, pa_row) for row, pa_row in rows[:TOP_PER_PHASE]]

    waiting.sort(key=lambda w: (_parse(w["since"]) or now, w["case_id"]))
    oldest = waiting[0] if waiting else None
    oldest_since = _parse(oldest["since"]) if oldest else None
    return {
        "counts": by_state,
        "open": sum(by_state[s] for s in OPEN_STATES if s in by_state),
        "phases": phases,
        # Open cases whose phase is not known yet (no event): in the band, in no stage card.
        "no_phase_open": no_phase_open,
        "waiting": {
            "count": len(waiting),
            "oldest": oldest,
            "oldest_hours": (round(max(0.0, (now - oldest_since).total_seconds()) / 3600, 1) if oldest_since else None),
            "cases": waiting,
        },
        "funnel": {
            # Distinct OpenCTI vulnerabilities (the funnel's unit) an open case carries, among the viewer's.
            "in_case": len(in_case),
            "resolved_window_days": RESOLVED_WINDOW_DAYS,
            "resolved_recent": resolved_recent,
        },
    }


def empty() -> dict[str, Any]:
    """The overview with no case (no organization context yet)."""
    return build([], view_of.ViewerView(), {})
