"""OpenCTI security coverage tools (CTEM-16, #1089).

* ``manage_opencti_security_coverage`` - list, get (with results and the covered
  vulnerabilities and attack patterns), create, update, delete.

A security coverage asks OpenAEV to test an OpenCTI threat: OpenAEV builds a
scenario from the covered object (a Grouping, a Report, an Intrusion Set, a
Campaign, an Incident or a Case Incident: ``COVERED_ENTITIES_TYPE`` of
``securityCoverage-domain.ts``) and sends its results back as security coverage
results. In the CTEM loop the ``create`` action is the coverage request of the
review step: the gate ``ctem.analyst_review`` of the fleet's catalog maps onto
it (``app.agents.builtin_fleet_docs``), so it runs alone out of the box and
pauses for a person's decision when the gate is pinned to wait.

The writes follow the run's OpenCTI draft context, as every request of the
provider does: the threat package a fleet drafts sits in a draft until a person
validates it, and its coverage belongs in the same draft (a live coverage would
point at an object the live graph does not hold). With no draft active, or with
the integration's Live writes on, the coverage is written live and reaches
OpenAEV at once.

In a draft, ``create`` sends ``auto_enrichment_disable: true`` unless the caller
passes the flag (#2323): OpenCTI publishes the enrichment event of a draft
object too, and a validation module that does not honour the draft marker
would build and run the scenario before the analyst review decides. After the
draft is validated the caller turns it on with ``update``
(``auto_enrichment_disable: false``) on the live coverage; ``get`` shows it.
An explicit ``false`` in a draft is the opt-in and is sent as given.

A coverage that carries a periodicity makes OpenAEV schedule the scenario it
builds two minutes later, then at that periodicity; without one the scenario
is built and nothing is scheduled. When the run's fleet validation gate
(``ctem.validation.non_readonly``) waits for a person, ``create`` and
``update`` refuse a periodicity by naming the gate (#2102): the coverage is
created without it, and the repeat is set on the OpenAEV scenario after the
approval (``manage_openaev_scenario`` ``schedule_recurrence``), never through
the coverage.
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql
from .access import _refuse_unapplied

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"

#: What a security coverage may cover (``COVERED_ENTITIES_TYPE`` in OpenCTI).
COVERED_TYPES: tuple[str, ...] = (
    "Grouping",
    "Report",
    "Intrusion-Set",
    "Campaign",
    "Incident",
    "Case-Incident",
)

_DEFAULT_LIMIT = 20

# The fields each action reads; any other field passed (an argument the tool does
# not declare included) is refused by name and nothing is sent.
_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "covered_id", "limit"}),
    "get": frozenset({"coverage_id"}),
    "create": frozenset(
        {
            "covered_id",
            "name",
            "description",
            "periodicity",
            "duration",
            "type_affinity",
            "platforms_affinity",
            "auto_enrichment_disable",
        }
    ),
    "update": frozenset(
        {
            "coverage_id",
            "name",
            "description",
            "periodicity",
            "duration",
            "type_affinity",
            "platforms_affinity",
            "auto_enrichment_disable",
        }
    ),
    "delete": frozenset({"coverage_id"}),
}

_COVERAGE_FIELDS = (
    "id name description periodicity duration type_affinity platforms_affinity auto_enrichment_disable "
    "coverage_last_result coverage_information { coverage_name coverage_score } "
    "objectCovered { id entity_type representative { main } }"
)

_LIST_QUERY = (
    "query SecurityCoverages($first: Int, $search: String, $filters: FilterGroup) {"
    "  securityCoverages(first: $first, search: $search, filters: $filters,"
    "    orderBy: created_at, orderMode: desc) { edges { node { " + _COVERAGE_FIELDS + " } } } }"
)

_GET_QUERY = (
    "query SecurityCoverage($id: String!) {"
    "  securityCoverage(id: $id) { " + _COVERAGE_FIELDS + " external_uri"
    "    results { id name coverage_last_result coverage_information { coverage_name coverage_score } }"
    "    coveredVulnerabilities(first: 50) { count entities {"
    "      coverage_information { coverage_name coverage_score } to { id name } } }"
    "    coveredAttackPatterns(first: 50) { count entities {"
    "      coverage_information { coverage_name coverage_score } to { id name x_mitre_id } } } } }"
)

_COVERED_TYPE_QUERY = "query CoveredType($id: String!) { stixDomainObject(id: $id) { id entity_type } }"

_ADD_MUTATION = (
    "mutation SecurityCoverageAdd($input: SecurityCoverageAddInput!) {"
    "  securityCoverageAdd(input: $input) { id name objectCovered { id entity_type } } }"
)

_PATCH_MUTATION = (
    "mutation SecurityCoverageFieldPatch($id: ID!, $input: [EditInput]!) {"
    "  securityCoverageFieldPatch(id: $id, input: $input) { id name } }"
)

_DELETE_MUTATION = "mutation SecurityCoverageDelete($id: ID!) { securityCoverageDelete(id: $id) }"


def _scores(info: Any) -> str:
    """``coverage_information`` as ``Prevention 80%, Detection 40%``; ``no result yet`` when empty."""
    parts = [
        f"{i.get('coverage_name')} {i.get('coverage_score')}%"
        for i in info or ()
        if isinstance(i, dict) and i.get("coverage_name")
    ]
    return ", ".join(parts) or "no result yet"


def _covered(node: dict[str, Any]) -> str:
    covered = node.get("objectCovered") or {}
    if not covered:
        return "-"
    main = (covered.get("representative") or {}).get("main") or ""
    return f"{covered.get('entity_type')} {main} (id: {covered.get('id')})".replace("  ", " ")


def _format_coverage(node: dict[str, Any]) -> list[str]:
    affinity = ", ".join(node.get("platforms_affinity") or ()) or "-"
    return [
        f"{node.get('name')} (id: {node.get('id')})",
        f"  covers: {_covered(node)}",
        f"  periodicity: {node.get('periodicity') or '-'}, duration: {node.get('duration') or '-'}, "
        f"type affinity: {node.get('type_affinity') or '-'}, platforms: {affinity}, "
        f"auto enrichment: {'off' if node.get('auto_enrichment_disable') else 'on'}",
        f"  last result: {node.get('coverage_last_result') or 'none'}; scores: {_scores(node.get('coverage_information'))}",
    ]


def _format_covered_entities(label: str, block: Any, *, with_mitre: bool = False) -> list[str]:
    if not isinstance(block, dict):
        return []
    entities = [e for e in block.get("entities") or () if isinstance(e, dict)]
    lines = [f"  {label} ({block.get('count', len(entities))}):"]
    for entity in entities:
        to = entity.get("to") or {}
        mitre = f" {to.get('x_mitre_id')}" if with_mitre and to.get("x_mitre_id") else ""
        scores = _scores(entity.get("coverage_information"))
        lines.append(f"    - {to.get('name') or '-'}{mitre} (id: {to.get('id')}): {scores}")
    return lines


def periodicity_refusal(gate: str, action: str) -> str:
    """The answer to a periodicity given while the validation *gate* waits (#2102). Pure."""
    return (
        f"Error: refused: validation gate {gate} waits for a person; a periodicity on a security coverage "
        "makes OpenAEV start its scenario two minutes later, before the decision. "
        f"{'Create' if action == 'create' else 'Update'} the coverage without periodicity and set the repeat "
        "on the OpenAEV scenario after approval (manage_openaev_scenario schedule_recurrence); nothing was written."
    )


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    waiting_validation_gate: str = "",
) -> list[ToolDefinition]:
    """The security coverage tool; *waiting_validation_gate* names the run's validation gate when it waits."""

    async def _gql(query: str, variables: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def manage_opencti_security_coverage(
        action: str,
        coverage_id: str = "",
        covered_id: str = "",
        name: str = "",
        description: str = "",
        periodicity: str = "",
        duration: str = "",
        type_affinity: str = "",
        platforms_affinity: list[str] | None = None,
        auto_enrichment_disable: bool | None = None,
        search: str = "",
        limit: int = _DEFAULT_LIMIT,
        **unknown: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _APPLIES,
            {
                "coverage_id": coverage_id,
                "covered_id": covered_id,
                "name": name,
                "description": description,
                "periodicity": periodicity,
                "duration": duration,
                "type_affinity": type_affinity,
                "platforms_affinity": platforms_affinity or None,
                "auto_enrichment_disable": auto_enrichment_disable,
                "search": search,
                "limit": None if limit == _DEFAULT_LIMIT else limit,
            },
            dict(unknown),
        )
        if refused:
            return refused
        if waiting_validation_gate and periodicity and action in ("create", "update"):
            return periodicity_refusal(waiting_validation_gate, action)

        if action == "list":
            filters = None
            if covered_id:
                filters = {
                    "mode": "and",
                    "filterGroups": [],
                    "filters": [{"key": ["objectCovered"], "values": [covered_id], "operator": "eq", "mode": "or"}],
                }
            data, err = await _gql(
                _LIST_QUERY, {"first": min(max(limit, 1), 100), "search": search or None, "filters": filters}
            )
            if err:
                return err
            nodes = _edges(data, "securityCoverages")
            if not nodes:
                return "No security coverage found."
            lines = [f"Security coverages ({len(nodes)}):"]
            for node in nodes:
                lines.extend(f"- {line}" if i == 0 else line for i, line in enumerate(_format_coverage(node)))
            return "\n".join(lines)

        if action == "get":
            if not coverage_id:
                return "Error: coverage_id is required for action 'get'; nothing was read."
            data, err = await _gql(_GET_QUERY, {"id": coverage_id})
            if err:
                return err
            coverage = data.get("securityCoverage")
            if not isinstance(coverage, dict) or not coverage:
                return f"Security coverage {coverage_id} not found."
            lines = _format_coverage(coverage)
            results = [r for r in coverage.get("results") or () if isinstance(r, dict)]
            lines.append(f"  results ({len(results)}):")
            for result in results:
                lines.append(
                    f"    - {result.get('coverage_last_result') or '-'}: {_scores(result.get('coverage_information'))} "
                    f"(id: {result.get('id')})"
                )
            lines.extend(_format_covered_entities("vulnerabilities", coverage.get("coveredVulnerabilities")))
            lines.extend(
                _format_covered_entities("attack patterns", coverage.get("coveredAttackPatterns"), with_mitre=True)
            )
            if coverage.get("external_uri"):
                lines.append(f"  in the validation module: {coverage['external_uri']}")
            return "\n".join(lines)

        if action == "create":
            if not covered_id or not name:
                return (
                    "Error: covered_id (the Grouping, Report, Intrusion Set, Campaign, Incident or Case Incident "
                    "to test) and name are required for action 'create'; nothing was written."
                )
            data, err = await _gql(_COVERED_TYPE_QUERY, {"id": covered_id})
            if err:
                return err
            covered = data.get("stixDomainObject") or {}
            if not covered:
                return f"Error: object {covered_id} not found in OpenCTI; nothing was written."
            if covered.get("entity_type") not in COVERED_TYPES:
                return (
                    f"Error: a security coverage covers a {', '.join(COVERED_TYPES)}; {covered_id} is a "
                    f"{covered.get('entity_type')}. Put it in a Grouping first; nothing was written."
                )
            draft = headers.get("opencti-draft-id") or ""
            # In a draft the flag defaults to true (#2323): the coverage reaches the
            # validation module once the review validated the draft, never before.
            disabled = bool(draft) if auto_enrichment_disable is None else bool(auto_enrichment_disable)
            inp: dict[str, Any] = {
                "name": name,
                "objectCovered": covered_id,
                "auto_enrichment_disable": disabled,
            }
            for key, value in (
                ("description", description),
                ("periodicity", periodicity),
                ("duration", duration),
                ("type_affinity", type_affinity),
            ):
                if value:
                    inp[key] = value
            if platforms_affinity:
                inp["platforms_affinity"] = list(platforms_affinity)
            data, err = await _gql(_ADD_MUTATION, {"input": inp})
            if err:
                return err
            created = data.get("securityCoverageAdd") or {}
            return (
                f"Created security coverage '{created.get('name')}' (id: {created.get('id')}) on "
                f"{covered.get('entity_type')} {covered_id}"
                + (
                    f" in OpenCTI draft {draft} with auto enrichment off: nothing is sent to the validation module. "
                    "Once the draft is validated, turn it on on the live coverage (action 'update', "
                    "auto_enrichment_disable false)."
                    if draft and disabled
                    else f" in OpenCTI draft {draft} with auto enrichment on, as asked: the validation module may "
                    "receive it before the draft is validated."
                    if draft
                    else " with auto enrichment off: the validation module receives it once it is turned on."
                    if disabled
                    else ". The validation module builds its scenario from it; read its results with action 'get'."
                )
            )

        if action == "update":
            if not coverage_id:
                return "Error: coverage_id is required for action 'update'; nothing was written."
            edits: list[dict[str, Any]] = [
                {"key": key, "value": [value]}
                for key, value in (
                    ("name", name),
                    ("description", description),
                    ("periodicity", periodicity),
                    ("duration", duration),
                    ("type_affinity", type_affinity),
                )
                if value
            ]
            if platforms_affinity:
                edits.append({"key": "platforms_affinity", "value": list(platforms_affinity)})
            if auto_enrichment_disable is not None:
                # Tri-state: unset leaves the flag as stored, true and false are written.
                edits.append({"key": "auto_enrichment_disable", "value": [bool(auto_enrichment_disable)]})
            if not edits:
                return (
                    "Error: give name, description, periodicity, duration, type_affinity, platforms_affinity "
                    "or auto_enrichment_disable to update; nothing was written."
                )
            _, err = await _gql(_PATCH_MUTATION, {"id": coverage_id, "input": edits})
            if err:
                return err
            return f"Updated security coverage {coverage_id}: {', '.join(e['key'] for e in edits)}."

        if action == "delete":
            if not coverage_id:
                return "Error: coverage_id is required for action 'delete'; nothing was written."
            _, err = await _gql(_DELETE_MUTATION, {"id": coverage_id})
            if err:
                return err
            return f"Deleted security coverage {coverage_id} and its results."

        return f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."

    executor.register("manage_opencti_security_coverage", manage_opencti_security_coverage)

    return [
        ToolDefinition(
            name="manage_opencti_security_coverage",
            description=(
                "Manage OpenCTI security coverages: the request that asks the validation module (OpenAEV) to "
                "test a threat. Actions: 'list' (search, covered_id), 'get' (coverage_id: scores, results, "
                "covered vulnerabilities and attack patterns), 'create' (covered_id of a Grouping, Report, "
                "Intrusion Set, Campaign, Incident or Case Incident + name; periodicity and duration as ISO-8601 "
                "durations such as P1D, type_affinity, platforms_affinity), 'update' (coverage_id + fields), "
                "'delete' (coverage_id). Writes follow the active OpenCTI draft, like the covered object they "
                "point at. In a CTEM loop fleet, 'create' is the coverage request of the review step, gate "
                "ctem.analyst_review."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "coverage_id": {"type": "string", "description": "Security coverage id (get, update, delete)."},
                    "covered_id": {
                        "type": "string",
                        "description": "Id of the object to test (create), or to list its coverages (list).",
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "periodicity": {
                        "type": "string",
                        "description": (
                            "How often to re-test, ISO-8601 (e.g. P1D). OpenAEV starts the scenario two minutes "
                            "after a coverage with a periodicity; refused while the fleet's validation gate waits."
                        ),
                    },
                    "duration": {"type": "string", "description": "How long the coverage runs, ISO-8601 (e.g. P30D)."},
                    "type_affinity": {"type": "string", "description": "The kind of test, e.g. ENDPOINT or CLOUD."},
                    "platforms_affinity": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Platforms to test, e.g. ['linux', 'windows'].",
                    },
                    "auto_enrichment_disable": {
                        "type": "boolean",
                        "description": "true: never sent to the validation module by itself. Omitted: kept; on create true in a draft.",
                    },
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": _DEFAULT_LIMIT},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        )
    ]
