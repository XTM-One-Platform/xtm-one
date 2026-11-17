"""Microsoft Purview Data Map - catalog discovery, assets, lineage, glossary.

The Data Map is the Unified Data Governance pillar, exposed through the Apache
Atlas-compatible REST API on ``https://{account}.purview.azure.com`` (audience
``https://purview.azure.net``). These tools answer "where does this data live,
what is it, and how does it flow?":

* ``search_purview_catalog``      - full-text + faceted discovery over every
  scanned asset (the data catalog search box).
* ``get_purview_asset``           - one asset's type, qualified name, schema,
  and classifications by GUID.
* ``get_purview_asset_lineage``   - upstream/downstream data flow for an asset.
* ``list_purview_glossaries`` / ``list_purview_glossary_terms`` /
  ``get_purview_glossary_term`` - the business glossary.
* ``list_purview_collections``    - the account's collection hierarchy.

These require a Purview governance account name on the integration and a
data-plane role on the service principal (Data Curator for the catalog,
Collection Admin for collections).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    ACCOUNT_API_VERSION,
    PurviewClient,
    fmt_list,
    next_skiptoken,
    safe_json,
    trunc,
)

SRC = "integration:purview"
_DATAMAP = "/datamap/api/atlas/v2"
_SEARCH = "/datamap/api/search/query"


def _attr(entity: dict[str, Any], key: str, default: str = "") -> str:
    return str((entity.get("attributes") or {}).get(key, default) or default)


def _classifications(entity: dict[str, Any]) -> str:
    cls = entity.get("classifications") or entity.get("classification") or []
    names = [
        c.get("typeName") if isinstance(c, dict) else str(c) for c in cls if c
    ]
    return ", ".join(n for n in names if n)


def _fmt_search_hit(h: dict[str, Any]) -> str:
    classifications = _classifications(h)
    cls_str = f" | classifications: {classifications}" if classifications else ""
    return (
        f"- {h.get('name', h.get('qualifiedName', '?'))} "
        f"[{h.get('entityType', h.get('objectType', '?'))}]\n"
        f"  guid: {h.get('id', '?')} | {trunc(h.get('qualifiedName'), 140)}{cls_str}"
    )


def _fmt_glossary(g: dict[str, Any]) -> str:
    terms = g.get("terms") or []
    return (
        f"- {g.get('name', 'Glossary')} | guid: {g.get('guid', '?')} | "
        f"{len(terms)} term(s)"
    )


def _fmt_term(t: dict[str, Any]) -> str:
    return (
        f"- {t.get('name', 'Term')} | guid: {t.get('guid', '?')} | "
        f"status: {t.get('status', '?')}\n"
        f"  {trunc(t.get('shortDescription') or t.get('longDescription'), 160)}"
    )


def _fmt_collection(c: dict[str, Any]) -> str:
    parent = (c.get("parentCollection") or {}).get("referenceName") or "-"
    return (
        f"- {c.get('friendlyName', c.get('name', '?'))} | name: {c.get('name', '?')} | "
        f"parent: {parent}"
    )


async def _map_json_list(
    client: PurviewClient, path: str, action: str, **kwargs: Any
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """GET an Atlas endpoint that returns a bare JSON array (not ``{value: []}``)."""
    resp, err = await client.map_request("get", path, action, **kwargs)
    if err:
        return None, err
    try:
        data = resp.json()  # type: ignore[union-attr]
    except (ValueError, AttributeError):
        return [], None
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        return data.get("value", []) or [], None
    return [], None


def register(
    client: PurviewClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def search_purview_catalog(
        keywords: str = "*",
        entity_type: str = "",
        object_type: str = "",
        limit: int = 25,
        continuation_token: str = "",
    ) -> str:
        body: dict[str, Any] = {
            "keywords": keywords or "*",
            "limit": max(1, min(limit, 100)),
        }
        and_clauses: list[dict[str, Any]] = []
        if entity_type:
            and_clauses.append({"entityType": entity_type})
        if object_type:
            and_clauses.append({"objectType": object_type})
        if and_clauses:
            body["filter"] = {"and": and_clauses}
        if continuation_token:
            body["continuationToken"] = continuation_token
        resp, err = await client.map_request(
            "post", _SEARCH, "search the data catalog", json=body
        )
        if err:
            return err
        data = safe_json(resp)
        hits = data.get("value", []) or []
        out = fmt_list(
            hits,
            _fmt_search_hit,
            noun="asset",
            total=data.get("@search.count"),
            empty_msg="No catalog assets match this search.",
        )
        token = data.get("continuationToken")
        if token:
            out += (
                f'\n[More results available - pass continuation_token="{token}"]'
            )
        return out

    async def get_purview_asset(guid: str) -> str:
        data, err = await client.map_get_json(
            f"{_DATAMAP}/entity/guid/{quote(guid.strip(), safe='')}",
            "get data asset",
        )
        if err:
            return err
        entity = (data or {}).get("entity") or {}
        if not entity:
            return f"No asset found for guid {guid}."
        classifications = _classifications(entity)
        lines = [
            f"Asset: {_attr(entity, 'name') or '?'} [{entity.get('typeName', '?')}]",
            f"guid: {entity.get('guid', '?')} | status: {entity.get('status', '?')}",
            f"qualifiedName: {_attr(entity, 'qualifiedName') or '-'}",
        ]
        description = _attr(entity, "description")
        if description:
            lines.append(f"description: {trunc(description, 240)}")
        if classifications:
            lines.append(f"classifications: {classifications}")
        return "\n".join(lines)

    async def get_purview_asset_lineage(
        guid: str, direction: str = "BOTH", depth: int = 3
    ) -> str:
        direction = (direction or "BOTH").upper()
        if direction not in ("INPUT", "OUTPUT", "BOTH"):
            return "Error: direction must be one of INPUT, OUTPUT, BOTH."
        data, err = await client.map_get_json(
            f"{_DATAMAP}/lineage/{quote(guid.strip(), safe='')}",
            "get asset lineage",
            params={"direction": direction, "depth": max(1, min(depth, 10))},
        )
        if err:
            return err
        data = data or {}
        relations = data.get("relations", []) or []
        entity_map = data.get("guidEntityMap", {}) or {}

        def _name(g: str) -> str:
            ent = entity_map.get(g) or {}
            return (ent.get("attributes") or {}).get("name") or g

        if not relations:
            return f"No lineage relations found for asset {guid} (direction {direction})."
        lines = [
            f"Lineage for {_name(data.get('baseEntityGuid', guid))} "
            f"(direction {direction}, {len(relations)} relation(s)):"
        ]
        for rel in relations[:40]:
            lines.append(
                f"- {_name(rel.get('fromEntityId', '?'))} -> "
                f"{_name(rel.get('toEntityId', '?'))}"
            )
        if len(relations) > 40:
            lines.append(f"  ... and {len(relations) - 40} more relations")
        return "\n".join(lines)

    async def list_purview_glossaries() -> str:
        items, err = await _map_json_list(
            client, f"{_DATAMAP}/glossary", "list glossaries"
        )
        if err:
            return err
        return fmt_list(
            items or [],
            _fmt_glossary,
            noun="glossary",
            empty_msg="No business glossaries found in this Purview account.",
        )

    async def list_purview_glossary_terms(
        glossary_guid: str, limit: int = 50
    ) -> str:
        items, err = await _map_json_list(
            client,
            f"{_DATAMAP}/glossary/{quote(glossary_guid.strip(), safe='')}/terms",
            "list glossary terms",
            params={"limit": max(1, min(limit, 100))},
        )
        if err:
            return err
        return fmt_list(
            items or [],
            _fmt_term,
            noun="glossary term",
            empty_msg="This glossary has no terms.",
        )

    async def get_purview_glossary_term(term_guid: str) -> str:
        data, err = await client.map_get_json(
            f"{_DATAMAP}/glossary/term/{quote(term_guid.strip(), safe='')}",
            "get glossary term",
        )
        if err:
            return err
        term = data or {}
        lines = [
            f"Term: {term.get('name', '?')} | guid: {term.get('guid', '?')} | "
            f"status: {term.get('status', '?')}",
        ]
        desc = term.get("longDescription") or term.get("shortDescription")
        if desc:
            lines.append(trunc(desc, 400))
        return "\n".join(lines)

    async def list_purview_collections(skiptoken: str = "") -> str:
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skipToken"] = skiptoken
        data, err = await client.map_get_json(
            "/account/collections",
            "list collections",
            api_version=ACCOUNT_API_VERSION,
            params=params,
        )
        if err:
            return err
        data = data or {}
        return fmt_list(
            data.get("value", []) or [],
            _fmt_collection,
            noun="collection",
            skiptoken=next_skiptoken(data),
            empty_msg="No collections found in this Purview account.",
        )

    handlers = {
        "search_purview_catalog": search_purview_catalog,
        "get_purview_asset": get_purview_asset,
        "get_purview_asset_lineage": get_purview_asset_lineage,
        "list_purview_glossaries": list_purview_glossaries,
        "list_purview_glossary_terms": list_purview_glossary_terms,
        "get_purview_glossary_term": get_purview_glossary_term,
        "list_purview_collections": list_purview_collections,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return [
        ToolDefinition(
            name="search_purview_catalog",
            description=(
                "Search the Microsoft Purview Data Map (data catalog) for scanned "
                "assets - tables, files, columns, reports, and more - across every "
                "registered data source. Use keywords (default '*' for everything) "
                "and optionally narrow by entity_type (e.g. 'azure_sql_table') or "
                "object_type (e.g. 'Tables', 'Files'). Returns each asset's name, "
                "type, qualifiedName, GUID (use with get_purview_asset / "
                "get_purview_asset_lineage), and classifications."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": "Search text applied to all searchable fields. '*' matches everything.",
                        "default": "*",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "Filter by Atlas entity type, e.g. 'azure_sql_table'.",
                    },
                    "object_type": {
                        "type": "string",
                        "description": "Filter by object type facet, e.g. 'Tables' or 'Files'.",
                    },
                    "limit": {"type": "integer", "default": 25},
                    "continuation_token": {
                        "type": "string",
                        "description": "Pagination token from a previous search's 'More results' hint.",
                    },
                },
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_asset",
            description=(
                "Get one data asset from the Purview Data Map by its GUID, "
                "including its type, qualified name, description, and applied "
                "classifications. Find GUIDs with search_purview_catalog."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "guid": {"type": "string", "description": "The asset GUID."}
                },
                "required": ["guid"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_asset_lineage",
            description=(
                "Trace the data lineage of an asset in the Purview Data Map - which "
                "upstream sources feed it and which downstream assets it feeds. "
                "direction is INPUT (upstream), OUTPUT (downstream), or BOTH "
                "(default); depth controls how many hops (default 3). Returns the "
                "from -> to relationships by asset name. Use to answer impact / "
                "root-cause questions about data flow."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "guid": {"type": "string", "description": "The asset GUID."},
                    "direction": {
                        "type": "string",
                        "enum": ["INPUT", "OUTPUT", "BOTH"],
                        "default": "BOTH",
                    },
                    "depth": {"type": "integer", "default": 3},
                },
                "required": ["guid"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="list_purview_glossaries",
            description=(
                "List the business glossaries in the Purview account, each with its "
                "GUID and term count. Use the GUID with list_purview_glossary_terms "
                "to browse the terms."
            ),
            input_schema={"type": "object", "properties": {}},
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="list_purview_glossary_terms",
            description=(
                "List the business terms in a glossary (by glossary_guid from "
                "list_purview_glossaries). Returns each term's name, GUID, status, "
                "and short description - the controlled vocabulary the organization "
                "uses to describe its data."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "glossary_guid": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["glossary_guid"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_glossary_term",
            description=(
                "Get one business glossary term by its GUID, including its full "
                "long description and status. Find term GUIDs with "
                "list_purview_glossary_terms."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "term_guid": {"type": "string", "description": "The glossary term GUID."}
                },
                "required": ["term_guid"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="list_purview_collections",
            description=(
                "List the collections in the Purview account - the hierarchical "
                "containers used to organize data sources and scope access. Returns "
                "each collection's name, friendly name, and parent. Requires the "
                "Collection Admin data-plane role."
            ),
            input_schema={
                "type": "object",
                "properties": {"skiptoken": {"type": "string"}},
            },
            source=SRC,
            priority=0,
        ),
    ]
