# SPDX-License-Identifier: Apache-2.0

"""OpenAEV inject tools.

* ``manage_openaev_inject`` - injects on a scenario or simulation (CRUD, bulk,
  activation) plus simulation-only execution (trigger, set status, results).
  ``scope`` selects scenario|simulation|global.

Triggering injects on a RUNNING simulation executes against live assets.

Expectations live in ``expectations.py``: they are the scored verdict on the
security controls, a different question from the inject execution status this
module reports, and they have their own analyst-facing read surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _api,
    _fmt_expectation_results_by_type,
    _inject_row_executed,
    _search_body,
    findings_fate,
    inject_read_answer,
    inject_targets_line,
    json_window,
    keep_findings_params,
    keep_findings_schema,
    mirror_content_targets,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"


#: Target types OpenAEV resolves execution traces for (TargetType.hasExecutionTraces).
_TRACE_TARGET_TYPES = ("ASSETS", "AGENT", "TEAMS", "PLAYERS", "AI_TARGETS", "MANUAL")
#: Targets listed per type, and trace reads in flight at once.
_TRACE_TARGETS_PER_TYPE = 50
_TRACE_READS_IN_FLIGHT = 8


def _terminal_trace(traces: list[Any]) -> dict[str, Any] | None:
    """The trace that ends a target's run: its last COMPLETE trace, else its latest."""
    rows = [t for t in traces if isinstance(t, dict)]
    if not rows:
        return None
    rows.sort(key=lambda t: str(t.get("execution_time") or ""))
    complete = [t for t in rows if t.get("execution_action") == "COMPLETE"]
    return (complete or rows)[-1]


def _target_label(target: dict[str, Any]) -> str:
    name = target.get("target_name") or target.get("target_id") or "?"
    return f"{target.get('target_type', '?')} {name} (id: {target.get('target_id', '?')})"


async def _target_traces(base_url: str, headers: dict[str, str], inject_id: str) -> str:
    """Every target of an inject with its terminal execution trace.

    The global status trace names only the targets an executor reached; a
    target OpenAEV closed before the distribution (agentless asset, inactive or
    overloaded agent, no executor, clearance) has its own COMPLETE trace, read
    here per target from /api/injects/execution-traces.
    """
    targets: list[dict[str, Any]] = []
    notes: list[str] = []
    for target_type in _TRACE_TARGET_TYPES:
        resp, err = await _api(
            base_url,
            headers,
            "post",
            f"/api/injects/{inject_id}/targets/{target_type}/search",
            "list inject targets",
            json=_search_body(size=_TRACE_TARGETS_PER_TYPE),
        )
        if err or resp is None:
            notes.append(f"{target_type}: targets not read ({err})")
            continue
        data = resp.json()
        rows = data.get("content", []) if isinstance(data, dict) else data
        rows = [r for r in rows or [] if isinstance(r, dict) and r.get("target_id")]
        for row in rows:
            row.setdefault("target_type", target_type)
        targets += rows
        total = data.get("totalElements") if isinstance(data, dict) else None
        if isinstance(total, int) and total > len(rows):
            notes.append(f"{target_type}: {len(rows)} of {total} targets listed")
    if not targets:
        return "\n".join([f"Inject {inject_id} has no target with execution traces.", *notes])

    gate = asyncio.Semaphore(_TRACE_READS_IN_FLIGHT)

    async def _one(target: dict[str, Any]) -> str:
        async with gate:
            resp, err = await _api(
                base_url,
                headers,
                "get",
                "/api/injects/execution-traces",
                "target execution traces",
                params={
                    "injectId": inject_id,
                    "targetId": target["target_id"],
                    "targetType": target["target_type"],
                },
            )
        if err or resp is None:
            return f"- {_target_label(target)}: traces not read ({err})"
        traces = resp.json()
        last = _terminal_trace(traces if isinstance(traces, list) else [])
        if last is None:
            return f"- {_target_label(target)}: no trace yet"
        return (
            f"- {_target_label(target)}: {last.get('execution_status', '?')} "
            f"{last.get('execution_action', '?')} at {last.get('execution_time', '?')}: "
            f"{last.get('execution_message', '')}"
        )

    lines = await asyncio.gather(*(_one(t) for t in targets))
    return "\n".join([f"Inject {inject_id}: {len(targets)} target(s), terminal trace of each:", *lines, *notes])


def _base(scope: str, parent_id: str) -> str | None:
    if scope == "scenario":
        return f"/api/scenarios/{parent_id}"
    if scope == "simulation":
        return f"/api/exercises/{parent_id}"
    return None


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_inject(
        action: str = "",
        scope: str = "scenario",
        parent_id: str = "",
        inject_id: str = "",
        title: str = "",
        description: str = "",
        injector_contract_id: str = "",
        content: str = "",
        depends_duration: int | None = None,
        parent_inject_id: str = "",
        parent_condition: str = "success",
        depends_on: str = "",
        team_ids: list[str] | None = None,
        asset_ids: list[str] | None = None,
        asset_group_ids: list[str] | None = None,
        all_teams: bool | None = None,
        tag_ids: list[str] | None = None,
        enabled: bool | None = None,
        status: str = "",
        message: str = "",
        search: str = "",
        limit: int = 20,
        keep_findings: bool = True,
        offset: int = 0,
    ) -> str:
        action = (action or "").strip().lower()
        scope = (scope or "scenario").strip().lower()

        if action in ("status", "target_traces") and scope != "global":
            return (
                f"action '{action}' is only available with scope='global' "
                "(provide inject_id)."
            )

        if scope == "global":
            if action == "get":
                if not inject_id:
                    return "inject_id is required."
                resp, err = await _api(
                    base_url, headers, "get", f"/api/injects/{inject_id}", "get inject"
                )
                if err:
                    return err
                return inject_read_answer(resp.json(), offset=offset)
            if action == "status":
                if not inject_id:
                    return "inject_id is required."
                resp, err = await _api(
                    base_url, headers, "get", "/api/injects/status",
                    "inject status", params={"injectId": inject_id},
                )
                if err:
                    return err
                return json_window(resp.json(), offset)
            if action == "target_traces":
                if not inject_id:
                    return "inject_id is required."
                return await _target_traces(base_url, headers, inject_id)
            return "For scope 'global', valid actions are: get, status, target_traces."

        base = _base(scope, parent_id)
        if base is None:
            return "scope must be 'scenario', 'simulation', or 'global'."
        if not parent_id:
            return "parent_id (scenario_id or exercise_id) is required."

        def _build_depends_on() -> list | None:
            # Chaining is what turns a flat list of atomic tests into a real ATTACK
            # PATH: each follow-on inject declares the inject it runs after, and the
            # dependency graph renders as the live attack-path map. `depends_on` is
            # the raw escape hatch (full InjectDependencyInput array for branching /
            # multi-parent); `parent_inject_id` is the ergonomic single-parent form
            # the orchestrator uses to build a linear kill chain. On create the API
            # binds the child to the inject being created, so only the parent id is
            # needed here.
            if depends_on:
                parsed = json.loads(depends_on)
                if not isinstance(parsed, list):
                    raise ValueError("depends_on must be a JSON array")
                return parsed
            if parent_inject_id:
                # "success" (default) gates the child on the parent having executed;
                # "failure" branches on the parent NOT executing. Mirrors the app's
                # default edge condition (key "Execution", operator "eq").
                cond_value = (parent_condition or "success").strip().lower() != "failure"
                return [
                    {
                        "dependency_relationship": {"inject_parent_id": parent_inject_id},
                        "dependency_condition": {
                            "mode": "and",
                            "conditions": [
                                {"key": "Execution", "operator": "eq", "value": cond_value}
                            ],
                        },
                    }
                ]
            return None

        def _inject_input(for_create: bool) -> dict:
            # Only send fields the caller actually provided so an 'update' that
            # changes one field cannot clear the title or flip the enabled /
            # all_teams flags. On 'create' fall back to the API-friendly
            # defaults (enabled, not all-teams).
            body: dict = {}
            if title:
                body["inject_title"] = title
            _all_teams = all_teams if all_teams is not None else (False if for_create else None)
            if _all_teams is not None:
                body["inject_all_teams"] = _all_teams
            _enabled = enabled if enabled is not None else (True if for_create else None)
            if _enabled is not None:
                body["inject_enabled"] = _enabled
            if description:
                body["inject_description"] = description
            if injector_contract_id:
                body["inject_injector_contract"] = injector_contract_id
            if content:
                body["inject_content"] = json.loads(content)
            if depends_duration is not None:
                body["inject_depends_duration"] = depends_duration
            deps = _build_depends_on()
            if deps is not None:
                body["inject_depends_on"] = deps
            if team_ids is not None:
                body["inject_teams"] = team_ids
            if asset_ids is not None:
                body["inject_assets"] = asset_ids
            if asset_group_ids is not None:
                body["inject_asset_groups"] = asset_group_ids
            if tag_ids is not None:
                body["inject_tags"] = tag_ids
            return body

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", f"{base}/injects/simple",
                "list injects",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No injects found."
            lines = [f"Injects ({len(items)}):"]
            for i in items:
                lines.append(
                    f"- {i.get('inject_title', '?')} (id: {i.get('inject_id')}) "
                    f"enabled={i.get('inject_enabled')}"
                )
            return "\n".join(lines)

        if action == "get":
            if not inject_id:
                return "inject_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"{base}/injects/{inject_id}", "get inject"
            )
            if err:
                return err
            return inject_read_answer(resp.json(), offset=offset)

        if action in ("create", "update"):
            if action == "create" and (not title or not injector_contract_id):
                return "title and injector_contract_id are required for 'create'."
            if action == "update" and not inject_id:
                return "inject_id is required for 'update'."
            try:
                body = _inject_input(for_create=action == "create")
            except ValueError as exc:
                return f"content / depends_on must be valid JSON: {exc}"
            targets_note = ""
            if "inject_content" in body:
                assets, groups, targets_note, targets_err = mirror_content_targets(
                    body["inject_content"], asset_ids, asset_group_ids
                )
                if targets_err:
                    return targets_err
                if assets is not None:
                    body["inject_assets"] = assets
                if groups is not None:
                    body["inject_asset_groups"] = groups
            if action == "update" and not body:
                return (
                    "Provide at least one field to update (title, description, "
                    "injector_contract_id, content, depends_duration, team_ids, "
                    "asset_ids, asset_group_ids, all_teams, tag_ids, or enabled)."
                )
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", f"{base}/injects", "create inject",
                    json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"{base}/injects/{inject_id}",
                    "update inject", json=body,
                )
            if err:
                return err
            i = resp.json()
            verb = "Created" if action == "create" else "Updated"
            lines = [f"{verb} inject '{i.get('inject_title')}' (id: {i.get('inject_id')})."]
            lines += [t for t in (targets_note, inject_targets_line(i)) if t]
            return "\n".join(lines)

        if action == "delete":
            if not inject_id:
                return "inject_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"{base}/injects/{inject_id}",
                "delete inject", params=keep_findings_params(keep_findings),
            )
            if err:
                return err
            return f"Deleted inject {inject_id}. " + findings_fate(keep_findings, "inject")

        if action == "duplicate":
            if not inject_id:
                return "inject_id is required for action 'duplicate'."
            resp, err = await _api(
                base_url, headers, "post", f"{base}/injects/{inject_id}",
                "duplicate inject",
            )
            if err:
                return err
            i = resp.json()
            return f"Duplicated inject - new id: {i.get('inject_id')}."

        if action == "activation":
            if not inject_id:
                return "inject_id is required for action 'activation'."
            if enabled is None:
                return "enabled (true/false) is required for action 'activation'."
            _, err = await _api(
                base_url, headers, "put", f"{base}/injects/{inject_id}/activation",
                "set inject activation", json={"inject_enabled": enabled},
            )
            if err:
                return err
            return f"Inject {inject_id} enabled={enabled}."

        if action == "trigger":
            if scope != "simulation" or not inject_id:
                return "scope='simulation' and inject_id are required for 'trigger'."
            _, err = await _api(
                base_url, headers, "put", f"{base}/injects/{inject_id}/trigger",
                "trigger inject",
            )
            if err:
                return err
            return f"Triggered inject {inject_id}."

        if action == "set_status":
            if scope != "simulation" or not inject_id or not status:
                return "scope='simulation', inject_id and status are required for 'set_status'."
            _, err = await _api(
                base_url, headers, "post", f"{base}/injects/{inject_id}/status",
                "set inject status", json={"status": status, "message": message or None},
            )
            if err:
                return err
            return f"Inject {inject_id} status set to {status}."

        if action == "results":
            if scope != "simulation":
                return "scope='simulation' is required for 'results'."
            resp, err = await _api(
                base_url, headers, "get", f"{base}/injects/results", "inject results"
            )
            if err:
                return err
            rows = resp.json() or []
            if not isinstance(rows, list) or not rows:
                return "No inject results for this simulation yet."
            blocks = [f"Inject results ({len(rows)}):"]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                title = row.get("inject_title") or row.get("inject_id", "?")
                exec_status = row.get("inject_status") or {}
                if isinstance(exec_status, dict):
                    exec_label = exec_status.get("status_name") or "?"
                else:
                    exec_label = str(exec_status)
                blocks.append("")
                blocks.append(f"=== {title} ===")
                blocks.append(
                    f"  Execution status: {exec_label} (this only says whether the "
                    "attack RAN - the expectations below say whether the defense "
                    "worked)"
                )
                blocks.append(
                    _fmt_expectation_results_by_type(
                        row.get("inject_expectation_results") or [],
                        executed=_inject_row_executed(row),
                    )
                )
            return "\n".join(blocks)

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, duplicate, activation, trigger (simulation), set_status "
            "(simulation), results (simulation)."
        )

    executor.register("manage_openaev_inject", manage_openaev_inject)

    return [
        ToolDefinition(
            name="manage_openaev_inject",
            description=(
                "Manage OpenAEV injects (the individual attack actions). scope: "
                "'scenario' or 'simulation' (parent_id = scenario_id or exercise_id) "
                "or 'global' (by inject_id). Actions: 'list' (search), 'get', 'create' "
                "(title + injector_contract_id, optional content JSON, depends_duration "
                "seconds, team_ids, asset_ids, asset_group_ids, all_teams, tag_ids), "
                "'update' (inject_id + fields), 'delete' (inject_id, "
                "keep_findings), 'duplicate' (inject_id), 'activation' (inject_id + "
                "enabled). Build an ATTACK "
                "PATH (not flat atomic tests) by CHAINING injects: pass "
                "parent_inject_id so this inject runs AFTER that parent (optionally "
                "parent_condition 'success'|'failure'); the resulting dependency "
                "graph is what renders as the live attack-path map. Use depends_on "
                "(raw JSON array of inject_depends_on entries) for branching / "
                "multi-parent chains. Global-only: "
                "'status' (inject_id - read the execution status), 'target_traces' "
                "(inject_id - every target with its terminal trace). Simulation-only: "
                "'trigger' (manually fire an inject against live assets), 'set_status' "
                "(inject_id + status + optional message), 'results'. Use "
                "search_openaev_injector_contracts to find injector_contract_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "delete", "duplicate",
                            "activation", "trigger", "set_status", "results", "status",
                            "target_traces",
                        ],
                    },
                    "scope": {"type": "string", "enum": ["scenario", "simulation", "global"], "default": "scenario"},
                    "parent_id": {"type": "string", "description": "scenario_id (scope=scenario) or exercise_id (scope=simulation)."},
                    "inject_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "injector_contract_id": {"type": "string", "description": "Injector contract (inject type) ID."},
                    "content": {"type": "string", "description": "Inject content as a JSON object string."},
                    "depends_duration": {"type": "integer", "description": "Delay in seconds from simulation start."},
                    "parent_inject_id": {"type": "string", "description": "Chain this inject AFTER the given parent inject to build an attack path (the dependency graph renders as the live attack-path map). On create the child is bound automatically."},
                    "parent_condition": {"type": "string", "enum": ["success", "failure"], "default": "success", "description": "Gate the chain on the parent having executed ('success', default) or not ('failure')."},
                    "depends_on": {"type": "string", "description": "Advanced: raw JSON array of inject_depends_on entries ({dependency_relationship:{inject_parent_id}, dependency_condition:{mode,conditions:[{key:'Execution',operator:'eq',value:true}]}}) for branching or multi-parent chains."},
                    "team_ids": {"type": "array", "items": {"type": "string"}},
                    "asset_ids": {"type": "array", "items": {"type": "string"}},
                    "asset_group_ids": {"type": "array", "items": {"type": "string"}},
                    "all_teams": {"type": "boolean", "description": "Target all teams (create default false). Omit on update to leave unchanged."},
                    "tag_ids": {"type": "array", "items": {"type": "string"}},
                    "enabled": {"type": "boolean", "description": "Enabled state (create default true). Required for 'activation'; omit on update to leave unchanged."},
                    "status": {"type": "string", "description": "Inject status (set_status)."},
                    "message": {"type": "string", "description": "Status message (set_status)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "keep_findings": keep_findings_schema(),
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "get/status: character to read on from when the answer was cut.",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
