# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""What a platform source editor needs to know about OpenCTI and OpenAEV, read as the editor.

The Data sources panel builds an OpenCTI or OpenAEV widget the way each
product's own widget stepper does, and needs the same answers: which entity
types and filter keys exist, which attributes a breakdown may use, which
values a filter may take, and the names of the values a stored widget holds.
Each is one :func:`read_schema` call, ``kind`` naming the question:

========================  =====================================================
``entity_types``          OpenCTI ``subTypes`` (entities: SDO + SCO; relationships:
                          core relationships + sightings); OpenAEV the engine's
                          base entities; OpenCRQ its metric catalog.
``filter_keys``           OpenCTI ``filterKeysSchema`` for the perspective (the
                          keys its widget editor offers); OpenAEV
                          ``/api/engine/schemas`` for the entities.
``attributes``            OpenCTI ``schemaAttributeNames`` for the types, with
                          the common ones; OpenAEV the entities' properties.
``values``                values of one filter key matching ``search``.
``labels``                the display names of stored values (``ids``).
``entities``              entities of ``types`` matching ``search``, or named by
                          their ``ids``: what a dashboard's entity input offers
                          (an OpenCTI custom view's "current entity").
========================  =====================================================

The platform lists entity types, filter keys, attributes and saved filters
whole: a ``search`` narrows them here, on the key and its label.

The same rules as reading a widget: the person's own access to the
integration (``platform_access.resolve_platform_client``, no admin bypass),
the per-person rate and concurrency limits and the bounded transport of
``dashboard_live``; nothing typed is ever spliced into a query (GraphQL
variables, URL parameters); every answer is cut to closed lists of short
strings; a failure is a fixed code. Answers are cached a minute per person.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dashboard_live import LiveError, text
from app.services.dashboard_live._http import request_json
from app.services.dashboard_live.opencti_entities import entity_label

logger = logging.getLogger(__name__)

KINDS = ("entity_types", "filter_keys", "attributes", "values", "labels", "entities", "saved_filters")
# The kinds a platform answers whole, whatever ``search`` says.
_LISTED_WHOLE = ("entity_types", "filter_keys", "attributes", "saved_filters")
MAX_ITEMS = 500
MAX_VALUES = 100

# ── OpenCTI ──────────────────────────────────────────────────────────────────

_CTI_TYPES = """query XtmOneSchemaTypes {
  sdos: subTypes(type: "Stix-Domain-Object") { edges { node { id label } } }
  scos: subTypes(type: "Stix-Cyber-Observable") { edges { node { id label } } }
  scrs: subTypes(type: "stix-core-relationship") { edges { node { id label } } }
}"""

_CTI_FILTER_KEYS = """query XtmOneSchemaFilterKeys {
  filterKeysSchema {
    entity_type
    filters_schema {
      filterKey
      filterDefinition { filterKey label type multiple subEntityTypes elementsForFilterValuesSearch }
    }
  }
}"""

_CTI_ATTRIBUTES = """query XtmOneSchemaAttributes($elementType: [String]!) {
  schemaAttributeNames(elementType: $elementType) { edges { node { value } } }
}"""

_CTI_SEARCH = {
    "core": """query XtmOneSchemaObjects($search: String, $types: [String]) {
  stixCoreObjects(search: $search, types: $types, first: 25) { edges { node { id entity_type representative { main } } } }
}""",
    "vocabulary": """query XtmOneSchemaVocabularies($search: String, $filters: FilterGroup) {
  vocabularies(search: $search, filters: $filters, first: 50) { edges { node { id name } } }
}""",
    "labels": """query XtmOneSchemaLabels($search: String) {
  labels(search: $search, first: 50) { edges { node { id value } } }
}""",
    "markings": """query XtmOneSchemaMarkings { me { allowed_marking { id definition } } }""",
    "kill_chain": """query XtmOneSchemaKillChain($search: String) {
  killChainPhases(search: $search, first: 50) { edges { node { id kill_chain_name phase_name } } }
}""",
    "creators": """query XtmOneSchemaCreators($entityTypes: [String!]) { creators(entityTypes: $entityTypes) { edges { node { id name } } } }""",
    "identities": """query XtmOneSchemaIdentities($search: String, $types: [String]) {
  identities(search: $search, types: $types, first: 25) { edges { node { id name entity_type } } }
}""",
}

_CTI_ENTITIES = """query XtmOneSchemaEntities($search: String, $types: [String], $filters: FilterGroup) {
  stixCoreObjects(search: $search, types: $types, filters: $filters, first: 25) { edges { node { id entity_type representative { main } } } }
}"""

_CTI_REPRESENTATIVES = """query XtmOneSchemaRepresentatives($filters: FilterGroup!) {
  filtersRepresentatives(filters: $filters) { id value entity_type }
}"""

#: The schema entries whose keys the widget editor offers, per perspective (OpenCTI ``WidgetFilters``).
_CTI_PERSPECTIVE_TYPES = {
    "entities": ("Stix-Core-Object",),
    "relationships": ("stix-core-relationship", "stix-sighting-relationship", "contains", "object-label"),
    "audits": ("History",),
}
#: Attributes a breakdown may always use (OpenCTI ``ENTITIES_WIDGET_COMMON_ATTRIBUTES``), and a relationship's.
_CTI_COMMON_ATTRIBUTES = (
    "created-by.internal_id", "object-label.internal_id", "object-assignee.internal_id",
    "object-marking.internal_id", "kill-chain-phase.internal_id", "x_opencti_workflow_id",
)  # fmt: skip
_CTI_RELATIONSHIP_ATTRIBUTES = (
    "internal_id", "entity_type", "relationship_type", "created-by.internal_id",
    "object-marking.internal_id", "kill-chain-phase.internal_id", "creator_id", "x_opencti_workflow_id",
)  # fmt: skip
_CTI_ENUM_TYPES = ("boolean",)


async def _cti(client, query: str, variables: dict | None = None) -> dict:
    from app.services.dashboard_live.opencti import _is_auth_error

    answer = await request_json(
        "POST", f"{client.base_url}/graphql", client.headers, json_body={"query": query, "variables": variables or {}}, what="OpenCTI"
    )
    if not isinstance(answer, dict):
        raise LiveError("upstream_error")
    if answer.get("errors"):
        if _is_auth_error(answer["errors"]):
            raise LiveError("access_denied")
        logger.info("Dashboard schema: OpenCTI answered errors %r", str(answer["errors"])[:200])
        raise LiveError("upstream_error")
    data = answer.get("data")
    if not isinstance(data, dict):
        raise LiveError("upstream_error")
    return data


def _edges(value: object) -> list[dict]:
    edges = value.get("edges") if isinstance(value, dict) else None
    return [e["node"] for e in edges or [] if isinstance(e, dict) and isinstance(e.get("node"), dict)]


def _option(value: object, label: object = None, group: object = None) -> dict | None:
    key = text(value, 200)
    if not key:
        return None
    option = {"value": key, "label": text(label, 200) or key}
    if group:
        option["group"] = text(group, 60)
    return option


def _options(items, limit: int = MAX_ITEMS) -> list[dict]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item and item["value"] not in seen:
            seen.add(item["value"])
            out.append(item)
        if len(out) >= limit:
            break
    return out


async def _cti_filter_keys(client, perspective: str) -> dict[str, dict]:
    data = await _cti(client, _CTI_FILTER_KEYS)
    wanted = set(_CTI_PERSPECTIVE_TYPES.get(perspective, ()))
    keys: dict[str, dict] = {}
    for entry in data.get("filterKeysSchema") or []:
        if not isinstance(entry, dict) or entry.get("entity_type") not in wanted:
            continue
        for item in entry.get("filters_schema") or []:
            definition = item.get("filterDefinition") if isinstance(item, dict) else None
            if not isinstance(definition, dict) or not text(definition.get("filterKey"), 100):
                continue
            key = text(definition["filterKey"], 100)
            known = keys.setdefault(
                key,
                {
                    "key": key,
                    "label": text(definition.get("label"), 120) or key,
                    "type": text(definition.get("type"), 20),
                    "multiple": bool(definition.get("multiple")),
                    "search": [],
                },
            )
            for element in definition.get("elementsForFilterValuesSearch") or []:
                value = text(element, 100)
                if value and value not in known["search"] and len(known["search"]) < 50:
                    known["search"].append(value)
    if perspective == "entities":
        keys.setdefault("entity_type", {"key": "entity_type", "label": "Entity type", "type": "string", "multiple": True, "search": []})
    elif perspective == "relationships":
        keys.pop("entity_type", None)
    return dict(sorted(keys.items())[:MAX_ITEMS])


async def _cti_values(client, perspective: str, key: str, search: str) -> list[dict]:
    # ``regardingOf`` pairs an entity (``id``) with relationship types: each sub-key is searched on its own.
    if key == "regardingOf.id":
        data = await _cti(client, _CTI_SEARCH["core"], {"search": search or None, "types": ["Stix-Core-Object"]})
        return _options(
            _option(n.get("id"), (n.get("representative") or {}).get("main"), n.get("entity_type")) for n in _edges(data.get("stixCoreObjects"))
        )
    if key == "regardingOf.relationship_type":
        key = "relationship_type"
    if key in ("entity_type", "fromTypes", "toTypes", "relationship_type"):
        data = await _cti(client, _CTI_TYPES)
        kinds = {"relationship_type": ("scrs",), "entity_type": ("sdos", "scos") if perspective != "relationships" else ("scrs",)}
        groups = kinds.get(key, ("sdos", "scos", "scrs"))
        # Shown by OpenCTI's names of the types (``entity_<type>``, ``relationship_<type>``), as its own picker.
        found = [_option(n.get("id"), entity_label(n.get("label")), group) for group in groups for n in _edges(data.get(group))]
        if key == "relationship_type":
            found.append(_option("stix-sighting-relationship", "Sighting"))
        return _options(o for o in found if o and search.lower() in o["label"].lower())[:MAX_VALUES]
    definition = (await _cti_filter_keys(client, perspective)).get(key)
    if definition is None:
        raise LiveError("unsupported")
    kind, elements = definition["type"], definition["search"]
    if kind == "boolean":
        return [{"value": "true", "label": "true"}, {"value": "false", "label": "false"}]
    if kind == "enum":
        return _options(_option(e) for e in elements if search.lower() in e.lower())[:MAX_VALUES]
    if kind == "vocabulary" and elements:
        filters = {"mode": "or", "filters": [{"key": "category", "values": elements[:1], "operator": "eq", "mode": "or"}], "filterGroups": []}
        data = await _cti(client, _CTI_SEARCH["vocabulary"], {"search": search or None, "filters": filters})
        return _options(_option(n.get("name")) for n in _edges(data.get("vocabularies")))[:MAX_VALUES]
    if kind != "id":
        raise LiveError("unsupported")
    if key == "objectLabel":
        data = await _cti(client, _CTI_SEARCH["labels"], {"search": search or None})
        return _options(_option(n.get("id"), n.get("value")) for n in _edges(data.get("labels")))
    if key == "objectMarking":
        data = await _cti(client, _CTI_SEARCH["markings"])
        markings = ((data.get("me") or {}).get("allowed_marking")) or []
        return _options(
            _option(m.get("id"), m.get("definition")) for m in markings if isinstance(m, dict) and search.lower() in str(m.get("definition") or "").lower()
        )[:MAX_VALUES]
    if key == "killChainPhases":
        data = await _cti(client, _CTI_SEARCH["kill_chain"], {"search": search or None})
        return _options(
            _option(n.get("id"), f"{n.get('kill_chain_name')}: {n.get('phase_name')}") for n in _edges(data.get("killChainPhases"))
        )
    if key == "creator_id":
        data = await _cti(client, _CTI_SEARCH["creators"], {"entityTypes": []})
        return _options(_option(n.get("id"), n.get("name")) for n in _edges(data.get("creators")) if search.lower() in str(n.get("name") or "").lower())[:MAX_VALUES]
    if key == "createdBy":
        data = await _cti(client, _CTI_SEARCH["identities"], {"search": search or None, "types": elements or None})
        return _options(_option(n.get("id"), n.get("name"), n.get("entity_type")) for n in _edges(data.get("identities")))
    data = await _cti(client, _CTI_SEARCH["core"], {"search": search or None, "types": elements or ["Stix-Core-Object"]})
    return _options(
        _option(n.get("id"), (n.get("representative") or {}).get("main"), n.get("entity_type")) for n in _edges(data.get("stixCoreObjects"))
    )


_CTI_SAVED_FILTERS = """query XtmOneSchemaSavedFilters {
  savedFilters(first: 100, orderBy: name, orderMode: asc) { edges { node { id name scope } } }
}"""


def _saved_filter_fits(scope: str, perspective: str) -> bool:
    """Whether a saved filter of *scope* filters a widget of *perspective* (OpenCTI's ``useBuildSavedFiltersOptions``)."""
    if perspective == "relationships":
        return scope == "relationships"
    if perspective == "audits":
        return "audit" in scope
    return "audit" not in scope and scope != "relationships"


async def _opencti(client, kind: str, params: dict) -> list[dict]:
    perspective = params.get("perspective") or "entities"
    if kind == "saved_filters":
        # The person's own and those shared with them, as OpenCTI lists them; only those of the widget's perspective.
        data = await _cti(client, _CTI_SAVED_FILTERS)
        return _options(
            _option(n.get("id"), n.get("name")) for n in _edges(data.get("savedFilters")) if _saved_filter_fits(str(n.get("scope") or ""), perspective)
        )
    if kind == "entity_types":
        data = await _cti(client, _CTI_TYPES)
        groups = ("scrs",) if perspective == "relationships" else ("sdos", "scos")
        # Shown by OpenCTI's names of the types (``entity_<type>``, ``relationship_<type>``), as its own picker.
        found = [_option(n.get("id"), entity_label(n.get("label")), group) for group in groups for n in _edges(data.get(group))]
        if perspective == "relationships":
            found.append(_option("stix-sighting-relationship", "Sighting", "scrs"))
        return _options(found)
    if kind == "filter_keys":
        return list((await _cti_filter_keys(client, perspective)).values())
    if kind == "attributes":
        if perspective == "relationships":
            return _options(_option(a) for a in _CTI_RELATIONSHIP_ATTRIBUTES)
        types = params.get("types") or []
        names: list[str] = ["entity_type"]
        if types:
            data = await _cti(client, _CTI_ATTRIBUTES, {"elementType": types})
            names += [str(n.get("value")) for n in _edges(data.get("schemaAttributeNames"))]
        # The editor's own rule: no internal ``i_`` attribute; hashes by algorithm.
        expanded = []
        for name in names:
            if name.startswith("i_"):
                continue
            expanded += [f"hashes.{h}" for h in ("MD5", "SHA-1", "SHA-256", "SHA-512")] if name == "hashes" else [name]
        return _options(_option(a) for a in [*sorted(set(expanded)), *_CTI_COMMON_ATTRIBUTES])
    if kind == "values":
        return await _cti_values(client, perspective, params["key"], params.get("search") or "")
    if kind == "entities":
        ids = params.get("ids") or []
        variables = {"types": params.get("types") or ["Stix-Core-Object"], "search": None if ids else (params.get("search") or None)}
        if ids:
            variables["filters"] = {"mode": "and", "filters": [{"key": ["id"], "values": ids, "operator": "eq", "mode": "or"}], "filterGroups": []}
        data = await _cti(client, _CTI_ENTITIES, variables)
        return _options(
            _option(n.get("id"), (n.get("representative") or {}).get("main"), n.get("entity_type")) for n in _edges(data.get("stixCoreObjects"))
        )
    # labels: the stored values of one key, named.
    if params["key"] == "regardingOf.id":
        # OpenCTI names the ids of a regardingOf filter (its nested ``id`` values) like any other.
        values: list = [{"key": "id", "values": params["ids"]}]
        filters = {"mode": "and", "filters": [{"key": ["regardingOf"], "values": values, "operator": "eq", "mode": "or"}], "filterGroups": []}
    else:
        filters = {"mode": "and", "filters": [{"key": [params["key"]], "values": params["ids"], "operator": "eq", "mode": "or"}], "filterGroups": []}
    data = await _cti(client, _CTI_REPRESENTATIVES, {"filters": filters})
    return _options(_option(r.get("id"), r.get("value"), r.get("entity_type")) for r in data.get("filtersRepresentatives") or [] if isinstance(r, dict))


# ── OpenAEV ──────────────────────────────────────────────────────────────────

#: The engine entities its widget editor offers (``FilterFieldBaseEntity``).
AEV_ENTITIES = ("expectation-inject", "finding", "asset", "vulnerable-endpoint", "inject", "scenario", "simulation")
#: Where each filter key's values are searched (``useSearchOptions``): ``(path, extra query parameters)``.
AEV_VALUE_ENDPOINTS: dict[str, tuple[str, dict[str, str]]] = {
    "base_simulation_side": ("simulations", {}),
    "base_scenario_side": ("scenarios", {}),
    "base_tags_side": ("tags", {}),
    "base_assets_side": ("endpoints", {"inputFilterOption": "ALL_INJECTS"}),
    "base_endpoint_side": ("endpoints", {"inputFilterOption": "ALL_INJECTS"}),
    "base_asset_groups_side": ("asset_groups", {"inputFilterOption": "ALL_INJECTS"}),
    "base_teams_side": ("teams", {"inputFilterOption": "ALL_INJECTS"}),
    "base_security_platforms_side": ("security_platforms", {}),
    "base_attack_patterns_side": ("attack_patterns", {}),
    "base_security_domains_side": ("domains", {}),
}


def _aev_prefix(client) -> str:
    return f"{client.base_url}/api/tenants/{quote(client.tenant_id, safe='')}" if client.tenant_id else f"{client.base_url}/api"


async def _aev_properties(client, entities: list[str]) -> list[dict]:
    params = [("classNames", e) for e in entities if e in AEV_ENTITIES]
    answer = await request_json(
        "GET", f"{_aev_prefix(client)}/engine/schemas", client.headers, params=params or None, what="OpenAEV"  # type: ignore[arg-type]
    )
    out = []
    for item in answer if isinstance(answer, list) else []:
        if not isinstance(item, dict) or not text(item.get("schema_property_name"), 100):
            continue
        values = [text(v, 100) for v in item.get("schema_property_values") or [] if text(v, 100)][:100]
        overrides = [text(o, 40) for o in item.get("schema_property_override_operators") or [] if text(o, 40)][:20]
        out.append(
            {
                "key": text(item["schema_property_name"], 100),
                "label": text(item.get("schema_property_label"), 120) or text(item["schema_property_name"], 100),
                "type": text(item.get("schema_property_type"), 40),
                "entity": text(item.get("schema_property_entity"), 60),
                "multiple": bool(item.get("schema_property_type_array")),
                "values": values,
                # Values searched in OpenAEV (``useSearchOptions``), and the operators it imposes on the key.
                "dynamic": bool(item.get("schema_property_has_dynamic_value")),
                "operators": overrides,
            }
        )
        if len(out) >= MAX_ITEMS:
            break
    return sorted(out, key=lambda p: (p["entity"], p["key"]))


#: The OpenAEV entities an entity input picks - a dashboard parameter's - and where they are searched.
AEV_PICKED_ENTITIES: dict[str, str] = {"simulation": "base_simulation_side", "scenario": "base_scenario_side"}


async def _openaev(client, kind: str, params: dict) -> list[dict]:
    if kind == "entities":
        # A simulation or a scenario, as an OpenAEV dashboard parameter offers them (``/<type>s/options``).
        types = [t for t in params.get("types") or [] if t in AEV_PICKED_ENTITIES]
        if len(types) != 1:
            raise LiveError("unsupported")
        params = {**params, "key": AEV_PICKED_ENTITIES[types[0]]}
        kind = "labels" if params.get("ids") else "values"
    if kind == "entity_types":
        return [{"value": e, "label": e} for e in AEV_ENTITIES]
    if kind in ("filter_keys", "attributes"):
        return await _aev_properties(client, params.get("types") or [])
    key = params["key"]
    if key == "base_entity":
        return [{"value": e, "label": e} for e in AEV_ENTITIES]
    if key not in AEV_VALUE_ENDPOINTS:
        raise LiveError("unsupported")
    path, extra = AEV_VALUE_ENDPOINTS[key]
    url = f"{_aev_prefix(client)}/{path}/options"
    if kind == "values":
        answer = await request_json("GET", url, client.headers, params={**extra, "searchText": params.get("search") or ""}, what="OpenAEV")
    else:
        answer = await request_json("POST", url, client.headers, json_body=params["ids"], what="OpenAEV")
    return _options(_option(o.get("id"), o.get("label")) for o in (answer if isinstance(answer, list) else []) if isinstance(o, dict))


# ── OpenCRQ ──────────────────────────────────────────────────────────────────


async def _opencrq(client, kind: str, params: dict) -> list[dict]:
    """OpenCRQ widgets are metrics of a closed catalog (``dashboard_platform.opencrq.METRICS``): nothing to ask it."""
    from app.services.dashboard_platform.opencrq import METRICS

    if kind != "entity_types":
        raise LiveError("unsupported")
    return [{"value": name, "label": metric.label, "group": metric.shape, "window": metric.window} for name, metric in METRICS.items()]


# ── Entry point ──────────────────────────────────────────────────────────────


def _cache_key(user_id: uuid.UUID, integration_id: uuid.UUID, kind: str, params: dict) -> str:
    digest = hashlib.sha256(json.dumps([kind, params], sort_keys=True, default=str).encode()).hexdigest()[:32]
    return f"dashboard-schema:{user_id}:{integration_id}:{digest}"


async def read_schema(
    db: AsyncSession, user_id: uuid.UUID, integration_id: uuid.UUID, source: str, kind: str, params: dict
) -> dict:
    """``{"status": "ok", "items": [...]}`` or ``{"status": "error", "code"}`` (see the module docstring)."""
    from app.core.egress import egress_route
    from app.services.dashboard_live import _cache_get, _cache_set, _enforce_rate, _Slot
    from app.services.platform_access import PlatformAccessError, resolve_platform_client

    try:
        client = await resolve_platform_client(db, user_id, integration_id, source)
    except PlatformAccessError as exc:
        return {"status": "error", "code": exc.code}
    key = _cache_key(user_id, integration_id, kind, params)
    cached = await _cache_get(key)
    if cached is not None:
        return cached
    reader = {"opencti": _opencti, "openaev": _openaev, "opencrq": _opencrq}[source]
    try:
        await _enforce_rate(user_id)
        async with _Slot(str(user_id)):
            with egress_route(client.route):
                items = await asyncio.wait_for(reader(client, kind, params), timeout=20)
    except LiveError as exc:
        return {"status": "error", "code": exc.code}
    except TimeoutError:
        return {"status": "error", "code": "timeout"}
    except Exception as exc:
        logger.warning("Dashboard schema failed (%s %s %s): %r", source, integration_id, kind, str(exc)[:200])
        return {"status": "error", "code": "upstream_error"}
    if kind in _LISTED_WHOLE and params.get("search"):
        # The platform lists these whole: ``search`` narrows them here, on the key and its label.
        needle = str(params["search"]).lower()
        items = [i for i in items if any(needle in str(i.get(f) or "").lower() for f in ("value", "key", "label"))]
    result: dict[str, Any] = {"status": "ok", "items": items}
    await _cache_set(key, result)
    return result


async def entity_input_names(db: AsyncSession, user_id: uuid.UUID, definition: dict) -> dict[str, str]:
    """The names of the entities the dashboard's entity inputs start on (``dashboard_tokens.starting_tokens``),
    by id, read as *user_id* reads them: what a page without a session (an export's) shows in place of the ids.

    An input its integration or its platform does not name keeps its id: nothing here fails the page.
    """
    from app.services.dashboard_tokens import starting_tokens

    try:
        tokens = starting_tokens(definition)
    except ValueError:
        return {}
    names: dict[str, str] = {}
    for entry in (definition.get("inputs") or {}).values():
        if not isinstance(entry, dict) or entry.get("type") != "input.entity":
            continue
        options = entry.get("options") or {}
        value = tokens.get(str(entry.get("token")))
        ids = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        integration = _uuid(options.get("integration_id"))
        source = options.get("source")
        if not ids or integration is None or source not in ("opencti", "openaev", "opencrq"):
            continue
        params = {"types": [str(t) for t in options.get("entityTypes") or []][:10] or None, "ids": [str(i) for i in ids][:50]}
        result = await read_schema(db, user_id, integration, source, "entities", {k: v for k, v in params.items() if v})
        for item in result.get("items") or [] if result.get("status") == "ok" else []:
            if isinstance(item, dict) and item.get("value") in ids and item.get("label"):
                names[str(item["value"])] = str(item["label"])
    return names


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


__all__ = ["AEV_ENTITIES", "AEV_VALUE_ENDPOINTS", "KINDS", "entity_input_names", "read_schema"]
