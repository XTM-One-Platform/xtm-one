"""OpenCTI ingestion tools - RSS/CSV/JSON/TAXII feeds + intake forms.

* ``manage_opencti_ingestion`` - one consolidated tool over all pull-feed types
  (``src/modules/ingestion/*``). ``feed_type`` selects the GraphQL family.
* ``manage_opencti_form``      - intake forms (``src/modules/form/form.graphql``).

Start/stop is done with action='toggle' (sets ``ingestion_running``). Type
specific creation fields are supplied via the ``config`` JSON object.
Requires INGESTION_SETINGESTIONS for writes. Not draft-gated.

OpenCTI stores a feed's marking and author refs exactly as given and applies
only internal ids: a STIX id or a definition name there marks nothing, with no
error. create, update and test therefore resolve them first (``markings``)
and refuse the call, writing nothing, when one cannot be resolved; create and
get read back the markings, author and ingestion user OpenCTI stored.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql, _graphql_upload
from .access import _refuse_unapplied
from .markings import fetch_markings, resolve_author, resolve_markings

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"

#: The fields each action of manage_opencti_ingestion reads. Any other field passed
#: (an undeclared argument included) is refused by name with nothing written: a
#: list that ignores ``uri`` must never answer as if it had filtered on it.
_INGESTION_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"feed_id"}),
    "create": frozenset(
        {
            "name",
            "description",
            "uri",
            "user_id",
            "scheduling_period",
            "authentication_type",
            "authentication_value",
            "running",
            "config",
        }
    ),
    "update": frozenset({"feed_id", "name", "description", "uri", "config"}),
    "toggle": frozenset({"feed_id", "running"}),
    "reset": frozenset({"feed_id"}),
    "delete": frozenset({"feed_id"}),
    "test": frozenset({"name", "uri", "user_id", "authentication_type", "authentication_value", "config"}),
}
_DEFAULT_FEED_LIMIT = 50

#: config keys an agent may try for an RSS label import switch OpenCTI does not have.
_LABEL_IMPORT_KEYS = ("label_import", "import_labels", "ingest_labels", "labels_import")

# feed_type -> GraphQL operation names + the AddInput type + the per-get fields
_FEEDS: dict[str, dict] = {
    "rss": {
        "list": "ingestionRsss",
        "get": "ingestionRss",
        "add": "ingestionRssAdd",
        "add_input": "IngestionRssAddInput",
        "patch": "ingestionRssFieldPatch",
        "delete": "ingestionRssDelete",
        "reset": None,
        "test": None,
        "needs_uri": True,
        "needs_auth": False,
        "needs_user": True,
        "marking_key": "object_marking_refs",
        "author_key": "created_by_ref",
        "get_fields": (
            "id name description uri scheduling_period ingestion_running"
            " report_types current_state_date last_execution_date"
            " user { id name } defaultCreatedBy { id name entity_type }"
            " defaultMarkingDefinitions { id definition }"
        ),
    },
    "csv": {
        "list": "ingestionCsvs",
        "get": "ingestionCsv",
        "add": "ingestionCsvAdd",
        "add_input": "IngestionCsvAddInput",
        "patch": "ingestionCsvFieldPatch",
        "delete": "ingestionCsvDelete",
        "reset": "ingestionCsvResetState",
        "test": "ingestionCsvTester",
        "needs_uri": True,
        "needs_auth": True,
        "needs_user": True,
        "marking_key": "markings",
        "get_fields": (
            "id name description uri authentication_type ingestion_running markings"
            " csv_mapper_type current_state_date last_execution_date user { id name }"
        ),
    },
    "json": {
        "list": "ingestionJsons",
        "get": "ingestionJson",
        "add": "ingestionJsonAdd",
        "add_input": "IngestionJsonAddInput",
        "patch": "ingestionJsonFieldPatch",
        "delete": "ingestionJsonDelete",
        "reset": "ingestionJsonResetState",
        "test": "ingestionJsonTester",
        "needs_uri": True,
        "needs_auth": True,
        "needs_user": True,
        "marking_key": "markings",
        "get_fields": (
            "id name description uri verb authentication_type ingestion_running"
            " markings last_execution_date user { id name }"
        ),
    },
    "taxii": {
        "list": "ingestionTaxiis",
        "get": "ingestionTaxii",
        "add": "ingestionTaxiiAdd",
        "add_input": "IngestionTaxiiAddInput",
        "patch": "ingestionTaxiiFieldPatch",
        "delete": "ingestionTaxiiDelete",
        "reset": "ingestionTaxiiResetState",
        "test": None,
        "needs_uri": True,
        "needs_auth": True,
        "needs_user": True,
        "get_fields": (
            "id name description uri collection version authentication_type"
            " ingestion_running added_after_start confidence_to_score"
            " last_execution_date user { id name }"
        ),
    },
    "taxii_collection": {
        "list": "ingestionTaxiiCollections",
        "get": "ingestionTaxiiCollection",
        "add": "ingestionTaxiiCollectionAdd",
        "add_input": "IngestionTaxiiCollectionAddInput",
        "patch": "ingestionTaxiiCollectionFieldPatch",
        "delete": "ingestionTaxiiCollectionDelete",
        "reset": None,
        "test": None,
        "needs_uri": False,
        "needs_auth": False,
        "needs_user": False,
        "get_fields": "id name description ingestion_running confidence_to_score",
    },
}


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def _resolve_refs(feed_type: str, spec: dict[str, Any], extra: dict[str, Any]) -> str | None:
        """Rewrite the marking and author refs of *extra* to internal ids, in place.

        Answers the error to return (nothing is written then), else None.
        """
        marking_key: str | None = spec.get("marking_key")
        aliases = ("object_marking_refs", "markings")
        given = [k for k in aliases if k in extra]
        if given and not marking_key:
            return f"Error: a {feed_type} feed takes no markings. Nothing was written."
        if given and marking_key:
            refs: list[Any] = []
            for key in given:
                value = extra.pop(key)
                refs += value if isinstance(value, list) else [value]
            nodes, err = await resolve_markings(_gql, refs)
            if err:
                return err
            extra[marking_key] = [n["id"] for n in nodes]
        author_key: str | None = spec.get("author_key")
        given = [k for k in ("created_by_ref", "created_by", "author") if k in extra]
        if given and not author_key:
            return (
                f"Error: a {feed_type} feed takes no author (OpenCTI sets none on what it "
                "ingests). Nothing was written."
            )
        if given and author_key:
            ref = ""
            for key in given:
                ref = extra.pop(key) or ref
            node, err = await resolve_author(_gql, ref)
            if err:
                return err
            if node:
                extra[author_key] = node["id"]
        unsupported = [k for k in _LABEL_IMPORT_KEYS if k in extra]
        if unsupported:
            return (
                f"Error: OpenCTI has no label import setting on ingestion feeds "
                f"({', '.join(unsupported)}): an RSS 2.0 feed sends each item's categories "
                "as labels of its report; a new label is created only when the ingestion "
                "user's role has Manage labels (SETTINGS_SETLABELS), so that role is the "
                "switch. Nothing was written."
            )
        return None

    async def _read_feed(spec: dict[str, Any], feed_id: str) -> tuple[dict[str, Any] | None, str | None]:
        field = spec["get"]
        query = f"query FeedGet($id: String!) {{  {field}(id: $id) {{ {spec['get_fields']} }} }}"
        data, err = await _gql(query, {"id": feed_id})
        if err:
            return None, err
        return data.get(field), None

    async def _marking_names(ids: list[str]) -> dict[str, str]:
        """Definition name by internal id, for the feeds that store bare ids."""
        if not ids:
            return {}
        nodes, err = await fetch_markings(_gql)
        if err:
            return {}
        return {str(n.get("id")): str(n.get("definition")) for n in nodes}

    async def _describe(feed_type: str, spec: dict[str, Any], node: dict[str, Any]) -> list[str]:
        """The feed's fields, markings, author and ingestion user, one per line."""
        lines = [f"{feed_type} feed '{node.get('name')}' (id: {node.get('id')})"]
        shown = ("id", "name", "user", "defaultCreatedBy", "defaultMarkingDefinitions", "markings")
        for key, value in node.items():
            if key in shown or value in (None, "", []):
                continue
            lines.append(f"  {key}: {value}")
        if spec.get("marking_key"):
            if "defaultMarkingDefinitions" in node:
                marks = [
                    f"{m.get('definition')} ({m.get('id')})" for m in node.get("defaultMarkingDefinitions") or [] if m
                ]
            else:
                ids = node.get("markings") or []
                names = await _marking_names(ids)
                marks = [f"{names.get(i, 'unknown marking')} ({i})" for i in ids]
            lines.append(f"  markings: {', '.join(marks) if marks else 'none'}")
        if spec.get("author_key"):
            author = node.get("defaultCreatedBy")
            lines.append(
                f"  author: {author.get('name')} ({author.get('entity_type')}, {author.get('id')})"
                if author
                else "  author: none"
            )
        if "user" in node:
            user = node.get("user")
            lines.append(
                f"  ingestion user: {user.get('name')} ({user.get('id')})"
                if user
                else "  ingestion user: none (the feed cannot run)"
            )
        if feed_type == "rss":
            lines.append(
                "  labels: RSS 2.0 item categories are sent as report labels; an existing "
                "label is attached, a new one is created only when the ingestion user's "
                "role has Manage labels (SETTINGS_SETLABELS), else it is skipped"
            )
        return lines

    def _stored_marking_ids(node: dict[str, Any]) -> set[str]:
        if "defaultMarkingDefinitions" in node:
            return {m.get("id") for m in node.get("defaultMarkingDefinitions") or [] if m}
        return set(node.get("markings") or [])

    async def manage_opencti_ingestion(
        action: str,
        feed_type: str = "",
        feed_id: str = "",
        name: str = "",
        description: str = "",
        uri: str = "",
        user_id: str = "",
        scheduling_period: str = "",
        authentication_type: str = "none",
        authentication_value: str = "",
        running: bool = True,
        config: str = "",
        search: str = "",
        limit: int = _DEFAULT_FEED_LIMIT,
        **extra_args: object,
    ) -> str:
        action = (action or "").strip().lower()
        feed_type = (feed_type or "").strip().lower()
        spec = _FEEDS.get(feed_type)
        if not spec:
            return (
                f"feed_type must be one of: {', '.join(_FEEDS)}."
            )
        # Defaults count as not passed: a field is refused only when the caller set it.
        refused = _refuse_unapplied(
            action,
            _INGESTION_APPLIES,
            {
                "feed_id": feed_id,
                "name": name,
                "description": description,
                "uri": uri,
                "user_id": user_id,
                "scheduling_period": scheduling_period,
                "authentication_type": "" if (authentication_type or "none") == "none" else authentication_type,
                "authentication_value": authentication_value,
                "running": None if running else "false",
                "config": config,
                "search": search,
                "limit": None if limit == _DEFAULT_FEED_LIMIT else limit,
            },
            dict(extra_args),
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

        if action == "list":
            field = spec["list"]
            query = (
                f"query FeedList($first: Int, $search: String) {{"
                f"  {field}(first: $first, search: $search) {{"
                f"    edges {{ node {{ id name ingestion_running }} }} }} }}"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, field)
            if not nodes:
                return f"No {feed_type} feeds found."
            lines = [f"{feed_type} feeds ({len(nodes)}):"]
            for n in nodes:
                state = "running" if n.get("ingestion_running") else "stopped"
                lines.append(f"- {n.get('name')} [{state}] (id: {n.get('id')})")
            return "\n".join(lines)

        if action == "get":
            if not feed_id:
                return "feed_id is required for action 'get'."
            node, err = await _read_feed(spec, feed_id)
            if err:
                return err
            if not node:
                return f"{feed_type} feed {feed_id} not found."
            return "\n".join(await _describe(feed_type, spec, node))

        if action in ("create", "update", "test") and (err := await _resolve_refs(feed_type, spec, extra)):
            return err

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            if spec["needs_uri"] and not uri:
                return f"uri is required to create a {feed_type} feed."
            if (
                spec["needs_user"]
                and not user_id
                and "user_id" not in extra
                and "automatic_user" not in extra
            ):
                return (
                    f"user_id is required to create a {feed_type} feed (the ingestion "
                    "runs as this user). Get one from manage_opencti_user list or "
                    "manage_opencti_profile get, or pass automatic_user in config."
                )
            inp: dict = {"name": name}
            if description:
                inp["description"] = description
            if spec["needs_uri"]:
                inp["uri"] = uri
            if user_id:
                inp["user_id"] = user_id
            if scheduling_period:
                inp["scheduling_period"] = scheduling_period
            if spec["needs_auth"]:
                inp["authentication_type"] = authentication_type or "none"
                if authentication_value:
                    inp["authentication_value"] = authentication_value
            inp["ingestion_running"] = bool(running)
            inp.update(extra)
            field = spec["add"]
            query = (
                f"mutation FeedAdd($input: {spec['add_input']}!) {{"
                f"  {field}(input: $input) {{ id name }} }}"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            node = data.get(field) or {}
            created = f"Created {feed_type} feed '{node.get('name')}' (id: {node.get('id')})."
            if not node.get("id"):
                return created
            # OpenCTI answers a create without checking the refs it stores: read
            # the feed back so the answer says what it holds.
            stored, err = await _read_feed(spec, node["id"])
            if err or not stored:
                return f"{created} It could not be read back ({err or 'not found'})."
            lines = [created, *(await _describe(feed_type, spec, stored))[1:]]
            marking_key = spec.get("marking_key")
            wanted = set(inp.get(marking_key) or []) if marking_key else set()
            if wanted - _stored_marking_ids(stored):
                lines.insert(
                    0,
                    "Error: the feed was created but OpenCTI did not store every marking "
                    "asked for; set them with action 'update' or delete the feed.",
                )
            return "\n".join(lines)

        if action == "update":
            if not feed_id:
                return "feed_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if uri and spec["needs_uri"]:
                edits.append({"key": "uri", "value": [uri]})
            for key, value in extra.items():
                edits.append(
                    {"key": key, "value": value if isinstance(value, list) else [value]}
                )
            if not edits:
                return "Provide name, description, uri, or config fields to update."
            field = spec["patch"]
            query = (
                f"mutation FeedPatch($id: ID!, $input: [EditInput!]!) {{"
                f"  {field}(id: $id, input: $input) {{ id name }} }}"
            )
            _, err = await _gql(query, {"id": feed_id, "input": edits})
            if err:
                return err
            return f"Updated {feed_type} feed {feed_id}."

        if action == "toggle":
            if not feed_id:
                return "feed_id is required for action 'toggle'."
            field = spec["patch"]
            query = (
                f"mutation FeedToggle($id: ID!, $input: [EditInput!]!) {{"
                f"  {field}(id: $id, input: $input) {{ id name ingestion_running }} }}"
            )
            data, err = await _gql(
                query,
                {
                    "id": feed_id,
                    "input": [{"key": "ingestion_running", "value": [bool(running)]}],
                },
            )
            if err:
                return err
            node = data.get(field) or {}
            state = "running" if node.get("ingestion_running") else "stopped"
            return f"{feed_type} feed '{node.get('name')}' is now {state}."

        if action == "reset":
            if not spec["reset"]:
                return f"reset is not supported for feed_type '{feed_type}'."
            if not feed_id:
                return "feed_id is required for action 'reset'."
            field = spec["reset"]
            query = f"mutation FeedReset($id: ID!) {{ {field}(id: $id) {{ id name }} }}"
            _, err = await _gql(query, {"id": feed_id})
            if err:
                return err
            return f"Reset ingestion state for {feed_type} feed {feed_id}."

        if action == "delete":
            if not feed_id:
                return "feed_id is required for action 'delete'."
            field = spec["delete"]
            query = f"mutation FeedDelete($id: ID!) {{ {field}(id: $id) }}"
            _, err = await _gql(query, {"id": feed_id})
            if err:
                return err
            return f"Deleted {feed_type} feed {feed_id}."

        if action == "test":
            if not spec["test"]:
                return f"test is not supported for feed_type '{feed_type}'."
            inp = {"name": name or "test", "uri": uri}
            if spec["needs_auth"]:
                inp["authentication_type"] = authentication_type or "none"
                if authentication_value:
                    inp["authentication_value"] = authentication_value
            if user_id:
                inp["user_id"] = user_id
            inp.update(extra)
            field = spec["test"]
            query = (
                f"mutation FeedTest($input: {spec['add_input']}!) {{"
                f"  {field}(input: $input) {{ nbEntities nbRelationships }} }}"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            res = data.get(field) or {}
            return (
                f"Test parsed {res.get('nbEntities', '?')} entities and "
                f"{res.get('nbRelationships', '?')} relationships."
            )

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "toggle, reset, delete, test."
        )

    # ── Intake forms ───────────────────────────────────────────────────
    async def manage_opencti_form(
        action: str,
        form_id: str = "",
        name: str = "",
        description: str = "",
        form_schema: str = "",
        active: bool | None = None,
        values: str = "",
        content: str = "",
        search: str = "",
        limit: int = 50,
    ) -> str:
        action = (action or "").strip().lower()

        if action == "list":
            query = (
                "query Forms($first: Int, $search: String) {"
                "  forms(first: $first, search: $search, orderBy: name,"
                "    orderMode: asc) { edges { node { id name active } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "forms")
            if not nodes:
                return "No intake forms found."
            lines = [f"Intake forms ({len(nodes)}):"]
            for n in nodes:
                state = "active" if n.get("active") else "inactive"
                lines.append(f"- {n.get('name')} [{state}] (id: {n.get('id')})")
            return "\n".join(lines)

        if action == "get":
            if not form_id:
                return "form_id is required for action 'get'."
            query = (
                "query Form($id: ID!) {"
                "  form(id: $id) { id name description active form_schema } }"
            )
            data, err = await _gql(query, {"id": form_id})
            if err:
                return err
            f = data.get("form")
            if not f:
                return f"Form {form_id} not found."
            return "\n".join(
                [
                    f"{f.get('name')} (id: {f.get('id')})",
                    f"  active: {f.get('active')}",
                    f"  description: {f.get('description')}",
                    f"  schema: {f.get('form_schema')}",
                ]
            )

        if action == "create":
            if not name or not description or not form_schema:
                return (
                    "name, description and form_schema (a JSON string) are required "
                    "for action 'create'."
                )
            try:
                json.loads(form_schema)
            except ValueError as exc:
                return f"form_schema must be a valid JSON string: {exc}"
            query = (
                "mutation FormAdd($input: FormAddInput!) {"
                "  formAdd(input: $input) { id name } }"
            )
            inp = {
                "name": name,
                "description": description,
                "form_schema": form_schema,
                "active": True if active is None else bool(active),
            }
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            f = data.get("formAdd") or {}
            return f"Created intake form '{f.get('name')}' (id: {f.get('id')})."

        if action == "update":
            if not form_id:
                return "form_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if form_schema:
                try:
                    json.loads(form_schema)
                except ValueError as exc:
                    return f"form_schema must be a valid JSON string: {exc}"
                edits.append({"key": "form_schema", "value": [form_schema]})
            if active is not None:
                edits.append({"key": "active", "value": [bool(active)]})
            if not edits:
                return "Provide name, description, form_schema, or active to update."
            query = (
                "mutation FormPatch($id: ID!, $input: [EditInput!]!) {"
                "  formFieldPatch(id: $id, input: $input) { id name } }"
            )
            _, err = await _gql(query, {"id": form_id, "input": edits})
            if err:
                return err
            return f"Updated intake form {form_id}."

        if action == "delete":
            if not form_id:
                return "form_id is required for action 'delete'."
            query = "mutation FormDelete($id: ID!) { formDelete(id: $id) }"
            _, err = await _gql(query, {"id": form_id})
            if err:
                return err
            return f"Deleted intake form {form_id}."

        if action == "submit":
            if not form_id or not values:
                return "form_id and values (a JSON string) are required for 'submit'."
            try:
                json.loads(values)
            except ValueError as exc:
                return f"values must be a valid JSON string: {exc}"
            query = (
                "mutation FormSubmit($input: FormSubmissionInput!) {"
                "  formSubmit(input: $input) { success bundleId message entityId } }"
            )
            data, err = await _gql(
                query, {"input": {"formId": form_id, "values": values}}
            )
            if err:
                return err
            r = data.get("formSubmit") or {}
            return (
                f"Form submission success={r.get('success')} "
                f"entityId={r.get('entityId')} bundleId={r.get('bundleId')} "
                f"{r.get('message') or ''}".strip()
            )

        if action == "import":
            if not content:
                return "content (a form configuration export JSON) is required for 'import'."
            query = (
                "mutation FormImport($file: Upload!) {"
                "  formImport(file: $file) { id name } }"
            )
            async with _client(headers, timeout=60) as client:
                data, err = await _graphql_upload(
                    client,
                    base_url,
                    headers,
                    query,
                    {},
                    "file",
                    f"{name or 'form'}.json",
                    content.encode("utf-8"),
                    "application/json",
                )
            if err:
                return err
            f = data.get("formImport") or {}
            return f"Imported intake form '{f.get('name')}' (id: {f.get('id')})."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, submit, import."
        )

    executor.register("manage_opencti_ingestion", manage_opencti_ingestion)
    executor.register("manage_opencti_form", manage_opencti_form)

    return [
        ToolDefinition(
            name="manage_opencti_ingestion",
            description=(
                "Manage OpenCTI ingestion feeds (requires INGESTION_SETINGESTIONS for "
                "writes). feed_type: 'rss', 'csv', 'json', 'taxii' (pull from a remote "
                "TAXII server), or 'taxii_collection' (expose OpenCTI as a TAXII "
                "endpoint). Actions: 'list', 'get' (feed_id), 'create', 'update', "
                "'toggle' (running=true/false to start/stop), 'reset' (csv/json/taxii "
                "only - resets ingestion cursor), 'delete', 'test' (csv/json only). "
                "Common create fields: name, uri, user_id (the user the ingestion runs "
                "as - required for rss/csv/json/taxii), description, scheduling_period, "
                "authentication_type (none/basic/bearer/certificate for csv/json/"
                "taxii), authentication_value. Type-specific fields go in 'config' (a "
                "JSON object): rss -> report_types, object_marking_refs, created_by_ref, "
                "confidence_level; csv -> csv_mapper_id, csv_mapper_type, markings; "
                "json -> json_mapper_id, verb, body, headers, query_attributes, "
                "markings; taxii -> collection, version (v1/v2/v21), added_after_start, "
                "confidence_to_score; taxii_collection -> authorized_members, "
                "confidence_to_score. Marking refs take an internal id, a STIX id or a "
                "name (TLP:CLEAR; list_opencti_markings), the author an id or an exact "
                "identity name; an unknown ref is an error and nothing is written. A field "
                "the action does not apply (uri on list, name on get) is refused by name "
                "and nothing is sent. create "
                "and get show the stored markings, author and ingestion user. No RSS label "
                "setting: new category labels need the ingestion user's Manage labels."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "toggle", "reset", "delete", "test"],
                    },
                    "feed_type": {
                        "type": "string",
                        "enum": ["rss", "csv", "json", "taxii", "taxii_collection"],
                    },
                    "feed_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "uri": {"type": "string", "description": "Feed source URL."},
                    "user_id": {"type": "string", "description": "User the ingestion runs as."},
                    "scheduling_period": {"type": "string", "description": "ISO 8601 duration, e.g. PT1H, P1D."},
                    "authentication_type": {"type": "string", "enum": ["none", "basic", "bearer", "certificate"], "default": "none"},
                    "authentication_value": {"type": "string", "description": "Credentials (e.g. 'user:pass' for basic, token for bearer)."},
                    "running": {"type": "boolean", "description": "Start (true) / stop (false) for 'toggle'; initial running state for 'create'.", "default": True},
                    "config": {"type": "string", "description": "JSON object of type-specific fields (see description)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action", "feed_type"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_form",
            description=(
                "Manage OpenCTI intake forms (requires INGESTION_SETINGESTIONS for "
                "writes). Actions: 'list' (search), 'get' (form_id), 'create' (name + "
                "description + form_schema JSON string, optional active), 'update' "
                "(form_id + fields), 'delete' (form_id), 'submit' (form_id + values "
                "JSON string -> creates a STIX bundle), 'import' (content = a form "
                "configuration export JSON)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "delete", "submit", "import"],
                    },
                    "form_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "form_schema": {"type": "string", "description": "Form schema as a JSON string (create/update)."},
                    "active": {"type": "boolean", "description": "Whether the form is active."},
                    "values": {"type": "string", "description": "Submission values as a JSON string (submit)."},
                    "content": {"type": "string", "description": "Form configuration export JSON (import)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
