"""OpenCTI integration - STIX knowledge, draft, NLQ-filter and dev tools.

Relocated from the original single-file provider. Exposes
``register_knowledge`` which builds the STIX knowledge, draft management,
NLQ-filter validation, and developer tools. Shared primitives live in
``_common``; ``build_tools`` (the provider entry point) lives in
``__init__``.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import difflib
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from typing import Any
from urllib.parse import quote

import httpx
from json_repair import repair_json

from app.core.config import settings
from app.core.utils import format_exception_message
from app.integrations.file_delivery import link_lines, read_pipe_file, uploaded_file_answer
from app.integrations.http import (
    clip_text,
    format_entity_list,
    format_http_error,
    format_network_error,
)
from app.tools.base import (
    HITL_APPROVAL_KEYWORD,
    CallableToolExecutor,
    FileContext,
    ToolDefinition,
    ToolResult,
    is_error_shaped_result,
    offered_tools,
)

from ._common import (
    _CASE_TYPES_FIELDS,
    _CONTAINER_MUTATIONS,
    _DRAFT_HEADER,
    _DRAFT_REQUIRED_MSG,
    _DRAFT_REQUIRED_TOOLS,
    _ENTITY_FIELDS,
    _OBSERVABLE_TYPES,
    _PROVIDER,
    _RELATIONSHIP_FIELDS,
    _SCO_ENTITY_FIELDS,
    _SDO_ENTITY_FIELDS,
    _SDO_MUTATIONS,
    _STIX_PATTERN_MAP,
    _UI_DRAFT_UNUSABLE_MSG,
    ABSTRACT_STIX_CORE_OBJECT,
    ABSTRACT_STIX_CYBER_OBSERVABLE,
    ABSTRACT_STIX_DOMAIN_OBJECT,
    ABSTRACT_STIX_META_OBJECT,
    ENTITY_TYPE_USER,
    INSTANCE_REGARDING_OF,
    LISTING_CONTAINER_ITEM_TEXT_LIMIT,
    RELATIVE_DATE_RE,
    RULE_PREFIX,
    _build_stix_pattern,
    _client,
    _edges,
    _entity_read_call,
    _format_entity,
    _format_relationship,
    _graphql,
    _graphql_upload,
    _relationship_read_call,
    _request_headers,
    format_bulk_outcome,
)
from .access import _refuse_unapplied
from .markings import resolve_markings
from .workflow import (
    WORKFLOW_FILTER_KEY,
    describe_resolution,
    make_gql,
    resolve_status_names,
    status_names_in,
)

logger = logging.getLogger(__name__)

# What a listing tool's description says about a long text it cuts:
# the marker the agent will meet and the read that returns the text whole.
_ENTITY_LISTING_CUT_NOTE = (
    " A long description ends with '... [truncated, N more characters: read "
    "it in full with get_opencti_entity(entity_id=...)]'."
)
_RELATIONSHIP_LISTING_CUT_NOTE = (
    " A long description ends with '... [truncated, N more characters: read "
    "it in full with get_opencti_relationship(relationship_id=...)]'."
)

# The keys of OpenCTI's ``StixDomainObjectsOrdering`` and
# ``StixCyberObservablesOrdering`` GraphQL enums (opencti-graphql
# config/schema/opencti.graphql). OpenCTI refuses any other ``orderBy`` with a
# BAD_USER_INPUT error, so the listing tools resolve the value first.
_SDO_ORDERINGS: tuple[str, ...] = (
    "name",
    "entity_type",
    "created",
    "modified",
    "created_at",
    "updated_at",
    "refreshed_at",
    "published",
    "valid_from",
    "valid_until",
    "indicator_pattern",
    "x_opencti_workflow_id",
    "createdBy",
    "creator",
    "objectMarking",
    "_score",
    "first_seen",
    "last_seen",
    "attribute_count",
    "x_opencti_negative",
    "confidence",
    "first_observed",
    "last_observed",
    "number_observed",
    "number_seen",
    "max_distinct_count",
    "incident_type",
    "severity",
    "priority",
    "rating",
    "context",
    "attribute_abstract",
    "opinion",
    "pattern_type",
    "report_types",
    "note_types",
    "channel_types",
    "x_opencti_base_severity",
    "event_types",
    "x_opencti_organization_type",
    "submitted",
    "product",
    "result_name",
    "operatingSystem",
    "x_opencti_cvss_base_severity",
    "pir_score",
    "last_pir_score_date",
)
_SCO_ORDERINGS: tuple[str, ...] = (
    "entity_type",
    "created_at",
    "updated_at",
    "observable_value",
    "objectMarking",
    "createdBy",
    "creator",
    "_score",
)
# Names a model commonly guesses, mapped to the closest key of the enum.
_ORDERING_ALIASES_COMMON: dict[str, str] = {
    "updated": "updated_at",
    "type": "entity_type",
    "author": "createdBy",
    "created_by": "createdBy",
    "marking": "objectMarking",
    "score": "_score",
    "relevance": "_score",
}
_SDO_ORDERING_ALIASES: dict[str, str] = {
    **_ORDERING_ALIASES_COMMON,
    "title": "name",
    "value": "name",
    "status": "x_opencti_workflow_id",
}
_SCO_ORDERING_ALIASES: dict[str, str] = {
    **_ORDERING_ALIASES_COMMON,
    "value": "observable_value",
    "name": "observable_value",
    "created": "created_at",
    "modified": "updated_at",
}


def _default_ordering(search: str | None) -> str:
    """The ``orderBy`` of a call that names none: relevance for a keyword search.

    OpenCTI ranks the matches of a ``search`` by ``_score`` (its own search
    page sorts by it); sending ``created_at`` instead returns the newest of
    possibly thousands of keyword matches, not the best ones. Without a
    keyword there is no score, so the newest come first.
    """
    return "_score" if (search or "").strip() else "created_at"


def _resolve_ordering(
    order_by: str | None,
    valid: tuple[str, ...],
    aliases: dict[str, str],
    enum_name: str,
    search: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(key, error)``: the enum key ``order_by`` names, or an error listing them.

    A key of the enum passes as is, an alias or a case variant is mapped, an
    empty value falls back to :func:`_default_ordering` (``_score`` with a
    *search*, else ``created_at``); anything else is refused before the call
    with the valid values, so the agent corrects it in one step.
    """
    raw = (order_by or "").strip()
    if not raw:
        return _default_ordering(search), None
    if raw in valid:
        return raw, None
    lowered = raw.lower()
    if lowered in aliases:
        return aliases[lowered], None
    for key in valid:
        if key.lower() == lowered:
            return key, None
    return raw, (f"Error: order_by '{raw}' is not a value of OpenCTI's {enum_name}. Valid values: {', '.join(valid)}.")


def _storage_url(base_url: str, kind: str, file_id: str) -> str:
    """An OpenCTI file URL: ``kind`` is ``get`` (download) or ``view`` (in the browser)."""
    return f"{base_url}/storage/{kind}/{quote(str(file_id), safe='')}"


#: The tool that reads an OpenCTI file's content. Not every surface offers it
#: (the platform MCP server does not, an agent's tool scope may drop it), so a
#: text that points at it does so only where it is offered.
_DOWNLOAD_FILE_TOOL = "download_opencti_file"


def _offers_file_download(surface: tuple[ToolDefinition, ...] | list[ToolDefinition] | None) -> bool:
    """Whether *surface* offers :data:`_DOWNLOAD_FILE_TOOL` under that exact name.

    ``None`` (outside an agent loop, nothing is known) offers nothing; a
    multi-instance surface offers only suffixed names, which this text does
    not spell.
    """
    return any(tool.name == _DOWNLOAD_FILE_TOOL for tool in surface or ())


def _container_full_description(file_download_offered: bool) -> str:
    """The ``get_opencti_container_full`` description for a surface."""
    files = (
        f"attached files (with IDs for download via {_DOWNLOAD_FILE_TOOL})"
        if file_download_offered
        else "attached files (with IDs and download links)"
    )
    return (
        "Get the full contents of an OpenCTI container (Report, Grouping, "
        "Note, Observed-Data, Opinion, Case-Incident, Case-Rfi, Case-Rft, "
        "Feedback) with all contained entities and relationships resolved. "
        "Returns the container header (name, description, content, author, "
        f"published date), {files}, then two sections: "
        "Entities (ID, type, representative name, description) and "
        "Relationships (ID, relationship type, description, from/to with "
        "ID, type and name). Use this to understand the full context of "
        "a report, case, or grouping."
    )


# ── Shared STIX core-object metadata (createdBy / markings / labels / refs) ──
# Every SDO / SCO / SRO ``*AddInput`` in OpenCTI accepts the same author,
# marking, label, organization and external-reference fields. They all expect
# **IDs** (internal or STIX), never display names. Centralised here so every
# create tool exposes them consistently (instead of each tool silently dropping
# TLP markings, labels and authorship).


def _apply_stix_metadata(
    input_data: dict[str, Any],
    *,
    created_by: str = "",
    object_marking: list[str] | None = None,
    object_label: list[str] | None = None,
    object_organization: list[str] | None = None,
    external_references: list[str] | None = None,
) -> None:
    """Attach common STIX core-object metadata to a mutation ``input`` dict.

    Only non-empty values are set, so the default behaviour of every caller is
    unchanged when the agent does not pass these optional fields.
    """
    if created_by:
        input_data["createdBy"] = created_by
    if object_marking:
        input_data["objectMarking"] = object_marking
    if object_label:
        input_data["objectLabel"] = object_label
    if object_organization:
        input_data["objectOrganization"] = object_organization
    if external_references:
        input_data["externalReferences"] = external_references


#: JSON-schema fragment exposing the shared metadata fields on every create
#: tool. Spread into each ToolDefinition ``properties`` with ``**``.
_STIX_METADATA_PROPS: dict[str, Any] = {
    "created_by": {
        "type": "string",
        "description": (
            "Author identity ID (createdBy) — the OpenCTI internal/STIX id of "
            "the authoring identity. ID, not a name."
        ),
        "default": "",
    },
    "object_marking": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Marking-definition IDs to apply (e.g. a TLP marking). IDs, not "
            "names."
        ),
    },
    "object_label": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Label IDs to attach. IDs, not names — use add_opencti_label for "
            "name-based (auto-creating) labelling."
        ),
    },
    "external_references": {
        "type": "array",
        "items": {"type": "string"},
        "description": "External-reference IDs to attach.",
    },
}

# STIX 2.1 ``standard_id`` pattern — ``<type>--<uuid>`` (e.g.
# ``intrusion-set--7e3b...``). Widget ``id`` / ``layout.i`` are bare UUIDs
# (no ``type--`` prefix), so they never match — only genuine entity references
# embedded in widget filters do.
_STIX_ID_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*--"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Relationship-like STIX objects cannot be resolved via ``stixCoreObject`` and
# are virtually never used as dashboard filter targets, so they are excluded
# from the dashboard existence check to avoid false "missing" verdicts.
_NON_CORE_STIX_PREFIXES = ("relationship--", "sighting--")


def _collect_dashboard_stix_ids(node: Any) -> set[str]:
    """Recursively collect every STIX ``standard_id`` referenced inside a
    dashboard widget map.

    Walks the entire widget structure (filter values such as ``regardingOf``
    ids, ``toId``, ``fromId``, ``objects`` …) and returns the set of STIX core
    object IDs that should resolve to a real entity in OpenCTI.
    """
    found: set[str] = set()
    if isinstance(node, str):
        if _STIX_ID_RE.match(node) and not node.startswith(_NON_CORE_STIX_PREFIXES):
            found.add(node)
    elif isinstance(node, dict):
        for value in node.values():
            found |= _collect_dashboard_stix_ids(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            found |= _collect_dashboard_stix_ids(item)
    return found


#: A dashboard export read from a file of the run (import_opencti_dashboard).
_MAX_DASHBOARD_FILE_BYTES = 10 * 1024 * 1024
# Merge participants: OpenCTI's backend imposes no count limit, so this mirrors
# the 2-4 range its own merge toolbar allows, keeping an irreversible action
# human-sized.
_MERGE_MAX_PARTICIPANTS = 4
# Distinct ids looked up by the preflight before the participant cap can be
# checked on resolved entities: aliases collapse, so the raw list may exceed the
# cap, but not by enough to fan out into an unbounded number of lookups.
_MERGE_MAX_PREFLIGHT_IDS = 2 * _MERGE_MAX_PARTICIPANTS


def _merge_cap_refusal(participants: int) -> str:
    return (
        f"❌ Refusing to merge {participants} entities at once (limit "
        f"{_MERGE_MAX_PARTICIPANTS}, matching the OpenCTI merge "
        "toolbar). Merge cannot be undone - split this into smaller "
        "batches so each one stays reviewable."
    )


# JavaScript's ``String.prototype.trim()`` set (WhiteSpace + LineTerminator).
# Python's ``str.strip()`` differs: it also drops U+001C-U+001F and U+0085 and
# keeps U+FEFF.
_JS_TRIM_CHARS = (
    "\t\n\v\f\r \xa0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff"
    + "".join(map(chr, range(0x2000, 0x200B)))
)


def _label_identity(value: str) -> str:
    """The identity OpenCTI gives a label value.

    OpenCTI derives a label's standard_id from ``normalizeName(value)`` =
    ``value.toLowerCase().trim()``: two values are one label exactly when this
    matches. Never case folding, and never the accent folding the ``value``
    filter applies to its hits.
    """
    return (value or "").lower().strip(_JS_TRIM_CHARS)


def _match_label(edges: list[dict], label_name: str) -> dict | None:
    """The label among *edges* that OpenCTI holds as *label_name*.

    An exact value wins; otherwise the one with the same ``_label_identity``.
    Not ``casefold()``, which is broader (``straße`` == ``strasse``), and not
    the accent folding the lookup filter applies to its hits (``cafe`` for
    ``café``): OpenCTI keeps those apart.
    """
    for edge in edges:
        if edge.get("value") == label_name and edge.get("id"):
            return edge
    identity = _label_identity(label_name)
    for edge in edges:
        if _label_identity(str(edge.get("value") or "")) == identity and edge.get("id"):
            return edge
    return None


# ``label(id:)`` also takes a label NAME: OpenCTI resolves it to the standard_id
# of ``normalizeName(name)``, the same lookup ``labelAdd`` uses to return an
# existing label (``domain/label.js`` ``findById``, unchanged from 5.7 to 7.x).
# An id-shaped argument (UUID or STIX id) is loaded as an id instead.
_LABEL_BY_NAME_QUERY = """
query LabelByName($id: String!) {
    label(id: $id) { id value color }
}
"""

# The ``value`` filter folds case AND accents, so it can return several labels
# OpenCTI holds apart; its hits are the fallback and the near-match hints.
_LABEL_BY_VALUE_QUERY = """
query FindLabel($filters: FilterGroup) {
    labels(filters: $filters, first: 5) {
        edges { node { id value color } }
    }
}
"""


def _is_permission_failure(error: str) -> bool:
    """True for a GraphQL failure ``_describe_graphql_failure`` rendered as a refusal."""
    return "FORBIDDEN_ACCESS" in error or "permission denied" in error.lower()


async def _find_label(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    label_name: str,
    *,
    live: bool = False,
) -> tuple[dict | None, list[dict], str | None]:
    """The label OpenCTI holds as *label_name*, the folded hits, and an error.

    The identity lookup comes first: the folded ``value`` filter returns at most
    a page of hits in no set order, so with many accent variants it could miss
    the one label that matches. The filter runs only when the identity lookup
    finds no label with this identity (for instance a value that is itself
    id-shaped), and its hits are returned so a refused create can name them.
    """
    # ``live`` only when asked: the knowledge tools' lookups keep the call
    # shape their test doubles and the draft path expect.
    live_kw: dict[str, Any] = {"live": True} if live else {}
    data, err = await _graphql(
        client, base_url, headers, _LABEL_BY_NAME_QUERY, {"id": label_name}, **live_kw
    )
    if err:
        return None, [], err
    label = data.get("label")
    # An id-shaped name is loaded as an id, which can be ANOTHER label's: only
    # a label that is this name counts.
    if (
        isinstance(label, dict)
        and label.get("id")
        and _label_identity(str(label.get("value") or "")) == _label_identity(label_name)
    ):
        return label, [], None
    value_filter = {
        "mode": "and",
        "filters": [{"key": "value", "values": [label_name]}],
        "filterGroups": [],
    }
    data, err = await _graphql(
        client, base_url, headers, _LABEL_BY_VALUE_QUERY, {"filters": value_filter}, **live_kw
    )
    if err:
        return None, [], err
    edges = _edges(data, "labels")
    return _match_label(edges, label_name), edges, None


# Platform configuration and draft lifecycle documents of the label and draft
# tools (``list_opencti_labels``, ``create_opencti_label``,
# ``apply_opencti_draft``, ``delete_opencti_draft``).
_LABELS_LIST_QUERY = """
query ListLabels($search: String, $first: Int, $after: ID) {
    labels(search: $search, first: $first, after: $after, orderBy: value, orderMode: asc) {
        edges { node { id value color } }
        pageInfo { endCursor hasNextPage globalCount }
    }
}
"""

_LABEL_CREATE_MUTATION = """
mutation CreateLabel($input: LabelAddInput!) {
    labelAdd(input: $input) { id value color }
}
"""

_LABEL_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# The fields each action of manage_opencti_label reads; any other field is refused.
_LABEL_APPLIES: dict[str, frozenset[str]] = {
    "update": frozenset({"label", "value", "color"}),
    "delete": frozenset({"label"}),
}

# ``labelEdit`` (SETTINGS_SETLABELS): a label is no STIX core object, so
# ``stixCoreObjectEdit`` answers "Stix-Core-Object cannot be found" for it.
_LABEL_PATCH_MUTATION = """
mutation PatchLabel($id: ID!, $input: [EditInput]!) {
    labelEdit(id: $id) { fieldPatch(input: $input) { id value color } }
}
"""

_LABEL_DELETE_MUTATION = """
mutation DeleteLabel($id: ID!) {
    labelEdit(id: $id) { delete }
}
"""

# An internal id (UUID) or a label's STIX id (``label--<uuid>``).
_LABEL_ID_RE = re.compile(
    r"^(?:label--)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


async def _resolve_label(
    client: Any,
    base_url: str,
    headers: dict[str, str],
    label_ref: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """The label *label_ref* names (its id, STIX id or value), and an error.

    ``label(id:)`` loads an id and resolves a value; the label it answers is
    taken when the reference is its id, is id-shaped, or has the label's
    identity. Otherwise the value is matched as ``_find_label`` does (the
    lookup ``labelAdd`` uses). Read live: labels are never staged in a draft.
    """
    data, err = await _graphql(client, base_url, headers, _LABEL_BY_NAME_QUERY, {"id": label_ref}, live=True)
    if err:
        return None, err
    label = data.get("label")
    if (
        isinstance(label, dict)
        and label.get("id")
        and (
            label.get("id") == label_ref
            or _LABEL_ID_RE.match(label_ref)
            or _label_identity(str(label.get("value") or "")) == _label_identity(label_ref)
        )
    ):
        return label, None
    label, _hits, err = await _find_label(client, base_url, headers, label_ref, live=True)
    return label, err


_DRAFT_READ_QUERY = """
query ReadDraft($id: String!) {
    draftWorkspace(id: $id) { id name draft_status }
}
"""

_DRAFT_VALIDATE_MUTATION = """
mutation ValidateDraft($id: ID!) {
    draftWorkspaceValidate(id: $id) { id }
}
"""

_DRAFT_DELETE_MUTATION = """
mutation DeleteDraft($id: ID!) {
    draftWorkspaceDelete(id: $id)
}
"""


# A label or a marking is a ref relation, written through the ``relationAdd`` /
# ``relationDelete`` of an ``*Edit`` root. OpenCTI loads the element restricted
# to that root's own type (``patchElementWithRefRelationships`` ->
# ``storeLoadByIdWithRefs(.., { type })`` in
# ``domain/stixObjectOrStixRelationship.ts``), so ``stixDomainObjectEdit``
# answers "Element can not be loaded" for an observable or a relationship, in a
# draft or not. The element's kind picks the root, as OpenCTI's own label and
# marking views do: ``stixCoreObjectEdit`` for every SDO and SCO, the
# relationship roots for the rest.
_REF_TARGET_QUERY = """
query RefTarget($id: String!) {
    stixObjectOrStixRelationship(id: $id) {
        ... on BasicObject { id entity_type parent_types }
        ... on BasicRelationship { id entity_type parent_types }
    }
}
"""

_STIX_CORE_RELATIONSHIP = "stix-core-relationship"
_STIX_SIGHTING_RELATIONSHIP = "stix-sighting-relationship"
_ENTITY_TYPE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")

# What the result calls the element written through each root.
_REF_TARGET_NOUN = {
    "stixCoreObjectEdit": "entity",
    "stixCoreRelationshipEdit": "relationship",
    "stixSightingRelationshipEdit": "sighting",
}

# The query field, fragment and formatter that read an element back after a
# label is added (a sighting has no formatter here, so it is only named).
_REF_READ_BACK = {
    "stixCoreObjectEdit": ("stixCoreObject", _ENTITY_FIELDS, _format_entity),
    "stixCoreRelationshipEdit": (
        "stixCoreRelationship",
        _RELATIONSHIP_FIELDS,
        _format_relationship,
    ),
}


def _ref_edit_root(element: dict) -> str | None:
    """The ``*Edit`` root that writes a ref relation on *element*, if any."""
    parents = element.get("parent_types") or []
    if ABSTRACT_STIX_CORE_OBJECT in parents:
        return "stixCoreObjectEdit"
    if _STIX_CORE_RELATIONSHIP in parents:
        return "stixCoreRelationshipEdit"
    if element.get("entity_type") == _STIX_SIGHTING_RELATIONSHIP:
        return "stixSightingRelationshipEdit"
    return None


async def _resolve_ref_target(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    element_id: str,
    what: str,
) -> tuple[str, str | None]:
    """The edit root for *element_id*, and an error (then the root is empty).

    The lookup carries the request headers, so in a draft it sees the draft's
    elements. *what* names the ref relation in the refusal ("a label"). The
    refusals never echo *element_id*: the draft gate reads an error naming a
    "locked" draft as a dead one, and a model may pass a name as the id.
    """
    data, err = await _graphql(
        client, base_url, headers, _REF_TARGET_QUERY, {"id": element_id}
    )
    if err:
        return "", err
    element = data.get("stixObjectOrStixRelationship")
    if not isinstance(element, dict) or not element.get("id"):
        return "", (
            "Error: no entity, observable, relationship or sighting has this ID "
            "in OpenCTI. Nothing was changed."
        )
    root = _ref_edit_root(element)
    if root is None:
        # OpenCTI's own value: shown only when it is a plain type name.
        entity_type = str(element.get("entity_type") or "")
        kind = (
            f" (type {entity_type})"
            if _ENTITY_TYPE_NAME_RE.fullmatch(entity_type)
            else ""
        )
        return "", (
            f"Error: this element{kind} cannot carry {what}: only entities, "
            "observables, relationships and sightings can. Nothing was changed."
        )
    return root, None


def _ref_add_mutation(operation: str, root: str) -> str:
    return (
        f"mutation {operation}($id: ID!, $input: StixRefRelationshipAddInput!) {{\n"
        f"    {root}(id: $id) {{\n"
        "        relationAdd(input: $input) { id }\n"
        "    }\n"
        "}"
    )


def _ref_delete_mutation(operation: str, root: str) -> str:
    return (
        f"mutation {operation}($id: ID!, $toId: StixRef!, "
        "$relationship_type: String!) {\n"
        f"    {root}(id: $id) {{\n"
        "        relationDelete(toId: $toId, relationship_type: $relationship_type)"
        " { id }\n"
        "    }\n"
        "}"
    )


# External references rendered per history entry (a modification reference can
# carry many; the entry line must stay readable), and the length of each one.
_HISTORY_MAX_REFS = 3
_HISTORY_MAX_REF_CHARS = 200


def _history_ref_text(ref: dict) -> str:
    """One external reference of a history entry, on one bounded line."""
    parts = (ref.get("source_name"), ref.get("external_id"), ref.get("url"))
    text = " ".join(" ".join(str(part) for part in parts if part).split())
    if len(text) > _HISTORY_MAX_REF_CHARS:
        text = text[: _HISTORY_MAX_REF_CHARS - 3] + "..."
    return text


_MERGE_PREFLIGHT_QUERY = """
query MergePreflight($id: String!) {
    stixCoreObject(id: $id) {
        id
        standard_id
        entity_type
        representative { main }
    }
}
"""


def _merge_action_code(target_id: str, source_ids: list[str]) -> str:
    """Short stable code identifying one exact merge operation.

    Derived from the participants (source order-independent) so that a human's
    approval authorises *that* merge and nothing else: an approval for "merge B
    into A" cannot be replayed to justify merging D into C. Short enough for a
    human to include in a free-text answer.
    """
    payload = "|".join([target_id, *sorted(source_ids)])
    return "M-" + hashlib.sha256(payload.encode()).hexdigest()[:8].upper()


def _merge_label(obj: dict) -> str:
    """Short human-readable label for a merge participant.

    ``representative.main`` is OpenCTI's own display value for any Stix core
    object (name for SDOs, observable_value for SCOs), so it works without
    per-type fragments.
    """
    name = (obj.get("representative") or {}).get("main")
    return f"'{name}'" if name else f"entity {obj.get('id') or '?'}"


_filter_keys_schema_query_validation = """
query FilterKeysSchema {
filterKeysSchema {
    entity_type
    filters_schema {
    filterKey
    }
}
}
"""


async def _get_available_keys(
    base_url: str,
    headers: dict[str, str],
    timeout: int = 30,
) -> set[str]:
    async with _client(headers, timeout=timeout) as client:
        data, err = await _graphql(
            client, base_url, headers, _filter_keys_schema_query_validation
        )
    if err:
        raise RuntimeError(f"Failed to fetch filterKeysSchema: {err}")

    keys: set[str] = set()
    for entry in data.get("filterKeysSchema", []):
        for fs in entry.get("filters_schema", []):
            keys.add(fs["filterKey"])
    return keys


_FILTER_HINT_BUDGET_S = 10
# OpenCTI rejects an unknown filter key with UNSUPPORTED_ERROR, and with
# BAD_USER_INPUT_ERROR since OpenCTI#19096. Both stay accepted: deployments run both.
_FILTER_KEYS_ERROR_CODES = ("UNSUPPORTED_ERROR", "BAD_USER_INPUT_ERROR")
_REJECTED_KEYS_RE = re.compile(r'"keys": \[([^\]]*)')


def _close_keys(key: str, available: set[str]) -> list[str]:
    """Fuzzy matches plus keys that contain the rejected one (labels -> objectLabel)."""

    def norm(text: str) -> str:
        return text.lower().replace("_", "")

    needle = norm(key).removesuffix("s")
    contained = sorted(
        (a for a in available if len(needle) >= 4 and needle in norm(a)),
        key=lambda a: (len(a), a),
    )
    fuzzy = difflib.get_close_matches(key, available, n=3)
    return list(dict.fromkeys([*fuzzy, *contained]))[:3]


async def _filter_key_hint(err: str, base_url: str, headers: dict[str, str]) -> str:
    """OpenCTI rejected filter keys: append close schema keys so the agent can retry.

    Only runs after a rejection, and OpenCTI stays the judge of what is valid.
    """
    if not any(code in err for code in _FILTER_KEYS_ERROR_CODES) or "filter keys" not in err:
        return err
    match = _REJECTED_KEYS_RE.search(err)
    rejected = re.findall(r'"([^"]+)"', match.group(1)) if match else []
    if not rejected:
        return err
    try:
        # wait_for bounds the whole step: safe_request can retry 429s for far longer.
        available = await asyncio.wait_for(
            _get_available_keys(base_url, headers, timeout=_FILTER_HINT_BUDGET_S),
            timeout=_FILTER_HINT_BUDGET_S,
        )
    except (RuntimeError, httpx.HTTPError, KeyError, TypeError, AttributeError, TimeoutError) as exc:
        logger.warning("filterKeysSchema hint skipped: %s", format_exception_message(exc))
        return err
    if not available:
        return err
    close = {k: _close_keys(k, available) for k in rejected}
    hints = " ".join(f"{k} -> {', '.join(m)}." for k, m in close.items() if m)
    examples = [
        k for k in ("objectLabel", "createdBy", "workflow_id", "x_opencti_cvss_base_score")
        if k in available
    ]
    e_g = f" (e.g. {', '.join(examples)})" if examples else ""
    parts = [f"Hint: OpenCTI filter keys include{e_g}."]
    if hints:
        parts.append(hints)
    parts.append("Correct the rejected keys and retry.")
    return f"{err}\n{' '.join(parts)}"


def register_knowledge(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    draft_id: str = "",
    autonomous: bool = False,
    direct_mutations: bool = False,
    hitl_approval_fn: Callable[[str], Awaitable[bool]] | None = None,
    persist_file_fn: Callable[[str, bytes, str], Awaitable[Any]] | None = None,
    live_writes: bool = False,
    gated_draft_validation: bool = False,
    case_draft_fn: Callable[[str], Awaitable[bool]] | None = None,
) -> list[ToolDefinition]:
    """Build the OpenCTI STIX knowledge, draft, NLQ-filter and dev tools.

    ``base_url`` and ``headers`` (including any ``opencti-draft-id``) are
    prepared by ``build_tools`` in ``__init__``. ``draft_id`` mirrors the
    active draft scope header (empty when the user is not in a draft).

    ``autonomous`` is True for server-side runs with no interactive user
    (e.g. assignments). In that mode the draft gate auto-provisions a draft on
    the first mutation instead of telling the agent to ask the user — so a
    fully automated run creates the draft and performs the action.

    ``direct_mutations`` bypasses the draft gate entirely: knowledge mutations
    write straight to the live graph and tool descriptions carry no draft
    instructions. Used by the platform MCP servers (``/mcp/opencti``), which
    are stateless (a draft header set by one JSON-RPC request cannot persist
    to the next) and by decision expose direct queries/mutations only.

    ``hitl_approval_fn`` is supplied when the agent has human-in-the-loop
    enabled. It verifies against the run's durable ``human_input_history`` that
    a human approved a specific action code, letting ``merge_opencti_entities``
    escalate for approval in an autonomous run instead of refusing outright.
    Absent (None) means HITL is off, and merge stays refused there.

    ``live_writes`` is the integration's opt-in Live writes setting (off by
    default): the draft gate is skipped as with ``direct_mutations``, so
    knowledge changes reach the live graph without review in a draft, and
    ``apply_opencti_draft`` and ``create_opencti_label`` are offered. A draft
    the user has open in the UI still receives the changes. Off, no tool that
    writes outside a draft is registered and no agent-facing text names the
    setting: when the option is gone, it is gone from the tools.

    ``gated_draft_validation`` (#1480) offers ``apply_opencti_draft`` with Live
    writes off: the run's fleet declares a gate naming that tool (the CTEM
    loop's ``ctem.analyst_review``), so the validation of a draft the loop
    staged is a person's decision, or the fleet's policy, never the model's.

    ``case_draft_fn`` (#2089) answers whether a draft id holds the threat
    package a CTEM case records: with ``gated_draft_validation`` and Live
    writes off, ``delete_opencti_draft`` may discard that draft (a refused
    review) besides the drafts this build created, and no other.
    """
    _draft_id = draft_id
    _offer_apply = live_writes or gated_draft_validation
    # Drafts this build created (create_opencti_draft, the autonomous gate):
    # the only ones delete_opencti_draft may remove with the setting off.
    _created_drafts: set[str] = set()
    # A fleet gate deciding the draft validation (#2089) keeps the draft gate
    # with Live writes on: what that gate reviews must sit in a draft until
    # it is answered, never in the live graph.
    _skip_gate = direct_mutations or (live_writes and not gated_draft_validation)


    async def validate_nlq_filter(
        filter_str: str,
    ) -> dict:
        """Validate that the provided NLQ filter string is correctly formatted and uses valid filter keys."""

        # ---------------------------------------------------------------------------
        # ---------------------------------------------------------------------------
        # Filter Validation Logic
        # ---------------------------------------------------------------------------
        # ---------------------------------------------------------------------------

        def _is_valid_date(s: str) -> bool:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.timestamp() == 0:
                    return False
                ms = dt.microsecond // 1000
                roundtrip = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"
                return roundtrip == s
            except (ValueError, AttributeError):
                return False


        def _is_not_empty(value: Any) -> bool:
            if value is None:
                return False
            return not (isinstance(value, (list, str)) and len(value) == 0)


        def _is_filter_format_correct(f: dict) -> bool:
            return bool(f.get("key") and _is_not_empty(f["key"]))


        def _is_filter_group_format_correct(fg: dict) -> bool:
            mode = fg.get("mode")
            filters = fg.get("filters")
            filter_groups = fg.get("filterGroups")
            if mode not in ("and", "or"):
                return False
            if not isinstance(filters, list):
                return False
            if not all(_is_filter_format_correct(f) for f in filters):
                return False
            if not isinstance(filter_groups, list):
                return False
            return all(_is_filter_group_format_correct(g) for g in filter_groups)


        def _check_filter_group_values_syntax(fg: dict) -> None:
            for f in fg.get("filters", []):
                if f.get("operator") == "within":
                    values = f.get("values", [])
                    if len(values) != 2:
                        raise ValueError(
                            f'A filter with "within" operator must have 2 values: {f}'
                        )
                    if any(v is None or v == "" for v in values):
                        raise ValueError(
                            f'A filter with "within" operator must have 2 values: {f}'
                        )
                    for v in values:
                        if not RELATIVE_DATE_RE.match(str(v)) and not _is_valid_date(str(v)):
                            raise ValueError(
                                "The values for filter with 'within' operator are not valid: "
                                f"you should provide a datetime or a valid relative date. {f}"
                            )
            for sub in fg.get("filterGroups", []):
                _check_filter_group_values_syntax(sub)


        def _is_filter_group_not_empty(fg: dict | None) -> bool:
            if fg is None:
                return False
            filters = fg.get("filters") or []
            filter_groups = fg.get("filterGroups") or []
            return len(filters) > 0 or len(filter_groups) > 0


        def _extract_filter_keys(fg: dict) -> list[str]:
            keys: list[str] = []
            for f in fg.get("filters", []):
                k = f.get("key", [])
                if isinstance(k, list):
                    keys.extend(k)
                else:
                    keys.append(k)
            for sub in fg.get("filterGroups", []):
                keys.extend(_extract_filter_keys(sub))
            return keys


        def _check_filter_keys(filter_group: dict, available_keys: set[str]) -> None:
            root_keys = [k.split(".")[0] for k in _extract_filter_keys(filter_group)]
            incorrect = [
                k for k in root_keys
                if k not in available_keys and not k.startswith(RULE_PREFIX)
            ]
            if incorrect:
                raise ValueError(
                    f"Incorrect filter keys not existing in any schema definition: {incorrect}"
                )


        def check_filters_format(filter_group: dict) -> None:
            if not _is_filter_group_format_correct(filter_group):
                raise ValueError(f"Incorrect filters format: {filter_group}")
            _check_filter_group_values_syntax(filter_group)


        async def check_filters_validity(
            filter_group: dict,
            base_url: str,
            headers: dict[str, str],
            *,
            no_filters_checking: bool = False,
        ) -> None:
            check_filters_format(filter_group)
            if not no_filters_checking and _is_filter_group_not_empty(filter_group):
                available_keys = await _get_available_keys(base_url, headers)
                _check_filter_keys(filter_group, available_keys)

        # ---------------------------------------------------------------------------
        # ---------------------------------------------------------------------------
        # Filters Entity IDs Extraction & Replacement Logic
        # ---------------------------------------------------------------------------
        # ---------------------------------------------------------------------------



        _filter_keys_schema_query_id_mapping = """
        query FilterKeysSchema {
        filterKeysSchema {
            entity_type
            filters_schema {
            filterKey
            filterDefinition {
                filterKey
                type
                elementsForFilterValuesSearch
            }
            }
        }
        }
        """

        _sub_types_query = """
        query SubTypes($type: String!) {
        subTypes(type: $type, first: 500) {
            edges { node { id label } }
        }
        }
        """

        _stix_core_objects_query = """
        query StixCoreObjects($filters: FilterGroup, $search: String) {
        stixCoreObjects(
            first: 10,
            filters: $filters,
            search: $search,
            orderBy: _score,
            orderMode: desc
        ) {
            edges { node { id } }
        }
        }
        """

        _users_query = """
        query Users($filters: FilterGroup, $search: String) {
        users(
            first: 10,
            filters: $filters,
            search: $search,
            orderBy: _score,
            orderMode: desc
        ) {
            edges { node { id } }
        }
        }
        """

        _stix_meta_objects_query = """
        query StixMetaObjects($filters: FilterGroup, $search: String) {
        stixMetaObjects(
            first: 10,
            filters: $filters,
            search: $search,
            orderBy: _score,
            orderMode: desc
        ) {
            edges { node { id } }
        }
        }
        """


        async def _fetch_sub_types(
            base_url: str,
            headers: dict[str, str],
            abstract_type: str,
        ) -> set[str]:
            async with _client(headers, timeout=30) as client:
                data, err = await _graphql(
                    client, base_url, headers,
                    _sub_types_query,
                    {"type": abstract_type},
                )
            if err:
                raise RuntimeError(f"Failed to fetch subTypes for {abstract_type}: {err}")
            return {
                edge["node"]["id"]
                for edge in data.get("subTypes", {}).get("edges", [])
            }


        async def _build_stix_core_object_types(
            base_url: str,
            headers: dict[str, str],
        ) -> set[str]:
            sco_types = await _fetch_sub_types(base_url, headers, ABSTRACT_STIX_CYBER_OBSERVABLE)
            sdo_types = await _fetch_sub_types(base_url, headers, ABSTRACT_STIX_DOMAIN_OBJECT)
            return sco_types | sdo_types | {
                ABSTRACT_STIX_CORE_OBJECT,
                ABSTRACT_STIX_DOMAIN_OBJECT,
                ABSTRACT_STIX_CYBER_OBSERVABLE,
            }


        async def _build_stix_meta_object_types(
            base_url: str,
            headers: dict[str, str],
        ) -> set[str]:
            meta_types = await _fetch_sub_types(base_url, headers, ABSTRACT_STIX_META_OBJECT)
            meta_types.add(ABSTRACT_STIX_META_OBJECT)
            return meta_types


        def _is_stix_core_object(entity_type: str, sco_types: set[str]) -> bool:
            return entity_type in sco_types


        def _is_stix_meta_object(entity_type: str, meta_types: set[str]) -> bool:
            return entity_type in meta_types


        def _filter_key_matches(f: dict, keys: list[str]) -> bool:
            f_keys = f.get("key", [])
            if isinstance(f_keys, str):
                f_keys = [f_keys]
            return any(k in keys for k in f_keys)


        def _extract_filter_group_values(
            fg: dict,
            keys: list[str] | None = None,
        ) -> list[str]:
            filters: list[dict] = fg.get("filters", [])
            filter_groups: list[dict] = fg.get("filterGroups", [])

            matched_filters: list[dict]
            if keys is not None:
                matched_filters = [f for f in filters if _filter_key_matches(f, keys)]
            else:
                matched_filters = list(filters)

            ids: list[str] = []
            for f in matched_filters:
                f_keys = f.get("key", [])
                if isinstance(f_keys, str):
                    f_keys = [f_keys]
                if INSTANCE_REGARDING_OF in f_keys:
                    for v in f.get("values", []):
                        if isinstance(v, dict) and v.get("key") == "id":
                            ids.extend(v.get("values", []))
                else:
                    ids.extend(f.get("values", []))

            for sub in filter_groups:
                ids.extend(_extract_filter_group_values(sub, keys))

            return list(set(ids))


        def _filters_entity_ids_mapping_result(
            input_filters: dict,
            keys_to_replace: list[str],
            values_ids_map: dict[str, str | None],
        ) -> dict:
            filters: list[dict] = input_filters.get("filters", [])
            filter_groups: list[dict] = input_filters.get("filterGroups", [])

            has_content = (len(filters) > 0 or len(filter_groups) > 0)
            if not has_content:
                return dict(input_filters)

            new_filters: list[dict] = []
            for f in filters:
                f_keys = f.get("key", [])
                key = f_keys[0] if isinstance(f_keys, list) and f_keys else f_keys

                if key in keys_to_replace:
                    if key == INSTANCE_REGARDING_OF:
                        id_values: list[str] = []
                        for v in f.get("values", []):
                            if isinstance(v, dict) and v.get("key") == "id":
                                id_values.extend(v.get("values", []))

                        resolved = [values_ids_map[v] for v in id_values if values_ids_map.get(v)]
                        non_id_values = [
                            v for v in f.get("values", [])
                            if not (isinstance(v, dict) and v.get("key") == "id")
                        ]
                        if resolved:
                            f = {**f, "values": [*non_id_values, {"key": "id", "values": resolved}]}
                        else:
                            f = {**f, "values": non_id_values}
                    else:
                        resolved_values = [
                            values_ids_map[v] for v in f.get("values", [])
                            if values_ids_map.get(v)
                        ]
                        f = {**f, "values": resolved_values}

                operator = f.get("operator")
                if len(f.get("values", [])) == 0 and operator and operator not in ("nil", "not_nil"):
                    continue
                new_filters.append(f)

            new_filter_groups = [
                _filters_entity_ids_mapping_result(fg, keys_to_replace, values_ids_map)
                for fg in filter_groups
            ]

            return {**input_filters, "filters": new_filters, "filterGroups": new_filter_groups}


        def _build_entity_types_filter(entity_types: list[str]) -> dict:
            return {
                "mode": "and",
                "filters": [
                    {
                        "key": ["entity_type"],
                        "values": entity_types,
                        "operator": "eq",
                        "mode": "or",
                    }
                ],
                "filterGroups": [],
            }


        async def _resolve_values_ids_for_entity_types(
            base_url: str,
            headers: dict[str, str],
            values_to_resolve: list[str],
            entity_types: list[str],
            sco_types: set[str],
            meta_types: set[str],
        ) -> tuple[list[tuple[str, str | None]], list[str]]:
            not_resolved: list[str] = []
            map_content: list[tuple[str, str | None]] = []
            entity_types_filter = _build_entity_types_filter(entity_types)

            for value in values_to_resolve:
                result_ids: list[str] = []

                if all(_is_stix_core_object(t, sco_types) for t in entity_types):
                    async with _client(headers, timeout=30) as client:
                        data, err = await _graphql(
                            client, base_url, headers,
                            _stix_core_objects_query,
                            {"filters": entity_types_filter, "search": value},
                        )
                    if not err:
                        result_ids = [
                            edge["node"]["id"]
                            for edge in data.get("stixCoreObjects", {}).get("edges", [])
                        ]

                elif len(entity_types) == 1 and ENTITY_TYPE_USER in entity_types:
                    async with _client(headers, timeout=30) as client:
                        data, err = await _graphql(
                            client, base_url, headers,
                            _users_query,
                            {"filters": entity_types_filter, "search": value},
                        )
                    if not err:
                        result_ids = [
                            edge["node"]["id"]
                            for edge in data.get("users", {}).get("edges", [])
                        ]

                elif all(_is_stix_meta_object(t, meta_types) for t in entity_types):
                    async with _client(headers, timeout=30) as client:
                        data, err = await _graphql(
                            client, base_url, headers,
                            _stix_meta_objects_query,
                            {"filters": entity_types_filter, "search": value},
                        )
                    if not err:
                        result_ids = [
                            edge["node"]["id"]
                            for edge in data.get("stixMetaObjects", {}).get("edges", [])
                            if edge and edge.get("node", {}).get("id")
                        ]

                if result_ids:
                    map_content.append((value, result_ids[0]))
                else:
                    not_resolved.append(value)
                    map_content.append((value, None))

            return map_content, not_resolved


        async def filters_entity_ids_mapping(
            base_url: str,
            headers: dict[str, str],
            filters: dict,
        ) -> dict:
            # 01. Fetch filter definitions + type sets
            async with _client(headers, timeout=30) as client:
                schema_data, schema_err = await _graphql(
                    client, base_url, headers, _filter_keys_schema_query_id_mapping
                )
            if schema_err:
                raise RuntimeError(f"Failed to fetch filterKeysSchema: {schema_err}")

            sco_types = await _build_stix_core_object_types(base_url, headers)
            meta_types = await _build_stix_meta_object_types(base_url, headers)

            sco_filter_defs: list[dict] = []
            for entry in schema_data.get("filterKeysSchema", []):
                if entry["entity_type"] == ABSTRACT_STIX_CORE_OBJECT:
                    for fs in entry.get("filters_schema", []):
                        sco_filter_defs.append(fs["filterDefinition"])
                    break

            ids_filter_defs = [d for d in sco_filter_defs if d.get("type") == "id"]

            # 02. Separate SCO keys from other entity type keys
            sco_ids_filter_keys = [
                d["filterKey"]
                for d in ids_filter_defs
                if all(_is_stix_core_object(t, sco_types) for t in d.get("elementsForFilterValuesSearch", []))
            ] + [INSTANCE_REGARDING_OF]

            sco_values_to_resolve = _extract_filter_group_values(filters, sco_ids_filter_keys)

            other_ids_resolutions: list[dict] = []
            for d in ids_filter_defs:
                if d.get("filterKey") == WORKFLOW_FILTER_KEY:
                    # Template ids (names and Status ids were resolved to them
                    # before this step): nothing left to resolve here.
                    continue
                elements = d.get("elementsForFilterValuesSearch", [])
                if any(not _is_stix_core_object(t, sco_types) for t in elements):
                    vals = _extract_filter_group_values(filters, [d["filterKey"]])
                    if vals:
                        other_ids_resolutions.append({
                            "filterKey": d["filterKey"],
                            "entityTypes": elements,
                            "valuesToResolve": vals,
                        })

            all_ids_filter_keys = sco_ids_filter_keys + [r["filterKey"] for r in other_ids_resolutions]

            # 03. Resolve values → IDs
            sco_map, not_resolved_scos = await _resolve_values_ids_for_entity_types(
                base_url, headers,
                sco_values_to_resolve,
                [ABSTRACT_STIX_CORE_OBJECT],
                sco_types,
                meta_types,
            )

            other_maps: list[tuple[str, str | None]] = []
            all_not_resolved: list[str] = list(not_resolved_scos)

            for resolution in other_ids_resolutions:
                m, nr = await _resolve_values_ids_for_entity_types(
                    base_url, headers,
                    resolution["valuesToResolve"],
                    resolution["entityTypes"],
                    sco_types,
                    meta_types,
                )
                other_maps.extend(m)
                all_not_resolved.extend(nr)

            values_ids_map: dict[str, str | None] = dict(sco_map)
            values_ids_map.update(dict(other_maps))

            # 04. Replace values in filters
            resolved_filters = _filters_entity_ids_mapping_result(
                filters, all_ids_filter_keys, values_ids_map
            )

            return {
                "filters": resolved_filters,
                "notResolvedValues": all_not_resolved,
            }

        # ---------------------------------------------------------------------------
        # ---------------------------------------------------------------------------
        # Run validation + mapping
        # ---------------------------------------------------------------------------
        # ---------------------------------------------------------------------------

        # Format filter to dict

        logger.debug("validate_nlq_filter: raw input: %s", filter_str)
        empty_filter_group = {"mode": "and", "filters": [], "filterGroups": []}

        try:
            repaired_filter_str = repair_json(filter_str, return_objects=False)
            if repaired_filter_str != filter_str:
                logger.warning(
                    "validate_nlq_filter: json_repair modified the input (repair workaround triggered)"
                )
            filter = json.loads(repaired_filter_str)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("validate_nlq_filter: failed to normalize filter JSON: %s", format_exception_message(exc))
            return {
                "filters": empty_filter_group,
                "notResolvedValues": [],
                "error": "OPENCTI_NLQ [JSON_INVALID]: Filter is not a valid JSON string.",
            }

        logger.debug("validate_nlq_filter: parsed filter: %s", filter)

        if not isinstance(filter, dict):
            logger.warning(
                "validate_nlq_filter: parsed filter has invalid type: %s",
                type(filter).__name__,
            )
            return {
                "filters": empty_filter_group,
                "notResolvedValues": [],
                "error": "OPENCTI_NLQ [JSON_NOT_OBJECT]: Filter must decode to a JSON object.",
            }

        if "filterGroups" not in filter:
            filter["filterGroups"] = []

        try:
            await check_filters_validity(
                filter,
                base_url,
                headers,
            )
        except (ValueError, RuntimeError, httpx.HTTPError, TypeError, AttributeError) as exc:
            logger.warning("validate_nlq_filter: validation failed: %s", format_exception_message(exc))
            return {
                "filters": empty_filter_group,
                "notResolvedValues": [],
                "error": "OPENCTI_NLQ [FILTER_VALIDATION_FAILED]: Filter validation failed.",
            }

        logger.debug("validate_nlq_filter: filter format and keys are valid")

        try:
            filter, resolved_statuses, status_err = await resolve_status_names(
                filter, make_gql(base_url, headers)
            )
        except (RuntimeError, httpx.HTTPError, TypeError, AttributeError) as exc:
            logger.warning("validate_nlq_filter: status resolution failed: %s", format_exception_message(exc))
            resolved_statuses, status_err = {}, "Workflow status names could not be resolved."
        if status_err:
            return {
                "filters": empty_filter_group,
                "notResolvedValues": status_names_in(filter),
                "error": f"OPENCTI_NLQ [STATUS_NOT_FOUND]: {status_err}",
            }

        try:
            mapping_result = await filters_entity_ids_mapping(
                base_url,
                headers,
                filter,
            )
        except (ValueError, RuntimeError, httpx.HTTPError, TypeError, AttributeError) as exc:
            logger.warning("validate_nlq_filter: entity ID mapping failed: %s", format_exception_message(exc))
            return {
                "filters": empty_filter_group,
                "notResolvedValues": [],
                "error": "OPENCTI_NLQ [ENTITY_MAPPING_FAILED]: Entity ID mapping failed.",
            }

        not_resolved = mapping_result.get("notResolvedValues", [])

        if not_resolved:
            logger.info(
                "validate_nlq_filter: %d value(s) could not be resolved to entity IDs: %s",
                len(not_resolved),
                not_resolved,
            )
        else:
            logger.debug("validate_nlq_filter: all entity values resolved successfully")

        logger.debug("validate_nlq_filter: returning resolved filter: %s", mapping_result["filters"])

        final_output = {
            "filters": mapping_result["filters"],
            "notResolvedValues": not_resolved
        }
        if resolved_statuses:
            final_output["resolvedStatusNames"] = resolved_statuses

        return final_output

    # ── 1. Search ─────────────────────────────────────────────────────

    async def search_opencti(
        search: str,
        types: list[str] | None = None,
        limit: int = 10,
    ) -> str:
        """Search for entities in OpenCTI by keyword."""
        if (routed := await _label_type_route(types, search, limit)) is not None:
            return routed
        query = (
            "query SearchEntities($types: [String], $search: String, "
            "$first: Int, $orderBy: StixCoreObjectsOrdering, $orderMode: OrderingMode) {"
            "  stixCoreObjects(types: $types, search: $search, first: $first, "
            "    orderBy: $orderBy, orderMode: $orderMode) {"
            "    edges { node {" + _ENTITY_FIELDS + "} }"
            "    pageInfo { globalCount }"
            "  }"
            "}"
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {
                    "search": search,
                    "types": types,
                    "first": min(limit, 50),
                    "orderBy": _default_ordering(search),
                    "orderMode": "desc",
                },
            )
        if err:
            return err
        nodes = _edges(data, "stixCoreObjects")
        total = (
            data.get("stixCoreObjects", {})
            .get("pageInfo", {})
            .get("globalCount", len(nodes))
        )
        return format_entity_list(
            nodes,
            partial(_format_entity, base_url=base_url),
            noun="entity",
            total=total,
            empty_msg=f"No entities found for '{search}'.",
        )

    # ── 2. Get entity by ID ───────────────────────────────────────────

    async def get_opencti_entity(entity_id: str) -> str:
        """Get full details of a specific entity by its OpenCTI or STIX ID."""
        query = (
            "query GetEntity($id: String!) {  stixCoreObject(id: $id) {"
            + _ENTITY_FIELDS
            + "}}"
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"id": entity_id},
            )
        if err:
            return err
        entity = data.get("stixCoreObject")
        if not entity:
            return f"Entity '{entity_id}' not found."
        return _format_entity(entity, base_url, full_text=True)

    # ── 3. Get relationship by ID ────────────────────────────────────

    async def get_opencti_relationship(relationship_id: str) -> str:
        """Get full details of a specific relationship by its OpenCTI or STIX ID."""
        query = (
            "query GetRelationship($id: String!) {  stixCoreRelationship(id: $id) {"
            + _RELATIONSHIP_FIELDS
            + "}}"
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"id": relationship_id},
            )
        if err:
            return err
        rel = data.get("stixCoreRelationship")
        if not rel:
            return f"Relationship '{relationship_id}' not found."
        return _format_relationship(rel, base_url, full_text=True)

    # ── 4. List STIX domain objects ───────────────────────────────────

    async def list_opencti_entities(
        types: list[str] | None = None,
        search: str = "",
        limit: int = 15,
        order_by: str = "",
        order_mode: str = "desc",
        filters: str = "",
    ) -> str | ToolResult:
        """List STIX domain objects, optionally filtered by type and search.

        The listed objects also come back structured (each record as OpenCTI
        answered it, id, type and name always present, ``[]`` when none) for
        an assignment's gate check and for a ``to_workset`` JSON split, one
        workset item per object.
        """
        if (routed := await _label_type_route(types, search, limit)) is not None:
            return routed
        order_by, order_err = _resolve_ordering(
            order_by, _SDO_ORDERINGS, _SDO_ORDERING_ALIASES, "StixDomainObjectsOrdering", search
        )
        if order_err:
            return order_err
        query = (
            "query ListSDOs($types: [String], $filters: FilterGroup, "
            "$search: String, $first: Int, $orderBy: StixDomainObjectsOrdering, "
            "$orderMode: OrderingMode) {"
            "  stixDomainObjects(types: $types, filters: $filters, search: $search, "
            "    first: $first, orderBy: $orderBy, orderMode: $orderMode) {"
            "    edges { node {" + _SDO_ENTITY_FIELDS + "} }"
            "    pageInfo { globalCount }"
            "  }"
            "}"
        )
        variables: dict[str, Any] = {
            "types": types,
            "search": search or None,
            "first": min(limit, 50),
            "orderBy": order_by,
            "orderMode": order_mode,
        }
        status_note = ""
        if filters:
            try:
                variables["filters"] = json.loads(filters)
            except json.JSONDecodeError:
                return "Error: 'filters' must be a valid JSON string (OpenCTI FilterGroup format)."
            if isinstance(variables["filters"], dict):
                resolved_fg, resolved_statuses, status_err = await resolve_status_names(
                    variables["filters"], make_gql(base_url, headers), types
                )
                if status_err:
                    return f"Error: {status_err}"
                variables["filters"] = resolved_fg
                status_note = describe_resolution(resolved_statuses)
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(client, base_url, headers, query, variables)
        if err:
            return await _filter_key_hint(err, base_url, headers) if filters else err
        nodes = _edges(data, "stixDomainObjects")
        total = (
            data.get("stixDomainObjects", {})
            .get("pageInfo", {})
            .get("globalCount", len(nodes))
        )
        text = format_entity_list(
            nodes,
            partial(_format_entity, base_url=base_url),
            noun="domain object",
            total=total,
            empty_msg="No domain objects found.",
        )
        if status_note:
            text = f"{status_note}\n{text}"
        listed = [
            {**node, "id": node.get("id"), "entity_type": node.get("entity_type"), "name": node.get("name")}
            for node in nodes
        ]
        return ToolResult(tool_call_id="", content=text, structured=listed)

    # ── 5. List STIX cyber observables ────────────────────────────────

    async def list_opencti_observables(
        types: list[str] | None = None,
        search: str = "",
        limit: int = 15,
        order_by: str = "",
        order_mode: str = "desc",
    ) -> str:
        """List STIX cyber observables, optionally filtered by type and search."""
        order_by, order_err = _resolve_ordering(
            order_by, _SCO_ORDERINGS, _SCO_ORDERING_ALIASES, "StixCyberObservablesOrdering", search
        )
        if order_err:
            return order_err
        query = (
            "query ListSCOs($types: [String], $search: String, "
            "$first: Int, $orderBy: StixCyberObservablesOrdering, "
            "$orderMode: OrderingMode) {"
            "  stixCyberObservables(types: $types, search: $search, "
            "    first: $first, orderBy: $orderBy, orderMode: $orderMode) {"
            "    edges { node {" + _SCO_ENTITY_FIELDS + "} }"
            "    pageInfo { globalCount }"
            "  }"
            "}"
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {
                    "types": types,
                    "search": search or None,
                    "first": min(limit, 50),
                    "orderBy": order_by,
                    "orderMode": order_mode,
                },
            )
        if err:
            return err
        nodes = _edges(data, "stixCyberObservables")
        total = (
            data.get("stixCyberObservables", {})
            .get("pageInfo", {})
            .get("globalCount", len(nodes))
        )
        return format_entity_list(
            nodes,
            partial(_format_entity, base_url=base_url),
            noun="observable",
            total=total,
            empty_msg="No observables found.",
        )

    # ── 6. List STIX core relationships ───────────────────────────────

    async def list_opencti_relationships(
        entity_id: str,
        relationship_type: str = "",
        from_types: list[str] | None = None,
        to_types: list[str] | None = None,
        limit: int = 20,
    ) -> str:
        """List relationships of a specific entity."""
        query = (
            "query ListRels($fromOrToId: [String], "
            "$relationship_type: [String], $fromTypes: [String], "
            "$toTypes: [String], $first: Int, "
            "$orderBy: StixCoreRelationshipsOrdering, $orderMode: OrderingMode) {"
            "  stixCoreRelationships(fromOrToId: $fromOrToId, "
            "    relationship_type: $relationship_type, fromTypes: $fromTypes, "
            "    toTypes: $toTypes, first: $first, "
            "    orderBy: $orderBy, orderMode: $orderMode) {"
            "    edges { node {" + _RELATIONSHIP_FIELDS + "} }"
            "    pageInfo { globalCount }"
            "  }"
            "}"
        )
        variables: dict[str, Any] = {
            "fromOrToId": [entity_id],
            "first": min(limit, 50),
            "orderBy": "created_at",
            "orderMode": "desc",
        }
        if relationship_type:
            variables["relationship_type"] = [relationship_type]
        if from_types:
            variables["fromTypes"] = from_types
        if to_types:
            variables["toTypes"] = to_types
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(client, base_url, headers, query, variables)
        if err:
            return err
        nodes = _edges(data, "stixCoreRelationships")
        total = (
            data.get("stixCoreRelationships", {})
            .get("pageInfo", {})
            .get("globalCount", len(nodes))
        )
        return format_entity_list(
            nodes,
            partial(_format_relationship, base_url=base_url),
            noun="relationship",
            total=total,
            empty_msg=f"No relationships found for entity '{entity_id}'.",
        )

    # ── 7. Create STIX core relationship ──────────────────────────────

    async def create_opencti_relationship(
        from_id: str,
        to_id: str,
        relationship_type: str,
        description: str = "",
        confidence: int | None = None,
        start_time: str = "",
        stop_time: str = "",
        created_by: str = "",
        object_marking: list[str] | None = None,
        object_label: list[str] | None = None,
        external_references: list[str] | None = None,
        kill_chain_phases: list[str] | None = None,
    ) -> str:
        """Create a STIX core relationship between two entities."""
        # Note: from/to return StixObjectOrStixRelationshipOrCreator union.
        # ``name`` is NOT on the StixDomainObject interface — resolve it
        # via individual SDO type fragments (same pattern as _RELATIONSHIP_FIELDS).
        query = """
        mutation CreateRelationship($input: StixCoreRelationshipAddInput!) {
            stixCoreRelationshipAdd(input: $input) {
                id standard_id entity_type relationship_type
                from {
                    ... on BasicObject { id entity_type }
                    ... on StixCyberObservable { observable_value }
                    ... on AttackPattern { name }
                    ... on Campaign { name }
                    ... on CourseOfAction { name }
                    ... on Individual { name }
                    ... on Organization { name }
                    ... on Sector { name }
                    ... on System { name }
                    ... on Indicator { name }
                    ... on Infrastructure { name }
                    ... on IntrusionSet { name }
                    ... on Position { name }
                    ... on City { name }
                    ... on Country { name }
                    ... on Region { name }
                    ... on Malware { name }
                    ... on ThreatActor { name }
                    ... on Tool { name }
                    ... on Vulnerability { name }
                    ... on Incident { name }
                    ... on Event { name }
                    ... on Channel { name }
                    ... on Narrative { name }
                    ... on DataComponent { name }
                    ... on DataSource { name }
                    ... on Case { name }
                }
                to {
                    ... on BasicObject { id entity_type }
                    ... on StixCyberObservable { observable_value }
                    ... on AttackPattern { name }
                    ... on Campaign { name }
                    ... on CourseOfAction { name }
                    ... on Individual { name }
                    ... on Organization { name }
                    ... on Sector { name }
                    ... on System { name }
                    ... on Indicator { name }
                    ... on Infrastructure { name }
                    ... on IntrusionSet { name }
                    ... on Position { name }
                    ... on City { name }
                    ... on Country { name }
                    ... on Region { name }
                    ... on Malware { name }
                    ... on ThreatActor { name }
                    ... on Tool { name }
                    ... on Vulnerability { name }
                    ... on Incident { name }
                    ... on Event { name }
                    ... on Channel { name }
                    ... on Narrative { name }
                    ... on DataComponent { name }
                    ... on DataSource { name }
                    ... on Case { name }
                }
            }
        }
        """
        input_data: dict[str, Any] = {
            "fromId": from_id,
            "toId": to_id,
            "relationship_type": relationship_type,
        }
        if description:
            input_data["description"] = description
        if confidence is not None:
            input_data["confidence"] = confidence
        if start_time:
            input_data["start_time"] = start_time
        if stop_time:
            input_data["stop_time"] = stop_time
        if kill_chain_phases:
            input_data["killChainPhases"] = kill_chain_phases
        _apply_stix_metadata(
            input_data,
            created_by=created_by,
            object_marking=object_marking,
            object_label=object_label,
            external_references=external_references,
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"input": input_data},
            )
        if err:
            return err
        rel = data.get("stixCoreRelationshipAdd", {})
        from_obj = rel.get("from", {}) or {}
        to_obj = rel.get("to", {}) or {}
        fn = (
            from_obj.get("name")
            or from_obj.get("observable_value")
            or from_obj.get("id", "?")
        )
        tn = (
            to_obj.get("name")
            or to_obj.get("observable_value")
            or to_obj.get("id", "?")
        )
        return (
            f"Relationship created: [{from_obj.get('entity_type', '?')}] {fn}"
            f" --{rel.get('relationship_type', relationship_type)}-->"
            f" [{to_obj.get('entity_type', '?')}] {tn}"
            f" (ID: {rel.get('id', 'unknown')})"
        )

    # ── 8. Create STIX cyber observable ───────────────────────────────

    async def create_opencti_observable(
        type: str,
        value: str,
        x_opencti_score: int | None = None,
        x_opencti_description: str = "",
        create_indicator: bool = False,
        created_by: str = "",
        object_marking: list[str] | None = None,
        object_label: list[str] | None = None,
        external_references: list[str] | None = None,
    ) -> str:
        """Create a STIX cyber observable in OpenCTI."""
        type_info = _OBSERVABLE_TYPES.get(type)
        if not type_info:
            supported = ", ".join(sorted(_OBSERVABLE_TYPES.keys()))
            return (
                f"Unsupported observable type: '{type}'. Supported types: {supported}"
            )
        var_name, input_type, field_name = type_info
        query = (
            f"mutation CreateObservable("
            f"  $type: String!,"
            f"  $x_opencti_score: Int,"
            f"  $x_opencti_description: String,"
            f"  $createIndicator: Boolean,"
            f"  $createdBy: String,"
            f"  $objectMarking: [String],"
            f"  $objectLabel: [String],"
            f"  $externalReferences: [String],"
            f"  ${var_name}: {input_type}"
            f") {{"
            f"  stixCyberObservableAdd("
            f"    type: $type,"
            f"    x_opencti_score: $x_opencti_score,"
            f"    x_opencti_description: $x_opencti_description,"
            f"    createIndicator: $createIndicator,"
            f"    createdBy: $createdBy,"
            f"    objectMarking: $objectMarking,"
            f"    objectLabel: $objectLabel,"
            f"    externalReferences: $externalReferences,"
            f"    {var_name}: ${var_name}"
            f"  ) {{"
            f"    id entity_type observable_value x_opencti_score"
            f"  }}"
            f"}}"
        )
        variables: dict[str, Any] = {
            "type": type,
            var_name: {
                field_name: int(value) if type == "Autonomous-System" else value
            },
            "createIndicator": create_indicator,
        }
        if x_opencti_score is not None:
            variables["x_opencti_score"] = x_opencti_score
        if x_opencti_description:
            variables["x_opencti_description"] = x_opencti_description
        if created_by:
            variables["createdBy"] = created_by
        if object_marking:
            variables["objectMarking"] = object_marking
        if object_label:
            variables["objectLabel"] = object_label
        if external_references:
            variables["externalReferences"] = external_references
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(client, base_url, headers, query, variables)
        if err:
            return err
        obs = data.get("stixCyberObservableAdd", {})
        return (
            f"Observable created: [{obs.get('entity_type', type)}]"
            f" {obs.get('observable_value', value)}"
            f" (ID: {obs.get('id', 'unknown')},"
            f" Score: {obs.get('x_opencti_score', 'N/A')})"
        )

    # ── 9. Create note ────────────────────────────────────────────────

    async def create_opencti_note(
        content: str,
        object_ids: list[str],
        attribute_abstract: str = "",
        note_types: list[str] | None = None,
        confidence: int | None = None,
        created_by: str = "",
        object_marking: list[str] | None = None,
        object_label: list[str] | None = None,
        external_references: list[str] | None = None,
    ) -> str:
        """Create an analyst note attached to one or more entities."""
        query = """
        mutation CreateNote($input: NoteAddInput!) {
            noteAdd(input: $input) { id entity_type attribute_abstract }
        }
        """
        input_data: dict[str, Any] = {
            "content": content,
            "note_types": note_types or ["internal-note"],
            "objects": object_ids,
        }
        if attribute_abstract:
            input_data["attribute_abstract"] = attribute_abstract
        else:
            input_data["attribute_abstract"] = content[:200]
        if confidence is not None:
            input_data["confidence"] = confidence
        _apply_stix_metadata(
            input_data,
            created_by=created_by,
            object_marking=object_marking,
            object_label=object_label,
            external_references=external_references,
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"input": input_data},
            )
        if err:
            return err
        note = data.get("noteAdd", {})
        return f"Note created (ID: {note.get('id', 'unknown')}) attached to {len(object_ids)} entity/entities."

    # ── 10. Update SDO field(s) ───────────────────────────────────────

    async def update_opencti_field(
        entity_id: str,
        key: str,
        value: str | list[str],
    ) -> str:
        """Update a field on a STIX domain object."""
        query = """
        mutation UpdateSDO($id: ID!, $input: [EditInput]!) {
            stixDomainObjectEdit(id: $id) {
                fieldPatch(input: $input) { id entity_type }
            }
        }
        """
        input_val = value if isinstance(value, list) else [value]
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {
                    "id": entity_id,
                    "input": [{"key": key, "value": input_val}],
                },
            )
        if err:
            return err
        result = data.get("stixDomainObjectEdit", {}).get("fieldPatch", {})
        return f"Updated '{key}' on [{result.get('entity_type', '?')}] {result.get('id', entity_id)}."

    # ── 11. Update relationship field(s) ─────────────────────────────

    async def update_opencti_relationship_field(
        relationship_id: str,
        key: str,
        value: str | list[str],
    ) -> str:
        """Update a field on a STIX core relationship."""
        query = """
        mutation UpdateRel($id: ID!, $input: [EditInput]!) {
            stixCoreRelationshipEdit(id: $id) {
                fieldPatch(input: $input) { id entity_type }
            }
        }
        """
        input_val = value if isinstance(value, list) else [value]
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {
                    "id": relationship_id,
                    "input": [{"key": key, "value": input_val}],
                },
            )
        if err:
            return err
        result = data.get("stixCoreRelationshipEdit", {}).get("fieldPatch", {})
        return f"Updated '{key}' on relationship {result.get('id', relationship_id)}."

    # ── 12. Delete entity ────────────────────────────────────────────

    async def delete_opencti_entity(entity_id: str) -> str:
        """Delete any STIX core object (domain object or observable)."""
        query = """
        mutation DeleteEntity($id: ID!) {
            stixCoreObjectEdit(id: $id) { delete }
        }
        """
        async with _client(headers, timeout=30) as client:
            _, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"id": entity_id},
            )
            if err and "cannot be found" in err.lower():
                hint = await _not_core_object_hint(client, str(entity_id or "").strip())
                if hint:
                    return hint
        if err:
            return err
        return f"Entity {entity_id} deleted."

    async def _not_core_object_hint(client: Any, object_id: str) -> str | None:
        """The tool to use when *object_id* is a label, which
        ``stixCoreObjectEdit`` cannot find: a label is no STIX core object.
        """
        if not object_id:
            return None
        data, err = await _graphql(client, base_url, headers, _LABEL_BY_NAME_QUERY, {"id": object_id}, live=True)
        label = None if err else data.get("label")
        if not (isinstance(label, dict) and label.get("id")):
            return None
        name = f"'{label.get('value')}' (id: `{label.get('id')}`)"
        if live_writes:
            tool = f"delete it with manage_opencti_label (action `delete`, label `{label.get('id')}`)"
        else:
            tool = (
                "no tool of this integration deletes a label; a person removes it in "
                "OpenCTI under Settings > Taxonomies > Labels"
            )
        return (
            f"Error: `{object_id}` is the label {name}, not a STIX core object, so "
            f"delete_opencti_entity cannot delete it: {tool}. Nothing was deleted."
        )

    # ── 12b. Merge same-type entities ────────────────────────────────
    #
    # Deliberately NOT added to _DRAFT_REQUIRED_TOOLS. OpenCTI's
    # ``stixEdit.merge`` ignores draft context entirely: ``stixObjectMerge`` ->
    # ``mergeEntities`` has no draft handling at all, and the draft id is only
    # used to scope Redis locks. Routing a merge through the draft gate would
    # therefore report "staged for human review" while irreversibly rewriting
    # the live graph -- OpenCTI's own UI disables merge inside a draft for
    # exactly that reason. A false sense of reviewability is worse than no
    # gate, so the guards here are explicit instead: autonomous runs are
    # refused, IDs must be passed verbatim (no name resolution in this tool),
    # and every participant is validated before anything is written.
    #
    # It is also intentionally absent from OPENCTI_MCP_TOOLS in
    # api/v1/mcp_platform.py, whose allow-list fails closed -- so the
    # ``direct_mutations`` MCP path cannot reach merge either.

    async def merge_opencti_entities(
        target_id: str,
        source_ids: list[str],
    ) -> str:
        """Merge same-type STIX entities into a target entity. Irreversible."""
        # An autonomous run has no interactive user, and OpenCTI cannot stage a
        # merge in a draft for later review. With human-in-the-loop enabled the
        # merge is escalated for explicit approval further down; without it
        # there is no human anywhere in the path, so refuse before spending any
        # network calls on a preflight whose result cannot be used.
        if autonomous and hitl_approval_fn is None:
            return (
                "❌ Merge refused: merging is irreversible and OpenCTI cannot "
                "stage it in a draft for review, so it is not available in "
                "autonomous runs unless the agent has human-in-the-loop "
                "enabled. Report the duplicate candidates you identified, with "
                "their IDs, and leave the merge to a human."
            )

        target = (target_id or "").strip()
        if not target:
            return "❌ `target_id` is required: the ID of the entity to keep."

        # An LLM may pass a single id as a bare string; treat it as one source
        # instead of letting GraphQL reject the shape.
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        if not isinstance(source_ids, list):
            return "❌ `source_ids` must be a list of OpenCTI entity IDs."

        sources: list[str] = []
        for raw in source_ids:
            sid = str(raw).strip()
            if sid and sid != target and sid not in sources:
                sources.append(sid)
        if not sources:
            return (
                "❌ `source_ids` must contain at least one entity ID different "
                "from `target_id`. OpenCTI rejects merging an entity into itself."
            )

        # The same entity answers to several ids (internal id, standard_id,
        # any stix id), so the cap is enforced on RESOLVED entities after the
        # preflight. Before it, only the number of lookups is bounded.
        if len(sources) + 1 > _MERGE_MAX_PREFLIGHT_IDS:
            return (
                f"❌ Refusing to look up {len(sources) + 1} distinct ids for one "
                f"merge (at most {_MERGE_MAX_PREFLIGHT_IDS}; the merge itself is "
                f"limited to {_MERGE_MAX_PARTICIPANTS} entities). Pass one id per "
                "entity - several ids of the same entity are not needed - and "
                "split larger merges into smaller batches."
            )

        # Preflight: every participant must exist, be readable, and share the
        # target's entity_type. OpenCTI enforces the type rule server-side too,
        # but its raw "Cannot merge entities of different types" error does not
        # say which entity was the odd one out.
        async with _client(headers, timeout=30) as client:
            resolved: dict[str, dict] = {}
            for eid in [target, *sources]:
                data, err = await _graphql(
                    client,
                    base_url,
                    headers,
                    _MERGE_PREFLIGHT_QUERY,
                    {"id": eid},
                )
                if err:
                    return err
                obj = data.get("stixCoreObject")
                if not obj:
                    return (
                        f"❌ Entity '{eid}' was not found, or is not readable "
                        "with your permissions. Merge aborted; nothing was "
                        "changed."
                    )
                resolved[eid] = obj

            target_obj = resolved[target]
            target_type = target_obj.get("entity_type")
            target_iid = target_obj.get("id") or target

            # Compare RESOLVED identities: an alias of the target is a
            # self-merge, and two aliases of one source are one source.
            source_iids: dict[str, str] = {}
            dropped: list[str] = []
            for sid in sources:
                iid = resolved[sid].get("id") or sid
                if iid == target_iid:
                    dropped.append(f"'{sid}' is the target entity itself")
                elif iid in source_iids.values():
                    dropped.append(
                        f"'{sid}' is the same entity as another source "
                        f"({_merge_label(resolved[sid])})"
                    )
                else:
                    source_iids[sid] = iid
            if not source_iids:
                return (
                    "❌ `source_ids` must contain at least one entity different "
                    "from `target_id`, and every id given resolves to the "
                    f"target ({'; '.join(dropped)}). OpenCTI rejects merging an "
                    "entity into itself. Merge aborted; nothing was changed."
                )
            sources = list(source_iids)
            if len(sources) + 1 > _MERGE_MAX_PARTICIPANTS:
                return _merge_cap_refusal(len(sources) + 1)
            dropped_note = (
                f"\nIgnored ids that duplicate a participant: {'; '.join(dropped)}."
                if dropped
                else ""
            )

            mismatched = [
                f"{_merge_label(resolved[sid])} is "
                f"{resolved[sid].get('entity_type')}"
                for sid in sources
                if resolved[sid].get("entity_type") != target_type
            ]
            if mismatched:
                return (
                    "❌ OpenCTI only merges entities of the same type. Target "
                    f"{_merge_label(target_obj)} is {target_type}, but "
                    f"{'; '.join(mismatched)}. Merge aborted; nothing was "
                    "changed."
                )

            # Autonomous run with HITL on: the merge only proceeds once a human
            # has approved THIS operation. Verified against the run's durable
            # human_input_history rather than taken on the model's word, and
            # bound to an action code so an approval cannot be replayed for a
            # different set of entities.
            if autonomous and hitl_approval_fn is not None:
                action_code = _merge_action_code(
                    target_iid, list(source_iids.values())
                )
                if not await hitl_approval_fn(action_code):
                    plan = "\n".join(
                        f"  - {_merge_label(resolved[sid])} "
                        f"({resolved[sid].get('entity_type')}, {source_iids[sid]})"
                        for sid in sources
                    )
                    return (
                        "⏸️ Human approval required before this merge. Nothing "
                        "has been changed yet.\n\n"
                        f"Keep: {_merge_label(target_obj)} "
                        f"({target_type}, {target_iid})\n"
                        f"Delete, merging into the above:\n{plan}{dropped_note}\n\n"
                        "Call `request_human_input` now, quoting the entities "
                        "above verbatim and telling the human their reply must "
                        f"START with `{HITL_APPROVAL_KEYWORD} {action_code}` to "
                        "confirm, naming no other code in that reply. Once they "
                        "have approved, call this tool again "
                        "with the same arguments and it will proceed. Any other "
                        "answer leaves the merge blocked - if the human "
                        "declines, do not retry, just report that the merge was "
                        "not approved."
                    )

            mutation = """
            mutation MergeEntities($id: ID!, $sourceIds: [String]!) {
                stixEdit(id: $id) {
                    merge(stixObjectsIds: $sourceIds) { id entity_type }
                }
            }
            """
            data, err = await _graphql(
                client,
                base_url,
                headers,
                mutation,
                {"id": target_iid, "sourceIds": list(source_iids.values())},
            )
        if err:
            return err
        merged = (data.get("stixEdit") or {}).get("merge") or {}
        kept_id = merged.get("id") or target_iid
        noun = "entity" if len(sources) == 1 else "entities"
        absorbed = ", ".join(_merge_label(resolved[sid]) for sid in sources)
        return (
            f"Merged {len(sources)} duplicate {target_type} {noun} into "
            f"{_merge_label(target_obj)} ({kept_id}).\n"
            f"Absorbed and now deleted: {absorbed}\n"
            "Aliases, relationships, container memberships and files from the "
            "absorbed entities were transferred to the kept entity. This "
            f"applied at once and cannot be undone.{dropped_note}"
        )

    # ── 13. Delete relationship ──────────────────────────────────────

    async def delete_opencti_relationship(relationship_id: str) -> str:
        """Delete a STIX core relationship."""
        query = """
        mutation DeleteRelationship($id: ID!) {
            stixCoreRelationshipEdit(id: $id) { delete }
        }
        """
        async with _client(headers, timeout=30) as client:
            _, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"id": relationship_id},
            )
        if err:
            return err
        return f"Relationship {relationship_id} deleted."

    # ── 14. Add label to an entity or relationship ───────────────────

    async def add_opencti_label(entity_id: str, label_name: str) -> str:
        """Add a label to an entity, observable, relationship or sighting.

        Looks the label up by name through ``label(id:)`` (not fuzzy search),
        falling back to the ``value`` filter (see ``_find_label``), before
        creating it. OpenCTI holds one label per
        ``normalizeName`` value (see ``_label_identity``), so an existing
        ``apt28`` is attached for ``APT28`` instead of being re-created -
        creating needs SETTINGS_SETLABELS, which attaching does not. The
        element is resolved before the label is created, so a missing element
        creates nothing, and its kind picks the edit root (``_ref_edit_root``).

        Returns the full updated entity or relationship so the caller can see
        the new state.
        """
        label_name = str(label_name or "").strip(_JS_TRIM_CHARS)
        if not label_name:
            return "Error: `label_name` is required."
        near_q = """
        query NearLabels($search: String) {
            labels(search: $search, first: 5) {
                edges { node { id value } }
            }
        }
        """
        create_q = """
        mutation CreateLabel($input: LabelAddInput!) {
            labelAdd(input: $input) { id value }
        }
        """
        async with _client(headers, timeout=30) as client:
            # Step 1: Find the label OpenCTI holds as this name.
            existing, label_edges, err = await _find_label(
                client, base_url, headers, label_name
            )
            if err:
                return err
            root, err = await _resolve_ref_target(
                client, base_url, headers, entity_id, "a label"
            )
            if err:
                return err
            used_value = label_name
            if existing:
                label_id = existing["id"]
                used_value = existing.get("value") or label_name
            else:
                # Step 2: Create the label and get its ID back
                create_data, err = await _graphql(
                    client,
                    base_url,
                    headers,
                    create_q,
                    {"input": {"value": label_name}},
                )
                if err:
                    if not _is_permission_failure(err):
                        return err
                    near_data, near_err = await _graphql(
                        client, base_url, headers, near_q, {"search": label_name}
                    )
                    # The lookup's own hits (accent-folded spellings) first.
                    near_edges = label_edges + (
                        [] if near_err else _edges(near_data, "labels")
                    )
                    near = list(
                        dict.fromkeys(
                            str(e.get("value")) for e in near_edges if e.get("value")
                        )
                    )[:5]
                    hint = (
                        f" Existing labels that look close: {', '.join(near)}."
                        if near
                        else ""
                    )
                    return (
                        f"Failed to add label '{label_name}': no such label exists "
                        "in OpenCTI and this account cannot create labels "
                        "(creating one needs the SETTINGS_SETLABELS capability; "
                        f"attaching an existing one does not).{hint} Nothing was "
                        f"changed. OpenCTI answered: {err}"
                    )
                created = create_data.get("labelAdd") or {}
                label_id = created.get("id")
                if not label_id:
                    return f"Failed to create label '{label_name}'."
                used_value = created.get("value") or label_name
            # Step 3: Add the label to the element using the label ID
            _, err = await _graphql(
                client,
                base_url,
                headers,
                _ref_add_mutation("AddLabel", root),
                {
                    "id": entity_id,
                    "input": {
                        "toId": label_id,
                        "relationship_type": "object-label",
                    },
                },
            )
            if err:
                return err
            # Re-read the element to return its updated state
            element = None
            read_back = _REF_READ_BACK.get(root)
            if read_back:
                read_field, read_fields, render = read_back
                read_data, err = await _graphql(
                    client,
                    base_url,
                    headers,
                    f"query ReadEntity($id: String!) {{  {read_field}(id: $id) {{"
                    + read_fields
                    + "}}",
                    {"id": entity_id},
                )
                if err:
                    return err
                element = read_data.get(read_field)
        noun = _REF_TARGET_NOUN[root]
        added = f"Label '{used_value}' added"
        if used_value != label_name:
            added += f" (the existing label matching '{label_name}')"
        if element and read_back:
            return f"{added}. Updated {noun}:\n\n" + render(element, base_url)
        return f"{added} to {noun} {entity_id}."

    # ── 15. Remove label from an entity or relationship ──────────────

    async def remove_opencti_label(entity_id: str, label_name: str) -> str:
        """Remove a label from an entity, observable, relationship or sighting."""
        label_name = str(label_name or "").strip(_JS_TRIM_CHARS)
        if not label_name:
            return "Error: `label_name` is required."
        async with _client(headers, timeout=30) as client:
            existing, _, err = await _find_label(client, base_url, headers, label_name)
            if err:
                return err
            if not existing:
                return f"Label '{label_name}' not found."
            label_id = existing["id"]
            removed_value = existing.get("value") or label_name
            root, err = await _resolve_ref_target(
                client, base_url, headers, entity_id, "a label"
            )
            if err:
                return err
            _, err = await _graphql(
                client,
                base_url,
                headers,
                _ref_delete_mutation("RemoveLabel", root),
                {
                    "id": entity_id,
                    "toId": label_id,
                    "relationship_type": "object-label",
                },
            )
        if err:
            return err
        return (
            f"Label '{removed_value}' removed from {_REF_TARGET_NOUN[root]} {entity_id}."
        )

    # ── 16. Add marking definition to an entity or relationship ──────

    async def add_opencti_marking(
        entity_id: str,
        marking_definition_id: str,
    ) -> str:
        """Add a marking definition (TLP, PAP, etc.) to an entity, observable,
        relationship or sighting."""
        async with _client(headers, timeout=30) as client:
            if marking_definition_id.lower().startswith("marking-definition--") or ":" in marking_definition_id:
                # A STIX id or a definition name: OpenCTI's relationAdd wants the
                # internal id (markings.resolve_markings).
                async def _marking_gql(
                    query: str, variables: dict[str, Any] | None = None
                ) -> tuple[dict[str, Any], str | None]:
                    return await _graphql(client, base_url, headers, query, variables)

                nodes, err = await resolve_markings(_marking_gql, [marking_definition_id])
                if err:
                    return err
                marking_definition_id = nodes[0]["id"]
            root, err = await _resolve_ref_target(
                client, base_url, headers, entity_id, "a marking"
            )
            if err:
                return err
            _, err = await _graphql(
                client,
                base_url,
                headers,
                _ref_add_mutation("AddMarking", root),
                {
                    "id": entity_id,
                    "input": {
                        "toId": marking_definition_id,
                        "relationship_type": "object-marking",
                    },
                },
            )
        if err:
            return err
        return (
            f"Marking definition {marking_definition_id} added to "
            f"{_REF_TARGET_NOUN[root]} {entity_id}."
        )

    # ── 17. Remove marking definition from an entity or relationship ─

    async def remove_opencti_marking(
        entity_id: str,
        marking_definition_id: str,
    ) -> str:
        """Remove a marking definition from an entity, observable, relationship
        or sighting."""
        async with _client(headers, timeout=30) as client:
            root, err = await _resolve_ref_target(
                client, base_url, headers, entity_id, "a marking"
            )
            if err:
                return err
            _, err = await _graphql(
                client,
                base_url,
                headers,
                _ref_delete_mutation("RemoveMarking", root),
                {
                    "id": entity_id,
                    "toId": marking_definition_id,
                    "relationship_type": "object-marking",
                },
            )
        if err:
            return err
        return (
            f"Marking definition {marking_definition_id} removed from "
            f"{_REF_TARGET_NOUN[root]} {entity_id}."
        )

    # ── 18. Get container with all objects ────────────────────────────

    container_types = {
        "Report",
        "Grouping",
        "Note",
        "Observed-Data",
        "Opinion",
        "Case-Incident",
        "Case-Rfi",
        "Case-Rft",
        "Feedback",
    }

    async def get_opencti_container_full(
        container_id: str,
        limit: int = 200,
    ) -> str | ToolResult:
        """Fetch a container and resolve all entities and relationships.

        The container also comes back structured: its own scalar fields, the
        object count, ``entities`` and ``relationships`` (each record as
        OpenCTI answered it), so a ``to_workset`` JSON split on either list
        stores one workset item per object.
        """
        # Compact from/to resolution for relationship endpoints
        ft = (
            "... on BasicObject { id entity_type } "
            "... on AttackPattern { name } "
            "... on Campaign { name } "
            "... on CourseOfAction { name } "
            "... on Individual { name } "
            "... on Organization { name } "
            "... on Sector { name } "
            "... on System { name } "
            "... on Indicator { name } "
            "... on Infrastructure { name } "
            "... on IntrusionSet { name } "
            "... on Position { name } "
            "... on City { name } "
            "... on Country { name } "
            "... on Region { name } "
            "... on Malware { name } "
            "... on ThreatActor { name } "
            "... on Tool { name } "
            "... on Vulnerability { name } "
            "... on Incident { name } "
            "... on Event { name } "
            "... on Channel { name } "
            "... on Narrative { name } "
            "... on DataComponent { name } "
            "... on DataSource { name } "
            "... on Case { name } "
            "... on StixCyberObservable { observable_value }"
        )
        # Node selection for objects inside the container
        nd = (
            "... on BasicObject { id entity_type } "
            "... on BasicRelationship { id entity_type } "
            "... on AttackPattern { name description } "
            "... on Campaign { name description } "
            "... on CourseOfAction { name description } "
            "... on Incident { name description } "
            "... on Indicator { name description } "
            "... on Infrastructure { name description } "
            "... on IntrusionSet { name description } "
            "... on Malware { name description } "
            "... on ThreatActor { name description } "
            "... on Tool { name description } "
            "... on Vulnerability { name description } "
            "... on Report { name description } "
            "... on Note { attribute_abstract content } "
            "... on ObservedData { first_observed last_observed } "
            "... on Opinion { explanation opinion } "
            "... on Grouping { name description } "
            "... on Individual { name description } "
            "... on Organization { name description } "
            "... on Sector { name description } "
            "... on System { name description } "
            "... on City { name description } "
            "... on Country { name description } "
            "... on Region { name description } "
            "... on Position { name description } "
            "... on Event { name description } "
            "... on Channel { name description } "
            "... on Narrative { name description } "
            "... on DataComponent { name description } "
            "... on DataSource { name description } "
            "... on Case { name description } "
            "... on StixCyberObservable { observable_value x_opencti_description } "
            "... on IPv4Addr { value } "
            "... on IPv6Addr { value } "
            "... on DomainName { value } "
            "... on Url { value } "
            "... on EmailAddr { value } "
            "... on Hostname { value } "
            "... on StixFile { sco_name: name } "
            "... on Software { sco_name: name } "
            "... on Mutex { sco_name: name } "
            "... on StixCoreRelationship { "
            "relationship_type description "
            "from { " + ft + " } "
            "to { " + ft + " } "
            "}"
        )
        # Objects connection block (reused for each container type)
        oc = (
            "objects(first: $first) { edges { node { "
            + nd
            + " } } pageInfo { globalCount } }"
        )
        # Author resolution
        cb = (
            "createdBy { ... on Identity { id name entity_type } }"
        )
        # Attached files
        fi = (
            "importFiles(first: 10) { edges { node { "
            "id name uploadStatus lastModified "
            "metaData { mimetype } "
            "} } }"
        )
        query = (
            "query ContainerFull($id: String!, $first: Int) { "
            "stixDomainObject(id: $id) { "
            "id entity_type standard_id created_at "
            "... on Report { name description report_types published "
            "report_content: content "
            + cb + " " + fi + " " + oc + " } "
            "... on Grouping { name description context "
            + cb + " " + fi + " " + oc + " } "
            "... on Note { attribute_abstract content note_types "
            + cb + " " + fi + " " + oc + " } "
            "... on ObservedData { first_observed last_observed "
            "number_observed " + cb + " " + fi + " " + oc + " } "
            "... on Opinion { explanation opinion "
            + cb + " " + fi + " " + oc + " } "
            "... on CaseIncident { name description "
            + cb + " " + fi + " " + oc + " } "
            "... on CaseRfi { name description "
            + cb + " " + fi + " " + oc + " } "
            "... on CaseRft { name description "
            + cb + " " + fi + " " + oc + " } "
            "... on Feedback { name description "
            + cb + " " + fi + " " + oc + " } "
            "} }"
        )
        first = min(max(limit, 1), 1000)
        async with _client(headers, timeout=60) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"id": container_id, "first": first},
            )
        if err:
            return err
        sdo = data.get("stixDomainObject")
        if not sdo:
            return f"Container {container_id} not found."
        etype = sdo.get("entity_type", "Unknown")
        if etype not in container_types:
            return (
                f"Entity {container_id} is a {etype}, not a container. "
                "Container types: " + ", ".join(sorted(container_types)) + "."
            )
        # ── Container header ──
        cname = (
            sdo.get("name")
            or sdo.get("attribute_abstract")
            or sdo.get("explanation")
            or sdo.get("id", "Unknown")
        )
        lines: list[str] = [f"=== [{etype}] {cname} ==="]
        sdo_id = sdo.get("id", "")
        lines.append(f"ID: {sdo_id or 'N/A'}")
        if sdo_id and base_url:
            lines.append(f"Link: [Open in OpenCTI]({base_url}/dashboard/id/{sdo_id})")
        if sdo.get("standard_id"):
            lines.append(f"STIX ID: {sdo['standard_id']}")
        # The container's own texts are shown whole, like get_opencti_entity's;
        # only the objects it holds are cut, marked.
        if sdo.get("description"):
            lines.append(f"Description: {sdo['description']}")
        for field, label in (
            ("report_types", "Report types"),
            ("published", "Published"),
            ("context", "Context"),
            ("note_types", "Note types"),
            ("opinion", "Opinion"),
            ("first_observed", "First observed"),
            ("last_observed", "Last observed"),
            ("number_observed", "Number observed"),
        ):
            val = sdo.get(field)
            if val:
                if isinstance(val, list):
                    lines.append(f"{label}: {', '.join(str(v) for v in val)}")
                else:
                    lines.append(f"{label}: {val}")
        # Report.content is aliased: its String clashes with Note.content's String!.
        content = sdo.get("content") or sdo.get("report_content")
        if content:
            lines.append(f"Content: {content}")

        # ── Author ──
        created_by = sdo.get("createdBy")
        if created_by and isinstance(created_by, dict):
            author_name = created_by.get("name", "Unknown")
            author_type = created_by.get("entity_type", "")
            lines.append(f"Author: {author_name}" + (f" ({author_type})" if author_type else ""))
        else:
            lines.append("Author: Unknown")

        # ── Attached files ──
        import_files = sdo.get("importFiles") or {}
        file_edges = import_files.get("edges", []) if isinstance(import_files, dict) else []
        file_nodes = [e["node"] for e in file_edges if e.get("node")]
        if file_nodes:
            lines.append("")
            lines.append(f"--- Attached Files ({len(file_nodes)}) ---")
            for fn in file_nodes:
                f_name = fn.get("name", "unknown")
                f_id = fn.get("id", "N/A")
                f_mime = (fn.get("metaData") or {}).get("mimetype", "unknown")
                f_status = fn.get("uploadStatus", "")
                line = f"  {f_name} (ID: {f_id}, MIME: {f_mime})"
                if f_status:
                    line += f" [{f_status}]"
                if fn.get("id"):
                    line += f" - download: {_storage_url(base_url, 'get', fn['id'])}"
                lines.append(line)
            if _offers_file_download(offered_tools()):
                lines.append(
                    f"  → Use {_DOWNLOAD_FILE_TOOL} with the file ID to read file contents."
                )

        # ── Extract objects ──
        obj_data = sdo.get("objects") or {}
        if not isinstance(obj_data, dict):
            obj_data = {}
        edges = obj_data.get("edges", [])
        total = (obj_data.get("pageInfo") or {}).get(
            "globalCount",
            len(edges),
        )
        nodes = [e["node"] for e in edges if e.get("node")]

        # Separate entities from relationships
        ent_list: list[dict] = []
        rel_list: list[dict] = []
        for n in nodes:
            if (
                n.get("relationship_type")
                or "relationship" in (n.get("entity_type") or "").lower()
            ):
                rel_list.append(n)
            else:
                ent_list.append(n)

        # ── Entities ──
        lines.append("")
        lines.append(f"--- Entities ({len(ent_list)}) ---")
        if not ent_list:
            lines.append("  (none)")
        for ent in ent_list:
            et = ent.get("entity_type", "?")
            en = (
                ent.get("name")
                or ent.get("sco_name")  # aliased name for SCO types
                or ent.get("observable_value")
                or ent.get("value")
                or ent.get("attribute_abstract")
                or ent.get("explanation")
                or ent.get("id", "?")
            )
            line = f"  [{et}] {en} (ID: {ent.get('id', 'N/A')})"
            ed = (
                ent.get("description")
                or ent.get("x_opencti_description")
                or ent.get("content")
                or ""
            )
            if ed:
                ed = clip_text(ed, LISTING_CONTAINER_ITEM_TEXT_LIMIT, _entity_read_call(ent))
                line += f"\n    Description: {ed}"
            lines.append(line)

        # ── Relationships ──
        lines.append("")
        lines.append(f"--- Relationships ({len(rel_list)}) ---")
        if not rel_list:
            lines.append("  (none)")
        for rel in rel_list:
            fr = rel.get("from") or {}
            to = rel.get("to") or {}
            fn = fr.get("name") or fr.get("observable_value") or fr.get("id", "?")
            tn = to.get("name") or to.get("observable_value") or to.get("id", "?")
            line = (
                f"  [{rel.get('relationship_type', '?')}] (ID: {rel.get('id', 'N/A')})"
            )
            line += f"\n    From: [{fr.get('entity_type', '?')}] {fn} (ID: {fr.get('id', 'N/A')})"
            line += f"\n    To: [{to.get('entity_type', '?')}] {tn} (ID: {to.get('id', 'N/A')})"
            if rel.get("description"):
                rd = clip_text(
                    rel["description"],
                    LISTING_CONTAINER_ITEM_TEXT_LIMIT,
                    _relationship_read_call(rel),
                )
                line += f"\n    Description: {rd}"
            lines.append(line)

        # ── Summary ──
        if total > first:
            lines.append(
                f"\n(Showing {len(nodes)} of {total} objects. Increase limit to see more.)"
            )
        else:
            lines.append(f"\nTotal objects: {total}")
        structured = {
            "id": sdo_id,
            "entity_type": etype,
            "name": cname,
            "standard_id": sdo.get("standard_id"),
            "total_objects": total,
            "shown_objects": len(nodes),
            "entities": ent_list,
            "relationships": rel_list,
        }
        return ToolResult(tool_call_id="", content="\n".join(lines), structured=structured)

    # ── 19. Create STIX indicator ────────────────────────────────────

    async def create_opencti_indicator(
        name: str = "",
        pattern: str = "",
        pattern_type: str = "stix",
        pattern_version: str = "",
        observable_type: str = "",
        observable_value: str = "",
        hash_algorithm: str = "",
        description: str = "",
        confidence: int | None = None,
        x_opencti_score: int | None = None,
        x_opencti_main_observable_type: str = "",
        x_opencti_detection: bool | None = None,
        indicator_types: list[str] | None = None,
        kill_chain_phases: list[str] | None = None,
        valid_from: str = "",
        valid_until: str = "",
        revoked: bool | None = None,
        created_by: str = "",
        object_marking: list[str] | None = None,
        object_label: list[str] | None = None,
        object_organization: list[str] | None = None,
        external_references: list[str] | None = None,
        based_on: list[str] | None = None,
        create_observables: bool = False,
    ) -> str:
        """Create a STIX indicator in OpenCTI.

        ``pattern_type`` defaults to ``stix`` and can be any pattern language
        the platform accepts (``stix``, ``sigma``, ``yara``, ``snort``,
        ``suricata``, ``spl``, ``eql``, ``kql``, ``shodan``, ``tanium-signal``,
        …). For any non-STIX language you MUST supply the raw ``pattern``
        verbatim. For ``stix`` you may instead provide ``observable_type`` +
        ``observable_value`` (+ optional ``hash_algorithm`` for file hashes) to
        auto-generate the pattern. Set ``x_opencti_main_observable_type``
        explicitly for non-STIX detections (it cannot be inferred from a
        non-STIX pattern) and ``x_opencti_detection=true`` to flag the
        indicator as a detection rule.
        """
        pattern_type = (pattern_type or "stix").strip()
        is_stix = pattern_type.lower() == "stix"

        # Build or validate pattern. Auto-generation is STIX-only — every other
        # pattern language (kql, sigma, yara, …) must be supplied verbatim.
        if not pattern:
            if not is_stix:
                return (
                    f"Error: a raw 'pattern' is required for pattern_type "
                    f"'{pattern_type}'. Auto-generation from observable_type/"
                    "observable_value is only supported for pattern_type 'stix'."
                )
            if not observable_type or not observable_value:
                return (
                    "Error: provide either 'pattern' (raw STIX pattern) or both "
                    "'observable_type' and 'observable_value' for auto-generation."
                )
            pattern = _build_stix_pattern(
                observable_type,
                observable_value,
                hash_algorithm,
            )
            if not pattern:
                if hash_algorithm:
                    return f"Error: unrecognized hash algorithm '{hash_algorithm}'."
                supported = ", ".join(sorted(_STIX_PATTERN_MAP.keys()))
                return (
                    f"Error: cannot auto-generate pattern for type "
                    f"'{observable_type}'. Supported types: {supported}. "
                    "For other types, provide a raw STIX pattern."
                )

        # Determine the main observable type for OpenCTI metadata. An explicit
        # value always wins; otherwise fall back to observable_type and, for
        # STIX patterns only, token inference. Non-STIX patterns (kql, sigma, …)
        # carry no STIX token, so the field is omitted when unknown rather than
        # set to a junk "Unknown".
        main_obs_type = x_opencti_main_observable_type or observable_type
        if hash_algorithm and not main_obs_type:
            main_obs_type = "StixFile"
        if not main_obs_type and is_stix:
            # Infer from pattern string
            _infer: dict[str, str] = {
                "ipv4-addr:": "IPv4-Addr",
                "ipv6-addr:": "IPv6-Addr",
                "domain-name:": "Domain-Name",
                "url:": "Url",
                "email-addr:": "Email-Addr",
                "mac-addr:": "Mac-Addr",
                "file:": "StixFile",
                "hostname:": "Hostname",
                "autonomous-system:": "Autonomous-System",
                "directory:": "Directory",
                "mutex:": "Mutex",
                "software:": "Software",
                "user-agent:": "User-Agent",
                "process:": "Process",
                "network-traffic:": "Network-Traffic",
                "windows-registry-key:": "Windows-Registry-Key",
            }
            for token, otype in _infer.items():
                if token in pattern:
                    main_obs_type = otype
                    break

        query = """
        mutation CreateIndicator($input: IndicatorAddInput!) {
            indicatorAdd(input: $input) {
                id entity_type standard_id name pattern pattern_type
                valid_from valid_until x_opencti_score x_opencti_detection
                x_opencti_main_observable_type
            }
        }
        """
        input_data: dict[str, Any] = {
            "name": name or pattern,
            "pattern": pattern,
            "pattern_type": pattern_type,
        }
        if main_obs_type:
            input_data["x_opencti_main_observable_type"] = main_obs_type
        if pattern_version:
            input_data["pattern_version"] = pattern_version
        if description:
            input_data["description"] = description
        if confidence is not None:
            input_data["confidence"] = confidence
        if x_opencti_score is not None:
            input_data["x_opencti_score"] = x_opencti_score
        if x_opencti_detection is not None:
            input_data["x_opencti_detection"] = x_opencti_detection
        if indicator_types:
            input_data["indicator_types"] = indicator_types
        if kill_chain_phases:
            input_data["killChainPhases"] = kill_chain_phases
        if valid_from:
            input_data["valid_from"] = valid_from
        if valid_until:
            input_data["valid_until"] = valid_until
        if revoked is not None:
            input_data["revoked"] = revoked
        if based_on:
            input_data["basedOn"] = based_on
        if create_observables:
            # NOTE: the schema field is ``createObservables`` (Boolean) — the
            # previous ``x_create_opencti_observables`` key is not part of
            # IndicatorAddInput and made GraphQL reject the whole mutation.
            input_data["createObservables"] = True
        _apply_stix_metadata(
            input_data,
            created_by=created_by,
            object_marking=object_marking,
            object_label=object_label,
            object_organization=object_organization,
            external_references=external_references,
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"input": input_data},
            )
        if err:
            return err
        ind = data.get("indicatorAdd", {})
        lines = [
            f"Indicator created: {ind.get('name', name or pattern)}",
            f"  ID: {ind.get('id', 'unknown')}",
            f"  STIX ID: {ind.get('standard_id', 'N/A')}",
            f"  Pattern type: {ind.get('pattern_type', pattern_type)}",
            f"  Pattern: {ind.get('pattern', pattern)}",
        ]
        _obs = ind.get("x_opencti_main_observable_type") or main_obs_type
        if _obs:
            lines.append(f"  Observable type: {_obs}")
        if ind.get("x_opencti_detection") is not None:
            lines.append(f"  Detection: {ind['x_opencti_detection']}")
        if ind.get("x_opencti_score") is not None:
            lines.append(f"  Score: {ind['x_opencti_score']}")
        if ind.get("valid_from"):
            lines.append(
                f"  Valid: {ind['valid_from']} → {ind.get('valid_until', 'ongoing')}"
            )
        if create_observables:
            lines.append("  Observables created from pattern: Yes")
        return "\n".join(lines)

    # ── 20. Create container ─────────────────────────────────────────

    async def create_opencti_container(
        container_type: str,
        name: str,
        description: str = "",
        content: str = "",
        published: str = "",
        context: str = "",
        report_types: list[str] | None = None,
        severity: str = "",
        priority: str = "",
        confidence: int | None = None,
        object_ids: list[str] | None = None,
        created_by: str = "",
        object_marking: list[str] | None = None,
        object_label: list[str] | None = None,
        external_references: list[str] | None = None,
        case_types: list[str] | None = None,
    ) -> str | ToolResult:
        """Create a container (Report, Case-Incident, etc.) in OpenCTI.

        The created container also comes back structured (id, standard_id,
        entity_type, name), so a flow's next step can reference it.
        """
        mutation_info = _CONTAINER_MUTATIONS.get(container_type)
        if not mutation_info:
            supported = ", ".join(sorted(_CONTAINER_MUTATIONS.keys()))
            return f"Unsupported container type: '{container_type}'. Supported: {supported}"
        mutation_name, input_type = mutation_info

        # Validate required fields per type
        if container_type == "Report" and not published:
            return "Error: 'published' date is required for Report (ISO 8601 format, e.g. '2025-06-15T00:00:00Z')."
        if container_type == "Grouping" and not context:
            return "Error: 'context' is required for Grouping."

        container_fragments: dict[str, str] = {
            "Report": "name description published report_types",
            "Case-Incident": "name description severity priority response_types",
            "Case-Rfi": "name description severity priority information_types",
            "Case-Rft": "name description severity priority takedown_types",
            "Grouping": "name description context",
        }
        fields = container_fragments.get(container_type, "name description")
        query = (
            f"mutation CreateContainer($input: {input_type}!) {{"
            f"  {mutation_name}(input: $input) {{"
            f"    id entity_type standard_id {fields}"
            f"  }}"
            f"}}"
        )
        input_data: dict[str, Any] = {"name": name}
        if description:
            input_data["description"] = description
        if content:
            input_data["content"] = content
        if confidence is not None:
            input_data["confidence"] = confidence
        if object_ids:
            input_data["objects"] = object_ids
        # Type-specific fields
        if container_type == "Report":
            input_data["published"] = published
            if report_types:
                input_data["report_types"] = report_types
        if container_type == "Grouping":
            input_data["context"] = context
        if container_type.startswith("Case-"):
            if severity:
                input_data["severity"] = severity
            if priority:
                input_data["priority"] = priority
            if case_types:
                input_data[_CASE_TYPES_FIELDS[container_type]] = case_types
        _apply_stix_metadata(
            input_data,
            created_by=created_by,
            object_marking=object_marking,
            object_label=object_label,
            external_references=external_references,
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"input": input_data},
            )
        if err:
            return err
        result = data.get(mutation_name, {})
        lines = [
            f"Container created: [{result.get('entity_type', container_type)}] {result.get('name', name)}",
            f"  ID: {result.get('id', 'unknown')}",
            f"  STIX ID: {result.get('standard_id', 'N/A')}",
        ]
        if result.get("description"):
            lines.append(
                "  Description: "
                + clip_text(
                    result["description"],
                    LISTING_CONTAINER_ITEM_TEXT_LIMIT,
                    _entity_read_call(result),
                )
            )
        if result.get("published"):
            lines.append(f"  Published: {result['published']}")
        if result.get("context"):
            lines.append(f"  Context: {result['context']}")
        if result.get("severity"):
            lines.append(f"  Severity: {result['severity']}")
        if result.get("priority"):
            lines.append(f"  Priority: {result['priority']}")
        stored_types = result.get(_CASE_TYPES_FIELDS.get(container_type, ""))
        if stored_types:
            lines.append(f"  Types: {', '.join(stored_types)}")
        if object_ids:
            lines.append(f"  Initial objects: {len(object_ids)}")
        created = {
            "id": result.get("id"),
            "standard_id": result.get("standard_id"),
            "entity_type": result.get("entity_type", container_type),
            "name": result.get("name", name),
        }
        return ToolResult(tool_call_id="", content="\n".join(lines), structured=created)

    # ── 21. Create STIX domain object ────────────────────────────────

    async def create_opencti_entity(
        type: str,
        name: str,
        description: str = "",
        aliases: list[str] | None = None,
        confidence: int | None = None,
        first_seen: str = "",
        last_seen: str = "",
        severity: str = "",
        objective: str = "",
        malware_types: list[str] | None = None,
        is_family: bool | None = None,
        threat_actor_types: list[str] | None = None,
        tool_types: list[str] | None = None,
        infrastructure_types: list[str] | None = None,
        created_by: str = "",
        object_marking: list[str] | None = None,
        object_label: list[str] | None = None,
        external_references: list[str] | None = None,
    ) -> str:
        """Create a generic STIX domain object in OpenCTI."""
        mutation_info = _SDO_MUTATIONS.get(type)
        if not mutation_info:
            supported = ", ".join(sorted(_SDO_MUTATIONS.keys()))
            return f"Unsupported entity type: '{type}'. Supported: {supported}"
        mutation_name, input_type = mutation_info

        # Extra fields returned only for specific types
        extra_fields: dict[str, str] = {
            "Incident": " severity",
            "Malware": " is_family",
        }
        extra = extra_fields.get(type, "")
        query = (
            f"mutation CreateSDO($input: {input_type}!) {{"
            f"  {mutation_name}(input: $input) {{"
            f"    id entity_type standard_id name description{extra}"
            f"  }}"
            f"}}"
        )
        input_data: dict[str, Any] = {"name": name}
        if description:
            input_data["description"] = description
        if confidence is not None:
            input_data["confidence"] = confidence
        if aliases:
            input_data["aliases"] = aliases
        # Time-range fields (for types that support them)
        if first_seen:
            input_data["first_seen"] = first_seen
        if last_seen:
            input_data["last_seen"] = last_seen
        if severity:
            input_data["severity"] = severity
        if objective:
            input_data["objective"] = objective
        # Type-specific list fields
        if malware_types and type == "Malware":
            input_data["malware_types"] = malware_types
        if is_family is not None and type == "Malware":
            input_data["is_family"] = is_family
        if threat_actor_types and type.startswith("Threat-Actor"):
            input_data["threat_actor_types"] = threat_actor_types
        if tool_types and type == "Tool":
            input_data["tool_types"] = tool_types
        if infrastructure_types and type == "Infrastructure":
            input_data["infrastructure_types"] = infrastructure_types
        _apply_stix_metadata(
            input_data,
            created_by=created_by,
            object_marking=object_marking,
            object_label=object_label,
            external_references=external_references,
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                query,
                {"input": input_data},
            )
        if err:
            return err
        result = data.get(mutation_name, {})
        lines = [
            f"Entity created: [{result.get('entity_type', type)}] {result.get('name', name)}",
            f"  ID: {result.get('id', 'unknown')}",
            f"  STIX ID: {result.get('standard_id', 'N/A')}",
        ]
        if result.get("description"):
            d = clip_text(
                result["description"],
                LISTING_CONTAINER_ITEM_TEXT_LIMIT,
                _entity_read_call(result),
            )
            lines.append(f"  Description: {d}")
        if result.get("severity"):
            lines.append(f"  Severity: {result['severity']}")
        if result.get("is_family") is not None:
            lines.append(f"  Is family: {result['is_family']}")
        return "\n".join(lines)

    # ── 22. Add entity/relationship to container ─────────────────────

    async def add_opencti_to_container(
        container_id: str,
        entity_ids: list[str],
    ) -> str:
        """Add one or more entities or relationships to a container."""
        query = """
        mutation AddToContainer($id: ID!, $input: StixRefRelationshipAddInput!) {
            stixDomainObjectEdit(id: $id) {
                relationAdd(input: $input) { id }
            }
        }
        """
        added: list[str] = []
        errors: list[str] = []
        async with _client(headers, timeout=30) as client:
            for eid in entity_ids:
                _, err = await _graphql(
                    client,
                    base_url,
                    headers,
                    query,
                    {
                        "id": container_id,
                        "input": {
                            "toId": eid,
                            "relationship_type": "object",
                        },
                    },
                )
                if err:
                    errors.append(f"{eid}: {err}")
                else:
                    added.append(eid)
        return format_bulk_outcome(
            "Added", "add", f"to container {container_id}", added, errors
        )

    # ── 23. Remove entity/relationship from container ────────────────

    async def remove_opencti_from_container(
        container_id: str,
        entity_ids: list[str],
    ) -> str:
        """Remove one or more entities or relationships from a container."""
        query = """
        mutation RemoveFromContainer(
            $id: ID!, $toId: StixRef!, $relationship_type: String!
        ) {
            stixDomainObjectEdit(id: $id) {
                relationDelete(
                    toId: $toId, relationship_type: $relationship_type
                ) { id }
            }
        }
        """
        removed: list[str] = []
        errors: list[str] = []
        async with _client(headers, timeout=30) as client:
            for eid in entity_ids:
                _, err = await _graphql(
                    client,
                    base_url,
                    headers,
                    query,
                    {
                        "id": container_id,
                        "toId": eid,
                        "relationship_type": "object",
                    },
                )
                if err:
                    errors.append(f"{eid}: {err}")
                else:
                    removed.append(eid)
        return format_bulk_outcome(
            "Removed", "remove", f"from container {container_id}", removed, errors
        )

    # ── 24. Promote observable to indicator ───────────────────────────

    async def promote_opencti_observable(
        entity_id: str,
        x_opencti_score: int | None = None,
        indicator_types: list[str] | None = None,
    ) -> str:
        """Lookup an observable by ID and create a corresponding indicator."""
        read_q = (
            "query ReadObs($id: String!) {  stixCoreObject(id: $id) {"
            + _ENTITY_FIELDS
            + "}}"
        )
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                read_q,
                {"id": entity_id},
            )
            if err:
                return err
            entity = data.get("stixCoreObject")
            if not entity:
                return f"Entity '{entity_id}' not found."

            entity_type = entity.get("entity_type", "")
            parents = entity.get("parent_types") or []
            if (
                "Stix-Cyber-Observable" not in parents
                and "StixCyberObservable" not in parents
            ):
                return (
                    f"Entity '{entity_id}' is a {entity_type}, not an "
                    "observable. This tool only works with STIX Cyber "
                    "Observables."
                )

            obs_value = (
                entity.get("observable_value")
                or entity.get("value")
                or entity.get("sco_name")
                or entity.get("name")
            )
            if not obs_value:
                return f"Could not determine the value of observable '{entity_id}'."

            # Detect hash-based files
            hash_alg = ""
            if entity_type in ("StixFile", "Artifact"):
                hashes = entity.get("hashes") or []
                if hashes:
                    for pref in ("SHA-256", "SHA-1", "MD5"):
                        for h in hashes:
                            if isinstance(h, dict) and h.get("algorithm") == pref:
                                hash_alg = pref
                                obs_value = h["hash"]
                                break
                        if hash_alg:
                            break

            pattern = _build_stix_pattern(
                entity_type,
                obs_value,
                hash_alg,
            )
            if not pattern:
                return (
                    f"Cannot auto-generate STIX pattern for "
                    f"{entity_type} '{obs_value}'. Use "
                    f"create_opencti_indicator with a manual pattern."
                )

            # Create the indicator
            create_q = """
            mutation CreateIndicator($input: IndicatorAddInput!) {
                indicatorAdd(input: $input) {
                    id entity_type standard_id name pattern
                    pattern_type x_opencti_main_observable_type
                    x_opencti_score
                }
            }
            """
            ind_input: dict[str, Any] = {
                "name": f"Indicator: {obs_value}",
                "pattern": pattern,
                "pattern_type": "stix",
                "x_opencti_main_observable_type": entity_type,
            }
            if x_opencti_score is not None:
                ind_input["x_opencti_score"] = x_opencti_score
            if indicator_types:
                ind_input["indicator_types"] = indicator_types
            ind_data, err = await _graphql(
                client,
                base_url,
                headers,
                create_q,
                {"input": ind_input},
            )
            if err:
                return err
            ind = ind_data.get("indicatorAdd", {})

            # Link with based-on relationship
            rel_q = """
            mutation CreateBasedOn(
                $input: StixCoreRelationshipAddInput!
            ) {
                stixCoreRelationshipAdd(input: $input) { id }
            }
            """
            _, err = await _graphql(
                client,
                base_url,
                headers,
                rel_q,
                {
                    "input": {
                        "fromId": ind.get("id"),
                        "toId": entity_id,
                        "relationship_type": "based-on",
                    },
                },
            )
            if err:
                return err

        return (
            f"Observable promoted to indicator.\n"
            f"  Observable: [{entity_type}] {obs_value}"
            f" (ID: {entity_id})\n"
            f"  Indicator: {ind.get('name', obs_value)}"
            f" (ID: {ind.get('id', 'unknown')})\n"
            f"  Pattern: {pattern}\n"
            f"  Linked with 'based-on' relationship."
        )

    # ── 25. Upload file to entity ────────────────────────────────────

    async def upload_opencti_file(
        entity_id: str,
        file_name: str = "",
        file_content: str = "",
        encoding: str = "text",
        mime_type: str = "application/octet-stream",
        no_trigger_import: bool = True,
        source_file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload / attach a file to an existing OpenCTI entity.

        To attach a file the user already uploaded to the conversation (or the
        assignment trigger) **byte-for-byte**, pass ``source_file_id`` with its
        file_id — the original bytes are forwarded as-is (no re-authoring or
        re-encoding). This is the right path when the user attached a PDF /
        document / image and wants *that* file on the entity.

        Otherwise the agent authors the artifact via ``file_content``: when the
        target file is a PDF (``file_name`` ends in ``.pdf`` or ``mime_type`` is
        ``application/pdf``) and ``encoding`` is not ``base64``, the supplied
        Markdown / HTML text is rendered to a real PDF server-side via the
        shared Playwright renderer *before* upload. This lets agents author a
        summary / report in their natural Markdown format and attach a proper,
        print-ready PDF to the entity in a single step — no manual browser
        "export to PDF" required.
        """
        file_name = (file_name or "").strip()

        # source_file_id (forward an uploaded file as-is) and file_content
        # (author an artifact) are mutually exclusive content sources — reject
        # both rather than silently ignoring file_content, so an LLM mistake
        # surfaces a clear error (mirrors the Drata evidence tools).
        if source_file_id.strip() and file_content:
            return (
                "Error: provide only one content source — source_file_id "
                "(forward an uploaded file as-is) or file_content (author the "
                "artifact), not both."
            )

        if source_file_id and source_file_id.strip():
            # ── Forward a user-uploaded file as-is ────────────────────────
            if file_context is None:
                return (
                    "Error: no uploaded files are available to reference via "
                    "source_file_id."
                )
            try:
                resolved = await file_context.resolve_upload(source_file_id.strip())
            except Exception:
                logger.warning(
                    "OpenCTI upload: failed to read uploaded file %s",
                    source_file_id,
                    exc_info=True,
                )
                return (
                    "Error: the referenced uploaded file could not be read from "
                    "storage (it may have expired). Re-upload it and try again."
                )
            if resolved is None:
                return (
                    f"Error: '{source_file_id}' is not an available uploaded "
                    "file. Use a file_id from the Available Files list."
                )
            file_bytes, original_name, resolved_mime = resolved
            file_name = file_name or original_name
            if not file_name:
                return "file_name is required."
            base_mime = (mime_type or "").split(";", 1)[0].strip().lower()
            # Keep an explicit non-default mime if the agent set one; otherwise
            # use the uploaded file's own content type.
            if not base_mime or base_mime == "application/octet-stream":
                mime_type = resolved_mime or "application/octet-stream"
            # Normalise a PDF filename to carry .pdf (parity with the
            # file_content path) so downstream UI/open behaviour is correct when
            # the agent renamed the file without an extension.
            final_mime = (mime_type or "").split(";", 1)[0].strip().lower()
            if final_mime == "application/pdf" and not file_name.lower().endswith(
                ".pdf"
            ):
                file_name = f"{file_name}.pdf"
        else:
            base_mime = (mime_type or "").split(";", 1)[0].strip().lower()
            if not file_name:
                # Reject early — an empty name would upload a nameless file (or,
                # with a PDF mime, normalise to a bare ``.pdf`` with no base
                # name below).
                return "file_name is required."
            if not file_content:
                return (
                    "Provide file_content (text / Markdown / HTML, or base64) "
                    "or source_file_id (a file the user uploaded)."
                )
            wants_pdf = (
                base_mime == "application/pdf"
                or file_name.lower().endswith(".pdf")
            )

            # Normalise the filename so a PDF always carries a ``.pdf``
            # extension — clients/UIs rely on it for correct download/open
            # behaviour (e.g. an ``application/pdf`` mime with a bare
            # ``summary`` file name).
            if wants_pdf and not file_name.lower().endswith(".pdf"):
                file_name = f"{file_name}.pdf"

            if wants_pdf and encoding != "base64":
                # Render Markdown / HTML → real PDF. LLMs cannot reliably emit
                # valid PDF binary inline, so they author the *content* in
                # Markdown / HTML and we convert it here (identical pipeline to
                # the ``generate_file`` builtin tool).
                from app.services.pdf_render import render_text_to_pdf

                try:
                    file_bytes = await render_text_to_pdf(
                        file_content,
                        title=file_name.rsplit(".", 1)[0] or file_name,
                    )
                except Exception:
                    logger.warning(
                        "OpenCTI upload: PDF rendering failed for %s",
                        file_name,
                        exc_info=True,
                    )
                    return (
                        "PDF rendering failed — the headless browser used for "
                        "server-side PDF generation could not render the "
                        "supplied content. Retry with encoding='base64' and "
                        "pre-rendered PDF bytes, or attach the content as "
                        "Markdown (.md) / HTML (.html) instead."
                    )
                mime_type = "application/pdf"
            elif encoding == "base64":
                try:
                    # validate=True so non-alphabet characters raise instead of
                    # being silently dropped — otherwise malformed input would
                    # be accepted and uploaded despite the "Invalid base64"
                    # contract.
                    file_bytes = base64.b64decode(file_content, validate=True)
                except Exception:
                    return "Invalid base64 content — could not decode."
                # Pre-rendered binary that targets a PDF (``.pdf`` name or
                # explicit ``application/pdf``) must carry the correct mime so
                # the attachment metadata matches — otherwise a bare
                # ``summary.pdf`` base64 upload would land as
                # ``application/octet-stream`` (the default), mismatching the
                # text-render branch above.
                if wants_pdf:
                    mime_type = "application/pdf"
            else:
                file_bytes = file_content.encode("utf-8")

        query = """
        mutation UploadFile($id: ID!, $file: Upload!, $noTriggerImport: Boolean) {
            stixCoreObjectEdit(id: $id) {
                importPush(file: $file, noTriggerImport: $noTriggerImport) {
                    id
                    name
                    uploadStatus
                    lastModified
                    metaData { mimetype }
                }
            }
        }
        """
        async with _client(headers, timeout=120) as client:
            data, err = await _graphql_upload(
                client,
                base_url,
                headers,
                query,
                {"id": entity_id, "noTriggerImport": no_trigger_import},
                "file",
                file_name,
                file_bytes,
                mime_type,
            )
        if err:
            return err
        f_info = (data.get("stixCoreObjectEdit") or {}).get("importPush", {})
        if not isinstance(f_info, dict):
            f_info = {}
        uploaded_id = f_info.get("id")
        meta = f_info.get("metaData") if isinstance(f_info.get("metaData"), dict) else {}
        # The name, the OpenCTI file id (a path ending in the name) and its
        # link reach the model only in the closing quarantine block: the
        # name may be a third party's text.
        subject = f"File '{source_file_id.strip()}'" if source_file_id.strip() else "File"
        return uploaded_file_answer(
            f"{subject} uploaded successfully.\n"
            f"  Entity: {entity_id}\n"
            f"  Status: {f_info.get('uploadStatus', 'unknown')}",
            target=_PROVIDER,
            filename=str(f_info.get("name") or file_name),
            content_type=str(meta.get("mimetype") or mime_type),
            details=[f"File ID: {uploaded_id}"] if uploaded_id else [],
            links=[("Download URL", _storage_url(base_url, "get", uploaded_id) if uploaded_id else None)],
        )

    # ── 25b. Download file from entity ───────────────────────────────

    async def download_opencti_file(file_id: str) -> str | ToolResult:
        """Download a file from OpenCTI by its file ID (from importFiles).

        Uses the REST endpoint GET /storage/get/{file_id}. Every answer carries
        the file's OpenCTI view and download links and, when the run can store
        files, a download card and a file_id to forward.
        For text-based files, returns the text content directly.
        For binary files (PDF, DOCX, XLSX, images), returns a ToolResult
        with the file attached as a FileRef so the LLM provider can handle
        it natively (e.g. PDF vision) or fall back to text extraction
        automatically via the file resolver pipeline.
        """
        from app.llm.base import FileRef
        from app.tools.base import ToolResult

        url = _storage_url(base_url, "get", file_id)
        try:
            async with _client(headers, timeout=60, follow_redirects=True) as client:
                resp = await client.get(
                    url, headers=_request_headers(headers, headers.get(_DRAFT_HEADER))
                )
            if not resp.is_success:
                return format_http_error(resp, _PROVIDER, "download_opencti_file")
        except httpx.HTTPError as exc:
            return format_network_error(exc, _PROVIDER, "download_opencti_file")
        content_type = resp.headers.get("content-type", "")
        file_name = file_id.rsplit("/", 1)[-1] if "/" in file_id else file_id
        data = resp.content
        file_size = len(data)
        stored = None
        if persist_file_fn is not None:
            try:
                stored = await persist_file_fn(
                    file_name,
                    data,
                    content_type.split(";", 1)[0].strip() or "application/octet-stream",
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("OpenCTI file persistence failed: %s", format_exception_message(exc))
                stored = None
        header = (
            f"File: {file_id}\n"
            f"Content-Type: {content_type}\n"
            f"Size: {file_size} bytes\n"
            + "".join(
                f"{line}\n"
                for line in link_lines(
                    [("Web URL", _storage_url(base_url, "view", file_id)), ("Download URL", url)]
                )
            )
        )
        if stored is not None:
            header += (
                f"Stored as a reusable file: [[FILE:{stored.file_id}]] - file_id: "
                f"{stored.file_id}; forward it to any tool that takes a file, and "
                "include the marker verbatim so the user gets a download card.\n"
            )
        # Text-based content — return directly as string
        if any(t in content_type for t in ("text/", "json", "xml", "csv", "markdown")):
            text = resp.text
            if len(text) > 50_000:
                text = text[:50_000] + "\n\n… (truncated at 50,000 characters)"
            return f"{header}\n{text}"
        # Binary content (PDF, DOCX, XLSX, images, etc.) — attach as
        # FileRef so the file resolver pipeline can send it natively to
        # LLM providers that support it, or extract text as fallback.
        file_ref = stored or FileRef.from_bytes(
            data=data,
            filename=file_name,
            content_type=content_type or "application/octet-stream",
            file_id=file_id,
        )
        return ToolResult(
            tool_call_id="",  # will be set by the executor
            content=f"{header}\nThe file has been attached for analysis.",
            files=[file_ref],
        )

    # ── 26. Import dashboard ─────────────────────────────────────────

    async def _verify_dashboard_stix_ids(
        stix_ids: list[str],
    ) -> tuple[set[str], str | None]:
        """Check which of *stix_ids* do NOT exist in the connected OpenCTI.

        All lookups are batched into a single aliased GraphQL request
        (``stixCoreObject(id:)`` resolves by STIX ``standard_id``). Returns
        ``(missing_ids, error)`` where *error* is set only when the check
        itself could not run (connectivity / GraphQL error) — in that case the
        caller fails open rather than blocking a potentially valid import.
        """
        if not stix_ids:
            return set(), None
        aliases = {f"e{i}": sid for i, sid in enumerate(stix_ids)}
        var_defs = ", ".join(f"${alias}: String!" for alias in aliases)
        fields = "\n".join(
            f"  {alias}: stixCoreObject(id: ${alias}) {{ id standard_id }}"
            for alias in aliases
        )
        query = f"query VerifyDashboardIds({var_defs}) {{\n{fields}\n}}"
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(client, base_url, headers, query, dict(aliases))
        if err:
            return set(), err
        return {sid for alias, sid in aliases.items() if not data.get(alias)}, None

    async def import_opencti_dashboard(
        dashboard_json: str = "",
        raw_widgets: bool = False,
        file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Import a custom dashboard into OpenCTI from a JSON configuration.

        The configuration is *dashboard_json*, or the file of the run
        *file_id* names (an OpenCTI export a user attached, or one
        ``download_xtm_hub_resource`` fetched from the XTM Hub).

        When raw_widgets is False (default), dashboard_json must be the full
        OpenCTI export format with a pre-encoded base64 manifest — use this
        when importing an existing dashboard export.

        When raw_widgets is True, dashboard_json should contain raw widget
        configurations: {"name": "...", "description": "...", "widgets": {"id": {...}, ...}}.
        The tool automatically builds the manifest and base64-encodes it.

        Before uploading, every STIX ID embedded in the widget filters is
        verified against the live OpenCTI knowledge base. If any referenced
        entity does not exist (a hallucinated or stale ID), the import is
        rejected with the offending IDs instead of producing broken
        ("deleted") widget filters.
        """
        import base64 as _b64

        if str(file_id or "").strip():
            if dashboard_json:
                return (
                    "Error: provide only one configuration - dashboard_json or "
                    "file_id, not both."
                )
            piped = await read_pipe_file(
                file_context,
                file_id,
                max_bytes=_MAX_DASHBOARD_FILE_BYTES,
                target="OpenCTI",
            )
            if isinstance(piped, str):
                return piped
            try:
                dashboard_json = piped[0].decode("utf-8-sig")
            except UnicodeDecodeError:
                return "Error: the dashboard file is not UTF-8 JSON."
        elif not dashboard_json:
            return "Error: provide dashboard_json or the file_id of a dashboard export."

        try:
            config = (
                json.loads(dashboard_json)
                if isinstance(dashboard_json, str)
                else dashboard_json
            )
        except (json.JSONDecodeError, TypeError) as exc:
            return f"Invalid JSON: {exc}"

        if not isinstance(config, dict):
            return "Dashboard configuration must be a JSON object."

        if not raw_widgets and "widgets" in config and "configuration" not in config:
            raw_widgets = True

        # Resolve the widget map regardless of input shape so embedded STIX IDs
        # can be verified before importing. This is the single choke point
        # through which every generated dashboard passes, so guarding it stops
        # hallucinated / stale entity IDs from creating broken widget filters.
        widget_map: Any = None
        if raw_widgets:
            widget_map = config.get("widgets")
            if not widget_map or not isinstance(widget_map, dict):
                return "raw_widgets=true requires a 'widgets' object in the JSON."
        else:
            existing_manifest = config.get("configuration", {}).get("manifest")
            if isinstance(existing_manifest, str) and existing_manifest:
                try:
                    decoded = json.loads(_b64.b64decode(existing_manifest))
                    if isinstance(decoded, dict):
                        widget_map = decoded.get("widgets")
                except ValueError:
                    # Invalid base64 (``binascii.Error``) or invalid JSON
                    # (``json.JSONDecodeError``) — both subclass ``ValueError``.
                    # Leave ``widget_map`` as None so verification is skipped
                    # (fail open) rather than crashing the import here.
                    widget_map = None

        if widget_map:
            stix_ids = sorted(_collect_dashboard_stix_ids(widget_map))
            if stix_ids:
                missing, verify_err = await _verify_dashboard_stix_ids(stix_ids)
                if verify_err:
                    # The check itself failed (connectivity / GraphQL). Fail
                    # open so a transient verification error never blocks a
                    # legitimate import.
                    logger.warning(
                        "Dashboard STIX ID verification skipped (%s)", verify_err
                    )
                elif missing:
                    bullet = "\n".join(f"  - {sid}" for sid in sorted(missing))
                    return (
                        "Dashboard NOT imported — the following STIX IDs in the "
                        "widget filters do not exist in the connected OpenCTI "
                        f"instance:\n{bullet}\n\n"
                        "These IDs appear to be fabricated or stale. For each "
                        "target entity, call search_opencti(search='ENTITY_"
                        "NAME', types=['ENTITY_TYPE']) and copy the exact "
                        "'STIX ID' (standard_id) from the result into the widget "
                        "filters (regardingOf.id, toId, fromId, objects, …). "
                        "Never invent STIX IDs. Rebuild the affected widgets "
                        "with the resolved IDs and import again."
                    )

        if raw_widgets:
            manifest_obj = {
                "widgets": widget_map,
                "config": config.get("config", {}),
            }
            manifest_b64 = _b64.b64encode(
                json.dumps(manifest_obj, ensure_ascii=False).encode()
            ).decode()
            config = {
                "openCTI_version": config.get("openCTI_version", "7.260306.1"),
                "type": "dashboard",
                "configuration": {
                    "name": config.get("name", "Untitled Dashboard"),
                    "description": config.get("description", ""),
                    "manifest": manifest_b64,
                },
            }

        file_bytes = json.dumps(config, ensure_ascii=False).encode("utf-8")
        file_name = f"{config.get('name', config.get('configuration', {}).get('name', 'dashboard'))}.json"
        file_name = file_name.replace(" ", "_").replace("/", "_")

        query = """
        mutation ImportDashboard($file: Upload!) {
            workspaceConfigurationImport(file: $file)
        }
        """
        async with _client(headers, timeout=60) as client:
            data, err = await _graphql_upload(
                client,
                base_url,
                headers,
                query,
                {},
                "file",
                file_name,
                file_bytes,
                "application/json",
            )
        if err:
            return err
        workspace_id = data.get("workspaceConfigurationImport", "")
        dash_name = config.get(
            "name", config.get("configuration", {}).get("name", "N/A")
        )
        return (
            f"Dashboard imported successfully.\n"
            f"  Workspace ID: {workspace_id}\n"
            f"  Name: {dash_name}\n"
            f"  URL: {base_url}/dashboard/workspaces/dashboards/{workspace_id}"
        )

    # ── 27. Entity time-series stats ─────────────────────────────────

    async def get_opencti_entity_stats(
        entity_id: str,
        stat_type: str = "indicators",
        interval: str = "month",
        start_date: str = "",
        end_date: str = "",
    ) -> str:
        """Get time-series statistics for a STIX entity.

        Returns counts bucketed by time period. stat_type options:
        - indicators: IOCs related to this entity
        - victims: victim entities targeted by this entity
        - containers: reports/groupings/cases mentioning this entity
        - targeting: entities that target this entity (for victim entities)
        """
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        start = start_date or (now - timedelta(days=730)).strftime("%Y-%m-%dT00:00:00.000Z")
        end = end_date or now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Build filters based on stat_type
        if stat_type == "indicators":
            filters = {
                "mode": "and",
                "filters": [
                    {"key": "entity_type", "values": ["Indicator"]},
                    {"key": "regardingOf", "values": [
                        {"key": "id", "values": [entity_id]},
                        {"key": "relationship_type", "values": ["indicates"]},
                    ]},
                ],
                "filterGroups": [],
            }
            field = "created_at"
        elif stat_type == "victims":
            filters = {
                "mode": "and",
                "filters": [
                    {"key": "entity_type", "values": [
                        "Identity", "Individual", "Organization", "Sector",
                        "System", "Location", "Country", "Region", "City",
                    ]},
                    {"key": "regardingOf", "values": [
                        {"key": "id", "values": [entity_id]},
                        {"key": "relationship_type", "values": ["targets"]},
                    ]},
                ],
                "filterGroups": [],
            }
            field = "created_at"
        elif stat_type == "containers":
            filters = {
                "mode": "and",
                "filters": [
                    {"key": "entity_type", "values": [
                        "Report", "Grouping", "Case-Incident", "Case-Rfi", "Case-Rft",
                    ]},
                    {"key": "objects", "values": [entity_id]},
                ],
                "filterGroups": [],
            }
            field = "created_at"
        elif stat_type == "targeting":
            filters = {
                "mode": "and",
                "filters": [
                    {"key": "entity_type", "values": [
                        "Threat-Actor", "Intrusion-Set", "Campaign", "Malware",
                    ]},
                    {"key": "regardingOf", "values": [
                        {"key": "id", "values": [entity_id]},
                        {"key": "relationship_type", "values": ["targets"]},
                    ]},
                ],
                "filterGroups": [],
            }
            field = "created_at"
        else:
            return f"Error: unknown stat_type '{stat_type}'. Use: indicators, victims, containers, targeting."

        query = (
            "query EntityTimeSeries("
            "  $field: String!, $operation: StatsOperation!,"
            "  $startDate: DateTime!, $endDate: DateTime!,"
            "  $interval: String!, $filters: FilterGroup"
            ") {"
            "  stixCoreObjectsTimeSeries("
            "    field: $field, operation: $operation,"
            "    startDate: $startDate, endDate: $endDate,"
            "    interval: $interval, filters: $filters"
            "  ) { date value }"
            "}"
        )
        variables = {
            "field": field,
            "operation": "count",
            "startDate": start,
            "endDate": end,
            "interval": interval,
            "filters": filters,
        }
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(client, base_url, headers, query, variables)
        if err:
            return err

        series = data.get("stixCoreObjectsTimeSeries", [])
        if not series:
            return f"No {stat_type} statistics found for entity {entity_id}."

        total = sum(item.get("value", 0) for item in series)
        non_zero = [item for item in series if item.get("value", 0) > 0]

        lines = [
            f"Time-series statistics: {stat_type} for entity {entity_id}",
            f"  Period: {start} → {end} (interval: {interval})",
            f"  Total count: {total}",
            f"  Non-zero periods: {len(non_zero)} / {len(series)}",
            "",
        ]
        for item in series:
            v = item.get("value", 0)
            if v > 0:
                lines.append(f"  {item.get('date', '?')}: {v}")
        return "\n".join(lines)

    # ── Top-N related entities ────────────────────────────────────

    async def get_opencti_top_related(
        entity_id: str,
        related_type: str = "victims",
        limit: int = 10,
        start_date: str = "",
        end_date: str = "",
    ) -> str:
        """Get top-N related entities by type for a STIX entity.

        Returns a ranked distribution. related_type options:
        - victims: top targeted sectors, countries, organizations
        - threats: top threat actors, intrusion sets targeting this entity
        - techniques: top ATT&CK techniques used
        - malware: top malware families associated
        """
        # Map related_type to relationship_type, toTypes, and field
        if related_type == "victims":
            rel_types = ["targets"]
            to_types = [
                "Identity", "Individual", "Organization", "Sector",
                "System", "Location", "Country", "Region", "City",
            ]
            field = "internal_id"
        elif related_type == "threats":
            rel_types = ["targets"]
            to_types = [
                "Threat-Actor", "Intrusion-Set", "Campaign",
            ]
            field = "internal_id"
        elif related_type == "techniques":
            rel_types = ["uses"]
            to_types = ["Attack-Pattern"]
            field = "internal_id"
        elif related_type == "malware":
            rel_types = ["uses"]
            to_types = ["Malware"]
            field = "internal_id"
        else:
            return f"Error: unknown related_type '{related_type}'. Use: victims, threats, techniques, malware."

        query = (
            "query EntityDistribution("
            "  $field: String!, $operation: StatsOperation!,"
            "  $limit: Int, $objectId: [String],"
            "  $relationship_type: [String], $toTypes: [String],"
            "  $startDate: DateTime, $endDate: DateTime"
            ") {"
            "  stixCoreObjectsDistribution("
            "    field: $field, operation: $operation,"
            "    limit: $limit, objectId: $objectId,"
            "    relationship_type: $relationship_type, toTypes: $toTypes,"
            "    startDate: $startDate, endDate: $endDate"
            "  ) {"
            "    label value"
            "    entity {"
            "      ... on BasicObject { id entity_type }"
            "      ... on StixObject { representative { main } }"
            "    }"
            "  }"
            "}"
        )
        variables: dict[str, Any] = {
            "field": field,
            "operation": "count",
            "limit": min(limit, 50),
            "objectId": [entity_id],
            "relationship_type": rel_types,
            "toTypes": to_types,
        }
        if start_date:
            variables["startDate"] = start_date
        if end_date:
            variables["endDate"] = end_date

        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(client, base_url, headers, query, variables)
        if err:
            return err

        dist = data.get("stixCoreObjectsDistribution", [])
        if not dist:
            return f"No {related_type} distribution found for entity {entity_id}."

        lines = [
            f"Top {related_type} for entity {entity_id}:",
            "",
        ]
        for i, item in enumerate(dist, 1):
            entity_data = item.get("entity") or {}
            name = (
                (entity_data.get("representative") or {}).get("main")
                or item.get("label", "?")
            )
            etype = entity_data.get("entity_type", "?")
            eid = entity_data.get("id", "")
            count = item.get("value", 0)
            lines.append(f"  {i}. [{etype}] {name} — {count} relationships")
            if eid:
                lines.append(f"     ID: {eid}")
        return "\n".join(lines)

    # ── 29. Entity audit history ──────────────────────────────────────

    async def get_opencti_entity_history(
        entity_id: str,
        limit: int = 100,
        types: list[str] | None = None,
    ) -> str:
        """Get the modification history (audit log) for a STIX entity.

        Returns the most recent log entries showing who changed what and when.
        Includes: timestamp, user, action type, context (changed fields, old/new values).
        """
        filters: dict[str, Any] = {
            "mode": "and",
            "filters": [
                {"key": "context_data.id", "values": [entity_id], "operator": "eq"},
            ],
            "filterGroups": [],
        }
        if types:
            filters["filters"].append(
                {"key": "event_type", "values": types, "operator": "eq"}
            )

        query = (
            "query EntityLogs("
            "  $first: Int, $filters: FilterGroup,"
            "  $orderBy: LogsOrdering, $orderMode: OrderingMode"
            ") {"
            "  logs("
            "    first: $first, filters: $filters,"
            "    orderBy: $orderBy, orderMode: $orderMode"
            "  ) {"
            "    edges {"
            "      node {"
            "        id"
            "        event_type"
            "        event_scope"
            "        timestamp"
            "        user { id name }"
            "        context_data {"
            "          entity_type"
            "          entity_name"
            "          message"
            "          commit"
            "          external_references { source_name external_id url }"
            "        }"
            "      }"
            "    }"
            "  }"
            "}"
        )
        variables = {
            "first": min(limit, 200),
            "filters": filters,
            "orderBy": "timestamp",
            "orderMode": "desc",
        }
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(client, base_url, headers, query, variables)
        if err:
            return err

        logs = _edges(data, "logs")
        if not logs:
            return f"No history entries found for entity {entity_id}."

        lines = [
            f"Modification history for entity {entity_id}:",
            f"  Entries: {len(logs)}",
            "",
        ]
        for log_entry in logs:
            ts = log_entry.get("timestamp", "?")
            user = (log_entry.get("user") or {}).get("name", "System")
            event_type = log_entry.get("event_type", "?")
            event_scope = log_entry.get("event_scope", "")
            ctx = log_entry.get("context_data") or {}
            message = ctx.get("message", "")
            entity_name = ctx.get("entity_name", "")

            action = f"{event_type}"
            if event_scope:
                action += f"/{event_scope}"

            line = f"  [{ts}] {user} — {action}"
            if entity_name:
                line += f" on '{entity_name}'"
            if message:
                line += f": {message}"
            refs = [
                _history_ref_text(ref)
                for ref in (ctx.get("external_references") or [])
                if isinstance(ref, dict)
            ]
            refs = [ref for ref in refs if ref]
            if refs:
                shown = refs[:_HISTORY_MAX_REFS]
                if len(refs) > len(shown):
                    shown.append(f"+{len(refs) - len(shown)} more")
                line += f" [refs: {', '.join(shown)}]"
            lines.append(line)
        return "\n".join(lines)

    # ── Register all tools ────────────────────────────────────────────

    executor.register("validate_nlq_filter", validate_nlq_filter)
    executor.register("search_opencti", search_opencti)
    executor.register("get_opencti_entity", get_opencti_entity)
    executor.register("get_opencti_relationship", get_opencti_relationship)
    executor.register("list_opencti_entities", list_opencti_entities)
    executor.register("list_opencti_observables", list_opencti_observables)
    executor.register("list_opencti_relationships", list_opencti_relationships)
    executor.register("create_opencti_relationship", create_opencti_relationship)
    executor.register("create_opencti_observable", create_opencti_observable)
    executor.register("create_opencti_note", create_opencti_note)
    executor.register("update_opencti_field", update_opencti_field)
    executor.register(
        "update_opencti_relationship_field", update_opencti_relationship_field
    )
    executor.register("delete_opencti_entity", delete_opencti_entity)
    executor.register("merge_opencti_entities", merge_opencti_entities)
    executor.register("delete_opencti_relationship", delete_opencti_relationship)
    executor.register("add_opencti_label", add_opencti_label)
    executor.register("remove_opencti_label", remove_opencti_label)
    executor.register("add_opencti_marking", add_opencti_marking)
    executor.register("remove_opencti_marking", remove_opencti_marking)
    executor.register(
        "get_opencti_container_full",
        get_opencti_container_full,
    )
    executor.register("create_opencti_indicator", create_opencti_indicator)
    executor.register("create_opencti_container", create_opencti_container)
    executor.register("create_opencti_entity", create_opencti_entity)
    executor.register("add_opencti_to_container", add_opencti_to_container)
    executor.register(
        "remove_opencti_from_container",
        remove_opencti_from_container,
    )
    executor.register(
        "promote_opencti_observable",
        promote_opencti_observable,
    )
    executor.register("upload_opencti_file", upload_opencti_file)
    executor.register("download_opencti_file", download_opencti_file)
    executor.register("import_opencti_dashboard", import_opencti_dashboard)
    executor.register("get_opencti_entity_stats", get_opencti_entity_stats)
    executor.register("get_opencti_top_related", get_opencti_top_related)
    executor.register("get_opencti_entity_history", get_opencti_entity_history)

    # ── Dev Debug Tools ───────────────────────────────────────────────

    async def dev_debug_hello() -> str:
        """Return a simple greeting."""
        return "hello world"

    async def dev_debug_json_wrap(text: str = "") -> str:
        """Wrap text into a JSON object with an 'output' attribute."""
        return json.dumps({"output": text or ""})

    async def dev_debug_json_wrap_raw(text: str = "") -> dict:
        """Wrap text into a dict with an 'output' attribute (returns raw dict)."""
        return {"output": text or ""}

    async def dev_debug_append_hello(text: str = "") -> str:
        """Append ' hello' to the input text."""
        return (text or "") + " hello"

    # Dev/debug PoC handlers are registered ONLY when DEBUG is enabled so they
    # never reach production agents (the matching tool definitions below are
    # gated by the same flag, keeping defs and handlers in lockstep).
    if settings.debug:
        executor.register("dev_debug_hello", dev_debug_hello)
        executor.register("dev_debug_json_wrap", dev_debug_json_wrap)
        executor.register("dev_debug_json_wrap_raw", dev_debug_json_wrap_raw)
        executor.register("dev_debug_append_hello", dev_debug_append_hello)

    # ── Dev tool: file hash (FileContext proof-of-concept) ────────────

    async def dev_files_hash(
        file_context: FileContext | None = None,
    ) -> str:
        """Read every file in the current context, compute MD5 / SHA-1 / SHA-256 hashes."""
        import hashlib

        if not file_context or file_context.is_empty:
            return json.dumps({"files": [], "note": "No files in context"})

        results = []
        for ref in file_context.list_files():
            data = await file_context.read_bytes(ref.file_id)
            if data is None:
                results.append(
                    {
                        "file_id": ref.file_id,
                        "filename": ref.filename,
                        "error": "could not read file bytes",
                    }
                )
                continue
            results.append(
                {
                    "file_id": ref.file_id,
                    "filename": ref.filename,
                    "content_type": ref.content_type,
                    "size": len(data),
                    "hashes": {
                        "md5": hashlib.md5(data).hexdigest(),  # noqa: S324
                        "sha1": hashlib.sha1(data).hexdigest(),  # noqa: S324
                        # Include legacy hash formats for OpenCTI interoperability;
                        # not used for security decisions.
                        "sha256": hashlib.sha256(data).hexdigest(),
                    },
                }
            )
        return json.dumps({"files": results}, indent=2)

    if settings.debug:
        executor.register("dev_files_hash", dev_files_hash)

    # ── Draft management ─────────────────────────────────────────────
    # When the opencti-draft-id header is present (user is in a draft in
    # the UI), mutations work directly — no gate, no use_opencti_draft.
    # The agent is informed of the active draft via tool descriptions.
    #
    # When NO header is present, the LLM must call use_opencti_draft
    # (reusing a draft from conversation history) or create_opencti_draft
    # before any mutation is allowed.

    if not _draft_id:
        async def use_opencti_draft(draft_id: str) -> str:
            """Select a draft to work in when the user is NOT viewing one in the UI.

            Call this when you found a draft ID in the conversation history
            (from a previous `create_opencti_draft` call). This tells the system
            to direct all subsequent mutations to that draft.
            """
            if not draft_id or not draft_id.strip():
                return "❌ No draft ID provided."
            draft_id = draft_id.strip()

            # Validate the draft exists and check its status
            check_query = """
                query CopilotGetDraft($id: String!) {
                    draftWorkspace(id: $id) { id name draft_status }
                }
            """
            async with _client(headers, timeout=30) as client:
                data, err = await _graphql(
                    client, base_url, headers, check_query,
                    {"id": draft_id},
                )
            if err:
                return (
                    f"❌ Draft **{draft_id}** is no longer accessible "
                    f"(it may have been deleted or validated). "
                    f"Call `create_opencti_draft` to create a new draft instead."
                )

            draft_data = data.get("draftWorkspace")
            if not draft_data:
                return (
                    f"❌ Draft **{draft_id}** was not found — it may have been deleted. "
                    f"Call `create_opencti_draft` to create a new draft instead."
                )

            draft_status = (draft_data.get("draft_status") or "").lower()
            if draft_status != "open":
                return (
                    f"❌ Draft **{draft_id}** has status `{draft_status}` and cannot be modified. "
                    f"Call `create_opencti_draft` to create a new draft instead."
                )

            headers["opencti-draft-id"] = draft_id
            draft_name = draft_data.get("name", "Untitled Draft")
            draft_url = f"{base_url}/dashboard/data/import/draft/{draft_id}"
            return (
                f"✅ Draft activated: [{draft_name}]({draft_url}) (id: `{draft_id}`)"
            )

        executor.register("use_opencti_draft", use_opencti_draft)

    async def create_opencti_draft(
        name: str = "",
    ) -> str:
        """Explicitly create a new draft workspace in OpenCTI.

        All subsequent knowledge-mutation calls in this turn will use it.
        """
        draft_name = name or f"XTM One Draft - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        query = """
            mutation draftWorkspaceAdd($input: DraftWorkspaceAddInput!) {
                draftWorkspaceAdd(input: $input) { id }
            }
        """
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client, base_url, headers, query,
                {"input": {"name": draft_name}},
            )
        if err:
            return f"❌ Failed to create draft: {err}"
        draft_id = (data.get("draftWorkspaceAdd") or {}).get("id", "")
        if not draft_id:
            return "❌ Failed to create draft: no ID returned"

        headers["opencti-draft-id"] = draft_id
        _created_drafts.add(draft_id)
        draft_url = f"{base_url}/dashboard/data/import/draft/{draft_id}"
        return (
            f"✅ Draft created: [{draft_name}]({draft_url}) (id: `{draft_id}`)"
        )

    executor.register("create_opencti_draft", create_opencti_draft)

    async def get_opencti_draft_info(draft_id: str = "") -> str:
        """Get details about a draft workspace (name, status, link).

        If no draft_id is provided, uses the currently active draft.
        """
        did = (draft_id or "").strip() or headers.get("opencti-draft-id", "")
        if not did:
            return "❌ No draft is currently active and no draft ID was provided."
        query = """
            query draftWorkspace($id: String!) {
                draftWorkspace(id: $id) { id name draft_status }
            }
        """
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client, base_url, headers, query,
                {"id": did},
            )
        if err:
            return f"❌ Could not retrieve draft info: {err}"
        draft_data = data.get("draftWorkspace")
        if not draft_data:
            return f"❌ Draft `{did}` not found — it may have been deleted or validated."
        draft_name = draft_data.get("name", "Untitled Draft")
        draft_status = draft_data.get("draft_status", "unknown")
        draft_url = f"{base_url}/dashboard/data/import/draft/{did}"
        return f"Draft: [{draft_name}]({draft_url}) — status: {draft_status} (id: `{did}`)"

    executor.register("get_opencti_draft_info", get_opencti_draft_info)

    async def list_opencti_drafts() -> str:
        """List all open draft workspaces available to the user."""
        query = """
            query DraftWorkspaces {
                draftWorkspaces(filters: { mode: and, filters: [{ key: "draft_status", values: ["open"] }], filterGroups: [] }) {
                    edges { node { id name draft_status } }
                }
            }
        """
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client, base_url, headers, query, {},
            )
        if err:
            return f"❌ Could not list drafts: {err}"
        edges = (data.get("draftWorkspaces") or {}).get("edges") or []
        if not edges:
            return "No open drafts found. You can create one with `create_opencti_draft`."
        lines = ["Open drafts:", ""]
        for edge in edges:
            node = edge.get("node", {})
            did = node.get("id", "")
            name = node.get("name", "Untitled")
            url = f"{base_url}/dashboard/data/import/draft/{did}"
            lines.append(f"- **{name}** → [Open draft]({url}) (id: `{did}`)")
        lines.append("")
        lines.append("Present these drafts to the user WITH their links. Ask which one to use.")
        return "\n".join(lines)

    executor.register("list_opencti_drafts", list_opencti_drafts)

    async def _read_draft(client: httpx.AsyncClient, did: str) -> tuple[dict | None, str | None]:
        """The draft *did* as ``{id, name, draft_status}``, or an error."""
        data, err = await _graphql(
            client, base_url, headers, _DRAFT_READ_QUERY, {"id": did}
        )
        if err:
            return None, f"Error: could not read draft `{did}`: {err}"
        node = data.get("draftWorkspace")
        if not node:
            return None, (
                f"Error: draft `{did}` was not found: it may already be deleted. "
                "Nothing was changed."
            )
        return node, None

    async def delete_opencti_draft(draft_id: str) -> str:
        """Delete a draft workspace and everything staged in it.

        The draft is read first, so the answer names it, then read again to
        confirm it is gone.
        """
        did = (draft_id or "").strip()
        if not did:
            return "Error: `draft_id` is required."
        if (
            not live_writes
            and did not in _created_drafts
            and not (gated_draft_validation and case_draft_fn is not None and await case_draft_fn(did))
        ):
            # Setting off: no wording names it or offers to turn it on.
            return (
                f"Error: draft `{did}` was not created in this run, so it is not "
                "deleted here: delete it in OpenCTI (Data > Import > Drafts). "
                "Nothing was changed."
            )
        async with _client(headers, timeout=30) as client:
            node, err = await _read_draft(client, did)
            if err:
                return err
            name = node.get("name") or "Untitled Draft"
            _, err = await _graphql(
                client, base_url, headers, _DRAFT_DELETE_MUTATION, {"id": did}
            )
            if err:
                return f"Error: draft '{name}' (`{did}`) was not deleted: {err}"
            check, check_err = await _graphql(
                client, base_url, headers, _DRAFT_READ_QUERY, {"id": did}
            )
        _created_drafts.discard(did)
        if headers.get(_DRAFT_HEADER) == did:
            headers.pop(_DRAFT_HEADER, None)
        if not check_err and check.get("draftWorkspace"):
            return (
                f"Error: OpenCTI accepted the deletion of draft '{name}' (`{did}`) "
                "but still lists it; read it again with get_opencti_draft_info."
            )
        return f"✅ Draft '{name}' (`{did}`) deleted: it no longer exists in OpenCTI."

    executor.register("delete_opencti_draft", delete_opencti_draft)

    if _offer_apply:

        async def apply_opencti_draft(draft_id: str) -> str:
            """Validate (apply) a draft workspace: its staged changes are
            written to the live graph. Offered when the integration's Live
            writes setting is on, or when the run's fleet gates this tool
            (#1480). Answers the draft's name and final status.
            """
            did = (draft_id or "").strip()
            if not did:
                return "Error: `draft_id` is required."
            async with _client(headers, timeout=60) as client:
                node, err = await _read_draft(client, did)
                if err:
                    return err
                name = node.get("name") or "Untitled Draft"
                status = str(node.get("draft_status") or "").lower()
                if status and status != "open":
                    return (
                        f"Error: draft '{name}' (`{did}`) has status `{status}` "
                        "and cannot be validated again. Nothing was changed."
                    )
                data, err = await _graphql(
                    client, base_url, headers, _DRAFT_VALIDATE_MUTATION, {"id": did}
                )
                if err:
                    return f"Error: draft '{name}' (`{did}`) was not validated: {err}"
                work = data.get("draftWorkspaceValidate") or {}
                after, after_err = await _graphql(
                    client, base_url, headers, _DRAFT_READ_QUERY, {"id": did}
                )
            if headers.get(_DRAFT_HEADER) == did:
                headers.pop(_DRAFT_HEADER, None)
            final = "unknown"
            if not after_err:
                final = str((after.get("draftWorkspace") or {}).get("draft_status") or "unknown")
            work_line = f" OpenCTI work `{work['id']}` applies it." if work.get("id") else ""
            return (
                f"✅ Draft '{name}' (`{did}`) validated: status `{final}`.{work_line} "
                "Its changes are written to the live graph as OpenCTI processes "
                "the work; later changes in this run need a new draft or live writes."
            )

        executor.register("apply_opencti_draft", apply_opencti_draft)

    # ── Labels (read: always; create: only with the Live writes setting on) ─

    async def list_opencti_labels(search: str = "", limit: int = 50, after: str = "") -> str:
        """List the labels OpenCTI holds: id, value and color, paginated."""
        first = max(1, min(int(limit or 50), 500))
        variables: dict[str, Any] = {"first": first}
        if (search or "").strip():
            variables["search"] = search.strip()
        if (after or "").strip():
            variables["after"] = after.strip()
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(
                client, base_url, headers, _LABELS_LIST_QUERY, variables, live=True
            )
        if err:
            return err
        block = data.get("labels") or {}
        nodes = [e.get("node") or {} for e in block.get("edges") or []]
        page = block.get("pageInfo") or {}
        total = page.get("globalCount")
        if not nodes:
            scope = f" matching '{search.strip()}'" if (search or "").strip() else ""
            return f"No label{scope} in OpenCTI."
        head = f"{len(nodes)} label(s)" + (f" of {total}" if total is not None else "") + ":"
        lines = [head]
        for node in nodes:
            lines.append(
                f"- {node.get('value')} (id: `{node.get('id')}`, color: {node.get('color') or 'none'})"
            )
        if page.get("hasNextPage") and page.get("endCursor"):
            lines.append(f"More labels: call again with after=`{page['endCursor']}`.")
        return "\n".join(lines)

    executor.register("list_opencti_labels", list_opencti_labels)

    # A label is written outside any draft (OpenCTI's Settings > Labels):
    # registered with the setting on only. Off, a label is added through the
    # draft path (add_opencti_label).
    async def create_opencti_label(value: str, color: str = "") -> str:
        """Create a label in OpenCTI, or return the one it already holds.

        A label is platform configuration, not knowledge: it is written to the
        live graph, never into a draft, as OpenCTI's Settings > Labels does.
        Idempotent: a label with the same normalised value (OpenCTI's own
        identity, ``_label_identity``) is returned unchanged.
        """
        label_value = str(value or "").strip(_JS_TRIM_CHARS)
        if not label_value:
            return "Error: `value` is required."
        label_color = str(color or "").strip()
        if label_color and not _LABEL_COLOR_RE.match(label_color):
            return (
                f"Error: color '{label_color}' is not a hex color such as #ff8800. "
                "Nothing was created."
            )
        async with _client(headers, timeout=30) as client:
            existing, _edges_seen, err = await _find_label(
                client, base_url, headers, label_value, live=True
            )
            if err:
                return err
            if existing:
                return (
                    f"Label '{existing.get('value')}' already exists "
                    f"(id: `{existing.get('id')}`, color: {existing.get('color') or 'none'}); "
                    "nothing was created."
                )
            label_input: dict[str, Any] = {"value": label_value}
            if label_color:
                label_input["color"] = label_color
            data, err = await _graphql(
                client, base_url, headers, _LABEL_CREATE_MUTATION,
                {"input": label_input}, live=True,
            )
        if err:
            return err
        created = data.get("labelAdd") or {}
        if not created.get("id"):
            return f"Error: OpenCTI returned no label for '{label_value}'."
        return (
            f"✅ Label '{created.get('value') or label_value}' created in the live "
            f"graph (id: `{created['id']}`, color: {created.get('color') or 'none'})."
        )

    if live_writes:
        executor.register("create_opencti_label", create_opencti_label)

    # Same rule as create_opencti_label: a label is platform configuration
    # written outside any draft, so the tool exists with the setting on only.
    async def manage_opencti_label(
        action: str, label: str, value: str = "", color: str = "", **extra_args: object
    ) -> str:
        """Update (value, color) or delete a label OpenCTI holds, named by its
        id or its value. Written to the live graph, never into a draft.
        """
        verb = str(action or "").strip().lower()
        # A field the action ignores (value or color on delete, or an undeclared one)
        # is refused by name: nothing is sent, never a silent "deleted".
        refused = _refuse_unapplied(
            verb, _LABEL_APPLIES, {"label": label, "value": value, "color": color}, dict(extra_args)
        )
        if refused:
            return refused
        if verb not in ("update", "delete"):
            return "Error: `action` is `update` or `delete`. Nothing was changed."
        ref = str(label or "").strip(_JS_TRIM_CHARS)
        if not ref:
            return "Error: `label` (the label's id or value) is required."
        new_value = str(value or "").strip(_JS_TRIM_CHARS)
        new_color = str(color or "").strip()
        if verb == "update":
            if not new_value and not new_color:
                return "Error: `update` needs a new `value`, a new `color`, or both. Nothing was changed."
            if new_color and not _LABEL_COLOR_RE.match(new_color):
                return f"Error: color '{new_color}' is not a hex color such as #ff8800. Nothing was changed."
        async with _client(headers, timeout=30) as client:
            found, err = await _resolve_label(client, base_url, headers, ref)
            if err:
                return err
            if not found:
                return (
                    f"Error: OpenCTI holds no label '{ref}'. List them with list_opencti_labels. Nothing was changed."
                )
            lid = str(found["id"])
            old = f"'{found.get('value')}' (id: `{lid}`)"
            if verb == "delete":
                _, err = await _graphql(client, base_url, headers, _LABEL_DELETE_MUTATION, {"id": lid}, live=True)
                if err:
                    return f"Error: label {old} was not deleted: {err}"
                return f"✅ Label {old} deleted; OpenCTI also removes it from the objects that carried it."
            if new_value and _label_identity(new_value) != _label_identity(str(found.get("value") or "")):
                clash, _hits, err = await _find_label(client, base_url, headers, new_value, live=True)
                if err:
                    return err
                if clash and clash.get("id") != lid:
                    return (
                        f"Error: label '{clash.get('value')}' (id: `{clash.get('id')}`) "
                        f"already holds the value '{new_value}'. Nothing was changed."
                    )
            patch_input: list[dict[str, Any]] = []
            if new_value:
                patch_input.append({"key": "value", "value": [new_value]})
            if new_color:
                patch_input.append({"key": "color", "value": [new_color]})
            data, err = await _graphql(
                client,
                base_url,
                headers,
                _LABEL_PATCH_MUTATION,
                {"id": lid, "input": patch_input},
                live=True,
            )
        if err:
            return f"Error: label {old} was not updated: {err}"
        patched = (data.get("labelEdit") or {}).get("fieldPatch") or {}
        return (
            f"✅ Label {old} updated: value '{patched.get('value') or new_value or found.get('value')}', "
            f"color {patched.get('color') or new_color or found.get('color') or 'none'}."
        )

    if live_writes:
        executor.register("manage_opencti_label", manage_opencti_label)

    async def _label_type_route(types: Any, search: str, limit: int) -> str | None:
        """Answer a ``Label`` type in the entity tools with labels, never with
        other objects: a label is no STIX core object, and the entity queries
        ignore the type and return whatever else matches.
        """
        wanted = [types] if isinstance(types, str) else list(types or [])
        names = {str(t).strip().lower() for t in wanted if str(t).strip()}
        if "label" not in names:
            return None
        if len(names) > 1:
            return (
                "Error: `Label` cannot be listed with other types: a label is "
                "not an entity. List labels with list_opencti_labels, and the "
                "other types without `Label`."
            )
        return await list_opencti_labels(search=search or "", limit=limit)

    # ── Draft-gate knowledge-mutation tools ────────────────────────────
    # When no opencti-draft-id header is present at turn start, wrap
    # mutation tool handlers so they block until the LLM explicitly
    # activates a draft (via use_opencti_draft or create_opencti_draft).
    # This forces the LLM to check conversation history for an existing
    # draft before creating a new one.
    # ``direct_mutations`` (platform MCP servers) skips the gate: the
    # original handlers stay registered and write to the live graph.
    #
    # The gate is installed even when a draft IS active at build time (the user
    # is viewing one in the UI): ``_graphql`` drops a draft OpenCTI rejects
    # (validated, deleted, locked), and without the gate the next
    # mutation would then write straight to the live graph.

    if not _skip_gate:
        _original_handlers: dict[str, Any] = {
            tn: executor._handlers.get(tn)
            for tn in _DRAFT_REQUIRED_TOOLS
            if tn in executor._handlers
        }

        for _tool_name in _DRAFT_REQUIRED_TOOLS:
            _captured_name = _tool_name

            async def _draft_gate_wrapper(
                _tn: str = _captured_name, **kwargs: Any
            ) -> str:
                # No draft active yet for this turn.
                if not headers.get("opencti-draft-id"):
                    if not autonomous:
                        # Interactive run: defer to the human-oriented flow
                        # (check history / ask which draft / ask for a name).
                        # A run that started in a UI draft has no
                        # ``use_opencti_draft``: its draft comes from the UI.
                        return _UI_DRAFT_UNUSABLE_MSG if _draft_id else _DRAFT_REQUIRED_MSG
                    # Autonomous run (no user to ask): self-heal by creating a
                    # draft now, then fall through to run the mutation. The
                    # draft is left open for asynchronous human validation.
                    created = await create_opencti_draft(name="")
                    if not headers.get("opencti-draft-id"):
                        # Draft creation failed — surface the error verbatim.
                        return created
                    logger.info(
                        "OpenCTI autonomous run: auto-created draft for "
                        "gated mutation %s",
                        _tn,
                    )
                # Draft is active (pre-existing or just auto-created).
                active_draft = headers.get("opencti-draft-id")
                if active_draft:
                    original = _original_handlers.get(_tn)
                    if original is None:
                        return _DRAFT_REQUIRED_MSG
                    result = await original(**kwargs)
                    if headers.get("opencti-draft-id") != active_draft:
                        # ``_graphql`` already dropped the rejected draft (its
                        # message carries OpenCTI's own reason), or a concurrent
                        # call moved the run to another draft: leave that one.
                        return result
                    # Detect locked/deleted draft errors and provide recovery.
                    # Error-shaped results only: a successful one may well say
                    # "locked" (a label named "blocked-ips").
                    if isinstance(result, str) and is_error_shaped_result(result) and (
                        "locked" in result.lower()
                        or "DRAFT_LOCKED" in result
                        or "Could not find draft" in result
                        or "draft workspace" in result.lower()
                        or "not in an open state" in result.lower()
                    ):
                        headers.pop("opencti-draft-id", None)
                        if active_draft == _draft_id:
                            return _UI_DRAFT_UNUSABLE_MSG
                        return (
                            "❌ The current draft is no longer usable "
                            "(it may be locked, validated, or deleted). "
                            "Call `create_opencti_draft` to create a new draft, "
                            "then retry this action."
                        )
                    return result
                return _DRAFT_REQUIRED_MSG

            if _original_handlers.get(_tool_name) is not None:
                # Expose the real handler's signature (``inspect.signature``
                # follows ``__wrapped__``), so the executor keeps filtering
                # hallucinated arguments and injecting a file context exactly
                # as for the unwrapped tool.
                _draft_gate_wrapper.__wrapped__ = _original_handlers[_tool_name]  # type: ignore[attr-defined]
            executor.register(_tool_name, _draft_gate_wrapper)

    # ── Tool definitions ──────────────────────────────────────────────

    _draft_suffix = (
        " NOTE: knowledge changes are staged in an OpenCTI draft for human review, and "
        "no draft is active yet. Do NOT reuse or pick an existing draft on your own - "
        "enriching a draft the user did not intend must be avoided (creating a new one "
        "is fine once no draft exists, see below). "
        "First discover existing drafts: check the conversation history for draft IDs "
        "from earlier `create_opencti_draft` calls, and call `list_opencti_drafts`. "
        "If one or more drafts already exist, ASK the user whether to reuse one "
        "(present the list when there are several) or create a new one - never pick a "
        "draft yourself. Create a new draft (`create_opencti_draft`) only when the user "
        "asks for one OR no drafts exist yet; you may give it a short descriptive name "
        "yourself (no need to ask the user for the name). Activate the chosen draft with "
        "`use_opencti_draft`, then retry this tool."
    )

    # Draft-gate messaging is execution-context aware. Interactive chat keeps the
    # human-oriented flow (surface existing drafts, ask for a name). Autonomous
    # runs (assignments) are told a draft is auto-managed so they just proceed —
    # never stalling on a question to a user that isn't there.
    if autonomous:
        _gate_suffix = (
            " Note: you are running autonomously with no interactive user. A "
            "draft workspace is created automatically for your changes the moment "
            "you call this tool — just call it directly and proceed. Do NOT ask "
            "anyone and do NOT wait for confirmation."
        )
        _create_draft_desc = (
            "Create a new draft workspace in OpenCTI. All subsequent "
            "knowledge-mutation calls (create, update, delete) are scoped to it "
            "for later human review/validation. You are running autonomously: a "
            "draft is auto-created on the first mutation, so you normally do NOT "
            "need to call this. If you do, the name is optional (auto-generated "
            "when omitted) — never wait for user input."
        )
        _create_draft_name_desc = (
            "Optional name for the draft workspace. Auto-generated when omitted."
        )
        _create_draft_required: list[str] = []
    else:
        _gate_suffix = _draft_suffix
        _create_draft_desc = (
            "Create a new draft workspace in OpenCTI. All subsequent "
            "knowledge-mutation calls (create, update, delete entities/"
            "relationships) will be scoped to this draft. The user can "
            "then review and validate the changes before applying them. "
            "IMPORTANT: Before calling this tool, you MUST ask the user "
            "what name they want for the draft. Do NOT use a default name "
            "without asking first."
        )
        _create_draft_name_desc = (
            "Name for the draft workspace. "
            "MUST be provided by the user — ask them before calling this tool."
        )
        _create_draft_required = ["name"]

    _tools = [
        ToolDefinition(
            name="create_opencti_draft",
            priority=0,
            description=_create_draft_desc,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": _create_draft_name_desc,
                    },
                },
                "required": _create_draft_required,
            },
            source="integration:opencti",
        ),
    ]

    # Only expose use_opencti_draft when user is NOT already in a draft
    if not _draft_id:
        _tools.append(
            ToolDefinition(
                name="use_opencti_draft",
                priority=0,
                description=(
                    "Select a draft to work in (only needed when the user is NOT "
                    "viewing a draft in the UI). Call this with a draft ID from "
                    "conversation history to direct mutations to that draft."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "draft_id": {
                            "type": "string",
                            "description": "The ID of the draft to activate (UUID from a previous create_opencti_draft call).",
                        },
                    },
                    "required": ["draft_id"],
                },
                source="integration:opencti",
            ),
        )

    # Always available — lets the agent resolve draft IDs to names
    _tools.append(
        ToolDefinition(
            name="get_opencti_draft_info",
            priority=0,
            description=(
                "Get the name and link of a draft workspace. "
                "Call this to know the name of the current or any draft. "
                "If no draft_id is provided, returns info about the active draft."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "Draft ID to look up (optional — defaults to the currently active draft).",
                    },
                },
                "required": [],
            },
            source="integration:opencti",
        ),
    )

    # Always available — lets the agent list open drafts
    _tools.append(
        ToolDefinition(
            name="list_opencti_drafts",
            priority=0,
            description=(
                "List all open draft workspaces. Use this to find existing open drafts "
                "the user can choose from instead of creating a new one."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="integration:opencti",
        ),
    )
    _tools.append(
        ToolDefinition(
            name="delete_opencti_draft",
            priority=0,
            description=(
                "Delete an OpenCTI draft workspace and everything staged in it. "
                + (
                    "Allowed on any draft. "
                    if live_writes
                    else "Allowed on a draft created in this run, or the draft holding a case's "
                    "recorded threat package that a refused review discards. "
                    if gated_draft_validation
                    else "Allowed on a draft created in this run (create_opencti_draft). "
                )
                + "Answers the draft's name and confirms it no longer exists."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "The draft's id."},
                },
                "required": ["draft_id"],
            },
            source="integration:opencti",
        )
    )
    if _offer_apply:
        _tools.append(
            ToolDefinition(
                name="apply_opencti_draft",
                priority=0,
                description=(
                    "Validate (apply) an open OpenCTI draft workspace: its staged "
                    "changes are written to the live graph, with no further review. "
                    + (
                        "Offered because this integration allows live writes. "
                        if live_writes
                        else "A gate of this fleet decides it: the call waits for its approval "
                        "unless the fleet's level lets it run. "
                    )
                    + "Answers the draft's name and its final status."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string", "description": "The draft's id."},
                    },
                    "required": ["draft_id"],
                },
                source="integration:opencti",
            )
        )
    _tools += [
        ToolDefinition(
            name="list_opencti_labels",
            priority=0,
            description=(
                "List the labels OpenCTI holds (Settings > Labels): id, value and "
                "color, filtered by a search text, paginated with `after`. Use it, "
                "not the entity tools, to find labels."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text the label value contains."},
                    "limit": {"type": "integer", "description": "Labels per page (default 50, at most 500)."},
                    "after": {"type": "string", "description": "The cursor the previous page named."},
                },
                "required": [],
            },
            source="integration:opencti",
        ),
    ]
    if live_writes:
        _tools.append(
            ToolDefinition(
                name="create_opencti_label",
                priority=0,
                description=(
                    "Create a label in OpenCTI (platform configuration, written to the "
                    "live graph, never into a draft), or return the existing one when "
                    "OpenCTI already holds the same normalised value. Needs the "
                    "SETTINGS_SETLABELS capability. To tag an entity, use "
                    "add_opencti_label."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "description": "The label's value."},
                        "color": {"type": "string", "description": "Hex color such as #ff8800 (optional)."},
                    },
                    "required": ["value"],
                },
                source="integration:opencti",
            )
        )
        _tools.append(
            ToolDefinition(
                name="manage_opencti_label",
                priority=0,
                description=(
                    "Update a label's value and/or color, or delete the label, in OpenCTI "
                    "(Settings > Labels; live graph, never a draft). Needs SETTINGS_SETLABELS. "
                    "A deleted label leaves every object that carried it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["update", "delete"]},
                        "label": {"type": "string", "description": "The label's id or current value."},
                        "value": {"type": "string", "description": "update: the new value."},
                        "color": {"type": "string", "description": "update: the new hex color such as #ff8800."},
                    },
                    "required": ["action", "label"],
                },
                source="integration:opencti",
            )
        )

    _tools += [
        ToolDefinition(
            name="validate_nlq_filter",
            priority=0,
            description="Validate NLQ filter",
            input_schema={
                "type": "object",
                "properties": {
                    "filter_str": {
                        "type": "string",
                        "description": "NLQ filter in string format. A workflow_id status name or Status id becomes its template id.",
                    },
                },
                "required": ["filter_str"],
            },
            source="integration:opencti",
        ),
        # 1. Search
        ToolDefinition(
            name="search_opencti",
            priority=0,
            description=(
                "Search for entities in OpenCTI by keyword. Returns STIX domain objects "
                "(threat actors, malware, indicators, reports, incidents, etc.) and "
                "STIX cyber observables (IPs, domains, URLs, hashes, etc.), best match "
                "first. Use 'types' to filter by entity type."
                + _ENTITY_LISTING_CUT_NOTE
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search keyword (e.g. APT29, ransomware, 192.168.1.1, CVE-2024-1234)",
                    },
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by STIX entity types (optional). Examples: "
                            "Threat-Actor, Malware, Indicator, Report, Incident, "
                            "Campaign, Vulnerability, Intrusion-Set, Tool, "
                            "Attack-Pattern, Infrastructure, IPv4-Addr, Domain-Name, Url"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, max 50)",
                        "default": 10,
                    },
                },
                "required": ["search"],
            },
            source="integration:opencti",
        ),
        # 2. Get entity
        ToolDefinition(
            name="get_opencti_entity",
            priority=0,
            description=(
                "Get full details of a specific entity by its OpenCTI ID or STIX ID. "
                "Returns comprehensive information including the whole description "
                "and content (the listings cut long ones), labels, "
                "markings, author, external references, and type-specific fields."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity's OpenCTI internal ID or STIX ID (e.g. indicator--...)",
                    },
                },
                "required": ["entity_id"],
            },
            source="integration:opencti",
        ),
        # 3. Get relationship
        ToolDefinition(
            name="get_opencti_relationship",
            description=(
                "Get full details of a specific STIX core relationship by its OpenCTI ID "
                "or STIX ID. Returns the relationship type, source and target entities, "
                "description (in full), confidence, time range, labels, markings, and author."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "relationship_id": {
                        "type": "string",
                        "description": "The relationship's OpenCTI internal ID or STIX ID (e.g. relationship--...)",
                    },
                },
                "required": ["relationship_id"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 4. List entities (SDOs)
        ToolDefinition(
            name="list_opencti_entities",
            priority=0,
            description=(
                "List STIX domain objects from OpenCTI with optional type filter, search, "
                "and ordering. Use this to browse threat actors, malware, indicators, "
                "reports, incidents, campaigns, vulnerabilities, etc."
                + _ENTITY_LISTING_CUT_NOTE
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by entity types (optional). Examples: "
                            "Threat-Actor, Malware, Indicator, Report, Incident, "
                            "Campaign, Vulnerability, Intrusion-Set, Tool, "
                            "Attack-Pattern, Infrastructure, Note, Opinion, "
                            "Course-Of-Action, Grouping, Observed-Data"
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": "Search keyword (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 15, max 50)",
                        "default": 15,
                    },
                    "order_by": {
                        "type": "string",
                        "description": (
                            "A StixDomainObjectsOrdering key, e.g. created_at, updated_at, created, modified, "
                            "name, confidence, published, _score. Default: _score (relevance, best match first) "
                            "with a search, created_at otherwise. An invalid one is answered with all keys."
                        ),
                    },
                    "order_mode": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "description": "Sort order (default: desc)",
                        "default": "desc",
                    },
                    "filters": {
                        "type": "string",
                        "description": (
                            "OpenCTI FilterGroup as JSON string (optional). Use OpenCTI filter keys "
                            "(labels are objectLabel, author is createdBy, status is workflow_id: a status template id "
                            "from list_opencti_subtypes or a status name, resolved for 'types'); "
                            "keys OpenCTI rejects are listed in the error, so correct them and retry. "
                            'Example: {"mode":"and","filters":[{"key":"confidence","values":["80"],"operator":"gte"}],"filterGroups":[]}'
                        ),
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:opencti",
        ),
        # 5. List observables (SCOs)
        ToolDefinition(
            name="list_opencti_observables",
            priority=2,
            description=(
                "List STIX cyber observables from OpenCTI with optional type filter and search. "
                "Use this to browse IP addresses, domains, URLs, file hashes, etc. "
                "Optionally create a report from the results."
                + _ENTITY_LISTING_CUT_NOTE
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by observable types (optional). Examples: "
                            "IPv4-Addr, IPv6-Addr, Domain-Name, Url, Email-Addr, "
                            "StixFile, Hostname, Mac-Addr, User-Agent, Artifact, "
                            "Process, Software, Autonomous-System, X509-Certificate"
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": "Search keyword (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 15, max 50)",
                        "default": 15,
                    },
                    "order_by": {
                        "type": "string",
                        "description": (
                            "Sort key: entity_type, created_at, updated_at, observable_value (value), "
                            "objectMarking, createdBy, creator, _score. Default: _score (relevance, best "
                            "match first) with a search, created_at otherwise."
                        ),
                    },
                    "order_mode": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                },
                "required": [],
            },
            source="integration:opencti",
        ),
        # 6. List relationships
        ToolDefinition(
            name="list_opencti_relationships",
            description=(
                "List STIX core relationships of a specific entity. Shows what the entity "
                "is related to (e.g. 'indicates', 'uses', 'targets', 'attributed-to', "
                "'related-to', 'mitigates', 'delivers', 'exploits'). "
                "Returns source and target entity names, relationship type, and confidence."
                + _RELATIONSHIP_LISTING_CUT_NOTE
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity's OpenCTI ID",
                    },
                    "relationship_type": {
                        "type": "string",
                        "description": (
                            "Filter by relationship type (optional). Examples: "
                            "indicates, uses, targets, attributed-to, related-to, "
                            "mitigates, delivers, exploits, drops, communicates-with, "
                            "based-on, variant-of, part-of, located-at"
                        ),
                        "default": "",
                    },
                    "from_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter source entity types (optional)",
                    },
                    "to_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter target entity types (optional)",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["entity_id"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 7. Create relationship
        ToolDefinition(
            name="create_opencti_relationship",
            priority=0,
            description=(
                "Create a STIX core relationship between two entities in OpenCTI. "
                "The relationship connects a source entity (from) to a target entity (to) "
                "with a specific relationship type (e.g. 'indicates', 'uses', 'targets')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_id": {
                        "type": "string",
                        "description": "Source entity OpenCTI ID",
                    },
                    "to_id": {
                        "type": "string",
                        "description": "Target entity OpenCTI ID",
                    },
                    "relationship_type": {
                        "type": "string",
                        "description": (
                            "Relationship type. Examples: indicates, uses, targets, "
                            "attributed-to, related-to, mitigates, delivers, exploits, "
                            "drops, communicates-with, based-on, variant-of, part-of"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Relationship description (optional)",
                        "default": "",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence level 0-100 (optional)",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time ISO 8601 (optional)",
                        "default": "",
                    },
                    "stop_time": {
                        "type": "string",
                        "description": "Stop time ISO 8601 (optional)",
                        "default": "",
                    },
                    "kill_chain_phases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Kill chain phase IDs to attach (optional).",
                    },
                    **_STIX_METADATA_PROPS,
                },
                "required": ["from_id", "to_id", "relationship_type"],
            },
            source="integration:opencti",
        ),
        # 8. Create observable
        ToolDefinition(
            name="create_opencti_observable",
            description=(
                "Create a STIX cyber observable (IOC) in OpenCTI. Supports common "
                "observable types like IP addresses, domains, URLs, file hashes, etc. "
                "Optionally creates an associated indicator."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": (
                            "Observable type. Supported: "
                            + ", ".join(sorted(_OBSERVABLE_TYPES.keys()))
                        ),
                    },
                    "value": {
                        "type": "string",
                        "description": "The observable value (e.g. IP address, domain name, URL, file name)",
                    },
                    "x_opencti_score": {
                        "type": "integer",
                        "description": "Score 0-100 (optional)",
                    },
                    "x_opencti_description": {
                        "type": "string",
                        "description": "Description (optional)",
                        "default": "",
                    },
                    "create_indicator": {
                        "type": "boolean",
                        "description": "Also create a STIX indicator for this observable (default: false)",
                        "default": False,
                    },
                    **_STIX_METADATA_PROPS,
                },
                "required": ["type", "value"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 9. Create note
        ToolDefinition(
            name="create_opencti_note",
            description=(
                "Create an analyst note in OpenCTI attached to one or more entities. "
                "Use this to annotate findings, add context, or record analysis results. "
                "Markdown is supported in the content field."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Note content (Markdown supported)",
                    },
                    "object_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of entity IDs to attach the note to",
                    },
                    "attribute_abstract": {
                        "type": "string",
                        "description": "Short abstract/summary (optional, auto-generated from content if omitted)",
                        "default": "",
                    },
                    "note_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Note type(s): internal-note, external, assessment (default: internal-note)",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence level 0-100 (optional)",
                    },
                    **_STIX_METADATA_PROPS,
                },
                "required": ["content", "object_ids"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 10. Update SDO field
        ToolDefinition(
            name="update_opencti_field",
            description=(
                "Update a field on a STIX domain object in OpenCTI. Use this to change "
                "name, description, confidence, aliases, severity, and other fields. "
                "The value is always passed as a string or array of strings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity's OpenCTI ID",
                    },
                    "key": {
                        "type": "string",
                        "description": (
                            "Field name to update. Examples: name, description, confidence, "
                            "aliases, severity, x_opencti_score, revoked, first_seen, last_seen"
                        ),
                    },
                    "value": {
                        "description": "New value (string or array of strings)",
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                },
                "required": ["entity_id", "key", "value"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 11. Update relationship field
        ToolDefinition(
            name="update_opencti_relationship_field",
            description=(
                "Update a field on a STIX core relationship in OpenCTI. Use this to "
                "change description, confidence, start_time, stop_time, etc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "relationship_id": {
                        "type": "string",
                        "description": "The relationship's OpenCTI ID",
                    },
                    "key": {
                        "type": "string",
                        "description": "Field name. Examples: description, confidence, start_time, stop_time, revoked",
                    },
                    "value": {
                        "description": "New value (string or array of strings)",
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                },
                "required": ["relationship_id", "key", "value"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 12. Delete entity
        ToolDefinition(
            name="delete_opencti_entity",
            description=(
                "Delete any STIX core object (domain object or cyber observable) "
                "from OpenCTI by its ID. This is permanent and cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity's OpenCTI ID to delete",
                    },
                },
                "required": ["entity_id"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 12b. Merge same-type entities
        ToolDefinition(
            name="merge_opencti_entities",
            description=(
                "Merge two or more OpenCTI entities OF THE SAME TYPE into a "
                "single entity, to deduplicate a knowledge base - e.g. two "
                "'France' Country entities, duplicate Malware, or the same "
                "Threat Actor recorded twice. The target entity is kept; the "
                "source entities are DELETED and their aliases, relationships, "
                "container memberships and files are transferred onto the "
                "target. OpenCTI cannot stage a merge in a draft, so this "
                "applies immediately and CANNOT BE UNDONE: always "
                "show the user the exact entities you resolved and obtain their "
                "explicit approval before calling this tool. Pass IDs only - "
                "resolve duplicate candidates first with `search_opencti` or "
                "`list_opencti_entities`. Entities of different types cannot be "
                "merged (a Country cannot merge with a City), and at most "
                f"{_MERGE_MAX_PARTICIPANTS} entities in total (the target plus "
                "its sources) may be merged per call. In an autonomous run with "
                "human-in-the-loop enabled, this tool returns the exact merge "
                "plan and requires you to obtain approval via "
                "`request_human_input` before it will proceed. Requires the "
                "KNOWLEDGE_KNUPDATE_KNMERGE capability on the OpenCTI account."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": (
                            "OpenCTI ID of the entity to KEEP. Its attributes "
                            "win on conflict, so prefer the most complete or "
                            "most-connected of the duplicates."
                        ),
                    },
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "OpenCTI IDs of the duplicate entities to merge "
                            "into the target. These are permanently deleted. "
                            "Each must have the same entity_type as the target."
                        ),
                    },
                },
                "required": ["target_id", "source_ids"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 13. Delete relationship
        ToolDefinition(
            name="delete_opencti_relationship",
            description=(
                "Delete a STIX core relationship from OpenCTI by its ID. This is permanent and cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "relationship_id": {
                        "type": "string",
                        "description": "The relationship's OpenCTI ID to delete",
                    },
                },
                "required": ["relationship_id"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 14. Add label
        ToolDefinition(
            name="add_opencti_label",
            priority=2,
            description=(
                "Add a label to an entity, observable, relationship or sighting in "
                "OpenCTI and return the full updated entity or relationship. Creates "
                "the label automatically if it doesn't exist yet. "
                "Use labels for categorization "
                "(e.g. 'reviewed', 'high-priority', 'false-positive', 'apt')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "OpenCTI ID of the entity, observable, "
                            "relationship or sighting"
                        ),
                    },
                    "label_name": {
                        "type": "string",
                        "description": "Label name (e.g. 'reviewed', 'high-priority')",
                    },
                },
                "required": ["entity_id", "label_name"],
            },
            source="integration:opencti",
        ),
        # 15. Remove label
        ToolDefinition(
            name="remove_opencti_label",
            priority=2,
            description=(
                "Remove a label from an entity, observable, relationship or "
                "sighting in OpenCTI."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "OpenCTI ID of the entity, observable, "
                            "relationship or sighting"
                        ),
                    },
                    "label_name": {
                        "type": "string",
                        "description": "Label name to remove",
                    },
                },
                "required": ["entity_id", "label_name"],
            },
            source="integration:opencti",
        ),
        # 16. Add marking
        ToolDefinition(
            name="add_opencti_marking",
            priority=2,
            description=(
                "Add a marking definition (TLP, PAP, etc.) to an entity, observable, "
                "relationship or sighting in OpenCTI. "
                "Takes the marking definition's internal id, its STIX id or its name (TLP:CLEAR); "
                "list_opencti_markings lists them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "OpenCTI ID of the entity, observable, "
                            "relationship or sighting"
                        ),
                    },
                    "marking_definition_id": {
                        "type": "string",
                        "description": "The marking definition's OpenCTI ID",
                    },
                },
                "required": ["entity_id", "marking_definition_id"],
            },
            source="integration:opencti",
        ),
        # 17. Remove marking
        ToolDefinition(
            name="remove_opencti_marking",
            priority=2,
            description=(
                "Remove a marking definition from an entity, observable, "
                "relationship or sighting in OpenCTI."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "OpenCTI ID of the entity, observable, "
                            "relationship or sighting"
                        ),
                    },
                    "marking_definition_id": {
                        "type": "string",
                        "description": "The marking definition's OpenCTI ID",
                    },
                },
                "required": ["entity_id", "marking_definition_id"],
            },
            source="integration:opencti",
        ),
        # 18. Get container full
        ToolDefinition(
            name="get_opencti_container_full",
            priority=0,
            # Names the file tool only on a surface that offers it:
            # the loop and the MCP servers resolve it against their tools.
            description=_container_full_description(False),
            describe_for_surface=lambda surface: _container_full_description(
                _offers_file_download(surface)
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "container_id": {
                        "type": "string",
                        "description": (
                            "The OpenCTI ID or STIX ID of the container (Report, Grouping, Note, Case, etc.)"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max objects to return (default 200, max 1000). "
                            "Start with small limits and increase only if needed."
                        ),
                        "default": 200,
                    },
                },
                "required": ["container_id"],
            },
            source="integration:opencti",
        ),
        # 19. Create indicator
        ToolDefinition(
            name="create_opencti_indicator",
            priority=0,
            description=(
                "Create an indicator in OpenCTI. Supports any pattern language via "
                "pattern_type (stix [default], sigma, yara, snort, suricata, spl, "
                "eql, kql, shodan, tanium-signal, or any custom pattern_type_ov "
                "vocabulary value). For non-STIX languages supply the raw 'pattern' "
                "and set x_opencti_main_observable_type explicitly; for stix you may "
                "instead pass observable_type + observable_value (+ hash_algorithm "
                "for file hashes) to auto-generate the pattern. Set "
                "x_opencti_detection=true to flag a detection rule, link observables "
                "via based_on, attach markings/labels/author, and set "
                "create_observables=true to also create the corresponding "
                "observable(s)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Indicator name (defaults to the pattern if omitted)",
                        "default": "",
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Detection/observable pattern in the language given by "
                            "pattern_type. For stix e.g. \"[ipv4-addr:value = '1.2.3.4']\" "
                            "or \"[file:hashes.'SHA-256' = 'abc...']\"; for "
                            "kql/sigma/yara/spl/etc. the raw rule text. If omitted "
                            "(stix only), provide observable_type + observable_value."
                        ),
                        "default": "",
                    },
                    "pattern_type": {
                        "type": "string",
                        "description": (
                            "Pattern language: stix (default), sigma, yara, snort, "
                            "suricata, spl, eql, kql, shodan, tanium-signal, or any "
                            "value present in the platform's pattern_type_ov vocabulary."
                        ),
                        "default": "stix",
                    },
                    "pattern_version": {
                        "type": "string",
                        "description": "Optional pattern language version.",
                        "default": "",
                    },
                    "observable_type": {
                        "type": "string",
                        "description": (
                            "Observable type for STIX auto-pattern generation. Supported: "
                            + ", ".join(sorted(_STIX_PATTERN_MAP.keys()))
                        ),
                        "default": "",
                    },
                    "observable_value": {
                        "type": "string",
                        "description": "Observable value for auto-pattern generation (e.g. IP, domain, hash, URL)",
                        "default": "",
                    },
                    "hash_algorithm": {
                        "type": "string",
                        "description": "Hash algorithm when creating file hash indicators: MD5, SHA-1, SHA-256, SHA-512, SSDEEP",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "Indicator description",
                        "default": "",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence level 0-100",
                    },
                    "x_opencti_score": {
                        "type": "integer",
                        "description": "OpenCTI score 0-100",
                    },
                    "x_opencti_main_observable_type": {
                        "type": "string",
                        "description": (
                            "Main observable type (e.g. IPv4-Addr, Domain-Name, Url, "
                            "StixFile). Auto-inferred from STIX patterns; set it "
                            "explicitly for non-STIX detections (kql, sigma, …)."
                        ),
                        "default": "",
                    },
                    "x_opencti_detection": {
                        "type": "boolean",
                        "description": "Flag the indicator as a detection rule.",
                    },
                    "indicator_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Indicator types: malicious-activity, anomalous-activity, benign, compromised, unknown",
                    },
                    "kill_chain_phases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Kill chain phase IDs to attach.",
                    },
                    "valid_from": {
                        "type": "string",
                        "description": "Valid from date (ISO 8601)",
                        "default": "",
                    },
                    "valid_until": {
                        "type": "string",
                        "description": "Valid until date (ISO 8601)",
                        "default": "",
                    },
                    "revoked": {
                        "type": "boolean",
                        "description": "Mark the indicator as revoked.",
                    },
                    "based_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Observable IDs to link via 'based-on' at creation "
                            "(creates the indicator→observable based-on relationships)."
                        ),
                    },
                    "object_organization": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Organization IDs to restrict/share the indicator with.",
                    },
                    **_STIX_METADATA_PROPS,
                    "create_observables": {
                        "type": "boolean",
                        "description": "Also create corresponding STIX observable(s) from the pattern (default: false)",
                        "default": False,
                    },
                },
                "required": [],
            },
            source="integration:opencti",
        ),
        # 20. Create container
        ToolDefinition(
            name="create_opencti_container",
            description=(
                "Create a container in OpenCTI: Report, Case-Incident, Case-Rfi "
                "(Request for Information), Case-Rft (Request for Takedown), or "
                "Grouping. Containers hold references to other STIX objects. "
                "Optionally pass object_ids to add entities/relationships at creation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "container_type": {
                        "type": "string",
                        "enum": sorted(_CONTAINER_MUTATIONS.keys()),
                        "description": "Container type to create",
                    },
                    "name": {"type": "string", "description": "Container name"},
                    "description": {
                        "type": "string",
                        "description": "Description (Markdown supported)",
                        "default": "",
                    },
                    "content": {
                        "type": "string",
                        "description": "Rich content body (Markdown, optional)",
                        "default": "",
                    },
                    "published": {
                        "type": "string",
                        "description": "Publication date (ISO 8601). Required for Report.",
                        "default": "",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Grouping context. Required for Grouping. "
                            "Examples: suspicious-activity, malware-analysis, unspecified"
                        ),
                        "default": "",
                    },
                    "report_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Report types (for Report only): threat-report, internal-report, attack-pattern, tool, etc.",
                    },
                    "severity": {
                        "type": "string",
                        "description": "Severity (for Case types): low, medium, high, critical",
                        "default": "",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Priority (for Case types): P1, P2, P3, P4",
                        "default": "",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence level 0-100",
                    },
                    "object_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of entity/relationship IDs to include in the container at creation",
                    },
                    "case_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The case's own types, for Case types only: the RFI's "
                            "information types (Case-Rfi), the takedown types "
                            "(Case-Rft) or the response types (Case-Incident). "
                            "Values of that OpenCTI vocabulary."
                        ),
                    },
                    **_STIX_METADATA_PROPS,
                },
                "required": ["container_type", "name"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 21. Create entity (SDO)
        ToolDefinition(
            name="create_opencti_entity",
            description=(
                "Create a STIX domain object (entity) in OpenCTI. Supports: "
                + ", ".join(sorted(_SDO_MUTATIONS.keys()))
                + ". Use this for threat actors, malware, campaigns, incidents, "
                "vulnerabilities, attack patterns, tools, intrusion sets, etc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": sorted(_SDO_MUTATIONS.keys()),
                        "description": "STIX domain object type to create",
                    },
                    "name": {"type": "string", "description": "Entity name"},
                    "description": {
                        "type": "string",
                        "description": "Description (Markdown supported)",
                        "default": "",
                    },
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Alternative names / aliases (for types that support them)",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence level 0-100",
                    },
                    "first_seen": {
                        "type": "string",
                        "description": "First seen date (ISO 8601, for Campaign, Incident, Malware, etc.)",
                        "default": "",
                    },
                    "last_seen": {
                        "type": "string",
                        "description": "Last seen date (ISO 8601)",
                        "default": "",
                    },
                    "severity": {
                        "type": "string",
                        "description": "Severity (for Incident): low, medium, high, critical",
                        "default": "",
                    },
                    "objective": {
                        "type": "string",
                        "description": "Objective (for Campaign)",
                        "default": "",
                    },
                    "malware_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Malware types (for Malware): backdoor, ransomware, trojan, worm, dropper, etc.",
                    },
                    "is_family": {
                        "type": "boolean",
                        "description": "Whether this is a malware family (for Malware, default: true for new entries)",
                    },
                    "threat_actor_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Threat actor types (for Threat-Actor-*): nation-state, criminal, hacktivist, insider, etc.",
                    },
                    "tool_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tool types (for Tool): exploitation, remote-access, information-gathering, etc.",
                    },
                    "infrastructure_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Infrastructure types (for Infrastructure): botnet, command-and-control, hosting-malware, etc.",
                    },
                    **_STIX_METADATA_PROPS,
                },
                "required": ["type", "name"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 22. Add to container
        ToolDefinition(
            name="add_opencti_to_container",
            description=(
                "Add one or more entities or relationships to an OpenCTI container "
                "(Report, Case, Grouping, Note, etc.). Pass the container ID and "
                "a list of entity/relationship IDs to add."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "container_id": {
                        "type": "string",
                        "description": "The container's OpenCTI ID",
                    },
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of entity or relationship IDs to add to the container",
                    },
                },
                "required": ["container_id", "entity_ids"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 23. Remove from container
        ToolDefinition(
            name="remove_opencti_from_container",
            description=(
                "Remove one or more entities or relationships from an OpenCTI container "
                "(Report, Case, Grouping, Note, etc.). Pass the container ID and "
                "a list of entity/relationship IDs to remove."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "container_id": {
                        "type": "string",
                        "description": "The container's OpenCTI ID",
                    },
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of entity or relationship IDs to remove from the container",
                    },
                },
                "required": ["container_id", "entity_ids"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 24. Promote observable to indicator
        ToolDefinition(
            name="promote_opencti_observable",
            description=(
                "Promote an existing STIX Cyber Observable to a STIX Indicator. "
                "Reads the observable, auto-generates the STIX pattern, creates "
                "the indicator, and links them with a 'based-on' relationship. "
                "Works with IPs, domains, URLs, emails, file hashes, hostnames, "
                "and other common observable types."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The observable's OpenCTI ID to promote",
                    },
                    "x_opencti_score": {
                        "type": "integer",
                        "description": "Score to assign to the new indicator (0-100)",
                    },
                    "indicator_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Indicator types: malicious-activity, anomalous-activity, benign, compromised, unknown",
                    },
                },
                "required": ["entity_id"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 25. Upload file to entity
        ToolDefinition(
            name="upload_opencti_file",
            description=(
                "Upload or attach a file to an existing OpenCTI entity. "
                "Supports any entity type (reports, incidents, indicators, "
                "observables, etc.). Use this to attach PDFs, images, CSVs, "
                "STIX bundles, or any document to an entity.\n\n"
                "Uploaded files: to attach a file the user already uploaded to "
                "this conversation AS-IS (byte-for-byte), pass source_file_id "
                "with its file_id from the Available Files list — preferred when "
                "the user attached a document/PDF/image and wants THAT file on "
                "the entity (do NOT re-author it).\n\n"
                "PDF support: to attach a real, print-ready PDF you author, set "
                "file_name ending in '.pdf' (or mime_type='application/pdf') "
                "and pass your report as Markdown or HTML in file_content — "
                "the platform renders it to a styled A4 PDF server-side before "
                "uploading. Do NOT base64-encode Markdown/HTML when targeting a "
                "PDF, and do NOT tell the user PDF generation is unsupported. "
                "Only set encoding='base64' when you already have pre-rendered "
                "binary bytes (e.g. an image or a PDF you downloaded elsewhere). "
                "Provide source_file_id OR file_content, not both."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The OpenCTI entity ID to attach the file to",
                    },
                    "file_name": {
                        "type": "string",
                        "description": (
                            "File name with extension "
                            "(e.g. 'report.pdf', 'iocs.csv', 'analysis.md'). "
                            "A '.pdf' extension triggers server-side PDF "
                            "rendering of Markdown/HTML file_content. Optional "
                            "with source_file_id (defaults to the uploaded "
                            "file's own name)."
                        ),
                    },
                    "file_content": {
                        "type": "string",
                        "description": (
                            "File content — plain text (UTF-8) by default, "
                            "or base64-encoded if encoding is set to 'base64'. "
                            "For PDF output, supply Markdown or HTML text here "
                            "(the platform renders it to a real PDF via a "
                            "headless browser). Omit when using source_file_id."
                        ),
                    },
                    "source_file_id": {
                        "type": "string",
                        "description": (
                            "Attach a file the user already uploaded to this "
                            "conversation BYTE-FOR-BYTE, by its file_id (from the "
                            "Available Files list). Use this to forward an "
                            "existing PDF/document/image as-is instead of "
                            "re-authoring it. Mutually exclusive with "
                            "file_content."
                        ),
                    },
                    "encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "description": (
                            "Content encoding: 'text' for UTF-8 text / Markdown "
                            "/ HTML content (default, including PDF rendering — "
                            "the backend handles the conversion), 'base64' only "
                            "for pre-rendered binary files"
                        ),
                        "default": "text",
                    },
                    "mime_type": {
                        "type": "string",
                        "description": (
                            "MIME type (e.g. 'text/csv', 'application/pdf', "
                            "'text/markdown', 'application/json'). Setting "
                            "'application/pdf' (or a '.pdf' file_name) renders "
                            "Markdown/HTML file_content to a real PDF. "
                            "Default: 'application/octet-stream'"
                        ),
                        "default": "application/octet-stream",
                    },
                    "no_trigger_import": {
                        "type": "boolean",
                        "description": (
                            "Skip triggering import connectors for this file "
                            "(default: true — just attach the file)"
                        ),
                        "default": True,
                    },
                },
                # ``file_name`` / ``file_content`` are no longer hard-required:
                # the source_file_id path supplies bytes + name from the
                # uploaded file. The handler validates that exactly one content
                # source (file_content or source_file_id) is provided.
                "required": ["entity_id"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 25b. Download file from entity
        ToolDefinition(
            name="download_opencti_file",
            workspace_ingest=True,
            description=(
                "Download a file from OpenCTI by its file ID. File IDs are "
                "returned by get_opencti_container_full in the Attached Files "
                "section. For text files (CSV, JSON, XML, Markdown), returns "
                "the text content directly. For binary files (PDF, DOCX, XLSX, "
                "images), the file is attached for native analysis by the AI "
                "model — no manual text extraction needed. Use this to read "
                "the content of files attached to reports, cases, and other "
                "containers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": (
                            "The file ID from OpenCTI (from the importFiles "
                            "field returned by get_opencti_container_full)"
                        ),
                    },
                },
                "required": ["file_id"],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 26. Import dashboard
        ToolDefinition(
            name="import_opencti_dashboard",
            description=(
                "Import a custom dashboard into OpenCTI from a JSON "
                "configuration. Returns the new workspace/dashboard ID. "
                "Every STIX ID referenced in the widget filters is verified "
                "against the OpenCTI knowledge base first; if any "
                "referenced entity does not exist the import is rejected and "
                "the offending IDs are returned, so resolve real STIX IDs via "
                "search_opencti rather than inventing them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_json": {
                        "type": "string",
                        "description": (
                            "The dashboard configuration as a JSON string. "
                            "With raw_widgets=false (default), provide the "
                            "full OpenCTI export: 'type' ('dashboard'), "
                            "'openCTI_version', and a 'configuration' object "
                            "with 'name' and a base64-encoded 'manifest'. "
                            "With raw_widgets=true, provide raw widgets: "
                            "{'name': ..., 'description': ..., 'widgets': "
                            "{'ID': {...}, ...}} and the manifest is built "
                            "and base64-encoded automatically."
                        ),
                    },
                    "raw_widgets": {
                        "type": "boolean",
                        "description": (
                            "Set true when dashboard_json contains a raw "
                            "'widgets' object instead of a pre-encoded "
                            "OpenCTI export manifest. Defaults to false."
                        ),
                    },
                    "file_id": {
                        "type": "string",
                        "description": (
                            "Instead of dashboard_json: a file of the run "
                            "holding the dashboard export (attached, or "
                            "downloaded from the XTM Hub)."
                        ),
                    },
                },
                "required": [],
            },
            source="integration:opencti",
            priority=1,
        ),
        # 27. Entity time-series stats
        ToolDefinition(
            name="get_opencti_entity_stats",
            priority=0,
            description=(
                "Get time-series statistics for a STIX entity over time. "
                "Returns counts bucketed by time period (month/week/day). "
                "Use stat_type to choose what to count: "
                "'indicators' (IOCs related to the entity), "
                "'victims' (targeted identities/locations), "
                "'containers' (reports/groupings/cases mentioning the entity), "
                "'targeting' (threat entities targeting this entity — for victim entities). "
                "Useful for trend analysis, activity summaries, and forecasting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity's OpenCTI ID or STIX ID",
                    },
                    "stat_type": {
                        "type": "string",
                        "enum": ["indicators", "victims", "containers", "targeting"],
                        "description": (
                            "What to count: 'indicators' (IOCs), 'victims' (targeted entities), "
                            "'containers' (reports/cases), 'targeting' (threats targeting this entity)"
                        ),
                        "default": "indicators",
                    },
                    "interval": {
                        "type": "string",
                        "enum": ["month", "week", "day"],
                        "description": "Time bucket interval (default: month)",
                        "default": "month",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date in ISO 8601 format (default: 24 months ago)",
                        "default": "",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in ISO 8601 format (default: now)",
                        "default": "",
                    },
                },
                "required": ["entity_id"],
            },
            source="integration:opencti",
        ),
        # 28. Top-N related entities
        ToolDefinition(
            name="get_opencti_top_related",
            priority=0,
            description=(
                "Get top-N related entities by type for a STIX entity. "
                "Returns a ranked distribution of related entities. "
                "Use related_type to choose: "
                "'victims' (top targeted entities), 'threats' (top threat actors), "
                "'techniques' (top ATT&CK techniques used), 'malware' (top malware families). "
                "Useful for understanding an entity's relationships and patterns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity's OpenCTI ID or STIX ID",
                    },
                    "related_type": {
                        "type": "string",
                        "enum": ["victims", "threats", "techniques", "malware"],
                        "description": (
                            "What to rank: 'victims' (targeted entities), 'threats' (threat actors), "
                            "'techniques' (ATT&CK patterns), 'malware' (malware families)"
                        ),
                        "default": "victims",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10, max 50)",
                        "default": 10,
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Filter relationships from this date (ISO 8601, optional)",
                        "default": "",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Filter relationships until this date (ISO 8601, optional)",
                        "default": "",
                    },
                },
                "required": ["entity_id"],
            },
            source="integration:opencti",
        ),
        # 29. Entity audit history
        ToolDefinition(
            name="get_opencti_entity_history",
            priority=0,
            description=(
                "Get the modification history (audit log) for a STIX entity. "
                "Returns the most recent log entries showing who changed what and when, "
                "including timestamps, user names, action types (create, update, delete, merge), "
                "and context (changed fields, messages). Useful for understanding "
                "how an entity has evolved over time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity's OpenCTI ID or STIX ID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max log entries to return (default 100, max 200)",
                        "default": 100,
                    },
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by event types (optional). "
                            "Values: create, update, delete, merge"
                        ),
                    },
                },
                "required": ["entity_id"],
            },
            source="integration:opencti",
        ),
    ]

    # When no draft context is active, augment descriptions of
    # knowledge-mutation tools so the LLM knows they require a draft.
    # When a draft IS active (from header), the agent is forced to work
    # inside it — inform it clearly so there's no ambiguity.
    if _draft_id:
        _draft_active_suffix = (
            f" [ACTIVE DRAFT CONTEXT — LOCKED BY UI] "
            f"The user is currently viewing draft `{base_url}/dashboard/data/import/draft/{_draft_id}`. "
            f"All mutations go ONLY to this draft — you CANNOT target any other draft. "
            f"If the user asks you to create/modify something in a DIFFERENT draft, "
            f"you MUST REFUSE and explain that they are currently working inside this draft "
            f"and need to switch drafts in the UI first. "
            f"Use `get_opencti_draft_info` to get the current draft name."
        )
        _tools = [
            dataclasses.replace(t, description=t.description + _draft_active_suffix)
            if t.name in _DRAFT_REQUIRED_TOOLS
            else t
            for t in _tools
        ]
    elif not _skip_gate:
        _tools = [
            dataclasses.replace(t, description=t.description + _gate_suffix)
            if t.name in _DRAFT_REQUIRED_TOOLS
            else t
            for t in _tools
        ]

    # ── Dev/debug PoC tools (DEBUG only) ──────────────────────────────
    # Hello-world / JSON-wrap / file-hash proof-of-concept helpers. They add
    # tool-picker noise and context tokens with zero production value, so they
    # are exposed only when DEBUG is enabled (default off). The handlers are
    # registered under the same flag, so defs and handlers stay in lockstep.
    if settings.debug:
        _tools += [
            ToolDefinition(
                name="dev_debug_hello",
                description="Returns the text 'hello world'. No input required.",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                source=f"integration:{_PROVIDER}",
                priority=2,
            ),
            ToolDefinition(
                name="dev_debug_json_wrap",
                description=(
                    "Wraps the given text into a JSON object with an 'output' attribute. "
                    "Returns '{\"output\": \"TEXT\"}'. Useful for post-processing."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The text to wrap in JSON",
                        },
                    },
                    "required": ["text"],
                },
                source=f"integration:{_PROVIDER}",
                priority=2,
            ),
            ToolDefinition(
                name="dev_debug_json_wrap_raw",
                description=(
                    "Wraps the given text into a dict with an 'output' attribute. "
                    "Returns the dict directly (auto-serialized to JSON)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The text to wrap",
                        },
                    },
                    "required": ["text"],
                },
                source=f"integration:{_PROVIDER}",
                priority=2,
            ),
            ToolDefinition(
                name="dev_debug_append_hello",
                description="Appends ' hello' to the input text. Useful as a simple prepare/finalize tool.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The text to append hello to",
                        },
                    },
                    "required": ["text"],
                },
                source=f"integration:{_PROVIDER}",
                priority=2,
            ),
            ToolDefinition(
                name="dev_files_hash",
                description=(
                    "Compute MD5, SHA-1, and SHA-256 hashes for every file attached to the "
                    "current conversation. Returns a JSON array of file metadata + hashes. "
                    "Useful for verifying file integrity."
                ),
                input_schema={"type": "object", "properties": {}},
                source=f"integration:{_PROVIDER}",
                priority=2,
            ),
        ]

    return _tools
