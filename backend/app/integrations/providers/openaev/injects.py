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
from datetime import UTC, datetime, timedelta
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    FINDINGS_KEPT_AS_HISTORY,
    _api,
    _fmt_expectation_results_by_type,
    _inject_row_executed,
    _relation_ids,
    _search_body,
    inject_read_answer,
    inject_targets_line,
    json_window,
    mirror_content_targets,
)
from .testing import _contract_id, _stored_documents

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"


#: Target types OpenAEV resolves execution traces for (TargetType.hasExecutionTraces).
_TRACE_TARGET_TYPES = ("ASSETS", "AGENT", "TEAMS", "PLAYERS", "AI_TARGETS", "MANUAL")
#: Page size of the per-type targets search, and the targets read per type at most.
_TRACE_SEARCH_PAGE = 200
_TRACE_TARGETS_MAX_PER_TYPE = 5000
#: Targets listed per answer by default and at most (offset / limit page the rest).
_TRACE_TARGETS_PER_ANSWER = 100
_TRACE_TARGETS_PER_ANSWER_MAX = 300
#: Up to this many targets, every trace is read so the status counts cover all of them.
_TRACE_STATUS_READS_MAX = 600
_TRACE_READS_IN_FLIGHT = 8


def _terminal_trace(traces: list[Any]) -> dict[str, Any] | None:
    """The trace that ends a target's run: its last COMPLETE trace, else its latest."""
    rows = [t for t in traces if isinstance(t, dict)]
    if not rows:
        return None
    rows.sort(key=lambda t: str(t.get("execution_time") or ""))
    complete = [t for t in rows if t.get("execution_action") == "COMPLETE"]
    return (complete or rows)[-1]


#: Trace statuses that end a step without a failure.
_OK_TRACE_STATUSES = frozenset({"SUCCESS", "INFO"})
#: Characters of a failing step's message kept in the answer (stdout/stderr JSON).
_CAUSE_MESSAGE_CHARS = 600


def _failure_cause(traces: list[Any], terminal: dict[str, Any]) -> dict[str, Any] | None:
    """The step that made a non-success terminal trace fail.

    The implant's last COMPLETE trace always reads 'Payload completed'; OpenAEV
    rewrites its status from the agent's earlier traces, so the reason of an
    ERROR lives in the latest earlier non-COMPLETE trace whose status is not a
    success (a COMMAND_EXECUTION or PREREQUISITE step with its stdout, stderr
    and exit code).
    """
    if str(terminal.get("execution_status") or "").upper() in _OK_TRACE_STATUSES:
        return None
    rows = [
        t
        for t in traces
        if isinstance(t, dict)
        and t is not terminal
        and t.get("execution_action") != "COMPLETE"
        and str(t.get("execution_status") or "").upper() not in _OK_TRACE_STATUSES
    ]
    if not rows:
        return None
    rows.sort(key=lambda t: str(t.get("execution_time") or ""))
    return rows[-1]


def _cause_line(cause: dict[str, Any]) -> str:
    message = str(cause.get("execution_message") or "")
    if len(message) > _CAUSE_MESSAGE_CHARS:
        message = message[:_CAUSE_MESSAGE_CHARS] + f"... [{len(message) - _CAUSE_MESSAGE_CHARS} more chars]"
    return (
        f"    cause: {cause.get('execution_action', '?')} {cause.get('execution_status', '?')} "
        f"at {cause.get('execution_time', '?')}: {message}"
    )


#: The runtime setting that bounds an inject's execution (InjectExecutionThresholdService).
_THRESHOLD_KEY = "inject_execution_threshold_minutes"


def _utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _threshold_minutes(settings: Any) -> int | None:
    items = settings.get("runtime_settings") if isinstance(settings, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("key") == _THRESHOLD_KEY:
            try:
                return int(str(item.get("value")))
            except ValueError:
                return None
    return None


def _execution_timeout_line(sent: datetime | None, minutes: int | None) -> str:
    """When OpenAEV ends the targets that gave no terminal trace (the finalization job)."""
    if minutes is None:
        bound = "threshold not exposed by this OpenAEV (its default is 10 minutes)"
    else:
        bound = f"{minutes} min after distribution"
    if sent is None:
        return f"execution_timeout: {bound}; not distributed yet (no tracking_sent_date)."
    deadline = f", deadline {_stamp(sent + timedelta(minutes=minutes))}" if minutes is not None else ""
    return (
        f"execution_timeout: {bound} (sent {_stamp(sent)}{deadline}); a target with no terminal "
        "trace by then is ended with a timeout trace and the inject ends."
    )


def _expectation_window_line(inject: Any, sent: datetime | None) -> str:
    """How long the expectations wait for a result: a scoring window, not the run."""
    content = inject.get("inject_content") if isinstance(inject, dict) else None
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            content = None
    rows = content.get("expectations") if isinstance(content, dict) else None
    parts: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        seconds = row.get("expectation_expiration_time") if isinstance(row, dict) else None
        if not isinstance(seconds, int | float) or seconds <= 0:
            continue
        part = f"{row.get('expectation_type', '?')} {seconds / 3600:g} h"
        if sent is not None:
            part += f" (expires {_stamp(sent + timedelta(seconds=seconds))})"
        parts.append(part)
    if not parts:
        return "expectation_window: no expiration time on the inject's expectations (OpenAEV's default applies)."
    return (
        f"expectation_window: {', '.join(parts)}; the time the expectations wait for a "
        "result after distribution, not the end of the execution."
    )


async def _timing_lines(base_url: str, headers: dict[str, str], inject_id: str) -> list[str]:
    """The execution timeout and the expectation window, two named facts."""
    status, inject, settings = await asyncio.gather(
        _api(base_url, headers, "get", "/api/injects/status", "inject status", params={"injectId": inject_id}),
        _api(base_url, headers, "get", f"/api/injects/{inject_id}", "get inject"),
        _api(base_url, headers, "get", "/api/settings", "get settings"),
    )
    status_body = status[0].json() if not status[1] and status[0] is not None else None
    sent = _utc(status_body.get("tracking_sent_date")) if isinstance(status_body, dict) else None
    lines: list[str] = []
    if status[1]:
        lines.append(f"status not read ({status[1]})")
    minutes = None
    if settings[1] or settings[0] is None:
        lines.append(f"settings not read ({settings[1]})")
    else:
        minutes = _threshold_minutes(settings[0].json())
    lines.append(_execution_timeout_line(sent, minutes))
    if inject[1] or inject[0] is None:
        lines.append(f"expectation_window: inject not read ({inject[1]})")
    else:
        lines.append(_expectation_window_line(inject[0].json(), sent))
    return lines


def _target_label(target: dict[str, Any]) -> str:
    name = target.get("target_name") or target.get("target_id") or "?"
    return f"{target.get('target_type', '?')} {name} (id: {target.get('target_id', '?')})"


async def _targets_of_type(
    base_url: str, headers: dict[str, str], inject_id: str, target_type: str
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Every target of one type, paged to the end: (targets, total OpenAEV counts, error)."""
    rows: list[dict[str, Any]] = []
    total: int | None = None
    page = 0
    while len(rows) < _TRACE_TARGETS_MAX_PER_TYPE:
        resp, err = await _api(
            base_url,
            headers,
            "post",
            f"/api/injects/{inject_id}/targets/{target_type}/search",
            "list inject targets",
            json=_search_body(page=page, size=_TRACE_SEARCH_PAGE),
        )
        if err or resp is None:
            return rows, total, f"page {page + 1} not read ({err})"
        data = resp.json()
        content = data.get("content", []) if isinstance(data, dict) else data
        content = content if isinstance(content, list) else []
        if isinstance(data, dict) and isinstance(data.get("totalElements"), int):
            total = data["totalElements"]
        for row in content:
            if isinstance(row, dict) and row.get("target_id"):
                row.setdefault("target_type", target_type)
                rows.append(row)
        page += 1
        if len(content) < _TRACE_SEARCH_PAGE or (total is not None and len(rows) >= total):
            break
    return rows[:_TRACE_TARGETS_MAX_PER_TYPE], total, None


def _status_counts(statuses: list[tuple[str, str]]) -> list[str]:
    """'TYPE N: STATUS n, ...' per target type, statuses by count."""
    by_type: dict[str, dict[str, int]] = {}
    for target_type, status in statuses:
        counts = by_type.setdefault(target_type, {})
        counts[status] = counts.get(status, 0) + 1
    lines = []
    for target_type, counts in by_type.items():
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        parts = ", ".join(f"{status} {n}" for status, n in ranked)
        lines.append(f"  {target_type} {sum(counts.values())}: {parts}")
    return lines


async def _target_traces(
    base_url: str,
    headers: dict[str, str],
    inject_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """Every target of an inject with its terminal execution trace.

    The global status trace names only the targets an executor reached; a
    target OpenAEV closed before the distribution (agentless asset, inactive or
    overloaded agent, no executor, clearance) has its own COMPLETE trace, read
    here per target from /api/injects/execution-traces. The execution timeout
    and the expectation window head the answer as two named lines, so a
    running inject is told apart from an expired expectation.

    The targets search is paged to the end for every type, so the per-type
    totals and the per-status counts cover every target; the listed targets are
    a window (offset, limit) over the combined list, and a cut answer ends with
    a 'truncated' line naming the offset of the next call.
    """
    timing = await _timing_lines(base_url, headers, inject_id)
    targets: list[dict[str, Any]] = []
    type_totals: list[str] = []
    notes: list[str] = []
    for target_type in _TRACE_TARGET_TYPES:
        rows, total, err = await _targets_of_type(base_url, headers, inject_id, target_type)
        if err and not rows:
            notes.append(f"{target_type}: targets not read ({err})")
            continue
        targets += rows
        if rows or total:
            type_totals.append(f"{target_type} {total if isinstance(total, int) else len(rows)}")
        if err:
            notes.append(f"{target_type}: {len(rows)} of {total if total is not None else '?'} targets read, {err}")
        elif isinstance(total, int) and total > len(rows):
            notes.append(
                f"{target_type}: {len(rows)} of {total} targets read "
                f"(cap {_TRACE_TARGETS_MAX_PER_TYPE} per type); the rest are not listed or counted"
            )
    if not targets:
        return "\n".join([f"Inject {inject_id} has no target with execution traces.", *timing, *notes])

    offset = max(0, int(offset or 0))
    size = max(1, min(int(limit or _TRACE_TARGETS_PER_ANSWER), _TRACE_TARGETS_PER_ANSWER_MAX))
    if offset >= len(targets):
        return (
            f"offset {offset} is past the last target: inject {inject_id} has "
            f"{len(targets)} target(s) ({', '.join(type_totals)})."
        )
    window = targets[offset : offset + size]
    counted = targets if len(targets) <= _TRACE_STATUS_READS_MAX else window

    gate = asyncio.Semaphore(_TRACE_READS_IN_FLIGHT)

    async def _one(target: dict[str, Any]) -> tuple[str, str]:
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
            return "NOT_READ", f"- {_target_label(target)}: traces not read ({err})"
        traces = resp.json()
        last = _terminal_trace(traces if isinstance(traces, list) else [])
        if last is None:
            return "NO_TRACE", f"- {_target_label(target)}: no trace yet"
        line = (
            f"- {_target_label(target)}: {last.get('execution_status', '?')} "
            f"{last.get('execution_action', '?')} at {last.get('execution_time', '?')}: "
            f"{last.get('execution_message', '')}"
        )
        cause = _failure_cause(traces if isinstance(traces, list) else [], last)
        if cause is not None:
            line += "\n" + _cause_line(cause)
        return str(last.get("execution_status") or "?"), line

    results = await asyncio.gather(*(_one(t) for t in counted))
    by_target = {id(t): r for t, r in zip(counted, results, strict=True)}
    scope = "every target" if counted is targets else f"the {len(window)} target(s) of this page only"
    head = [
        f"Inject {inject_id}: {len(targets)} target(s) ({', '.join(type_totals)}).",
        f"Terminal status counts over {scope}:",
        *_status_counts([(str(t.get("target_type")), by_target[id(t)][0]) for t in counted]),
        f"Targets {offset + 1}-{offset + len(window)} of {len(targets)}, terminal trace of each:",
    ]
    lines = [by_target[id(t)][1] for t in window]
    rest = len(targets) - offset - len(window)
    if rest > 0:
        lines.append(f"truncated: {rest} more target(s), call again with offset={offset + len(window)}.")
    return "\n".join([*timing, *head, *lines, *notes])


def _base(scope: str, parent_id: str) -> str | None:
    if scope == "scenario":
        return f"/api/scenarios/{parent_id}"
    if scope == "simulation":
        return f"/api/exercises/{parent_id}"
    return None


#: The relations of a stored inject an update sends back, as (InjectInput
#: key, id key of an element when it is an object). OpenAEV's PUT replaces
#: each one: a relation left out is stored empty.
_INJECT_RELATIONS: tuple[tuple[str, str], ...] = (
    ("inject_teams", "team_id"),
    ("inject_assets", "asset_id"),
    ("inject_asset_groups", "asset_group_id"),
    ("inject_tags", "tag_id"),
)

#: Scalars of InjectInput carried over as the stored inject answers them.
_INJECT_SCALARS = (
    "inject_title",
    "inject_description",
    "inject_content",
    "inject_depends_duration",
    "inject_all_teams",
    "inject_enabled",
    "inject_country",
    "inject_city",
)


def _stored_dependencies(raw: Any) -> list[dict[str, Any]]:
    """The stored ``inject_depends_on`` as InjectDependencyInput entries."""
    out: list[dict[str, Any]] = []
    for dep in raw if isinstance(raw, list) else []:
        if not isinstance(dep, dict):
            continue
        rel = dep.get("dependency_relationship")
        parent = rel.get("inject_parent_id") if isinstance(rel, dict) else None
        if isinstance(parent, dict):
            parent = parent.get("inject_id")
        if not parent:
            continue
        entry: dict[str, Any] = {"dependency_relationship": {"inject_parent_id": str(parent)}}
        if dep.get("dependency_condition") is not None:
            entry["dependency_condition"] = dep["dependency_condition"]
        out.append(entry)
    return out


def stored_inject_input(stored: Any, changes: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The full InjectInput of an update: the stored inject, *changes* laid over it.

    OpenAEV's PUT /api/{scenarios|exercises}/{id}/injects/{inject_id} replaces
    every field: a title left out is refused, a relation left out is stored
    empty, ``inject_enabled`` falls back to true, ``inject_all_teams`` to false
    and an empty ``inject_depends_on`` removes the chain. Returns
    ``(body, refusal)``; a refusal names what the stored answer lacks so the
    update never wipes it. Nothing is written when it is not empty.
    """
    if not isinstance(stored, dict) or not stored:
        return (
            {},
            "Refused: the stored inject could not be read, so an update would wipe its fields. Nothing was written.",
        )
    body: dict[str, Any] = {k: stored[k] for k in _INJECT_SCALARS if k in stored}
    body["inject_injector_contract"] = _contract_id(stored.get("inject_injector_contract"))
    injector = stored.get("inject_injector")
    if isinstance(injector, dict):
        injector = injector.get("injector_id")
    if isinstance(injector, str) and injector and "inject_injector_contract" not in changes:
        body["inject_injector"] = injector
    missing: list[str] = []
    for key, id_key in _INJECT_RELATIONS:
        if key in changes:
            continue
        if key not in stored:
            missing.append(key)
            continue
        body[key] = _relation_ids(stored.get(key), id_key)
    if "inject_secret_references" in stored:
        body["inject_secret_references"] = _relation_ids(stored.get("inject_secret_references"), "secret_reference_id")
    if "inject_depends_on" in stored:
        body["inject_depends_on"] = _stored_dependencies(stored.get("inject_depends_on"))
    if "inject_documents" in stored:
        body["inject_documents"] = _stored_documents(stored.get("inject_documents"))
    body.update(changes)
    if not body.get("inject_title"):
        missing.append("inject_title (pass title)")
    if not body.get("inject_injector_contract"):
        missing.append("inject_injector_contract (pass injector_contract_id)")
    if missing:
        return {}, (
            "Refused: OpenAEV replaces every field of an inject on update, and the "
            "stored inject as read lacks " + ", ".join(missing) + ", so the update "
            "would wipe it. Nothing was written."
        )
    return body, ""


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
        limit: int | None = None,
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
                return await _target_traces(base_url, headers, inject_id, offset=offset, limit=limit)
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
                json=_search_body(size=min(limit or 20, 50), text_search=search),
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
                resp, err = await _api(base_url, headers, "get", f"{base}/injects/{inject_id}", "read inject")
                if err:
                    return err
                body, refusal = stored_inject_input(resp.json() if resp is not None else None, body)
                if refusal:
                    return refusal
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
                "delete inject",
            )
            if err:
                return err
            return f"Deleted inject {inject_id}. {FINDINGS_KEPT_AS_HISTORY}"

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
                "'update' (inject_id + fields; the others are kept), 'delete' (inject_id; its "
                "findings stay as history), 'duplicate' (inject_id), 'activation' (inject_id + "
                "enabled). Build an ATTACK "
                "PATH (not flat atomic tests) by CHAINING injects: pass "
                "parent_inject_id so this inject runs AFTER that parent (optionally "
                "parent_condition 'success'|'failure'); the resulting dependency "
                "graph is what renders as the live attack-path map. Use depends_on "
                "(raw JSON array of inject_depends_on entries) for branching / "
                "multi-parent chains. Global-only: "
                "'status' (inject_id - read the execution status), 'target_traces' "
                "(inject_id - each target's terminal trace, counts, timing). Simulation-only: "
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
                    "limit": {"type": "integer"},
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "get/status: char offset of a cut answer; target_traces: first target (limit per answer, default 100).",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
