# SPDX-License-Identifier: Apache-2.0

"""OpenAEV PREVIEW-feature tools: attack path and the chaining engine.

Both surfaces are gated by an ``enabled_dev_features`` flag and, crucially,
answer **404 rather than 403** when the flag is off - indistinguishable from a
wrong URL. So each tool checks ``PlatformCapabilities`` first and returns a
precise explanation instead of letting the agent chase a phantom id:

* ``query_openaev_attack_path`` - ``ATTACK_PATH``. Read-only causal graph of a
  simulation: which endpoints were reached, through which executions, and what
  was found on them. The ``/seed`` endpoint is deliberately NOT exposed - it is
  a synthetic data generator the platform marks for removal before production.
* ``query_openaev_chaining`` - ``INJECT_CHAINING``. Read-only view of workflow
  configuration, its steps and its condition trees. Every chaining route exists
  ONLY in tenant-scoped form, so the tenant id is resolved automatically.
  Authoring step/condition trees is not exposed: it is a graph-editing surface
  where a malformed write silently changes execution order.
"""

from __future__ import annotations

import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import PlatformCapabilities, _api, json_answer

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

_ATTACK_PATH = "/api/attack-path"
#: The chaining engine's routes sit at the tenant root (``StepApi``,
#: ``ConditionApi``, ``WorkflowApi``: ``/api/tenants/{id}/steps``, ...), with no
#: ``/chaining`` segment; ``_api`` adds the tenant prefix.
_STEPS = "/api/steps"
_CONDITIONS = "/api/conditions"
_WORKFLOWS = "/api/workflows"

#: Finding categories the attack-path drawer understands. An unknown category
#: returns an empty page rather than an error, so validate locally.
_FINDING_CATEGORIES = ["credentials", "users", "files", "cves"]

#: Sentinel / fabricated id tokens the model emits when it does not actually
#: have a real id (the "Workflow ID : placeholder" 404 the operator hit). OpenAEV
#: turns any of these into a confusing 404 (or, for RBAC-masked routes, an
#: indistinguishable "not found"), so reject them locally with the fix instead of
#: round-tripping a guaranteed failure. Compared case-insensitively after strip.
_PLACEHOLDER_ID_TOKENS = frozenset(
    {
        "placeholder",
        "example",
        "todo",
        "tbd",
        "none",
        "null",
        "n/a",
        "na",
        "id",
        "your-workflow-id",
        "workflow-id",
        "workflow_id",
        "your_workflow_id",
        "step-id",
        "step_id",
        "condition-id",
        "condition_id",
        "xxx",
        "xxxx",
        "unknown",
        "string",
    }
)


def _placeholder_id_error(field: str, value: str, how_to_get: str) -> str | None:
    """Return an error when *value* is a sentinel/fabricated id, else ``None``.

    Catches empty values, obvious placeholders (``placeholder``, ``example``,
    ``your-workflow-id``, ...) and template markers (``{{...}}``, ``<...>``,
    ``$var``) so an invented id fails locally with the exact way to resolve a
    real one, rather than surfacing as OpenAEV's overloaded 404.
    """
    raw = (value or "").strip()
    if not raw:
        return f"{field} is required. {how_to_get}"
    lowered = raw.lower()
    is_placeholder = (
        lowered in _PLACEHOLDER_ID_TOKENS
        or lowered.startswith(("{{", "<", "$", "your-", "your_", "example-", "example_"))
        or lowered.endswith(("}}", ">"))
        or "placeholder" in lowered
    )
    if is_placeholder:
        return (
            f"{field}='{value}' is not a real id - it looks like a placeholder or a "
            f"template variable. Never invent an id. {how_to_get}"
        )
    return None


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    capabilities: PlatformCapabilities,
) -> list[ToolDefinition]:
    async def query_openaev_attack_path(
        action: str = "",
        simulation_id: str = "",
        scenario_id: str = "",
        ref: str = "",
        category: str = "",
        mode: str = "",
        page: int = 0,
        limit: int = 50,
    ) -> str:
        action = (action or "").strip().lower()
        gate = await capabilities.require_feature("ATTACK_PATH", "The attack path view")
        if gate:
            return gate

        if action == "list_simulations":
            params = {"scenarioId": scenario_id} if scenario_id else None
            resp, err = await _api(
                base_url, headers, "get", f"{_ATTACK_PATH}/simulations",
                "list attack-path simulations", params=params,
            )
            if err:
                return err
            rows = resp.json() or []
            if not rows:
                return (
                    "No simulations have attack-path data. The graph is built from "
                    "executed simulation data, so only simulations that have "
                    "actually run appear here."
                )
            return json_answer(rows, 3000)

        if not simulation_id:
            return f"simulation_id is required for action '{action}'."

        if action == "graph":
            params = {"mode": mode} if mode else None
            resp, err = await _api(
                base_url, headers, "get",
                f"{_ATTACK_PATH}/simulations/{simulation_id}/graph",
                "get attack path graph", params=params,
            )
            if err:
                return err
            graph = resp.json() or {}
            nodes = graph.get("attackPathNodes") or []
            edges = graph.get("attackPathEdges") or []
            summary = [
                f"Attack path for simulation {simulation_id}: {len(nodes)} node(s), "
                f"{len(edges) if isinstance(edges, list) else '?'} edge group(s)."
            ]
            summary.append(
                "Note the attack-path DTOs use camelCase field names, unlike the "
                "snake_case entity fields elsewhere in the API."
            )
            summary.append(json_answer(graph, 4000))
            return "\n".join(summary)

        if action in ("endpoint_findings", "endpoint_relations"):
            if not ref:
                return (
                    f"ref is required for '{action}': the endpoint key (asset id, "
                    "or the raw value of a discovered endpoint)."
                )
            suffix = (
                "endpoint/findings" if action == "endpoint_findings" else "endpoint/relations"
            )
            resp, err = await _api(
                base_url, headers, "get",
                f"{_ATTACK_PATH}/simulations/{simulation_id}/{suffix}",
                f"attack path {action}", params={"ref": ref},
            )
            if err:
                return err
            return json_answer(resp.json(), 4000)

        if action == "findings":
            resolved = next(
                (c for c in _FINDING_CATEGORIES if c == category.strip().lower()), None
            )
            if resolved is None:
                return (
                    f"category must be one of: {', '.join(_FINDING_CATEGORIES)} "
                    f"(got '{category}'). An unknown category returns an empty "
                    "page rather than an error, so it is validated here."
                )
            resp, err = await _api(
                base_url, headers, "get",
                f"{_ATTACK_PATH}/simulations/{simulation_id}/findings",
                "attack path findings",
                params={
                    "category": resolved,
                    "page": max(page, 0),
                    "size": min(max(limit, 1), 50),
                },
            )
            if err:
                return err
            return json_answer(resp.json(), 4000)

        if action == "execution":
            if not ref:
                return (
                    "ref is required for 'execution': the execution id, passed as "
                    "a query parameter because injector-sourced ids can end with a "
                    "marker character that a path segment cannot carry."
                )
            resp, err = await _api(
                base_url, headers, "get",
                f"{_ATTACK_PATH}/simulations/{simulation_id}/execution",
                "attack path execution detail", params={"ref": ref},
            )
            if err:
                return err
            return json_answer(resp.json(), 4000)

        return (
            f"Unknown action '{action}'. Valid actions: list_simulations, graph, "
            "endpoint_findings, endpoint_relations, findings, execution."
        )

    async def query_openaev_chaining(
        action: str = "",
        workflow_id: str = "",
        step_id: str = "",
        condition_id: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        gate = await capabilities.require_feature(
            "INJECT_CHAINING", "The inject chaining / workflow engine"
        )
        if gate:
            return gate
        tenant_id = await capabilities.tenant_id()
        if not tenant_id:
            return (
                "The chaining endpoints exist only in tenant-scoped form and the "
                "default tenant id could not be read from GET /api/settings (the "
                "API token needs platform-settings read access)."
            )

        workflow_hint = (
            "A workflow_id is the id of a real chaining workflow - read it from "
            "the scenario/simulation configuration or an attack-path state you "
            "already fetched, do not guess it."
        )
        step_hint = (
            "A step_id comes from the 'steps' action on a real workflow_id - list "
            "the steps first, then use one of the ids it returns."
        )
        condition_hint = (
            "A condition_id comes from the 'conditions' action on a real "
            "workflow_id - list the conditions first, then use one of the ids it "
            "returns."
        )

        if action in ("workflow_config", "workflow_assets"):
            id_err = _placeholder_id_error("workflow_id", workflow_id, workflow_hint)
            if id_err:
                return id_err
            suffix = (
                "configuration" if action == "workflow_config" else "valid-assets"
            )
            resp, err = await _api(
                base_url, headers, "get", f"{_WORKFLOWS}/{workflow_id}/{suffix}",
                f"get workflow {suffix}", tenant_id=tenant_id,
            )
            if err:
                return err
            return json_answer(resp.json(), 4000)

        if action == "steps":
            id_err = _placeholder_id_error("workflow_id", workflow_id, workflow_hint)
            if id_err:
                return id_err
            resp, err = await _api(
                base_url, headers, "get", _STEPS,
                "list workflow steps", params={"workflow_id": workflow_id},
                tenant_id=tenant_id,
            )
            if err:
                return err
            return json_answer(resp.json(), 4000)

        if action == "step":
            id_err = _placeholder_id_error("step_id", step_id, step_hint)
            if id_err:
                return id_err
            resp, err = await _api(
                base_url, headers, "get", f"{_STEPS}/{step_id}",
                "get workflow step", tenant_id=tenant_id,
            )
            if err:
                return err
            return json_answer(resp.json(), 3000)

        if action == "conditions":
            id_err = _placeholder_id_error("workflow_id", workflow_id, workflow_hint)
            if id_err:
                return id_err
            resp, err = await _api(
                base_url, headers, "get", _CONDITIONS,
                "list workflow conditions", params={"workflow_id": workflow_id},
                tenant_id=tenant_id,
            )
            if err:
                return err
            return json_answer(resp.json(), 4000)

        if action == "condition":
            id_err = _placeholder_id_error("condition_id", condition_id, condition_hint)
            if id_err:
                return id_err
            resp, err = await _api(
                base_url, headers, "get", f"{_CONDITIONS}/{condition_id}",
                "get workflow condition", tenant_id=tenant_id,
            )
            if err:
                return err
            return json_answer(resp.json(), 3000)

        return (
            f"Unknown action '{action}'. Valid actions: workflow_config, "
            "workflow_assets, steps, step, conditions, condition."
        )

    executor.register("query_openaev_attack_path", query_openaev_attack_path)
    executor.register("query_openaev_chaining", query_openaev_chaining)

    return [
        ToolDefinition(
            name="query_openaev_attack_path",
            description=(
                "Read the OpenAEV ATTACK PATH of a simulation: the causal graph of "
                "which endpoints were reached, through which executions, and what "
                "was discovered on them. Read-only. PREVIEW FEATURE - requires "
                "ATTACK_PATH in the platform's enabled_dev_features, and the tool "
                "says so explicitly when it is off rather than returning a bare "
                "404. Actions: 'list_simulations' (which simulations have "
                "attack-path data; optional scenario_id), 'graph' (simulation_id), "
                "'endpoint_findings' / 'endpoint_relations' (simulation_id + ref = "
                "the endpoint key), 'findings' (simulation_id + category "
                "credentials|users|files|cves, paginated), 'execution' "
                "(simulation_id + ref = execution id - command, output and "
                "findings, with credentials masked server-side)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_simulations", "graph", "endpoint_findings",
                            "endpoint_relations", "findings", "execution",
                        ],
                    },
                    "simulation_id": {"type": "string"},
                    "scenario_id": {"type": "string", "description": "Restrict list_simulations to one scenario."},
                    "ref": {"type": "string", "description": "Endpoint key (endpoint_* actions) or execution id ('execution')."},
                    "category": {"type": "string", "enum": _FINDING_CATEGORIES},
                    "mode": {"type": "string", "description": "Optional graph mode."},
                    "page": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 50, "description": "Page size, capped at 50 by the API."},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="query_openaev_chaining",
            description=(
                "Read OpenAEV WORKFLOW / CHAINING configuration - how injects are "
                "chained into a conditional flow, the steps of a workflow and the "
                "condition trees that gate them. Read-only. PREVIEW FEATURE - "
                "requires INJECT_CHAINING in enabled_dev_features; the tool "
                "reports that explicitly instead of returning a bare 404. Actions: "
                "'workflow_config' (workflow_id), 'workflow_assets' (workflow_id - "
                "assets in scope), 'steps' (workflow_id), 'step' (step_id), "
                "'conditions' (workflow_id), 'condition' (condition_id). Authoring "
                "steps and conditions is intentionally not exposed - it is a graph "
                "edit where a malformed write silently changes execution order."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "workflow_config", "workflow_assets", "steps", "step",
                            "conditions", "condition",
                        ],
                    },
                    "workflow_id": {"type": "string"},
                    "step_id": {"type": "string"},
                    "condition_id": {"type": "string"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
