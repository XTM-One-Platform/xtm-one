"""OpenCTI marking definitions and the refs other tools take.

* ``list_opencti_markings`` - read the marking definitions (TLP, PAP,
  statements) with their internal id and STIX id.
* :func:`resolve_markings` / :func:`resolve_author` - turn a ref an agent
  holds (an internal id, a STIX id, or the definition or identity name) into
  the node OpenCTI stores. An ingestion feed keeps ``object_marking_refs``,
  ``markings`` and ``created_by_ref`` exactly as given: a STIX id or a name
  stored there marks nothing and raises no error, so the refs are resolved
  before they are written.

Both resolvers take the caller's ``gql`` coroutine, so a tool keeps its own
client, draft scope and test stub.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql

_SOURCE = "integration:opencti"

#: ``gql(query, variables) -> (data, error)``, as each provider module builds it.
Gql = Callable[[str, dict[str, Any] | None], Awaitable[tuple[dict[str, Any], str | None]]]

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_STIX_ID = re.compile(r"^[a-z][a-z0-9-]*--[0-9a-f-]{36}$", re.IGNORECASE)

_MARKING_FIELDS = "id standard_id x_opencti_stix_ids definition definition_type x_opencti_order"
_MARKINGS_QUERY = (
    "query MarkingList($first: Int, $search: String) {"
    "  markingDefinitions(first: $first, search: $search) {"
    f"    edges {{ node {{ {_MARKING_FIELDS} }} }} }} }}"
)
_MARKING_QUERY = f"query MarkingGet($id: String!) {{ markingDefinition(id: $id) {{ {_MARKING_FIELDS} }} }}"
_IDENTITY_FIELDS = "id standard_id name entity_type"
_IDENTITY_QUERY = f"query IdentityGet($id: String!) {{ identity(id: $id) {{ {_IDENTITY_FIELDS} }} }}"
_IDENTITIES_QUERY = (
    "query IdentitySearch($search: String, $first: Int) {"
    f"  identities(search: $search, first: $first) {{ edges {{ node {{ {_IDENTITY_FIELDS} }} }} }} }}"
)

#: Markings a platform holds are few; one page covers them all.
_CATALOG_SIZE = 500


def is_object_id(ref: str) -> bool:
    """True for an internal id (UUID) or a STIX id (``type--uuid``)."""
    return bool(_UUID.match(ref) or _STIX_ID.match(ref))


def _marking_matches(node: dict[str, Any], ref: str) -> bool:
    key = ref.strip().lower()
    ids = [node.get("id"), node.get("standard_id"), *(node.get("x_opencti_stix_ids") or [])]
    if key in {str(i).lower() for i in ids if i}:
        return True
    return (node.get("definition") or "").strip().lower() == key


async def fetch_markings(
    gql: Gql, search: str = "", limit: int = _CATALOG_SIZE
) -> tuple[list[dict[str, Any]], str | None]:
    """The marking definitions, filtered by *search* when given."""
    data, err = await gql(_MARKINGS_QUERY, {"first": limit, "search": search or None})
    if err:
        return [], err
    return _edges(data, "markingDefinitions"), None


async def resolve_markings(gql: Gql, refs: Iterable[Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve each ref to its marking definition, in order, without duplicates.

    A ref is an internal id, a STIX id (``marking-definition--...``) or a
    definition name (``TLP:CLEAR``, case-insensitive). The error names every ref
    nothing matches; the caller then writes nothing.
    """
    wanted = [str(r).strip() for r in refs if str(r).strip()]
    if not wanted:
        return [], None
    catalog, err = await fetch_markings(gql)
    if err:
        return [], (
            f"Error: could not read the marking definitions to resolve {', '.join(wanted)}: {err} Nothing was written."
        )
    resolved: list[dict[str, Any]] = []
    missing: list[str] = []
    for ref in wanted:
        node = next((n for n in catalog if _marking_matches(n, ref)), None)
        if node is None and is_object_id(ref):
            data, lookup_err = await gql(_MARKING_QUERY, {"id": ref})
            node = None if lookup_err else (data or {}).get("markingDefinition")
        if node is None:
            missing.append(ref)
        elif all(n.get("id") != node.get("id") for n in resolved):
            resolved.append(node)
    if missing:
        return [], (
            "Error: no marking definition matches "
            + ", ".join(repr(m) for m in missing)
            + ". Give an internal id, a STIX id or a definition name such as TLP:CLEAR "
            "(list_opencti_markings lists them). Nothing was written."
        )
    return resolved, None


async def resolve_author(gql: Gql, ref: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve an author ref (internal id, STIX id or exact identity name).

    An empty ref answers ``(None, None)``. A name shared by several identities
    is an error listing them, never a guess.
    """
    ref = str(ref or "").strip()
    if not ref:
        return None, None
    if is_object_id(ref):
        data, err = await gql(_IDENTITY_QUERY, {"id": ref})
        if err:
            return None, f"Error: could not resolve the author {ref!r}: {err} Nothing was written."
        node = (data or {}).get("identity")
        if not node:
            return None, (
                f"Error: no identity (organization, individual, sector, system) has the id "
                f"{ref!r}. Nothing was written."
            )
        return node, None
    data, err = await gql(_IDENTITIES_QUERY, {"search": ref, "first": 50})
    if err:
        return None, f"Error: could not resolve the author {ref!r}: {err} Nothing was written."
    exact = [n for n in _edges(data, "identities") if (n.get("name") or "").strip().lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0], None
    if not exact:
        return None, (
            f"Error: no identity is named {ref!r}. Give the exact name, the internal id or the "
            "STIX id of an organization or individual. Nothing was written."
        )
    choices = "; ".join(f"{n.get('name')} ({n.get('entity_type')}, id {n.get('id')})" for n in exact)
    return None, (
        f"Error: {len(exact)} identities are named {ref!r}: {choices}. Give the id of the one "
        "to use. Nothing was written."
    )


def register(base_url: str, headers: dict[str, str], executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def list_opencti_markings(search: str = "", limit: int = 100) -> str:
        nodes, err = await fetch_markings(_gql, search, min(max(limit, 1), _CATALOG_SIZE))
        if err:
            return err
        if not nodes:
            return f"No marking definitions match '{search}'." if search else "No marking definitions found."
        nodes = sorted(nodes, key=lambda n: (n.get("definition_type") or "", n.get("x_opencti_order") or 0))
        lines = [f"Marking definitions ({len(nodes)}):"]
        for n in nodes:
            lines.append(
                f"- {n.get('definition')} [{n.get('definition_type')}] "
                f"id: {n.get('id')}, STIX id: {n.get('standard_id')}"
            )
        return "\n".join(lines)

    executor.register("list_opencti_markings", list_opencti_markings)

    return [
        ToolDefinition(
            name="list_opencti_markings",
            description=(
                "List OpenCTI marking definitions (TLP, PAP, statements; read-only) with "
                "their internal id and STIX id. add_opencti_marking and the ingestion feed "
                "tools take the internal id, the STIX id or the name (TLP:CLEAR)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text in the definition, e.g. TLP or PAP."},
                    "limit": {"type": "integer", "default": 100},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
    ]
