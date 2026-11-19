"""OpenCTI data-sharing tools - live streams, public CSV feeds, TAXII collections.

Single consolidated ``manage_opencti_data_sharing`` tool over
``src/modules/dataSharing/*``. share_type selects the family:

* ``live_stream``      - StreamCollection (live STIX event stream)
* ``feed``             - public CSV feed (full-replace edits)
* ``taxii_collection`` - TAXII 2.1 collection export

``filters`` / ``origin_filters`` are JSON STRINGS (serialized FilterGroup).
Requires TAXIIAPI_SETCOLLECTIONS for writes. Not draft-gated.
"""

from __future__ import annotations

import json
import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql
from .access import _refuse_unapplied
from .workflow import resolve_filters_json, with_status_notes

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"

# The fields each action reads, for every share_type; any other field passed (an
# argument the tool does not declare included) is refused by name and nothing is sent.
# A field left at its default (limit, enabled) counts as not passed.
_DATA_SHARING_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"share_id"}),
    "create": frozenset({"name", "description", "filters", "config"}),
    "update": frozenset({"share_id", "name", "description", "filters", "config"}),
    "toggle": frozenset({"share_id", "enabled"}),
    "delete": frozenset({"share_id"}),
}


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def _fetch_feed_input(feed_id: str) -> tuple[dict | None, str | None]:
        query = (
            "query Feed($id: String!) {"
            "  feed(id: $id) { name description filters separator rolling_time"
            "    feed_date_attribute include_header feed_types feed_public"
            "    feed_attributes { attribute multi_match_strategy multi_match_separator"
            "      mappings { type attribute relationship_type target_entity_type } } } }"
        )
        data, err = await _gql(query, {"id": feed_id})
        if err:
            return None, err
        f = data.get("feed")
        if not f:
            return None, f"Feed {feed_id} not found."
        return f, None

    async def manage_opencti_data_sharing(
        action: str,
        share_type: str = "",
        share_id: str = "",
        name: str = "",
        description: str = "",
        filters: str = "",
        enabled: bool = True,
        config: str = "",
        search: str = "",
        limit: int = 50,
        **unknown: object,
    ) -> str:
        action = (action or "").strip().lower()
        share_type = (share_type or "").strip().lower()
        if share_type not in ("live_stream", "feed", "taxii_collection"):
            return "share_type must be 'live_stream', 'feed', or 'taxii_collection'."
        refused = _refuse_unapplied(
            action,
            _DATA_SHARING_APPLIES,
            {
                "share_id": share_id,
                "name": name,
                "description": description,
                "filters": filters,
                # enabled defaults to True: only an explicit false is a field passed
                # (named as text, since a bare False counts as empty).
                "enabled": None if enabled else "false",
                "config": config,
                "search": search,
                "limit": None if limit == 50 else limit,
            },
            dict(unknown),
        )
        if refused:
            return refused

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."
        if filters:
            try:
                json.loads(filters)
            except ValueError as exc:
                return f"filters must be a valid JSON string (FilterGroup): {exc}"
            filters, _, status_err = await resolve_filters_json(filters, _gql)
            if status_err:
                return status_err

        # ── live_stream ────────────────────────────────────────────────
        if share_type == "live_stream":
            if action == "list":
                query = (
                    "query Streams($first: Int, $search: String) {"
                    "  streamCollections(first: $first, search: $search) {"
                    "    edges { node { id name stream_live stream_public } } } }"
                )
                data, err = await _gql(
                    query, {"first": min(max(limit, 1), 200), "search": search or None}
                )
                if err:
                    return err
                nodes = _edges(data, "streamCollections")
                if not nodes:
                    return "No live streams found."
                lines = [f"Live streams ({len(nodes)}):"]
                for n in nodes:
                    state = "live" if n.get("stream_live") else "paused"
                    pub = "public" if n.get("stream_public") else "private"
                    lines.append(f"- {n.get('name')} [{state}/{pub}] (id: {n.get('id')})")
                return "\n".join(lines)

            if action == "get":
                if not share_id:
                    return "share_id is required for action 'get'."
                query = (
                    "query Stream($id: String!) {"
                    "  streamCollection(id: $id) { id name description filters"
                    "    origin_filters stream_live stream_public } }"
                )
                data, err = await _gql(query, {"id": share_id})
                if err:
                    return err
                s = data.get("streamCollection")
                if not s:
                    return f"Live stream {share_id} not found."
                return "\n".join(
                    [
                        f"{s.get('name')} (id: {s.get('id')})",
                        f"  live: {s.get('stream_live')} public: {s.get('stream_public')}",
                        f"  description: {s.get('description') or '-'}",
                        f"  filters: {s.get('filters') or '-'}",
                    ]
                )

            if action == "create":
                if not name:
                    return "name is required for action 'create'."
                inp = {"name": name, "stream_live": True, "stream_public": False}
                if description:
                    inp["description"] = description
                if filters:
                    inp["filters"] = filters
                inp.update(extra)
                query = (
                    "mutation StreamAdd($input: StreamCollectionAddInput!) {"
                    "  streamCollectionAdd(input: $input) { id name } }"
                )
                data, err = await _gql(query, {"input": inp})
                if err:
                    return err
                s = data.get("streamCollectionAdd") or {}
                return f"Created live stream '{s.get('name')}' (id: {s.get('id')})."

            if action in ("update", "toggle"):
                if not share_id:
                    return f"share_id is required for action '{action}'."
                if action == "toggle":
                    edits = [{"key": "stream_live", "value": [bool(enabled)]}]
                else:
                    edits = []
                    if name:
                        edits.append({"key": "name", "value": [name]})
                    if description:
                        edits.append({"key": "description", "value": [description]})
                    if filters:
                        edits.append({"key": "filters", "value": [filters]})
                    for key, value in extra.items():
                        edits.append(
                            {"key": key, "value": value if isinstance(value, list) else [value]}
                        )
                    if not edits:
                        return "Provide name, description, filters, or config to update."
                query = (
                    "mutation StreamEdit($id: ID!, $input: [EditInput]!) {"
                    "  streamCollectionEdit(id: $id) {"
                    "    fieldPatch(input: $input) { id name stream_live } } }"
                )
                data, err = await _gql(query, {"id": share_id, "input": edits})
                if err:
                    return err
                if action == "toggle":
                    s = (data.get("streamCollectionEdit") or {}).get("fieldPatch") or {}
                    state = "live" if s.get("stream_live") else "paused"
                    return f"Live stream '{s.get('name')}' is now {state}."
                return f"Updated live stream {share_id}."

            if action == "delete":
                if not share_id:
                    return "share_id is required for action 'delete'."
                query = (
                    "mutation StreamDelete($id: ID!) {"
                    "  streamCollectionEdit(id: $id) { delete } }"
                )
                _, err = await _gql(query, {"id": share_id})
                if err:
                    return err
                return f"Deleted live stream {share_id}."

            return (
                f"Unknown action '{action}' for live_stream. Valid: list, get, create, "
                "update, toggle, delete."
            )

        # ── taxii_collection ───────────────────────────────────────────
        if share_type == "taxii_collection":
            if action == "list":
                query = (
                    "query Taxiis($first: Int, $search: String) {"
                    "  taxiiCollections(first: $first, search: $search) {"
                    "    edges { node { id name taxii_public } } } }"
                )
                data, err = await _gql(
                    query, {"first": min(max(limit, 1), 200), "search": search or None}
                )
                if err:
                    return err
                nodes = _edges(data, "taxiiCollections")
                if not nodes:
                    return "No TAXII collections found."
                lines = [f"TAXII collections ({len(nodes)}):"]
                for n in nodes:
                    pub = "public" if n.get("taxii_public") else "private"
                    lines.append(f"- {n.get('name')} [{pub}] (id: {n.get('id')})")
                return "\n".join(lines)

            if action == "get":
                if not share_id:
                    return "share_id is required for action 'get'."
                query = (
                    "query Taxii($id: String!) {"
                    "  taxiiCollection(id: $id) { id name description filters"
                    "    taxii_public include_inferences score_to_confidence } }"
                )
                data, err = await _gql(query, {"id": share_id})
                if err:
                    return err
                t = data.get("taxiiCollection")
                if not t:
                    return f"TAXII collection {share_id} not found."
                return "\n".join(
                    [
                        f"{t.get('name')} (id: {t.get('id')})",
                        f"  public: {t.get('taxii_public')}",
                        f"  include_inferences: {t.get('include_inferences')}",
                        f"  score_to_confidence: {t.get('score_to_confidence')}",
                        f"  filters: {t.get('filters') or '-'}",
                    ]
                )

            if action == "create":
                if not name:
                    return "name is required for action 'create'."
                inp = {"name": name}
                if description:
                    inp["description"] = description
                if filters:
                    inp["filters"] = filters
                inp.update(extra)
                query = (
                    "mutation TaxiiAdd($input: TaxiiCollectionAddInput!) {"
                    "  taxiiCollectionAdd(input: $input) { id name } }"
                )
                data, err = await _gql(query, {"input": inp})
                if err:
                    return err
                t = data.get("taxiiCollectionAdd") or {}
                return f"Created TAXII collection '{t.get('name')}' (id: {t.get('id')})."

            if action in ("update", "toggle"):
                if not share_id:
                    return f"share_id is required for action '{action}'."
                if action == "toggle":
                    edits = [{"key": "taxii_public", "value": [bool(enabled)]}]
                else:
                    edits = []
                    if name:
                        edits.append({"key": "name", "value": [name]})
                    if description:
                        edits.append({"key": "description", "value": [description]})
                    if filters:
                        edits.append({"key": "filters", "value": [filters]})
                    for key, value in extra.items():
                        edits.append(
                            {"key": key, "value": value if isinstance(value, list) else [value]}
                        )
                    if not edits:
                        return "Provide name, description, filters, or config to update."
                query = (
                    "mutation TaxiiEdit($id: ID!, $input: [EditInput]!) {"
                    "  taxiiCollectionEdit(id: $id) {"
                    "    fieldPatch(input: $input) { id name taxii_public } } }"
                )
                data, err = await _gql(query, {"id": share_id, "input": edits})
                if err:
                    return err
                if action == "toggle":
                    t = (data.get("taxiiCollectionEdit") or {}).get("fieldPatch") or {}
                    state = "public" if t.get("taxii_public") else "private"
                    return f"TAXII collection '{t.get('name')}' is now {state}."
                return f"Updated TAXII collection {share_id}."

            if action == "delete":
                if not share_id:
                    return "share_id is required for action 'delete'."
                query = (
                    "mutation TaxiiDelete($id: ID!) {"
                    "  taxiiCollectionEdit(id: $id) { delete } }"
                )
                _, err = await _gql(query, {"id": share_id})
                if err:
                    return err
                return f"Deleted TAXII collection {share_id}."

            return (
                f"Unknown action '{action}' for taxii_collection. Valid: list, get, "
                "create, update, toggle, delete."
            )

        # ── feed (public CSV) ──────────────────────────────────────────
        if action == "list":
            query = (
                "query Feeds($first: Int, $search: String) {"
                "  feeds(first: $first, search: $search) {"
                "    edges { node { id name feed_types feed_public } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "feeds")
            if not nodes:
                return "No public feeds found."
            lines = [f"Public feeds ({len(nodes)}):"]
            for n in nodes:
                pub = "public" if n.get("feed_public") else "private"
                types = ", ".join(n.get("feed_types") or [])
                lines.append(f"- {n.get('name')} [{pub}] types=[{types}] (id: {n.get('id')})")
            return "\n".join(lines)

        if action == "get":
            if not share_id:
                return "share_id is required for action 'get'."
            f, err = await _fetch_feed_input(share_id)
            if err:
                return err
            return "\n".join(
                [
                    f"{f.get('name')} (id: {share_id})",
                    f"  public: {f.get('feed_public')}",
                    f"  feed_types: {', '.join(f.get('feed_types') or [])}",
                    f"  separator: {f.get('separator')} include_header: {f.get('include_header')}",
                    f"  rolling_time: {f.get('rolling_time')} date_attribute: {f.get('feed_date_attribute')}",
                    f"  attributes: {len(f.get('feed_attributes') or [])}",
                    f"  filters: {f.get('filters') or '-'}",
                ]
            )

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            required = [
                "separator", "feed_date_attribute", "rolling_time",
                "include_header", "feed_types", "feed_attributes",
            ]
            missing = [k for k in required if k not in extra]
            if missing:
                return (
                    "For a CSV feed, config must include: "
                    f"{', '.join(required)}. Missing: {', '.join(missing)}. "
                    "feed_attributes is an array of {attribute, mappings:[{type, "
                    "attribute, relationship_type?, target_entity_type?}]}."
                )
            inp = {"name": name}
            if description:
                inp["description"] = description
            if filters:
                inp["filters"] = filters
            inp.update(extra)
            query = (
                "mutation FeedAdd($input: FeedAddInput!) {"
                "  feedAdd(input: $input) { id name } }"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            f = data.get("feedAdd") or {}
            return f"Created public feed '{f.get('name')}' (id: {f.get('id')})."

        if action in ("update", "toggle"):
            if not share_id:
                return f"share_id is required for action '{action}'."
            current, err = await _fetch_feed_input(share_id)
            if err:
                return err
            # Rebuild a full FeedAddInput from the current state, then override.
            inp = {
                "name": current.get("name"),
                "description": current.get("description"),
                "filters": current.get("filters"),
                "separator": current.get("separator"),
                "feed_date_attribute": current.get("feed_date_attribute"),
                "rolling_time": current.get("rolling_time"),
                "include_header": current.get("include_header"),
                "feed_types": current.get("feed_types"),
                "feed_public": current.get("feed_public"),
                "feed_attributes": [
                    {
                        "attribute": a.get("attribute"),
                        "multi_match_strategy": a.get("multi_match_strategy"),
                        "multi_match_separator": a.get("multi_match_separator"),
                        "mappings": [
                            {
                                "type": m.get("type"),
                                "attribute": m.get("attribute"),
                                "relationship_type": m.get("relationship_type"),
                                "target_entity_type": m.get("target_entity_type"),
                            }
                            for m in (a.get("mappings") or [])
                        ],
                    }
                    for a in (current.get("feed_attributes") or [])
                ],
            }
            if action == "toggle":
                inp["feed_public"] = bool(enabled)
            else:
                if name:
                    inp["name"] = name
                if description:
                    inp["description"] = description
                if filters:
                    inp["filters"] = filters
                inp.update(extra)
            inp = {k: v for k, v in inp.items() if v is not None}
            query = (
                "mutation FeedEdit($id: ID!, $input: FeedAddInput!) {"
                "  feedEdit(id: $id, input: $input) { id name feed_public } }"
            )
            data, err = await _gql(query, {"id": share_id, "input": inp})
            if err:
                return err
            if action == "toggle":
                f = data.get("feedEdit") or {}
                state = "public" if f.get("feed_public") else "private"
                return f"Public feed '{f.get('name')}' is now {state}."
            return f"Updated public feed {share_id}."

        if action == "delete":
            if not share_id:
                return "share_id is required for action 'delete'."
            query = "mutation FeedDelete($id: ID!) { feedDelete(id: $id) }"
            _, err = await _gql(query, {"id": share_id})
            if err:
                return err
            return f"Deleted public feed {share_id}."

        return (
            f"Unknown action '{action}' for feed. Valid: list, get, create, update, "
            "toggle, delete."
        )

    executor.register("manage_opencti_data_sharing", with_status_notes(manage_opencti_data_sharing))

    return [
        ToolDefinition(
            name="manage_opencti_data_sharing",
            description=(
                "Manage OpenCTI data-sharing outputs (requires TAXIIAPI_SETCOLLECTIONS "
                "for writes). share_type: 'live_stream' (live STIX event stream), "
                "'feed' (public CSV feed), or 'taxii_collection' (TAXII 2.1 export). "
                "Actions: 'list', 'get' (share_id), 'create' (name + optional filters "
                "+ type-specific config), 'update' (share_id + fields), 'toggle' "
                "(enabled=true/false: starts/pauses a live_stream's streaming, or "
                "publishes/unpublishes a feed/taxii_collection), 'delete'. 'filters' "
                "is a JSON STRING (serialized FilterGroup). Type-specific 'config' "
                "(JSON object): live_stream -> stream_public, origin_filters; "
                "taxii_collection -> taxii_public, include_inferences, "
                "score_to_confidence; feed -> separator, feed_date_attribute, "
                "rolling_time, include_header, feed_types (array), feed_attributes "
                "(array of {attribute, mappings:[{type, attribute, relationship_type?, "
                "target_entity_type?}]}), feed_public. CSV feed create REQUIRES those "
                "feed fields in config; feed update fetches the current feed and "
                "merges your changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "toggle", "delete"],
                    },
                    "share_type": {
                        "type": "string",
                        "enum": ["live_stream", "feed", "taxii_collection"],
                    },
                    "share_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "filters": {"type": "string", "description": "FilterGroup as a JSON string."},
                    "enabled": {"type": "boolean", "description": "Target state for 'toggle' (stream live / public).", "default": True},
                    "config": {"type": "string", "description": "JSON object of type-specific fields (see description)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action", "share_type"],
            },
            source=_SOURCE,
        ),
    ]
