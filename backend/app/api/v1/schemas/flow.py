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

"""Request/response schemas for the visual flow builder API.

The canvas document (``graph``) is deliberately typed as a free-form ``dict``
here: its real contract lives in ``app/services/flow_graph.py``, which produces
structured, node-addressed issues the builder can pin on the canvas. Pydantic
would only be able to answer "invalid", with no way to say *which node*.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, field_validator

from app.models.flow import FLOW_STATUSES
from app.services.flow_graph import MAX_EDGES, MAX_NODES

#: The ``flow_event_types.name`` column is ``VARCHAR(120)``.
EVENT_TYPE_NAME_MAX_LENGTH = 120

# Stripped from both ends of an event type name: everything `str.isspace()`
# counts as whitespace (all of it lives in the Basic Multilingual Plane) plus
# the zero-width characters, which are not whitespace - a name made only of
# them is as invisible as an empty one. Other format characters are content:
# the tag characters closing a subdivision flag emoji are format characters
# too. One `str.strip` over the whole set stays linear on any padding; a
# trailing-anchored regex would not.
_EVENT_TYPE_NAME_PADDING = (
    "".join(ch for ch in map(chr, range(0x10000)) if ch.isspace())
    + "\u200b\u200c\u200d\u2060\ufeff"
)


def _strip_event_type_name(value: Any) -> Any:
    return value.strip(_EVENT_TYPE_NAME_PADDING) if isinstance(value, str) else value


# Stripped BEFORE the length check: validating the raw value let "   " through
# `min_length=1`, and the handler then stored an empty, unpickable name.
EventTypeName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=EVENT_TYPE_NAME_MAX_LENGTH),
    BeforeValidator(_strip_event_type_name),
]


class FlowValidationIssueResponse(BaseModel):
    code: str
    message: str
    severity: str
    node_id: str | None = None
    edge_id: str | None = None
    field: str | None = None


class FlowValidationResponse(BaseModel):
    valid: bool
    issues: list[FlowValidationIssueResponse] = Field(default_factory=list)


class FlowSummaryResponse(BaseModel):
    """List-view shape — no graph payload, so the library page stays cheap."""

    id: str
    user_id: str | None = None
    owner_name: str | None = None
    name: str
    description: str
    status: str
    version_number: int
    node_count: int
    tags: list[str] = Field(default_factory=list)
    is_company_managed: bool
    shared_group_ids: list[str] | None = None
    #: Whether the REQUESTING user may edit it - ``helpers.can_manage``, share
    #: tiers included, so the builder never opens writable on a flow the API
    #: refuses to save.
    can_manage: bool = False
    #: Not company-managed and its owner's account is deactivated: nothing starts it
    #: until the account is reactivated. Derived, never stored.
    paused_by_owner_deactivation: bool = False
    created_at: str
    updated_at: str


class FlowResponse(FlowSummaryResponse):
    graph: dict[str, Any]
    # Warnings from the last validation, so the canvas can render them without
    # a second round-trip after loading.
    issues: list[FlowValidationIssueResponse] = Field(default_factory=list)


class FlowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    graph: dict[str, Any] | None = None
    tags: list[str] | None = None
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None


class FlowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    graph: dict[str, Any] | None = None
    status: str | None = Field(default=None, pattern="|".join(FLOW_STATUSES))
    tags: list[str] | None = None
    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    user_id: str | None = None
    commit_message: str | None = Field(default=None, max_length=500)
    # Optimistic lock: the flow version the client LOADED. When provided with
    # a graph and the server has advanced past it, the save is refused (409)
    # instead of silently overwriting the concurrent change.
    base_version: int | None = Field(default=None, ge=1)


class FlowCallableToolResponse(BaseModel):
    """One directly callable tool (built-in or custom) with its signature,
    so the Call-tool node's drawer can offer a picker + a typed form."""

    name: str
    label: str
    description: str = ""
    source: str = "custom"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # A custom tool's id, which the drawer stores next to the name it picks
    # (`tool_id`, `candidate_tool_ids`). None for a built-in.
    id: str | None = None
    # The one custom tool of this name a node that stores the name alone
    # runs for the caller. Several tools may share a name; each can be picked.
    designated: bool = False
    # Why the caller can use it: "own", "company", "shared" or "other" (an
    # admin's reach), and whose it is, to tell tools of one name apart. None
    # for a built-in.
    origin: str | None = None
    owner_name: str | None = None


class FlowIntegrationOperationResponse(BaseModel):
    """One callable operation of a connected integration — a ToolDefinition
    surfaced to the builder so the Integration node's drawer can offer a
    picker + a parameters form instead of free text and raw JSON."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class FlowEventFieldSchema(BaseModel):
    """One typed field of a declared event — the signature's parameter."""

    # Identifier-shaped: the name becomes a `{{node.fields.<name>}}` path
    # segment, so dots/spaces would make every reference ambiguous.
    name: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: str = Field(default="string", pattern="^(string|number|boolean|json)$")
    required: bool = False
    description: str = Field(default="", max_length=500)


class FlowEventTypeCreate(BaseModel):
    """Every writer declares through this - the REST route and the flow-builder
    agent tool - so a stored declaration always passes the same rules."""

    name: EventTypeName
    description: str = Field(default="", max_length=2000)
    fields: list[FlowEventFieldSchema] = Field(default_factory=list, max_length=32)


class FlowEventTypeUpdate(BaseModel):
    name: EventTypeName | None = None
    description: str | None = Field(default=None, max_length=2000)
    fields: list[FlowEventFieldSchema] | None = Field(default=None, max_length=32)


class FlowEventFieldResponse(BaseModel):
    """A stored field, served as stored: the request rules above apply on
    write only, so a row stored before one of them held still lists."""

    name: str
    type: str = "string"
    required: bool = False
    description: str = ""


class FlowEventTypeResponse(BaseModel):
    id: str
    user_id: str | None = None
    name: str
    description: str
    fields: list[FlowEventFieldResponse] = Field(default_factory=list)
    is_company_managed: bool
    created_at: str
    updated_at: str


class FlowValidateRequest(BaseModel):
    """Dry validation — the builder calls this as the canvas changes."""

    graph: dict[str, Any]


class FlowOutputSchemasRequest(BaseModel):
    """A draft graph whose nodes' declared outputs are asked for.

    Held to the size a save accepts: the answer scans every edge once per
    agent node, so an unbounded draft is refused here, as a 422.
    """

    graph: dict[str, Any]

    @field_validator("graph")
    @classmethod
    def _within_the_flow_limits(cls, graph: dict[str, Any]) -> dict[str, Any]:
        for key, limit in (("nodes", MAX_NODES), ("edges", MAX_EDGES)):
            items = graph.get(key)
            if isinstance(items, list) and len(items) > limit:
                raise ValueError(f"A flow holds at most {limit} {key}.")
        return graph


class FlowOutputSchemasResponse(BaseModel):
    """Node id → output name → the JSON Schema that output declares."""

    nodes: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)


class FlowVersionListItemResponse(BaseModel):
    id: str
    version_number: int
    author_id: str | None = None
    author_name: str | None = None
    commit_message: str | None = None
    is_active: bool = False
    is_locked: bool = False
    node_count: int = 0
    created_at: str


class FlowVersionListResponse(BaseModel):
    items: list[FlowVersionListItemResponse]
    total: int
    # How many versions are retained per flow, so the UI can say so plainly.
    max_entries: int


class FlowVersionDetailResponse(FlowVersionListItemResponse):
    graph: dict[str, Any]


class FlowRestoreRequest(BaseModel):
    commit_message: str | None = Field(default=None, max_length=500)


class FlowImportRequest(BaseModel):
    """A flow document — native XTM One, LangFlow or n8n export."""

    document: dict[str, Any]
    # Overrides the name carried in the document.
    name: str | None = Field(default=None, max_length=200)


class FlowImportReportResponse(BaseModel):
    format: str
    nodes_imported: int
    nodes_unresolved: int
    # Nodes mapped to a local kind but carrying review notes: they will not
    # run until someone clears them (`source_payload.review`).
    nodes_to_review: int = 0
    edges_imported: int
    edges_dropped: int
    # What the import could not carry over, flow-wide (credentials, pinned
    # data, ...). Per-node detail lives on the nodes themselves.
    warnings: list[str] = Field(default_factory=list)


class FlowImportResponse(BaseModel):
    flow: FlowResponse
    report: FlowImportReportResponse


# ─── Node catalog ────────────────────────────────────────────────────────────


class FlowCatalogOptionResponse(BaseModel):
    value: str
    label: str
    # Optional list-view metadata for multi-select fields.
    description: str | None = None
    # A lucide icon name, or an http(s) URL for a custom icon.
    icon: str | None = None
    # Model options only: which configured provider publishes this model. Model
    # ids are not unique across providers, so the builder needs this to narrow
    # the model list once an LLM node pins a provider.
    provider_id: str | None = None


class FlowCatalogCategoryResponse(BaseModel):
    id: str
    label: str


class FlowNodeCatalogResponse(BaseModel):
    """Everything the builder needs to render the library and config forms."""

    categories: list[FlowCatalogCategoryResponse]
    node_kinds: list[dict[str, Any]]
    edge_kinds: list[str]
    # Live option lists for `select` fields that declare a `source`, keyed by
    # that source name (`agents`, `integrations`, `assignments`, ...). Scoped to
    # what the calling user can actually see.
    sources: dict[str, list[FlowCatalogOptionResponse]]
    limits: dict[str, int]


class FlowDetachRequest(BaseModel):
    """Detach a selection into its own flow.

    ``graph`` is the canvas as the user currently sees it — sent along so the
    split happens on screen state rather than on the last save, which would
    silently detach the wrong thing when the editor has unsaved changes.

    ``mode="collapse"`` is the UE "Collapse to Function": the selection
    becomes a sub-flow with a parameterised Manual trigger, and the parent
    keeps a wired ``flow.call`` in its place.
    """

    mode: str = Field(default="detach", pattern="^(detach|collapse)$")
    graph: dict[str, Any]
    node_ids: list[str] = Field(min_length=1)
    name: str | None = Field(default=None, max_length=200)


class FlowDetachReportResponse(BaseModel):
    nodes_detached: int
    edges_detached: int
    # Edges that crossed the selection boundary and could not survive the split.
    edges_dropped: int
    # `{{node.field}}` references the split broke. A detach cuts both ways: the
    # child may read from a node that stayed, and a node that stayed may read
    # from one that left — so both halves are reported.
    references_removed: list[str] = Field(default_factory=list)
    parent_references_removed: list[str] = Field(default_factory=list)


class FlowDetachResponse(BaseModel):
    """Both halves, so the builder can refresh the parent without re-fetching."""

    flow: FlowResponse
    parent: FlowResponse
    report: FlowDetachReportResponse


# ─── Execution ───────────────────────────────────────────────


class FlowRunRequest(BaseModel):
    """Manual trigger.

    ``prompt`` is the user's message for this run — read downstream as
    ``{{<trigger>.prompt}}``; when omitted, the trigger node's configured
    default applies.  ``parameters`` fill the node's declared inputs and
    surface as ``{{<trigger>.payload.<key>}}``.
    """

    prompt: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    # Files attached to this run (uploaded first via POST /flows/{id}/files).
    # They reach every agent node of the run, the way chat attachments reach
    # an agent, and surface as ``{{<trigger>.files}}``.
    file_ids: list[str] = Field(default_factory=list)


class FlowChatEntryResponse(BaseModel):
    """One conversational entry the chat surface exposes for a flow.

    An ACTIVE flow whose Chat start point leads to a chat-enabled agent node
    appears in the chat picker under the FLOW's name; the conversation binds
    to the underlying agent.
    """

    flow_id: str
    flow_name: str
    description: str | None = None
    agent_id: str
    greeting: str | None = None


class FlowFileUploadResponse(BaseModel):
    """One uploaded run attachment, ready to reference in a run request."""

    file_id: str
    filename: str
    content_type: str
    size: int


class FlowActivateResponse(BaseModel):
    flow: FlowResponse
    # Present when the graph carries a `trigger.webhook` node: the inbound
    # URL path template and the freshly minted (or existing) shared secret.
    # The secret is shown here and on subsequent activations only — never in
    # list/detail payloads.
    webhook_url: str | None = None
    webhook_secret: str | None = None
    warnings: list[FlowValidationIssueResponse] = Field(default_factory=list)


class FlowRunStepResponse(BaseModel):
    id: str
    seq: int
    node_id: str
    node_kind: str
    iteration: int
    status: str
    # The node's RENDERED configuration — the values it computed with.
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    # A completed step whose answer read as an error.
    warning: str | None = None
    started_at: str
    completed_at: str | None = None


class FlowRunSummaryResponse(BaseModel):
    id: str
    flow_id: str
    flow_version_number: int
    status: str
    trigger_type: str
    depth: int
    parent_run_id: str | None = None
    result_summary: str | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    # Filled on the cross-flow listing (`GET /flows/runs/all`), where rows
    # from many flows sit side by side; per-flow listings leave it None.
    flow_name: str | None = None


class FlowRunListResponse(BaseModel):
    runs: list[FlowRunSummaryResponse] = Field(default_factory=list)
    total: int


class FlowRunStatusCountsResponse(BaseModel):
    """How many flow runs each status holds, keyed in the Executions vocabulary."""

    counts: dict[str, int] = Field(default_factory=dict)


class FlowRunDetailResponse(FlowRunSummaryResponse):
    trigger_payload: dict[str, Any] | None = None
    trigger_context: dict[str, Any] | None = None
    steps: list[FlowRunStepResponse] = Field(default_factory=list)
    # Debugger: the node the run is paused on (breakpoint / step mode).
    paused_node_id: str | None = None
    step_mode: bool = False
    # Human-in-the-loop: the Ask-user node awaiting an answer, and its
    # question — what the respond box displays.
    awaiting_input_node_id: str | None = None
    awaiting_input_question: str | None = None
    # The structured ask of an Ask-user node with options (#842, S6): question,
    # options, recommendation, deadline. None for a free-text node.
    awaiting_input_ask: dict[str, Any] | None = None
