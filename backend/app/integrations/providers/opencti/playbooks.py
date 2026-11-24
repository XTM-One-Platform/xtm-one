"""OpenCTI playbook management tools.

Consolidated, action-based tools for the OpenCTI automation playbooks
(``src/modules/playbook/playbook.graphql``):

* ``manage_opencti_playbook``      - list/get/create/delete/toggle/execute/
                                     duplicate/components
* ``edit_opencti_playbook_graph``  - add/replace/insert nodes, add/delete
                                     links, update canvas positions

Playbooks are platform automation, NOT STIX knowledge, so these tools are
not draft-gated.

CRITICAL node configuration contract: OpenCTI component configuration
schemas (``playbook-components.ts``) declare ``filters`` as a *string* — a
FilterGroup serialized as JSON **inside** the configuration JSON (double
encoding). Components do ``JSON.parse(configuration.filters)`` at execution
time and the dashboard does the same to render node filters. When the LLM
passes ``filters`` as a nested object, OpenCTI stores it without error but the
node renders with empty filters and the playbook silently never matches
anything. ``_normalize_node_configuration`` therefore accepts both forms,
validates the FilterGroup shape, and always stores the double-encoded string.

OpenCTI does not check the REST of a node configuration against the
component's schema either, and reads it back as the schema declares: one
stored ``inPirFilters`` object (an array is declared) broke a platform's
resolved-filters cache and every entity creation with it. Every
property is therefore checked against the type the component's
``configuration_schema`` declares before the node is sent
(``_configuration_type_error``); ``components`` shows that schema. Some
schemas declare string items for arrays whose items the executor reads as
``{label, value}`` objects: for the components whose runtime shape is known
(``_ARRAY_ITEM_SHAPES``), the items are checked against that shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Any

from app.services.content_safety import wrap_untrusted
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql
from .access import _refuse_unapplied
from .workflow import resolve_configuration_filters

# The fields each action of manage_opencti_playbook reads; any other field passed (an
# argument the tool does not declare included) is refused by name and nothing is sent.
# A field left at its default (running false, limit 20) counts as not passed.
_PLAYBOOK_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"playbook_id"}),
    "create": frozenset({"name", "description"}),
    "delete": frozenset({"playbook_id"}),
    "toggle": frozenset({"playbook_id", "running"}),
    "execute": frozenset({"playbook_id", "entity_id"}),
    "duplicate": frozenset({"playbook_id"}),
    "components": frozenset({"component_id"}),
}

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"

# Components whose configuration schema REQUIRES filters (playbook-components.ts):
# the node is non-functional without them, so reject early instead of letting
# OpenCTI store a broken node.
_FILTER_REQUIRED_COMPONENTS = {
    "PLAYBOOK_INTERNAL_DATA_CRON",
    "PLAYBOOK_FILTERING_COMPONENT",
    "PLAYBOOK_REDUCING_COMPONENT",
}

# Components that accept optional filters - warn (don't fail) when omitted so
# the agent can surface it instead of silently confirming success.
_FILTER_CAPABLE_COMPONENTS = _FILTER_REQUIRED_COMPONENTS | {
    "PLAYBOOK_INTERNAL_DATA_STREAM",
    "PLAYBOOK_INTERNAL_MANUAL_TRIGGER",
}


def _normalize_filter_group(obj: object) -> dict | None:
    """Coerce a loosely shaped filter group into OpenCTI's canonical FilterGroup.

    LLMs frequently emit scalar keys, numeric values, or omit ``filterGroups``.
    Playbook node filters are re-parsed by the dashboard and by
    ``isStixMatchFilterGroup`` at execution time, so anything non-canonical is
    silently broken downstream. Returns the normalized FilterGroup, or ``None``
    when the value cannot be interpreted as one.
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
        # operator/mode must stay canonical strings - forwarding a non-string
        # (operator: 1, mode: true) would re-create the silently-broken node
        # this normalization exists to prevent. Default only for None/"" so
        # falsy non-strings (operator: 0, mode: false) are rejected too.
        operator = item.get("operator")
        if operator in (None, ""):
            operator = "eq"
        if not isinstance(operator, str):
            return None
        item_mode = item.get("mode")
        if item_mode in (None, ""):
            item_mode = "or"
        if not isinstance(item_mode, str) or item_mode.lower() not in ("and", "or"):
            return None
        filters.append(
            {
                "key": key,
                "values": normalized_values,
                "operator": operator.lower(),
                "mode": item_mode.lower(),
            }
        )
    groups: list[dict] = []
    for group in raw_groups:
        normalized = _normalize_filter_group(group)
        if normalized is None:
            return None
        groups.append(normalized)
    return {"mode": mode.lower(), "filters": filters, "filterGroups": groups}


def _has_filter_content(filter_group: dict) -> bool:
    """True when the (normalized) FilterGroup contains at least one filter.

    Recursive: a group whose only content is empty nested filterGroups
    matches nothing and must not satisfy the required-filters gate.
    """
    if filter_group.get("filters"):
        return True
    return any(
        _has_filter_content(group) for group in filter_group.get("filterGroups", [])
    )


def _normalize_node_configuration(
    configuration: str, component_id: str
) -> tuple[str, str | None, str]:
    """Validate and normalize a node configuration JSON string.

    Returns ``(normalized_configuration, error, warning)``. The ``filters``
    entry - if present - is validated as a FilterGroup (object or JSON string
    form accepted) and ALWAYS stored as a JSON string, which is what OpenCTI's
    component configuration schemas declare (double encoding). Components that
    require filters fail fast when they are missing or empty; filter-capable
    components produce a warning instead.
    """
    config: object = {}
    if configuration:
        try:
            config = json.loads(configuration)
        except ValueError as exc:
            return configuration, f"configuration must be valid JSON: {exc}", ""
        if not isinstance(config, dict):
            return (
                configuration,
                "configuration must be a JSON object matching the component's "
                "configuration schema (see manage_opencti_playbook "
                "action='components').",
                "",
            )

    filters_value = config.get("filters") if isinstance(config, dict) else None
    normalized_filters: dict | None = None
    if filters_value == "" and isinstance(config, dict):
        # An explicitly empty filters string would make components'
        # JSON.parse(configuration.filters) throw - treat it as absent.
        config.pop("filters", None)
    if filters_value not in (None, ""):
        raw: object = filters_value
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError as exc:
                return configuration, f"configuration.filters must be valid JSON: {exc}", ""
        normalized_filters = _normalize_filter_group(raw)
        if normalized_filters is None:
            return (
                configuration,
                "configuration.filters must be a FilterGroup object with 'mode' "
                "('and'/'or'), 'filters' (array of {key, values, operator?, "
                "mode?}) and 'filterGroups' (array). Pass it as an object or as "
                "a JSON string - it is stored as a JSON string either way.",
                "",
            )
        if _has_filter_content(normalized_filters):
            # OpenCTI declares configuration.filters as a STRING (double-
            # encoded FilterGroup) - storing an object renders as empty
            # filters in the dashboard and never matches at execution time.
            config["filters"] = json.dumps(normalized_filters)
        else:
            # An empty/hollow FilterGroup matches nothing - drop the key so
            # the stored configuration matches the "no filters" warning
            # instead of persisting an explicitly empty FilterGroup string.
            config.pop("filters", None)

    has_filters = normalized_filters is not None and _has_filter_content(
        normalized_filters
    )
    if component_id in _FILTER_REQUIRED_COMPONENTS and not has_filters:
        return (
            configuration,
            f"Component {component_id} requires non-empty configuration.filters "
            "(a FilterGroup). The node would be stored but the playbook would "
            "never match anything.",
            "",
        )
    warning = ""
    if component_id in _FILTER_CAPABLE_COMPONENTS and not has_filters:
        warning = (
            f" WARNING: no filters configured for {component_id} - the node "
            "will process ALL events. Add configuration.filters (FilterGroup) "
            "if this is not intended."
        )
    if not configuration:
        return "", None, warning
    return json.dumps(config), None, warning


#: JSON Schema type -> the Python values ``json.loads`` gives for it, in the
#: order a received value is named (a bool is never a number, an integral
#: float is an integer, as JSON Schema counts them).
_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int, float),
    "number": (int, float),
}


#: Array properties whose items a component's executor reads as objects
#: carrying the referenced id in ``value`` (``label`` is the name the OpenCTI
#: form shows), while its ``configuration_schema`` may declare string items.
#: component id -> property -> whether a bare id string is read too.
#: OpenCTI (``modules/playbook/components/``): ``PirStreamConfiguration.
#: inPirFilters`` is read as ``pir.value`` by the PIR stream listener and the
#: resolved-filters cache, ``authorized_members`` as ``m.value`` by
#: ``convertMembersToUsersFromElements``, ``caseTemplates`` as
#: ``caseTemplates[i].value``; the sharing components map
#: ``typeof o !== 'string' ? o.value : o``. A bare id elsewhere is stored and
#: the node matches or targets nothing, silently. Components and
#: properties not listed are not checked item by item.
_ARRAY_ITEM_SHAPES: dict[str, dict[str, bool]] = {
    "PLAYBOOK_DATA_STREAM_PIR": {"inPirFilters": False},
    "PLAYBOOK_NOTIFIER_COMPONENT": {"authorized_members": False},
    "PLAYBOOK_CONTAINER_WRAPPER_COMPONENT": {"caseTemplates": False},
    "PLAYBOOK_SHARING_COMPONENT": {"organizations": True},
    "PLAYBOOK_UNSHARING_COMPONENT": {"organizations": True},
}


def _item_shape_problem(item: object, bare_id: bool) -> str | None:
    """Why *item* is not an item the component reads, or ``None``."""
    if isinstance(item, str) and bare_id:
        return None if item.strip() else "an empty id"
    if item is None:
        return "null"
    if not isinstance(item, dict):
        name = _json_type_name(item)
        return f"{'an' if name[0] in 'aeiou' else 'a'} {name}"
    value = item.get("value")
    if not isinstance(value, str) or not value.strip():
        return 'an object without a non-empty string "value"'
    if "label" in item and not isinstance(item["label"], str):
        return 'an object whose "label" is not a string'
    return None


def _array_item_mismatches(config: dict, component_id: str) -> list[str]:
    """One sentence per array property of ``_ARRAY_ITEM_SHAPES`` whose items
    are not the shape the component's runtime reads, naming the first one."""
    mismatches: list[str] = []
    for key, bare_id in _ARRAY_ITEM_SHAPES.get(component_id, {}).items():
        items = config.get(key)
        if not isinstance(items, list):
            continue  # absent, null, or a wrong type the schema check names
        for index, item in enumerate(items):
            problem = _item_shape_problem(item, bare_id)
            if problem is None:
                continue
            accepted = (
                'ids, or objects {"value": "ID", "label": "NAME"}'
                if bare_id
                else 'objects {"value": "ID", "label": "NAME"}'
            )
            mismatches.append(
                f"configuration.{key} items must be {accepted} (label optional), "
                f"which is what {component_id} reads, whatever item type its "
                f"schema shows; item {index} is {problem}."
            )
            break
    return mismatches


def _matches_json_type(value: object, json_type: str) -> bool:
    python_types = _JSON_TYPES.get(json_type)
    if python_types is None:
        return True  # a type this check does not know is not refused
    if isinstance(value, bool):
        return json_type == "boolean"
    if json_type == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, python_types)


def _is_json_text_of(text: str, types: list) -> bool:
    """Whether *text* is the JSON text of an object or array of *types*."""
    try:
        decoded = json.loads(text)
    except ValueError:
        return False
    return isinstance(decoded, (dict, list)) and any(
        _matches_json_type(decoded, t) for t in types if isinstance(t, str)
    )


def _json_type_name(value: object) -> str:
    return next(
        (name for name in _JSON_TYPES if _matches_json_type(value, name)),
        type(value).__name__,
    )


#: Said when a value of an AI-agent component is not one of its choices:
#: the choices are the agents the connected platform reads from the intent
#: catalog, so a missing agent is a binding or sharing matter, not a typo.
_AGENT_CHOICE_HINT = (
    " The choices are the agents the intent catalog offers this component: an "
    "agent appears there once it is enabled, shared with a group or company-managed, "
    "and bound to the component's intent for the platform's vertical "
    "(list_agent_intent_bindings, bind_agent_to_intent)."
)


def _declared_choices(declared: dict[str, object]) -> list[object] | None:
    """The ``const`` values of a property's NON-EMPTY top-level ``oneOf``,
    or ``None`` when it declares no closed list (no ``oneOf``, an empty one,
    or a choice without ``const``): those stay form hints."""
    choices = declared.get("oneOf")
    if not isinstance(choices, list) or not choices:
        return None
    if not all(isinstance(c, dict) and "const" in c for c in choices):
        return None
    return [c["const"] for c in choices]


def _choice_mismatch(key: str, value: object, declared: dict[str, object], component_id: str) -> str | None:
    """The sentence for a value outside a property's declared choices.

    A list value is checked item by item (a multiple select); a value of
    another type is left to the type check.
    """
    allowed = _declared_choices(declared)
    if allowed is None:
        return None
    values = value if isinstance(value, list) else [value]
    # An empty string is a select left unset, as the platform's forms store it.
    outside = [v for v in values if v != "" and v not in allowed]
    if not outside:
        return None
    shown = ", ".join(_compact_json(v) for v in allowed[:50])
    if len(allowed) > 50:
        shown += f" (and {len(allowed) - 50} more)"
    hint = _AGENT_CHOICE_HINT if "AI_AGENT" in (component_id or "").upper() else ""
    return (
        f"configuration.{key} must be one of the component's choices; "
        f"got {', '.join(_compact_json(v) for v in outside[:10])}. Allowed: {shown}.{hint}"
    )


def _configuration_type_error(
    configuration: str, schema_json: str | None, component_id: str = ""
) -> str | None:
    """Every property whose JSON type differs from the component's schema,
    every value outside a property's non-empty ``oneOf`` choices, and every
    listed array whose items are not the shape the runtime reads.

    One sentence per property, so an agent fixes them all in one retry.

    OpenCTI stores a node configuration without validating it, and reads it
    back as the schema declares: an ``inPirFilters`` written as an object
    instead of an array broke the platform-wide resolved-filters cache, and
    with it every entity creation. Only the declared top-level
    ``type`` is compared - the schemas also carry form hints (``$ref`` labels,
    empty ``oneOf``, string ``items`` the runtime reads as objects) that are
    not validation rules. A NON-EMPTY top-level ``oneOf`` of ``const``
    values is a closed list: the runtime of an AI-agent component re-checks
    the agent against the intent catalog and routes the bundle to its
    ``unmodified`` port without calling it, so a value outside the list is
    refused here rather than saved as a node that silently does nothing. A
    schema that cannot be read checks no type. The
    items of the arrays in ``_ARRAY_ITEM_SHAPES`` are checked against the
    shape the component's executor reads, with or without a schema.
    """
    if not configuration:
        return None
    try:
        config = json.loads(configuration)
    except ValueError:
        return None
    if not isinstance(config, dict):
        return None
    try:
        schema = json.loads(schema_json) if schema_json else None
    except ValueError:
        schema = None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        properties = {}
    mismatches: list[str] = []
    for key, value in config.items():
        declared = properties.get(key)
        if value is None or not isinstance(declared, dict):
            continue
        if "type" not in declared:
            if choice_error := _choice_mismatch(key, value, declared, component_id):
                mismatches.append(choice_error)
            continue
        types = declared["type"] if isinstance(declared["type"], list) else [declared["type"]]
        if not any(_matches_json_type(value, t) for t in types if isinstance(t, str)):
            expected = " or ".join(str(t) for t in types)
            # A string property can hold a JSON-encoded structure
            # (``applyWithFilters`` is a FilterGroup, as ``filters`` is): an
            # agent that sent the structure itself is told how to send it,
            # rather than having a string it may not mean written for it.
            # The reverse mistake is told too: the JSON text of a value sent
            # where the value itself is declared.
            hint = ""
            if "string" in types and isinstance(value, (dict, list)):
                hint = (
                    " A JSON-encoded value is expected: pass the JSON text of "
                    "the value as a string."
                )
            elif isinstance(value, str) and _is_json_text_of(value, types):
                hint = " Pass the value itself, not its JSON text."
            mismatches.append(
                f"configuration.{key} must be of type {expected}, as the "
                f"component's configuration schema declares; got {_json_type_name(value)}.{hint}"
            )
            continue
        if choice_error := _choice_mismatch(key, value, declared, component_id):
            mismatches.append(choice_error)
    mismatches.extend(_array_item_mismatches(config, component_id))
    if not mismatches:
        return None
    return (
        " ".join(mismatches)
        + " Nothing was saved. Read the schema with manage_opencti_playbook "
        "action='components' and component_id."
    )


#: How much of one component's configuration schema ``components`` shows.
_SCHEMA_DISPLAY_LIMIT = 8000

#: How many failed schema reads the node writes of one tool set make before
#: they stop asking and send their nodes unchecked.
_SCHEMA_READ_ATTEMPTS = 2


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _choice_label(choice: object) -> str:
    """One ``oneOf`` choice on one line: its JSON-encoded ``const`` and
    title, so a title the platform's users wrote cannot add a line."""
    if isinstance(choice, dict) and "const" in choice:
        const, title = choice["const"], choice.get("title")
        label = _compact_json(const)
        return f"{label} {_compact_json(title)}" if title not in (None, "", const) else label
    return _compact_json(choice)


def _schema_for_display(schema_json: str) -> str:
    """A component's configuration schema, cut to ``_SCHEMA_DISPLAY_LIMIT``.

    A schema can list every connector, agent or template of the platform as
    ``oneOf`` choices. Past the limit, those lists are moved after the
    schema, so the cut takes choices and keeps the property names and types.
    """
    if len(schema_json) <= _SCHEMA_DISPLAY_LIMIT:
        return schema_json
    choices: list[str] = []

    def _without_choices(node: object, path: str) -> object:
        if isinstance(node, list):
            return [_without_choices(item, path) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, object] = {}
        for key, value in node.items():
            if key == "oneOf" and isinstance(value, list) and value:
                out[key] = f"{len(value)} choices, listed after the schema"
                choices.extend(f"- {path}: {_choice_label(choice)}" for choice in value)
            else:
                out[key] = _without_choices(value, f"{path}.{key}" if path else key)
        return out

    try:
        text = _compact_json(_without_choices(json.loads(schema_json), ""))
    except ValueError:
        text = schema_json
    if choices:
        text += "\nChoices (oneOf):\n" + "\n".join(choices)
    if len(text) > _SCHEMA_DISPLAY_LIMIT:
        text = text[:_SCHEMA_DISPLAY_LIMIT] + " ... (truncated)"
    return text


# The graph ``get`` shows: every node and link up to these bounds, then a note
# naming what was left out (a playbook of a few dozen nodes is shown whole).
_GRAPH_NODE_LIMIT = 200
_GRAPH_LINK_LIMIT = 400
_GRAPH_CONFIG_LIMIT = 1_000

# Graph edits on one playbook, serialized in this process: OpenCTI rewrites the
# whole ``playbook_definition`` on every node or link mutation (read, modify,
# write), so two edits sent together each write the definition they read and
# the later one drops what the other added. Keyed by platform and playbook; a
# lock nobody holds or waits on is dropped with its last reference.
_GRAPH_LOCKS: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = weakref.WeakValueDictionary()

_POSITIONS_SHAPE = (
    'a JSON object mapping node ids to {"x": NUMBER, "y": NUMBER}, e.g. '
    '{"NODE_ID": {"x": 100, "y": 200}}, or the array OpenCTI stores, '
    '[{"id": "NODE_ID", "position": {"x": 100, "y": 200}}]'
)


def _graph_lock(base_url: str, playbook_id: str) -> asyncio.Lock:
    key = (base_url, playbook_id)
    lock = _GRAPH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _GRAPH_LOCKS[key] = lock
    return lock


def _parentless_warning(graph: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None) -> str:
    """A one-line warning naming the nodes no link points to, when there are
    several: the OpenCTI editor lays a playbook out as one tree and refuses to
    open it ("multiple roots") until every node but the entry is linked."""
    if graph is None:
        return ""
    nodes, links = graph
    targets = {str((link.get("to") or {}).get("id")) for link in links}
    parentless = [str(n.get("id")) for n in nodes if n.get("id") and str(n.get("id")) not in targets]
    if len(parentless) < 2:
        return ""
    return (
        f" WARNING: nodes without a parent link: {', '.join(parentless)}. The OpenCTI "
        "editor cannot open the playbook until only the entry node has none: link "
        "them with add_link (or add nodes with parent_node_id/parent_port_id)."
    )


def _parse_graph(definition: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """The nodes and links of a ``playbook_definition``, None when unreadable."""
    if not definition:
        return [], []
    try:
        parsed = json.loads(definition) if isinstance(definition, str) else definition
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    nodes = [n for n in parsed.get("nodes") or [] if isinstance(n, dict)]
    links = [link for link in parsed.get("links") or [] if isinstance(link, dict)]
    return nodes, links


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _fmt_position(position: Any) -> str:
    if isinstance(position, dict) and _number(position.get("x")) and _number(position.get("y")):
        return f"({position['x']:g}, {position['y']:g})"
    return "(none)"


def _fmt_graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> str:
    """Every node (id, name, component, position, configuration) and link."""
    lines = ["nodes:"]
    for node in nodes[:_GRAPH_NODE_LIMIT]:
        lines.append(
            f"- {node.get('id', '?')} '{node.get('name', '')}' "
            f"[{node.get('component_id', '?')}] at {_fmt_position(node.get('position'))}"
        )
        config = node.get("configuration")
        if config not in (None, "", "{}"):
            text = config if isinstance(config, str) else _compact_json(config)
            if len(text) > _GRAPH_CONFIG_LIMIT:
                text = text[:_GRAPH_CONFIG_LIMIT] + " ... (truncated)"
            lines.append(f"    configuration: {text}")
    if len(nodes) > _GRAPH_NODE_LIMIT:
        lines.append(f"({len(nodes) - _GRAPH_NODE_LIMIT} more nodes not shown)")
    lines.append("links:" if links else "links: none")
    for link in links[:_GRAPH_LINK_LIMIT]:
        source = link.get("from") or {}
        target = link.get("to") or {}
        lines.append(
            f"- {link.get('id', '?')}: {source.get('id', '?')} "
            f"(port {source.get('port', '?')}) -> {target.get('id', '?')}"
        )
    if len(links) > _GRAPH_LINK_LIMIT:
        lines.append(f"({len(links) - _GRAPH_LINK_LIMIT} more links not shown)")
    return "\n".join(lines)


def _positions_payload(positions: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The ``[{id, position: {x, y}}]`` array ``playbookUpdatePositions`` reads.

    OpenCTI ``JSON.parse``s the string and calls ``.filter`` on it: anything
    but an array is an INTERNAL_SERVER_ERROR. The friendly map is converted.
    """
    try:
        parsed = json.loads(positions)
    except ValueError as exc:
        return None, f"Error: positions must be valid JSON ({exc}); expected {_POSITIONS_SHAPE}."
    if isinstance(parsed, dict):
        parsed = [{"id": key, "position": value} for key, value in parsed.items()]
    if not isinstance(parsed, list) or not parsed:
        return None, f"Error: positions is empty or not a map of node positions; expected {_POSITIONS_SHAPE}."
    payload = []
    for item in parsed:
        position = item.get("position") if isinstance(item, dict) else None
        node = item.get("id") if isinstance(item, dict) else None
        if not (
            isinstance(node, str)
            and node
            and isinstance(position, dict)
            and _number(position.get("x"))
            and _number(position.get("y"))
        ):
            return None, (
                f"Error: positions entry {_compact_json(item)[:200]} has no node id "
                f"and numeric x and y; expected {_POSITIONS_SHAPE}."
            )
        payload.append({"id": node, "position": {"x": position["x"], "y": position["y"]}})
    return payload, None


# OpenCTI's PlaybookExecute answers this for every playbook not eligible for
# the entity, not only for a missing one (playbook-domain.ts).
_INELIGIBLE_ERROR = "does not exist for this entity"

# The components a playbook can start from (playbook-components.ts).
_ENTRY_COMPONENTS = {
    "PLAYBOOK_INTERNAL_DATA_STREAM",
    "PLAYBOOK_INTERNAL_MANUAL_TRIGGER",
    "PLAYBOOK_INTERNAL_DATA_CRON",
}


def _entry_nodes(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The nodes no link points to: the playbook's entry (one in a valid graph)."""
    targets = {str((link.get("to") or {}).get("id")) for link in links}
    return [n for n in nodes if n.get("id") and str(n.get("id")) not in targets]


def _ineligible_hint(playbook_id: str, entity_id: str, pb: dict[str, Any] | None, read_error: str | None) -> str:
    """What an agent checks when OpenCTI refuses to execute *pb* on an entity.

    OpenCTI words every ineligibility as "Playbook does not exist for this
    entity": the entity does not match the entry component's filters, the
    entry is not a stream listener or a manual trigger, manual enrollment is
    off on it, or Enterprise Edition is off. The entry node and its
    configuration (its filters) are shown so the mismatch can be checked.
    """
    lines = [
        f"Hint: OpenCTI gives this error when playbook {playbook_id} exists but is not "
        f"eligible for entity {entity_id}: most often the entity does not match the "
        "filters of the playbook's entry component; also when the entry is neither a "
        "live stream listener nor a manual trigger, manual enrollment is off on it, "
        "or Enterprise Edition is off. Compare the entity with the entry filters "
        "below, then change the entity or the filters and execute again."
    ]
    if read_error or not pb:
        lines.append(
            f"The playbook could not be read to show its entry ({read_error or 'not found'}); "
            "read it with action 'get'."
        )
        return "\n".join(lines)
    state = "running" if pb.get("playbook_running") else "stopped"
    lines.append(f"Playbook '{pb.get('name', '?')}' is {state}.")
    graph = _parse_graph(pb.get("playbook_definition"))
    entries = _entry_nodes(*graph) if graph else []
    if not entries:
        lines.append("Its entry node could not be read; read the playbook with action 'get'.")
        return "\n".join(lines)
    for node in entries:
        component = str(node.get("component_id") or "?")
        kind = "" if component in _ENTRY_COMPONENTS else " (not an entry component)"
        config = node.get("configuration")
        text = config if isinstance(config, str) else _compact_json(config) if config else ""
        if len(text) > _GRAPH_CONFIG_LIMIT:
            text = text[:_GRAPH_CONFIG_LIMIT] + " ... (truncated)"
        detail = f"entry node {node.get('id')} [{component}]{kind}, configuration: {text or '(none)'}"
        # Node names and configurations are text the platform's users wrote.
        lines.append(wrap_untrusted(detail, f"OpenCTI playbook {playbook_id} entry"))
    return "\n".join(lines)


def _fmt_playbook(node: dict) -> str:
    running = node.get("playbook_running")
    state = "running" if running else "stopped"
    parts = [f"- {node.get('name', '?')} [{state}] (id: {node.get('id', '?')})"]
    if node.get("description"):
        parts.append(f"    {node['description']}")
    return "\n".join(parts)


# Step statuses OpenCTI writes for a step that did its work; any other status
# (error, failed, ...) is a failed step whose message is shown.
_STEP_OK_STATUSES = frozenset({"success", "succeeded", "complete", "completed", "done", "ok"})
_EXECUTIONS_SHOWN = 5
_STEP_MESSAGE_LIMIT = 400


def _fmt_executions(execs: list[dict[str, Any]], node_names: dict[str, str]) -> list[str]:
    """The recent executions with each failed step and its reason.

    A step that failed (an AI agent call that errored, a connector that
    refused) is otherwise only visible in the OpenCTI UI: the reason is the
    step's ``message``, shown here with the node name it ran.
    """
    lines = [f"  recent executions: {len(execs)}"]
    for execution in execs[:_EXECUTIONS_SHOWN]:
        steps = execution.get("steps") or []
        failed = [s for s in steps if str(s.get("status") or "").lower() not in _STEP_OK_STATUSES]
        verdict = f"{len(failed)} failed" if failed else "no failed step"
        lines.append(
            f"  - execution {execution.get('id', '?')} at {execution.get('execution_start') or '?'}: "
            f"{len(steps)} step(s), {verdict}"
        )
        for step in failed:
            step_id = str(step.get("id") or "?")
            name = node_names.get(step_id) or node_names.get(step_id.split("-", 1)[-1]) or ""
            label = f"{name} ({step_id})" if name else step_id
            message = str(step.get("message") or "no message")
            if len(message) > _STEP_MESSAGE_LIMIT:
                message = message[:_STEP_MESSAGE_LIMIT] + " ... (truncated)"
            # A step message can quote the processed entity: data, never instructions.
            lines.append(
                wrap_untrusted(
                    f"failed step {label}, status {step.get('status') or 'unknown'}: {message}",
                    "OpenCTI playbook step",
                )
            )
    return lines


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    # The components' configuration schemas, kept for the tool set (one run):
    # several of OpenCTI's schema resolvers query its database, and a playbook
    # is built node by node. A successful answer is kept, an empty list too.
    # A failed one is asked again by a later node write until the node writes
    # saw ``_SCHEMA_READ_ATTEMPTS`` failed reads - one transient error does
    # not leave the rest of the run unchecked, and a platform that
    # keeps failing is not queried on every node. An explicit schema read
    # (``components`` with component_id) always asks again and its failure
    # costs the node writes nothing. Calls are serialized: one that waited
    # while another read was answered takes that answer, so writes issued
    # together share one read, a failed one included.
    _lookup: tuple[dict[str, str | None], str | None] | None = None
    _reads_sent = 0
    _failed_write_reads = 0
    _lookup_lock = asyncio.Lock()

    async def _component_schemas(
        *, retry_failed: bool = False
    ) -> tuple[dict[str, str | None], str | None]:
        nonlocal _lookup, _reads_sent, _failed_write_reads
        reads_before = _reads_sent
        async with _lookup_lock:
            if _lookup is not None and (
                not _lookup[1]
                or _reads_sent != reads_before
                or not (retry_failed or _failed_write_reads < _SCHEMA_READ_ATTEMPTS)
            ):
                return _lookup
            data, err = await _gql(
                "query PlaybookComponentSchemas {"
                "  playbookComponents { id configuration_schema } }"
            )
            _reads_sent += 1
            if err and not retry_failed:
                _failed_write_reads += 1
            schemas = {} if err else {
                c.get("id"): c.get("configuration_schema")
                for c in data.get("playbookComponents") or []
                if c.get("id")
            }
            _lookup = (schemas, err)
            return _lookup

    async def manage_opencti_playbook(
        action: str,
        playbook_id: str = "",
        name: str = "",
        description: str = "",
        running: bool = False,
        entity_id: str = "",
        search: str = "",
        limit: int = 20,
        component_id: str = "",
        **unknown: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _PLAYBOOK_APPLIES,
            {
                "playbook_id": playbook_id,
                "name": name,
                "description": description,
                "running": running,
                "entity_id": entity_id,
                "search": search,
                "limit": None if limit == 20 else limit,
                "component_id": component_id,
            },
            dict(unknown),
        )
        if refused:
            return refused

        if action == "list":
            query = (
                "query Playbooks($first: Int, $search: String) {"
                "  playbooks(first: $first, search: $search,"
                "    orderBy: name, orderMode: asc) {"
                "    edges { node { id name description playbook_running"
                "      queue_messages } }"
                "  }"
                "}"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "playbooks")
            if not nodes:
                return "No playbooks found."
            return f"Playbooks ({len(nodes)}):\n" + "\n".join(
                _fmt_playbook(n) for n in nodes
            )

        if action == "get":
            if not playbook_id:
                return "playbook_id is required for action 'get'."
            query = (
                "query Playbook($id: String!) {"
                "  playbook(id: $id) { id name description playbook_running"
                "    playbook_definition queue_messages"
                "    last_executions { id execution_start"
                "      steps { id status message duration } } }"
                "}"
            )
            data, err = await _gql(query, {"id": playbook_id})
            if err:
                return err
            pb = data.get("playbook")
            if not pb:
                return f"Playbook {playbook_id} not found."
            lines = [
                _fmt_playbook(pb),
                f"  queue_messages: {pb.get('queue_messages', 0)}",
            ]
            execs = pb.get("last_executions") or []
            definition = pb.get("playbook_definition")
            graph = _parse_graph(definition)
            if execs:
                node_names = {
                    str(n.get("id")): str(n.get("name") or "")
                    for n in (graph[0] if graph else [])
                    if isinstance(n, dict) and n.get("id")
                }
                lines.extend(_fmt_executions(execs, node_names))
            if graph is None:
                lines.append(f"  definition: {len(definition)} chars, not readable JSON")
            else:
                nodes, links = graph
                lines.append(f"  graph: {len(nodes)} nodes, {len(links)} links")
                if nodes or links:
                    # Node names and configurations are text the platform's
                    # users wrote: data, never instructions.
                    lines.append(wrap_untrusted(_fmt_graph(nodes, links), f"OpenCTI playbook {playbook_id} graph"))
            return "\n".join(lines)

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            query = (
                "mutation PlaybookAdd($input: PlaybookAddInput!) {"
                "  playbookAdd(input: $input) { id name playbook_running }"
                "}"
            )
            data, err = await _gql(
                query, {"input": {"name": name, "description": description or None}}
            )
            if err:
                return err
            pb = data.get("playbookAdd") or {}
            return (
                f"Created playbook '{pb.get('name')}' (id: {pb.get('id')}). "
                "It is stopped by default - use action 'toggle' with running=true "
                "after adding nodes."
            )

        if action == "delete":
            if not playbook_id:
                return "playbook_id is required for action 'delete'."
            query = "mutation PlaybookDelete($id: ID!) { playbookDelete(id: $id) }"
            _, err = await _gql(query, {"id": playbook_id})
            if err:
                return err
            return f"Deleted playbook {playbook_id}."

        if action == "toggle":
            if not playbook_id:
                return "playbook_id is required for action 'toggle'."
            query = (
                "mutation PlaybookToggle($id: ID!, $input: [EditInput!]!) {"
                "  playbookFieldPatch(id: $id, input: $input) {"
                "    id name playbook_running } }"
            )
            data, err = await _gql(
                query,
                {
                    "id": playbook_id,
                    "input": [{"key": "playbook_running", "value": [running]}],
                },
            )
            if err:
                return err
            pb = data.get("playbookFieldPatch") or {}
            new_state = "running" if pb.get("playbook_running") else "stopped"
            return f"Playbook '{pb.get('name')}' is now {new_state}."

        if action == "execute":
            if not playbook_id or not entity_id:
                return "playbook_id and entity_id are required for action 'execute'."
            query = (
                "mutation PlaybookExecute($id: ID!, $entityId: String!) {"
                "  playbookExecute(id: $id, entityId: $entityId) }"
            )
            data, err = await _gql(query, {"id": playbook_id, "entityId": entity_id})
            if err and _INELIGIBLE_ERROR in err.lower():
                pb_data, read_err = await _gql(
                    "query Playbook($id: String!) {"
                    "  playbook(id: $id) { id name playbook_running playbook_definition } }",
                    {"id": playbook_id},
                )
                pb = None if read_err else pb_data.get("playbook")
                return f"{err}\n{_ineligible_hint(playbook_id, entity_id, pb, read_err)}"
            if err:
                return err
            return (
                f"Triggered playbook {playbook_id} on entity {entity_id} "
                f"(accepted: {data.get('playbookExecute')}). The run is asynchronous: "
                "read action='get' for its steps, a failed step (an AI agent call "
                "included) is listed there with its reason."
            )

        if action == "duplicate":
            if not playbook_id:
                return "playbook_id is required for action 'duplicate'."
            query = "mutation PlaybookDuplicate($id: ID!) { playbookDuplicate(id: $id) }"
            data, err = await _gql(query, {"id": playbook_id})
            if err:
                return err
            return f"Duplicated playbook - new playbook id: {data.get('playbookDuplicate')}."

        if action == "components" and component_id:
            schemas, err = await _component_schemas(retry_failed=True)
            if err:
                return err
            if component_id not in schemas:
                return f"No component '{component_id}' is listed by this platform."
            schema = schemas[component_id] or ""
            if not schema:
                return f"Component '{component_id}' declares no configuration schema."
            # The schema carries text the platform's users wrote (connector,
            # agent and template names): data, never instructions.
            return f"Configuration schema of {component_id}:\n" + wrap_untrusted(
                _schema_for_display(schema), f"OpenCTI playbook component {component_id}"
            )

        if action == "components":
            query = (
                "query PlaybookComponents {"
                "  playbookComponents { id name description category"
                "    is_entry_point ports { id type } } }"
            )
            data, err = await _gql(query)
            if err:
                return err
            comps = data.get("playbookComponents") or []
            if not comps:
                return "No playbook components available."
            lines = [f"Playbook components ({len(comps)}):"]
            for c in comps:
                entry = " [entry point]" if c.get("is_entry_point") else ""
                ports = ", ".join(p.get("id", "") for p in (c.get("ports") or []))
                lines.append(
                    f"- {c.get('name')} ({c.get('category')}){entry} "
                    f"(component_id: {c.get('id')})"
                )
                if ports:
                    lines.append(f"    ports: {ports}")
            return "\n".join(lines)

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, delete, "
            "toggle, execute, duplicate, components."
        )

    _graph_query = "query PlaybookGraph($id: String!) {  playbook(id: $id) { id playbook_definition } }"

    async def _read_graph(
        playbook_id: str,
    ) -> tuple[tuple[list[dict[str, Any]], list[dict[str, Any]]] | None, str | None]:
        """The playbook's current nodes and links, or the error to answer."""
        data, err = await _gql(_graph_query, {"id": playbook_id})
        if err:
            return None, err
        pb = (data or {}).get("playbook")
        if not pb:
            return None, f"Error: playbook {playbook_id} not found."
        graph = _parse_graph(pb.get("playbook_definition"))
        if graph is None:
            return None, f"Error: the definition of playbook {playbook_id} is not readable JSON."
        return graph, None

    def _unknown(kind: str, wanted: list[str], known: list[str], playbook_id: str) -> str:
        missing = [w for w in wanted if w not in set(known)]
        if not missing:
            return ""
        listed = ", ".join(known[:30]) + (" ..." if len(known) > 30 else "")
        return (
            f"Error: unknown {kind} id {', '.join(repr(m) for m in missing)} in playbook "
            f"{playbook_id}; its {kind}s are: {listed or 'none'}. Nothing was changed. "
            "Read the graph with manage_opencti_playbook action='get'."
        )

    async def _check_existing(playbook_id: str, node_ids: list[str], link_ids: list[str]) -> str:
        """An error naming the node or link ids the playbook does not hold."""
        graph, err = await _read_graph(playbook_id)
        if err:
            return err
        nodes, links = graph or ([], [])
        node_known = [str(n.get("id")) for n in nodes if n.get("id")]
        link_known = [str(link.get("id")) for link in links if link.get("id")]
        return _unknown("node", node_ids, node_known, playbook_id) or _unknown(
            "link", link_ids, link_known, playbook_id
        )

    async def _lost_after_write(playbook_id: str, node_ids: list[str], link_ids: list[str]) -> str:
        """Read the graph back: an error when a node or link the write created or
        relied on is gone (another writer rewrote the definition meanwhile).
        A read-back that fails proves nothing either way and answers nothing."""
        lost, _ = await _read_back(playbook_id, node_ids, link_ids)
        return lost

    async def _read_back(
        playbook_id: str, node_ids: list[str], link_ids: list[str]
    ) -> tuple[str, tuple[list[dict[str, Any]], list[dict[str, Any]]] | None]:
        """``_lost_after_write`` with the graph it read (None when unreadable)."""
        data, err = await _gql(_graph_query, {"id": playbook_id})
        pb = None if err else (data or {}).get("playbook")
        graph = _parse_graph(pb.get("playbook_definition")) if pb else None
        if graph is None:
            return "", None
        nodes, links = graph
        node_known = {str(n.get("id")) for n in nodes}
        link_known = {str(link.get("id")) for link in links}
        lost = [f"node {n}" for n in node_ids if n and n not in node_known]
        lost += [f"link {link}" for link in link_ids if link and link not in link_known]
        if not lost:
            return "", graph
        return (
            f"Error: OpenCTI answered the write, but {', '.join(lost)} is missing from "
            f"playbook {playbook_id} when read back: another edit of this playbook, "
            "outside this run, rewrote its definition at the same time. Read the graph "
            "with manage_opencti_playbook action='get' and redo the edit."
        ), graph

    async def _link_new_node(
        playbook_id: str, created: str, parent_node_id: str, parent_port_id: str, filter_warning: str
    ) -> str:
        """Link a node ``add_node`` just created under its parent, inside the
        same locked edit; a refused link removes the node again (no orphan)."""
        data, err = await _gql(
            "mutation AddLink($id: ID!, $input: PlaybookAddLinkInput!) {  playbookAddLink(id: $id, input: $input) }",
            {
                "id": playbook_id,
                "input": {"from_node": parent_node_id, "from_port": parent_port_id, "to_node": created},
            },
        )
        if err:
            _, undo_err = await _gql(
                "mutation DeleteNode($id: ID!, $nodeId: ID!) {  playbookDeleteNode(id: $id, nodeId: $nodeId) { id } }",
                {"id": playbook_id, "nodeId": created},
            )
            undo = (
                f" The new node {created} could not be removed ({undo_err}): delete it with action='delete_node'."
                if undo_err
                else " The new node was removed again; the playbook is unchanged."
            )
            return (
                f"Error: the link from node {parent_node_id} (port {parent_port_id}) "
                f"to the new node was refused: {err}.{undo}"
            )
        link = str(data.get("playbookAddLink") or "")
        lost = await _lost_after_write(playbook_id, [created, parent_node_id], [link])
        if lost:
            return lost
        return (
            f"Added node (node_id: {created}) under node {parent_node_id} "
            f"(port {parent_port_id}, link_id: {link}).{filter_warning}"
        )

    async def edit_opencti_playbook_graph(
        action: str,
        playbook_id: str = "",
        name: str = "",
        component_id: str = "",
        position_x: float = 0.0,
        position_y: float = 0.0,
        configuration: str = "",
        description: str = "",
        node_id: str = "",
        link_id: str = "",
        from_node: str = "",
        from_port: str = "",
        to_node: str = "",
        parent_node_id: str = "",
        parent_port_id: str = "",
        child_node_id: str = "",
        positions: str = "",
    ) -> str:
        if not playbook_id:
            return "playbook_id is required."
        # One edit at a time per playbook (see ``_GRAPH_LOCKS``): ``_edit_graph``
        # takes the lock once the arguments are checked, so a burst of writes
        # still shares one component schema read; it is released here.
        lock = _graph_lock(base_url, playbook_id)
        held: list[bool] = []
        try:
            return await _edit_graph(
                lock=lock,
                held=held,
                action=action,
                playbook_id=playbook_id,
                name=name,
                component_id=component_id,
                position_x=position_x,
                position_y=position_y,
                configuration=configuration,
                description=description,
                node_id=node_id,
                link_id=link_id,
                from_node=from_node,
                from_port=from_port,
                to_node=to_node,
                parent_node_id=parent_node_id,
                parent_port_id=parent_port_id,
                child_node_id=child_node_id,
                positions=positions,
            )
        finally:
            if held:
                lock.release()

    async def _edit_graph(
        lock: asyncio.Lock,
        held: list[bool],
        action: str,
        playbook_id: str = "",
        name: str = "",
        component_id: str = "",
        position_x: float = 0.0,
        position_y: float = 0.0,
        configuration: str = "",
        description: str = "",
        node_id: str = "",
        link_id: str = "",
        from_node: str = "",
        from_port: str = "",
        to_node: str = "",
        parent_node_id: str = "",
        parent_port_id: str = "",
        child_node_id: str = "",
        positions: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        if not playbook_id:
            return "playbook_id is required."

        # Node configuration is documented as a JSON string; validate and
        # normalize it upfront so a malformed value returns a friendly error
        # instead of an opaque GraphQL failure or a silently broken node.
        # In particular, configuration.filters is double-encoded (a FilterGroup
        # serialized as a JSON string INSIDE the configuration JSON) because
        # that is what OpenCTI component schemas declare - a nested object is
        # stored without error but renders as empty filters and never matches.
        filter_warning = ""
        if action in ("add_node", "replace_node", "insert_node"):
            configuration, config_error, filter_warning = (
                _normalize_node_configuration(configuration, component_id)
            )
            if config_error:
                return config_error
            # workflow_id filter values are stored as status template ids, the
            # ids the playbook filter editor lists ("change status" keeps its
            # per-type Status id: it is not a filter).
            configuration, status_note, status_err = await resolve_configuration_filters(configuration, _gql)
            if status_err:
                return status_err
            if status_note:
                filter_warning += " " + status_note
            if component_id and configuration:
                # OpenCTI stores a node configuration without checking it
                # against the component's schema: check it here. A
                # schema that cannot be read never blocks the write - the node
                # was never checked before either - but the result says so.
                # The item shapes the component's runtime reads
                # (``_ARRAY_ITEM_SHAPES``) are checked either way.
                schemas, err = await _component_schemas()
                schema_known = not err and component_id in schemas
                type_error = _configuration_type_error(
                    configuration,
                    schemas[component_id] if schema_known else None,
                    component_id,
                )
                if type_error:
                    return type_error
                if not schema_known:
                    filter_warning += (
                        " NOTE: the configuration was not checked against the "
                        "component's schema ("
                        + ("its schemas could not be read" if err else "the platform does not list this component")
                        + ")."
                    )

        # The graph is read, written and read back under the playbook's lock.
        await lock.acquire()
        held.append(True)

        def _node_input() -> dict[str, Any]:
            return {
                "name": name,
                "component_id": component_id,
                "position": {"x": position_x, "y": position_y},
                "configuration": configuration or None,
                "description": description or None,
            }

        if action == "add_node":
            if not name or not component_id:
                return "name and component_id are required for 'add_node'."
            if bool(parent_node_id) != bool(parent_port_id):
                return (
                    "Error: parent_node_id and parent_port_id go together for "
                    "'add_node' (the parent node and the port of it the new node "
                    "hangs from). Nothing was changed."
                )
            if parent_node_id:
                unknown = await _check_existing(playbook_id, [parent_node_id], [])
                if unknown:
                    return unknown
            query = (
                "mutation AddNode($id: ID!, $input: PlaybookAddNodeInput!) {  playbookAddNode(id: $id, input: $input) }"
            )
            data, err = await _gql(query, {"id": playbook_id, "input": _node_input()})
            if err:
                return err
            created = str(data.get("playbookAddNode") or "")
            if parent_node_id:
                return await _link_new_node(playbook_id, created, parent_node_id, parent_port_id, filter_warning)
            lost, graph = await _read_back(playbook_id, [created], [])
            if lost:
                return lost
            return f"Added node (node_id: {created}).{filter_warning}{_parentless_warning(graph)}"

        if action == "replace_node":
            if not node_id or not name or not component_id:
                return "node_id, name and component_id are required for 'replace_node'."
            unknown = await _check_existing(playbook_id, [node_id], [])
            if unknown:
                return unknown
            query = (
                "mutation ReplaceNode($id: ID!, $nodeId: ID!,"
                " $input: PlaybookAddNodeInput!) {"
                "  playbookReplaceNode(id: $id, nodeId: $nodeId, input: $input) }"
            )
            data, err = await _gql(
                query, {"id": playbook_id, "nodeId": node_id, "input": _node_input()}
            )
            if err:
                return err
            return (
                f"Replaced node (node_id: {data.get('playbookReplaceNode')})."
                f"{filter_warning}"
            )

        if action == "insert_node":
            if not (parent_node_id and parent_port_id and child_node_id):
                return (
                    "parent_node_id, parent_port_id and child_node_id are required "
                    "for 'insert_node'."
                )
            # insert_node creates a NEW node (PlaybookAddNodeInput!) between the
            # parent and child, so name/component_id are required just like add_node.
            if not name or not component_id:
                return "name and component_id are required for 'insert_node'."
            unknown = await _check_existing(playbook_id, [parent_node_id, child_node_id], [])
            if unknown:
                return unknown
            query = (
                "mutation InsertNode($id: ID!, $parentNodeId: ID!, $parentPortId: ID!,"
                " $childNodeId: ID!, $input: PlaybookAddNodeInput!) {"
                "  playbookInsertNode(id: $id, parentNodeId: $parentNodeId,"
                "    parentPortId: $parentPortId, childNodeId: $childNodeId,"
                "    input: $input) { nodeId linkId } }"
            )
            data, err = await _gql(
                query,
                {
                    "id": playbook_id,
                    "parentNodeId": parent_node_id,
                    "parentPortId": parent_port_id,
                    "childNodeId": child_node_id,
                    "input": _node_input(),
                },
            )
            if err:
                return err
            res = data.get("playbookInsertNode") or {}
            lost = await _lost_after_write(
                playbook_id,
                [str(res.get("nodeId") or ""), parent_node_id],
                [str(res.get("linkId") or "")],
            )
            if lost:
                return lost
            return (
                f"Inserted node {res.get('nodeId')} with link {res.get('linkId')}."
                f"{filter_warning}"
            )

        if action == "add_link":
            if not (from_node and from_port and to_node):
                return "from_node, from_port and to_node are required for 'add_link'."
            unknown = await _check_existing(playbook_id, [from_node, to_node], [])
            if unknown:
                return unknown
            query = (
                "mutation AddLink($id: ID!, $input: PlaybookAddLinkInput!) {"
                "  playbookAddLink(id: $id, input: $input) }"
            )
            data, err = await _gql(
                query,
                {
                    "id": playbook_id,
                    "input": {
                        "from_node": from_node,
                        "from_port": from_port,
                        "to_node": to_node,
                    },
                },
            )
            if err:
                return err
            created = data.get("playbookAddLink")
            lost = await _lost_after_write(playbook_id, [from_node, to_node], [str(created or "")])
            if lost:
                return lost
            return f"Added link (link_id: {created})."

        if action == "delete_node":
            if not node_id:
                return "node_id is required for 'delete_node'."
            unknown = await _check_existing(playbook_id, [node_id], [])
            if unknown:
                return unknown
            query = (
                "mutation DeleteNode($id: ID!, $nodeId: ID!) {"
                "  playbookDeleteNode(id: $id, nodeId: $nodeId) { id } }"
            )
            _, err = await _gql(query, {"id": playbook_id, "nodeId": node_id})
            if err:
                return err
            return f"Deleted node {node_id}."

        if action == "delete_link":
            if not link_id:
                return "link_id is required for 'delete_link'."
            unknown = await _check_existing(playbook_id, [], [link_id])
            if unknown:
                return unknown
            query = (
                "mutation DeleteLink($id: ID!, $linkId: ID!) {"
                "  playbookDeleteLink(id: $id, linkId: $linkId) { id } }"
            )
            _, err = await _gql(query, {"id": playbook_id, "linkId": link_id})
            if err:
                return err
            return f"Deleted link {link_id}."

        if action == "update_positions":
            if not positions:
                return f"Error: positions is required for 'update_positions': {_POSITIONS_SHAPE}."
            payload, shape_error = _positions_payload(positions)
            if shape_error:
                return shape_error
            unknown = await _check_existing(playbook_id, [entry["id"] for entry in payload or []], [])
            if unknown:
                return unknown
            positions = json.dumps(payload)
            query = (
                "mutation UpdatePositions($id: ID!, $positions: String!) {"
                "  playbookUpdatePositions(id: $id, positions: $positions) }"
            )
            _, err = await _gql(query, {"id": playbook_id, "positions": positions})
            if err:
                return err
            return f"Updated the positions of {len(payload or [])} node(s) of playbook {playbook_id}."

        if action == "relink":
            if not (link_id and to_node):
                return "link_id and to_node are required for 'relink' (from_node and from_port optional)."
            if bool(from_node) != bool(from_port):
                return "from_node and from_port go together for 'relink': give both or neither."
            nodes = [to_node] + ([from_node] if from_node else [])
            unknown = await _check_existing(playbook_id, nodes, [link_id])
            if unknown:
                return unknown
            query = (
                "mutation Relink($id: ID!, $linkId: ID!, $toNodeId: ID!, $fromNodeId: ID, $fromPortId: ID) {"
                "  playbookRelink(id: $id, linkId: $linkId, toNodeId: $toNodeId,"
                "    fromNodeId: $fromNodeId, fromPortId: $fromPortId) }"
            )
            data, err = await _gql(
                query,
                {
                    "id": playbook_id,
                    "linkId": link_id,
                    "toNodeId": to_node,
                    "fromNodeId": from_node or None,
                    "fromPortId": from_port or None,
                },
            )
            if err:
                return err
            relinked = str(data.get("playbookRelink") or "")
            lost = await _lost_after_write(playbook_id, nodes, [relinked] if relinked else [])
            if lost:
                return lost
            return f"Relinked link {link_id} to node {to_node} (link_id: {relinked or link_id})."

        if action == "move_node":
            if not (node_id and parent_node_id and parent_port_id):
                return "node_id, parent_node_id and parent_port_id are required for 'move_node'."
            unknown = await _check_existing(playbook_id, [node_id, parent_node_id], [])
            if unknown:
                return unknown
            query = (
                "mutation MoveNode($id: ID!, $nodeId: ID!, $parentNodeId: ID!, $parentPortId: ID!) {"
                "  playbookMoveNode(id: $id, nodeId: $nodeId, parentNodeId: $parentNodeId,"
                "    parentPortId: $parentPortId) }"
            )
            data, err = await _gql(
                query,
                {
                    "id": playbook_id,
                    "nodeId": node_id,
                    "parentNodeId": parent_node_id,
                    "parentPortId": parent_port_id,
                },
            )
            if err:
                return err
            link = str(data.get("playbookMoveNode") or "")
            lost = await _lost_after_write(playbook_id, [node_id, parent_node_id], [link] if link else [])
            if lost:
                return lost
            return (
                f"Moved node {node_id} and its branch under node {parent_node_id} "
                f"(port {parent_port_id}, link_id: {link})."
            )

        return (
            f"Unknown action '{action}'. Valid actions: add_node, replace_node, "
            "insert_node, add_link, relink, move_node, delete_node, delete_link, update_positions."
        )

    executor.register("manage_opencti_playbook", manage_opencti_playbook)
    executor.register("edit_opencti_playbook_graph", edit_opencti_playbook_graph)

    return [
        ToolDefinition(
            name="manage_opencti_playbook",
            description=(
                "Manage OpenCTI automation playbooks. Actions: "
                "'list' (search optional), 'get' (playbook_id; answers the whole graph: "
                "every node with id, name, component_id, position and configuration, "
                "every link with id, from node and port, to node), "
                "'create' (name, optional description), 'delete' (playbook_id), "
                "'toggle' (playbook_id + running=true/false to start/stop), "
                "'execute' (playbook_id + entity_id to run a playbook manually on a "
                "STIX entity), 'duplicate' (playbook_id), and 'components' (list the "
                "available playbook component types with their component_id and ports, "
                "needed before adding nodes; with component_id, the configuration "
                "schema of that component). A newly created playbook is empty and "
                "stopped - add nodes with edit_opencti_playbook_graph, then toggle it on."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "delete", "toggle",
                            "execute", "duplicate", "components",
                        ],
                        "description": "The operation to perform.",
                    },
                    "playbook_id": {"type": "string", "description": "Playbook ID (get/delete/toggle/execute/duplicate)."},
                    "name": {"type": "string", "description": "Playbook name (create)."},
                    "description": {"type": "string", "description": "Playbook description (create)."},
                    "running": {"type": "boolean", "description": "Desired running state for 'toggle' (true=start, false=stop)."},
                    "entity_id": {"type": "string", "description": "STIX entity ID to run the playbook on (execute)."},
                    "search": {"type": "string", "description": "Search term (list)."},
                    "limit": {"type": "integer", "description": "Max results for 'list' (default 20, max 200).", "default": 20},
                    "component_id": {"type": "string", "description": "With 'components': the component whose configuration schema to show."},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="edit_opencti_playbook_graph",
            description=(
                "Edit the node/link graph of an OpenCTI playbook. Actions: "
                "'add_node' (name, component_id, position_x/position_y, optional "
                "configuration JSON, parent_node_id + parent_port_id to link it "
                "in the same edit), 'replace_node' (node_id + node fields), "
                "'insert_node' (parent_node_id, parent_port_id, child_node_id + node "
                "fields), 'add_link' (from_node, from_port, to_node), 'delete_node' "
                "(node_id), 'delete_link' (link_id), 'relink' (link_id, to_node, optional "
                "from_node + from_port), 'move_node' (node_id with its branch under "
                "parent_node_id + parent_port_id), 'update_positions' (positions: "
                "{node_id: {x, y}} as a JSON string). A node left without a parent "
                "link makes the playbook unviewable in the OpenCTI editor until it is "
                "linked: give add_node a parent, or use insert_node. Call manage_opencti_playbook "
                "action='components' first to get valid component_id and port values, "
                "and action='get' for the node and link ids. Every action requires "
                "playbook_id. An id the playbook does not hold is refused. Edits on one "
                "playbook run one at a time, so several may be sent in one turn. "
                "IMPORTANT: node filters go INSIDE configuration under the 'filters' "
                "key as a FilterGroup {mode, filters: [{key, values, operator, mode}], "
                "filterGroups} - e.g. configuration='{\"create\": true, \"update\": "
                "false, \"delete\": false, \"filters\": {\"mode\": \"and\", "
                "\"filters\": [{\"key\": [\"entity_type\"], \"values\": [\"Report\"], "
                "\"operator\": \"eq\", \"mode\": \"or\"}], \"filterGroups\": []}}'. "
                "The tool validates the FilterGroup and stores it in the format "
                "OpenCTI expects; invalid or missing filters on filtering components "
                "are rejected instead of silently creating a broken node."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "add_node",
                            "replace_node",
                            "insert_node",
                            "add_link",
                            "relink",
                            "move_node",
                            "delete_node",
                            "delete_link",
                            "update_positions",
                        ],
                        "description": "The graph operation to perform.",
                    },
                    "playbook_id": {"type": "string", "description": "Playbook ID (required for all actions)."},
                    "name": {"type": "string", "description": "Node name (add_node/replace_node/insert_node)."},
                    "component_id": {"type": "string", "description": "Component type id from action='components' (node actions)."},
                    "position_x": {"type": "number", "description": "Node X position on canvas.", "default": 0},
                    "position_y": {"type": "number", "description": "Node Y position on canvas.", "default": 0},
                    "configuration": {"type": "string", "description": "Node configuration as a JSON string (optional). Stream/filter components take a 'filters' key holding a FilterGroup ({mode, filters, filterGroups}); the stream component also takes 'create'/'update'/'delete' booleans. Every property must have the type the component's configuration schema declares (read it with manage_opencti_playbook action='components' and component_id), or nothing is saved."},
                    "description": {"type": "string", "description": "Node description (optional)."},
                    "node_id": {"type": "string", "description": "Target node id (replace_node/delete_node)."},
                    "link_id": {"type": "string", "description": "Target link id (delete_link)."},
                    "from_node": {"type": "string", "description": "Source node id (add_link)."},
                    "from_port": {"type": "string", "description": "Source port id (add_link)."},
                    "to_node": {"type": "string", "description": "Target node id (add_link)."},
                    "parent_node_id": {"type": "string", "description": "Parent node id (insert_node, or add_node to link the new node)."},
                    "parent_port_id": {"type": "string", "description": "Parent port id (insert_node, or add_node with parent_node_id)."},
                    "child_node_id": {"type": "string", "description": "Existing child node id to re-parent (insert_node)."},
                    "positions": {
                        "type": "string",
                        "description": (
                            "update_positions: a JSON object mapping node ids to {x, y}, "
                            'e.g. \'{"NODE_ID": {"x": 100, "y": 200}}\' (the array '
                            '[{"id", "position": {"x", "y"}}] OpenCTI stores is accepted too).'
                        ),
                    },
                },
                "required": ["action", "playbook_id"],
            },
            source=_SOURCE,
        ),
    ]
