"""OpenCTI PIR (Priority Intelligence Requirements) tools.

* ``manage_opencti_pir``              - CRUD + flag/unflag elements
* ``query_opencti_pir_entities``      - flagged StixDomainObjects ranked by
  PIR score (the canonical ``stixDomainObjects(pirId: ...)`` query, mirrors the
  UI ``PirKnowledgeEntitiesSourcesFlaggedListQuery``)
* ``query_opencti_pir_relationships`` - rich, read-only relationship query

IMPORTANT filter shapes (``src/modules/pir/pir.graphql``):
* ``pir_filters`` and each ``pir_criteria[].filters`` are native ``FilterGroup``
  objects on write (pirAdd). The tool schema declares these arguments as JSON
  strings (consistent with every other filter argument in the provider and
  accepted by all LLM providers); ``_coerce_json`` then parses the string and
  forwards a native object to the API. An already-parsed object is also
  tolerated defensively. On read they come back as JSON strings.
* The agent can validate a FilterGroup with ``validate_nlq_filter`` first.

CRITICAL write contract: OpenCTI stores ``pir_filters`` and each
criterion's ``filters`` verbatim (``JSON.stringify`` of the GraphQL input) and
both the dashboard and the PIR manager re-parse them later, so a loosely
shaped FilterGroup is accepted by the API but produces a PIR that neither
renders nor flags anything. The PIR manager only matches *relationship* stream
events, and the dashboard criteria display resolves ``entity_type`` +
``toId`` pairs, which is exactly what the OpenCTI creation form generates
(``pir-form-utils.ts``). Therefore every criterion MUST contain an
``entity_type`` filter (the relationship type, e.g. ``targets``) and a
``toId`` filter whose values are OpenCTI internal IDs (UUIDs). All filters are
normalized to the canonical shape (array keys, string values, explicit
operator/mode) before being sent.

Not draft-gated (PIR is a platform analysis feature, not STIX knowledge).
"""

from __future__ import annotations

import json
import logging

from app.core.utils import is_valid_uuid
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql
from .access import _refuse_unapplied

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"
_PIR_TYPES = ("THREAT_LANDSCAPE", "THREAT_ORIGIN", "THREAT_CUSTOM")

# The fields each action of manage_opencti_pir reads; any other field passed (an
# argument the tool does not declare included) is refused by name and nothing is sent.
# A field left at its default (pir_type THREAT_LANDSCAPE, pir_rescan_days 0, limit 20)
# counts as not passed.
_PIR_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"pir_id"}),
    "create": frozenset({"name", "description", "pir_type", "pir_rescan_days", "pir_filters", "pir_criteria"}),
    "update": frozenset({"pir_id", "name", "description", "pir_rescan_days"}),
    "delete": frozenset({"pir_id"}),
    "flag": frozenset({"pir_id", "relationship_id", "source_id", "matching_criteria", "relationship_author_id"}),
    "unflag": frozenset({"pir_id", "relationship_id", "source_id"}),
}

_FILTER_GROUP_HINT = (
    "a FilterGroup object with 'mode' ('and'/'or'), 'filters' (array of "
    "{key, values, operator?, mode?}) and 'filterGroups' (array)"
)

_CRITERION_HINT = (
    "Each pir_criteria entry needs an integer 'weight' >= 1 and a 'filters' "
    "FilterGroup whose 'filters' array contains BOTH an 'entity_type' filter "
    "(the relationship type to match, e.g. 'targets') AND a 'toId' filter "
    "(OpenCTI internal IDs of the related entity). Example: {\"weight\": 1, "
    "\"filters\": {\"mode\": \"and\", \"filterGroups\": [], \"filters\": ["
    "{\"key\": [\"entity_type\"], \"values\": [\"targets\"], \"operator\": "
    "\"eq\", \"mode\": \"or\"}, {\"key\": [\"toId\"], \"values\": "
    "[\"ENTITY_UUID\"], \"operator\": \"eq\", \"mode\": \"or\"}]}}"
)


def _normalize_filter_group(obj: object) -> dict | None:
    """Coerce a loosely shaped filter group into OpenCTI's canonical FilterGroup.

    LLMs frequently emit scalar keys, numeric values, or omit ``filterGroups``.
    OpenCTI stores PIR filters verbatim and re-parses them in the dashboard and
    the PIR manager, so anything non-canonical is silently broken downstream.
    Returns the normalized FilterGroup, or ``None`` when the value cannot be
    interpreted as one.
    """
    if not isinstance(obj, dict):
        return None
    mode = obj.get("mode", "and")
    if not isinstance(mode, str) or mode.lower() not in ("and", "or"):
        return None
    raw_filters = obj.get("filters", [])
    raw_groups = obj.get("filterGroups", [])
    if not isinstance(raw_filters, list) or not isinstance(raw_groups, list):
        return None
    filters: list[dict] = []
    for item in raw_filters:
        if not isinstance(item, dict) or "key" not in item or "values" not in item:
            return None
        key = item["key"]
        if isinstance(key, str):
            key = [key]
        if (
            not isinstance(key, list)
            or not key
            or not all(isinstance(k, str) for k in key)
        ):
            return None
        values = item["values"]
        if not isinstance(values, list):
            values = [values]
        normalized_values: list[str] = []
        for value in values:
            if isinstance(value, bool):
                # The OpenCTI UI serializes every filter value as a string.
                normalized_values.append("true" if value else "false")
            elif isinstance(value, (int, float)):
                normalized_values.append(str(value))
            elif isinstance(value, str):
                normalized_values.append(value)
            else:
                return None
        # operator/mode are stored verbatim too: coerce case, reject
        # non-strings instead of forwarding a non-canonical shape.
        operator = item.get("operator")
        if operator is None or operator == "":
            operator = "eq"
        elif isinstance(operator, str):
            operator = operator.strip().lower() or "eq"
        else:
            return None
        item_mode = item.get("mode")
        if item_mode is None or item_mode == "":
            item_mode = "or"
        elif (
            not isinstance(item_mode, str)
            or item_mode.strip().lower() not in ("and", "or")
        ):
            return None
        else:
            item_mode = item_mode.strip().lower()
        filters.append(
            {
                "key": key,
                "values": normalized_values,
                "operator": operator,
                "mode": item_mode,
            }
        )
    groups: list[dict] = []
    for group in raw_groups:
        normalized = _normalize_filter_group(group)
        if normalized is None:
            return None
        groups.append(normalized)
    return {"mode": mode.lower(), "filters": filters, "filterGroups": groups}


def _criterion_contract_error(filters_obj: dict) -> str | None:
    """Check a normalized criterion FilterGroup against the PIR matching contract.

    The PIR manager only evaluates criteria against *relationship* stream
    events, and the OpenCTI dashboard renders criteria by resolving the
    ``entity_type`` + ``toId`` filter pair — a criterion missing either filter
    is stored without error but never matches and renders blank/broken.
    """
    flat = filters_obj.get("filters", [])
    rel_filter = next((f for f in flat if "entity_type" in f["key"]), None)
    to_id_filter = next((f for f in flat if "toId" in f["key"]), None)
    if rel_filter is None or to_id_filter is None:
        return (
            "Invalid pir_criteria: a criterion is missing its 'entity_type' "
            f"and/or 'toId' filter. {_CRITERION_HINT}"
        )
    if not rel_filter["values"]:
        return "Invalid pir_criteria: the 'entity_type' filter has no values."
    if not to_id_filter["values"]:
        return (
            "Invalid pir_criteria: the 'toId' filter has no values. Resolve "
            "entities first (e.g. with search_opencti) and use their 'id' field."
        )
    invalid_ids = [v for v in to_id_filter["values"] if not is_valid_uuid(v)]
    if invalid_ids:
        return (
            "Invalid pir_criteria: 'toId' values must be OpenCTI internal IDs "
            f"(UUIDs), got {invalid_ids}. Resolve entities first (e.g. with "
            "search_opencti) and use their 'id' field."
        )
    return None


def _normalize_criteria(criteria_obj: object) -> tuple[list[dict] | None, str | None]:
    """Normalize and validate a pir_criteria array. Returns (criteria, error)."""
    if not isinstance(criteria_obj, list) or not criteria_obj:
        return None, (
            "pir_criteria must be a non-empty JSON array of {weight, filters}. "
            + _CRITERION_HINT
        )
    normalized: list[dict] = []
    for entry in criteria_obj:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("weight"), int)
            or isinstance(entry.get("weight"), bool)
            or entry["weight"] < 1
        ):
            return None, _CRITERION_HINT
        filters_obj = _normalize_filter_group(entry.get("filters"))
        if filters_obj is None:
            return None, (
                f"Invalid pir_criteria: 'filters' must be {_FILTER_GROUP_HINT}. "
                + _CRITERION_HINT
            )
        contract_error = _criterion_contract_error(filters_obj)
        if contract_error:
            return None, contract_error
        normalized.append({"weight": entry["weight"], "filters": filters_obj})
    return normalized, None


def _coerce_json(value: object) -> object:
    """Accept either a JSON string or an already-parsed object/array.

    The tool schema types PIR filter inputs as JSON strings, but the module
    docs describe them as native ``FilterGroup`` objects, so a caller may pass
    either form. Returning the value untouched when it is already parsed avoids
    a ``TypeError`` from ``json.loads`` on non-string input.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# Flagged-entities (StixDomainObjects) query helpers
# ---------------------------------------------------------------------------
# Listing the entities flagged into a PIR ranked by ``pir_score`` is ONLY
# possible through the ``stixDomainObjects`` root query with the ``pirId``
# argument set (OpenCTI threads it into the Elastic sort builder — see
# ``utils/sorting.ts``; without it the API throws "You should provide a PIR ID
# to order by pir_score."). The ``pirRelationships`` query cannot do it: its
# resolver strips ``pirId`` (``R.dissoc('pirId', opts)``) before paginating, so
# the sort builder never receives it. This mirrors the OpenCTI UI query
# ``PirKnowledgeEntitiesSourcesFlaggedListQuery`` exactly.

_PIR_SCORE_FILTER = "pir_score"
_LAST_PIR_SCORE_DATE_FILTER = "last_pir_score_date"

_PIR_ENTITY_ORDERINGS = (
    "pir_score",
    "last_pir_score_date",
    "created_at",
    "created",
    "name",
    "entity_type",
)


def _filter_key(key: object) -> str:
    """Return a filter key as a scalar string (filters allow list or scalar keys)."""
    if isinstance(key, list):
        return str(key[0]) if key else ""
    return str(key) if key is not None else ""


def _format_filters_in_pir_context(filter_group: dict, pir_id: str) -> dict:
    """Rewrite ``pir_score`` / ``last_pir_score_date`` filters into PIR context.

    Mirrors the OpenCTI UI ``formatFiltersInPirContext`` helper: a score/date
    filter is wrapped so it resolves against the right PIR's nested
    ``pir_information``; every other filter passes through unchanged.
    """
    formatted: list[dict] = []
    for f in filter_group.get("filters", []):
        if not isinstance(f, dict):
            continue
        key = _filter_key(f.get("key"))
        if key in (_PIR_SCORE_FILTER, _LAST_PIR_SCORE_DATE_FILTER):
            sub_key = "score" if key == _PIR_SCORE_FILTER else "date"
            formatted.append(
                {
                    "key": key,
                    "values": [
                        {**f, "key": sub_key},
                        {"key": "pir_ids", "values": [pir_id]},
                    ],
                }
            )
        else:
            formatted.append(f)
    raw_groups = filter_group.get("filterGroups", [])
    groups = [
        _format_filters_in_pir_context(g, pir_id)
        for g in raw_groups
        if isinstance(g, dict)
    ]
    return {
        "mode": filter_group.get("mode", "and"),
        "filters": formatted,
        "filterGroups": groups,
    }


def _build_pir_context_filters(pir_id: str, user_filters: object) -> dict:
    """Build the ``regardingOf`` + ``in-pir`` scope filter for a PIR's entities.

    The base filter restricts ``stixDomainObjects`` to entities that have an
    ``in-pir`` relationship pointing at this PIR. Any caller-supplied
    FilterGroup is PIR-context-formatted and nested as an additional group.
    """
    nested: list[dict] = []
    if isinstance(user_filters, dict) and (
        user_filters.get("filters") or user_filters.get("filterGroups")
    ):
        nested.append(_format_filters_in_pir_context(user_filters, pir_id))
    return {
        "mode": "and",
        "filters": [
            {
                "key": "regardingOf",
                "operator": "eq",
                "mode": "and",
                "values": [
                    {"key": "id", "values": [pir_id], "operator": "eq", "mode": "or"},
                    {
                        "key": "relationship_type",
                        "values": ["in-pir"],
                        "operator": "eq",
                        "mode": "or",
                    },
                ],
            }
        ],
        "filterGroups": nested,
    }


def _summarize_criterion_filters(filters_json: object) -> str:
    """Render a PIR criterion's FilterGroup (stored as a JSON string) compactly.

    Each ``pir_explanation[].criterion.filters`` is the JSON-stringified
    FilterGroup that matched the entity (e.g. ``targets`` toId ``<uuid>``). We
    surface a short ``key=values`` summary so the agent can explain *why* an
    entity is flagged without an extra round-trip.
    """
    try:
        fg = filters_json if isinstance(filters_json, dict) else json.loads(filters_json)
    except (ValueError, TypeError):
        return "criterion"
    if not isinstance(fg, dict):
        return "criterion"
    parts: list[str] = []
    for f in fg.get("filters", []):
        if not isinstance(f, dict):
            continue
        key = _filter_key(f.get("key"))
        values = f.get("values") or []
        val_str = ", ".join(str(v) for v in values)
        if key:
            parts.append(f"{key}={val_str}" if val_str else key)
    return " & ".join(parts) if parts else "criterion"


def _format_pir_entity(node: dict, base_url: str, rank: int) -> str:
    """Format one flagged StixDomainObject (with its PIR information) for the agent."""
    info = node.get("pirInformation") or {}
    score = info.get("pir_score")
    score_str = str(score) if score is not None else "N/A"
    representative = node.get("representative") or {}
    name = representative.get("main") or node.get("id") or "Unknown"
    entity_type = node.get("entity_type", "Entity")
    lines = [f"{rank}. [{entity_type}] {name} - PIR score {score_str}"]

    last_date = info.get("last_pir_score_date")
    if last_date:
        lines.append(f"   Last score change: {last_date}")

    node_id = node.get("id")
    if node_id and base_url:
        lines.append(
            f"   ID: {node_id} | Link: [Open in OpenCTI]({base_url}/dashboard/id/{node_id})"
        )
    elif node_id:
        lines.append(f"   ID: {node_id}")

    labels = [
        lbl.get("value") for lbl in (node.get("objectLabel") or []) if lbl.get("value")
    ]
    if labels:
        lines.append(f"   Labels: {', '.join(labels)}")

    markings = [
        m.get("definition")
        for m in (node.get("objectMarking") or [])
        if m.get("definition")
    ]
    if markings:
        lines.append(f"   Markings: {', '.join(markings)}")

    author = (node.get("createdBy") or {}).get("name")
    if author:
        lines.append(f"   Author: {author}")

    seen: set[str] = set()
    criteria: list[str] = []
    for explanation in info.get("pir_explanation") or []:
        raw = (explanation.get("criterion") or {}).get("filters")
        if isinstance(raw, str) and raw and raw not in seen:
            seen.add(raw)
            criteria.append(_summarize_criterion_filters(raw))
    if criteria:
        lines.append(f"   Matched criteria: {'; '.join(criteria)}")

    return "\n".join(lines)


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def manage_opencti_pir(
        action: str,
        pir_id: str = "",
        name: str = "",
        description: str = "",
        pir_type: str = "THREAT_LANDSCAPE",
        pir_rescan_days: int = 0,
        pir_filters: str = "",
        pir_criteria: str = "",
        relationship_id: str = "",
        source_id: str = "",
        matching_criteria: str = "",
        relationship_author_id: str = "",
        search: str = "",
        limit: int = 20,
        **unknown: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _PIR_APPLIES,
            {
                "pir_id": pir_id,
                "name": name,
                "description": description,
                "pir_type": None if pir_type == "THREAT_LANDSCAPE" else pir_type,
                "pir_rescan_days": pir_rescan_days or None,
                "pir_filters": pir_filters,
                "pir_criteria": pir_criteria,
                "relationship_id": relationship_id,
                "source_id": source_id,
                "matching_criteria": matching_criteria,
                "relationship_author_id": relationship_author_id,
                "search": search,
                "limit": None if limit == 20 else limit,
            },
            dict(unknown),
        )
        if refused:
            return refused

        if action == "list":
            query = (
                "query Pirs($first: Int, $search: String) {"
                "  pirs(first: $first, search: $search, orderBy: name,"
                "    orderMode: asc) { edges { node { id name pir_type description"
                "    pir_rescan_days } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "pirs")
            if not nodes:
                return "No PIRs found."
            lines = [f"PIRs ({len(nodes)}):"]
            for n in nodes:
                lines.append(
                    f"- {n.get('name')} [{n.get('pir_type')}] "
                    f"rescan={n.get('pir_rescan_days')}d (id: {n.get('id')})"
                )
                if n.get("description"):
                    lines.append(f"    {n['description']}")
            return "\n".join(lines)

        if action == "get":
            if not pir_id:
                return "pir_id is required for action 'get'."
            query = (
                "query Pir($id: ID!) {"
                "  pir(id: $id) { id name pir_type description pir_rescan_days"
                "    pir_filters pir_criteria { weight filters } queue_messages } }"
            )
            data, err = await _gql(query, {"id": pir_id})
            if err:
                return err
            p = data.get("pir")
            if not p:
                return f"PIR {pir_id} not found."
            crits = p.get("pir_criteria") or []
            lines = [
                f"{p.get('name')} [{p.get('pir_type')}] (id: {p.get('id')})",
                f"  description: {p.get('description') or '-'}",
                f"  rescan period: {p.get('pir_rescan_days')} days",
                f"  scope filters: {p.get('pir_filters')}",
                f"  criteria ({len(crits)}):",
            ]
            for c in crits:
                lines.append(f"    - weight {c.get('weight')}: {c.get('filters')}")
            return "\n".join(lines)

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            if pir_type not in _PIR_TYPES:
                return f"pir_type must be one of {', '.join(_PIR_TYPES)}."
            if not pir_filters or not pir_criteria:
                return (
                    "pir_filters (a FilterGroup JSON object) and pir_criteria (a JSON "
                    "array of {weight, filters}) are required for 'create'. Tip: "
                    "validate filter JSON with validate_nlq_filter first."
                )
            try:
                raw_filters = _coerce_json(pir_filters)
            except (ValueError, TypeError) as exc:
                return f"pir_filters must be valid JSON: {exc}"
            filters_obj = _normalize_filter_group(raw_filters)
            if filters_obj is None:
                return f"pir_filters must be {_FILTER_GROUP_HINT}."
            try:
                raw_criteria = _coerce_json(pir_criteria)
            except (ValueError, TypeError) as exc:
                return f"pir_criteria must be valid JSON: {exc}"
            criteria_obj, criteria_error = _normalize_criteria(raw_criteria)
            if criteria_error:
                return criteria_error
            query = (
                "mutation PirAdd($input: PirAddInput!) {"
                "  pirAdd(input: $input) { id name } }"
            )
            inp = {
                "name": name,
                "pir_type": pir_type,
                "description": description or None,
                "pir_rescan_days": pir_rescan_days or 30,
                "pir_criteria": criteria_obj,
                "pir_filters": filters_obj,
            }
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            p = data.get("pirAdd") or {}
            return f"Created PIR '{p.get('name')}' (id: {p.get('id')})."

        if action == "update":
            if not pir_id:
                return "pir_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if pir_rescan_days:
                edits.append({"key": "pir_rescan_days", "value": [pir_rescan_days]})
            if not edits:
                return "Provide name, description, or pir_rescan_days to update."
            query = (
                "mutation PirPatch($id: ID!, $input: [EditInput!]!) {"
                "  pirFieldPatch(id: $id, input: $input) { id name } }"
            )
            _, err = await _gql(query, {"id": pir_id, "input": edits})
            if err:
                return err
            return f"Updated PIR {pir_id}."

        if action == "delete":
            if not pir_id:
                return "pir_id is required for action 'delete'."
            query = "mutation PirDelete($id: ID!) { pirDelete(id: $id) }"
            _, err = await _gql(query, {"id": pir_id})
            if err:
                return err
            return f"Deleted PIR {pir_id}."

        if action == "flag":
            if not (pir_id and relationship_id and source_id and matching_criteria):
                return (
                    "pir_id, relationship_id, source_id and matching_criteria (JSON "
                    "array of {weight, filters}) are required for 'flag'."
                )
            try:
                raw_crit = _coerce_json(matching_criteria)
            except (ValueError, TypeError) as exc:
                return f"matching_criteria must be valid JSON: {exc}"
            # Mirror the create-path contract: matching criteria are stored on
            # the flag relationship and re-parsed by OpenCTI exactly like
            # pir_criteria, so they must follow the same canonical shape.
            crit, crit_error = _normalize_criteria(raw_crit)
            if crit_error:
                return crit_error.replace("pir_criteria", "matching_criteria")
            query = (
                "mutation PirFlag($id: ID!, $input: PirFlagElementInput!) {"
                "  pirFlagElement(id: $id, input: $input) }"
            )
            inp = {
                "relationshipId": relationship_id,
                "sourceId": source_id,
                "matchingCriteria": crit,
                "relationshipAuthorId": relationship_author_id or None,
            }
            _, err = await _gql(query, {"id": pir_id, "input": inp})
            if err:
                return err
            return f"Flagged element {source_id} into PIR {pir_id}."

        if action == "unflag":
            if not (pir_id and relationship_id and source_id):
                return "pir_id, relationship_id and source_id are required for 'unflag'."
            query = (
                "mutation PirUnflag($id: ID!, $input: PirUnflagElementInput!) {"
                "  pirUnflagElement(id: $id, input: $input) }"
            )
            _, err = await _gql(
                query,
                {
                    "id": pir_id,
                    "input": {"relationshipId": relationship_id, "sourceId": source_id},
                },
            )
            if err:
                return err
            return f"Unflagged element {source_id} from PIR {pir_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, flag, unflag."
        )

    async def query_opencti_pir_entities(
        pir_id: str = "",
        entity_types: list[str] | None = None,
        search: str = "",
        filters: str = "",
        order_by: str = "pir_score",
        order_mode: str = "desc",
        limit: int = 25,
    ) -> str:
        """List the StixDomainObjects flagged into a PIR, ranked by PIR score.

        Mirrors the OpenCTI UI ``PirKnowledgeEntitiesSourcesFlaggedListQuery``:
        ``stixDomainObjects(pirId: ...)`` with the ``regardingOf`` / ``in-pir``
        scope filter and per-node ``pirInformation`` (score, last score date,
        matching criteria). This is the ONLY path that can order by
        ``pir_score`` — the ``pirId`` argument is required by OpenCTI's sort
        builder.
        """
        if not pir_id:
            return "pir_id is required."
        user_filters: object = None
        if filters:
            try:
                user_filters = _coerce_json(filters)
            except (ValueError, TypeError) as exc:
                return f"filters must be a valid JSON string (FilterGroup): {exc}"
        context_filters = _build_pir_context_filters(pir_id, user_filters)
        query = (
            "query PirEntities($pirId: ID!, $search: String, $first: Int,"
            " $orderBy: StixDomainObjectsOrdering, $orderMode: OrderingMode,"
            " $types: [String], $filters: FilterGroup) {"
            "  stixDomainObjects(search: $search, first: $first, orderBy: $orderBy,"
            "    pirId: $pirId, orderMode: $orderMode, types: $types,"
            "    filters: $filters) {"
            "    edges { node {"
            "      id entity_type created_at"
            "      representative { main }"
            "      objectLabel { id value color }"
            "      objectMarking { id definition x_opencti_order x_opencti_color }"
            "      creators { id name }"
            "      createdBy { ... on Identity { id name } }"
            "      pirInformation(pirId: $pirId) {"
            "        pir_score last_pir_score_date"
            "        pir_explanation { criterion { filters } }"
            "      }"
            "    } }"
            "    pageInfo { globalCount }"
            "  }"
            "}"
        )
        order = order_by if order_by in _PIR_ENTITY_ORDERINGS else "pir_score"
        data, err = await _gql(
            query,
            {
                "pirId": pir_id,
                "search": search or None,
                "first": min(max(limit, 1), 200),
                "orderBy": order,
                "orderMode": "asc" if order_mode == "asc" else "desc",
                "types": entity_types or None,
                "filters": context_filters,
            },
        )
        if err:
            return err
        nodes = _edges(data, "stixDomainObjects")
        if not nodes:
            return f"No entities are flagged into PIR {pir_id}."
        total = (
            data.get("stixDomainObjects", {})
            .get("pageInfo", {})
            .get("globalCount", len(nodes))
        )
        header = (
            f"PIR {pir_id} flagged entities ({len(nodes)} shown of {total}, "
            f"ordered by {order} {'asc' if order_mode == 'asc' else 'desc'}):"
        )
        body = [_format_pir_entity(n, base_url, i) for i, n in enumerate(nodes, 1)]
        return "\n".join([header, "", *body])

    async def query_opencti_pir_relationships(
        pir_id: str = "",
        from_types: list[str] | None = None,
        from_id: list[str] | None = None,
        confidences: list[int] | None = None,
        search: str = "",
        filters: str = "",
        start_date: str = "",
        end_date: str = "",
        order_by: str = "created_at",
        order_mode: str = "desc",
        limit: int = 25,
        after: str = "",
    ) -> str:
        if not pir_id:
            return "pir_id is required."
        filters_obj = None
        if filters:
            try:
                filters_obj = _coerce_json(filters)
            except (ValueError, TypeError) as exc:
                return f"filters must be a valid JSON string (FilterGroup): {exc}"
        query = (
            "query PirRels($pirId: ID!, $first: Int, $after: ID, $orderBy: PirRelationshipOrdering,"
            " $orderMode: OrderingMode, $fromTypes: [String], $fromId: [String],"
            " $confidences: [Int], $search: String, $filters: FilterGroup,"
            " $startDate: DateTime, $endDate: DateTime) {"
            "  pirRelationships(pirId: $pirId, first: $first, after: $after, orderBy: $orderBy,"
            "    orderMode: $orderMode, fromTypes: $fromTypes, fromId: $fromId,"
            "    confidences: $confidences, search: $search, filters: $filters,"
            "    startDate: $startDate, endDate: $endDate) {"
            "    edges { node { id pir_score fromId fromType toId created_at"
            "      from { ... on StixDomainObject { representative { main } } } } }"
            "    pageInfo { endCursor hasNextPage globalCount } } }"
        )
        # ``pirRelationships`` cannot sort by ``pir_score`` (its resolver strips
        # ``pirId`` before the sort builder runs, raising "You should provide a
        # PIR ID to order by pir_score."). Only date orderings are valid here;
        # use ``query_opencti_pir_entities`` to rank by PIR score.
        order = order_by if order_by in ("created_at", "updated_at") else "created_at"
        data, err = await _gql(
            query,
            {
                "pirId": pir_id,
                "first": min(max(limit, 1), 200),
                "after": after or None,
                "orderBy": order,
                "orderMode": "asc" if order_mode == "asc" else "desc",
                "fromTypes": from_types or None,
                "fromId": from_id or None,
                "confidences": confidences or None,
                "search": search or None,
                "filters": filters_obj,
                "startDate": start_date or None,
                "endDate": end_date or None,
            },
        )
        if err:
            return err
        nodes = _edges(data, "pirRelationships")
        if not nodes:
            return f"No flagged relationships found for PIR {pir_id}."
        page = (data.get("pirRelationships") or {}).get("pageInfo") or {}
        total = page.get("globalCount")
        total = total if isinstance(total, int) and total >= len(nodes) else None
        shown = f"{len(nodes)} shown of {total}" if total is not None else f"{len(nodes)} shown"
        lines = [f"PIR {pir_id} flagged relationships ({shown}):"]
        for n in nodes:
            source = (n.get("from") or {}).get("representative") or {}
            name = source.get("main") or n.get("fromId")
            lines.append(
                f"- [{n.get('fromType')}] {name} "
                f"score={n.get('pir_score')} since {n.get('created_at')}"
            )
        if page.get("hasNextPage") and page.get("endCursor"):
            lines.append(f"\nNext page: call again with after='{page['endCursor']}'.")
        lines.append(
            "\nTip: to rank the flagged ENTITIES by PIR score (with their "
            "matching criteria), use query_opencti_pir_entities."
        )
        return "\n".join(lines)

    executor.register("manage_opencti_pir", manage_opencti_pir)
    executor.register("query_opencti_pir_entities", query_opencti_pir_entities)
    executor.register(
        "query_opencti_pir_relationships", query_opencti_pir_relationships
    )

    return [
        ToolDefinition(
            name="manage_opencti_pir",
            description=(
                "Manage OpenCTI Priority Intelligence Requirements (requires PIRAPI / "
                "PIRAPI_PIRUPDATE; flag/unflag require BYPASS). Actions: 'list' "
                "(search), 'get' (pir_id - shows scope filters + weighted criteria), "
                "'create' (name + pir_type [THREAT_LANDSCAPE/THREAT_ORIGIN/"
                "THREAT_CUSTOM] + pir_rescan_days + pir_filters + pir_criteria), "
                "'update' (pir_id + only the fields you want to change: name/"
                "description/pir_rescan_days), 'delete' (pir_id), 'flag'/'unflag' "
                "(pir_id + relationship_id + source_id; flag also needs "
                "matching_criteria). PIRs match RELATIONSHIP events, so each "
                "pir_criteria entry is {\"weight\": int, \"filters\": FILTER_GROUP} "
                "whose filters MUST contain BOTH an 'entity_type' filter (the "
                "relationship type, e.g. 'targets') AND a 'toId' filter (OpenCTI "
                "internal IDs - resolve entities with search_opencti first, names "
                "are rejected). Example criterion: {\"weight\": 1, \"filters\": "
                "{\"mode\": \"and\", \"filterGroups\": [], \"filters\": [{\"key\": "
                "[\"entity_type\"], \"values\": [\"targets\"], \"operator\": \"eq\", "
                "\"mode\": \"or\"}, {\"key\": [\"toId\"], \"values\": "
                "[\"ENTITY_UUID\"], \"operator\": \"eq\", \"mode\": \"or\"}]}}. "
                "pir_filters is a scope FilterGroup (e.g. confidence gte 60, values "
                "as strings) that excludes non-matching relationships. Pass "
                "pir_filters and pir_criteria as JSON strings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "delete", "flag", "unflag"],
                    },
                    "pir_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "pir_type": {
                        "type": "string",
                        "enum": ["THREAT_LANDSCAPE", "THREAT_ORIGIN", "THREAT_CUSTOM"],
                        "default": "THREAT_LANDSCAPE",
                    },
                    "pir_rescan_days": {"type": "integer", "description": "Rescan period in days (create default 30; on update, omit to keep current)."},
                    "pir_filters": {"type": "string", "description": "Scope FilterGroup for create (e.g. confidence gte), serialized as a JSON string. Filter values must be strings."},
                    "pir_criteria": {"type": "string", "description": "JSON string: array of {weight, filters(FilterGroup)} for create. Each criterion's filters must contain an 'entity_type' filter (relationship type, e.g. 'targets') and a 'toId' filter (entity UUIDs resolved via search_opencti)."},
                    "relationship_id": {"type": "string", "description": "Relationship ID (flag/unflag)."},
                    "source_id": {"type": "string", "description": "Flagged source entity ID (flag/unflag)."},
                    "matching_criteria": {"type": "string", "description": "JSON string: array of {weight, filters} (flag). Same shape as pir_criteria (entity_type + toId filters)."},
                    "relationship_author_id": {"type": "string", "description": "Optional author ID (flag)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="query_opencti_pir_entities",
            description=(
                "List the ENTITIES (StixDomainObjects) flagged into a PIR, ranked "
                "by their PIR score (read-only; requires PIRAPI). This is the right "
                "tool to answer 'what is flagged in this PIR and why, ranked by "
                "score'. Requires pir_id. Optional: entity_types (e.g. "
                "['Intrusion-Set','Malware']), search, filters (an extra FilterGroup "
                "JSON string, narrowing the flagged entities - e.g. by objectLabel "
                "or createdBy). Order by pir_score (default), last_pir_score_date, "
                "created_at, created, name, or entity_type. Returns each flagged "
                "entity with its PIR score, last score change, and the matching "
                "criteria that explain why it was flagged. Mirrors the OpenCTI UI "
                "PIR Knowledge > Entities list (the canonical stixDomainObjects "
                "query with a pirId - the only way to order by pir_score)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pir_id": {"type": "string", "description": "PIR ID (required)."},
                    "entity_types": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these flagged entity types (e.g. ['Intrusion-Set','Malware'])."},
                    "search": {"type": "string"},
                    "filters": {"type": "string", "description": "Extra FilterGroup JSON string to narrow the flagged entities (advanced; merged with the PIR scope). pir_score / last_pir_score_date keys are auto-scoped to this PIR."},
                    "order_by": {"type": "string", "enum": ["pir_score", "last_pir_score_date", "created_at", "created", "name", "entity_type"], "default": "pir_score"},
                    "order_mode": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["pir_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="query_opencti_pir_relationships",
            description=(
                "List the underlying in-pir RELATIONSHIPS flagged into a PIR "
                "(read-only; lower-level than query_opencti_pir_entities). Requires "
                "pir_id. Optional filters: from_types (e.g. "
                "['Intrusion-Set','Malware']), from_id, confidences, search, filters "
                "(FilterGroup JSON string), start_date/end_date (ISO 8601). Ordered "
                "by created_at (default) or updated_at. NOTE: ordering by pir_score "
                "is NOT supported here (OpenCTI requires the pirId on the entity "
                "query) - to rank flagged ENTITIES by PIR score, use "
                "query_opencti_pir_entities instead. Each relationship shows its "
                "source entity and per-relationship PIR score."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pir_id": {"type": "string", "description": "PIR ID (required)."},
                    "from_types": {"type": "array", "items": {"type": "string"}, "description": "Filter by source entity types."},
                    "from_id": {"type": "array", "items": {"type": "string"}, "description": "Filter by specific source entity IDs."},
                    "confidences": {"type": "array", "items": {"type": "integer"}, "description": "Filter by confidence levels."},
                    "search": {"type": "string"},
                    "filters": {"type": "string", "description": "FilterGroup JSON string (advanced)."},
                    "start_date": {"type": "string", "description": "ISO 8601 start (optional)."},
                    "end_date": {"type": "string", "description": "ISO 8601 end (optional)."},
                    "order_by": {"type": "string", "enum": ["created_at", "updated_at"], "default": "created_at", "description": "pir_score is not sortable on relationships; use query_opencti_pir_entities to rank by score."},
                    "order_mode": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    "limit": {"type": "integer", "default": 25},
                    "after": {
                        "type": "string",
                        "description": "Cursor of the next page, as the previous answer names it.",
                    },
                },
                "required": ["pir_id"],
            },
            source=_SOURCE,
        ),
    ]
