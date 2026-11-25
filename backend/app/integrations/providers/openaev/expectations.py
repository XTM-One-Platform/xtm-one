# SPDX-License-Identifier: Apache-2.0

"""OpenAEV expectation tools - reading the verdict on the defenses.

``manage_openaev_expectation`` is the analyst-facing view of expectation
results. An inject is the attack; an expectation is the scored judgement on
whether the security control did its job. Everything the platform reports as a
"score", an ATT&CK coverage figure or a posture percentage is computed from
expectations - never from inject execution status.

Deliberately EXCLUDED from this tool: the ``/api/injects/expectations*``
endpoints. Those are collector-facing polling routes designed for EDR/SIEM
integrations - they return only unfilled agent-level rows and they exist so a
security product can post its own verdict. Having an AI agent call them meant
(a) reading a queue instead of results, and (b) writing verdicts while
impersonating a security platform. The read paths here are the same ones the
OpenAEV UI uses, and the single write path (``validate``) is the documented
manual-validation endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    EXPECTATION_RESULT_TYPES,
    EXPECTATION_TYPES,
    _api,
    _fmt_expectation_results_by_type,
    _fmt_expectation_row,
    expectation_label,
    simulation_results_answer,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

#: ``io.openaev.utils.TargetType`` - the level a target-results read addresses.
_TARGET_TYPES = [
    "AGENT",
    "AGENTS",
    "ASSETS",
    "ASSETS_GROUPS",
    "AI_TARGETS",
    "PLAYERS",
    "TEAMS",
    "ENDPOINTS",
    "MANUAL",
]

#: Drift lives on three parents, each with its own base path.
_DRIFT_BASES = {
    "simulation": "/api/exercises",
    "scenario": "/api/scenarios",
    "atomic": "/api/atomic-testings",
}


def _attack_pattern_label(entry: dict) -> str:
    """Label the technique of one results-by-attack-pattern row.

    ``inject_attack_pattern`` is serialized with OpenAEV's ``MonoIdSerializer``
    (``@Schema(implementation = String.class)``), so it is a BARE ID STRING, not
    an object - calling ``.get()`` on it raises ``AttributeError``. Older builds
    that inline the object are still handled, mirroring the defensive read in
    ``services/ctem_metrics_collector``.
    """
    ap = entry.get("inject_attack_pattern")
    if isinstance(ap, dict):
        external_id = ap.get("attack_pattern_external_id") or ""
        name = ap.get("attack_pattern_name") or ap.get("attack_pattern_id") or "?"
        return f"{external_id} {name}".strip()
    if ap:
        # Only the id is available; the technique name is not in this payload.
        return f"attack pattern {ap}"
    return "unmapped"


def _fmt_drift(d: dict, scope: str, parent_id: str) -> str:
    """Render an ``ExpectationsDriftOutput``.

    Drift is a divergence between an inject's STORED expectations and the
    predefined expectations its injector contract exposes today. It is not an
    error and it does not mean the recorded results are wrong.
    """
    detected = bool(d.get("drift_detected"))
    drifted = d.get("drifted_inject_count", 0)
    total = d.get("total_inject_count", 0)
    dismissed = bool(d.get("drift_dismissed"))
    lines = [f"Expectations drift for {scope} {parent_id}:"]
    if not detected:
        lines.append(
            f"  No drift: all {total} inject(s) whose contract declares "
            "expectations match their contract."
        )
        return "\n".join(lines)
    lines.append(f"  Drift detected on {drifted} of {total} inject(s).")
    lines.append(f"  Dismissed: {'yes' if dismissed else 'no'}")
    lines.append(
        "  Meaning: injects inherit their contract's predefined expectations at "
        "creation time and keep them afterwards, so the contracts have since "
        "changed OR the expectations were customised on purpose. Drift is "
        "compared on the validation-defining attributes only (type, group "
        "semantics, expected security platforms) - a tuned score, name or "
        "expiration is never drift."
    )
    lines.append(
        "  'realign' overwrites the stored expectations from the contract and "
        "resets their expected score to 100, discarding intentional "
        "customisation. Prefer 'dismiss' when the difference is deliberate."
    )
    lines.append(
        "  Note: total_inject_count counts only injects whose contract declares "
        "expectations at all, not every inject."
    )
    return "\n".join(lines)


#: ``InjectExpectationService.COLLECTOR``: the ``sourceType`` of a result a
#: collector (EDR, SIEM, LLM firewall) wrote. Only those sources upload traces.
_COLLECTOR_SOURCE_TYPE = "collector"

#: ``GET /api/inject-expectations-traces`` (``InjectExpectationTraceApi``)
#: takes BOTH parameters as required ``@RequestParam``: a missing one is a bare
#: Spring 400 before OpenAEV's own logic runs.
_TRACES_PATH = "/api/inject-expectations-traces"

#: Upper bound on the collectors probed when the expectation row cannot be
#: located: past it the caller is asked for the row's locator or a source_id.
_MAX_PROBED_COLLECTORS = 10

_NO_COLLECTOR_SOURCE = (
    "No collector scored expectation {expectation_id}: traces exist only for "
    "collector results (an EDR, SIEM or firewall collector uploads the alert it "
    "matched). A manual, media-pressure or vulnerability-manager result has none, "
    "so there is nothing to read."
)


def _collector_sources(row: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``(sourceId, label)`` of each collector result on an expectation row.

    A result written by a collector carries ``sourceType`` ``collector``; the
    built-in sources (manual validation, media pressure, the vulnerability
    manager) carry their own type and never upload traces. A result without a
    ``sourceType`` (an older OpenAEV) is kept: an unknown collector id answers
    an empty list, never an error.
    """
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in row.get("inject_expectation_results") or []:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("sourceId") or "").strip()
        stype = str(r.get("sourceType") or "").strip().lower()
        if not sid or sid in seen or (stype and stype != _COLLECTOR_SOURCE_TYPE):
            continue
        seen.add(sid)
        sources.append((sid, str(r.get("sourceName") or sid)))
    return sources


def _traces_bad_request(err: str, expectation_id: str, source_id: str) -> str:
    """Name the parameters behind a 400 of the traces endpoint.

    The ``_api`` wording of a 400 ends in OpenAEV's bare ``Bad Request``, which
    tells the agent nothing about what to change.
    """
    if "bad request" not in err.lower():
        return err
    return (
        f"OpenAEV refused the traces read (HTTP 400). GET {_TRACES_PATH} requires "
        f"both injectExpectationId (sent: {expectation_id or 'none'}) and sourceId "
        f"(sent: {source_id or 'none'}), the id of a collector that scored the "
        "row (target_results lists each source). Check both ids. Original "
        f"error: {err}"
    )


def _fmt_trace_lines(items: list[Any]) -> list[str]:
    lines = []
    for t in items:
        if not isinstance(t, dict):
            continue
        name = t.get("inject_expectation_trace_alert_name", "?")
        date = t.get("inject_expectation_trace_date", "")
        link = t.get("inject_expectation_trace_alert_link")
        line = f"  - {name}"
        if date:
            line += f" ({date})"
        if link:
            line += f" {link}"
        lines.append(line)
    return lines


async def _find_row(
    base_url: str,
    headers: dict[str, str],
    expectation_id: str,
    *,
    inject_id: str,
    target_id: str,
    target_type: str,
    parent_target_id: str,
    exercise_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the expectation row through a locator the caller gave.

    OpenAEV has no read of one expectation by id: the row is found in the
    target results of its inject (inject_id + target_id + target_type) or in
    the expectations of its simulation (exercise_id). ``(None, None)`` means no
    locator was given or the row is not under it.
    """
    resolved = next(
        (t for t in _TARGET_TYPES if t.lower() == (target_type or "").strip().lower()),
        None,
    )
    if inject_id and target_id and resolved:
        params = {"parentTargetId": parent_target_id} if parent_target_id else None
        resp, err = await _api(
            base_url,
            headers,
            "get",
            f"/api/atomic-testings/{inject_id}/target_results/{target_id}/types/{resolved}",
            "get target results",
            params=params,
        )
    elif exercise_id:
        resp, err = await _api(
            base_url,
            headers,
            "get",
            f"/api/exercises/{exercise_id}/expectations",
            "get simulation expectations",
        )
    else:
        return None, None
    if err or resp is None:
        return None, err
    rows = resp.json() or []
    row = next(
        (r for r in rows if isinstance(r, dict) and r.get("inject_expectation_id") == expectation_id),
        None,
    )
    return row, None


async def _probe_collectors(base_url: str, headers: dict[str, str]) -> tuple[list[tuple[str, str]] | None, str | None]:
    """The collectors a traces read can address, when the row is not located.

    The traces endpoint resolves the collector's security platform, so only a
    collector bound to one can hold traces.
    """
    resp, err = await _api(base_url, headers, "get", "/api/collectors", "list collectors")
    if err or resp is None:
        return None, err
    sources = []
    for c in resp.json() or []:
        if not isinstance(c, dict) or not c.get("collector_security_platform"):
            continue
        cid = str(c.get("collector_id") or "").strip()
        if cid:
            sources.append((cid, str(c.get("collector_name") or cid)))
    return sources, None


async def _traces_answer(
    base_url: str,
    headers: dict[str, str],
    expectation_id: str,
    *,
    source_id: str,
    inject_id: str,
    target_id: str,
    target_type: str,
    parent_target_id: str,
    exercise_id: str,
) -> str:
    """Read the alert evidence of one expectation row, one call per collector.

    ``GET /api/inject-expectations-traces`` is keyed by expectation AND source:
    with no ``source_id`` the sources are the collector results of the row
    (located by the caller's locator), else every collector bound to a security
    platform. Traces are merged and labelled by source.
    """
    if source_id:
        sources = [(source_id, source_id)]
    else:
        row, err = await _find_row(
            base_url,
            headers,
            expectation_id,
            inject_id=inject_id,
            target_id=target_id,
            target_type=target_type,
            parent_target_id=parent_target_id,
            exercise_id=exercise_id,
        )
        if err:
            return err
        if row is not None:
            sources = _collector_sources(row)
        else:
            probed, err = await _probe_collectors(base_url, headers)
            if err:
                return err
            sources = probed or []
            if len(sources) > _MAX_PROBED_COLLECTORS:
                return (
                    f"{len(sources)} collectors could hold traces for expectation "
                    f"{expectation_id}. Pass source_id (the collector id a "
                    "target_results row lists), or locate the row with inject_id + "
                    "target_id + target_type, or exercise_id."
                )
        if not sources:
            return _NO_COLLECTOR_SOURCE.format(expectation_id=expectation_id)

    blocks: list[str] = []
    found = 0
    for sid, label in sources:
        resp, err = await _api(
            base_url,
            headers,
            "get",
            _TRACES_PATH,
            "get expectation traces",
            params={"injectExpectationId": expectation_id, "sourceId": sid},
        )
        if err or resp is None:
            err = _traces_bad_request(err or "no response", expectation_id, sid)
            if len(sources) == 1:
                return err
            blocks.append(f"Source {label} ({sid}): {err}")
            continue
        lines = _fmt_trace_lines(resp.json() or [])
        if not lines:
            continue
        found += len(lines)
        blocks.append(f"Source {label} ({sid}), {len(lines)} alert(s):")
        blocks.extend(lines)
    if not found:
        empty = (
            f"No alert evidence recorded for expectation {expectation_id} "
            f"(collector source(s) read: {', '.join(label for _, label in sources)}). "
            "Traces are optional: a collector can confirm a detection without "
            "pushing back an alert link, so an empty list does NOT contradict a "
            "SUCCESS verdict. A collector uploads traces only for the alerts it "
            "matched, so a Not Detected row normally has none."
        )
        return "\n".join([empty, *blocks]) if blocks else empty
    return "\n".join([f"Alert evidence ({found}):", *blocks])


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_expectation(
        action: str = "",
        exercise_id: str = "",
        exercise_ids: list[str] | None = None,
        scenario_id: str = "",
        inject_id: str = "",
        expectation_id: str = "",
        target_id: str = "",
        target_type: str = "",
        parent_target_id: str = "",
        expectation_type: str = "",
        source_id: str = "",
        score: float | None = None,
        dismissed: bool | None = None,
        scope: str = "simulation",
    ) -> str:
        action = (action or "").strip().lower()
        scope = (scope or "simulation").strip().lower()

        if action == "simulation_results":
            if not exercise_id:
                return "exercise_id is required for action 'simulation_results'."
            return await simulation_results_answer(_api, base_url, headers, exercise_id)

        if action == "global_scores":
            ids = [i for i in (exercise_ids or []) if i]
            if not ids and exercise_id:
                ids = [exercise_id]
            if not ids:
                return (
                    "exercise_ids (or exercise_id) is required for action "
                    "'global_scores'."
                )
            resp, err = await _api(
                base_url, headers, "post", "/api/exercises/global-scores",
                "get global scores", json={"exercise_ids": ids},
            )
            if err:
                return err
            data = resp.json() or {}
            by_id = data.get("global_scores_by_exercise_ids", data)
            if not isinstance(by_id, dict) or not by_id:
                return "No global scores returned for those simulations."
            blocks = []
            for ex_id, results in by_id.items():
                blocks.append(
                    _fmt_expectation_results_by_type(
                        results, subject=f"simulation {ex_id}"
                    )
                )
            return "\n\n".join(blocks)

        if action == "by_attack_pattern":
            if not exercise_id:
                return "exercise_id is required for action 'by_attack_pattern'."
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/exercises/{exercise_id}/injects/results-by-attack-patterns",
                "get results by attack pattern",
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return (
                    "No ATT&CK-mapped results for this simulation. Injects only "
                    "appear here when their injector contract is mapped to an "
                    "attack pattern."
                )
            blocks = [
                "Results per ATT&CK technique. Note this view counts only "
                "expectations whose target is a DIRECT relation of the inject, so "
                "it can legitimately differ from 'simulation_results' (an asset "
                "reached only through an asset group is represented by the group "
                "row there). An inject mapped to several techniques contributes "
                "its results once per technique."
            ]
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                blocks.append("")
                blocks.append(f"=== {_attack_pattern_label(entry)} ===")
                # The per-technique payload nests one level: the outer field is
                # `inject_expectation_results` (a list of per-inject rows), and
                # each row carries its own `results` list of the by-type
                # verdicts.
                for row in entry.get("inject_expectation_results") or []:
                    if not isinstance(row, dict):
                        continue
                    blocks.append(
                        _fmt_expectation_results_by_type(
                            row.get("results") or [],
                            subject=row.get("inject_title")
                            or row.get("inject_id")
                            or "inject",
                        )
                    )
            return "\n".join(blocks)

        if action in ("target_results", "target_results_merged"):
            if not inject_id or not target_id or not target_type:
                return (
                    "inject_id, target_id and target_type are required for "
                    f"'{action}'. target_type is one of: {', '.join(_TARGET_TYPES)}."
                )
            resolved = next(
                (t for t in _TARGET_TYPES if t.lower() == target_type.strip().lower()),
                None,
            )
            if resolved is None:
                return (
                    f"target_type must be one of: {', '.join(_TARGET_TYPES)} "
                    f"(got '{target_type}')."
                )
            path = f"/api/atomic-testings/{inject_id}/target_results/{target_id}/types/{resolved}"
            params: dict | None = None
            if action == "target_results_merged":
                path += "/merged"
            elif parent_target_id:
                params = {"parentTargetId": parent_target_id}
            resp, err = await _api(
                base_url, headers, "get", path, "get target results", params=params
            )
            if err:
                return err
            rows = resp.json() or []
            if not rows:
                return (
                    f"No expectation rows for target {target_id} on inject "
                    f"{inject_id}. The inject may not have executed against it yet."
                )
            header = f"Expectation rows for {resolved} {target_id} ({len(rows)}):"
            if action == "target_results_merged":
                header += (
                    "\n(merged view: one row per expectation type, score = the max "
                    "across results, and collector-sourced results are OMITTED - "
                    "use target_results to see which security platform reported)"
                )
            return header + "\n\n" + "\n\n".join(_fmt_expectation_row(r) for r in rows)

        if action == "agent_breakdown":
            if not inject_id or not target_id or not expectation_type:
                return (
                    "inject_id, target_id (the asset) and expectation_type are "
                    "required for 'agent_breakdown'."
                )
            resolved_type = next(
                (
                    t
                    for t in EXPECTATION_TYPES
                    if t.lower() == expectation_type.strip().lower()
                ),
                None,
            )
            if resolved_type is None:
                return (
                    f"expectation_type must be one of: {', '.join(EXPECTATION_TYPES)} "
                    f"(got '{expectation_type}')."
                )
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/atomic-testings/{inject_id}/target_results/{target_id}/asset_with_agents",
                "get agent breakdown",
                params={"expectationType": resolved_type},
            )
            if err:
                return err
            rows = resp.json() or []
            if not rows:
                return (
                    f"No per-agent {resolved_type} rows under asset {target_id}. The "
                    "asset may be agentless (an AI target or an agentless endpoint), "
                    "in which case the asset row IS the leaf and carries the verdict."
                )
            lines = [
                f"Per-agent {resolved_type} results under asset {target_id} "
                f"({len(rows)} agent(s)). Agent rows are the ground truth written by "
                "collectors; the asset above them requires ALL its agents to succeed:"
            ]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # InjectExpectationAgentOutput exposes the readable name as
                # `inject_expectation_agent_name`; `inject_expectation_agent` is
                # only the agent id, so it is a last-resort fallback.
                agent = (
                    row.get("inject_expectation_agent_name")
                    or row.get("inject_expectation_agent")
                    or "?"
                )
                status = str(row.get("inject_expectation_status") or "UNKNOWN").upper()
                lines.append(
                    f"  - {agent}: {status} ({expectation_label(resolved_type, status)})"
                )
            return "\n".join(lines)

        if action == "traces":
            if not expectation_id:
                return "expectation_id is required for action 'traces'."
            return await _traces_answer(
                base_url,
                headers,
                expectation_id,
                source_id=source_id,
                inject_id=inject_id,
                target_id=target_id,
                target_type=target_type,
                parent_target_id=parent_target_id,
                exercise_id=exercise_id,
            )

        if action in ("drift", "realign_drift", "dismiss_drift"):
            root = _DRIFT_BASES.get(scope)
            if root is None:
                return (
                    "scope must be 'simulation', 'scenario' or 'atomic' for the "
                    "drift actions."
                )
            parent_id = (
                inject_id
                if scope == "atomic"
                else (scenario_id if scope == "scenario" else exercise_id)
            )
            if not parent_id:
                needed = {
                    "atomic": "inject_id",
                    "scenario": "scenario_id",
                    "simulation": "exercise_id",
                }[scope]
                return f"{needed} is required for '{action}' with scope='{scope}'."
            base = f"{root}/{parent_id}/expectations-drift"
            if action == "drift":
                resp, err = await _api(
                    base_url, headers, "get", base, "get expectations drift"
                )
                if err:
                    return err
                return _fmt_drift(resp.json() or {}, scope, parent_id)
            if action == "realign_drift":
                resp, err = await _api(
                    base_url, headers, "post", f"{base}/realign",
                    "realign expectations drift",
                )
                if err:
                    return err
                out = resp.json() if resp.content else {}
                count = out.get("realigned_inject_count", "?")
                return (
                    f"Realigned {count} inject(s) in {scope} {parent_id}: their "
                    "expectations were rewritten from the injector contracts and "
                    "their expected score reset to 100. Any intentional "
                    "customisation is gone, and the drift dismissal was reset."
                )
            if dismissed is None:
                return (
                    "dismissed (true to downgrade the drift warning to an "
                    "indicator, false to restore it) is required for "
                    "'dismiss_drift'."
                )
            resp, err = await _api(
                base_url, headers, "put", f"{base}/dismiss",
                "dismiss expectations drift", json={"dismissed": dismissed},
            )
            if err:
                return err
            verb = "Dismissed" if dismissed else "Restored"
            return (
                f"{verb} the expectations-drift warning for {scope} {parent_id}. "
                "The dismissal is shared between all users and is reset by a "
                "realignment."
            )

        if action == "validate":
            if not expectation_id or score is None:
                return (
                    "expectation_id and score are required for 'validate'. Score "
                    "is compared against the expectation's expected score - "
                    "SUCCESS needs score >= expected (technical expectations "
                    "default to 100, manual ones to 50)."
                )
            body = {
                "source_id": "xtm-one",
                "source_type": "ui",
                "source_name": "XTM One agent",
                "expectation_score": score,
            }
            _, err = await _api(
                base_url, headers, "put", f"/api/expectations/{expectation_id}",
                "validate expectation", json=body,
            )
            if err:
                if "asset group" in err.lower():
                    return (
                        "OpenAEV refuses direct validation of an ASSET GROUP "
                        "expectation: a group score is always derived from its "
                        "assets. Validate the asset (which fans the verdict out to "
                        "its agents) or an individual agent instead, and the group "
                        "will be recomputed. Original error: " + err
                    )
                return err
            return (
                f"Recorded a manual validation of expectation {expectation_id} with "
                f"score {score}. Note the fan-out rules: validating an ASSET that "
                "has agents writes the verdict onto every agent row underneath it, "
                "and validating a TEAM overwrites every player row with the team "
                "score. The parent rows are then recomputed from those children."
            )

        return (
            f"Unknown action '{action}'. Valid actions: simulation_results, "
            "global_scores, by_attack_pattern, target_results, "
            "target_results_merged, agent_breakdown, traces, drift, "
            "realign_drift, dismiss_drift, validate."
        )

    executor.register("manage_openaev_expectation", manage_openaev_expectation)

    return [
        ToolDefinition(
            name="manage_openaev_expectation",
            description=(
                "Read and interpret OpenAEV EXPECTATIONS - the scored verdicts on "
                "whether the security controls did their job. Use this, not "
                "inject execution status, for any question about security "
                "posture, detection coverage or whether a simulation went well.\n"
                "CRITICAL SEMANTICS: an inject's execution status only says the "
                "attack RAN; expectations say whether the defense WORKED. A "
                "simulation where every inject EXECUTED but the detection "
                "expectations FAILED is a security failure, not a success. "
                "PREVENTION success = the attack was blocked. DETECTION success = "
                "it was alerted on but still ran. VULNERABILITY is INVERTED - "
                "success means NOT vulnerable, failure means a scanner proved the "
                "target vulnerable. PENDING is not FAILED: it means the collection "
                "window is still open.\n"
                "Actions: 'simulation_results' (exercise_id - the per-type verdict "
                "for a whole simulation), 'global_scores' (exercise_ids - the same "
                "for several simulations at once), 'by_attack_pattern' "
                "(exercise_id - results per ATT&CK technique), 'target_results' "
                "(inject_id + target_id + target_type - the individual expectation "
                "rows for one target, including which security platform reported "
                "what), 'target_results_merged' (one row per type, collector "
                "sources omitted), 'agent_breakdown' (inject_id + target_id asset + "
                "expectation_type - per-agent detail under an asset), 'traces' "
                "(expectation_id - the alert evidence behind a verdict, read per "
                "collector source of the row), 'drift' / "
                "'realign_drift' / 'dismiss_drift' (scope simulation|scenario|"
                "atomic - whether stored expectations still match their injector "
                "contracts), 'validate' (expectation_id + score - record a MANUAL "
                "validation; asset-group rows cannot be validated directly)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "simulation_results", "global_scores",
                            "by_attack_pattern", "target_results",
                            "target_results_merged", "agent_breakdown", "traces",
                            "drift", "realign_drift", "dismiss_drift", "validate",
                        ],
                    },
                    "exercise_id": {"type": "string", "description": "Simulation (exercise) ID."},
                    "exercise_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Simulation IDs for 'global_scores'.",
                    },
                    "scenario_id": {"type": "string", "description": "Scenario ID (drift actions with scope='scenario')."},
                    "inject_id": {"type": "string", "description": "Inject / atomic test ID."},
                    "expectation_id": {"type": "string", "description": "Expectation row ID (traces, validate). For traces, inject_id + target_id + target_type or exercise_id locate its row."},
                    "target_id": {"type": "string", "description": "Target entity ID (asset, agent, team, player, asset group)."},
                    "target_type": {
                        "type": "string",
                        "enum": _TARGET_TYPES,
                        "description": "Level being read. Agent rows are the ground truth; asset and group rows are derived from them.",
                    },
                    "parent_target_id": {"type": "string", "description": "Parent target ID when reading an asset reached through an asset group."},
                    "expectation_type": {
                        "type": "string",
                        "enum": EXPECTATION_TYPES,
                        "description": (
                            "Stored expectation type for 'agent_breakdown'. Results "
                            f"aggregate into the families {', '.join(EXPECTATION_RESULT_TYPES)} "
                            "(MANUAL, ARTICLE and CHALLENGE all report as "
                            "HUMAN_RESPONSE)."
                        ),
                    },
                    "source_id": {
                        "type": "string",
                        "description": "Collector ID for 'traces'. Optional: without it the tool reads every collector source of the row.",
                    },
                    "score": {
                        "type": "number",
                        "description": (
                            "Score for 'validate'. SUCCESS requires score >= the "
                            "expectation's expected score (100 for technical "
                            "expectations, 50 for manual ones by default). A value "
                            "strictly between 0 and expected produces a PARTIAL "
                            "contribution to aggregates."
                        ),
                    },
                    "dismissed": {"type": "boolean", "description": "For 'dismiss_drift': true to downgrade the warning, false to restore it."},
                    "scope": {
                        "type": "string",
                        "enum": ["simulation", "scenario", "atomic"],
                        "default": "simulation",
                        "description": "Which parent the drift actions address.",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
