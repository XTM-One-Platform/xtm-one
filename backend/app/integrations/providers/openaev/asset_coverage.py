# SPDX-License-Identifier: Apache-2.0

"""What OpenAEV already runs against one asset (xtm-one #2368).

* ``get_openaev_asset_coverage`` - read-only: the asset's asset groups (static
  and dynamic membership), then every played inject that concerns the asset
  (targeted directly, through one of its groups, or evidenced by the
  expectations persisted at execution), grouped by the scenario its simulation
  belongs to (with the scenario's recurrence) or by atomic testing, with the
  latest result per scenario and whether that result can be reused as evidence.

A scenario search by name cannot answer "is this asset already tested": a
recurring scenario that targets the asset's group is found only by reading
what targets the asset. This tool is that read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _inject_row_executed, _inject_row_state, _search_body

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

#: Inject rows read per page, and pages read at most (the newest rows are kept).
_PAGE = 100
_MAX_PAGES = 3
#: Simulations and scenarios resolved at most per answer.
_MAX_PARENTS = 40
_READS_IN_FLIGHT = 6


def _row_time(row: dict[str, Any]) -> str:
    status = row.get("inject_status")
    sent = status.get("tracking_sent_date") if isinstance(status, dict) else None
    return str(sent or row.get("inject_updated_at") or "")


def _status_name(row: dict[str, Any]) -> str:
    status = row.get("inject_status")
    if isinstance(status, dict):
        return str(status.get("status_name") or "no status")
    return str(status or "no status")


def _expectation_summary(row: dict[str, Any]) -> tuple[str, bool]:
    """'TYPE RESULT, ...' over the row's expectation results, and whether one is scored."""
    parts: list[str] = []
    scored = False
    for entry in row.get("inject_expectation_results") or []:
        if not isinstance(entry, dict):
            continue
        result = str(entry.get("avgResult") or "UNKNOWN").upper()
        if result not in ("UNKNOWN", "PENDING"):
            scored = True
        parts.append(f"{entry.get('type', '?')} {result}")
    return ", ".join(parts), scored


def usable_result(row: dict[str, Any]) -> tuple[bool, str]:
    """Whether the latest inject row is evidence of the control, and why not when it is not."""
    if row.get("inject_enabled") is False:
        return False, "the inject is disabled, so it never runs"
    state = _inject_row_state(row)
    if state == "in_progress":
        return False, f"the last run is still in progress ({_status_name(row)})"
    if not _inject_row_executed(row):
        return False, f"the inject has not executed ({_status_name(row)})"
    summary, scored = _expectation_summary(row)
    if not summary:
        return False, "it executed but declares no expectation, so it measured no control"
    if not scored:
        return False, f"its expectations are not scored yet ({summary})"
    return True, summary


def _recurrence_label(scenario: dict[str, Any] | None) -> str:
    if not isinstance(scenario, dict):
        return "recurrence not read"
    cron = scenario.get("scenario_recurrence")
    if not cron:
        return "not recurring"
    end = scenario.get("scenario_recurrence_end")
    tail = f", until {end}" if end else ""
    return f"recurring ({cron}{tail})"


def format_asset_coverage(
    asset_id: str,
    asset: dict[str, Any] | None,
    asset_err: str | None,
    rows: list[dict[str, Any]],
    total: int | None,
    rows_err: str | None,
    exercises: dict[str, dict[str, Any] | None],
    scenarios: dict[str, dict[str, Any] | None],
) -> str:
    """Render the coverage of one asset: groups, then one block per scenario or atomic testing."""
    name = asset.get("asset_name") if isinstance(asset, dict) else None
    lines = [f"OpenAEV coverage of asset {name or asset_id} (id: {asset_id})"]
    if asset_err:
        lines.append(f"  Asset groups: not read ({asset_err}).")
    else:
        groups = asset.get("asset_asset_groups") if isinstance(asset, dict) else None
        groups = [g for g in groups or [] if isinstance(g, dict)]
        if groups:
            listed = ", ".join(f"{g.get('asset_group_name', '?')} (id: {g.get('asset_group_id', '?')})" for g in groups)
            lines.append(f"  Asset groups (static and dynamic membership): {listed}")
        else:
            lines.append("  Asset groups: none.")
    if rows_err and not rows:
        lines.append(f"  Injects targeting the asset: not read ({rows_err}).")
        return "\n".join(lines)
    if not rows:
        lines.append(
            "  No scenario, simulation or atomic testing has targeted this asset, directly or "
            "through its groups: nothing covers it in OpenAEV."
        )
        return "\n".join(lines)

    by_parent: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        exercise_id = row.get("inject_exercise")
        if exercise_id:
            exercise = exercises.get(str(exercise_id))
            scenario_id = _scenario_of(exercise)
            key = ("scenario", scenario_id) if scenario_id else ("simulation", str(exercise_id))
        else:
            key = ("atomic", str(row.get("inject_id") or "?"))
        by_parent.setdefault(key, []).append(row)

    shown = len(rows)
    count = f"{shown} of {total}" if isinstance(total, int) and total > shown else str(shown)
    lines.append(
        f"  Injects that concern the asset (direct, through a group, or evidenced at execution): {count}, "
        f"in {len(by_parent)} scenario(s), simulation(s) or atomic testing(s)."
    )
    ordered = sorted(by_parent.items(), key=lambda kv: max(_row_time(r) for r in kv[1]), reverse=True)
    for (kind, parent_id), parent_rows in ordered:
        latest = max(parent_rows, key=_row_time)
        ok, why = usable_result(latest)
        when = _row_time(latest) or "?"
        if kind == "scenario":
            scenario = scenarios.get(parent_id)
            title = scenario.get("scenario_name") if isinstance(scenario, dict) else None
            head = f"  - Scenario {title or parent_id} (id: {parent_id}), {_recurrence_label(scenario)}"
            rerun = "re-run this scenario (manage_openaev_scenario action launch)"
        elif kind == "simulation":
            head = f"  - Simulation {parent_id} (no scenario read)"
            rerun = "re-run its scenario or simulation"
        else:
            head = f"  - Atomic testing {latest.get('inject_title') or parent_id} (id: {parent_id})"
            rerun = "relaunch this atomic testing (manage_openaev_atomic_testing action relaunch)"
        lines.append(head)
        lines.append(
            f"      Latest result on the asset: {latest.get('inject_title') or '?'} at {when}: {_status_name(latest)}"
        )
        if ok:
            lines.append(f"      Usable as evidence: yes ({why}).")
        else:
            lines.append(f"      Usable as evidence: no, {why}; to measure it, {rerun}.")
    if rows_err:
        lines.append(f"  More injects not read ({rows_err}).")
    if isinstance(total, int) and total > shown:
        lines.append(f"  {total - shown} older inject(s) not read: the newest {shown} are summarised.")
    return "\n".join(lines)


def _scenario_of(exercise: dict[str, Any] | None) -> str | None:
    if not isinstance(exercise, dict):
        return None
    scenario = exercise.get("exercise_scenario")
    if isinstance(scenario, dict):
        scenario = scenario.get("scenario_id")
    return str(scenario) if scenario else None


def register(base_url: str, headers: dict[str, str], executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def _read(path: str, action: str) -> dict[str, Any] | None:
        resp, err = await _api(base_url, headers, "get", path, action)
        if err or resp is None:
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None

    async def get_openaev_asset_coverage(asset_id: str) -> str:
        asset_id = (asset_id or "").strip()
        if not asset_id:
            return "asset_id is required (the OpenAEV asset id, from search_openaev_assets)."
        asset_resp, asset_err = await _api(base_url, headers, "get", f"/api/assets/{asset_id}", "get asset")
        asset = asset_resp.json() if asset_resp is not None and not asset_err else None
        rows: list[dict[str, Any]] = []
        total: int | None = None
        rows_err: str | None = None
        for page in range(_MAX_PAGES):
            resp, err = await _api(
                base_url,
                headers,
                "post",
                f"/api/assets/{asset_id}/injects/search",
                "injects concerning the asset",
                json=_search_body(page=page, size=_PAGE),
            )
            if err or resp is None:
                rows_err = err or "OpenAEV answered nothing"
                break
            data = resp.json()
            content = data.get("content", []) if isinstance(data, dict) else data
            content = [r for r in content if isinstance(r, dict)] if isinstance(content, list) else []
            if isinstance(data, dict) and isinstance(data.get("totalElements"), int):
                total = data["totalElements"]
            rows += content
            if len(content) < _PAGE or (total is not None and len(rows) >= total):
                break

        gate = asyncio.Semaphore(_READS_IN_FLIGHT)

        async def _gated(path: str, action: str) -> dict[str, Any] | None:
            async with gate:
                return await _read(path, action)

        exercise_ids = list(dict.fromkeys(str(r["inject_exercise"]) for r in rows if r.get("inject_exercise")))
        exercise_ids = exercise_ids[:_MAX_PARENTS]
        read = await asyncio.gather(*(_gated(f"/api/exercises/{e}", "get simulation") for e in exercise_ids))
        exercises = dict(zip(exercise_ids, read, strict=True))
        scenario_ids = list(dict.fromkeys(s for s in (_scenario_of(e) for e in read) if s))[:_MAX_PARENTS]
        read = await asyncio.gather(*(_gated(f"/api/scenarios/{s}", "get scenario") for s in scenario_ids))
        scenarios = dict(zip(scenario_ids, read, strict=True))
        return format_asset_coverage(
            asset_id,
            asset if isinstance(asset, dict) else None,
            asset_err,
            rows,
            total,
            rows_err,
            exercises,
            scenarios,
        )

    executor.register("get_openaev_asset_coverage", get_openaev_asset_coverage)
    return [
        ToolDefinition(
            name="get_openaev_asset_coverage",
            description=(
                "What OpenAEV already runs against one asset, read-only: its asset groups (static "
                "and dynamic), then every scenario (with its recurrence), simulation and atomic "
                "testing whose injects target the asset directly or through those groups, with "
                "the latest result on the asset and whether it is usable as evidence. Read it "
                "before proposing a new scan or scenario for the asset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "The OpenAEV asset id (search_openaev_assets)",
                    },
                },
                "required": ["asset_id"],
            },
            source=_SOURCE,
            priority=1,
        )
    ]
