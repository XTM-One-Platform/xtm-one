# SPDX-License-Identifier: Apache-2.0
"""OpenCTI workflow statuses: per-entity-type Status ids and name resolution.

A workflow status exists twice in OpenCTI:

* a **status template** (``StatusTemplate``: a name and a colour shared by every
  workflow, managed by ``manage_opencti_status_template``);
* a **Status** of one entity type (``SubType.statuses``), which points at a
  template and carries its own id and order.

FILTERS take the **template** id: the ``workflow_id`` key of list and search
filters, playbook listener and filtering steps, live streams, triggers and
retention rules. OpenCTI's filter editors list ``workflow_id`` values from
``statusTemplates``, so a per-type Status id stored in a filter renders as a
chip nobody can select again (its backend accepts both ids at evaluation).
Only SETTING a status (``x_opencti_workflow_id``, playbook "change status")
takes the per-type **Status** id.

This module reads both ids (``subType`` / ``subTypes``, plus the request-access
workflow when the OpenCTI version exposes ``statusesRequestAccess``) and, in a
``workflow_id`` filter, turns a status NAME or a per-type Status id into its
template id.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from ._common import _client, _edges, _graphql

Gql = Callable[[str, dict | None], Awaitable[tuple[dict, str | None]]]

WORKFLOW_FILTER_KEY = "workflow_id"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_STATUS_FIELDS = "id order template { id name color }"
_REQUEST_ACCESS_FIELD = "statusesRequestAccess"
_TYPE_FIELDS = "id label workflowEnabled statuses { " + _STATUS_FIELDS + " }"


def make_gql(base_url: str, headers: dict[str, str]) -> Gql:
    """A GraphQL caller bound to one OpenCTI, for the helpers below."""

    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    return _gql


def _type_fields(with_request_access: bool) -> str:
    if not with_request_access:
        return _TYPE_FIELDS
    return f"{_TYPE_FIELDS} {_REQUEST_ACCESS_FIELD} {{ {_STATUS_FIELDS} }}"


async def fetch_type_workflows(
    gql: Gql, *, type_id: str = "", search: str = "", limit: int = 500
) -> tuple[list[dict], str | None]:
    """Entity sub-types with their Status objects (ids, order, template).

    ``type_id`` reads one entity type exactly (``subType(id:)``); otherwise
    ``subTypes`` is listed with the optional ``search``. The request-access
    statuses are asked for first and dropped when the OpenCTI version does not
    know the field.
    """

    async def _run(with_ra: bool) -> tuple[list[dict], str | None]:
        fields = _type_fields(with_ra)
        if type_id:
            query = f"query SubTypeWorkflow($id: String!) {{ subType(id: $id) {{ {fields} }} }}"
            data, err = await gql(query, {"id": type_id})
            if err:
                return [], err
            node = (data or {}).get("subType")
            return ([node] if node else []), None
        query = (
            "query SubTypeWorkflows($first: Int, $search: String) {"
            f"  subTypes(first: $first, search: $search) {{ edges {{ node {{ {fields} }} }} }} }}"
        )
        data, err = await gql(query, {"first": min(max(limit, 1), 500), "search": search or None})
        if err:
            return [], err
        return _edges(data, "subTypes"), None

    nodes, err = await _run(True)
    if err and _REQUEST_ACCESS_FIELD in err:
        nodes, err = await _run(False)
    return nodes, err


def _sorted_statuses(statuses: list[dict] | None) -> list[dict]:
    return sorted(
        (s for s in (statuses or []) if isinstance(s, dict)),
        key=lambda s: (s.get("order") is None, s.get("order") or 0),
    )


def format_status(status: dict) -> str:
    """``NEW (status id <id> to set, template <id> for filters, order 1, color #...)``."""
    tpl = status.get("template") or {}
    parts = [f"status id {status.get('id')} to set", f"template {tpl.get('id') or '?'} for filters"]
    if status.get("order") is not None:
        parts.append(f"order {status.get('order')}")
    if tpl.get("color"):
        parts.append(f"color {tpl.get('color')}")
    return f"{tpl.get('name') or '?'} ({', '.join(parts)})"


def format_type_workflow(node: dict) -> list[str]:
    """Lines describing one type's workflow statuses (empty when it has none)."""
    lines: list[str] = []
    statuses = _sorted_statuses(node.get("statuses"))
    if statuses:
        lines.append("  workflow statuses: " + "; ".join(format_status(s) for s in statuses))
    request_access = _sorted_statuses(node.get(_REQUEST_ACCESS_FIELD))
    if request_access:
        lines.append("  request-access workflow statuses: " + "; ".join(format_status(s) for s in request_access))
    return lines


# ── workflow_id name resolution ──────────────────────────────────────────


def _keys_of(f: dict) -> list[str]:
    key = f.get("key")
    if isinstance(key, list):
        return [k for k in key if isinstance(k, str)]
    return [key] if isinstance(key, str) else []


def _walk(fg: dict):
    if not isinstance(fg, dict):
        return
    for f in fg.get("filters") or []:
        if isinstance(f, dict):
            yield f
    for sub in fg.get("filterGroups") or []:
        yield from _walk(sub)


def status_names_in(fg: dict) -> list[str]:
    """``workflow_id`` values of a FilterGroup that are not ids (status names)."""
    names: list[str] = []
    for f in _walk(fg):
        if WORKFLOW_FILTER_KEY in _keys_of(f):
            for v in f.get("values") or []:
                if isinstance(v, str) and v.strip() and not _UUID_RE.match(v.strip()):
                    names.append(v.strip())
    return list(dict.fromkeys(names))


def entity_types_in(fg: dict) -> list[str]:
    """Entity types an ``entity_type`` filter of the FilterGroup names."""
    types: list[str] = []
    for f in _walk(fg):
        if "entity_type" in _keys_of(f):
            types.extend(v for v in f.get("values") or [] if isinstance(v, str))
    return list(dict.fromkeys(types))


def status_ids_in(fg: dict[str, Any]) -> list[str]:
    """``workflow_id`` values of a FilterGroup that are ids (template or Status ids)."""
    ids: list[str] = []
    for f in _walk(fg):
        if WORKFLOW_FILTER_KEY in _keys_of(f):
            for v in f.get("values") or []:
                if isinstance(v, str) and _UUID_RE.match(v.strip()):
                    ids.append(v.strip())
    return list(dict.fromkeys(ids))


def _status_entries(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        s
        for s in [*(node.get("statuses") or []), *(node.get(_REQUEST_ACCESS_FIELD) or [])]
        if isinstance(s, dict)
    ]


async def resolve_status_names(
    fg: dict, gql: Gql, entity_types: list[str] | None = None
) -> tuple[dict, dict[str, list[str]], str | None]:
    """Turn ``workflow_id`` filter values into status TEMPLATE ids.

    A status name becomes its template id, and so does a per-type Status id
    (filters carry template ids: the OpenCTI filter editors list templates).
    Template ids and unknown ids pass unchanged; values are deduplicated.

    Returns ``(filter_group, resolved, error)``: ``resolved`` maps each value
    changed to the template id it became; ``error`` lists the valid names when
    a name matches no status of the entity types. The input is never mutated.
    A FilterGroup without a ``workflow_id`` value is returned as is, with no
    OpenCTI call; names alone scoped to entity types read those types only,
    any id reads every type once.
    """
    names = status_names_in(fg)
    ids = status_ids_in(fg)
    if not names and not ids:
        return fg, {}, None
    types = list(dict.fromkeys([*(entity_types or []), *entity_types_in(fg)]))
    nodes: list[dict] = []
    if types and not ids:
        for t in types:
            found, err = await fetch_type_workflows(gql, type_id=t)
            if err:
                return fg, {}, f"Could not read the workflow statuses of {t}: {err}"
            nodes.extend(found)
    else:
        nodes, err = await fetch_type_workflows(gql)
        if err:
            return fg, {}, f"Could not read the workflow statuses: {err}"

    template_of_status: dict[str, str] = {}
    template_of_name: dict[str, str] = {}
    display: dict[str, str] = {}
    for node in nodes:
        in_scope = not types or node.get("id") in types or node.get("label") in types
        for s in _status_entries(node):
            tpl = s.get("template") or {}
            tpl_id, name = tpl.get("id"), tpl.get("name")
            if not tpl_id:
                continue
            if s.get("id") and s["id"] != tpl_id:
                template_of_status[s["id"]] = tpl_id
            if name and in_scope:
                template_of_name.setdefault(name.lower(), tpl_id)
                display.setdefault(name.lower(), name)

    unknown = [n for n in names if n.lower() not in template_of_name]
    if unknown:
        scope = f" for {', '.join(types)}" if types else ""
        valid = ", ".join(sorted(display.values())) or "none (no workflow is enabled)"
        return (
            fg,
            {},
            (
                f"Unknown workflow status name(s){scope}: {', '.join(unknown)}. "
                f"Valid names: {valid}. Template ids: list_opencti_subtypes."
            ),
        )

    resolved: dict[str, list[str]] = {n: [template_of_name[n.lower()]] for n in names}
    resolved.update({i: [template_of_status[i]] for i in ids if i in template_of_status})
    if not resolved:
        return fg, {}, None
    out = copy.deepcopy(fg)
    for f in _walk(out):
        if WORKFLOW_FILTER_KEY not in _keys_of(f):
            continue
        new_values: list[Any] = []
        for v in f.get("values") or []:
            key = v.strip() if isinstance(v, str) else v
            mapped = resolved.get(key) if isinstance(key, str) else None
            new_values.extend(mapped if mapped else [v])
        f["values"] = list(dict.fromkeys(new_values))
    return out, resolved, None


def describe_resolution(resolved: dict[str, list[str]]) -> str:
    """One line echoing the values resolved, or '' when none were."""
    if not resolved:
        return ""
    parts = [f"{value} -> {', '.join(ids)}" for value, ids in resolved.items()]
    return (
        "workflow_id filter values set to status template ids (filters take "
        "template ids): " + "; ".join(parts) + "."
    )


_NOTES: ContextVar[list[str] | None] = ContextVar("opencti_workflow_filter_notes", default=None)


def with_status_notes(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap a tool handler so a ``resolve_filters_json`` note ends its text result."""

    @functools.wraps(handler)
    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        notes: list[str] = []
        token = _NOTES.set(notes)
        try:
            result = await handler(*args, **kwargs)
        finally:
            _NOTES.reset(token)
        if notes and isinstance(result, str):
            return result + "\n" + " ".join(dict.fromkeys(notes))
        return result

    return _wrapped


async def resolve_filters_json(
    filters: str, gql: Gql, entity_types: list[str] | None = None
) -> tuple[str, str, str | None]:
    """The JSON-string form for tools taking ``filters`` as a FilterGroup string.

    Returns ``(filters, note, error)``. Only a string naming ``workflow_id`` is
    parsed, so every other filter is passed through byte for byte. The note is
    also added to the result of a handler wrapped by ``with_status_notes``.
    """
    if not filters or WORKFLOW_FILTER_KEY not in filters:
        return filters, "", None
    try:
        fg = json.loads(filters)
    except ValueError:
        return filters, "", None  # the tool's own JSON check reports it
    if not isinstance(fg, dict):
        return filters, "", None
    out, resolved, err = await resolve_status_names(fg, gql, entity_types)
    if err:
        return filters, "", err
    if not resolved:
        return filters, "", None
    note = describe_resolution(resolved)
    notes = _NOTES.get()
    if notes is not None:
        notes.append(note)
    return json.dumps(out), note, None


async def resolve_configuration_filters(configuration: str, gql: Gql) -> tuple[str, str, str | None]:
    """``resolve_filters_json`` on the ``filters`` string of a playbook node configuration.

    Returns ``(configuration, note, error)``; a configuration without a
    ``workflow_id`` filter string is returned byte for byte. A playbook
    "change status" value lives outside ``filters`` and keeps its Status id.
    """
    if not configuration or WORKFLOW_FILTER_KEY not in configuration:
        return configuration, "", None
    try:
        config = json.loads(configuration)
    except ValueError:
        return configuration, "", None
    if not isinstance(config, dict) or not isinstance(config.get("filters"), str):
        return configuration, "", None
    filters, note, err = await resolve_filters_json(config["filters"], gql)
    if err or not note:
        return configuration, "", err
    config["filters"] = filters
    return json.dumps(config), note, None
